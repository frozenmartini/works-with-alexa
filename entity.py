"""Shared entity base for Works with Alexa.

Every entity is one feature of one endpoint, addressed by ``instance``. Omitting
``instance`` is the single reason the reference Homebridge plugins cannot drive a
fan speed (ref/06, ref/07), so it is threaded through every write here.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WorksWithAlexaCoordinator
from .models import Endpoint, Feature
from .protocol import WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)


class WorksWithAlexaEntity(CoordinatorEntity[WorksWithAlexaCoordinator]):
    """One feature of one endpoint."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator)
        self._endpoint_id = endpoint.endpoint_id
        self._feature_key = feature.key
        self._feature_name = feature.name
        self._instance = feature.instance

        self._attr_unique_id = f"{endpoint.endpoint_id}:{feature.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, endpoint.endpoint_id)},
            name=endpoint.friendly_name,
            manufacturer=endpoint.manufacturer,
            model=endpoint.model,
            serial_number=endpoint.serial_number,
        )

    @property
    def endpoint(self) -> Endpoint | None:
        return self.coordinator.data.get(self._endpoint_id)

    @property
    def feature(self) -> Feature | None:
        endpoint = self.endpoint
        if endpoint is None:
            return None
        return endpoint.features.get(self._feature_key)

    @property
    def available(self) -> bool:
        return super().available and self.feature is not None

    @property
    def live_value(self) -> Any:
        """Current value with the power-off sentinel filtered out."""
        endpoint, feature = self.endpoint, self.feature
        if endpoint is None or feature is None:
            return None
        return endpoint.live_value(feature)

    async def async_control(
        self,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        feature_name: str | None = None,
        instance: str | None = None,
    ) -> None:
        """Drive this feature, then refresh so the UI reflects the device.

        The blocked check is defence in depth. Platforms already skip blocked
        endpoints at discovery, but a write is irreversible in the physical
        world -- the Kohler Anthem runs water -- so it is re-checked at the last
        moment before the request leaves.
        """
        endpoint = self.endpoint
        if endpoint is not None and endpoint.blocked:
            raise HomeAssistantError(
                f"{endpoint.friendly_name} is read-only and will not be written to"
            )

        try:
            await self.coordinator.session.async_set_feature(
                self._endpoint_id,
                feature_name or self._feature_name,
                operation,
                instance=instance if feature_name else self._instance,
                payload=payload,
            )
        except WorksWithAlexaError as err:
            raise HomeAssistantError(
                f"{operation} failed on {self._feature_key}: {err}"
            ) from err

        await self.coordinator.async_request_refresh()

"""Shared entity base for Works with Alexa.

Every entity is one feature of one endpoint, addressed by ``instance``. Omitting
``instance`` is the single reason the reference Homebridge plugins cannot drive a
fan speed (ref/06, ref/07), so it is threaded through every write here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WorksWithAlexaCoordinator
from .models import Endpoint, Feature
from .protocol import WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

# How long an unconfirmed local value is trusted. Long enough for the device to
# report back through a poll or two, short enough that a command the device
# silently ignored heals itself rather than showing a lie indefinitely.
OPTIMISTIC_TIMEOUT = 30.0

# Distinguishes "assume nothing" from "assume None", which is a real state.
_UNSET: Any = object()


def _same_value(reported: Any, assumed: Any) -> bool:
    """Whether the device now reports what we assumed.

    Numeric where both sides are numeric: a setpoint sent as ``74`` reads back
    as ``74.0``, and comparing those as text would never match, leaving the
    assumption to sit until it timed out instead of reconciling.
    """
    if reported is None or assumed is None:
        return reported is assumed
    try:
        return float(reported) == float(assumed)
    except (TypeError, ValueError):
        return str(reported) == str(assumed)


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
        # feature key -> (assumed value, monotonic deadline)
        self._optimistic: dict[str, tuple[Any, float]] = {}

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

    def feature_value(self, key: str | None) -> Any:
        """Value of one feature, preferring an unconfirmed local write.

        Reads go through here rather than straight to the endpoint so an
        optimistic value shows immediately after a command, instead of the UI
        snapping back to the old state until the next poll lands.
        """
        if key is None:
            return None
        assumed = self._optimistic.get(key)
        if assumed is not None and time.monotonic() < assumed[1]:
            return assumed[0]

        endpoint = self.endpoint
        if endpoint is None:
            return None
        feature = endpoint.features.get(key)
        if feature is None:
            return None
        return endpoint.live_value(feature)

    @property
    def live_value(self) -> Any:
        """Current value with the power-off sentinel filtered out."""
        return self.feature_value(self._feature_key)

    def _assume(self, key: str, value: Any) -> None:
        """Hold a value locally until the device confirms it or it goes stale."""
        self._optimistic[key] = (value, time.monotonic() + OPTIMISTIC_TIMEOUT)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop assumptions the device has caught up with, or that went stale."""
        endpoint = self.endpoint
        now = time.monotonic()
        for key, (value, expires) in list(self._optimistic.items()):
            feature = endpoint.features.get(key) if endpoint else None
            confirmed = feature is not None and _same_value(
                endpoint.live_value(feature), value
            )
            if confirmed or now >= expires:
                del self._optimistic[key]
        super()._handle_coordinator_update()

    async def async_control(
        self,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        feature_name: str | None = None,
        instance: str | None = None,
        assume: Any = _UNSET,
    ) -> None:
        """Drive this feature, then refresh so the UI reflects the device.

        ``assume`` is the value this command is expected to produce. It is held
        as the entity's state from the moment the device accepts the command
        until a poll confirms it, so the UI does not lag a cycle behind. The
        device may take a few seconds to report the new value back, which is
        exactly the gap this covers.

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

        target_key = self._feature_key
        if feature_name:
            target_key = f"{feature_name}/{instance}" if instance else feature_name

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

        # Only assume once the device has actually accepted the command; a
        # failed write must not leave the UI showing a state that never happened.
        if assume is not _UNSET:
            self._assume(target_key, assume)
            self.async_write_ha_state()

        await self.coordinator.async_request_refresh()

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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Amazon's reachability flag, as information rather than availability.

        ``connectivity`` is the signal behind the Alexa app's "device is
        unresponsive", but on 2026-09-13 both Dreo fans reported UNREACHABLE
        while also reporting live-looking state. Until that is understood, a
        wrong "unavailable" would block control, whereas a wrong "available"
        only produces a failed command with a clear error.
        """
        endpoint = self.endpoint
        if endpoint is None or endpoint.reachable is None:
            return None
        return {"reachable": endpoint.reachable}

    def _reported(self, key: str) -> Any:
        """What the endpoint currently reports for an optimistic key.

        A key is either a feature key (``range/Fan.Speed``) or a feature key
        with a property appended (``thermostat/targetSetpoint``). Feature keys
        carry slashes of their own, so the whole key is tried as a feature
        first and only split when it is not one.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return None
        feature = endpoint.features.get(key)
        if feature is not None:
            return endpoint.live_value(feature)
        base, _, prop = key.rpartition("/")
        feature = endpoint.features.get(base) if base else None
        return feature.property_value(prop) if feature is not None else None

    def feature_property(self, prop: str) -> Any:
        """One named property of this entity's feature, assumption first.

        The thermostat sub-entities are a property of a shared feature rather
        than a feature of their own, so they need this instead of
        :meth:`feature_value` to see their own optimistic writes.
        """
        assumed = self._optimistic.get(f"{self._feature_key}/{prop}")
        if assumed is not None and time.monotonic() < assumed[1]:
            return assumed[0]
        feature = self.feature
        return feature.property_value(prop) if feature is not None else None

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
            confirmed = endpoint is not None and _same_value(self._reported(key), value)
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
        assume_property: str | None = None,
    ) -> None:
        """Drive this feature, then refresh so the UI reflects the device.

        ``assume`` is the value this command is expected to produce, held as the
        entity's state until a poll confirms it or it goes stale, so the UI does
        not lag a cycle behind. ``assume_property`` names a property of the
        feature rather than the feature itself, for the thermostat sub-entities.

        The assumption is applied **before** the request leaves, not after it
        lands. Amazon's round trip is a few hundred milliseconds, and anything
        driving an entity faster than that -- a HomeKit slider being dragged --
        otherwise reads back the value from two writes ago and fights the user.
        A command that fails rolls the assumption back, so a write that never
        happened still cannot leave a lie on screen.

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
        if assume_property:
            target_key = f"{target_key}/{assume_property}"

        if assume is not _UNSET:
            previous = self._optimistic.get(target_key, _UNSET)
            self._assume(target_key, assume)
            self.async_write_ha_state()

        try:
            await self.coordinator.session.async_set_feature(
                self._endpoint_id,
                feature_name or self._feature_name,
                operation,
                instance=instance if feature_name else self._instance,
                payload=payload,
            )
        except WorksWithAlexaError as err:
            if assume is not _UNSET:
                # The command never happened. Put back whatever was showing.
                if previous is _UNSET:
                    self._optimistic.pop(target_key, None)
                else:
                    self._optimistic[target_key] = previous
                self.async_write_ha_state()
            raise HomeAssistantError(
                f"{operation} failed on {self._feature_key}: {err}"
            ) from err

        await self.coordinator.async_request_refresh()

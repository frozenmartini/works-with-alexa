"""Shared entity base for Works with Alexa.

Every entity is one feature of one endpoint, addressed by ``instance``. Omitting
``instance`` is the single reason the reference Homebridge plugins cannot drive a
fan speed (ref/06, ref/07), so it is threaded through every write here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WorksWithAlexaCoordinator
from .dev_logging import event
from .discovery import feature_metadata
from .features import entity_name
from .models import Endpoint, Feature
from .protocol import WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

# How long an unconfirmed local value is trusted. Long enough for the device to
# report back through a poll or two, short enough that a command the device
# silently ignored heals itself rather than showing a lie indefinitely.
OPTIMISTIC_TIMEOUT = 30.0

# ``thermostat`` and ``temperatureSensor`` declare their scale explicitly; a bare
# ``range`` feature does not, even when its unit is "Degrees" (ref/07, Q3).
TEMPERATURE_UNITS = {
    "CELSIUS": UnitOfTemperature.CELSIUS,
    "FAHRENHEIT": UnitOfTemperature.FAHRENHEIT,
    "KELVIN": UnitOfTemperature.KELVIN,
}


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


class _OptimisticState:
    """One pending value, retaining earlier attempts for correct rollback."""

    def __init__(self, value: Any, deadline: float, previous: _OptimisticState | None = None) -> None:
        self.value = value
        self.deadline = deadline
        self.previous = previous
        self.rejected = False


class WorksWithAlexaEntity(CoordinatorEntity[WorksWithAlexaCoordinator]):
    """One feature of one endpoint, or one property of that feature, as Home Assistant sees it."""

    _attr_has_entity_name = True
    _wwa_operations: frozenset[str] = frozenset()

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
        prop: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._endpoint_id = endpoint.endpoint_id
        # The setup-time feature: its name and instance go on the wire, and it
        # stands in for attributes while the endpoint is missing from a poll.
        self._source = feature
        self._prop = prop
        self._key = f"{feature.key}/{prop}" if prop else feature.key
        self._pending: _OptimisticState | None = None
        self._cancel_expiry: Callable[[], None] | None = None
        self._removed = False

        self._attr_unique_id = f"{endpoint.endpoint_id}:{self._key}"
        self._attr_name = entity_name(feature, prop)
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
        return endpoint.features.get(self._source.key)

    @property
    def available(self) -> bool:
        return super().available and self.feature is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Discovery metadata and reachability, independent of availability.

        ``connectivity`` is the signal behind the Alexa app's "device is
        unresponsive", but on 2026-09-13 both Dreo fans reported UNREACHABLE
        while also reporting live-looking state. Until that is understood, a
        wrong "unavailable" would block control, whereas a wrong "available"
        only produces a failed command with a clear error.
        """
        endpoint = self.endpoint
        feature = self.feature or self._source
        attrs: dict[str, Any] = {
            "wwa_feature": feature_metadata(
                feature,
                writable=bool(self._wwa_operations) and self._wwa_operations <= feature.operations,
                prop=self._prop,
            )
        }
        if endpoint is not None and endpoint.reachable is not None:
            attrs["reachable"] = endpoint.reachable
        return attrs

    @property
    def reading(self) -> Any:
        """This entity's value, preferring an unconfirmed local write.

        Reads go through here rather than straight to the endpoint so an
        optimistic value shows immediately after a command, instead of the UI
        snapping back to the old state until the next poll lands.
        """
        pending = self._pending
        if pending is not None and time.monotonic() < pending.deadline:
            return pending.value
        return self._reported()

    def _reported(self) -> Any:
        """What the endpoint currently reports for this entity.

        A whole feature reads with the power-off sentinel filtered out. A
        property of a feature (the thermostat sub-entities) reads that one
        property, unfiltered.
        """
        feature = self.feature
        if feature is None:
            return None
        if self._prop:
            return feature.property_value(self._prop)
        return self.endpoint.live_value(feature)

    def _assume(self, value: Any) -> _OptimisticState:
        """Hold a value locally until the device confirms it or it goes stale."""
        record = _OptimisticState(value, time.monotonic() + OPTIMISTIC_TIMEOUT, self._pending)
        self._pending = record
        self._schedule_expiry()
        event(_LOGGER, "optimistic.applied", feature=self._key)
        return record

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._on_removed)

    @callback
    def _on_removed(self) -> None:
        # A write still in flight can fail after this and roll back; it must
        # not arm a timer that nothing would cancel.
        self._removed = True
        self._cancel_timer()

    @callback
    def _cancel_timer(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _schedule_expiry(self) -> None:
        """Revert a stale assumption even when the polling interval is longer."""
        self._cancel_timer()
        if self._pending is not None and not self._removed:
            self._cancel_expiry = async_call_later(
                self.hass, max(0.0, self._pending.deadline - time.monotonic()), self._expire
            )

    @callback
    def _expire(self, _now: Any) -> None:
        self._cancel_expiry = None
        now = time.monotonic()
        expired = self._pending is not None and now >= self._pending.deadline
        if expired:
            self._pending = None
            event(_LOGGER, "optimistic.expired", feature=self._key)
        self._schedule_expiry()
        if expired:
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop an assumption the device has caught up with, or that went stale."""
        now = time.monotonic()
        record = self._pending
        if record is not None:
            confirmed = _same_value(self._reported(), record.value)
            if confirmed or now >= record.deadline:
                self._pending = None
                event(_LOGGER, "optimistic.confirmed" if confirmed else "optimistic.expired", feature=self._key)
        self._schedule_expiry()
        super()._handle_coordinator_update()

    async def async_control(self, operation: str, payload: dict[str, Any] | None, assume: Any) -> None:
        """Drive this feature, then refresh so the UI reflects the device.

        ``assume`` is the value this command is expected to produce, held as the
        entity's state until a poll confirms it or it goes stale, so the UI does
        not lag a cycle behind.

        The assumption is applied **before** the request leaves, not after it
        lands. Amazon's round trip is a few hundred milliseconds, and anything
        driving an entity faster than that -- a HomeKit slider being dragged --
        otherwise reads back the value from two writes ago and fights the user.
        A command that fails rolls the assumption back, so a write that never
        happened still cannot leave a lie on screen.

        Capability classification happens centrally in features.py; each platform
        exposes only the commands it supports.
        """
        record = self._assume(assume)
        self.async_write_ha_state()

        try:
            await self.coordinator.session.async_set_feature(
                self._endpoint_id,
                self._source.name,
                operation,
                instance=self._source.instance,
                payload=payload,
            )
        except WorksWithAlexaError as err:
            record.rejected = True
            if self._pending is record:
                # Older failures must neither overwrite a newer command nor
                # be resurrected if the newer command also fails later.
                previous = record.previous
                while previous is not None and previous.rejected:
                    previous = previous.previous
                self._pending = previous
                self._schedule_expiry()
                event(_LOGGER, "optimistic.rolled_back", feature=self._key)
                self.async_write_ha_state()
            raise HomeAssistantError(
                f"{operation} failed on {self._source.key}: {err}"
            ) from err

        await self.coordinator.async_request_refresh()

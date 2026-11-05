"""Number entities for Works with Alexa.

Every writable ``range`` feature becomes a number -- the Window Fan's "Set
Temperature" (60..90 step 1, unit "Degrees" with no Celsius/Fahrenheit
discriminator, ref/07), the Dreo fans' speed ranges, the SmartDrape's "Opening"
0..100 Percent. The bounds come from the device's own supportedRange, never from
a constant -- they are device-specific.

A writable ``brightness`` feature becomes a number too. It is the one exception
to device bounds: Alexa sends no configuration for it, and its 0..100 is fixed by
the API.

A writable ``colorTemperature`` feature becomes a number in kelvin. Alexa reports
no bounds for it either, so these are the Smart Home API's; the device rejects or
falls back from what it cannot do.

A thermostat's setpoints become numbers too, one per setpoint the device
reports (target, lower, upper), so the companion integration kitbash can
assemble a climate from them. Those writes were verified from Home Assistant on
an ecobee on 2026-10-21.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import TEMPERATURE_UNITS, WorksWithAlexaEntity, selected_entities
from .features import SET_BRIGHTNESS, SET_COLOR_TEMPERATURE, SET_TARGET_SETPOINT
from .models import FEATURE_BRIGHTNESS, FEATURE_COLOR_TEMPERATURE, Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

# Alexa reports no bounds for a setpoint (the schema has an allowed-range
# property, but the ecobee does not send it), so these bound the slider, not
# the device. Widest sane values per scale; the device rejects what it cannot do.
_SETPOINT_BOUNDS = {
    UnitOfTemperature.FAHRENHEIT: (40.0, 100.0, 1.0),
    UnitOfTemperature.CELSIUS: (5.0, 38.0, 0.5),
    UnitOfTemperature.KELVIN: (278.0, 311.0, 0.5),
}

# A range setpoint travels as a pair on the wire (Smart Home API
# SetTargetTemperature), so writing one half sends the other half unchanged.
_SETPOINT_PAIR = {"lowerSetpoint": "upperSetpoint", "upperSetpoint": "lowerSetpoint"}


def _float_or_none(value: Any) -> float | None:
    """A reading as a float, or None when there is none or it is not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    # A bounded, writable range is a number; a thermostat's setpoints are one number
    # each, raw. Each half of a range pair knows the other, so a write sends the
    # other's pending value.
    entities: list[NumberEntity] = []
    setpoints: dict[tuple[str, str], WorksWithAlexaSetpoint] = {}
    for endpoint, feature, prop in selected_entities(coordinator, "number"):
        if feature.name == FEATURE_BRIGHTNESS:
            entities.append(WorksWithAlexaBrightness(coordinator, endpoint, feature))
        elif feature.name == FEATURE_COLOR_TEMPERATURE:
            entities.append(WorksWithAlexaColorTemperature(coordinator, endpoint, feature))
        elif prop is None:
            entities.append(WorksWithAlexaNumber(coordinator, endpoint, feature))
        else:
            setpoint = WorksWithAlexaSetpoint(coordinator, endpoint, feature, prop)
            setpoints[(endpoint.endpoint_id, prop)] = setpoint
            entities.append(setpoint)
    for (endpoint_id, prop), setpoint in setpoints.items():
        if pair := _SETPOINT_PAIR.get(prop):
            setpoint.sibling = setpoints.get((endpoint_id, pair))

    _LOGGER.debug("Adding %d numbers", len(entities))
    async_add_entities(entities)


class WorksWithAlexaNumber(WorksWithAlexaEntity, NumberEntity):
    """One writable range feature."""

    _attr_mode = NumberMode.BOX
    _wwa_operations = frozenset({"setRangeValue"})

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_native_min_value = feature.minimum
        self._attr_native_max_value = feature.maximum
        self._attr_native_step = feature.step or 1
        # Deliberately no device class or unit: "Degrees" carries no scale, and
        # guessing one would misreport the value outright (ref/07 Q3).

    @property
    def native_value(self) -> float | None:
        """Current value, or None while the device reports the off sentinel."""
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        # The API echoes back what it accepted; send an int where the step is
        # whole, since that is the shape confirmed against the device.
        payload = int(value) if float(value).is_integer() else value
        await self.async_control(
            "setRangeValue", {"rangeValue": payload}, assume=payload
        )


class WorksWithAlexaBrightness(WorksWithAlexaEntity, NumberEntity):
    """An endpoint's brightness, as a percentage.

    Amazon rejects values outside 0..100 with INVALID_VALUE. The ``{"brightness":
    45}`` payload is what public clients of this API send, captured from the Alexa
    web UI. Like a range, it reads unknown while the device reports the off sentinel.
    """

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _wwa_operations = frozenset({SET_BRIGHTNESS})

    @property
    def native_value(self) -> float | None:
        """Current level, or None while the device reports the off sentinel."""
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        level = round(value)
        await self.async_control(SET_BRIGHTNESS, {"brightness": level}, assume=level)


class WorksWithAlexaColorTemperature(WorksWithAlexaEntity, NumberEntity):
    """An endpoint's color temperature, in kelvin.

    Deliberately no device class: it would offer unit conversion, and this is a
    color, not a temperature reading. The ``{"colorTemperatureInKelvin": 3000}``
    payload is what public clients of this API send. A value the device falls back
    from shows what it really kept once the 30 second assumption expires.
    """

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1000
    _attr_native_max_value = 10000
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "K"
    _wwa_operations = frozenset({SET_COLOR_TEMPERATURE})

    @property
    def native_value(self) -> float | None:
        """Current kelvin, or None while the device reports the off sentinel."""
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        kelvin = round(value)
        await self.async_control(
            SET_COLOR_TEMPERATURE, {"colorTemperatureInKelvin": kelvin}, assume=kelvin
        )


class WorksWithAlexaSetpoint(WorksWithAlexaEntity, NumberEntity):
    """One setpoint of a thermostat feature, raw.

    Written with ``setTargetSetpoint`` keyed on the property name -- the rule
    every confirmed write follows (ref/07) -- carrying ``{value, scale}`` in the
    scale the device reports in. Verified from Home Assistant on an ecobee on
    2026-10-21: lower and upper writes were confirmed by the next poll. A
    target write in AUTO moved both lower and upper on the device, and the
    target itself was not reported back.
    """

    _attr_mode = NumberMode.BOX
    _wwa_operations = frozenset({SET_TARGET_SETPOINT})
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
        prop: str,
    ) -> None:
        super().__init__(coordinator, endpoint, feature, prop)
        self._scale = (feature.property_scale(prop) or feature.scale or "CELSIUS").upper()
        unit = TEMPERATURE_UNITS.get(self._scale, UnitOfTemperature.CELSIUS)
        self._attr_native_unit_of_measurement = unit
        low, high, step = _SETPOINT_BOUNDS.get(unit, _SETPOINT_BOUNDS[UnitOfTemperature.CELSIUS])
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self.sibling: WorksWithAlexaSetpoint | None = None

    @property
    def native_value(self) -> float | None:
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        payload = {self._prop: {"value": value, "scale": self._scale}}
        # The other half is read through its own entity, so an unconfirmed
        # write to it is sent, not the polled value it replaced.
        if self.sibling is not None and (current := self.sibling.native_value) is not None:
            payload[self.sibling._prop] = {"value": current, "scale": self._scale}
        await self.async_control(
            SET_TARGET_SETPOINT, payload, assume=value
        )

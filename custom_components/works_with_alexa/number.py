"""Number entities for Works with Alexa.

Every writable ``range`` feature becomes a number -- the Window Fan's "Set
Temperature" (60..90 step 1, unit "Degrees" with no Celsius/Fahrenheit
discriminator, ref/07), the Dreo fans' speed ranges, the SmartDrape's "Opening"
0..100 Percent. The bounds come from the device's own supportedRange, never from
a constant -- they are device-specific.

Brightness, colour temperature, percentage and power level become numbers
with Alexa's fixed bounds (``features.FIXED_RANGES``): the device declares none.
A colour becomes three numbers, hue, saturation and brightness, written
together.

A thermostat's setpoints become numbers too, one per setpoint the device
reports (target, lower, upper), so the companion integration kitbash can
assemble a climate from them. Those writes were verified from Home Assistant on
an ecobee on 2026-10-21.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import TEMPERATURE_UNITS, WorksWithAlexaEntity, selected_entities
from .features import COLOR_BOUNDS, FIXED_RANGES, SET_COLOR, SET_TARGET_SETPOINT
from .models import FEATURE_COLOR, Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

# A setpoint's bounds are Alexa's allowed range when the device sends one
# (`Endpoint.setpoint_bounds`), else this fallback; the step is always from
# here. Wide enough for frost protection and a hot shower; the device rejects
# what it cannot do.
_SETPOINT_BOUNDS = {
    UnitOfTemperature.FAHRENHEIT: (40.0, 120.0, 1.0),
    UnitOfTemperature.CELSIUS: (5.0, 50.0, 0.1),
    UnitOfTemperature.KELVIN: (278.0, 323.0, 0.1),
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


def _on_steps(low: float, high: float, step: float) -> tuple[float, float] | None:
    """A range moved inward onto the step grid, or ``None`` if no step fits.

    A range converted between scales comes out off the grid and with float
    noise (45 degF is 7.2222 degC); rounding inward keeps every offered value
    inside what Alexa allows.
    """
    digits = max(0, -Decimal(str(step)).as_tuple().exponent)
    low = round(math.ceil(round(low / step, 6)) * step, digits)
    high = round(math.floor(round(high / step, 6)) * step, digits)
    return (low, high) if low <= high else None


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
    colors: dict[str, dict[str, WorksWithAlexaColorComponent]] = {}
    for endpoint, feature, prop in selected_entities(coordinator, "number"):
        if feature.name in FIXED_RANGES:
            entities.append(WorksWithAlexaFixedRangeNumber(coordinator, endpoint, feature))
        elif feature.name == FEATURE_COLOR:
            component = WorksWithAlexaColorComponent(coordinator, endpoint, feature, prop)
            # One dict per endpoint, shared by its three components.
            component.components = colors.setdefault(endpoint.endpoint_id, {})
            component.components[prop] = component
            entities.append(component)
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


class WorksWithAlexaFixedRangeNumber(WorksWithAlexaEntity, NumberEntity):
    """One writable value on Alexa's fixed scale: brightness, colour temperature,
    percentage or power level.

    Written as ``{<property>: int}`` with the feature's set operation. The
    payload keys follow the confirmed bare-property-name rule; none was yet
    seen accepted live (2026-11-07).
    """

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._fixed = FIXED_RANGES[feature.name]
        self._wwa_operations = frozenset({self._fixed.operation})
        self._attr_native_min_value = self._fixed.minimum
        self._attr_native_max_value = self._fixed.maximum
        self._attr_native_step = self._fixed.step
        self._attr_native_unit_of_measurement = self._fixed.unit

    @property
    def native_value(self) -> float | None:
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        payload = round(value)
        await self.async_control(
            self._fixed.operation, {self._fixed.payload_key: payload}, assume=payload
        )


class WorksWithAlexaColorComponent(WorksWithAlexaEntity, NumberEntity):
    """One component of a colour feature: hue, saturation or brightness, raw.

    ``setColor`` takes all three at once as ``{"color": {hue, saturation,
    brightness}}``, so a write sends the other two components' values as their
    own entities read them: an unconfirmed write to one is sent, not the polled
    value it replaced. Like a setpoint, a component shows what Alexa reports
    even while the device is off: a bulb that is off keeps reporting its last
    colour (2026-11-07). The payload shape follows Alexa's directive
    but has not yet been seen accepted live (2026-11-07).
    """

    _attr_mode = NumberMode.BOX
    _wwa_operations = frozenset({SET_COLOR})

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
        prop: str,
    ) -> None:
        super().__init__(coordinator, endpoint, feature, prop)
        low, high, step = COLOR_BOUNDS[prop]
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self.components: dict[str, WorksWithAlexaColorComponent] = {prop: self}

    def _reported(self) -> Any:
        feature = self.feature
        return None if feature is None else feature.color_component(self._prop)

    @property
    def native_value(self) -> float | None:
        return _float_or_none(self.reading)

    async def async_set_native_value(self, value: float) -> None:
        color: dict[str, float] = {}
        for name, component in self.components.items():
            current = value if component is self else component.native_value
            if current is None:
                # Sending a guess would change the light; refuse instead.
                raise HomeAssistantError(
                    f"{SET_COLOR} needs the colour's {name}, which Alexa has not reported"
                )
            color[name] = current
        await self.async_control(SET_COLOR, {"color": color}, assume=value)


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
        if (alexa := endpoint.setpoint_bounds(feature, prop)) is not None:
            low, high = _on_steps(*alexa, step) or (low, high)
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

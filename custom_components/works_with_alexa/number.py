"""Number entities for Works with Alexa.

Every writable ``range`` feature becomes a number -- the Window Fan's "Set
Temperature" (60..90 step 1, unit "Degrees" with no Celsius/Fahrenheit
discriminator, ref/07), the Dreo fans' speed ranges, the SmartDrape's "Opening"
0..100 Percent. The bounds come from the device's own supportedRange, never from
a constant -- they are device-specific.

A thermostat's setpoints become numbers too, one per setpoint the device
reports (target, lower, upper), so the owner's second integration can assemble
a climate from them. Those writes have not yet been executed against a device.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .features import SET_TARGET_SETPOINT, number_features, setpoint_properties
from .models import Endpoint, Feature, humanise

_LOGGER = logging.getLogger(__name__)

_SCALE_UNITS = {
    "CELSIUS": UnitOfTemperature.CELSIUS,
    "FAHRENHEIT": UnitOfTemperature.FAHRENHEIT,
    "KELVIN": UnitOfTemperature.KELVIN,
}

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[NumberEntity] = []
    for endpoint in coordinator.selected.values():
        # Bounded and writable; without bounds there is nothing sane to render a
        # slider against.
        for feature in number_features(endpoint):
            entities.append(WorksWithAlexaNumber(coordinator, endpoint, feature))
        # A thermostat's setpoints, one number each, raw.
        for feature, prop in setpoint_properties(endpoint):
            entities.append(WorksWithAlexaSetpoint(coordinator, endpoint, feature, prop))

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
        self._attr_name = feature.label
        self._attr_native_min_value = feature.minimum
        self._attr_native_max_value = feature.maximum
        self._attr_native_step = feature.step or 1
        # Deliberately no device class or unit: "Degrees" carries no scale, and
        # guessing one would misreport the value outright (ref/07 Q3).

    @property
    def native_value(self) -> float | None:
        """Current setpoint, or None while the device reports the off sentinel."""
        value = self.live_value
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        # The API echoes back what it accepted; send an int where the step is
        # whole, since that is the shape confirmed against the device.
        payload = int(value) if float(value).is_integer() else value
        await self.async_control(
            "setRangeValue", {"rangeValue": payload}, assume=payload
        )


class WorksWithAlexaSetpoint(WorksWithAlexaEntity, NumberEntity):
    """One setpoint of a thermostat feature, raw.

    Written with ``setTargetSetpoint`` keyed on the property name -- the rule
    every confirmed write follows (ref/07) -- carrying ``{value, scale}`` in the
    scale the device reports in. Verified on an ecobee on 2026-10-21: lower and
    upper writes were confirmed by the next poll. A target write in AUTO moved
    both lower and upper on the device, and the target itself was not reported
    back.
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
        super().__init__(coordinator, endpoint, feature)
        self._prop = prop
        self._wwa_property = prop
        self._attr_unique_id = f"{endpoint.endpoint_id}:{feature.key}/{prop}"
        self._attr_name = humanise(prop)
        self._scale = (feature.property_scale(prop) or feature.scale or "CELSIUS").upper()
        unit = _SCALE_UNITS.get(self._scale, UnitOfTemperature.CELSIUS)
        self._attr_native_unit_of_measurement = unit
        low, high, step = _SETPOINT_BOUNDS.get(unit, _SETPOINT_BOUNDS[UnitOfTemperature.CELSIUS])
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step

    @property
    def native_value(self) -> float | None:
        value = self.feature_property(self._prop)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        payload = {self._prop: {"value": value, "scale": self._scale}}
        pair = _SETPOINT_PAIR.get(self._prop)
        feature = self.feature
        if pair and feature is not None and (current := feature.property_value(pair)) is not None:
            payload[pair] = {"value": float(current), "scale": self._scale}
        await self.async_control(
            SET_TARGET_SETPOINT, payload, assume=value, assume_property=self._prop
        )

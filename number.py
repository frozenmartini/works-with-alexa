"""Number entities for Works with Alexa.

Every writable ``range`` feature becomes a number. The Window Fan's "Set
Temperature" is the worked example: 60..90 step 1, unit "Degrees" with no
Celsius/Fahrenheit discriminator (ref/07). The bounds come from the device's
own supportedRange, never from a constant -- they are device-specific.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .const import CONTROLLABLE_CATEGORIES
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

FEATURE_RANGE = "range"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[WorksWithAlexaNumber] = []
    for endpoint in coordinator.data.values():
        if endpoint.blocked or endpoint.display_category not in CONTROLLABLE_CATEGORIES:
            continue
        for feature in endpoint.features_named(FEATURE_RANGE):
            # Without bounds there is nothing sane to render a slider against.
            if not feature.writable or feature.minimum is None or feature.maximum is None:
                continue
            entities.append(WorksWithAlexaNumber(coordinator, endpoint, feature))

    _LOGGER.debug("Adding %d numbers", len(entities))
    async_add_entities(entities)


class WorksWithAlexaNumber(WorksWithAlexaEntity, NumberEntity):
    """One writable range feature."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_name = feature.friendly_name or feature.key
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

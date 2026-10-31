"""Read-only entities for Works with Alexa.

A feature becomes a sensor when it reports a value and is read-only, or its
value represents action status. Connectivity feeds the reachable attribute
instead. The central capability plan assigns writable controls to their own
platforms, independent of manufacturer or device category.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import TEMPERATURE_UNITS, WorksWithAlexaEntity, selected_entities
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per readable feature."""
    coordinator = entry.runtime_data

    # Shared with the device picker, so what the picker offers and what this
    # actually builds cannot drift apart.
    entities = [
        WorksWithAlexaSensor(coordinator, endpoint, feature)
        for endpoint, feature, _ in selected_entities(coordinator, "sensor")
    ]

    _LOGGER.debug("Adding %d sensors", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSensor(WorksWithAlexaEntity, SensorEntity):
    """One readable feature of one endpoint."""

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)

        raw = feature.raw_value if isinstance(feature.raw_value, dict) else {}
        if unit := TEMPERATURE_UNITS.get((feature.scale or "").upper()):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = unit
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif raw.get("type") == "Percentage":
            # An inventory level (a vacuum's filter or brush) measured in percent.
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif isinstance(feature.value, (int, float)):
            # Numeric, but the device never said what the number means. Publish
            # it without a device class rather than guessing a scale; the raw
            # unit is kept as an attribute so nothing is lost.
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        """Current reading, with the power-off sentinel filtered out."""
        return self.reading

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        base = super().extra_state_attributes
        feature = self.feature or self._source
        attrs: dict[str, Any] = {"wwa_feature": base["wwa_feature"]}
        # Only meaningful where the scale is undeclared; where it is declared the
        # unit is already on the entity itself.
        if feature.unit_of_measure and not feature.scale:
            attrs["reported_unit"] = feature.unit_of_measure
        if "reachable" in base:
            attrs["reachable"] = base["reachable"]
        return attrs

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
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WorksWithAlexaConfigEntry
from .const import DOMAIN
from .coordinator import WorksWithAlexaCoordinator
from .models import Endpoint, Feature, sensor_features

_LOGGER = logging.getLogger(__name__)

# ``thermostat`` and ``temperatureSensor`` declare their scale explicitly; a bare
# ``range`` feature does not, even when its unit is "Degrees" (ref/07, Q3).
_SCALE_UNITS = {
    "CELSIUS": UnitOfTemperature.CELSIUS,
    "FAHRENHEIT": UnitOfTemperature.FAHRENHEIT,
    "KELVIN": UnitOfTemperature.KELVIN,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per readable feature."""
    coordinator = entry.runtime_data

    entities: list[WorksWithAlexaSensor] = []
    for endpoint in coordinator.selected.values():
        # Shared with the device picker, so what the picker offers and what this
        # actually builds cannot drift apart.
        for feature in sensor_features(endpoint):
            entities.append(WorksWithAlexaSensor(coordinator, endpoint, feature))

    _LOGGER.debug("Adding %d sensors", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSensor(CoordinatorEntity[WorksWithAlexaCoordinator], SensorEntity):
    """One readable feature of one endpoint."""

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

        self._attr_unique_id = f"{endpoint.endpoint_id}:{feature.key}"
        self._attr_name = feature.label
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, endpoint.endpoint_id)},
            name=endpoint.friendly_name,
            manufacturer=endpoint.manufacturer,
            model=endpoint.model,
            serial_number=endpoint.serial_number,
        )

        raw = feature.raw_value if isinstance(feature.raw_value, dict) else {}
        if unit := _SCALE_UNITS.get((feature.scale or "").upper()):
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
    def _feature(self) -> Feature | None:
        endpoint = self.coordinator.data.get(self._endpoint_id)
        if endpoint is None:
            return None
        return endpoint.features.get(self._feature_key)

    @property
    def available(self) -> bool:
        return super().available and self._feature is not None

    @property
    def native_value(self) -> Any:
        """Current reading, with the power-off sentinel filtered out."""
        endpoint = self.coordinator.data.get(self._endpoint_id)
        feature = self._feature
        if endpoint is None or feature is None:
            return None
        return endpoint.live_value(feature)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        feature = self._feature
        if feature is None:
            return None
        attrs: dict[str, Any] = {"feature": feature.key}
        # Only meaningful where the scale is undeclared; where it is declared the
        # unit is already on the entity itself.
        if feature.unit_of_measure and not feature.scale:
            attrs["reported_unit"] = feature.unit_of_measure
        endpoint = self.coordinator.data.get(self._endpoint_id)
        if endpoint is not None and endpoint.reachable is not None:
            attrs["reachable"] = endpoint.reachable
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

"""Read-only entities for Works with Alexa.

A feature becomes a sensor when it reports a value and is read-only, or its
value represents action status. Connectivity feeds the reachable attribute
instead. ``features.raw_entities`` assigns writable controls to their own
platforms, independent of manufacturer, and of device category except that a
scene trigger yields nothing.

A read-only ``mode`` shows Alexa's label for its value, as a select does, where
Alexa sends one: a heated blanket's Status, say, which reports ``"1"`` while the
Alexa app shows a word. An option Alexa sends no label for shows its raw value.
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
from .entity import (
    TEMPERATURE_UNITS,
    UNRECORDED,
    UNRECORDED_WITH_VALUE,
    WorksWithAlexaEntity,
    selected_entities,
)
from .models import FEATURE_MODE, Endpoint, Feature

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
        WorksWithAlexaModeSensor(coordinator, endpoint, feature)
        if _mode_labels(feature)
        else WorksWithAlexaSensor(coordinator, endpoint, feature)
        for endpoint, feature, _ in selected_entities(coordinator, "sensor")
    ]

    _LOGGER.debug("Adding %d sensors", len(entities))
    async_add_entities(entities)


def _mode_labels(feature: Feature) -> dict[str, str]:
    """A mode's labels by value, for the options Alexa actually labels.

    ``ModeOption.friendly_name`` falls back to the value itself, so an option
    whose label equals its value carries no label and is left out. The first of
    any duplicate value wins, as in the select.
    """
    if feature.name != FEATURE_MODE:
        return {}
    labels: dict[str, str] = {}
    for option in feature.mode_options:
        if option.friendly_name != option.value:
            labels.setdefault(option.value, option.friendly_name)
    return labels


class WorksWithAlexaSensor(WorksWithAlexaEntity, SensorEntity):
    """One readable feature of one endpoint."""

    _unrecorded_attributes = UNRECORDED_WITH_VALUE

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
        """Current reading, as Alexa reports it."""
        return self.reading

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        base = super().extra_state_attributes
        feature = self.feature or self._source
        # `reported_unit` goes second (the recorder keeps insertion order); every
        # other attribute of the base passes through, so none is dropped here.
        attrs: dict[str, Any] = {"wwa_feature": base.pop("wwa_feature")}
        # Only meaningful where the scale is undeclared; where it is declared the
        # unit is already on the entity itself.
        if feature.unit_of_measure and not feature.scale:
            attrs["reported_unit"] = feature.unit_of_measure
        attrs.update(base)
        return attrs


class WorksWithAlexaModeSensor(WorksWithAlexaSensor):
    """A read-only mode, shown by Alexa's label for its value.

    An enum when every option has a label, so a value outside the options reads
    unknown, as in the select. Otherwise a labelled value shows its label and
    any other its raw value. Either way the state is no longer Alexa's value, so
    ``alexa_value`` is recorded, as it is for a select.
    """

    _unrecorded_attributes = UNRECORDED

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._label_of = _mode_labels(feature)
        # A label is text: never a measurement, even where the raw value is a number.
        self._attr_state_class = None
        values = [option.value for option in feature.mode_options]
        self._enum = all(value in self._label_of for value in values)
        if self._enum:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(dict.fromkeys(self._label_of[value] for value in values))

    @property
    def native_value(self) -> Any:
        """Alexa's label for the reading; unknown or raw when it has none."""
        value = self.reading
        if value is None:
            return None
        if (label := self._label_of.get(str(value))) is not None:
            return label
        # An enum's state must be one of its options; the value stays visible
        # as ``alexa_value``.
        return None if self._enum else value

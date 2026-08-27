"""Select entities for Works with Alexa.

Every writable ``mode`` feature that is not the fan's speed becomes a select.
The Window Fan's Direction is the worked example: its options are "Exhaust" and
"Direct", which Home Assistant's forward/reverse fan direction cannot express
without renaming them (ref/07).
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .const import CONTROLLABLE_CATEGORIES
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .fan import FEATURE_MODE, speed_feature
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[WorksWithAlexaSelect] = []
    for endpoint in coordinator.data.values():
        if endpoint.blocked or endpoint.display_category not in CONTROLLABLE_CATEGORIES:
            continue
        claimed = speed_feature(endpoint)
        for feature in endpoint.features_named(FEATURE_MODE):
            if not feature.writable or not feature.mode_options:
                continue
            if claimed is not None and feature.key == claimed.key:
                continue  # already the fan's percentage
            entities.append(WorksWithAlexaSelect(coordinator, endpoint, feature))

    _LOGGER.debug("Adding %d selects", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSelect(WorksWithAlexaEntity, SelectEntity):
    """One writable mode feature, presented with the device's own labels."""

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_name = feature.friendly_name or feature.key
        # Labels are what the user picks; values are what the API accepts. Alexa
        # does not promise labels are unique, so keep the first of any duplicate
        # rather than letting a later one silently shadow it.
        self._value_of: dict[str, str] = {}
        for option in feature.mode_options:
            self._value_of.setdefault(option.friendly_name, option.value)
        self._label_of = {value: label for label, value in self._value_of.items()}
        self._attr_options = list(self._value_of)

    @property
    def current_option(self) -> str | None:
        """Selected label, or None when the device reports the off sentinel."""
        value = self.live_value
        if value is None:
            return None
        return self._label_of.get(str(value))

    async def async_select_option(self, option: str) -> None:
        value = self._value_of.get(option)
        if value is None:
            return
        await self.async_control("setMode", {"mode": value}, assume=value)

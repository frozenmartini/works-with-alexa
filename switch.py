"""Switch entities for Works with Alexa.

Every writable ``toggle`` feature becomes a switch -- the Window Fan's "Auto" is
the worked example. Power is not a switch: it is the fan entity's own on/off, and
exposing it twice would let the two disagree.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .const import CONTROLLABLE_CATEGORIES
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

FEATURE_TOGGLE = "toggle"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[WorksWithAlexaSwitch] = []
    for endpoint in coordinator.data.values():
        if endpoint.blocked or endpoint.display_category not in CONTROLLABLE_CATEGORIES:
            continue
        for feature in endpoint.features_named(FEATURE_TOGGLE):
            if feature.writable:
                entities.append(WorksWithAlexaSwitch(coordinator, endpoint, feature))

    _LOGGER.debug("Adding %d switches", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSwitch(WorksWithAlexaEntity, SwitchEntity):
    """One writable toggle feature."""

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_name = feature.friendly_name or feature.key

    @property
    def is_on(self) -> bool | None:
        value = self.live_value
        if value is None:
            return None
        return str(value).upper() == "ON"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_control("turnOn")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_control("turnOff")

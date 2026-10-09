"""Switch entities for Works with Alexa.

Every writable, unclaimed ``toggle`` feature becomes a switch -- the Window
Fan's "Auto", the Dreo fans' "Mute" and "Display Auto Off". Power becomes a
switch only where no natural entity owns it: the Roborock's on/off starts and
stops a clean, and its action features advertise no operations, so a switch is
the whole of what can be driven. On a fan, power is the fan's own on/off, and
exposing it twice would let the two disagree.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .features import switch_features
from .models import FEATURE_POWER, Endpoint, Feature

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[WorksWithAlexaSwitch] = []
    for endpoint in coordinator.selected.values():
        for feature in switch_features(endpoint):
            entities.append(WorksWithAlexaSwitch(coordinator, endpoint, feature))

    _LOGGER.debug("Adding %d switches", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSwitch(WorksWithAlexaEntity, SwitchEntity):
    """One writable toggle feature, or an endpoint's power."""

    _wwa_operations = frozenset({"turnOn", "turnOff"})

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_name = "Power" if feature.name == FEATURE_POWER else feature.label

    @property
    def is_on(self) -> bool | None:
        value = self.live_value
        if value is None:
            return None
        return str(value).upper() == "ON"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_control("turnOn", assume="ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_control("turnOff", assume="OFF")

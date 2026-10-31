"""Switch entities for Works with Alexa.

An endpoint's ``power`` becomes a switch named "Power" wherever it accepts both
``turnOn`` and ``turnOff``, whatever the device is -- a fan's on/off, the
Roborock's start/stop of a clean. Every writable ``toggle`` feature becomes a
switch too, under Alexa's own label: the Window Fan's "Auto", the Dreo fans'
"Mute" and "Display Auto Off". Nothing is assembled here; a whole fan is built
from these entities in kitbash.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .entity import WorksWithAlexaEntity, selected_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities = [
        WorksWithAlexaSwitch(coordinator, endpoint, feature)
        for endpoint, feature, _ in selected_entities(coordinator, "switch")
    ]

    _LOGGER.debug("Adding %d switches", len(entities))
    async_add_entities(entities)


class WorksWithAlexaSwitch(WorksWithAlexaEntity, SwitchEntity):
    """One writable toggle feature, or an endpoint's power."""

    _wwa_operations = frozenset({"turnOn", "turnOff"})

    @property
    def is_on(self) -> bool | None:
        value = self.reading
        if value is None:
            return None
        return str(value).upper() == "ON"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_control("turnOn", {}, assume="ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_control("turnOff", {}, assume="OFF")

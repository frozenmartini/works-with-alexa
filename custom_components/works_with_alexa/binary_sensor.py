"""Binary sensor entities for Works with Alexa.

A ``motionSensor`` that reports a value becomes a motion binary sensor:
``DETECTED`` is on, ``NOT_DETECTED`` off, anything else unknown. Like every
reading here it is polled; Alexa sends no push, so a short detection between
two polls is not seen.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .entity import WorksWithAlexaEntity, selected_entities

_LOGGER = logging.getLogger(__name__)

_MOTION_STATES = {"DETECTED": True, "NOT_DETECTED": False}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities = [
        WorksWithAlexaMotion(coordinator, endpoint, feature)
        for endpoint, feature, _ in selected_entities(coordinator, "binary_sensor")
    ]

    _LOGGER.debug("Adding %d binary sensors", len(entities))
    async_add_entities(entities)


class WorksWithAlexaMotion(WorksWithAlexaEntity, BinarySensorEntity):
    """One motion sensor feature."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    @property
    def is_on(self) -> bool | None:
        value = self.reading
        return None if value is None else _MOTION_STATES.get(str(value).upper())

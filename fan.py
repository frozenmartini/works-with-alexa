"""Fan entities for Works with Alexa.

One fan entity per controllable endpoint: power from the ``power`` feature, and
speed from whichever ``mode`` feature is labelled as speed. Direction is
deliberately *not* folded in here -- Home Assistant's fan direction is a
forward/reverse pair, and the Window Fan's own labels are "Exhaust"/"Direct",
which that mapping would hide. Direction gets its own select (ref/07).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from . import WorksWithAlexaConfigEntry
from .const import CONTROLLABLE_CATEGORIES, FAN_SPEED_LABELS
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

FEATURE_POWER = "power"
FEATURE_MODE = "mode"


def speed_feature(endpoint: Endpoint) -> Feature | None:
    """The ordered mode feature that represents speed, if the endpoint has one.

    Only an ORDERED mode may become a percentage; an unordered one is a plain
    choice and belongs in a select.
    """
    for feature in endpoint.features_named(FEATURE_MODE):
        label = (feature.friendly_name or "").strip().lower()
        if feature.ordered and feature.writable and label in FAN_SPEED_LABELS:
            return feature
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities = [
        WorksWithAlexaFan(coordinator, endpoint)
        for endpoint in coordinator.data.values()
        if not endpoint.blocked
        and endpoint.display_category in CONTROLLABLE_CATEGORIES
        and (power := endpoint.feature(FEATURE_POWER)) is not None
        and power.writable
    ]
    _LOGGER.debug("Adding %d fans", len(entities))
    async_add_entities(entities)


class WorksWithAlexaFan(WorksWithAlexaEntity, FanEntity):
    """Power, and speed where the endpoint exposes an ordered speed mode."""

    _attr_name = None  # the fan is the device

    def __init__(
        self, coordinator: WorksWithAlexaCoordinator, endpoint: Endpoint
    ) -> None:
        power = endpoint.feature(FEATURE_POWER)
        super().__init__(coordinator, endpoint, power)

        speed = speed_feature(endpoint)
        self._speed_key = speed.key if speed else None
        self._speed_instance = speed.instance if speed else None
        # The option *values* in declared order; the labels are only for display.
        self._speed_values: list[str] = (
            [option.value for option in speed.mode_options] if speed else []
        )

        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._speed_values:
            features |= FanEntityFeature.SET_SPEED
        self._attr_supported_features = features

    @property
    def _speed(self) -> Feature | None:
        endpoint = self.endpoint
        if endpoint is None or self._speed_key is None:
            return None
        return endpoint.features.get(self._speed_key)

    @property
    def is_on(self) -> bool | None:
        endpoint = self.endpoint
        return endpoint.is_on if endpoint else None

    @property
    def speed_count(self) -> int:
        return len(self._speed_values) or 1

    @property
    def percentage(self) -> int | None:
        """Current speed as a percentage, or None when it cannot be read.

        While the device is off, a mode reports ``0`` -- a value that is not in
        modeOptions. It is a sentinel, not a speed, and mapping it would raise
        (ref/07).
        """
        endpoint, speed = self.endpoint, self._speed
        if endpoint is None or speed is None or not self._speed_values:
            return None
        value = endpoint.live_value(speed)
        if value is None:
            return None
        value = str(value)
        if value not in self._speed_values:
            _LOGGER.debug(
                "Speed %r is not one of %s; reporting unknown",
                value,
                self._speed_values,
            )
            return None
        return ordered_list_item_to_percentage(self._speed_values, value)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set speed, treating 0% as off the way Home Assistant expects."""
        if percentage == 0:
            await self.async_turn_off()
            return
        if not self._speed_values:
            return
        value = percentage_to_ordered_list_item(self._speed_values, percentage)
        await self.async_control(
            "setMode",
            {"mode": value},
            feature_name=FEATURE_MODE,
            instance=self._speed_instance,
        )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self.async_control("turnOn")
        if percentage:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_control("turnOff")

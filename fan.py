"""Fan entities for Works with Alexa -- the *built* layer.

The one assembled entity, kept apart from the raw one-entity-per-feature layer
by owner request (2026-09-13) and chosen through its own option. One fan per
built FAN endpoint: power from the ``power`` feature, speed from an ORDERED
``mode`` feature labelled as speed (the Window Fan's Low..Turbo), and Direction
presented as oscillate by earlier user directive (ref/07). The same features
also appear raw -- a Power switch, a Fan Speed select, a Direction select --
when the device is in the raw list; the two layers never collide because the
fan lives on its own platform.
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
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .features import direction_feature as oscillation_feature
from .features import is_fan, speed_feature
from .models import FEATURE_MODE, FEATURE_POWER, Endpoint

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    # The built layer: chosen separately from the raw device list.
    entities = [
        WorksWithAlexaFan(coordinator, endpoint)
        for endpoint in coordinator.built.values()
        if is_fan(endpoint)
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

        oscillation = oscillation_feature(endpoint)
        self._oscillate_key = oscillation[0].key if oscillation else None
        self._oscillate_instance = oscillation[0].instance if oscillation else None
        self._oscillate_on = oscillation[1] if oscillation else None
        self._oscillate_off = oscillation[2] if oscillation else None

        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._speed_values:
            features |= FanEntityFeature.SET_SPEED
        if oscillation:
            features |= FanEntityFeature.OSCILLATE
        self._attr_supported_features = features

    @property
    def is_on(self) -> bool | None:
        value = self.feature_value(self._feature_key)
        if value is None:
            return None
        return str(value).upper() == "ON"

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
        if self._speed_key is None or not self._speed_values:
            return None
        value = self.feature_value(self._speed_key)
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
            assume=value,
        )

    @property
    def oscillating(self) -> bool | None:
        """Whether Direction is set to the oscillating option.

        None while the device reports the off sentinel, which is neither
        option, rather than guessing at False.
        """
        if self._oscillate_key is None:
            return None
        value = self.feature_value(self._oscillate_key)
        if value is None:
            return None
        value = str(value)
        if value not in (self._oscillate_on, self._oscillate_off):
            return None
        return value == self._oscillate_on

    async def async_oscillate(self, oscillating: bool) -> None:
        if self._oscillate_key is None:
            return
        value = self._oscillate_on if oscillating else self._oscillate_off
        await self.async_control(
            "setMode",
            {"mode": value},
            feature_name=FEATURE_MODE,
            instance=self._oscillate_instance,
            assume=value,
        )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self.async_control("turnOn", assume="ON")
        if percentage:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_control("turnOff", assume="OFF")

"""Select entities for Works with Alexa.

Every writable ``mode`` feature the fan has not absorbed becomes a select, with
the device's own labels: the Window Fan's Direction ("Exhaust" / "Direct",
which Home Assistant's fan direction cannot express, ref/07), the Dreo fans'
Mode (Auto / Natural / Normal / Sleep). Only the Window Fan's ordered speed mode
is absorbed, as the fan's percentage.

A thermostat's mode becomes a select too, offering exactly the modes the device
advertises, so the owner's second integration can assemble a climate from it.
That write has not yet been executed against a device.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .features import (
    SET_THERMOSTAT_MODE,
    THERMOSTAT_MODE_PROPERTY,
    select_features,
    thermostat_mode_feature,
)
from .models import Endpoint, Feature, humanise

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[SelectEntity] = []
    for endpoint in coordinator.selected.values():
        for feature in select_features(endpoint):
            entities.append(WorksWithAlexaSelect(coordinator, endpoint, feature))
        if (thermostat := thermostat_mode_feature(endpoint)) is not None:
            entities.append(WorksWithAlexaThermostatMode(coordinator, endpoint, thermostat))

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
        self._attr_name = feature.label
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


class WorksWithAlexaThermostatMode(WorksWithAlexaEntity, SelectEntity):
    """A thermostat's mode, raw: the device's own ``supportedModes`` as options.

    Written with ``setThermostatMode``. The payload mirrors the Smart Home
    directive, ``{"thermostatMode": {"value": "COOL"}}``, as every confirmed
    write so far has mirrored its directive; ref/08 inferred a bare string
    instead, which is tried second. **Not yet executed against a device.**
    """

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)
        self._attr_unique_id = (
            f"{endpoint.endpoint_id}:{feature.key}/{THERMOSTAT_MODE_PROPERTY}"
        )
        self._attr_name = humanise(THERMOSTAT_MODE_PROPERTY)
        self._attr_options = list(feature.supported_modes)

    @property
    def current_option(self) -> str | None:
        feature = self.feature
        if feature is None:
            return None
        mode = self.feature_property(THERMOSTAT_MODE_PROPERTY)
        if mode is None:
            mode = feature.property_value("thermostatModeValue")
        if mode is None:
            return None
        return str(mode) if str(mode) in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self.async_control(
                SET_THERMOSTAT_MODE,
                {THERMOSTAT_MODE_PROPERTY: {"value": option}},
                assume=option,
                assume_property=THERMOSTAT_MODE_PROPERTY,
            )
        except HomeAssistantError as first_error:
            _LOGGER.debug(
                "Wrapped thermostatMode payload was rejected (%s); trying the bare form",
                first_error,
            )
            try:
                await self.async_control(
                    SET_THERMOSTAT_MODE,
                    {THERMOSTAT_MODE_PROPERTY: option},
                    assume=option,
                    assume_property=THERMOSTAT_MODE_PROPERTY,
                )
            except HomeAssistantError:
                raise first_error from None

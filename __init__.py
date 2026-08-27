"""The Works with Alexa integration.

Sets up one authenticated Amazon session per config entry and rebuilds it from
the stored refresh token on every start. Stored credentials are never deleted on
an error: an auth failure raises :class:`ConfigEntryAuthFailed` so Home Assistant
runs its reauth flow, and a transient failure raises
:class:`ConfigEntryNotReady` so it retries.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AmazonAlexaSession
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_MAC_DMS,
    CONF_REFRESH_TOKEN,
    CONF_URL,
)
from .coordinator import WorksWithAlexaCoordinator
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type WorksWithAlexaConfigEntry = ConfigEntry[WorksWithAlexaCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Rebuild the session from the stored refresh token."""
    session = AmazonAlexaSession(
        async_get_clientsession(hass),
        domain=entry.data[CONF_URL],
        device_serial=entry.data[CONF_DEVICE_SERIAL],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        mac_dms=entry.data.get(CONF_MAC_DMS),
    )

    try:
        await session.async_setup()
    except AuthError as err:
        # Do not delete the stored credentials; let HA's reauth flow decide.
        raise ConfigEntryAuthFailed(str(err)) from err
    except WorksWithAlexaError as err:
        raise ConfigEntryNotReady(str(err)) from err

    # Amazon may have rotated the refresh token; persist the current one so the
    # next cold start uses it.
    if session.refresh_token != entry.data[CONF_REFRESH_TOKEN]:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: session.refresh_token},
        )

    coordinator = WorksWithAlexaCoordinator(hass, entry, session)
    # Fail setup here rather than after the platforms are up, so a bad first poll
    # surfaces as a retry instead of a shell of unavailable entities.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    # The device selection and the polling interval are both read at setup, so
    # changing either from the options flow needs the entry to come back up.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> None:
    """Reload so new options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Unload the entry and its platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

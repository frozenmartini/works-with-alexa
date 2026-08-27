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
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

# Platforms come online once the coordinator lands; forwarding here is then a
# one-line change.
PLATFORMS: list[Platform] = []

type WorksWithAlexaConfigEntry = ConfigEntry[AmazonAlexaSession]


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

    entry.runtime_data = session
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Unload the entry and its platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

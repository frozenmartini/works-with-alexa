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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AmazonAlexaSession
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_INCLUDE_ENDPOINTS,
    CONF_MAC_DMS,
    CONF_REFRESH_TOKEN,
    CONF_URL,
    DOMAIN,
)
from .coordinator import WorksWithAlexaCoordinator
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.LOCK,
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
    _purge_deselected(hass, entry, coordinator)
    # The device selection and the polling interval are both read at setup, so
    # changing either from the options flow needs the entry to come back up.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _purge_deselected(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    coordinator: WorksWithAlexaCoordinator,
) -> None:
    """Drop registry devices the user has deselected.

    Home Assistant keeps a device in the registry once created, so simply not
    rebuilding its entities leaves it behind as an empty shell that can only be
    disabled. Deselecting is meant to remove, so remove it here.

    Devices are only dropped when the selection is an explicit list. With no
    selection recorded every endpoint is included, and an endpoint missing from
    a poll is a transient gap, not a decision to forget the device.
    """
    if entry.options.get(CONF_INCLUDE_ENDPOINTS) is None:
        return

    registry = dr.async_get(hass)
    keep = set(coordinator.selected)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ours = {
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN
        }
        if ours and not ours & keep:
            _LOGGER.debug("Removing deselected device %s", device.name)
            # Detach rather than delete outright, so a device also provided by
            # another integration survives.
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device from the UI once it is no longer selected.

    Without this hook Home Assistant offers only "disable". A device that is
    still selected must not be deletable, or it would reappear on the next poll.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return True
    ours = {
        identifier for domain, identifier in device.identifiers if domain == DOMAIN
    }
    return not (ours & set(coordinator.selected))


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

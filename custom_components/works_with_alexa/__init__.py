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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import aiohttp

from .api import AmazonAlexaSession
# Importing .fingerprint binds that module onto this package's globals.
from .auth_watch import auth_event, fingerprint as _token_fingerprint
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_DIRECTED_ID,
    CONF_INCLUDE_ENDPOINTS,
    CONF_INCLUDE_SCENES,
    CONF_MAC_DMS,
    CONF_REFRESH_TOKEN,
    CONF_URL,
    DOMAIN,
)
from .coordinator import WorksWithAlexaCoordinator
from .dev_logging import async_enable_evidence_logging, event
from .features import writable_feature_keys
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type WorksWithAlexaConfigEntry = ConfigEntry[WorksWithAlexaCoordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await async_enable_evidence_logging(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Rebuild the session from the stored refresh token."""
    await async_enable_evidence_logging(hass)
    event(_LOGGER, "entry.setup.started", entry_id=entry.entry_id)

    credential_fields = (
        CONF_URL, CONF_DEVICE_SERIAL, CONF_REFRESH_TOKEN, CONF_MAC_DMS,
        "auth_method", "enrolled_at",
    )
    accepted_credentials = tuple(entry.data.get(key) for key in credential_fields)
    persistence_active = True

    def stop_persistence() -> None:
        nonlocal persistence_active
        persistence_active = False

    entry.async_on_unload(stop_persistence)

    def persist_credentials(refresh_token: str, mac_dms: dict) -> bool:
        nonlocal accepted_credentials, persistence_active
        if not persistence_active or accepted_credentials != tuple(
            entry.data.get(key) for key in credential_fields
        ):
            persistence_active = False
            auth_event("credentials.superseded", entry_id=entry.entry_id)
            return False
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: refresh_token, CONF_MAC_DMS: mac_dms}
        )
        accepted_credentials = tuple(entry.data.get(key) for key in credential_fields)
        return True

    session = AmazonAlexaSession(
        async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(),
                                   timeout=aiohttp.ClientTimeout(total=60)),
        domain=entry.data[CONF_URL],
        device_serial=entry.data[CONF_DEVICE_SERIAL],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        mac_dms=entry.data.get(CONF_MAC_DMS),
        on_credentials=persist_credentials,
        entry_id=entry.entry_id,
    )
    auth_event("runtime.started", entry_id=entry.entry_id,
               rt_fingerprint=_token_fingerprint(entry.data[CONF_REFRESH_TOKEN]),
               auth_method=entry.data.get("auth_method", "existing_credentials"),
               enrolled_at=entry.data.get("enrolled_at"))

    try:
        await session.async_setup()
    except AuthError as err:
        auth_event("reauth.required", level=logging.ERROR, entry_id=entry.entry_id,
                   stage=err.stage, root_token_rejected=err.root_token_rejected, trigger="setup")
        # Do not delete the stored credentials; let HA's reauth flow decide.
        raise ConfigEntryAuthFailed(str(err)) from err
    except WorksWithAlexaError as err:
        auth_event("setup.retry_required", level=logging.WARNING, entry_id=entry.entry_id,
                   error_type=type(err).__name__)
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = WorksWithAlexaCoordinator(hass, entry, session)
    # Fail setup here rather than after the platforms are up, so a bad first poll
    # surfaces as a retry instead of a shell of unavailable entities.
    await coordinator.async_config_entry_first_refresh()

    # Older entries used the registration serial when account lookup failed.
    # Learn their identity while those credentials still work, retaining the ID.
    if (
        getattr(entry, "unique_id", None) == entry.data[CONF_DEVICE_SERIAL]
        and entry.data.get(CONF_DIRECTED_ID) in (None, entry.unique_id)
    ):
        try:
            email = await session.async_get_account_email()
        except WorksWithAlexaError:
            auth_event("identity.lookup_deferred", entry_id=entry.entry_id)
        else:
            if email and persistence_active and accepted_credentials == tuple(
                entry.data.get(key) for key in credential_fields
            ):
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_DIRECTED_ID: email}
                )
                auth_event("identity.learned", entry_id=entry.entry_id)

    entry.runtime_data = coordinator
    _purge_deselected(hass, entry, coordinator)
    _purge_deselected_scenes(hass, entry)
    _purge_reclassified_sensors(hass, entry, coordinator)
    _purge_built_fan(hass, entry)
    # The device selection and the polling interval are both read at setup, so
    # changing either from the options flow needs the entry to come back up.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def request_reauth(err: AuthError) -> None:
        entry.async_start_reauth(hass)

    session.start_refresh_watcher(request_reauth)
    entry.async_on_unload(session.stop_refresh_watcher)
    entry.async_on_unload(hass.bus.async_listen_once(
        "homeassistant_stop", lambda _event: session.stop_refresh_watcher()
    ))
    event(_LOGGER, "entry.setup.completed", entry_id=entry.entry_id,
          selected_devices=len(coordinator.selected), selected_scenes=len(coordinator.selected_scenes))
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
    if entry.options.get(CONF_INCLUDE_SCENES, []):
        keep.add(f"{entry.entry_id}:scenes")
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
    keep = set(coordinator.selected)
    if entry.options.get(CONF_INCLUDE_SCENES, []):
        keep.add(f"{entry.entry_id}:scenes")
    return not (ours & keep)


@callback
def _purge_deselected_scenes(hass: HomeAssistant, entry: WorksWithAlexaConfigEntry) -> None:
    """Remove unticked buttons without removing the remaining Scenes group."""
    selected = set(entry.options.get(CONF_INCLUDE_SCENES, []))
    prefix = f"{entry.entry_id}:scene:"
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == "button"
            and entity.unique_id.startswith(prefix)
            and entity.unique_id[len(prefix):] not in selected
        ):
            registry.async_remove(entity.entity_id)
    if not selected:
        devices = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
            if (DOMAIN, f"{entry.entry_id}:scenes") in device.identifiers:
                devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


@callback
def _purge_reclassified_sensors(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry, coordinator: WorksWithAlexaCoordinator
) -> None:
    """Replace old manufacturer-blocked sensors with their writable entities."""
    replaced = {
        f"{endpoint.endpoint_id}:{key}"
        for endpoint in coordinator.selected.values()
        for key in writable_feature_keys(endpoint)
    }
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == "sensor" and entity.unique_id in replaced:
            event(_LOGGER, "entity.reclassified", old_platform="sensor")
            registry.async_remove(entity.entity_id)


@callback
def _purge_built_fan(hass: HomeAssistant, entry: WorksWithAlexaConfigEntry) -> None:
    """Forget the entities of the removed fan platform.

    The built Window Fan (``fan.window_fan``) was dropped on 2026-09-13; the
    owner assembles fans in kitbash from the raw entities instead. Without
    this the registry keeps the old entity as a restored, unavailable shell.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == "fan":
            _LOGGER.info("Removing the retired built fan entity %s", entity.entity_id)
            registry.async_remove(entity.entity_id)


async def _async_options_updated(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> None:
    """Reload so new options take effect."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and coordinator.configured_options == dict(entry.options):
        # Credential rotation also calls entry update listeners. It must not
        # reload the integration or cause another startup refresh.
        return
    event(_LOGGER, "options.updated", entry_id=entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Unload the entry and its platforms."""
    event(_LOGGER, "entry.unload", entry_id=entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

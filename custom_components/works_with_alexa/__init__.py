"""The Works with Alexa integration.

Sets up one authenticated Amazon session per config entry and rebuilds it from
the stored refresh token on every start. Stored credentials are never deleted on
an error: an auth failure raises :class:`ConfigEntryAuthFailed` so Home Assistant
runs its reauth flow, and a transient failure raises
:class:`ConfigEntryNotReady` so it retries.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)

from .auth_watch import auth_event, fingerprint
from .auth_runtime import AuthRuntime
from .const import (
    CONF_DIRECTED_ID,
    CONF_INCLUDE_ENDPOINTS,
    CONF_INCLUDE_SCENES,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from .coordinator import WorksWithAlexaCoordinator
from .dev_logging import async_enable_evidence_logging, event
from .features import writable_feature_keys
from .identity import account_id, is_email
from .protocol import AuthError, WorksWithAlexaError
from .scenes import scene_unique_id, scenes_device_id

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


async def async_migrate_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Replace a stored account email with its one-way key (entry 1.1 -> 1.2).

    0.1.0 stored the account email as the unique id, as ``directed_id`` and in
    the generated title. The key keeps duplicate and same-account checks
    working; the email is no longer kept anywhere in the entry.
    """
    if entry.version != 1:
        return False
    if entry.minor_version >= 2:
        return True
    changes: dict = {"minor_version": 2}
    email = entry.data.get(CONF_DIRECTED_ID)
    if is_email(email):
        changes["data"] = {**entry.data, CONF_DIRECTED_ID: account_id(email)}
    if is_email(entry.unique_id):
        changes["unique_id"] = account_id(entry.unique_id)
    # Either stored email may have generated the title; an entry need not
    # carry directed_id at all.
    if entry.title in {
        f"Works with Alexa ({value})"
        for value in (email, entry.unique_id) if is_email(value)
    }:
        changes["title"] = "Works with Alexa"
    hass.config_entries.async_update_entry(entry, **changes)
    event(_LOGGER, "entry.migrated", entry_id=entry.entry_id, minor_version=2,
          identity_replaced="data" in changes or "unique_id" in changes)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Rebuild the session from the stored refresh token."""
    await async_enable_evidence_logging(hass)
    event(_LOGGER, "entry.setup.started", entry_id=entry.entry_id)

    runtime = AuthRuntime(hass, entry)
    # The one cleanup hook, with no await since the client was made, so a failed
    # setup runs it too. Hooks pop last-first: this one runs last.
    entry.async_on_unload(runtime.cleanup)
    # Before the first await too: a stop during setup must retire the session.
    runtime.listen_for_stop()
    session = runtime.session
    auth_event("runtime.started", entry_id=entry.entry_id,
               rt_fingerprint=fingerprint(entry.data[CONF_REFRESH_TOKEN]),
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

    entry.runtime_data = coordinator
    _purge_registry(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    session.start_refresh_watcher(runtime.request_reauth)
    event(_LOGGER, "entry.setup.completed", entry_id=entry.entry_id,
          selected_devices=len(coordinator.selected), selected_scenes=len(coordinator.selected_scenes))
    return True


def _kept_identifiers(
    entry: WorksWithAlexaConfigEntry, endpoint_ids: Iterable[str]
) -> set[str]:
    """The device identifiers a selection keeps: the given endpoints, plus the
    Scenes device while at least one scene is selected."""
    keep = set(endpoint_ids)
    if entry.options.get(CONF_INCLUDE_SCENES, []):
        keep.add(scenes_device_id(entry.entry_id))
    return keep


def _our_identifiers(device: dr.DeviceEntry) -> set[str]:
    """The device's identifiers in this integration's domain."""
    return {identifier for domain, identifier in device.identifiers if domain == DOMAIN}


@callback
def _purge_registry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    coordinator: WorksWithAlexaCoordinator,
) -> None:
    """Remove what the current selection no longer builds, once per setup.

    Home Assistant keeps a device or an entity in its registry once created, so
    simply not rebuilding it leaves an empty shell that can only be disabled.
    Three removals, in this order:

    1. Deselected devices. Only when the selection is an explicit list, and only
       devices not on it: an endpoint missing from a poll is a transient gap,
       not a decision to forget the device, so the selection decides what is
       kept rather than what the latest poll returned. Detached rather than
       deleted outright, so a device also provided by another integration
       survives.
    2. Unticked scene buttons, keeping the remaining ones; the Scenes device is
       detached once no scene is selected.
    3. Replaced sensors. An endpoint's operations can change between setups, so
       a sensor row whose feature a writable entity of a selected endpoint now
       provides is removed.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    include = entry.options.get(CONF_INCLUDE_ENDPOINTS)
    if include is not None:
        keep = _kept_identifiers(entry, include)
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
            ours = _our_identifiers(device)
            if ours and not ours & keep:
                _LOGGER.debug("Removing deselected device %s", device.name)
                devices.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )

    selected_scenes = set(entry.options.get(CONF_INCLUDE_SCENES, []))
    prefix = scene_unique_id(entry.entry_id, "")
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if (
            entity.domain == "button"
            and entity.unique_id.startswith(prefix)
            and entity.unique_id[len(prefix):] not in selected_scenes
        ):
            entities.async_remove(entity.entity_id)
    if not selected_scenes:
        scenes_device = (DOMAIN, scenes_device_id(entry.entry_id))
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
            if scenes_device in device.identifiers:
                devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)

    replaced = {
        f"{endpoint.endpoint_id}:{key}"
        for endpoint in coordinator.selected.values()
        for key in writable_feature_keys(endpoint)
    }
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.domain == "sensor" and entity.unique_id in replaced:
            event(_LOGGER, "entity.reclassified", old_platform="sensor")
            entities.async_remove(entity.entity_id)


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
    return not (_our_identifiers(device) & _kept_identifiers(entry, coordinator.selected))


async def async_unload_entry(
    hass: HomeAssistant, entry: WorksWithAlexaConfigEntry
) -> bool:
    """Unload the entry and its platforms."""
    event(_LOGGER, "entry.unload", entry_id=entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

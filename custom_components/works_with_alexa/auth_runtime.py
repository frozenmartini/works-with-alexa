"""The credential guard and the runtime's one cleanup owner.

The guard decides whether this runtime may still write credentials, learn the
account identity or start a reauth flow: only while the stored credentials are
the ones it was built from (or last wrote itself). A newer sign-in, a failed
save or the runtime's own cleanup revokes it, and it never comes back.

The runtime owns the session's shutdown. One unload hook revokes the guard and
retires the session at once, then settles the refresh watcher and only then
detaches the client. The stop listener waits for an auth step in progress,
retires, settles and detaches. Neither ever closes HA's client.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
import copy
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback

from .auth_watch import auth_event
from .const import CONF_DEVICE_SERIAL, CONF_MAC_DMS, CONF_REFRESH_TOKEN, CONF_URL

CREDENTIAL_FIELDS = (
    CONF_URL, CONF_DEVICE_SERIAL, CONF_REFRESH_TOKEN, CONF_MAC_DMS,
    "auth_method", "enrolled_at",
)


def _credentials(entry: ConfigEntry) -> tuple[Any, ...]:
    """The entry's credential fields, deep-copied so nothing aliases ``entry.data``."""
    return copy.deepcopy(tuple(entry.data.get(key) for key in CREDENTIAL_FIELDS))


class CredentialGuard:
    """Refuses writes once the stored credentials are no longer this runtime's."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._snapshot = _credentials(entry)
        self._active = True

    @callback
    def is_current(self) -> bool:
        """Whether this runtime still owns the stored credentials."""
        return self._active and self._snapshot == _credentials(self._entry)

    @callback
    def publish(self, refresh_token: str, mac_dms: dict) -> bool:
        """Save a rotation; ``False`` if a newer sign-in or a cleanup got there first."""
        if not self.is_current():
            self.revoke()
            auth_event("credentials.superseded", entry_id=self._entry.entry_id)
            return False
        try:
            self._hass.config_entries.async_update_entry(self._entry, data={
                **self._entry.data,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_MAC_DMS: copy.deepcopy(mac_dms),
            })
        except BaseException:
            self.revoke()
            raise
        self._snapshot = _credentials(self._entry)
        return True

    @callback
    def revoke(self) -> None:
        """Refuse every later write; idempotent."""
        self._active = False


class AuthRuntime:
    """One entry runtime's cleanup: the unload hook, the stop listener, reauth."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, guard: CredentialGuard, session: Any
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._guard = guard
        self._session = session
        self._unsub_stop: Callable[[], None] | None = None

    @callback
    def listen_for_stop(self) -> None:
        """Release the session when Home Assistant stops (entries are not unloaded then)."""
        self._unsub_stop = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self.on_stop
        )

    @callback
    def cleanup(self) -> Coroutine[Any, Any, None]:
        """The one unload hook: refuse and retire now, then settle and detach.

        Revoking and retiring happen before this returns, so nothing can save,
        reauth or start a request once HA has the hook's task; the returned
        coroutine settles the watcher before it detaches the client.
        """
        if (unsub := self._unsub_stop) is not None:
            self._unsub_stop = None
            unsub()
        self._guard.revoke()
        self._session.retire()
        return self._session.async_release()

    async def on_stop(self, _event: Event) -> None:
        """Let an auth step in progress finish and save, then retire, settle, detach."""
        # The listener has fired and is gone; removing it again would log an error.
        self._unsub_stop = None
        await self._session.async_retire_after_auth()
        await self._session.async_release()

    @callback
    def request_reauth(self, _err: Exception) -> None:
        """Start reauth only while this runtime still owns the stored credentials."""
        if self._guard.is_current():
            self._entry.async_start_reauth(self._hass)

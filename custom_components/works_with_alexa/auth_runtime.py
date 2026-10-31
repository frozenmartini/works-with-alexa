"""The session's Home Assistant boundary: its client, its guard, its cleanup.

:func:`new_session` is the one recipe for a session over HA's HTTP client, for
the runtime and the config flow. :class:`AuthRuntime` builds one entry's session
and may write credentials, learn the account identity or start a reauth flow
only while the stored credentials are the ones it was built from (or last wrote
itself); a newer sign-in, a failed save or its own cleanup revokes that for good.

One unload hook revokes and retires at once, then settles the refresh watcher
and only then detaches the client. The stop listener waits for an auth step in
progress, retires, settles and detaches. Neither ever closes HA's client.
"""
from __future__ import annotations

from collections.abc import Callable, Coroutine
import copy
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AmazonAlexaSession
from .auth_watch import auth_event
from .const import CONF_DEVICE_SERIAL, CONF_MAC_DMS, CONF_REFRESH_TOKEN, CONF_URL

CREDENTIAL_FIELDS = (
    CONF_URL, CONF_DEVICE_SERIAL, CONF_REFRESH_TOKEN, CONF_MAC_DMS,
    "auth_method", "enrolled_at",
)


def _credentials(entry: ConfigEntry) -> tuple[Any, ...]:
    """The entry's credential fields, deep-copied so nothing aliases ``entry.data``."""
    return copy.deepcopy(tuple(entry.data.get(key) for key in CREDENTIAL_FIELDS))


def new_session(
    hass: HomeAssistant, *, domain: str, refresh_token: str, mac_dms: dict[str, Any] | None,
    **kwargs: Any,
) -> AmazonAlexaSession:
    """A session over its own HA client; the session's release detaches it."""
    client = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar(),
                                        timeout=aiohttp.ClientTimeout(total=60))
    try:
        return AmazonAlexaSession(client, domain=domain, refresh_token=refresh_token,
                                  mac_dms=mac_dms, **kwargs)
    except BaseException:
        # No hook owns the client yet, and HA does not detach it.
        client.detach()
        raise


class AuthRuntime:
    """One entry's session, its credential guard, cleanup, stop listener and reauth."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._snapshot: tuple[Any, ...] | None = _credentials(entry)
        self._unsub_stop: Callable[[], None] | None = None
        self.session = new_session(
            hass,
            domain=entry.data[CONF_URL],
            refresh_token=entry.data[CONF_REFRESH_TOKEN],
            mac_dms=entry.data.get(CONF_MAC_DMS),
            on_credentials=self.publish,
            entry_id=entry.entry_id,
        )

    @callback
    def is_current(self) -> bool:
        """Whether this runtime still owns the stored credentials."""
        return self._snapshot is not None and self._snapshot == _credentials(self._entry)

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
        if self._snapshot is not None:  # a revoke during the save stays
            self._snapshot = _credentials(self._entry)
        return True

    @callback
    def revoke(self) -> None:
        """Refuse every later write; idempotent."""
        self._snapshot = None

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
        self.revoke()
        self.session.retire()
        return self.session.async_release()

    async def on_stop(self, _event: Event) -> None:
        """Let an auth step in progress finish and save, then retire, settle, detach."""
        # The listener has fired and is gone; removing it again would log an error.
        self._unsub_stop = None
        await self.session.async_retire_after_auth()
        await self.session.async_release()

    @callback
    def request_reauth(self, _err: Exception) -> None:
        """Start reauth only while this runtime still owns the stored credentials."""
        if self.is_current():
            self._entry.async_start_reauth(self._hass)

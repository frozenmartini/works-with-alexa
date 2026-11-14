"""Polling coordinator for Works with Alexa.

One bulk ``features[]`` query per interval returns every endpoint on the account
(ref/09), so a single coordinator backs every entity rather than each entity
polling for itself -- 73 endpoints on the reference account would otherwise mean
73 requests per cycle against an API that signals throttling in the response
body (ref/00 §6).

Third-party scene discovery is a separate paginated query on each poll. Its
transient failures make scene buttons unavailable without stopping device polls.

A successful command is followed by an immediate refresh and, unless the polling
interval is short enough to catch up anyway, one more ``FOLLOW_UP_DELAY`` after
the last command: Alexa can update other properties of the device a few seconds
after acknowledging a command, once the immediate refresh has already read them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AmazonAlexaSession
from .auth_watch import auth_event
from .const import (
    CONF_INCLUDE_ENDPOINTS,
    CONF_INCLUDE_SCENES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .models import Endpoint
from .dev_logging import event
from .scenes import AlexaScene
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)

# Seconds from the last successful command to the follow-up refresh. Seen live
# on a heated blanket: after power off, a setting changed on Alexa's side after
# the immediate refresh and showed only at the next poll, 63 s later. A choice,
# not a measurement: long enough for such a delayed update, short of a 60 s poll.
FOLLOW_UP_DELAY = 15.0


class WorksWithAlexaCoordinator(DataUpdateCoordinator[dict[str, Endpoint]]):
    """Keeps one account's endpoints fresh for every platform."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: AmazonAlexaSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.session = session
        self.scenes: dict[str, AlexaScene] = {}
        # None until the first scene discovery, so its first failure is logged.
        self.scenes_available: bool | None = None
        # One follow-up refresh for the whole account, however many entities
        # were driven; each command restarts it.
        self._cancel_follow_up: Callable[[], None] | None = None
        self._follow_ups_stopped = False

    @property
    def selected_scenes(self) -> dict[str, AlexaScene]:
        """Scenes always require explicit opt-in, including existing entries."""
        allowed = set(self.config_entry.options.get(CONF_INCLUDE_SCENES, []))
        return {key: scene for key, scene in self.scenes.items() if key in allowed}

    @property
    def selected(self) -> dict[str, Endpoint]:
        """The endpoints the user chose to expose raw, one entity per feature.

        Polling stays account-wide -- one bulk query returns everything either
        way -- so this filters what becomes an entity, not what is fetched. A
        missing option means the selection was never made and everything is
        included; an empty list means the user chose nothing.
        """
        include = self.config_entry.options.get(CONF_INCLUDE_ENDPOINTS)
        if include is None:
            return self.data
        allowed = set(include)
        return {
            endpoint_id: endpoint
            for endpoint_id, endpoint in self.data.items()
            if endpoint_id in allowed
        }

    @callback
    def async_schedule_follow_up(self) -> None:
        """Refresh once more ``FOLLOW_UP_DELAY`` after the last command.

        Not when the regular poll comes as soon (the interval's minimum is 15 s),
        and never after shutdown: a command still in flight at unload must not
        arm a timer that nothing would cancel.
        """
        if self._follow_ups_stopped:
            return
        if self.update_interval is not None and self.update_interval.total_seconds() <= FOLLOW_UP_DELAY:
            return
        self._cancel_follow_up_timer()
        self._cancel_follow_up = async_call_later(self.hass, FOLLOW_UP_DELAY, self._async_follow_up)

    async def _async_follow_up(self, _now: datetime) -> None:
        self._cancel_follow_up = None
        event(_LOGGER, "poll.follow_up")
        # Through the debouncer, so it merges with any refresh already requested.
        await self.async_request_refresh()

    @callback
    def _cancel_follow_up_timer(self) -> None:
        if self._cancel_follow_up is not None:
            self._cancel_follow_up()
            self._cancel_follow_up = None

    async def async_shutdown(self) -> None:
        """Cancel the follow-up refresh too; Home Assistant calls this on unload."""
        self._follow_ups_stopped = True
        self._cancel_follow_up_timer()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Endpoint]:
        """Fetch every endpoint, keyed by endpoint id."""
        event(_LOGGER, "poll.started")
        try:
            endpoints = await self.session.async_get_endpoints()
            try:
                self.scenes = await self.session.async_get_scenes()
                if self.scenes_available is False:
                    _LOGGER.info("Alexa scenes are available again")
                self.scenes_available = True
            except AuthError:
                raise
            except WorksWithAlexaError:
                # A scene discovery outage must not stop physical device polling.
                # Log the outage once, not on every poll while it lasts.
                if self.scenes_available is not False:
                    _LOGGER.warning("Could not refresh Alexa scenes; scene buttons are unavailable")
                self.scenes_available = False
            event(_LOGGER, "poll.completed", endpoints=len(endpoints), scenes=len(self.scenes),
                  scenes_available=self.scenes_available)
            return endpoints
        except AuthError as err:
            auth_event("reauth.required", level=logging.ERROR, entry_id=self.config_entry.entry_id,
                       stage=err.stage, root_token_rejected=err.root_token_rejected, trigger="poll")
            event(_LOGGER, "poll.failed", level=logging.ERROR, error_type=type(err).__name__)
            # Never discard the stored credentials -- hand off to HA's reauth
            # flow and let it decide (ref/10).
            raise ConfigEntryAuthFailed(str(err)) from err
        except WorksWithAlexaError as err:
            event(_LOGGER, "poll.failed", level=logging.ERROR, error_type=type(err).__name__)
            raise UpdateFailed(str(err)) from err

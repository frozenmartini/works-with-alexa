"""Polling coordinator for Works with Alexa.

One bulk ``features[]`` query per interval returns every endpoint on the account
(ref/09), so a single coordinator backs every entity rather than each entity
polling for itself -- 73 endpoints on the reference account would otherwise mean
73 requests per cycle against an API that signals throttling in the response
body (ref/00 §6).

Third-party scene discovery is a separate paginated query on each poll. Its
transient failures make scene buttons unavailable without stopping device polls.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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
        self.configured_options = dict(entry.options)
        self.scenes: dict[str, AlexaScene] = {}
        self.scenes_available = False

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

    async def _async_update_data(self) -> dict[str, Endpoint]:
        """Fetch every endpoint, keyed by endpoint id."""
        event(_LOGGER, "poll.started")
        try:
            endpoints = await self.session.async_get_endpoints()
            try:
                self.scenes = await self.session.async_get_scenes()
                self.scenes_available = True
            except AuthError:
                raise
            except WorksWithAlexaError:
                # A scene discovery outage must not stop physical device polling.
                self.scenes_available = False
                _LOGGER.warning("Could not refresh Alexa scenes; scene buttons are unavailable")
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

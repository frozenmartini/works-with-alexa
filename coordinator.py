"""Polling coordinator for Works with Alexa.

One bulk ``features[]`` query per interval returns every endpoint on the account
(ref/09), so a single coordinator backs every entity rather than each entity
polling for itself -- 73 endpoints on the reference account would otherwise mean
73 requests per cycle against an API that signals throttling in the response
body (ref/00 §6).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AmazonAlexaSession
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import Endpoint
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

    async def _async_update_data(self) -> dict[str, Endpoint]:
        """Fetch every endpoint, keyed by endpoint id."""
        try:
            return await self.session.async_get_endpoints()
        except AuthError as err:
            # Never discard the stored credentials -- hand off to HA's reauth
            # flow and let it decide (ref/10).
            raise ConfigEntryAuthFailed(str(err)) from err
        except WorksWithAlexaError as err:
            raise UpdateFailed(str(err)) from err

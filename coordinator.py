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
from .const import (
    CONF_BUILT_ENDPOINTS,
    CONF_INCLUDE_ENDPOINTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .features import default_built
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

    @property
    def built(self) -> dict[str, Endpoint]:
        """The endpoints that also get an assembled entity (today: a fan).

        A separate list from :attr:`selected`, by owner request: a device may
        be raw, built, or both. With no choice recorded the fans with an
        ordered speed mode are built, which keeps the Window Fan as it was.
        """
        chosen = self.config_entry.options.get(CONF_BUILT_ENDPOINTS)
        if chosen is None:
            return {
                endpoint_id: endpoint
                for endpoint_id, endpoint in self.data.items()
                if default_built(endpoint)
            }
        allowed = set(chosen)
        return {
            endpoint_id: endpoint
            for endpoint_id, endpoint in self.data.items()
            if endpoint_id in allowed
        }

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

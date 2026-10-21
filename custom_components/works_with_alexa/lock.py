"""Lock entities for Works with Alexa.

A ``lock`` feature with both ``lock`` and ``unlock`` operations becomes a lock.
The Front Door (Lockly, ``SMARTLOCK``) is the worked example: it carries nothing
else.

State is ``lockState``: ``LOCKED`` and ``UNLOCKED`` as reported live, and
``JAMMED`` from the schema. The operations carry no ``payload`` field at all:
with ``"payload": {}`` Amazon answers BAD_REQUEST. Lock and unlock were
verified from Home Assistant on the Front Door on 2026-10-21: both accepted,
the door actuated, and the next poll confirmed the state.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WorksWithAlexaConfigEntry
from .coordinator import WorksWithAlexaCoordinator
from .entity import WorksWithAlexaEntity
from .features import lock_feature
from .models import Endpoint, Feature

_LOGGER = logging.getLogger(__name__)

STATE_LOCKED = "LOCKED"
STATE_UNLOCKED = "UNLOCKED"
STATE_JAMMED = "JAMMED"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[WorksWithAlexaLock] = []
    for endpoint in coordinator.selected.values():
        lock = lock_feature(endpoint)
        if lock is not None:
            entities.append(WorksWithAlexaLock(coordinator, endpoint, lock))
    _LOGGER.debug("Adding %d locks", len(entities))
    async_add_entities(entities)


class WorksWithAlexaLock(WorksWithAlexaEntity, LockEntity):
    """One lock feature."""

    _attr_name = None  # the lock is the device
    _wwa_operations = frozenset({"lock", "unlock"})

    def __init__(
        self,
        coordinator: WorksWithAlexaCoordinator,
        endpoint: Endpoint,
        feature: Feature,
    ) -> None:
        super().__init__(coordinator, endpoint, feature)

    @property
    def _state(self) -> str | None:
        value = self.live_value
        return None if value is None else str(value).upper()

    @property
    def is_locked(self) -> bool | None:
        """Only an explicit LOCKED or UNLOCKED decides; anything else is unknown.

        A value outside the schema must never read as unlocked.
        """
        state = self._state
        if state == STATE_LOCKED:
            return True
        if state == STATE_UNLOCKED:
            return False
        return None

    @property
    def is_jammed(self) -> bool | None:
        state = self._state
        return None if state is None else state == STATE_JAMMED

    async def async_lock(self, **kwargs: Any) -> None:
        await self.async_control("lock", assume=STATE_LOCKED)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.async_control("unlock", assume=STATE_UNLOCKED)

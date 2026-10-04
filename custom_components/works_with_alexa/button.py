"""Selected Alexa scenes as buttons, grouped under one Scenes device."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WorksWithAlexaConfigEntry
from .const import DOMAIN
from .coordinator import WorksWithAlexaCoordinator
from .dev_logging import event, operation
from .protocol import WorksWithAlexaError
from .scenes import AlexaScene

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WorksWithAlexaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    event(_LOGGER, "buttons.setup", count=len(coordinator.selected_scenes))
    async_add_entities(
        [WorksWithAlexaSceneButton(coordinator, scene)
         for scene in coordinator.selected_scenes.values()]
    )


class WorksWithAlexaSceneButton(CoordinatorEntity[WorksWithAlexaCoordinator], ButtonEntity):
    """A momentary activation, with no invented on/off state."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: WorksWithAlexaCoordinator, scene: AlexaScene) -> None:
        super().__init__(coordinator)
        self._automation_id = scene.automation_id
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}:scene:{scene.automation_id}"
        self._attr_name = scene.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}:scenes")},
            name="Scenes",
            manufacturer="Amazon Alexa",
            model="Third-party scenes",
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.scenes_available
            and self._automation_id in self.coordinator.selected_scenes
        )

    async def async_press(self) -> None:
        if not self.available:
            event(_LOGGER, "button.press.unavailable", level=logging.WARNING, scene=self._attr_name)
            raise HomeAssistantError("This Alexa scene is unavailable")
        with operation(_LOGGER, "button.press", scene=self._attr_name):
            try:
                await self.coordinator.session.async_activate_scene(self._automation_id)
            except WorksWithAlexaError as err:
                raise HomeAssistantError("Alexa scene activation failed") from err

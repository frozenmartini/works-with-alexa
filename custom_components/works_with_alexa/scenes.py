"""Third-party Alexa scenes, separate from physical endpoint features."""

from dataclasses import dataclass

from .features import numbered_choices


@dataclass(frozen=True)
class AlexaScene:
    automation_id: str
    name: str


def scene_choices(scenes: dict[str, AlexaScene]) -> dict[str, str]:
    """Stable, sorted labels; duplicate names remain individually selectable."""
    return numbered_choices(
        (scene.name.casefold(), scene.automation_id, scene.name)
        for scene in scenes.values()
    )


def scene_unique_id(entry_id: str, automation_id: str) -> str:
    """A scene button's unique id; with an empty automation id, the prefix all of
    an entry's scene buttons share."""
    return f"{entry_id}:scene:{automation_id}"


def scenes_device_id(entry_id: str) -> str:
    """The identifier of the one Scenes device an entry groups its scene buttons under."""
    return f"{entry_id}:scenes"

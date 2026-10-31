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

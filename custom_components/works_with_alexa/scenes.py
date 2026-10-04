"""Third-party Alexa scenes, separate from physical endpoint features."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AlexaScene:
    automation_id: str
    name: str


def scene_choices(scenes: dict[str, AlexaScene]) -> dict[str, str]:
    """Stable, sorted labels; duplicate names remain individually selectable."""
    choices: dict[str, str] = {}
    seen: dict[str, int] = {}
    for scene in sorted(scenes.values(), key=lambda s: (s.name.casefold(), s.automation_id)):
        seen[scene.name] = seen.get(scene.name, 0) + 1
        count = seen[scene.name]
        choices[scene.automation_id] = scene.name if count == 1 else f"{scene.name} #{count}"
    return choices

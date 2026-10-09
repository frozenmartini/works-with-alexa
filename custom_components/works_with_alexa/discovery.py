"""Optional, sanitized discovery hints for consumers of HA entity metadata."""

from __future__ import annotations

from typing import Any

from .features import SETPOINT_PROPERTIES, THERMOSTAT_MODE_PROPERTY
from .models import Feature, humanise


def _mode_order(feature: Feature) -> bool | None:
    if feature.name != "mode":
        return None
    order = str(feature.configuration.get("order", "")).upper()
    return {"ORDERED": True, "UNORDERED": False}.get(order)


# Exact feature labels or semantic instance suffixes only. Neither endpoint
# names, manufacturers, models, option labels nor numeric instances are clues.
_LABEL_ROLES = {
    "speed": "speed", "fan speed": "speed",
    "oscillate": "oscillate", "oscillation": "oscillate",
    "oscillation angle": "oscillation_angle",
    "direction": "direction", "fan direction": "direction",
    "mode": "mode", "fan mode": "mode",
    "temperature": "temperature", "air temperature": "temperature",
    "current temperature": "temperature",
    "set temperature": "setpoint", "target temperature": "setpoint",
    "setpoint": "setpoint", "target setpoint": "setpoint",
    "heating": "heating", "cooling": "cooling",
    "lock": "lock", "contact": "contact", "light": "light",
    "cover": "cover", "blind lift": "cover", "shade lift": "cover",
    "cover position": "cover", "blind position": "cover", "shade position": "cover",
}


def _role(feature: Feature, prop: str | None) -> str | None:
    if feature.name == "thermostat":
        if prop in SETPOINT_PROPERTIES:
            return "setpoint"
        if prop == THERMOSTAT_MODE_PROPERTY:
            return "mode"
        # A whole read-only thermostat feature may primarily report a setpoint.
        # Do not describe that value as the current temperature.
        return None
    capability_role = {
        "power": "power", "lock": "lock", "temperatureSensor": "temperature",
        "contactSensor": "contact",
    }.get(feature.name)
    if capability_role:
        return capability_role

    clues = []
    if feature.friendly_name:
        clues.append(feature.friendly_name)
    if isinstance(feature.instance, str):
        # Preserve semantic suffixes such as Fan.Speed and Blind.Lift while
        # ignoring arbitrary manufacturer prefixes.
        parts = feature.instance.split(".")
        clues.extend((humanise(parts[-1]), humanise(".".join(parts[-2:]))))
    roles = {
        _LABEL_ROLES[clue.casefold().strip()]
        for clue in clues
        if clue.casefold().strip() in _LABEL_ROLES
    }
    if len(roles) > 1:
        return None
    role = next(iter(roles), None)
    if role == "oscillate" and feature.name != "toggle":
        return None
    if role == "oscillation_angle" and feature.name != "range":
        return None
    if role == "speed" and not (
        feature.name == "range" or (feature.name == "mode" and _mode_order(feature) is True)
    ):
        return None
    return role or ("mode" if feature.name == "mode" else None)


def feature_metadata(
    feature: Feature, *, writable: bool, prop: str | None = None,
) -> dict[str, Any]:
    """Allowlisted metadata; writable describes this entity, not the endpoint.

    Labels come from Alexa (with the existing readable fallback), never the HA
    entity registry. Thermostat children share a feature key, distinguished by
    their property. Returned containers are fresh so consumers cannot mutate
    feature configuration through state attributes.
    """
    metadata: dict[str, Any] = {
        "key": feature.key,
        "type": feature.name,
        "instance": feature.instance,
        "label": feature.label,
        "operations": sorted(feature.operations),
        "writable": writable,
        "ordered": _mode_order(feature),
        "property": prop,
    }
    if (role := _role(feature, prop)) is not None:
        metadata["role"] = role
    return metadata

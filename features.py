"""Which feature becomes which entity.

One place for every classification rule, shared by the platforms and the
tests, so what the device picker promises and what the platforms build cannot
drift apart. No Home Assistant imports, so it runs anywhere :mod:`.models` does.

The principle, set by the owner on 2026-09-13: **expose every feature as raw as
possible and assemble nothing.** One Home Assistant entity per Alexa feature,
using the plainest primitive that can carry it:

* ``power`` and ``toggle`` -> switch;
* ``mode`` -> select, with the device's own labels;
* writable ``range`` -> number, with the device's own bounds;
* ``thermostat`` -> one number per setpoint it reports (target, lower, upper)
  and a select for its mode;
* ``lock`` -> lock (a lock/unlock pair has no plainer primitive);
* anything read-only with a value -> sensor.

So a blind is its "Opening" number, a thermostat is three numbers, a select and
a temperature sensor, and a vacuum is its power switch and two status sensors.
The owner's second integration does the assembling into HA- and HomeKit-shaped
devices.

The one typed entity that pre-dates this steer stays: the fan (power, plus the
Window Fan's ORDERED speed mode as percentage and its Direction as oscillate,
both by earlier user directive, ref/07). The speed mode is the only feature a
typed entity absorbs; Direction is also kept as a select, and a fan whose speed
is a numeric range keeps it as a number.
"""

from __future__ import annotations

from .const import FAN_SPEED_LABELS, OSCILLATE_OFF_LABEL, OSCILLATE_ON_LABEL
from .models import (
    FEATURE_LOCK,
    FEATURE_MODE,
    FEATURE_POWER,
    FEATURE_RANGE,
    FEATURE_THERMOSTAT,
    FEATURE_TOGGLE,
    Endpoint,
    Feature,
    humanise,
    is_controllable,
    sensor_features,
)

FAN_CATEGORY = "FAN"

# The setpoints a thermostat may report, in the order they are exposed. Which of
# them a device actually carries is read per poll: the ecobee reports all three,
# the Anthem only the target (2026-09-13 capture, ref/08).
SETPOINT_PROPERTIES = ("targetSetpoint", "lowerSetpoint", "upperSetpoint")
THERMOSTAT_MODE_PROPERTY = "thermostatMode"
SET_THERMOSTAT_MODE = "setThermostatMode"
SET_TARGET_SETPOINT = "setTargetSetpoint"


def _label(feature: Feature) -> str:
    return (feature.friendly_name or "").strip().lower()


def _writable_power(endpoint: Endpoint) -> Feature | None:
    power = endpoint.feature(FEATURE_POWER)
    return power if power is not None and power.writable else None


# --- fan ------------------------------------------------------------------------
def is_fan(endpoint: Endpoint) -> bool:
    """A controllable FAN with writable power."""
    return (
        is_controllable(endpoint)
        and endpoint.display_category == FAN_CATEGORY
        and _writable_power(endpoint) is not None
    )


def speed_feature(endpoint: Endpoint) -> Feature | None:
    """The ORDERED mode labelled as speed, if the fan has one (Low..Turbo).

    Only an ordered mode may become a percentage; an unordered one is a plain
    choice and stays a select. A numeric speed range is not absorbed either --
    it stays a raw number.
    """
    for feature in endpoint.features_named(FEATURE_MODE):
        if feature.ordered and feature.writable and _label(feature) in FAN_SPEED_LABELS:
            return feature
    return None


def direction_feature(endpoint: Endpoint) -> tuple[Feature, str, str] | None:
    """A mode standing in for oscillation, with its on and off option values.

    The Window Fan's Direction: Exhaust oscillates, Direct does not (user
    directive, ref/07). Not claimed -- the select keeps the real labels.
    """
    for feature in endpoint.features_named(FEATURE_MODE):
        if not feature.writable:
            continue
        values = {
            option.friendly_name.strip().lower(): option.value
            for option in feature.mode_options
        }
        on_value = values.get(OSCILLATE_ON_LABEL)
        off_value = values.get(OSCILLATE_OFF_LABEL)
        if on_value is not None and off_value is not None:
            return feature, on_value, off_value
    return None


# --- lock -----------------------------------------------------------------------
def lock_feature(endpoint: Endpoint) -> Feature | None:
    """A ``lock`` feature that can both lock and unlock."""
    if not is_controllable(endpoint):
        return None
    lock = endpoint.feature(FEATURE_LOCK)
    if lock is not None and {"lock", "unlock"} <= lock.operations:
        return lock
    return None


def is_lock(endpoint: Endpoint) -> bool:
    return lock_feature(endpoint) is not None


# --- thermostat, raw ------------------------------------------------------------
def thermostat_feature(endpoint: Endpoint) -> Feature | None:
    """A writable ``thermostat`` feature on a controllable endpoint.

    The Anthem carries one too, but it is blocked (Kohler) and category OTHER,
    so it stays read-only sensors (ref/08).
    """
    if not is_controllable(endpoint):
        return None
    thermostat = endpoint.feature(FEATURE_THERMOSTAT)
    if thermostat is not None and thermostat.writable:
        return thermostat
    return None


def setpoint_properties(endpoint: Endpoint) -> list[tuple[Feature, str]]:
    """(thermostat feature, property name) for each setpoint the device reports."""
    thermostat = thermostat_feature(endpoint)
    if thermostat is None or SET_TARGET_SETPOINT not in thermostat.operations:
        return []
    return [
        (thermostat, name)
        for name in SETPOINT_PROPERTIES
        if name in thermostat.properties
    ]


def thermostat_mode_feature(endpoint: Endpoint) -> Feature | None:
    """The thermostat feature, where its mode can be set and it lists its modes."""
    thermostat = thermostat_feature(endpoint)
    if (
        thermostat is not None
        and SET_THERMOSTAT_MODE in thermostat.operations
        and thermostat.supported_modes
    ):
        return thermostat
    return None


# --- raw primitives ---------------------------------------------------------------
def power_switch_feature(endpoint: Endpoint) -> Feature | None:
    """Writable power on a controllable endpoint that is not a fan.

    On a fan, power is the fan's own on/off and exposing it twice would let
    the two disagree. Everywhere else -- the Roborock, where Alexa's on/off
    starts and stops a clean -- it is a switch.
    """
    if not is_controllable(endpoint) or is_fan(endpoint):
        return None
    return _writable_power(endpoint)


def claimed_features(endpoint: Endpoint) -> frozenset[str]:
    """Feature keys a typed entity absorbs, so they are not also exposed raw.

    Only the fan absorbs anything: its power, and the ordered speed mode.
    """
    claimed: set[str] = set()
    if is_fan(endpoint):
        claimed.add(FEATURE_POWER)
        if (speed := speed_feature(endpoint)) is not None:
            claimed.add(speed.key)
    return frozenset(claimed)


def switch_features(endpoint: Endpoint) -> list[Feature]:
    """Power where the fan does not own it, then every writable toggle."""
    if not is_controllable(endpoint):
        return []
    features: list[Feature] = []
    if (power := power_switch_feature(endpoint)) is not None:
        features.append(power)
    features.extend(
        feature for feature in endpoint.features_named(FEATURE_TOGGLE) if feature.writable
    )
    return features


def select_features(endpoint: Endpoint) -> list[Feature]:
    """Every writable mode with options that the fan has not absorbed."""
    if not is_controllable(endpoint):
        return []
    claimed = claimed_features(endpoint)
    return [
        feature
        for feature in endpoint.features_named(FEATURE_MODE)
        if feature.writable and feature.mode_options and feature.key not in claimed
    ]


def number_features(endpoint: Endpoint) -> list[Feature]:
    """Every writable range with bounds."""
    if not is_controllable(endpoint):
        return []
    return [
        feature
        for feature in endpoint.features_named(FEATURE_RANGE)
        if feature.writable and feature.minimum is not None and feature.maximum is not None
    ]


def entity_plan(endpoint: Endpoint) -> dict[str, list[str]]:
    """Platform -> entity names this endpoint yields. What the picker promises.

    The fan and the lock are named after the device (``None`` name in Home
    Assistant terms), shown here as the device's friendly name.
    """
    plan: dict[str, list[str]] = {}
    device = endpoint.friendly_name.strip()
    if is_fan(endpoint):
        plan["fan"] = [device]
    if is_lock(endpoint):
        plan["lock"] = [device]
    switches = [feature.label for feature in switch_features(endpoint)]
    if any(feature.name == FEATURE_POWER for feature in switch_features(endpoint)):
        switches[0] = "Power"
    selects = [feature.label for feature in select_features(endpoint)]
    if thermostat_mode_feature(endpoint) is not None:
        selects.append(humanise(THERMOSTAT_MODE_PROPERTY))
    numbers = [feature.label for feature in number_features(endpoint)]
    numbers.extend(humanise(name) for _, name in setpoint_properties(endpoint))
    for platform, names in (
        ("switch", switches),
        ("select", selects),
        ("number", numbers),
        ("sensor", [feature.label for feature in sensor_features(endpoint)]),
    ):
        if names:
            plan[platform] = names
    return plan

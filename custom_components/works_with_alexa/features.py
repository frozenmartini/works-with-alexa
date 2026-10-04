"""Which feature becomes which entity.

One place for every classification rule, shared by the platforms, the pickers
and the tests, so what a picker promises and what the platforms build cannot
drift apart. No Home Assistant imports, so it runs anywhere :mod:`.models` does.

One layer, raw (owner request, 2026-09-13; the built Window Fan ``fan`` entity
that used to sit beside it moved to kitbash the same day): one Home Assistant
entity per Alexa feature, nothing absorbed, using the plainest primitive that
carries it:

* ``power`` and ``toggle`` -> switch;
* ``mode`` -> select, with the device's own labels;
* writable ``range`` -> number, with the device's own bounds;
* ``thermostat`` -> one number per setpoint it reports (target, lower, upper)
  and a select for its mode;
* ``lock`` -> lock (a lock/unlock pair has no plainer primitive);
* anything read-only with a value -> sensor.

Classification uses feature operations, options and bounds, never manufacturer
or accessory category. Unknown capabilities produce no writable entity until
their protocol is implemented. Chosen through the device picker. The owner's
second integration (kitbash)
assembles these into HA- and HomeKit-shaped devices, so raw and complete beats
clever. Raw entities carry the feature key in their unique id.
"""

from __future__ import annotations

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
    sensor_features,
)

# The setpoints a thermostat may report, in the order they are exposed. Which of
# them a device actually carries is read per poll: the ecobee reports all three,
# the Anthem only the target (2026-09-13 capture, ref/08).
SETPOINT_PROPERTIES = ("targetSetpoint", "lowerSetpoint", "upperSetpoint")
THERMOSTAT_MODE_PROPERTY = "thermostatMode"
SET_THERMOSTAT_MODE = "setThermostatMode"
SET_TARGET_SETPOINT = "setTargetSetpoint"


def _writable_power(endpoint: Endpoint) -> Feature | None:
    power = endpoint.feature(FEATURE_POWER)
    return power if power is not None and {"turnOn", "turnOff"} <= power.operations else None


# --- raw: lock ------------------------------------------------------------------
def lock_feature(endpoint: Endpoint) -> Feature | None:
    """A ``lock`` feature that can both lock and unlock."""
    lock = endpoint.feature(FEATURE_LOCK)
    if lock is not None and {"lock", "unlock"} <= lock.operations:
        return lock
    return None


def is_lock(endpoint: Endpoint) -> bool:
    return lock_feature(endpoint) is not None


# --- raw: thermostat ------------------------------------------------------------
def thermostat_feature(endpoint: Endpoint) -> Feature | None:
    """A writable ``thermostat`` feature, independent of accessory category."""
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


# --- raw: primitives ------------------------------------------------------------
def power_switch_feature(endpoint: Endpoint) -> Feature | None:
    """Power with both supported commands, independent of category."""
    return _writable_power(endpoint)


def switch_features(endpoint: Endpoint) -> list[Feature]:
    """Power first, then every writable toggle."""
    features: list[Feature] = []
    if (power := power_switch_feature(endpoint)) is not None:
        features.append(power)
    features.extend(
        feature for feature in endpoint.features_named(FEATURE_TOGGLE)
        if {"turnOn", "turnOff"} <= feature.operations
    )
    return features


def select_features(endpoint: Endpoint) -> list[Feature]:
    """Every writable mode with options, the speed mode included."""
    return [
        feature
        for feature in endpoint.features_named(FEATURE_MODE)
        if "setMode" in feature.operations and feature.mode_options
    ]


def number_features(endpoint: Endpoint) -> list[Feature]:
    """Every writable range with bounds."""
    return [
        feature
        for feature in endpoint.features_named(FEATURE_RANGE)
        if "setRangeValue" in feature.operations
        and feature.minimum is not None and feature.maximum is not None
    ]


def entity_plan(endpoint: Endpoint) -> dict[str, list[str]]:
    """Platform -> entity names the raw layer yields. What the picker promises.

    The lock is named after the device (``None`` name in Home Assistant terms),
    shown here as the device's friendly name.
    """
    plan: dict[str, list[str]] = {}
    if is_lock(endpoint):
        plan["lock"] = [endpoint.friendly_name.strip()]
    selects = [feature.label for feature in select_features(endpoint)]
    if thermostat_mode_feature(endpoint) is not None:
        selects.append(humanise(THERMOSTAT_MODE_PROPERTY))
    numbers = [feature.label for feature in number_features(endpoint)]
    numbers.extend(humanise(name) for _, name in setpoint_properties(endpoint))
    for platform, names in (
        ("switch", [feature.label for feature in switch_features(endpoint)]),
        ("select", selects),
        ("number", numbers),
        ("sensor", [feature.label for feature in sensor_features(endpoint)]),
    ):
        if names:
            plan[platform] = names
    return plan


def writable_feature_keys(endpoint: Endpoint) -> set[str]:
    """Feature identities represented by supported writable entities."""
    features = [*switch_features(endpoint), *select_features(endpoint), *number_features(endpoint)]
    if (lock := lock_feature(endpoint)) is not None:
        features.append(lock)
    thermostat = thermostat_feature(endpoint)
    if thermostat is not None and (setpoint_properties(endpoint) or thermostat_mode_feature(endpoint) is not None):
        features.append(thermostat)
    return {feature.key for feature in features}

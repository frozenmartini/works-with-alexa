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

``raw_entities`` is the one decision; the plan, the writable keys, the picker's
predicates and labels and the platforms' setups all derive from it.

Classification uses feature operations, options and bounds, never
manufacturer; the one category it reads is the scene trigger, which yields
nothing. Unknown capabilities produce no writable entity until
their protocol is implemented. Chosen through the device picker. The owner's
second integration (kitbash)
assembles these into HA- and HomeKit-shaped devices, so raw and complete beats
clever. Raw entities carry the feature key in their unique id.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NamedTuple

from .models import (
    FEATURE_ACTION,
    FEATURE_CONNECTIVITY,
    FEATURE_ENDPOINT_HEALTH,
    FEATURE_LOCK,
    FEATURE_MODE,
    FEATURE_POWER,
    FEATURE_RANGE,
    FEATURE_THERMOSTAT,
    FEATURE_TOGGLE,
    Endpoint,
    Feature,
    humanise,
)

# The setpoints a thermostat may report, in the order they are exposed. Which of
# them a device actually carries is read per poll: the ecobee reports all three,
# the Anthem only the target (2026-09-13 capture, ref/08).
SETPOINT_PROPERTIES = ("targetSetpoint", "lowerSetpoint", "upperSetpoint")
THERMOSTAT_MODE_PROPERTY = "thermostatMode"
SET_THERMOSTAT_MODE = "setThermostatMode"
SET_TARGET_SETPOINT = "setTargetSetpoint"

# A scene trigger is a routine's starting point, not a device: it has no state
# to control or read, so it yields nothing whatever features it carries.
SCENE_TRIGGER_CATEGORY = "SCENE_TRIGGER"

# Features that never become sensors even though they report a value. Every
# endpoint on the account -- phones, apps, scene triggers -- carries
# connectivity, so a sensor per reachability flag would drag all 60 non-devices
# into the picker. Reachability is reported as an attribute instead.
_NEVER_SENSORS = frozenset({FEATURE_CONNECTIVITY, FEATURE_ENDPOINT_HEALTH})

# Features that are sensors even when writable. An action's property is its
# *status* (IDLE / RUNNING); its operation performs the action rather than
# setting that status, so reading and driving it are different entities. The
# Roborock's Robot.Clean advertised `performAction` in one capture and no
# operations in another (2026-09-13), so the reading must not depend on it.
_ALWAYS_SENSORS = frozenset({FEATURE_ACTION})


class RawEntity(NamedTuple):
    """One Home Assistant entity the raw layer yields for an endpoint."""

    platform: str  # "lock" | "switch" | "select" | "number" | "sensor"
    feature: Feature
    prop: str | None  # a thermostat child's property, else None


def sensor_features(endpoint: Endpoint) -> list[Feature]:
    """Features that become read-only entities.

    A feature qualifies when it currently reports a value and is either
    read-only or an action (whose value is a status, not a setting).
    Features carrying no value would only ever be unknown, and
    ``connectivity`` is deliberately excluded (see ``_NEVER_SENSORS``).
    """
    return [
        feature
        for feature in endpoint.features.values()
        if feature.name not in _NEVER_SENSORS
        and (
            not feature.writable
            or feature.name in _ALWAYS_SENSORS
        )
        and endpoint.live_value(feature) is not None
    ]


def raw_entities(endpoint: Endpoint) -> list[RawEntity]:
    """Every entity the raw layer yields for one endpoint, in setup order.

    Platform by platform, wire order inside each, independent of manufacturer,
    and of accessory category except that a scene trigger yields nothing:

    * a ``lock`` that can both lock and unlock;
    * ``power`` with both commands, then every toggle with both, as switches;
    * every mode that can be set and lists its options (the speed mode
      included), then the thermostat's mode where it can be set and lists its
      modes, as selects;
    * every range that can be set and has both bounds, then one number per
      setpoint the thermostat reports, in ``SETPOINT_PROPERTIES`` order, where
      its target can be set;
    * :func:`sensor_features`, which reads the current poll.

    The thermostat counts only when writable,
    which each of its two tests implies, since each names an operation.
    """
    if endpoint.display_category == SCENE_TRIGGER_CATEGORY:
        return []
    on_off = {"turnOn", "turnOff"}
    entities: list[RawEntity] = []
    lock = endpoint.feature(FEATURE_LOCK)
    if lock is not None and {"lock", "unlock"} <= lock.operations:
        entities.append(RawEntity("lock", lock, None))
    power = endpoint.feature(FEATURE_POWER)
    if power is not None and on_off <= power.operations:
        entities.append(RawEntity("switch", power, None))
    entities.extend(
        RawEntity("switch", toggle, None)
        for toggle in endpoint.features_named(FEATURE_TOGGLE)
        if on_off <= toggle.operations
    )
    entities.extend(
        RawEntity("select", mode, None)
        for mode in endpoint.features_named(FEATURE_MODE)
        if "setMode" in mode.operations and mode.mode_options
    )
    thermostat = endpoint.feature(FEATURE_THERMOSTAT)
    if (
        thermostat is not None
        and SET_THERMOSTAT_MODE in thermostat.operations
        and thermostat.supported_modes
    ):
        entities.append(RawEntity("select", thermostat, THERMOSTAT_MODE_PROPERTY))
    entities.extend(
        RawEntity("number", range_, None)
        for range_ in endpoint.features_named(FEATURE_RANGE)
        if "setRangeValue" in range_.operations
        and range_.minimum is not None and range_.maximum is not None
    )
    if thermostat is not None and SET_TARGET_SETPOINT in thermostat.operations:
        entities.extend(
            RawEntity("number", thermostat, name)
            for name in SETPOINT_PROPERTIES
            if name in thermostat.properties
        )
    entities.extend(RawEntity("sensor", feature, None) for feature in sensor_features(endpoint))
    return entities


def entities_of(endpoint: Endpoint, platform: str) -> list[RawEntity]:
    """The raw entities of one platform, in setup order."""
    return [entity for entity in raw_entities(endpoint) if entity.platform == platform]


def entity_name(feature: Feature, prop: str | None = None) -> str:
    """The Home Assistant name of one feature, or of one property of it."""
    return humanise(prop) if prop else feature.label


def entity_plan(endpoint: Endpoint) -> dict[str, list[str]]:
    """Platform -> entity names the raw layer yields. What the picker promises.

    The lock is named after the device (``None`` name in Home Assistant terms),
    shown here as the device's friendly name.
    """
    plan: dict[str, list[str]] = {}
    for entity in raw_entities(endpoint):
        plan.setdefault(entity.platform, []).append(
            endpoint.friendly_name.strip()
            if entity.platform == "lock"
            else entity_name(entity.feature, entity.prop)
        )
    return plan


def writable_feature_keys(endpoint: Endpoint) -> set[str]:
    """Feature identities represented by supported writable entities."""
    return {entity.feature.key for entity in raw_entities(endpoint) if entity.platform != "sensor"}


def produces_entities(endpoint: Endpoint) -> bool:
    """The picker offers exactly the endpoints that the platforms can import."""
    return bool(raw_entities(endpoint))


def is_controllable(endpoint: Endpoint) -> bool:
    """Whether the raw layer yields any writable entity."""
    return bool(writable_feature_keys(endpoint))


def numbered_choices(rows: Iterable[tuple[Any, str, str]]) -> dict[str, str]:
    """Id -> label from ``(sort key, id, label)`` rows, sorted whole, so the id
    breaks ties between equal keys; the second and later repeats of a label get
    `` #n``, skipping any number a real label already uses."""
    choices: dict[str, str] = {}
    seen: dict[str, int] = {}
    used: set[str] = set()
    for _, choice_id, label in sorted(rows):
        n = seen.get(label, 0) + 1
        candidate = label if n == 1 else f"{label} #{n}"
        while candidate in used:
            n += 1
            candidate = f"{label} #{n}"
        seen[label] = n
        used.add(candidate)
        choices[choice_id] = candidate
    return choices


def endpoint_choices(endpoints: dict[str, Endpoint]) -> dict[str, str]:
    """Endpoint id -> label, for the device picker.

    Sorted by the name the user sees, since the ids are opaque. Endpoints
    yielding only sensors are marked read-only; capability rules decide this.
    Sorted first, numbered second, so which twin is #2 does not depend on poll
    order.
    """
    rows: list[tuple[str, str, str]] = []
    for endpoint in endpoints.values():
        # Most of an Alexa account is not devices -- Echo speakers, phone apps,
        # scene triggers. Offering an endpoint that yields nothing would be an
        # invitation to tick it and get an empty device.
        if not produces_entities(endpoint):
            continue
        # Sort on the stripped name: some names on a real account carry leading
        # whitespace, which would otherwise sort them ahead of everything.
        name = endpoint.friendly_name.strip()
        label = name
        if endpoint.manufacturer:
            label = f"{label} ({endpoint.manufacturer})"
        if not is_controllable(endpoint):
            label = f"{label} - read-only"
        # Names are not unique on an Alexa account. Two identical rows of
        # checkboxes are unpickable, so disambiguate the repeats.
        rows.append((name.lower(), endpoint.endpoint_id, label))
    return numbered_choices(rows)

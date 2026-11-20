"""Parsed representations of Alexa endpoints and their features.

Shapes here follow the live capture in the sibling reverse-engineering repo
(``ref/06-live-capture.md``, ``ref/07-window-fan.md``). Two rules from that work
drive the whole module:

* **Address by ``instance``, classify by ``friendlyName``.** Instance values are
  inconsistent between manufacturers -- some are semantic (``Fan.Speed``), some
  are bare integers (``1``, ``2``) -- so neither alone is enough to identify a
  feature.
* **Writability is per-feature, not per-endpoint.** A feature with an empty
  ``operations`` list is read-only, e.g. the Window Fan's Air Temperature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Feature names we know how to interpret.
FEATURE_POWER = "power"
FEATURE_MODE = "mode"
FEATURE_RANGE = "range"
FEATURE_TOGGLE = "toggle"
FEATURE_THERMOSTAT = "thermostat"
FEATURE_TEMPERATURE_SENSOR = "temperatureSensor"
FEATURE_CONNECTIVITY = "connectivity"
FEATURE_ENDPOINT_HEALTH = "endpointHealth"
FEATURE_LOCK = "lock"
FEATURE_INVENTORY = "inventory"
FEATURE_ACTION = "action"
FEATURE_BRIGHTNESS = "brightness"
FEATURE_COLOR = "color"
FEATURE_COLOR_TEMPERATURE = "colorTemperature"
FEATURE_PERCENTAGE = "percentage"
FEATURE_POWER_LEVEL = "powerLevel"
FEATURE_SPEAKER = "speaker"
FEATURE_MOTION_SENSOR = "motionSensor"
FEATURE_LIGHT_SENSOR = "lightSensor"
FEATURE_SECURITY_PANEL = "securityPanel"
FEATURE_SECURITY_PANEL_ALERT = "securityPanel.Alert"

# Splits camelCase for the label fallback: "temperatureSensor" -> "temperature Sensor".
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# The property name a feature type reports its current value under.
#
# The query document (``queries.py``) selects one spelling per property type
# and ``name`` on every property, so these are the keys the wire carries;
# ``_flatten_properties`` also files each value under its property name.
_PRIMARY_PROPERTIES = {
    FEATURE_POWER: ("powerStateValue",),
    FEATURE_MODE: ("modeValue",),
    FEATURE_RANGE: ("rangeValue",),
    FEATURE_TOGGLE: ("toggleStateValue",),
    FEATURE_THERMOSTAT: ("targetSetpoint", "value", "thermostatModeValue"),
    FEATURE_TEMPERATURE_SENSOR: ("temperature", "value"),
    FEATURE_CONNECTIVITY: ("reachabilityStatusValue",),
    # Names below come from a schema introspection (2026-09-13), not a capture
    # write-up: `Lock.lockState`, `InventoryLevel.inventoryLevelValue`,
    # `ActionState.actionStateValue`.
    FEATURE_LOCK: ("lockState",),
    FEATURE_INVENTORY: ("inventoryLevelValue",),
    FEATURE_ACTION: ("actionStateValue",),
    # Spellings from the 2026-11-07 schema introspection. `volumeValue` is the query's alias for `Volume.value`, which would clash
    # with the temperature types' `value { value scale }` (Int against Float).
    FEATURE_BRIGHTNESS: ("brightnessStateValue",),
    FEATURE_COLOR: ("colorStateValue",),
    FEATURE_COLOR_TEMPERATURE: ("colorTemperatureInKelvinStateValue",),
    FEATURE_PERCENTAGE: ("percentageValue",),
    FEATURE_POWER_LEVEL: ("powerLevelValue",),
    FEATURE_SPEAKER: ("volumeValue",),
    FEATURE_MOTION_SENSOR: ("detectionStateValue",),
    FEATURE_LIGHT_SENSOR: ("illuminanceValue",),
    FEATURE_SECURITY_PANEL: ("armStateValue",),
    FEATURE_SECURITY_PANEL_ALERT: ("sirenStateValue",),
}

# A thermostat's allowed setpoint range: `{heating, cooling}`, each a
# `{minimum, maximum}` pair of `{value, scale}` (schema introspection, 2026-11-07).
# The schema names its type after a thermostat-configuration feature, so it may
# arrive on a feature of its own rather than on the thermostat.
ALLOWED_RANGE_PROPERTY = "thermostatAllowedTemperatureRangeValue"

# Properties whose value sits one field down in an object of its own: a siren's
# state is `{"state": "OFF", ...}`.
_NESTED_VALUES = {"sirenStateValue": "state"}

# To and from Kelvin per scale: the formulas of Home Assistant's TemperatureConverter.
_TO_KELVIN = {
    "CELSIUS": lambda c: c + 273.15,
    "FAHRENHEIT": lambda f: (f - 32) / 1.8 + 273.15,
    "KELVIN": lambda k: k,
}
_FROM_KELVIN = {
    "CELSIUS": lambda k: k - 273.15,
    "FAHRENHEIT": lambda k: (k - 273.15) * 1.8 + 32,
    "KELVIN": lambda k: k,
}

# Tried in order after a type's own spellings, and alone for types not listed above.
_PROPERTY_FALLBACK = (
    "powerStateValue",
    "modeValue",
    "rangeValue",
    "toggleStateValue",
    "targetSetpoint",
    "temperature",
    "value",
)


@dataclass(frozen=True, slots=True)
class ModeOption:
    """One selectable value of a ``mode`` feature."""

    value: str
    friendly_name: str


@dataclass(frozen=True, slots=True)
class Feature:
    """A single capability of an endpoint."""

    name: str
    instance: str | int | None
    operations: frozenset[str]
    properties: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identifier for this feature within its endpoint."""
        return f"{self.name}/{self.instance}" if self.instance else self.name

    @property
    def friendly_name(self) -> str | None:
        """Human label, e.g. ``Fan Speed``. The instance alone does not say."""
        return _text(self.configuration.get("friendlyName"))

    @property
    def label(self) -> str:
        """Best entity name: the device's own label, else a readable instance.

        Inventory and action features carry no ``friendlyName`` at all, and a
        ``temperatureSensor`` has no instance, so the fallback turns
        ``Robot.Clean`` into ``Robot Clean`` and ``temperatureSensor`` into
        ``Temperature Sensor`` rather than showing the wire spelling.
        """
        if self.friendly_name:
            return self.friendly_name
        return humanise(str(self.instance or self.name))

    @property
    def writable(self) -> bool:
        """Empty ``operations`` means read-only (ref/06)."""
        return bool(self.operations)

    # --- mode ---------------------------------------------------------------
    @property
    def mode_options(self) -> list[ModeOption]:
        """Selectable values, in the order the device declares them.

        Never hardcode the option count -- it is device-specific.
        """
        return [
            ModeOption(value=str(opt.get("value")), friendly_name=_label(opt))
            for opt in self.configuration.get("modeOptions") or []
            if opt.get("value") is not None
        ]

    @property
    def ordered(self) -> bool | None:
        """Whether mode options form a scale (Low..Turbo) or an unordered set.

        Published in ``wwa_feature`` so a consumer can tell a speed scale from
        a set. ``None`` when the device does not say.
        """
        order = str(self.configuration.get("order", "")).upper()
        return {"ORDERED": True, "UNORDERED": False}.get(order)

    # --- range --------------------------------------------------------------
    @property
    def minimum(self) -> float | None:
        return _get(self.configuration, "supportedRange", "minimumValue")

    @property
    def maximum(self) -> float | None:
        return _get(self.configuration, "supportedRange", "maximumValue")

    @property
    def step(self) -> float | None:
        return _get(self.configuration, "supportedRange", "precision")

    @property
    def unit_of_measure(self) -> str | None:
        """Bare unit string, e.g. ``Degrees``.

        Note this carries no Celsius/Fahrenheit discriminator, unlike the
        ``scale`` field on thermostat and temperatureSensor features. Callers
        must handle scale per feature type (ref/07, open question Q3).

        Arrives as a ``{value:{text}}`` wrapper; a bare string is tolerated.
        """
        return _text(self.configuration.get("unitOfMeasure"))

    # --- current value ------------------------------------------------------
    @property
    def value(self) -> Any:
        """Raw current value, or ``None`` if this feature reports no state.

        The property carrying the value is named after the feature, so prefer the
        name this feature type is known to use before falling back to a scan.
        """
        for prop in self._candidate_properties:
            if prop in self.properties:
                raw = self.properties[prop]
                if prop in _NESTED_VALUES and isinstance(raw, dict):
                    raw = raw.get(_NESTED_VALUES[prop])
                return _unwrap(raw)
        return None

    @property
    def _candidate_properties(self) -> tuple[str, ...]:
        return _PRIMARY_PROPERTIES.get(self.name, ()) + _PROPERTY_FALLBACK

    @property
    def scale(self) -> str | None:
        """Explicit temperature scale, where the feature carries one.

        ``thermostat`` and ``temperatureSensor`` report ``FAHRENHEIT``/
        ``CELSIUS``/``KELVIN``. Bare ``range`` features do not -- see
        :attr:`unit_of_measure`.
        """
        for prop in self._candidate_properties:
            raw = self.properties.get(prop)
            if isinstance(raw, dict) and "scale" in raw:
                return raw["scale"]
        return None

    @property
    def raw_value(self) -> Any:
        """The primary property before unwrapping, for callers that need its
        siblings -- an inventory level's ``type`` and ``unit`` sit next to its
        ``value``."""
        for prop in self._candidate_properties:
            if prop in self.properties:
                return self.properties[prop]
        return None

    # --- named properties ---------------------------------------------------
    def property_value(self, name: str) -> Any:
        """One named property, unwrapped, or ``None``.

        The wire names every property, and a thermostat carries several of the
        same type: ``targetSetpoint``, ``lowerSetpoint`` and ``upperSetpoint``
        are three ``Setpoint`` entries told apart only by name.
        """
        if name not in self.properties:
            return None
        return _unwrap(self.properties[name])

    def property_scale(self, name: str) -> str | None:
        raw = self.properties.get(name)
        return raw.get("scale") if isinstance(raw, dict) else None

    def color_component(self, component: str) -> float | None:
        """One of a colour's ``hue``, ``saturation`` and ``brightness``, or ``None``.

        The three arrive together in one ``colorStateValue``: hue 0-360,
        saturation and brightness 0-1, Alexa's fixed rule (ColorController).
        """
        color = self.properties.get("colorStateValue")
        if not isinstance(color, dict):
            return None
        return color.get(component)

    @property
    def supported_modes(self) -> list[str]:
        """A thermostat's advertised modes, e.g. ``["OFF", "AUTO"]``.

        Only 2 of the 6 enum values on the Anthem, all 4 HVAC ones on the
        ecobee. Read per device; never offer the whole enum (ref/08).
        """
        return [str(mode) for mode in self.configuration.get("supportedModes") or []]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One device as Alexa sees it."""

    endpoint_id: str
    friendly_name: str
    display_category: str | None
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    features: dict[str, Feature] = field(default_factory=dict)
    # Who reports this endpoint to Alexa, as ``endpointReports[].reporter.namespace``
    # sends it, in wire order: ``SKILL`` (a vendor cloud through a linked skill),
    # ``AAA`` (registered with Amazon directly), ``ALEXABRIDGE``. A few endpoints
    # have several reporters.
    reporters: tuple[str, ...] = ()

    def feature(self, name: str, instance: str | None = None) -> Feature | None:
        """Look a feature up by name and instance."""
        return self.features.get(f"{name}/{instance}" if instance else name)

    def features_named(self, name: str) -> list[Feature]:
        """Every instance of a given feature type, e.g. all ``mode`` features."""
        return [f for f in self.features.values() if f.name == name]

    @property
    def reachable(self) -> bool | None:
        """Amazon's own reachability flag, or ``None`` when it is not reported.

        ``connectivity`` reports ``OK`` or ``UNREACHABLE``; the Alexa app's
        "device is unresponsive" comes from the same signal.
        """
        connectivity = self.feature(FEATURE_CONNECTIVITY)
        if connectivity is None or connectivity.value is None:
            return None
        return str(connectivity.value).upper() == "OK"

    def setpoint_bounds(self, feature: Feature, prop: str) -> tuple[float, float] | None:
        """Alexa's allowed range for one setpoint, in that setpoint's scale, or ``None``.

        Read from the thermostat feature itself, else from any feature of the
        endpoint that carries it. One range bounds every setpoint. When heating
        and cooling arrive separately, heating bounds ``lowerSetpoint``,
        cooling bounds ``upperSetpoint`` and ``targetSetpoint`` takes the union
        of the two. A device need not send one, so callers keep a fallback.
        """
        bounds = feature.properties.get(ALLOWED_RANGE_PROPERTY)
        if bounds is None:
            bounds = next(
                (
                    other.properties[ALLOWED_RANGE_PROPERTY]
                    for other in self.features.values()
                    if other.properties.get(ALLOWED_RANGE_PROPERTY) is not None
                ),
                None,
            )
        if not isinstance(bounds, dict):
            return None
        scale = (feature.property_scale(prop) or feature.scale or "CELSIUS").upper()
        heating = _temperature_range(bounds.get("heating"), scale)
        cooling = _temperature_range(bounds.get("cooling"), scale)
        if heating is None or cooling is None:
            return heating or cooling
        if prop == "lowerSetpoint":
            return heating
        if prop == "upperSetpoint":
            return cooling
        return min(heating[0], cooling[0]), max(heating[1], cooling[1])


def parse_endpoint(raw: dict[str, Any]) -> Endpoint | None:
    """Build an :class:`Endpoint` from one item of the bulk endpoints query."""
    endpoint_id = raw.get("id")
    if not endpoint_id:
        return None

    features: dict[str, Feature] = {}
    for raw_feature in raw.get("features") or []:
        # A feature Amazon nulled (a never-null field of it came back null) is
        # skipped, so it cannot fail the poll of every other device.
        if not isinstance(raw_feature, dict):
            continue
        parsed = _parse_feature(raw_feature)
        if parsed is not None:
            features[parsed.key] = parsed

    return Endpoint(
        endpoint_id=endpoint_id,
        friendly_name=_text(raw.get("friendlyNameObject")) or endpoint_id,
        display_category=_get(raw, "displayCategories", "primary", "value"),
        # The name, model and manufacturer arrive as {value:{text}} wrappers, though
        # a bare string is tolerated so fixtures and older captures still parse.
        manufacturer=_text(raw.get("manufacturer")),
        model=_text(raw.get("model")),
        serial_number=_text(raw.get("serialNumber")),
        features=features,
        reporters=_reporters(raw.get("endpointReports")),
    )


def _reporters(raw: Any) -> tuple[str, ...]:
    """Each report's ``reporter.namespace``, in wire order, duplicates kept.

    An entry without a namespace is skipped; a missing or null list gives none.
    """
    if not isinstance(raw, list):
        return ()
    namespaces = []
    for report in raw:
        reporter = report.get("reporter") if isinstance(report, dict) else None
        namespace = reporter.get("namespace") if isinstance(reporter, dict) else None
        if namespace is not None:
            namespaces.append(str(namespace))
    return tuple(namespaces)


def _parse_feature(raw: dict[str, Any]) -> Feature | None:
    name = raw.get("name")
    if not name:
        return None
    operations = frozenset(
        op["name"] for op in raw.get("operations") or [] if isinstance(op, dict) and op.get("name")
    )
    return Feature(
        name=name,
        instance=raw.get("instance"),
        operations=operations,
        properties=_flatten_properties(raw.get("properties")),
        configuration=raw.get("configuration") or {},
    )


def _flatten_properties(raw: Any) -> dict[str, Any]:
    """Collapse the properties list into a flat name -> value mapping.

    Properties arrive as a list resolved through GraphQL inline fragments, so a
    single feature may contribute several named values.
    """
    flat: dict[str, Any] = {}
    for prop in raw or []:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        values = {
            key: val
            for key, val in prop.items()
            if key not in ("__typename", "name") and val is not None
        }
        flat.update(values)
        # Also file the value under the property's own name. A thermostat's
        # three `Setpoint` entries all select `value`, and would otherwise
        # overwrite each other; `targetSetpoint` / `lowerSetpoint` /
        # `upperSetpoint` is the only thing that tells them apart.
        if isinstance(name, str) and name and len(values) == 1:
            flat[name] = next(iter(values.values()))
    return flat


def _unwrap(value: Any) -> Any:
    """Reduce a wrapped property to its bare value.

    Values arrive wrapped to varying depths: ``{"value": 50}`` for a range,
    ``{"value": 38.8, "scale": "CELSIUS"}`` for a temperature, and occasionally
    ``{"value": {"text": "..."}}`` for a label. Unwrap until something scalar
    falls out.
    """
    seen = 0
    while isinstance(value, dict) and seen < 4:
        if "text" in value:
            return value["text"]
        if "value" in value:
            value = value["value"]
            seen += 1
            continue
        # An action's state is ``{"status": "IDLE"}``; nothing else nests this way.
        if "status" in value:
            return value["status"]
        return None
    return value


def _temperature_range(raw: Any, scale: str) -> tuple[float, float] | None:
    """A ``{minimum, maximum}`` pair of ``{value, scale}`` as floats in ``scale``.

    ``None`` when either end is missing or unreadable, or the pair is inverted.
    A range sent in another scale than the setpoint's is converted.
    """
    if not isinstance(raw, dict) or scale not in _FROM_KELVIN:
        return None
    ends = []
    for end in (raw.get("minimum"), raw.get("maximum")):
        if not isinstance(end, dict):
            return None
        own = str(end.get("scale") or scale).upper()
        try:
            value = float(end["value"])
        except (KeyError, TypeError, ValueError):
            return None
        if own != scale:
            if own not in _TO_KELVIN:
                return None
            value = _FROM_KELVIN[scale](_TO_KELVIN[own](value))
        ends.append(value)
    low, high = ends
    return (low, high) if low <= high else None


def _text(value: Any) -> str | None:
    """Read a label that may be a bare string or a ``{value: {text: ...}}`` blob."""
    unwrapped = _unwrap(value)
    return unwrapped if isinstance(unwrapped, str) else None


def humanise(raw: str) -> str:
    """``Robot.Clean`` -> ``Robot Clean``; ``targetSetpoint`` -> ``Target Setpoint``."""
    words: list[str] = []
    for part in raw.replace("_", ".").split("."):
        words.extend(_CAMEL_BOUNDARY.sub(" ", part).split())
    return " ".join(word[:1].upper() + word[1:] for word in words) or raw


def _label(option: dict[str, Any]) -> str:
    """Best available human label for a mode option, falling back to its value."""
    for path in (
        ("modeResources", "friendlyName"),
        ("friendlyName",),
    ):
        found = _text(_get(option, *path))
        if found:
            return found
    return str(option.get("value"))


def _get(source: Any, *path: str) -> Any:
    """Walk a nested mapping, returning ``None`` if any step is missing."""
    current = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current

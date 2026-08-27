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

from dataclasses import dataclass, field
from typing import Any

from .const import BLOCKED_MANUFACTURERS

# Feature names we know how to interpret.
FEATURE_POWER = "power"
FEATURE_MODE = "mode"
FEATURE_RANGE = "range"
FEATURE_TOGGLE = "toggle"
FEATURE_THERMOSTAT = "thermostat"
FEATURE_TEMPERATURE_SENSOR = "temperatureSensor"
FEATURE_CONNECTIVITY = "connectivity"
FEATURE_ENDPOINT_HEALTH = "endpointHealth"


# The property name a feature type reports its current value under.
#
# Two naming conventions are accepted per feature. The Homebridge plugins'
# query document selects the ``*Value``-suffixed names (``powerStateValue``,
# ``toggleStateValue``, ``thermostatModeValue``) while the live captures in
# ``ref/07``/``ref/08`` are written up with the bare names. The raw response
# JSON was never committed, so rather than guess which the wire actually uses,
# both are tried. See ``threads/002`` Q5 -- once a real capture settles it, the
# losing spelling can be dropped.
_PRIMARY_PROPERTIES = {
    FEATURE_POWER: ("powerStateValue", "powerState"),
    FEATURE_MODE: ("modeValue", "mode"),
    FEATURE_RANGE: ("rangeValue",),
    FEATURE_TOGGLE: ("toggleStateValue", "toggleState"),
    FEATURE_THERMOSTAT: ("targetSetpoint", "value", "thermostatModeValue"),
    FEATURE_TEMPERATURE_SENSOR: ("temperature", "value"),
    FEATURE_CONNECTIVITY: ("connectivity", "reachability"),
}

# Tried in order for feature types not listed above.
_PROPERTY_FALLBACK = (
    "powerStateValue",
    "powerState",
    "modeValue",
    "mode",
    "rangeValue",
    "toggleStateValue",
    "toggleState",
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
    instance: str | None
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
    def ordered(self) -> bool:
        """Whether mode options form a scale (Low..Turbo) or an unordered set.

        Only an ORDERED mode may be mapped to a fan percentage; an UNORDERED one
        must stay a select.
        """
        return str(self.configuration.get("order", "")).upper() == "ORDERED"

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
                return _unwrap(self.properties[prop])
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

    def feature(self, name: str, instance: str | None = None) -> Feature | None:
        """Look a feature up by name and instance."""
        return self.features.get(f"{name}/{instance}" if instance else name)

    def features_named(self, name: str) -> list[Feature]:
        """Every instance of a given feature type, e.g. all ``mode`` features."""
        return [f for f in self.features.values() if f.name == name]

    @property
    def is_on(self) -> bool | None:
        """Power state, or ``None`` when the endpoint has no power feature.

        Some endpoints genuinely have none -- the SmartDrape blind is the worked
        example, and gating discovery on power is why every reference
        implementation cannot see it (ref/06).
        """
        power = self.feature(FEATURE_POWER)
        if power is None or power.value is None:
            return None
        return str(power.value).upper() == "ON"

    @property
    def blocked(self) -> bool:
        """Whether this endpoint may never be written to.

        Standing user directive: the Kohler Anthem runs water.
        """
        return (self.manufacturer or "").strip().lower() in BLOCKED_MANUFACTURERS

    def live_value(self, feature: Feature) -> Any:
        """Current value, with the power-off sentinel filtered out.

        When an endpoint's power is ``OFF``, its *writable* features report ``0``
        -- which is not a member of ``modeOptions`` and sits below the range
        minimum. It is an unknown sentinel, not a reading. Read-only features
        keep reporting real values while off, so they are exempt (ref/07, ref/09).

        The comparison is numeric, not textual: ``mode`` reports the sentinel as
        the string ``"0"`` but ``range`` reports it as the float ``0.0``, and a
        string test silently lets the latter through as a real reading.
        Non-numeric values (``"OFF"`` on power and toggle) are never sentinels.
        """
        value = feature.value
        if value is None or not feature.writable or self.is_on is not False:
            return value
        try:
            is_sentinel = float(value) == 0
        except (TypeError, ValueError):
            is_sentinel = False
        return None if is_sentinel else value


def parse_endpoint(raw: dict[str, Any]) -> Endpoint | None:
    """Build an :class:`Endpoint` from one item of the bulk endpoints query."""
    endpoint_id = raw.get("id") or raw.get("endpointId")
    if not endpoint_id:
        return None

    features: dict[str, Feature] = {}
    for raw_feature in raw.get("features") or []:
        parsed = _parse_feature(raw_feature)
        if parsed is not None:
            features[parsed.key] = parsed

    return Endpoint(
        endpoint_id=endpoint_id,
        friendly_name=raw.get("friendlyName") or endpoint_id,
        display_category=_get(raw, "displayCategories", "primary", "value"),
        # model / manufacturer arrive as {value:{text}} wrappers, though a bare
        # string is tolerated so fixtures and older captures still parse.
        manufacturer=_text(raw.get("manufacturer")),
        model=_text(raw.get("model")),
        serial_number=_text(raw.get("serialNumber")),
        features=features,
    )


def _parse_feature(raw: dict[str, Any]) -> Feature | None:
    name = raw.get("name")
    if not name:
        return None
    operations = frozenset(
        op["name"] for op in raw.get("operations") or [] if op.get("name")
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
    if isinstance(raw, dict):
        return dict(raw)
    flat: dict[str, Any] = {}
    for prop in raw or []:
        if not isinstance(prop, dict):
            continue
        for key, val in prop.items():
            if key != "__typename" and val is not None:
                flat[key] = val
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
        if "value" not in value:
            return None
        value = value["value"]
        seen += 1
    return value


def _text(value: Any) -> str | None:
    """Read a label that may be a bare string or a ``{value: {text: ...}}`` blob."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    unwrapped = _unwrap(value)
    return unwrapped if isinstance(unwrapped, str) else None


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

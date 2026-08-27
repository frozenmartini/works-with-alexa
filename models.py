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


# The property a given feature type reports its current value under.
_PRIMARY_PROPERTY = {
    FEATURE_POWER: "powerState",
    FEATURE_MODE: "mode",
    FEATURE_RANGE: "rangeValue",
    FEATURE_TOGGLE: "toggleState",
    FEATURE_THERMOSTAT: "targetSetpoint",
    FEATURE_TEMPERATURE_SENSOR: "temperature",
    FEATURE_CONNECTIVITY: "connectivity",
}

# Tried in order for feature types not listed above.
_PROPERTY_FALLBACK = (
    "powerState",
    "mode",
    "rangeValue",
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
        return self.configuration.get("friendlyName")

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
        """
        return self.configuration.get("unitOfMeasure")

    # --- current value ------------------------------------------------------
    @property
    def value(self) -> Any:
        """Raw current value, or ``None`` if this feature reports no state.

        The property carrying the value is named after the feature, so prefer the
        name this feature type is known to use before falling back to a scan.
        """
        preferred = _PRIMARY_PROPERTY.get(self.name)
        if preferred and preferred in self.properties:
            return _unwrap(self.properties[preferred])
        for prop in _PROPERTY_FALLBACK:
            if prop in self.properties:
                return _unwrap(self.properties[prop])
        return None

    @property
    def scale(self) -> str | None:
        """Explicit temperature scale, where the feature carries one.

        ``thermostat`` and ``temperatureSensor`` report ``FAHRENHEIT``/
        ``CELSIUS``/``KELVIN``. Bare ``range`` features do not -- see
        :attr:`unit_of_measure`.
        """
        preferred = _PRIMARY_PROPERTY.get(self.name)
        raw = self.properties.get(preferred) if preferred else None
        return raw.get("scale") if isinstance(raw, dict) else None


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
        """
        value = feature.value
        if value is None or not feature.writable:
            return value
        if self.is_on is False and str(value) == "0":
            return None
        return value


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
        manufacturer=raw.get("manufacturer"),
        model=raw.get("model"),
        serial_number=raw.get("serialNumber"),
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
    """Unwrap ``{value: ..., scale: ...}`` style wrappers to a bare value."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _label(option: dict[str, Any]) -> str:
    """Best available human label for a mode option, falling back to its value."""
    for path in (
        ("modeResources", "friendlyName"),
        ("friendlyName",),
    ):
        found = _get(option, *path)
        if isinstance(found, str) and found:
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

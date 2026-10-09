"""Sanitized expiry metadata from Amazon responses; no credentials retained."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re


def normalized(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_EXPIRY_KEYS = {
    "expires", "expiry", "expiration", "expiresin", "expiresat", "expirytime",
    "expirationtime", "expirationdate", "expirydate", "expirationtimestamp",
    "expirytimestamp", "lifetime", "ttl", "expirationseconds", "validuntil",
}
_SAFE_PATH_KEYS = {
    "response", "success", "tokens", "bearer", "data", "access_token",
    "refresh_token", "website_cookies", "cookies", "mac_dms", "registration_data",
    "accessToken", "refreshToken", "access", "refresh", "metadata",
}


def expiry_key(key: str) -> bool:
    name = normalized(key)
    return name in _EXPIRY_KEYS or any(
        name.startswith(prefix) and name[len(prefix):] in _EXPIRY_KEYS
        for prefix in ("accesstoken", "refreshtoken")
    )


def safe_value(value: object) -> object:
    """Only render numeric lifetimes or timezone-qualified ISO dates."""
    if isinstance(value, bool):
        return "[unrecognized expiry value]"
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"-?\d{1,18}(?:\.\d{1,6})?", value):
            return value
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return value
            except ValueError:
                pass
    if value is None:
        return None
    return "[unrecognized expiry value]"


def inspect_exchange(document: object, *, received_at: datetime | None = None) -> dict:
    """Return only allowlisted metadata, never a copy of the response."""
    if not isinstance(document, dict):
        raise ValueError("Expected a JSON object")
    if received_at is not None and received_at.utcoffset() is None:
        raise ValueError("Reception time must include a timezone")
    fields = []
    presence = {"access_token": False, "refresh_token": False}

    def walk(node: object, path: str, scope: str = "unspecified") -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", scope)
            return
        if not isinstance(node, dict):
            return
        names = {normalized(key) for key in node}
        # Amazon's bearer.expires_in is the access-token lifetime. A sibling
        # refresh_token does not make that field a refresh-token expiry.
        if scope == "unspecified" and "accesstoken" in names:
            scope = "access_token"
        for key, value in node.items():
            name = normalized(key)
            if name in ("accesstoken", "refreshtoken"):
                presence["access_token" if name == "accesstoken" else "refresh_token"] |= bool(value)
            label = key if key in _SAFE_PATH_KEYS else "<field>"
            child_scope = scope
            if name in ("bearer", "access", "accesstoken"):
                child_scope = "access_token"
            elif name in ("refresh", "refreshtoken"):
                child_scope = "refresh_token"
            elif name in ("cookies", "websitecookies"):
                child_scope = "cookie"
            elif name in ("macdms", "registrationdata"):
                child_scope = "other_credential"
            if expiry_key(key):
                # Only recognized ASCII expiry keys are safe to show verbatim.
                field_name = name
                if name.startswith("refreshtoken"):
                    child_scope = "refresh_token"
                    field_name = name[len("refreshtoken"):]
                elif name.startswith("accesstoken"):
                    child_scope = "access_token"
                    field_name = name[len("accesstoken"):]
                displayed = safe_value(value)
                expiry_label = key if re.fullmatch(r"[A-Za-z_]+", key) else name
                field = {"path": f"{path}.{expiry_label}", "scope": child_scope, "value": displayed}
                if received_at is not None and field_name == "expiresin" and isinstance(displayed, (int, float, str)):
                    try:
                        seconds = float(displayed)
                        if seconds >= 0:
                            field["estimated_expiry_utc"] = (
                                received_at.astimezone(timezone.utc) + timedelta(seconds=seconds)
                            ).isoformat()
                    except (ValueError, OverflowError):
                        pass
                fields.append(field)
            if isinstance(value, (dict, list)):
                walk(value, f"{path}.{label}", child_scope)

    walk(document, "$")
    explicit_refresh_expiry = any(
        item["scope"] == "refresh_token" and item["value"] is not None
        and item["value"] != "[unrecognized expiry value]" for item in fields
    )
    return {
        "credential_presence": presence,
        "expiry_fields": fields,
        "refresh_token_expiry_reported": explicit_refresh_expiry,
        "note": (
            "Explicit refresh-token expiry metadata found; inspect its field and units."
            if explicit_refresh_expiry else
            "No readable explicit refresh-token expiry found. This does not prove unlimited validity."
        ),
    }


def saved_registration_report(report: dict) -> dict:
    """Allowlist saved report fields again before displaying private storage."""
    fields = []
    for field in report.get("expiry_fields", []):
        if not isinstance(field, dict):
            raise ValueError("Invalid saved expiry field")
        path = field.get("path", "")
        if not isinstance(path, str) or not re.fullmatch(r"\$(?:\.[A-Za-z_<>]+|\[\d+\])+", path):
            raise ValueError("Invalid saved expiry path")
        keys = re.findall(r"\.([A-Za-z_<>]+)", path)
        if any(key not in _SAFE_PATH_KEYS and key != "<field>" and not expiry_key(key) for key in keys):
            raise ValueError("Unrecognized saved path")
        scope = field.get("scope")
        if scope not in {"access_token", "refresh_token", "cookie", "other_credential", "unspecified"}:
            raise ValueError("Invalid expiry scope")
        clean = {"path": path, "scope": scope, "value": safe_value(field.get("value"))}
        if "estimated_expiry_utc" in field:
            clean["estimated_expiry_utc"] = safe_value(field["estimated_expiry_utc"])
        fields.append(clean)
    presence = report.get("credential_presence", {})
    refresh_expiry = any(
        field["scope"] == "refresh_token" and field["value"] is not None
        and field["value"] != "[unrecognized expiry value]" for field in fields
    )
    return {
        "source_endpoint": "/auth/register",
        "received_at_utc": safe_value(report.get("received_at_utc")),
        "credential_presence": {key: presence.get(key) is True for key in ("access_token", "refresh_token")},
        "expiry_fields": fields,
        "refresh_token_expiry_reported": refresh_expiry,
        "note": (
            "Explicit refresh-token expiry metadata found; inspect its field and units."
            if refresh_expiry else
            "No readable explicit refresh-token expiry found. This does not prove unlimited validity."
        ),
    }


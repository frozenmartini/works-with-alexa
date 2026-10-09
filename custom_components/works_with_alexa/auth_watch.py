"""Safe authentication evidence: stable fingerprints, never credential fragments."""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

from .dev_logging import event

_LOGGER = logging.getLogger(__package__ + ".auth_watch")
_TOKEN_KEYS = {"access_token", "refresh_token", "token", "adp_token", "device_private_key"}
_CONTAINERS = {"response", "success", "tokens", "bearer", "error", "mac_dms"}
_NUMERIC = {"expires_in", "token_expires_in", "refresh_token_expires_in", "ttl"}
_CODES = {"invalid_grant", "invalid_token", "unauthorized_client", "invalid_request",
          "InvalidToken", "InvalidGrant", "MissingValue", "ThrottlingException", "AccessDenied"}
TERMINAL_CODES = {"invalid_grant", "invalid_token", "unauthorized_client", "InvalidToken", "InvalidGrant"}
_TERMS = ("expired", "revoked", "invalid", "authentication", "auth time", "unauthorized",
          "missing", "throttl", "denied", "register")


def fingerprint(value: Any) -> str | None:
    """A comparable one-way identifier with no portion of the original secret."""
    if not isinstance(value, str) or not value:
        return None
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:24]


def error_code(document: Any) -> str | None:
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if error is None and isinstance(document.get("response"), dict):
        error = document["response"].get("error")
    code = error.get("code") if isinstance(error, dict) else error
    return code if isinstance(code, str) and code in _CODES else None


def redacted_exchange(document: Any, depth: int = 0) -> Any:
    """Keep known response structure; redact unknown fields and all cookie data.

    Token keys become *_fingerprint. Arbitrary scalar text and object keys are
    withheld, including error messages: even an error can reflect a credential.
    """
    if depth > 8 or not isinstance(document, dict):
        return "[REDACTED]"
    clean: dict[str, Any] = {}
    for index, (key, value) in enumerate(document.items()):
        if key in _TOKEN_KEYS:
            clean[key + "_fingerprint"] = fingerprint(value)
        elif key in _CONTAINERS:
            clean[key] = redacted_exchange(value, depth + 1) if isinstance(value, dict) else (
                value if key == "error" and isinstance(value, str) and value in _CODES else "[REDACTED]"
            )
        elif key in _NUMERIC:
            clean[key] = value if (
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                or isinstance(value, str) and re.fullmatch(r"\d{1,12}", value)
            ) else "[REDACTED]"
        elif key == "token_type":
            clean[key] = value if value in ("bearer", "bearer_token") else "[REDACTED]"
        elif key == "code":
            clean[key] = value if isinstance(value, str) and value in _CODES else "[REDACTED]"
        elif key in ("message", "error_description"):
            clean["message_terms"] = [term for term in _TERMS if isinstance(value, str) and term in value.lower()]
        elif key == "request_id":
            clean["amazon_request_fingerprint"] = fingerprint(value)
        else:
            clean[f"redacted_field_{index}"] = "[REDACTED]"
    return clean


def auth_event(name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    event(_LOGGER, "auth." + name, level=level, **fields)

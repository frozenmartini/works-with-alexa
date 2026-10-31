"""Browser sign-in: Amazon's PKCE device-registration flow, built as it is sent.

The paste-URL approach is demonstrated by superbeetle1973/alexapy-secure.
This independent implementation builds the one sign-in URL and the one
registration request it sends, here and nowhere else. Passwords and OTPs stay
on Amazon's own pages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .auth_watch import auth_event, redacted_exchange
from .const import APP_NAME, APP_VERSION, DEVICE_TYPE, MARKETPLACES
from .protocol import ApiConnectionError, AuthError, extract_tokens, parse_json, raise_for_throttle

# alexapy-secure's working form: the .com handle on every marketplace and the
# offline_access scope.
_HANDLE = "amzn_dp_project_dee_ios"
_SCOPE = "device_auth_access offline_access"
# The already-hex device-type suffix, hex-encoded once more below.
_DEVICE_TYPE_HEX = b"23413249564c5635564d32573831"
# The device metadata cookie: fixed, so computed once. Spaced JSON (the
# json.dumps defaults), base64 with the padding stripped.
_MAP_MD = base64.b64encode(json.dumps({
    "device_user_dictionary": [],
    "device_registration_data": {"software_version": "1"},
    "app_identifier": {
        "app_version": APP_VERSION, "bundle_id": "com.amazon.echo",
    },
}).encode()).decode().rstrip("=")


def direct_device_id(serial: str) -> str:
    """The 120-character client id alexapy-secure's working enrollment sends.

    The serial and the already-hex device-type suffix are hex-encoded together,
    so the suffix is encoded twice. This is the only device id sign-in sends.
    """
    return (serial.encode("ascii") + _DEVICE_TYPE_HEX).hex()


def _b64url(data: bytes) -> str:
    """URL-safe base64 without padding, as OAuth PKCE expects."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class InvalidRedirect(ValueError):
    """The input is not a complete redirect for this marketplace."""


class DirectEnrollment:
    """One flow's temporary verifier and device identity; never persisted."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        # 16 random bytes as uppercase hex (32 chars): the device serial.
        self.device_serial = secrets.token_bytes(16).hex().upper()
        # A 313-byte device-context blob, base64 without padding.
        self.frc = base64.b64encode(secrets.token_bytes(313)).decode("ascii").rstrip("=")
        # RFC 7636 S256: the challenge is the base64url SHA-256 of the verifier.
        self.verifier = _b64url(secrets.token_bytes(32))
        self.challenge = _b64url(hashlib.sha256(self.verifier.encode("ascii")).digest())

    @property
    def device_id(self) -> str:
        """Derived from the serial, so the sign-in and registration ids never drift."""
        return direct_device_id(self.device_serial)

    @property
    def login_url(self) -> str:
        query = {
            "openid.return_to": f"https://www.{self.domain}/ap/maplanding",
            "openid.assoc_handle": _HANDLE,
            "pageId": _HANDLE,
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.mode": "checkid_setup",
            "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
            "openid.oa2.client_id": f"device:{self.device_id}",
            "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
            "openid.oa2.response_type": "code",
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.pape.max_auth_age": "0",
            "openid.oa2.scope": _SCOPE,
            "openid.oa2.code_challenge_method": "S256",
            "openid.oa2.code_challenge": self.challenge,
            "language": MARKETPLACES.get(self.domain, "en_US"),
        }
        return f"https://www.{self.domain}/ap/register?{urlencode(query)}"

    def use_device_serial(self, serial: str) -> None:
        """Keep the entry's device identity when enrolling it again."""
        self.device_serial = serial

    def authorization_code(self, pasted_url: str) -> str:
        """Validate locally before sending anything to Amazon; errors omit input."""
        try:
            parts = urlsplit(pasted_url.strip())
            valid = (
                parts.scheme == "https"
                and parts.netloc == f"www.{self.domain}"
                and parts.path == "/ap/maplanding"
                and not parts.fragment
            )
            codes = parse_qs(parts.query, keep_blank_values=True).get("openid.oa2.authorization_code", [])
        except ValueError:
            raise InvalidRedirect("Invalid Amazon redirect") from None
        if not valid or len(codes) != 1 or not codes[0].strip():
            raise InvalidRedirect("Expected this marketplace's complete maplanding redirect")
        return codes[0]

    async def async_register(self, session: Any, code: str) -> dict[str, Any]:
        """Exchange once, using a client with an isolated cookie jar."""
        payload = {
            "requested_extensions": ["device_info", "customer_info"],
            "cookies": {"website_cookies": [], "domain": f".{self.domain}"},
            "registration_data": {
                "domain": "Device",
                "app_version": APP_VERSION,
                "device_type": DEVICE_TYPE,
                "device_name": f"%FIRST_NAME%'s%DUPE_STRATEGY_1ST%{APP_NAME}",
                "os_version": "16.6",
                "device_serial": self.device_serial,
                "device_model": "iPhone",
                "app_name": APP_NAME,
                "software_version": "1",
            },
            "auth_data": {
                "client_id": self.device_id,
                "authorization_code": code,
                "code_verifier": self.verifier,
                "code_algorithm": "SHA-256",
                "client_domain": "DeviceLegacy",
            },
            "user_context_map": {"frc": self.frc},
            "requested_token_type": ["bearer", "mac_dms", "website_cookies"],
        }
        auth_event("registration.sent", domain=self.domain)
        try:
            async with session.post(
                f"https://api.{self.domain}/auth/register", json=payload,
                headers={"Content-Type": "application/json"},
                cookies={"frc": self.frc, "map-md": _MAP_MD},
            ) as response:
                body = await response.text()
                received_at = datetime.now(timezone.utc)
                status = response.status
        except Exception as err:
            auth_event("registration.transport_failed", level=logging.ERROR, error_type=type(err).__name__)
            raise
        try:
            response_json = parse_json(body)
        except ApiConnectionError:
            response_json = None
        auth_event("registration.received", status=status, response_json=redacted_exchange(response_json))
        raise_for_throttle(body)
        if status == 429 or status >= 500:
            raise ApiConnectionError("Amazon registration is temporarily unavailable")
        if status >= 400:
            auth_event("registration.rejected", level=logging.ERROR, status=status)
            raise AuthError("Amazon rejected the authorization code")
        data = parse_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
            raise ApiConnectionError("Unexpected Amazon registration response")
        if not data["response"].get("success"):
            raise AuthError("Amazon rejected device registration")
        tokens = extract_tokens(data)
        if not tokens["refresh_token"] or not isinstance(tokens["mac_dms"], dict):
            raise AuthError("Amazon registration returned incomplete credentials")
        return {
            "refresh_token": tokens["refresh_token"],
            "mac_dms": tokens["mac_dms"],
            "device_serial": self.device_serial,
            "enrolled_at": received_at.isoformat(),
        }

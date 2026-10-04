"""Direct-browser enrollment using Amazon's PKCE device-registration flow.

The paste-URL approach is demonstrated by superbeetle1973/alexapy-secure.
This independent implementation shares the integration's existing builders
and response handling. Passwords and OTPs stay on Amazon's own pages.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .fingerprint import generate_pkce, new_fingerprint
from .login import build_signin_url, register_url, registration_body
from .protocol import ApiConnectionError, AuthError, extract_tokens, parse_json, raise_for_throttle

_LOCALES = {
    "amazon.com": "en_US", "amazon.co.uk": "en_GB", "amazon.de": "de_DE",
    "amazon.fr": "fr_FR", "amazon.it": "it_IT", "amazon.es": "es_ES",
    "amazon.com.au": "en_AU", "amazon.co.jp": "ja_JP", "amazon.com.br": "pt_BR",
    "amazon.ca": "en_CA", "amazon.com.mx": "es_MX",
}
_DIRECT_APP_VERSION = "2.2.556530.0"


class InvalidRedirect(ValueError):
    """The input is not a complete redirect for this marketplace."""


class DirectEnrollment:
    """One flow's temporary verifier and device identity; never persisted."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.fingerprint = new_fingerprint()
        self.verifier, self.challenge = generate_pkce()
        self.fingerprint["map_md"] = base64.b64encode(json.dumps({
            "device_user_dictionary": [],
            "device_registration_data": {"software_version": "1"},
            "app_identifier": {
                "app_version": _DIRECT_APP_VERSION, "bundle_id": "com.amazon.echo",
            },
        }).encode()).decode().rstrip("=")
        # Match the identity used by alexapy-secure's demonstrated enrollment.
        # Keep the relay's existing 92-character identity separate.
        self.device_id = (
            self.fingerprint["device_serial"].encode("ascii")
            + b"23413249564c5635564d32573831"
        ).hex()

    @property
    def login_url(self) -> str:
        parts = urlsplit(build_signin_url(
            self.domain, self.device_id, self.challenge,
            language=_LOCALES.get(self.domain, "en_US"),
        ))
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        query.update({
            "openid.assoc_handle": "amzn_dp_project_dee_ios",
            "pageId": "amzn_dp_project_dee_ios",
            "openid.oa2.scope": "device_auth_access offline_access",
        })
        return urlunsplit((parts.scheme, parts.netloc, "/ap/register", urlencode(query), ""))

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
        fingerprint = self.fingerprint
        payload = registration_body(
            self.domain, device_serial=fingerprint["device_serial"],
            device_id=self.device_id, authorization_code=code,
            code_verifier=self.verifier, frc=fingerprint["frc"].rstrip("="),
        )
        payload["registration_data"].update({
            "app_version": _DIRECT_APP_VERSION, "os_version": "16.6",
        })
        async with session.post(
            register_url(self.domain), json=payload,
            headers={"Content-Type": "application/json"},
            cookies={"frc": fingerprint["frc"].rstrip("="), "map-md": fingerprint["map_md"]},
        ) as response:
            body = await response.text()
            status = response.status
        raise_for_throttle(body)
        if status == 429 or status >= 500:
            raise ApiConnectionError("Amazon registration is temporarily unavailable")
        if status >= 400:
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
            "device_serial": fingerprint["device_serial"],
        }

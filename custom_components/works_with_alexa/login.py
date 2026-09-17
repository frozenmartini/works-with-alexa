"""Pure builders for the native Amazon login (the alexa-cookie2 flow).

No I/O: the signin URL, the registration body, and the success test are all
constructed here and driven by :mod:`.auth_proxy`. That keeps the fiddly,
easy-to-get-wrong parts under unit test without a browser or a live account.

Where alexa-cookie2 (which logs in successfully) and alexapy (blocked) diverge,
this follows alexa-cookie2, per ref/04 §1.1 and the Q7 finding on threads/002:

* ``device_id`` is the 92-char form (see :mod:`.fingerprint`), not alexapy's
  double-encoded 120-char one.
* the entry point is ``www.<domain>/ap/signin`` with a TLD-specific
  ``assoc_handle``, not alexapy's hardcoded ``amazon.com/ap/register``.

The surrounding OpenID boilerplate is taken verbatim from alexapy, which agrees
with alexa-cookie2 on those fields.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .const import APP_NAME, APP_VERSION, DEVICE_TYPE
from .fingerprint import ALEXA_APP_VERSION

_ASSOC_HANDLE_PREFIX = "amzn_dp_project_dee_ios"

# alexa-cookie2 uses the bare device scope; alexapy appends offline_access. The
# working login uses the bare form (ref/04 §1.1.1).
_OAUTH_SCOPE = "device_auth_access"


def assoc_handle(domain: str) -> str:
    """The ``assoc_handle`` / ``pageId`` for a marketplace.

    The TLD's last segment, underscore-prefixed, except ``.com`` which yields the
    bare prefix (ref/04 §1.1.1): amazon.com -> ...ios, amazon.de -> ...ios_de,
    amazon.co.uk -> ...ios_uk, amazon.co.jp -> ...ios_jp.
    """
    last = domain.rsplit(".", 1)[-1].lower()
    return _ASSOC_HANDLE_PREFIX if last == "com" else f"{_ASSOC_HANDLE_PREFIX}_{last}"


def build_signin_url(
    domain: str,
    device_id: str,
    code_challenge: str,
    *,
    language: str = "en_US",
) -> str:
    """The ``/ap/signin`` entry URL the browser is handed off to.

    ``client_id`` here must equal the one sent to ``/auth/register`` later, which
    is why both derive from the same persisted serial.
    """
    handle = assoc_handle(domain)
    query = {
        "openid.return_to": f"https://www.{domain}/ap/maplanding",
        "openid.assoc_handle": handle,
        "pageId": handle,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{device_id}",
        "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
        "openid.oa2.response_type": "code",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.pape.max_auth_age": "0",
        "openid.oa2.scope": _OAUTH_SCOPE,
        "openid.oa2.code_challenge_method": "S256",
        "openid.oa2.code_challenge": code_challenge,
        "language": language,
    }
    return f"https://www.{domain}/ap/signin?{urlencode(query)}"


def register_url(domain: str) -> str:
    return f"https://api.{domain}/auth/register"


def registration_body(
    domain: str,
    *,
    device_serial: str,
    device_id: str,
    authorization_code: str,
    code_verifier: str,
    frc: str,
    website_cookies: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The ``/auth/register`` POST body for a fresh PKCE exchange.

    Mirrors the shape both references send. ``client_id`` is the 92-char device
    id, matching the signin URL; the response yields the durable refresh token,
    ``mac_dms`` and web cookies (ref/04 §1.1.2).
    """
    return {
        "requested_extensions": ["device_info", "customer_info"],
        "cookies": {
            "website_cookies": website_cookies or [],
            "domain": f".{domain}",
        },
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_type": DEVICE_TYPE,
            "device_name": (
                f"%FIRST_NAME%'s%DUPE_STRATEGY_1ST%{APP_NAME}"
            ),
            "os_version": "17.5.1",
            "device_serial": device_serial,
            "device_model": "iPhone",
            "app_name": APP_NAME,
            "software_version": "1",
        },
        "auth_data": {
            "client_id": device_id,
            "authorization_code": authorization_code,
            "code_verifier": code_verifier,
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "user_context_map": {"frc": frc},
        "requested_token_type": ["bearer", "mac_dms", "website_cookies"],
    }


def extract_authorization_code(query: dict[str, Any]) -> str | None:
    """Read the authorization code out of the ``/ap/maplanding`` redirect query.

    Amazon redirects here with ``openid.oa2.authorization_code`` once login (and
    any MFA) completes. Absent means login is not yet done.
    """
    code = query.get("openid.oa2.authorization_code")
    return code if isinstance(code, str) and code else None


def is_login_complete(path: str, query: dict[str, Any]) -> bool:
    """Whether a navigation represents a finished login.

    Two success paths (ref/04 §1.1.1): the ``/ap/maplanding`` redirect carrying
    an authorization code (a fresh login), or a bounce straight to
    ``/spa/index.html`` (the browser already held a valid Amazon session). Only
    the first yields a code; the caller must handle a code-less success.
    """
    if "/spa/index.html" in path:
        return True
    if path.endswith("/ap/maplanding"):
        return extract_authorization_code(query) is not None
    return False


# Marketplace/app version constants surfaced for callers/tests.
__all__ = [
    "ALEXA_APP_VERSION",
    "assoc_handle",
    "build_signin_url",
    "extract_authorization_code",
    "is_login_complete",
    "register_url",
    "registration_body",
]

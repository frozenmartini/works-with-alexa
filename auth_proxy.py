"""The interactive login: a local relay the user completes Amazon's sign-in through.

This is the one part that must run in Home Assistant -- it needs a live browser
and the ``authcaptureproxy`` relay. All the shape of the flow (URLs, PKCE,
registration body, success detection) lives in :mod:`.login` and :mod:`.protocol`
and is unit-tested; this module is the thin driver and the ``/auth/register``
call.

Why we drive ``authcaptureproxy`` directly rather than reuse alexapy's
``AlexaProxy``: ``AlexaProxy`` builds the 120-char double-encoded ``device_id``
that is the suspected cause of Amazon's block. Everything below feeds the relay
our own correct 92-char id instead. If the login is ever refused, that field is
the first suspect (threads/002 Q7).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from authcaptureproxy import AuthCaptureProxy
from yarl import URL

from .fingerprint import generate_pkce, new_fingerprint
from .login import (
    build_signin_url,
    extract_authorization_code,
    is_login_complete,
    register_url,
    registration_body,
)
from .protocol import AuthError, extract_tokens, parse_json

_LOGGER = logging.getLogger(__name__)

# The relay is a real sign-in; give the user time to complete MFA.
_HTTPX_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)


class LoginProxy:
    """Wraps ``authcaptureproxy`` for one login attempt.

    A fresh instance per attempt; the fingerprint it mints is persisted by the
    config flow and reused on later logins so it does not churn.
    """

    def __init__(
        self,
        domain: str,
        proxy_url: URL,
        *,
        fingerprint: dict[str, str] | None = None,
    ) -> None:
        self._domain = domain
        # Reuse a persisted fingerprint if we have one; otherwise mint and let
        # the caller persist it. PKCE is per-attempt and never stored.
        self._fingerprint = fingerprint or new_fingerprint()
        self._verifier, challenge = generate_pkce()

        self._session = httpx.AsyncClient(
            timeout=_HTTPX_TIMEOUT, follow_redirects=False
        )
        # Seed the device-fingerprint cookies before the flow starts, the way
        # the reference clients do.
        self._session.cookies.set("frc", self._fingerprint["frc"])
        self._session.cookies.set("map-md", self._fingerprint["map_md"])

        signin_url = build_signin_url(
            domain, self._fingerprint["device_id"], challenge
        )
        self._proxy = AuthCaptureProxy(
            proxy_url=proxy_url, host_url=URL(signin_url), session=self._session
        )
        self._proxy.tests = {"login_complete": self._test_complete}

        self._authorization_code: str | None = None

    @property
    def fingerprint(self) -> dict[str, str]:
        """The fingerprint used, for the config flow to persist."""
        return self._fingerprint

    @property
    def access_url(self) -> URL:
        """The URL to hand the browser off to."""
        return self._proxy.access_url()

    def _test_complete(
        self, resp: httpx.Response, data: dict[str, Any], query: dict[str, Any]
    ) -> URL | str | None:
        """Completion test called by the relay on every navigation.

        Returns the caller's callback URL to bounce the browser back to Home
        Assistant once login finishes; ``None`` while it has not.
        """
        if not resp.url:
            return None
        path = resp.url.path
        merged = dict(self._proxy.query)
        merged.update(query)
        if not is_login_complete(path, merged):
            return None
        self._authorization_code = extract_authorization_code(merged)
        callback = self._proxy.init_query.get("callback_url")
        if callback:
            return URL(str(callback))
        return "Login complete. You can close this window."

    async def async_start(self) -> None:
        await self._proxy.start_proxy()

    async def async_stop(self) -> None:
        try:
            await self._proxy.stop_proxy()
        finally:
            if not self._session.is_closed:
                await self._session.aclose()

    async def async_finish(self) -> dict[str, Any]:
        """Exchange the captured authorization code for durable credentials.

        Returns the persisted credential set: refresh token, ``mac_dms``, device
        serial. Raises :class:`AuthError` if the login did not yield a code
        (e.g. the already-logged-in bounce that produces no code) or if Amazon
        rejected the registration.
        """
        if not self._authorization_code:
            raise AuthError(
                "Login completed without an authorization code; try again from a "
                "browser that is not already signed in to Amazon"
            )

        body = registration_body(
            self._domain,
            device_serial=self._fingerprint["device_serial"],
            device_id=self._fingerprint["device_id"],
            authorization_code=self._authorization_code,
            code_verifier=self._verifier,
            frc=self._fingerprint["frc"],
            website_cookies=_cookies_for(self._session, self._domain),
        )

        try:
            response = await self._session.post(
                register_url(self._domain),
                json=body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as err:
            raise AuthError(f"Registration request failed: {err}") from err

        if response.status_code >= 400:
            raise AuthError(
                f"Amazon rejected the registration (HTTP {response.status_code})"
            )

        tokens = extract_tokens(parse_json(response.text))
        if not tokens["refresh_token"]:
            raise AuthError("Registration succeeded but returned no refresh token")

        return {
            "refresh_token": tokens["refresh_token"],
            "mac_dms": tokens["mac_dms"],
            "device_serial": self._fingerprint["device_serial"],
        }


def _cookies_for(session: httpx.AsyncClient, domain: str) -> list[dict[str, str]]:
    """The Amazon cookies captured during login, as register wants them.

    The session only ever talked to Amazon through the relay, so every cookie in
    it is an Amazon cookie; no allowlist is needed here.
    """
    return [{"Name": name, "Value": value} for name, value in session.cookies.items()]

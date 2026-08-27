"""Amazon session handling and the GraphQL transport.

The durable credential is the **refresh token** (plus ``macDms`` and the device
serial). Access token, cookies and CSRF are disposable and rebuilt on every
start; nothing here persists them, and nothing here deletes stored credentials
on an error -- an auth failure raises so Home Assistant can run its reauth flow
instead (ref/10).

Response handling lives in :mod:`.protocol`, which has no I/O dependencies and
carries the unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    APP_NAME,
    APP_VERSION,
    COOKIE_EXCHANGE_PATH,
    CSRF_PATHS,
    DEVICE_TYPE,
    GRAPHQL_PATH,
    USER_AGENT,
)
from .models import Endpoint, parse_endpoint
from .protocol import (
    ApiConnectionError,
    AuthError,
    check_control_result,
    check_graphql_response,
    cookie_pairs,
    exchanged_cookies,
    extract_tokens,
    parse_json,
    raise_for_throttle,
)
from .queries import (
    ENDPOINTS_QUERY,
    SET_ENDPOINT_FEATURES_MUTATION,
    feature_control_request,
)

_LOGGER = logging.getLogger(__name__)

_REFRESH_BACKOFF = (1, 4, 16)

# The iOS-app device shape Amazon expects on the token endpoints. Both /auth/token
# and /ap/exchangetoken/cookies validate the full set -- sending only the subset
# that happens to pass today is what left the cookie exchange one 400 away from
# breaking (ref/04 §1.1.5/§1.1.7, corrected against alexa-cookie2 source).
_DEVICE_SHAPE = {
    "app_name": APP_NAME,
    "app_version": APP_VERSION,
    "di.sdk.version": "6.12.4",
    "di.hw.version": "iPhone",
    "di.os.name": "iOS",
    "di.os.version": "17.5.1",
}


class AmazonAlexaSession:
    """One authenticated session against the Alexa account API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        domain: str,
        device_serial: str,
        refresh_token: str,
        mac_dms: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._domain = domain
        self._device_serial = device_serial
        self._refresh_token = refresh_token
        self._mac_dms = mac_dms or {}

        # Rebuilt every start; never persisted.
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._csrf: str | None = None
        self._lock = asyncio.Lock()

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def refresh_token(self) -> str:
        """Current refresh token.

        Amazon may rotate it, so the config entry should be updated from this
        after a refresh.
        """
        return self._refresh_token

    @property
    def mac_dms(self) -> dict[str, Any]:
        return self._mac_dms

    # --- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Rebuild a usable session from the refresh token alone."""
        async with self._lock:
            await self._async_refresh()
        await self._async_rebuild_web_session()

    async def async_get_account_email(self) -> str | None:
        """Read the authenticated account's email.

        Identifying the account this way, rather than asking the user to type
        it, avoids the mismatch that makes alexapy's ``test_loggedin`` silently
        report failure.
        """
        data = await self._async_request(
            "GET",
            f"https://alexa.{self._domain}/api/users/me",
            params={"platform": "ios", "version": APP_VERSION},
        )
        return data.get("email") if isinstance(data, dict) else None

    # --- token handling -----------------------------------------------------

    async def _async_ensure_token(self) -> None:
        if self._token_valid():
            return
        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            if self._token_valid():
                return
            await self._async_refresh()

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.monotonic() < self._expires_at

    async def _async_refresh(self) -> None:
        """Exchange the refresh token for an access token, cookies and macDms.

        Transport failures are retried before giving up: turning a blip into an
        auth failure is what kills sessions needlessly, and the credential is
        rarely the thing at fault (ref/05).
        """
        payload = {
            **_DEVICE_SHAPE,
            "source_token": self._refresh_token,
            "package_name": "com.amazon.echo",
            "platform": "iOS",
            # Steady-state refresh wants the plain string; the list form is the
            # /auth/register shape and Amazon 400s it here (ref/04 §1.1.7).
            "requested_token_type": "access_token",
            "source_token_type": "refresh_token",
            "current_version": APP_VERSION,
            "device_type": DEVICE_TYPE,
            "device_serial": self._device_serial,
        }
        url = f"https://api.{self._domain}/auth/token"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

        last_error: Exception | None = None
        for attempt, backoff in enumerate(_REFRESH_BACKOFF):
            try:
                async with self._session.post(
                    url, json=payload, headers=headers
                ) as response:
                    body = await response.text()
                    status = response.status
            except (aiohttp.ClientError, TimeoutError) as err:
                last_error = err
            else:
                if status in (401, 403):
                    # The credential itself was refused. Retrying cannot help.
                    raise AuthError(
                        f"Amazon rejected the refresh token (HTTP {status})"
                    )
                raise_for_throttle(body)
                if status < 400:
                    self._apply_tokens(extract_tokens(parse_json(body)))
                    return
                last_error = ApiConnectionError(f"Token refresh returned HTTP {status}")

            if attempt + 1 < len(_REFRESH_BACKOFF):
                await asyncio.sleep(backoff)

        raise ApiConnectionError(
            f"Token refresh failed after {len(_REFRESH_BACKOFF)} attempts"
        ) from last_error

    def _apply_tokens(self, tokens: dict[str, Any]) -> None:
        self._access_token = tokens["access_token"]
        self._expires_at = time.monotonic() + tokens["lifetime"]
        if tokens["refresh_token"]:
            self._refresh_token = tokens["refresh_token"]
        if tokens["mac_dms"]:
            self._mac_dms = tokens["mac_dms"]
        self._load_cookies(tokens["website_cookies"])

    def _load_cookies(self, cookies: Any) -> None:
        """Install the cookies the token exchange handed back.

        The cookie domain has to match the domain the session was issued for or
        requests silently fail (ref/00 §2).
        """
        if pairs := cookie_pairs(cookies):
            self._session.cookie_jar.update_cookies(
                pairs, response_url=URL(f"https://{self._domain}")
            )

    async def _async_rebuild_web_session(self) -> None:
        """Turn the bearer token into a usable web session.

        The bearer alone is rejected by the web API; it needs browser cookies
        (a separate exchange) and the CSRF token those cookies carry. Both are
        redone on every refresh, never persisted (ref/04 §1.1.5-1.1.6).
        """
        await self._async_exchange_cookies()
        await self._async_fetch_csrf()

    async def _async_exchange_cookies(self) -> None:
        """Swap the refresh token for web session cookies.

        This is a distinct call from the bearer refresh, on ``www.<domain>`` and
        form-encoded, not the GraphQL host. An empty result is a hard failure:
        it means no web session was produced (ref/04 §1.1.5).
        """
        try:
            async with self._session.post(
                f"https://www.{self._domain}{COOKIE_EXCHANGE_PATH}",
                data={
                    # Amazon 400s ("MissingValue") without the full device shape;
                    # ref/04 §1.1.5 documented only four fields, alexa-cookie2 sends
                    # all of these. app_name need not match the registration name.
                    **_DEVICE_SHAPE,
                    "source_token_type": "refresh_token",
                    "source_token": self._refresh_token,
                    "requested_token_type": "auth_cookies",
                    "domain": f".{self._domain}",
                },
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "x-amzn-identity-auth-domain": f"api.{self._domain}",
                },
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApiConnectionError(f"Cookie exchange failed: {err}") from err

        if status in (401, 403):
            raise AuthError(f"Cookie exchange rejected the token (HTTP {status})")
        raise_for_throttle(body)
        if status >= 400:
            raise ApiConnectionError(f"Cookie exchange returned HTTP {status}")

        pairs = exchanged_cookies(parse_json(body), self._domain)
        if not pairs:
            raise AuthError("Cookie exchange produced no web session cookies")
        self._session.cookie_jar.update_cookies(
            pairs, response_url=URL(f"https://{self._domain}")
        )

    async def _async_fetch_csrf(self) -> None:
        """Read the CSRF token, which is itself a cookie.

        There is no CSRF endpoint; the first page that sets a ``csrf`` cookie
        wins, tried in a fixed order (ref/04 §1.1.6).
        """
        for path in CSRF_PATHS:
            try:
                await self._async_request(
                    "GET",
                    f"https://alexa.{self._domain}{path}",
                    allow_missing_json=True,
                )
            except ApiConnectionError:
                _LOGGER.debug("CSRF path %s failed; trying the next", path)
            for cookie in self._session.cookie_jar:
                if cookie.key == "csrf":
                    self._csrf = cookie.value
                    return
        raise AuthError("No csrf cookie was issued; the session is not usable")

    # --- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "Content-Type": "application/json; charset=utf-8",
            "Origin": f"https://alexa.{self._domain}",
            "Referer": f"https://alexa.{self._domain}/spa/index.html",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._csrf:
            headers["csrf"] = self._csrf
        return headers

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_missing_json: bool = False,
        _retried: bool = False,
    ) -> Any:
        await self._async_ensure_token()
        try:
            async with self._session.request(
                method, url, json=json_body, params=params, headers=self._headers()
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApiConnectionError(f"Request to {url} failed: {err}") from err

        if status in (401, 403):
            if _retried:
                raise AuthError(f"Amazon returned HTTP {status} after reauthenticating")
            # Exactly one forced refresh-and-retry; a second failure is real.
            _LOGGER.debug("HTTP %s; refreshing the session once and retrying", status)
            async with self._lock:
                self._access_token = None
                await self._async_refresh()
            await self._async_rebuild_web_session()
            return await self._async_request(
                method,
                url,
                json_body=json_body,
                params=params,
                allow_missing_json=allow_missing_json,
                _retried=True,
            )

        raise_for_throttle(body)

        if status >= 400:
            raise ApiConnectionError(f"HTTP {status} from {url}")

        if not body.strip():
            return {}
        try:
            return parse_json(body)
        except ApiConnectionError:
            if allow_missing_json:
                return {}
            raise

    async def _async_graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        result = await self._async_request(
            "POST", f"https://alexa.{self._domain}{GRAPHQL_PATH}", json_body=payload
        )
        return check_graphql_response(result)

    # --- operations ---------------------------------------------------------

    async def async_get_endpoints(self) -> dict[str, Endpoint]:
        """Fetch every endpoint with its live state, in one request."""
        data = await self._async_graphql(ENDPOINTS_QUERY)
        items = (data.get("endpoints") or {}).get("items") or []

        endpoints: dict[str, Endpoint] = {}
        for item in items:
            if (endpoint := parse_endpoint(item)) is not None:
                endpoints[endpoint.endpoint_id] = endpoint

        _LOGGER.debug("Fetched %s endpoints", len(endpoints))
        return endpoints

    async def async_set_feature(
        self,
        endpoint_id: str,
        feature_name: str,
        operation: str,
        *,
        instance: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drive one feature, and confirm the device actually accepted it."""
        request = feature_control_request(
            endpoint_id, feature_name, operation, instance=instance, payload=payload
        )
        data = await self._async_graphql(
            SET_ENDPOINT_FEATURES_MUTATION, {"r": [request]}
        )
        return check_control_result(data, endpoint_id)

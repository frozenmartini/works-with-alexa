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
import re
import time
from typing import Any
from uuid import uuid4

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
from .dev_logging import event, operation as logged_operation
from .scenes import AlexaScene
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
    ACTIVATE_SCENE_MUTATION,
    ENDPOINTS_QUERY,
    SCENES_QUERY,
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

    async def async_refresh_access_token(self) -> None:
        """Refresh the bearer token alone, without cookies or device requests.

        Used by the standalone experiment to test the /auth/token exchange.
        Any rotated refresh token remains available via refresh_token.
        """
        async with self._lock:
            await self._async_refresh()

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
        refresh_started = time.monotonic()
        event(_LOGGER, "auth.refresh.started")
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
                event(_LOGGER, "auth.refresh.sent", attempt=attempt + 1)
                async with self._session.post(
                    url, json=payload, headers=headers
                ) as response:
                    body = await response.text()
                    status = response.status
            except (aiohttp.ClientError, TimeoutError) as err:
                event(_LOGGER, "auth.refresh.transport_failed", error_type=type(err).__name__, attempt=attempt + 1)
                last_error = err
            else:
                event(_LOGGER, "auth.refresh.received", status=status, attempt=attempt + 1)
                if status in (401, 403):
                    event(_LOGGER, "auth.refresh.rejected", level=logging.ERROR, status=status)
                    # The credential itself was refused. Retrying cannot help.
                    raise AuthError(
                        f"Amazon rejected the refresh token (HTTP {status})"
                    )
                raise_for_throttle(body)
                if status < 400:
                    self._apply_tokens(extract_tokens(parse_json(body)))
                    event(_LOGGER, "auth.refresh.completed", elapsed_ms=round((time.monotonic() - refresh_started) * 1000, 3))
                    return
                last_error = ApiConnectionError(f"Token refresh returned HTTP {status}")

            if attempt + 1 < len(_REFRESH_BACKOFF):
                await asyncio.sleep(backoff)

        event(_LOGGER, "auth.refresh.failed", level=logging.ERROR, elapsed_ms=round((time.monotonic() - refresh_started) * 1000, 3))
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
        started = time.monotonic()
        path = URL(url).path
        http_id = uuid4().hex
        event(_LOGGER, "http.sent", method=method, path=path, retry=_retried, http_id=http_id)
        try:
            async with self._session.request(
                method, url, json=json_body, params=params, headers=self._headers()
            ) as response:
                body = await response.text()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as err:
            event(_LOGGER, "http.failed", level=logging.ERROR, method=method, path=path, http_id=http_id,
                  error_type=type(err).__name__, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            raise ApiConnectionError(f"Request to {url} failed: {err}") from err

        event(_LOGGER, "http.received", method=method, path=path, status=status, http_id=http_id,
              retry=_retried, elapsed_ms=round((time.monotonic() - started) * 1000, 3))

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
        match = re.search(r"(?:query|mutation)\s+(\w+)", query)
        with logged_operation(_LOGGER, "graphql", operation=match.group(1) if match else "anonymous"):
            result = await self._async_request(
                "POST", f"https://alexa.{self._domain}{GRAPHQL_PATH}", json_body=payload
            )
            if isinstance(result, dict) and result.get("errors"):
                errors = result["errors"]
                event(_LOGGER, "graphql.errors", level=logging.ERROR, error_count=len(errors))
            return check_graphql_response(result)

    # --- operations ---------------------------------------------------------

    async def async_get_scenes(self) -> dict[str, AlexaScene]:
        """Discover third-party scenes using Amazon's required 20-item pages."""
        scenes: dict[str, AlexaScene] = {}
        tokens: set[str] = set()
        page_input: dict[str, Any] = {
            "limit": 20,
            "locale": "en-US",
            "filter": {"types": ["THIRD_PARTY_SCENE"]},
        }
        while True:
            data = await self._async_graphql(SCENES_QUERY, {"input": page_input.copy()})
            page = data.get("automations")
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ApiConnectionError("Amazon returned an invalid scene list")
            for item in page["items"]:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "THIRD_PARTY_SCENE"
                    and isinstance(item.get("id"), str)
                    and item["id"]
                    and isinstance(item.get("name"), str)
                    and item["name"].strip()
                ):
                    scenes[item["id"]] = AlexaScene(item["id"], item["name"].strip())
            token = page.get("nextToken")
            if not token:
                return scenes
            if not isinstance(token, str) or token in tokens:
                raise ApiConnectionError("Amazon returned invalid scene pagination")
            tokens.add(token)
            page_input["nextToken"] = token

    async def async_activate_scene(self, automation_id: str) -> None:
        """Activate once; Amazon's placeholder response cannot verify the effect."""
        with logged_operation(_LOGGER, "scene.activate"):
            data = await self._async_graphql(
                ACTIVATE_SCENE_MUTATION,
                {"input": {
                    "automationId": automation_id,
                    "automationType": "THIRD_PARTY_SCENE",
                }},
            )
            automation = (data.get("activateAutomation") or {}).get("automation")
            if not automation:
                raise ApiConnectionError("Amazon did not acknowledge scene activation")
            event(_LOGGER, "scene.acknowledgement", level=logging.INFO,
                  response_name=automation.get("name"), response_type=automation.get("type"),
                  last_activated_at=automation.get("lastActivatedAt"), physical_execution_verified=False)

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
        with logged_operation(_LOGGER, "feature.control", feature=feature_name, command=operation):
            data = await self._async_graphql(
                SET_ENDPOINT_FEATURES_MUTATION, {"r": [request]}
            )
            responses = data.get("setEndpointFeatures") or {}
            # Log status codes without the endpoint identifiers or raw payload.
            codes = [item.get("code") for item in
                     [*(responses.get("featureControlResponses") or []), *(responses.get("errors") or [])]
                     if isinstance(item, dict) and isinstance(item.get("code"), str)
                     and re.fullmatch(r"[A-Z_]{1,64}", item["code"])]
            event(_LOGGER, "feature.response", codes=codes)
            result = check_control_result(data, endpoint_id)
            event(_LOGGER, "feature.acknowledgement", feature=feature_name, command=operation,
                  code=result.get("code"))
            return result

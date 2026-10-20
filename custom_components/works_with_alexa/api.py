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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
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
    GRAPHQL_PATH,
    USER_AGENT,
)
from .models import Endpoint, parse_endpoint
from .auth_watch import TERMINAL_CODES, auth_event, error_code, fingerprint, redacted_exchange
from .dev_logging import event, operation as logged_operation
from .scenes import AlexaScene
from .protocol import (
    ApiConnectionError,
    AuthError,
    check_control_result,
    check_graphql_response,
    cookie_pairs,
    extract_tokens,
    parse_json,
    raise_for_throttle,
    WorksWithAlexaError,
)
from .token_expiry import inspect_exchange
from .queries import (
    ACTIVATE_SCENE_MUTATION,
    ENDPOINTS_QUERY,
    SCENES_QUERY,
    SET_ENDPOINT_FEATURES_MUTATION,
    feature_control_request,
)

_LOGGER = logging.getLogger(__name__)

_REFRESH_BACKOFF = (1, 4, 16)

# Scheduled renewal retries a recoverable failure after a minute, then doubles
# the wait up to an hour, so a lasting failure cannot hit Amazon every minute
# while the entry waits for the user to sign in again.
_WATCHER_RETRY = 60
_WATCHER_RETRY_MAX = 3600

# Runtime form fields follow alexapy's Alexa iOS token/cookie exchanges.
# Enrollment is kept separate; no /auth/register call is made during recovery.
_DEVICE_SHAPE = {
    "app_name": APP_NAME,
    "app_version": APP_VERSION,
    "di.sdk.version": "6.12.4",
    "di.hw.version": "iPhone",
    "di.os.name": "iOS",
    "di.os.version": "16.6",
}


def _next_retry(previous: int) -> int:
    """The watcher's wait after another failure: 60 s, doubling, at most an hour."""
    return min(previous * 2, _WATCHER_RETRY_MAX) if previous else _WATCHER_RETRY


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
        on_credentials: Callable[[str, dict[str, Any]], bool | None] | None = None,
        entry_id: str | None = None,
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
        self._on_credentials = on_credentials
        self._entry_id = entry_id
        self._session_id = uuid4().hex
        self._web_refresh_pending = False
        self._watcher: asyncio.Task | None = None
        self._terminal_auth_error: AuthError | None = None

    def _auth(self, name: str, **fields: Any) -> None:
        auth_event(name, session_id=self._session_id, entry_id=self._entry_id, **fields)

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
            await self._async_refresh(reason="startup")
            await self._async_rebuild_web_session()

    async def async_refresh_access_token(self) -> None:
        """Refresh the bearer token alone, without cookies or device requests.

        Used by the standalone experiment to test the /auth/token exchange.
        Any rotated refresh token remains available via refresh_token.
        """
        async with self._lock:
            await self._async_refresh(reason="explicit_token_probe")

    def start_refresh_watcher(self, on_auth_failure: Callable[[AuthError], None]) -> None:
        """Maintain the session even when there are no entity listeners/polls."""
        if self._watcher is None:
            self._watcher = asyncio.create_task(
                self._async_watch_refresh(on_auth_failure), name="works_with_alexa_auth_refresh"
            )
            self._auth("watcher.started", rt_fingerprint=fingerprint(self._refresh_token))

    def stop_refresh_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None
            self._auth("watcher.stopped")

    def detach_client(self) -> None:
        """Release a flow's isolated HA client without closing HA's connector."""
        self.stop_refresh_watcher()
        self._session.detach()

    async def _async_watch_refresh(self, on_auth_failure: Callable[[AuthError], None]) -> None:
        retry_in = 0
        while True:
            await asyncio.sleep(retry_in or max(1, self._expires_at - time.monotonic()))
            try:
                await self._async_ensure_token(reason="scheduled")
            except AuthError as err:
                if not err.root_token_rejected:
                    retry_in = _next_retry(retry_in)
                    self._auth("watcher.retry_scheduled", level=logging.WARNING,
                               stage=err.stage, root_token_rejected=False, retry_in_seconds=retry_in)
                    continue
                self._auth("reauth.required", level=logging.ERROR, stage=err.stage,
                           root_token_rejected=err.root_token_rejected, trigger="watcher")
                on_auth_failure(err)
                return
            except WorksWithAlexaError as err:
                retry_in = _next_retry(retry_in)
                self._auth("watcher.retry_scheduled", level=logging.WARNING,
                           error_type=type(err).__name__, retry_in_seconds=retry_in)
            except Exception as err:
                retry_in = _next_retry(retry_in)
                self._auth("watcher.unexpected_error", level=logging.ERROR,
                           error_type=type(err).__name__, retry_in_seconds=retry_in)
            else:
                retry_in = 0

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
        email = data.get("email") if isinstance(data, dict) else None
        identified = isinstance(email, str) and bool(email.strip())
        self._auth("account.verified" if identified else "account.unidentified", stage="account")
        return email if identified else None

    # --- token handling -----------------------------------------------------

    async def _async_ensure_token(self, *, reason: str = "request") -> None:
        if self._terminal_auth_error is not None:
            raise self._terminal_auth_error
        if self._token_valid() and not self._web_refresh_pending:
            return
        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            if self._terminal_auth_error is not None:
                raise self._terminal_auth_error
            if not self._token_valid():
                await self._async_refresh(reason=reason)
            if self._web_refresh_pending:
                await self._async_rebuild_web_session()

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.monotonic() < self._expires_at

    async def _async_refresh(self, *, reason: str = "request") -> None:
        """Mint an access token; preserve/persist any returned root rotation."""
        started = time.monotonic()
        refresh_id = uuid4().hex
        before_at, before_rt = self._access_token, self._refresh_token
        self._auth("refresh.started", refresh_id=refresh_id, reason=reason,
                   at_fingerprint=fingerprint(before_at), rt_fingerprint=fingerprint(before_rt))
        payload = {
            **_DEVICE_SHAPE, "source_token": self._refresh_token,
            "package_name": "com.amazon.echo", "platform": "iOS",
            "requested_token_type": "access_token", "source_token_type": "refresh_token",
            "current_version": "6.12.4", "previous_version": "6.12.4",
        }
        url = f"https://api.{self._domain}/auth/token"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        last_error: Exception | None = None
        try:
            for attempt, backoff in enumerate(_REFRESH_BACKOFF, 1):
                self._auth("refresh.sent", refresh_id=refresh_id, attempt=attempt,
                           rt_fingerprint=fingerprint(self._refresh_token))
                try:
                    async with self._session.post(url, data=payload, headers=headers) as response:
                        body, status = await response.text(), response.status
                        content_type = response.content_type
                    try:
                        document = parse_json(body)
                    except ApiConnectionError:
                        document = None
                    self._auth("refresh.received", refresh_id=refresh_id, attempt=attempt,
                               status=status, content_type=content_type if isinstance(content_type, str) else None,
                               response_json=redacted_exchange(document), valid_json=document is not None)
                    code = error_code(document)
                    if status == 401 or (status < 500 and code in TERMINAL_CODES):
                        self._terminal_auth_error = AuthError(
                            "Amazon no longer accepts the refresh credential", stage="token",
                            root_token_rejected=True,
                        )
                        raise self._terminal_auth_error
                    raise_for_throttle(body)
                    if status == 429:
                        from .protocol import ThrottledError
                        raise ThrottledError("Amazon is rate-limiting token refresh")
                    if status >= 400:
                        raise ApiConnectionError(f"Token refresh returned HTTP {status}")
                    try:
                        tokens = extract_tokens(document)
                    except AuthError as err:
                        raise ApiConnectionError("Token response has no usable access token") from err
                    self._apply_tokens(tokens)
                    received_at = datetime.now(timezone.utc)
                    self._auth("refresh.completed", refresh_id=refresh_id, reason=reason,
                               at_fingerprint=fingerprint(self._access_token),
                               previous_at_fingerprint=fingerprint(before_at),
                               at_changed=before_at is not None and before_at != self._access_token,
                               at_first_observation=before_at is None,
                               rt_fingerprint=fingerprint(self._refresh_token),
                               previous_rt_fingerprint=fingerprint(before_rt),
                               rt_returned=bool(tokens["refresh_token"]), rt_changed=before_rt != self._refresh_token,
                               refresh_in_seconds=tokens["lifetime"],
                               next_refresh_utc=(received_at + timedelta(seconds=tokens["lifetime"])).isoformat(),
                               expiry=inspect_exchange(document, received_at=received_at),
                               elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    return
                except (aiohttp.ClientError, TimeoutError, ApiConnectionError) as err:
                    from .protocol import ThrottledError
                    if isinstance(err, ThrottledError):
                        raise
                    last_error = err
                    self._auth("refresh.attempt_failed", level=logging.WARNING, refresh_id=refresh_id,
                               attempt=attempt, error_type=type(err).__name__)
                if attempt < len(_REFRESH_BACKOFF):
                    self._auth("refresh.retry_scheduled", refresh_id=refresh_id, retry_in_seconds=backoff)
                    await asyncio.sleep(backoff)
            raise ApiConnectionError("Token refresh failed after retries") from last_error
        except (WorksWithAlexaError, aiohttp.ClientError, TimeoutError) as err:
            self._auth("refresh.failed", level=logging.ERROR, refresh_id=refresh_id,
                       stage="token", error_type=type(err).__name__,
                       root_token_rejected=isinstance(err, AuthError) and err.root_token_rejected,
                       new_login_required=isinstance(err, AuthError),
                       rt_fingerprint=fingerprint(self._refresh_token),
                       elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            raise

    def _apply_tokens(self, tokens: dict[str, Any]) -> None:
        if not isinstance(tokens["access_token"], str) or not tokens["access_token"]:
            raise ApiConnectionError("Token response has an invalid access token")
        if tokens["refresh_token"] is not None and not isinstance(tokens["refresh_token"], str):
            raise ApiConnectionError("Token response has an invalid refresh token")
        if tokens["mac_dms"] is not None and not isinstance(tokens["mac_dms"], dict):
            raise ApiConnectionError("Token response has invalid device credentials")
        old_rt, old_mac = self._refresh_token, self._mac_dms
        self._access_token = tokens["access_token"]
        self._expires_at = time.monotonic() + tokens["lifetime"]
        if tokens["refresh_token"]:
            self._refresh_token = tokens["refresh_token"]
        if tokens["mac_dms"]:
            self._mac_dms = tokens["mac_dms"]
        self._web_refresh_pending = True
        if self._on_credentials and (old_rt != self._refresh_token or old_mac != self._mac_dms):
            persisted = self._on_credentials(self._refresh_token, self._mac_dms)
            if persisted is not False:
                self._auth("credentials.persisted", rt_fingerprint=fingerprint(self._refresh_token),
                           rt_changed=old_rt != self._refresh_token, mac_changed=old_mac != self._mac_dms)
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
        """Rebuild derived web credentials without registering a device again."""
        self._auth("web_session.started", rt_fingerprint=fingerprint(self._refresh_token))
        try:
            await self._async_exchange_cookies()
            await self._async_fetch_csrf()
        except WorksWithAlexaError as err:
            self._auth("web_session.failed", level=logging.ERROR,
                       stage=getattr(err, "stage", "web_session"), error_type=type(err).__name__,
                       root_token_rejected=False, new_login_required=isinstance(err, AuthError))
            raise
        self._web_refresh_pending = False
        self._auth("web_session.completed")

    async def _async_exchange_cookies(self) -> None:
        """Use the root token directly, with alexapy's form and cookie metadata."""
        self._auth("cookies.sent", rt_fingerprint=fingerprint(self._refresh_token))
        try:
            async with self._session.post(
                f"https://www.{self._domain}{COOKIE_EXCHANGE_PATH}",
                data={**_DEVICE_SHAPE, "package_name": "com.amazon.echo", "platform": "iOS",
                      "source_token_type": "refresh_token", "source_token": self._refresh_token,
                      "requested_token_type": "auth_cookies", "domain": f".{self._domain}"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                         "x-amzn-identity-auth-domain": f"api.{self._domain}"},
            ) as response:
                body, status = await response.text(), response.status
            self._auth("cookies.received", status=status)
            if status in (401, 403):
                raise AuthError("Web-cookie exchange rejected the session context", stage="cookies")
            raise_for_throttle(body)
            if status >= 400:
                raise ApiConnectionError(f"Cookie exchange returned HTTP {status}")
            document = parse_json(body)
            response_data = document.get("response", {}) if isinstance(document, dict) else {}
            tokens = response_data.get("tokens", {}) if isinstance(response_data, dict) else {}
            buckets = tokens.get("cookies", {}) if isinstance(tokens, dict) else {}
            cookies = buckets.get(f".{self._domain}", []) if isinstance(buckets, dict) else []
            if not isinstance(cookies, list) or not cookies:
                raise ApiConnectionError("Cookie exchange produced no cookies for this marketplace")
            names = []
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                name, value = item.get("Name"), item.get("Value")
                if not isinstance(name, str) or not isinstance(value, str):
                    continue
                cookie = SimpleCookie()
                cookie[name] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
                morsel = cookie[name]
                morsel["domain"] = f".{self._domain}"
                morsel["path"] = item.get("Path") if isinstance(item.get("Path"), str) else "/"
                if isinstance(item.get("Expires"), str):
                    morsel["expires"] = item["Expires"]
                if item.get("Secure"):
                    morsel["secure"] = True
                if item.get("HttpOnly"):
                    morsel["httponly"] = True
                self._session.cookie_jar.update_cookies(cookie, response_url=URL(f"https://www.{self._domain}"))
                # Cookie names can contain arbitrary text, so fingerprint even those.
                names.append(fingerprint(name))
            if not names:
                raise ApiConnectionError("Cookie exchange contained no usable cookies")
            self._auth("cookies.completed", cookie_count=len(names), cookie_name_fingerprints=names,
                       domain=self._domain, response_json=redacted_exchange(document))
        except (aiohttp.ClientError, TimeoutError, CookieError) as err:
            self._auth("cookies.failed", level=logging.ERROR, error_type=type(err).__name__)
            raise ApiConnectionError("Cookie exchange transport failed") from err
        except WorksWithAlexaError as err:
            self._auth("cookies.failed", level=logging.ERROR, error_type=type(err).__name__,
                       root_token_rejected=False, new_login_required=isinstance(err, AuthError))
            raise

    def _csrf_cookie(self) -> str | None:
        cookies = self._session.cookie_jar.filter_cookies(URL(f"https://alexa.{self._domain}/"))
        morsel = cookies.get("csrf")
        return morsel.value if morsel is not None else None

    async def _async_fetch_csrf(self) -> None:
        """Match alexapy: reuse a valid-domain CSRF cookie or try its page list.

        These are direct HTTP reads. Calling the normal API helper here would
        recursively trigger recovery while the refresh lock is held.
        """
        self._csrf = self._csrf_cookie()
        if self._csrf:
            self._auth("csrf.completed", reused=True)
            return
        reachable = False
        transient_failure = False
        for path in CSRF_PATHS:
            self._auth("csrf.sent", path=path)
            try:
                async with self._session.get(
                    f"https://alexa.{self._domain}{path}", headers=self._headers()
                ) as response:
                    await response.read()
                    status = response.status
                    content_type = response.content_type
                self._auth("csrf.received", path=path, status=status,
                           content_type=content_type if isinstance(content_type, str) else None)
                if status != 200:
                    transient_failure |= status == 429 or status >= 500
                    continue
                reachable = True
            except (aiohttp.ClientError, TimeoutError) as err:
                transient_failure = True
                self._auth("csrf.path_failed", level=logging.WARNING, path=path, error_type=type(err).__name__)
                continue
            self._csrf = self._csrf_cookie()
            if self._csrf:
                self._auth("csrf.completed", reused=False, path=path)
                return
        self._auth("csrf.failed", level=logging.ERROR, root_token_rejected=False)
        if not reachable or transient_failure:
            raise ApiConnectionError("Alexa CSRF pages could not be reached")
        raise AuthError("No usable Alexa CSRF cookie was issued", stage="csrf")

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
                self._auth("api.rejected", level=logging.ERROR, status=status, path=path,
                           root_token_rejected=False, new_login_required=True)
                raise AuthError(f"Amazon returned HTTP {status} after session recovery", stage="api")
            # Exactly one forced refresh-and-retry; a second failure is real.
            _LOGGER.debug("HTTP %s; refreshing the session once and retrying", status)
            self._auth("api.recovery_started", status=status, path=path)
            async with self._lock:
                await self._async_refresh(reason="api_recovery")
                await self._async_rebuild_web_session()
            return await self._async_request(
                method,
                url,
                json_body=json_body,
                params=params,
                allow_missing_json=allow_missing_json,
                _retried=True,
            )

        if status >= 400:
            self._auth("api.failed", level=logging.WARNING, path=path, status=status,
                       root_token_rejected=False, new_login_required=False)
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
            self._auth("api.invalid_response", level=logging.WARNING, path=path, status=status,
                       root_token_rejected=False, new_login_required=False)
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
            try:
                return check_graphql_response(result)
            except AuthError:
                self._auth("api.rejected", level=logging.ERROR, stage="graphql",
                           root_token_rejected=False, new_login_required=True)
                raise

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

"""The auth half of the one session class, :class:`.api.AmazonAlexaSession`.

``SessionAuth`` holds everything the session authenticates with: the refresh
token and ``macDms``, the access token, the web cookies and CSRF, the lock that
serialises every auth step, the root-rejection latch, the scheduled renewal
task, the session's retirement and its release of the HTTP client.
:class:`.api.AmazonAlexaSession` subclasses it and adds the transport and the
account operations.

Retirement is one-way. Once retired, the session sends nothing further, saves
nothing, rebuilds nothing and starts no reauth; the credentials it holds stay
readable so a caller can still copy them out.

This module does no Home Assistant import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
import logging
import time
from typing import Any
from uuid import uuid4

import aiohttp
from yarl import URL

from .auth_watch import TERMINAL_CODES, auth_event, error_code, fingerprint, redacted_exchange
from .const import (
    APP_NAME,
    APP_VERSION,
    COOKIE_EXCHANGE_PATH,
    CSRF_PATHS,
    USER_AGENT,
)
from .protocol import (
    ApiConnectionError,
    AuthError,
    CredentialSaveError,
    SessionRetiredError,
    ThrottledError,
    cookie_pairs,
    extract_tokens,
    parse_json,
    raise_for_throttle,
    WorksWithAlexaError,
)

# Test seams: the retry wait, the watcher wait and every expiry read go through
# these, so a test can replace them without patching asyncio or time globally.
sleep = asyncio.sleep
monotonic = time.monotonic

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


class SessionAuth:
    """Credentials, tokens, web state, the lock, the watcher and retirement."""

    def __init__(
        self,
        client: aiohttp.ClientSession,
        *,
        domain: str,
        refresh_token: str,
        mac_dms: dict[str, Any] | None = None,
        on_credentials: Callable[[str, dict[str, Any]], bool | None] | None = None,
        entry_id: str | None = None,
    ) -> None:
        self._client = client
        self._domain = domain
        self._refresh_token = refresh_token
        self._mac_dms = copy.deepcopy(mac_dms) if mac_dms else {}

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
        self._retired = False
        self._detached = False

    def __repr__(self) -> str:
        return (f"<{type(self).__name__} domain={self._domain!r} "
                f"rt={fingerprint(self._refresh_token)} retired={self._retired}>")

    # --- reads --------------------------------------------------------------

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def mac_dms(self) -> dict[str, Any]:
        """A copy of the current ``macDms``; still readable after retirement."""
        return copy.deepcopy(self._mac_dms)

    @property
    def retired(self) -> bool:
        return self._retired

    @property
    def watcher(self) -> asyncio.Task | None:
        return self._watcher

    def emit(self, name: str, *, level: int = logging.INFO, **fields: Any) -> None:
        auth_event(name, level=level, session_id=self._session_id, entry_id=self._entry_id, **fields)

    def headers(self) -> dict[str, str]:
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

    # --- retirement ---------------------------------------------------------

    def check_active(self) -> None:
        """The retirement checkpoint: refuse any further work once retired."""
        if self._retired:
            raise SessionRetiredError("The session has been retired")

    def _watcher_state(self) -> str:
        task = self._watcher
        if task is None:
            return "none"
        if task.done():
            return "ended"
        return "cancelling" if task.cancelling() else "running"

    def retire(self) -> None:
        """Stop all further work, synchronously. Idempotent; takes no lock."""
        if self._retired:
            return
        state = self._watcher_state()
        self._retired = True
        self._on_credentials = None
        task = self._watcher
        if task is not None and not task.done() and not task.cancelling():
            # The reference is kept until async_release() settles it.
            task.cancel()
            self.emit("watcher.stopped")
        self.emit("session.retired", rt_fingerprint=fingerprint(self._refresh_token),
                  watcher_state=state)

    async def async_retire_after_auth(self) -> None:
        """Let an auth step holding the lock finish (and save), then retire."""
        if self._retired:
            return
        async with self._lock:
            self.retire()

    async def async_release(self) -> None:
        """Retire, wait for the watcher to end, then detach the client once.

        Idempotent; never closes the client and never raises for the
        watcher's outcome.
        """
        self.retire()
        task = self._watcher
        if task is not None and asyncio.current_task() is not task:
            # From inside the loop the wait is skipped: it ends on its own at its
            # next checkpoint.
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                # Cancelled while the watcher settles: detach once it has ended.
                task.add_done_callback(lambda _task: self._detach())
                raise
            if self._watcher is task:
                self._watcher = None
        self._detach()

    def _detach(self) -> None:
        if not self._detached:
            self._detached = True
            self._client.detach()

    # --- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Rebuild a usable session from the refresh token alone."""
        await self.async_rebuild("startup")

    async def async_rebuild(self, reason: str) -> None:
        """Refresh the access token, then rebuild cookies and CSRF; no latch check.

        ``startup`` at setup; ``api_recovery`` once after an API rejection.
        """
        self.check_active()
        async with self._lock:
            self.check_active()
            await self._async_refresh(reason=reason)
            await self._async_rebuild_web_session()

    async def async_refresh_access_token(self) -> None:
        """Refresh the bearer token alone, without cookies or device requests."""
        self.check_active()
        async with self._lock:
            self.check_active()
            await self._async_refresh(reason="explicit_token_probe")

    def start_refresh_watcher(self, on_auth_failure: Callable[[AuthError], None]) -> None:
        """Maintain the session even when there are no entity listeners/polls."""
        if self._watcher is None and not self._retired:
            self._watcher = asyncio.create_task(
                self._async_watch_refresh(on_auth_failure), name="works_with_alexa_auth_refresh"
            )
            self._watcher.add_done_callback(self._watcher_done)
            self.emit("watcher.started", rt_fingerprint=fingerprint(self._refresh_token))

    def _watcher_done(self, task: asyncio.Task) -> None:
        """Retrieve the task's outcome so no exception goes unretrieved."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            self.emit("watcher.unexpected_error", level=logging.ERROR,
                      error_type=type(err).__name__, retry_in_seconds=None)

    async def _async_watch_refresh(self, on_auth_failure: Callable[[AuthError], None]) -> None:
        retry_in = 0
        while True:
            if self._retired:
                return
            await sleep(retry_in or max(1, self._expires_at - monotonic()))
            if self._retired:
                return
            try:
                await self.async_ensure_token(reason="scheduled")
            except SessionRetiredError:
                return
            except AuthError as err:
                if not err.root_token_rejected:
                    retry_in = _next_retry(retry_in)
                    self.emit("watcher.retry_scheduled", level=logging.WARNING,
                              stage=err.stage, root_token_rejected=False, retry_in_seconds=retry_in)
                    continue
                if self._retired:
                    return
                self.emit("reauth.required", level=logging.ERROR, stage=err.stage,
                          root_token_rejected=err.root_token_rejected, trigger="watcher")
                on_auth_failure(err)
                return
            except WorksWithAlexaError as err:
                retry_in = _next_retry(retry_in)
                self.emit("watcher.retry_scheduled", level=logging.WARNING,
                          error_type=type(err).__name__, retry_in_seconds=retry_in)
            except Exception as err:
                retry_in = _next_retry(retry_in)
                self.emit("watcher.unexpected_error", level=logging.ERROR,
                          error_type=type(err).__name__, retry_in_seconds=retry_in)
            else:
                retry_in = 0

    # --- token handling -----------------------------------------------------

    async def async_ensure_token(self, *, reason: str = "request") -> None:
        # Retired before the latch: a retired owner never raises its AuthError.
        self.check_active()
        if self._terminal_auth_error is not None:
            raise self._terminal_auth_error
        if self._token_valid() and not self._web_refresh_pending:
            return
        async with self._lock:
            self.check_active()
            # Another caller may have refreshed while we waited for the lock.
            if self._terminal_auth_error is not None:
                raise self._terminal_auth_error
            if not self._token_valid():
                await self._async_refresh(reason=reason)
            if self._web_refresh_pending:
                await self._async_rebuild_web_session()

    def _token_valid(self) -> bool:
        return bool(self._access_token) and monotonic() < self._expires_at

    async def _async_refresh(self, *, reason: str = "request") -> None:
        """Mint an access token; preserve/persist any returned root rotation."""
        started = monotonic()
        refresh_id = uuid4().hex
        before_at, before_rt = self._access_token, self._refresh_token
        self.emit("refresh.started", refresh_id=refresh_id, reason=reason,
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
                self.check_active()
                self.emit("refresh.sent", refresh_id=refresh_id, attempt=attempt,
                          rt_fingerprint=fingerprint(self._refresh_token))
                try:
                    async with self._client.post(url, data=payload, headers=headers) as response:
                        body, status = await response.text(), response.status
                        content_type = response.content_type
                    # The reply is complete: from here to the commit nothing awaits,
                    # so a fully received, valid rotation is always kept in memory.
                    try:
                        document = parse_json(body)
                    except ApiConnectionError:
                        document = None
                    self.emit("refresh.received", refresh_id=refresh_id, attempt=attempt,
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
                        raise ThrottledError("Amazon is rate-limiting token refresh")
                    if status >= 400:
                        raise ApiConnectionError(f"Token refresh returned HTTP {status}")
                    try:
                        tokens = extract_tokens(document)
                    except AuthError as err:
                        raise ApiConnectionError("Token response has no usable access token") from err
                    rotated = self._apply_tokens(tokens)
                    try:
                        received_at = datetime.now(timezone.utc)
                        self.emit("refresh.completed", refresh_id=refresh_id, reason=reason,
                                  at_fingerprint=fingerprint(self._access_token),
                                  rt_fingerprint=fingerprint(self._refresh_token),
                                  rt_changed=before_rt != self._refresh_token,
                                  refresh_in_seconds=tokens["lifetime"],
                                  next_refresh_utc=(received_at + timedelta(seconds=tokens["lifetime"])).isoformat(),
                                  elapsed_ms=round((monotonic() - started) * 1000, 3))
                    except Exception:
                        # The rotation is committed: a failure while reporting it
                        # (an absurd lifetime, say) must not leave it unsaved.
                        self._save_rotation(*rotated)
                        raise
                    break
                except ThrottledError:
                    raise
                except (aiohttp.ClientError, TimeoutError, ApiConnectionError) as err:
                    last_error = err
                    self.emit("refresh.attempt_failed", level=logging.WARNING, refresh_id=refresh_id,
                              attempt=attempt, error_type=type(err).__name__)
                if attempt < len(_REFRESH_BACKOFF):
                    self.check_active()
                    self.emit("refresh.retry_scheduled", refresh_id=refresh_id, retry_in_seconds=backoff)
                    await sleep(backoff)
            else:
                raise ApiConnectionError("Token refresh failed after retries") from last_error
        except (WorksWithAlexaError, aiohttp.ClientError, TimeoutError) as err:
            self.emit("refresh.failed", level=logging.ERROR, refresh_id=refresh_id,
                      stage="token", error_type=type(err).__name__,
                      root_token_rejected=isinstance(err, AuthError) and err.root_token_rejected,
                      new_login_required=isinstance(err, AuthError),
                      rt_fingerprint=fingerprint(self._refresh_token),
                      elapsed_ms=round((monotonic() - started) * 1000, 3))
            # A reply read across a retirement (a root rejection included) must
            # reach a retired caller as the retired error, never as an AuthError.
            self.check_active()
            raise
        # Outside both trys: a failing save can never re-enter the retry loop
        # and re-send the pre-rotation root.
        self._save_rotation(*rotated)
        # The save callback may itself have retired the owner; a retired owner
        # installs no cookies, and its caller gets the retired error.
        self.check_active()
        # Install the cookies the token exchange handed back. The cookie domain
        # has to match the domain the session was issued for or requests
        # silently fail (ref/00 §2).
        if pairs := cookie_pairs(tokens["website_cookies"]):
            self._client.cookie_jar.update_cookies(
                pairs, response_url=URL(f"https://{self._domain}")
            )

    def _apply_tokens(self, tokens: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Validate and commit a token reply; return the root and ``macDms`` it replaced."""
        if not isinstance(tokens["access_token"], str) or not tokens["access_token"]:
            raise ApiConnectionError("Token response has an invalid access token")
        if tokens["refresh_token"] is not None and not isinstance(tokens["refresh_token"], str):
            raise ApiConnectionError("Token response has an invalid refresh token")
        if tokens["mac_dms"] is not None and not isinstance(tokens["mac_dms"], dict):
            raise ApiConnectionError("Token response has invalid device credentials")
        old_rt, old_mac = self._refresh_token, self._mac_dms
        self._access_token = tokens["access_token"]
        self._expires_at = monotonic() + tokens["lifetime"]
        if tokens["refresh_token"]:
            self._refresh_token = tokens["refresh_token"]
        if tokens["mac_dms"]:
            self._mac_dms = tokens["mac_dms"]
        self._web_refresh_pending = True
        return old_rt, old_mac

    def _save_rotation(self, old_rt: str, old_mac: dict[str, Any]) -> None:
        """Hand a changed root or ``macDms`` to the save callback, once."""
        if not self._on_credentials or (old_rt == self._refresh_token and old_mac == self._mac_dms):
            return
        try:
            persisted = self._on_credentials(self._refresh_token, self._mac_dms)
        except Exception as err:
            self.emit("credentials.save_failed", level=logging.ERROR,
                      rt_fingerprint=fingerprint(self._refresh_token), error_type=type(err).__name__)
            self._on_credentials = None
            raise CredentialSaveError("Rotated credentials could not be saved") from err
        if persisted is not False:
            self.emit("credentials.persisted", rt_fingerprint=fingerprint(self._refresh_token),
                      rt_changed=old_rt != self._refresh_token, mac_changed=old_mac != self._mac_dms)

    async def _async_rebuild_web_session(self) -> None:
        """Rebuild derived web credentials without registering a device again."""
        self.check_active()
        self.emit("web_session.started", rt_fingerprint=fingerprint(self._refresh_token))
        try:
            await self._async_exchange_cookies()
            await self._async_fetch_csrf()
        except WorksWithAlexaError as err:
            self.emit("web_session.failed", level=logging.ERROR,
                      stage=getattr(err, "stage", "web_session"), error_type=type(err).__name__,
                      root_token_rejected=False, new_login_required=isinstance(err, AuthError))
            raise
        self._web_refresh_pending = False
        self.emit("web_session.completed")

    async def _async_exchange_cookies(self) -> None:
        """Use the root token directly, with alexapy's form and cookie metadata."""
        self.emit("cookies.sent", rt_fingerprint=fingerprint(self._refresh_token))
        try:
            async with self._client.post(
                f"https://www.{self._domain}{COOKIE_EXCHANGE_PATH}",
                data={**_DEVICE_SHAPE, "package_name": "com.amazon.echo", "platform": "iOS",
                      "source_token_type": "refresh_token", "source_token": self._refresh_token,
                      "requested_token_type": "auth_cookies", "domain": f".{self._domain}"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                         "x-amzn-identity-auth-domain": f"api.{self._domain}"},
            ) as response:
                body, status = await response.text(), response.status
            self.emit("cookies.received", status=status)
            self.check_active()
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
                self._client.cookie_jar.update_cookies(cookie, response_url=URL(f"https://www.{self._domain}"))
                # Cookie names can contain arbitrary text, so fingerprint even those.
                names.append(fingerprint(name))
            if not names:
                raise ApiConnectionError("Cookie exchange contained no usable cookies")
            self.emit("cookies.completed", cookie_count=len(names), cookie_name_fingerprints=names,
                      domain=self._domain, response_json=redacted_exchange(document))
        except (aiohttp.ClientError, TimeoutError, CookieError) as err:
            self.emit("cookies.failed", level=logging.ERROR, error_type=type(err).__name__)
            raise ApiConnectionError("Cookie exchange transport failed") from err
        except WorksWithAlexaError as err:
            self.emit("cookies.failed", level=logging.ERROR, error_type=type(err).__name__,
                      root_token_rejected=False, new_login_required=isinstance(err, AuthError))
            raise

    def _csrf_cookie(self) -> str | None:
        cookies = self._client.cookie_jar.filter_cookies(URL(f"https://alexa.{self._domain}/"))
        morsel = cookies.get("csrf")
        return morsel.value if morsel is not None else None

    async def _async_fetch_csrf(self) -> None:
        """Match alexapy: reuse a valid-domain CSRF cookie or try its page list.

        These are direct HTTP reads. Calling the session's request helper here
        would recursively trigger recovery while the refresh lock is held.
        """
        self._csrf = self._csrf_cookie()
        if self._csrf:
            self.emit("csrf.completed", reused=True)
            return
        reachable = False
        transient_failure = False
        for path in CSRF_PATHS:
            self.check_active()
            self.emit("csrf.sent", path=path)
            try:
                async with self._client.get(
                    f"https://alexa.{self._domain}{path}", headers=self.headers()
                ) as response:
                    await response.read()
                    status = response.status
                    content_type = response.content_type
                self.check_active()
                self.emit("csrf.received", path=path, status=status,
                          content_type=content_type if isinstance(content_type, str) else None)
                if status != 200:
                    transient_failure |= status == 429 or status >= 500
                    continue
                reachable = True
            except (aiohttp.ClientError, TimeoutError) as err:
                transient_failure = True
                self.emit("csrf.path_failed", level=logging.WARNING, path=path, error_type=type(err).__name__)
                continue
            self._csrf = self._csrf_cookie()
            if self._csrf:
                self.emit("csrf.completed", reused=False, path=path)
                return
        self.check_active()
        self.emit("csrf.failed", level=logging.ERROR, root_token_rejected=False)
        if not reachable or transient_failure:
            raise ApiConnectionError("Alexa CSRF pages could not be reached")
        raise AuthError("No usable Alexa CSRF cookie was issued", stage="csrf")

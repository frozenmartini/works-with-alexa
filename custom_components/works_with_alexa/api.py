"""The Alexa account session facade and the GraphQL transport.

The durable credential is the **refresh token** (plus ``macDms`` and the device
serial). Access token, cookies and CSRF are disposable and rebuilt on every
start; nothing here persists them, and nothing here deletes stored credentials
on an error -- an auth failure raises so Home Assistant can run its reauth flow
instead (ref/10).

Every credential, the auth lock, the scheduled renewal and retirement live in
:class:`.session_auth.SessionAuth`; this facade holds only the HTTP client and
that owner, and asks it for readiness, headers and forced recovery.

Response handling lives in :mod:`.protocol`, which has no I/O dependencies and
carries the unit tests.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import logging
import re
from typing import Any
from uuid import uuid4

import aiohttp
from yarl import URL

from .const import (
    APP_VERSION,
    GRAPHQL_PATH,
)
from .models import Endpoint, parse_endpoint
from .auth_watch import fingerprint
from .dev_logging import event, operation as logged_operation
from .scenes import AlexaScene
from .protocol import (
    ApiConnectionError,
    AuthError,
    check_control_result,
    check_graphql_response,
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
from . import session_auth
from .session_auth import SessionAuth

_LOGGER = logging.getLogger(__name__)


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
        self._owner = SessionAuth(
            session,
            domain=domain,
            device_serial=device_serial,
            refresh_token=refresh_token,
            mac_dms=mac_dms,
            on_credentials=on_credentials,
            entry_id=entry_id,
        )
        self._detached = False

    def __repr__(self) -> str:
        return (f"<{type(self).__name__} domain={self.domain!r} "
                f"rt={fingerprint(self.refresh_token)} retired={self._owner.retired}>")

    def _auth(self, name: str, **fields: Any) -> None:
        self._owner.emit(name, **fields)

    @property
    def auth(self) -> SessionAuth:
        """The auth owner: credentials, tokens, lock, watcher and retirement."""
        return self._owner

    @property
    def domain(self) -> str:
        return self._owner.domain

    @property
    def refresh_token(self) -> str:
        """Current refresh token.

        Amazon may rotate it, so the config entry should be updated from this
        after a refresh. Still readable after retirement.
        """
        return self._owner.refresh_token

    @property
    def mac_dms(self) -> dict[str, Any]:
        """A copy of the current ``macDms``; still readable after retirement."""
        return copy.deepcopy(self._owner.mac_dms)

    # --- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Rebuild a usable session from the refresh token alone."""
        await self._owner.async_setup()

    async def async_refresh_access_token(self) -> None:
        """Refresh the bearer token alone, without cookies or device requests.

        Used by the standalone experiment to test the /auth/token exchange.
        Any rotated refresh token remains available via refresh_token.
        """
        await self._owner.async_refresh_access_token()

    def start_refresh_watcher(self, on_auth_failure: Callable[[AuthError], None]) -> None:
        """Maintain the session even when there are no entity listeners/polls."""
        self._owner.start_refresh_watcher(on_auth_failure)

    def stop_refresh_watcher(self) -> None:
        self._owner.stop_refresh_watcher()

    def retire(self) -> None:
        """Stop all further work at once: no request, save, rebuild or reauth."""
        self._owner.retire()

    async def async_retire_after_auth(self) -> None:
        """Wait for an auth step in progress to finish, then retire."""
        await self._owner.async_retire_after_auth()

    async def async_shutdown(self) -> None:
        """Retire, then wait for the watcher to end. Never touches the client."""
        await self._owner.async_shutdown()

    async def async_release(self) -> None:
        """Shut down, then release the HTTP client once (detach, never close)."""
        await self.async_shutdown()
        if not self._detached:
            self._detached = True
            self._session.detach()

    async def async_get_account_email(self) -> str | None:
        """Read the authenticated account's email.

        Identifying the account this way, rather than asking the user to type
        it, avoids the mismatch that makes alexapy's ``test_loggedin`` silently
        report failure.
        """
        data = await self._async_request(
            "GET",
            f"https://alexa.{self.domain}/api/users/me",
            params={"platform": "ios", "version": APP_VERSION},
        )
        email = data.get("email") if isinstance(data, dict) else None
        identified = isinstance(email, str) and bool(email.strip())
        self._auth("account.verified" if identified else "account.unidentified", stage="account")
        return email if identified else None

    # --- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return self._owner.headers()

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
        await self._owner.async_ensure_token()
        self._owner.check_active()
        started = session_auth.monotonic()
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
                  error_type=type(err).__name__, elapsed_ms=round((session_auth.monotonic() - started) * 1000, 3))
            raise ApiConnectionError(f"Request to {url} failed: {err}") from err

        event(_LOGGER, "http.received", method=method, path=path, status=status, http_id=http_id,
              retry=_retried, elapsed_ms=round((session_auth.monotonic() - started) * 1000, 3))
        # Retired during the read: no forced recovery, no retry, no result.
        self._owner.check_active()

        if status in (401, 403):
            if _retried:
                self._auth("api.rejected", level=logging.ERROR, status=status, path=path,
                           root_token_rejected=False, new_login_required=True)
                raise AuthError(f"Amazon returned HTTP {status} after session recovery", stage="api")
            # Exactly one forced refresh-and-retry; a second failure is real.
            _LOGGER.debug("HTTP %s; refreshing the session once and retrying", status)
            self._auth("api.recovery_started", status=status, path=path)
            await self._owner.async_force_recovery()
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
                "POST", f"https://alexa.{self.domain}{GRAPHQL_PATH}", json_body=payload
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

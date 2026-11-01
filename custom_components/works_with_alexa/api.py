"""The Alexa account session: one object, auth plus transport and operations.

The durable credential is the **refresh token** (plus ``macDms``). Access
token, cookies and CSRF are disposable and rebuilt on every start; nothing here
persists them, and nothing here deletes stored credentials on an error -- an
auth failure raises so Home Assistant can run its reauth flow instead (ref/10).

:class:`AmazonAlexaSession` is a :class:`.session_auth.SessionAuth`: every
credential, the auth lock, the scheduled renewal, retirement and the client's
release live in that base class; this module adds the request helper, the
GraphQL transport and the account operations.

Response handling lives in :mod:`.protocol`, which has no I/O dependencies.
"""

from __future__ import annotations

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


class AmazonAlexaSession(SessionAuth):
    """One authenticated session against the Alexa account API."""

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
        self.emit("account.verified" if identified else "account.unidentified", stage="account")
        return email if identified else None

    # --- transport ----------------------------------------------------------

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> Any:
        await self.async_ensure_token()
        self.check_active()
        started = session_auth.monotonic()
        path = URL(url).path
        http_id = uuid4().hex
        event(_LOGGER, "http.sent", method=method, path=path, retry=_retried, http_id=http_id)
        try:
            async with self._client.request(
                method, url, json=json_body, params=params, headers=self.headers()
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
        self.check_active()

        if status in (401, 403):
            if _retried:
                self.emit("api.rejected", level=logging.ERROR, status=status, path=path,
                          root_token_rejected=False, new_login_required=True)
                raise AuthError(f"Amazon returned HTTP {status} after session recovery", stage="api")
            # Exactly one forced refresh-and-retry; a second failure is real.
            _LOGGER.debug("HTTP %s; refreshing the session once and retrying", status)
            self.emit("api.recovery_started", status=status, path=path)
            await self.async_rebuild("api_recovery")
            return await self._async_request(
                method,
                url,
                json_body=json_body,
                params=params,
                _retried=True,
            )

        if status >= 400:
            self.emit("api.failed", level=logging.WARNING, path=path, status=status,
                      root_token_rejected=False, new_login_required=False)
        raise_for_throttle(body)

        if status >= 400:
            raise ApiConnectionError(f"HTTP {status} from {url}")

        if not body.strip():
            return {}
        try:
            return parse_json(body)
        except ApiConnectionError:
            self.emit("api.invalid_response", level=logging.WARNING, path=path, status=status,
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
                self.emit("api.rejected", level=logging.ERROR, stage="graphql",
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

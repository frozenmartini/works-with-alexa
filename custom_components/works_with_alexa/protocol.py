"""Pure request/response handling for the Alexa account API.

Deliberately free of Home Assistant and aiohttp imports so the fiddly parts --
errors arriving inside an HTTP 200, throttling signalled in the body, token
expiry arithmetic -- can be tested without a network or a Home Assistant
install. :mod:`.api` is the I/O shell around this.
"""

from __future__ import annotations

import json
from typing import Any

from .const import THROTTLE_MARKERS, TOKEN_EXPIRY_MARGIN

# Amazon's bearer tokens are short-lived. An absent expiry means "assume an
# hour", never "never expires".
DEFAULT_TOKEN_LIFETIME = 3600
MIN_TOKEN_LIFETIME = 60

# Compared against the message with whitespace removed, so these must be too.
_AUTH_MARKERS = ("unauthorized", "invalidtoken", "notauthenticated", "accessdenied")


class WorksWithAlexaError(Exception):
    """Base error."""


class AuthError(WorksWithAlexaError):
    """An auth layer failed; this need not mean the root token was rejected."""

    def __init__(self, message: str, *, stage: str = "session", root_token_rejected: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.root_token_rejected = root_token_rejected


class ApiConnectionError(WorksWithAlexaError):
    """Transient failure. Maps to ConfigEntryNotReady / UpdateFailed."""


class ThrottledError(ApiConnectionError):
    """Amazon is rate-limiting us.

    Signalled in the response *body*, not the status code, so a throttled reply
    can arrive as a perfectly ordinary HTTP 200 (ref/00 §6).
    """


class CommandError(WorksWithAlexaError):
    """A write was accepted by the transport but rejected by the device."""


def raise_for_throttle(body: str) -> None:
    """Detect throttling, which does not use HTTP 429."""
    if any(marker in body for marker in THROTTLE_MARKERS):
        raise ThrottledError("Amazon is rate-limiting this account")


def parse_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as err:
        raise ApiConnectionError("Response was not JSON") from err


def token_lifetime(expires_in: Any) -> int:
    """Seconds to treat a freshly issued access token as good for.

    Refreshing early by a margin avoids racing the expiry on a slow request.
    """
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = DEFAULT_TOKEN_LIFETIME
    if seconds <= 0:
        seconds = DEFAULT_TOKEN_LIFETIME
    return max(seconds - TOKEN_EXPIRY_MARGIN, MIN_TOKEN_LIFETIME)


def extract_tokens(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the useful fields out of a token-exchange response.

    Amazon nests these under ``response.success.tokens``; some paths return them
    flat. Accept either rather than depending on the wrapper being present.
    """
    if not isinstance(data, dict):
        raise AuthError("Token response was not an object")

    tokens = data
    nested = data.get("response")
    if isinstance(nested, dict):
        success = nested.get("success")
        if isinstance(success, dict) and isinstance(success.get("tokens"), dict):
            tokens = success["tokens"]

    bearer = tokens.get("bearer") if isinstance(tokens.get("bearer"), dict) else {}

    access_token = bearer.get("access_token") or tokens.get("access_token")
    if not access_token:
        raise AuthError("Token response contained no access token")

    return {
        "access_token": access_token,
        "lifetime": token_lifetime(bearer.get("expires_in") or tokens.get("expires_in")),
        # A rotated refresh token must be kept or the next cold start fails.
        "refresh_token": bearer.get("refresh_token") or tokens.get("refresh_token"),
        "mac_dms": tokens.get("mac_dms"),
        "website_cookies": tokens.get("website_cookies") or [],
    }


def cookie_pairs(cookies: Any) -> list[tuple[str, str]]:
    """Normalise the ``website_cookies`` list into name/value pairs.

    Amazon capitalises these inconsistently between responses.
    """
    pairs: list[tuple[str, str]] = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("Name") or cookie.get("name")
        value = cookie.get("Value") or cookie.get("value")
        if name and value is not None:
            pairs.append((str(name), str(value)))
    return pairs


def exchanged_cookies(data: Any, domain: str) -> list[tuple[str, str]]:
    """Pull the session cookies out of an ``/ap/exchangetoken/cookies`` reply.

    They arrive keyed by the dotted domain, e.g. ``.amazon.com``. The absence of
    that key is a hard error upstream (ref/04 §1.1.5): it means the exchange did
    not actually produce a web session, so callers should treat an empty result
    as a failure rather than proceeding cookieless.
    """
    if not isinstance(data, dict):
        return []
    response = data.get("response")
    tokens = response.get("tokens") if isinstance(response, dict) else None
    bucket = tokens.get("cookies") if isinstance(tokens, dict) else None
    if not isinstance(bucket, dict):
        return []
    # Prefer the exact dotted-domain key; fall back to whatever single bucket
    # exists, since Amazon occasionally returns the effective marketplace domain.
    entries = bucket.get(f".{domain}")
    if entries is None and len(bucket) == 1:
        entries = next(iter(bucket.values()))
    return cookie_pairs(entries)


def check_graphql_response(result: Any) -> dict[str, Any]:
    """Unwrap a GraphQL reply, raising on the errors hidden inside a 200."""
    if not isinstance(result, dict):
        raise ApiConnectionError("GraphQL response was not an object")

    if errors := result.get("errors"):
        message = "; ".join(
            str(error.get("message", error) if isinstance(error, dict) else error)
            for error in errors
            if error
        )
        raise_for_throttle(message)
        if any(marker in message.lower().replace(" ", "") for marker in _AUTH_MARKERS):
            raise AuthError(f"GraphQL rejected the session: {message}")
        raise ApiConnectionError(f"GraphQL error: {message}")

    data = result.get("data")
    if data is None:
        raise ApiConnectionError("GraphQL response contained no data")
    return data


def check_control_result(data: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
    """Verify a write actually succeeded.

    ``errors`` sits alongside the responses rather than replacing them, so a
    batch can partially succeed and a 200 proves nothing on its own (ref/00 §5).
    """
    result = data.get("setEndpointFeatures") or {}

    for error in result.get("errors") or []:
        if not isinstance(error, dict):
            continue
        if error.get("endpointId") in (endpoint_id, None):
            raise CommandError(
                f"{endpoint_id} rejected the command: {error.get('code', 'unknown')}"
            )

    for response in result.get("featureControlResponses") or []:
        if not isinstance(response, dict):
            continue
        if response.get("endpointId") != endpoint_id:
            continue
        code = response.get("code")
        if code and code != "SUCCESS":
            raise CommandError(f"{endpoint_id} returned {code}")
        return response

    raise CommandError(f"No response for {endpoint_id}")

"""The interactive login relay.

A real same-origin reverse proxy: the browser talks only to Home Assistant, and
every Amazon host in the flow is routed and rewritten so the page, its scripts,
and the proof-of-work CVF widget all stay on one origin. Without that, the CVF
XHR is cross-origin and Amazon blocks the login (see ref/04 and threads/002 Q8).

The rewriting rules live in :mod:`.relay` and are unit-tested. This module is the
aiohttp plumbing around them plus the ``/auth/register`` exchange. It runs only
inside a config flow and needs a live browser to exercise.
"""

from __future__ import annotations

import logging
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from aiohttp import web
from yarl import URL

from .fingerprint import generate_pkce, new_fingerprint
from .login import (
    build_signin_url,
    is_login_complete,
    register_url,
    registration_body,
)
from .protocol import AuthError, extract_tokens, parse_json
from .relay import (
    amazon_hosts,
    host_from_proxy_path,
    maplanding_code,
    origin_for,
    replace_hosts,
    rewrite_location,
    rewrite_referer,
    strip_host_prefix,
)

_LOGGER = logging.getLogger(__name__)

# Only text bodies carry URLs worth rewriting; images and fonts pass through.
_REWRITE_TYPES = ("text/", "javascript", "json", "application/x-www-form")
# Hop-by-hop and proxy headers we must not forward in either direction.
_STRIP_REQUEST = {"host", "connection", "origin", "referer", "cookie", "accept-encoding"}
_STRIP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding", "connection"}


class LoginRelay:
    """Serves one login attempt through a same-origin rewriting proxy."""

    def __init__(
        self,
        domain: str,
        mount_path: str,
        proxy_base: str,
        *,
        fingerprint: dict[str, str] | None = None,
    ) -> None:
        """``mount_path`` is the relay's path on HA (e.g. ``/auth/.../proxy``);
        ``proxy_base`` is the full browser-facing URL prefix ending in ``/``.
        """
        self._domain = domain
        self._mount_path = mount_path
        self._proxy_base = proxy_base
        self._hosts = amazon_hosts(domain)

        self._fingerprint = fingerprint or new_fingerprint()
        self._verifier, challenge = generate_pkce()
        self._signin = URL(build_signin_url(domain, self._fingerprint["device_id"], challenge))

        # The browser is the cookie store; we forward its Cookie header upstream
        # and mirror Set-Cookie back. A separate copy is accumulated for the
        # final registration call. frc/map-md are seeded so Amazon sees the
        # device fingerprint.
        self._client: aiohttp.ClientSession | None = None
        self._cookies: dict[str, str] = {
            "frc": self._fingerprint["frc"],
            "map-md": self._fingerprint["map_md"],
        }
        self._authorization_code: str | None = None
        self._callback_url: str | None = None

    @property
    def fingerprint(self) -> dict[str, str]:
        return self._fingerprint

    async def async_start(self) -> None:
        # DummyCookieJar: we manage cookies by hand so the browser stays the
        # single source of truth, as http-proxy-middleware does.
        self._client = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def async_stop(self) -> None:
        if self._client and not self._client.closed:
            await self._client.close()

    # --- the proxy handler --------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Proxy one browser request to the right Amazon host and rewrite the reply."""
        if self._client is None:
            await self.async_start()

        tail = request.rel_url.raw_path_qs[len(self._mount_path):] or "/"

        # Remember the callback to resume the HA flow, passed on the first hit.
        if "callback_url" in request.query and not self._callback_url:
            self._callback_url = request.query["callback_url"]

        host = host_from_proxy_path(tail, self._hosts)
        if host is None:
            # Initial hit (or anything without a host prefix): serve the signin
            # page from the marketplace's www host.
            host = f"www.{self._domain}"
            upstream = self._signin
        else:
            upstream = URL(f"https://{host}").join(URL(strip_host_prefix(tail, host)))

        return await self._proxy(request, host, upstream)

    async def _proxy(
        self, request: web.Request, host: str, upstream: URL
    ) -> web.StreamResponse:
        body = await request.read()
        headers = self._request_headers(request, host)

        assert self._client is not None
        try:
            async with self._client.request(
                request.method,
                upstream,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ) as resp:
                raw = await resp.read()
                status = resp.status
                resp_headers = resp.headers.copy()
                content_type = resp.headers.get("Content-Type", "")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Relay upstream error for %s: %s", upstream, err)
            return web.Response(status=502, text="Upstream error")

        # Success: Amazon redirected to maplanding with the authorization code.
        location = resp_headers.get("Location", "")
        if code := self._detect_success(request, upstream, location):
            self._authorization_code = code
            target = self._callback_url or f"{self._proxy_base}"
            return web.HTTPFound(target)

        self._capture_cookies(resp_headers)
        return self._build_response(status, resp_headers, raw, content_type, host)

    def _detect_success(
        self, request: web.Request, upstream: URL, location: str
    ) -> str | None:
        """Return the authorization code if this navigation finished login."""
        for candidate in (location, str(upstream)):
            if not candidate:
                continue
            path = URL(candidate).path if "://" in candidate else candidate.split("?")[0]
            query = _query_of(candidate)
            if is_login_complete(path, query):
                return maplanding_code(candidate) or self._authorization_code
        return None

    # --- header / cookie / body helpers -------------------------------------

    def _request_headers(self, request: web.Request, host: str) -> dict[str, str]:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST
        }
        headers["Host"] = host
        headers["Origin"] = origin_for(host)
        if referer := rewrite_referer(
            request.headers.get("Referer", ""), self._proxy_base, self._hosts
        ):
            headers["Referer"] = referer
        # Forward the browser's cookies plus the seeded fingerprint.
        browser = request.headers.get("Cookie", "")
        merged = dict(_parse_cookie_header(browser))
        merged.update(self._cookies)
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
        # Ask upstream for identity encoding so we can rewrite the body.
        headers["Accept-Encoding"] = "identity"
        return headers

    def _capture_cookies(self, resp_headers: "aiohttp.typedefs.LooseHeaders") -> None:
        for raw in resp_headers.getall("Set-Cookie", []):
            jar = SimpleCookie()
            jar.load(raw)
            for name, morsel in jar.items():
                self._cookies[name] = morsel.value

    def _build_response(
        self,
        status: int,
        resp_headers: Any,
        raw: bytes,
        content_type: str,
        host: str,
    ) -> web.Response:
        out_headers: dict[str, str] = {}
        for key, value in resp_headers.items():
            low = key.lower()
            if low in _STRIP_RESPONSE:
                continue
            if low == "location":
                out_headers["Location"] = rewrite_location(
                    value, self._proxy_base, self._hosts, host
                )
                continue
            if low == "set-cookie":
                continue  # re-emitted below, adjusted for the proxy origin
            out_headers[key] = value

        body = raw
        if any(t in content_type.lower() for t in _REWRITE_TYPES):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            body = replace_hosts(text, self._proxy_base, self._hosts).encode("utf-8")

        response = web.Response(status=status, body=body, headers=out_headers)
        # Re-emit cookies scoped to the proxy origin: drop Secure (proxy may be
        # http) and Domain (so they bind to the HA host the browser is on).
        for raw_cookie in resp_headers.getall("Set-Cookie", []):
            response.headers.add("Set-Cookie", _rescope_cookie(raw_cookie))
        return response

    # --- registration -------------------------------------------------------

    async def async_finish(self) -> dict[str, Any]:
        """Exchange the captured code for durable credentials."""
        if not self._authorization_code:
            raise AuthError(
                "Login completed without an authorization code; try again from a "
                "browser not already signed in to Amazon"
            )
        assert self._client is not None

        body = registration_body(
            self._domain,
            device_serial=self._fingerprint["device_serial"],
            device_id=self._fingerprint["device_id"],
            authorization_code=self._authorization_code,
            code_verifier=self._verifier,
            frc=self._fingerprint["frc"],
            website_cookies=[
                {"Name": k, "Value": v} for k, v in self._cookies.items()
            ],
        )
        try:
            async with self._client.post(
                register_url(self._domain),
                json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientError as err:
            raise AuthError(f"Registration request failed: {err}") from err

        if status >= 400:
            raise AuthError(f"Amazon rejected the registration (HTTP {status})")

        tokens = extract_tokens(parse_json(text))
        if not tokens["refresh_token"]:
            raise AuthError("Registration succeeded but returned no refresh token")
        return {
            "refresh_token": tokens["refresh_token"],
            "mac_dms": tokens["mac_dms"],
            "device_serial": self._fingerprint["device_serial"],
        }


def _parse_cookie_header(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            out[name.strip()] = value.strip()
    return out


def _rescope_cookie(raw: str) -> str:
    """Strip Secure and Domain so a cookie binds to the proxy origin."""
    parts = [p for p in raw.split(";") if p.strip().lower() not in ("secure",)]
    parts = [p for p in parts if not p.strip().lower().startswith("domain=")]
    return ";".join(parts)


def _query_of(url: str) -> dict[str, Any]:
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

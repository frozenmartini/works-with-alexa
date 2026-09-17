"""A same-origin rewriting relay for Amazon's login.

Ported faithfully from alexa-cookie2's ``lib/proxy.js`` (the login that works),
because a pass-through relay does not: Amazon's proof-of-work CVF widget makes a
cross-origin XHR that CORS blocks unless the *entire* login -- pages, scripts,
and the widget's own fetch -- stays on one origin.

The trick is host-prefixed routing plus body rewriting. Every Amazon URL in every
proxied body is rewritten from ``https://<host>/...`` to
``<proxy_base><host>/...``, so the browser fetches everything (including the
signin JS) back through the relay, where that JS is rewritten in turn. The
browser therefore never leaves the proxy origin, and the CVF fetch is same-origin.

Only these functions decide correctness; they are pure and unit-tested, and
validated against a real captured signin page. :mod:`.auth_proxy` is the aiohttp
server around them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from html import escape as html_escape
from urllib.parse import urlsplit

# The exact host set alexa-cookie2 proxies: www/alexa/bare for the marketplace,
# plus the amazon.com constants. Asset and telemetry hosts (images, cloudfront,
# unagi) are deliberately not proxied -- they load cross-origin without trouble;
# only credentialed XHR like the CVF challenge must stay same-origin.
_CONSTANT_HOSTS = ("amazon.com", "www.amazon.com", "alexa.amazon.com")


def amazon_hosts(domain: str) -> list[str]:
    """The hosts to route and rewrite, for a given marketplace domain."""
    hosts = [f"www.{domain}", f"alexa.{domain}", domain, *_CONSTANT_HOSTS]
    seen: dict[str, None] = {}
    for host in hosts:
        seen.setdefault(host, None)
    return list(seen)


def replace_hosts(text: str, proxy_base: str, hosts: list[str]) -> str:
    """Rewrite ``https://<host>/`` to ``<proxy_base><host>/`` for each host.

    HTML-entity slashes (``&#x2F;``) are decoded first, since Amazon emits URLs
    that way inside inline JSON. ``proxy_base`` must end in ``/``.
    """
    text = text.replace("&#x2F;", "/")
    for host in hosts:
        pattern = re.compile(rf"https?://{re.escape(host)}:?[0-9]*/")
        text = pattern.sub(f"{proxy_base}{host}/", text)
    return text


def replace_hosts_back(text: str, proxy_base: str, hosts: list[str]) -> str:
    """Reverse :func:`replace_hosts`: proxy-prefixed URLs back to real Amazon.

    Used on the ``Referer`` header and on POST body fields so Amazon receives the
    real URLs it expects.
    """
    for host in hosts:
        pattern = re.compile(rf"{re.escape(proxy_base)}{re.escape(host)}/")
        text = pattern.sub(f"https://{host}/", text)
    return text


def host_from_proxy_path(tail: str, hosts: list[str]) -> str | None:
    """The Amazon host named by the first segment of a proxied path.

    ``tail`` is the path *after* the relay's mount point, e.g.
    ``/www.amazon.com/ap/signin`` -> ``www.amazon.com``. Returns ``None`` if the
    first segment is not a proxied host.
    """
    match = re.match(r"^/([^/?#]+)(?:[/?#]|$)", tail or "")
    if match and match.group(1) in hosts:
        return match.group(1)
    return None


def strip_host_prefix(tail: str, host: str) -> str:
    """Remove the ``/<host>`` prefix, yielding the upstream path.

    ``/www.amazon.com/ap/signin?x=1`` with host ``www.amazon.com`` ->
    ``/ap/signin?x=1``. An empty remainder becomes ``/``.
    """
    stripped = re.sub(rf"^/{re.escape(host)}", "", tail, count=1)
    return stripped or "/"


def rewrite_referer(referer: str, proxy_base: str, hosts: list[str]) -> str | None:
    """Map a proxy-origin ``Referer`` back to the real Amazon URL, or drop it."""
    if not referer:
        return None
    return replace_hosts_back(referer, proxy_base, hosts)


def origin_for(host: str) -> str:
    """The ``Origin`` header to send upstream: same-origin with the target."""
    return f"https://{host}"


def rewrite_location(
    location: str, proxy_base: str, hosts: list[str], request_host: str | None
) -> str:
    """Rewrite a redirect ``Location`` so the browser stays on the proxy origin.

    Absolute Amazon URLs are host-prefixed; a host-relative ``/path`` is prefixed
    with the host that served the redirect (mirroring alexa-cookie2).
    """
    rewritten = replace_hosts(location, proxy_base, hosts)
    if request_host and request_host in hosts and rewritten.startswith("/"):
        return f"{proxy_base}{request_host}{rewritten}"
    return rewritten


def is_amazon_host(host: str, hosts: list[str]) -> bool:
    return host in hosts


# --- form autofill ------------------------------------------------------------
# alexa_media_player's proxy fills the sign-in form server-side so the user only
# clicks through (alexapy's ``AlexaProxy.autofill``, via BeautifulSoup). The same
# here, on the raw HTML: an <input> whose name we hold a value for, and which has
# no value of its own, gets one. Inputs that already carry a value are left
# alone -- Amazon's password page repeats the email as a hidden, filled input,
# and overwriting that would be wrong.
_INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_NAME_ATTR = re.compile(
    r"""(?<![\w-])name\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""", re.IGNORECASE
)
_VALUE_ATTR = re.compile(
    r"""(?<![\w-])value\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""", re.IGNORECASE
)


def autofill_inputs(
    html: str, values: Mapping[str, str | Callable[[], str]]
) -> str:
    """Fill ``<input name=...>`` tags in ``html`` from ``values``.

    A value may be a callable, evaluated only when a matching input is actually
    on the page -- so a one-time code is generated for the page that asks for
    it, at the moment it asks, not once up front. Values are HTML-escaped; the
    rest of each tag is untouched, and nothing else in the page is.
    """
    if not values:
        return html

    def fill(match: re.Match[str]) -> str:
        tag = match.group(0)
        name_match = _NAME_ATTR.search(tag)
        if not name_match:
            return tag
        name = next(group for group in name_match.groups() if group is not None)
        if name not in values:
            return tag
        value_match = _VALUE_ATTR.search(tag)
        if value_match and any(value_match.groups()):
            return tag  # already filled; not ours to change
        value = values[name]
        if callable(value):
            value = value()
        if not value:
            return tag
        attr = f'value="{html_escape(value, quote=True)}"'
        if value_match:  # value="" -- replace the empty attribute in place
            return tag[: value_match.start()] + attr + tag[value_match.end() :]
        if tag.endswith("/>"):
            return f"{tag[:-2].rstrip()} {attr}/>"
        return f"{tag[:-1].rstrip()} {attr}>"

    return _INPUT_TAG.sub(fill, html)


def maplanding_code(url: str) -> str | None:
    """Extract ``openid.oa2.authorization_code`` from a maplanding URL, if present."""
    from urllib.parse import parse_qs

    query = parse_qs(urlsplit(url).query)
    values = query.get("openid.oa2.authorization_code")
    return values[0] if values else None

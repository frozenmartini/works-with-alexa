"""Constants for the Works with Alexa integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "works_with_alexa"

# Deliberately no Home Assistant imports in this module, so it and the pure
# modules that import it (protocol.py, enrollment.py, session_auth.py, api.py)
# import without Home Assistant. PLATFORMS lives in __init__.py for the same
# reason.

# --- Amazon device registration -------------------------------------------------
# The iOS Alexa app. Both alexa-cookie2 and alexapy register as this device type,
# which is why a refresh token minted by one is usable by the other (ref/10).
DEVICE_TYPE: Final = "A2IVLV5VM2W81"
APP_NAME: Final = "Amazon Alexa"
APP_VERSION: Final = "2.2.556530.0"
USER_AGENT: Final = (
    "AmazonWebView/Amazon Alexa/2.2.556530.0/iOS/16.6/iPhone"
)

# --- Endpoints ------------------------------------------------------------------
# Domain must stay consistent with the domain the cookies were issued for, or
# requests fail (ref/00 §2).
DEFAULT_DOMAIN: Final = "amazon.com"
# Domain -> the sign-in page's language. Keys in the flow's dropdown order.
MARKETPLACES: Final = {
    "amazon.com": "en_US",
    "amazon.ca": "en_CA",
    "amazon.de": "de_DE",
    "amazon.es": "es_ES",
    "amazon.fr": "fr_FR",
    "amazon.it": "it_IT",
    "amazon.in": "en_US",
    "amazon.nl": "en_US",
    "amazon.co.jp": "ja_JP",
    "amazon.co.uk": "en_GB",
    "amazon.com.au": "en_AU",
    "amazon.com.br": "pt_BR",
    "amazon.com.mx": "es_MX",
}

GRAPHQL_PATH: Final = "/nexus/v1/graphql"

# The bearer token alone does not satisfy the web API; it wants browser cookies,
# fetched by a separate exchange (ref/04 §1.1.5). Path is on www.<domain>, and
# the response is keyed by the dotted domain, e.g. ".amazon.com".
COOKIE_EXCHANGE_PATH: Final = "/ap/exchangetoken/cookies"

# There is no CSRF endpoint. The first of these that sets a `csrf` cookie wins,
# tried in order against alexa.<domain> (ref/04 §1.1.6).
CSRF_PATHS: Final = (
    "/spa/index.html",
    "/api/language",
    "/api/devices-v2/device?cached=false",
    "/templates/oobe/d-device-pick.handlebars",
    "/api/strings",
)

# --- Config entry keys ----------------------------------------------------------
CONF_URL: Final = "url"
CONF_DIRECTED_ID: Final = "directed_id"
CONF_DEVICE_SERIAL: Final = "device_serial"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_MAC_DMS: Final = "mac_dms"

CONF_SCAN_INTERVAL: Final = "scan_interval"
# The raw layer: endpoints exposed one entity per feature. Absent means all.
# Pre-release entries (before v0.1.0) may still carry "built_endpoints", the
# built layer's option until 2026-09-13, when the assembled Window Fan moved to
# kitbash; it is ignored.
CONF_INCLUDE_ENDPOINTS: Final = "include_endpoints"
CONF_INCLUDE_SCENES: Final = "include_scenes"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15

# Refresh 30 seconds before Amazon's reported access-token expiry.
TOKEN_EXPIRY_MARGIN: Final = 30

# Throttling is signalled in the response *body*, not the HTTP status (ref/00 §6).
THROTTLE_MARKERS: Final = (
    "ThrottlingException",
    "Rate exceeded",
    "Too many requests",
)

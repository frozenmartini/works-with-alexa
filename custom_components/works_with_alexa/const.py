"""Constants for the Works with Alexa integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "works_with_alexa"

# Deliberately no Home Assistant imports in this module: it keeps const.py and
# models.py importable outside a Home Assistant environment, so the pure-logic
# tests can run anywhere. PLATFORMS lives in __init__.py for the same reason.

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
SUPPORTED_DOMAINS: Final = [
    "amazon.com",
    "amazon.ca",
    "amazon.de",
    "amazon.es",
    "amazon.fr",
    "amazon.it",
    "amazon.in",
    "amazon.nl",
    "amazon.co.jp",
    "amazon.co.uk",
    "amazon.com.au",
    "amazon.com.br",
    "amazon.com.mx",
]

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
CONF_INCLUDE_ENDPOINTS: Final = "include_endpoints"
CONF_INCLUDE_SCENES: Final = "include_scenes"
# "built_endpoints" was the built layer's option until 2026-09-13, when the
# assembled Window Fan moved to kitbash; entries may still carry it, ignored.
CONF_EXCLUDE_ENDPOINTS: Final = "exclude_endpoints"
CONF_READ_ONLY_ENDPOINTS: Final = "read_only_endpoints"

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

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
    "AppleWebKit PitanguiBridge/2.2.556530.0-[HARDWARE=iPhone14_5]"
    "[SOFTWARE=17.5.1][DEVICE=iPhone]"
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
    "/api/language",
    "/spa/index.html",
    "/api/devices-v2/device?cached=false",
    "/templates/oobe/d-device-pick.handlebars",
)

AUTH_CALLBACK_PATH: Final = "/auth/works_with_alexa/callback"
AUTH_CALLBACK_NAME: Final = "auth:works_with_alexa:callback"
AUTH_PROXY_PATH: Final = "/auth/works_with_alexa/proxy"
AUTH_PROXY_NAME: Final = "auth:works_with_alexa:proxy"

DEFAULT_HASS_URL: Final = "http://homeassistant.local:8123"

# --- Config entry keys ----------------------------------------------------------
CONF_URL: Final = "url"
CONF_DIRECTED_ID: Final = "directed_id"
CONF_DEVICE_SERIAL: Final = "device_serial"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_MAC_DMS: Final = "mac_dms"

CONF_CREDENTIALS: Final = "credentials"
CONF_ACCOUNT: Final = "account"

# The alexa_media_player integration registers against the same iOS app device
# type as we do, so a refresh token it already holds works here (ref/10). Its
# entry keeps the token under `oauth`, and derives the device serial from Home
# Assistant's instance id -- see the borrow step in config_flow.
ALEXA_MEDIA_DOMAIN: Final = "alexa_media"
ALEXA_MEDIA_OAUTH: Final = "oauth"

CONF_HASS_URL: Final = "hass_url"
CONF_PUBLIC_URL: Final = "public_url"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_INCLUDE_ENDPOINTS: Final = "include_endpoints"
CONF_EXCLUDE_ENDPOINTS: Final = "exclude_endpoints"
CONF_READ_ONLY_ENDPOINTS: Final = "read_only_endpoints"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15

# --- Safety ---------------------------------------------------------------------
# Standing user directive: the Kohler Anthem is a shower valve. Turning it on runs
# water. It is never written to by this integration. Endpoints whose manufacturer
# matches are barred from producing writable entities at discovery time, in
# addition to the user-editable CONF_READ_ONLY_ENDPOINTS option.
BLOCKED_MANUFACTURERS: Final = frozenset({"kohler"})

# Writable entities are only created for endpoints in these display categories.
# The account exposes 73 endpoints using the same range/mode/toggle mechanism,
# including an INTERIOR_BLIND that is explicitly not a target of this
# integration (ref/03, ref/06) -- a generic control platform would silently
# start driving it. Read-only sensors are not gated this way; reading anything
# is harmless.
CONTROLLABLE_CATEGORIES: Final = frozenset({"FAN"})

# Mode features are addressed by instance but classified by friendlyName: the
# Window Fan's instances are bare integers whose numbering means nothing, and
# only the label says which one is speed (ref/07).
FAN_SPEED_LABELS: Final = ("fan speed", "speed")

# The Window Fan exposes no oscillate capability of its own. By user directive
# its Direction mode is presented as Home Assistant's oscillate control:
# Exhaust oscillates, Direct does not. Matched on the option labels rather than
# the instance number, for the same reason as the speed lookup.
OSCILLATE_ON_LABEL: Final = "exhaust"
OSCILLATE_OFF_LABEL: Final = "direct"

# Token refresh happens this many seconds before the access token actually expires.
TOKEN_EXPIRY_MARGIN: Final = 300

# Throttling is signalled in the response *body*, not the HTTP status (ref/00 §6).
THROTTLE_MARKERS: Final = (
    "ThrottlingException",
    "Rate exceeded",
    "Too many requests",
)

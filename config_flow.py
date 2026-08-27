"""Config and options flow for Works with Alexa.

The login happens through a local relay the user completes Amazon's real sign-in
through -- MFA and CAPTCHA included -- because a scripted credential POST is not
accepted. The relay's request handler is mounted on Home Assistant's own web
server (no separate port), and Amazon's final redirect bounces the browser back
to a callback view that resumes this flow.

Structure follows the proven ``alexa_media`` flow; the difference that matters is
:class:`~.auth_proxy.LoginRelay`, our own same-origin rewriting proxy driven with the correct
device fingerprint instead of alexapy's blocked one.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

import voluptuous as vol
from aiohttp import web
from aiohttp.web_exceptions import HTTPBadRequest
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .auth_proxy import LoginRelay
from .const import (
    ALEXA_MEDIA_DOMAIN,
    ALEXA_MEDIA_OAUTH,
    AUTH_CALLBACK_NAME,
    AUTH_CALLBACK_PATH,
    AUTH_PROXY_NAME,
    AUTH_PROXY_PATH,
    CONF_ACCOUNT,
    CONF_CREDENTIALS,
    CONF_DEVICE_SERIAL,
    CONF_DIRECTED_ID,
    CONF_HASS_URL,
    CONF_INCLUDE_ENDPOINTS,
    CONF_MAC_DMS,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    DEFAULT_DOMAIN,
    DEFAULT_HASS_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    SUPPORTED_DOMAINS,
)
from .fingerprint import alexa_media_device_serial
from .models import produces_entities
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)


def endpoint_choices(endpoints: dict[str, Any]) -> dict[str, str]:
    """Endpoint id -> label, for the device picker.

    Sorted by the name the user sees, since the ids are opaque. A blocked
    endpoint is marked rather than hidden: it is still worth importing for its
    readings, and the label is the only place to say it will never be written to.
    """
    labelled: list[tuple[str, str, str]] = []
    seen: dict[str, int] = {}
    for endpoint in endpoints.values():
        # Most of an Alexa account is not devices -- Echo speakers, phone apps,
        # scene triggers. Offering an endpoint that yields nothing would be an
        # invitation to tick it and get an empty device.
        if not produces_entities(endpoint):
            continue
        # Sort on the stripped name: some names on a real account carry leading
        # whitespace, which would otherwise sort them ahead of everything.
        name = endpoint.friendly_name.strip()
        label = name
        if endpoint.manufacturer:
            label = f"{label} ({endpoint.manufacturer})"
        if endpoint.blocked:
            label = f"{label} - read-only"
        # Names are not unique on an Alexa account. Two identical rows of
        # checkboxes are unpickable, so disambiguate the repeats.
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} #{seen[label]}"
        labelled.append((name.lower(), endpoint.endpoint_id, label))

    return {endpoint_id: label for _, endpoint_id, label in sorted(labelled)}


def _parse_bundle(raw: str) -> tuple[dict[str, Any], str | None]:
    """Read a credential bundle into the shape :meth:`_async_verify` expects.

    Accepts either the alexa-cookie2 wrapper (``{"cookieData": {...}}``) or the
    inner object on its own. Returns the credentials plus the marketplace the
    bundle was minted against, which is authoritative when present: the cookies
    are only valid for the domain that issued them (ref/00 §2).

    Raises ValueError with a reason that is safe to log -- no field values.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as err:
        raise ValueError(f"not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise ValueError("top level is not an object")

    data = parsed.get("cookieData", parsed)
    if not isinstance(data, dict):
        raise ValueError("cookieData is not an object")

    missing = [
        key
        for key in ("refreshToken", "macDms", "deviceSerial")
        if not data.get(key)
    ]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    if not isinstance(data["macDms"], dict):
        raise ValueError("macDms is not an object")

    domain = data.get("amazonPage")
    if domain not in SUPPORTED_DOMAINS:
        domain = None

    credentials = {
        "refresh_token": data["refreshToken"],
        "device_serial": data["deviceSerial"],
        "mac_dms": data["macDms"],
    }
    return credentials, domain


# The device fingerprint is persisted here so it is reused across login attempts
# rather than churning (threads/002 Q9). Not secret, but device-identifying.
FINGERPRINT_STORAGE_VERSION = 1
FINGERPRINT_STORAGE_KEY = f"{DOMAIN}_fingerprint"


class WorksWithAlexaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the login/registration flow."""

    VERSION = 1

    # Class-level so a second flow reuses one relay rather than double-mounting.
    proxy: LoginRelay | None = None
    proxy_view: "WorksWithAlexaProxyView | None" = None

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._fingerprint: dict[str, str] | None = None
        self._credentials: dict[str, Any] = {}
        self._email: str | None = None
        self._choices: dict[str, str] = {}

    # --- entry point --------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer adoption of existing credentials, or a fresh Amazon sign-in.

        The working paths come first. ``login`` is listed last and labelled as
        unfinished: Amazon serves a proof-of-work CVF challenge whose widget
        loads from a CDN host the relay cannot proxy, so a fresh registration
        cannot currently complete (threads/002 Q9, Q12).
        """
        options = ["import_token", "login"]
        if self._alexa_media_accounts():
            # Only worth offering when there is actually something to borrow --
            # and it is the path that works, so it leads.
            options.insert(0, "import_alexa_media")
        return self.async_show_menu(step_id="user", menu_options=options)

    def _default_hass_url(self) -> str:
        """This instance's own URL, for the browser to reach the relay on."""
        try:
            return get_url(self.hass, allow_external=False)
        except NoURLAvailableError:
            return DEFAULT_HASS_URL

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the marketplace domain and the URL the browser will reach."""
        if user_input is not None:
            self._config[CONF_URL] = user_input[CONF_URL]
            self._config[CONF_HASS_URL] = user_input[CONF_HASS_URL].rstrip("/")
            self._config[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            return await self.async_step_start_proxy()

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_DOMAIN): vol.In(
                    SUPPORTED_DOMAINS
                ),
                vol.Required(
                    CONF_HASS_URL, default=self._default_hass_url()
                ): str,
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="login", data_schema=schema)

    # --- credential import --------------------------------------------------

    async def async_step_import_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Adopt an account from an already-minted credential bundle.

        The refresh token is the durable credential; the access token, cookies
        and CSRF are rebuilt from it on every start (ref/10). So an account
        registered elsewhere can be adopted here without a second device
        registration -- and without the sign-in relay, which is what makes this
        the safe path when Amazon is challenging fresh registrations.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                credentials, bundle_domain = _parse_bundle(
                    user_input[CONF_CREDENTIALS]
                )
            except ValueError as err:
                _LOGGER.warning("Credential bundle rejected: %s", err)
                errors["base"] = "invalid_bundle"
            else:
                self._config[CONF_URL] = bundle_domain or user_input[CONF_URL]
                self._config[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
                # Unlike the login path there is no relay handshake to prove the
                # credentials work, so exercise them once here rather than
                # creating an entry that can only fail at setup.
                try:
                    email = await self._async_verify(credentials)
                except AuthError as err:
                    _LOGGER.warning("Imported credentials rejected: %s", err)
                    errors["base"] = "invalid_auth"
                except WorksWithAlexaError as err:
                    _LOGGER.warning("Could not verify imported bundle: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return await self._async_finalize(credentials, email)

        schema = vol.Schema(
            {
                vol.Required(CONF_CREDENTIALS): str,
                vol.Required(CONF_URL, default=DEFAULT_DOMAIN): vol.In(
                    SUPPORTED_DOMAINS
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(
            step_id="import_token", data_schema=schema, errors=errors
        )

    # --- borrowing from alexa_media_player ----------------------------------

    def _alexa_media_accounts(self) -> list[tuple[int, Any]]:
        """(index, entry) for each alexa_media account holding a refresh token.

        The index is the entry's position among all alexa_media entries, which
        is part of how that integration derives its device serial.
        """
        accounts = []
        for index, entry in enumerate(
            self.hass.config_entries.async_entries(ALEXA_MEDIA_DOMAIN)
        ):
            oauth = entry.data.get(ALEXA_MEDIA_OAUTH) or {}
            if oauth.get("refresh_token") and oauth.get("mac_dms"):
                accounts.append((index, entry))
        return accounts

    async def _async_alexa_media_serial(
        self, email: str, url: str, index: int
    ) -> str:
        """Reproduce the device serial alexa_media registered with.

        That integration does not store the serial; it recomputes it from Home
        Assistant's instance id, the account's position among its entries, and a
        hash of email+url. The refresh token is bound to the device that
        registered it, so the same serial has to be rebuilt the same way or
        Amazon refuses the exchange.
        """
        from homeassistant.helpers.instance_id import async_get as async_get_instance_id

        instance_id = await async_get_instance_id(self.hass)
        return alexa_media_device_serial(instance_id, email, url, index)

    async def async_step_import_alexa_media(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reuse a token the alexa_media_player integration already holds.

        Both integrations register as the same iOS app device type, so a token
        minted by one is accepted for the other (ref/10). Refreshing does not
        rotate the token, so borrowing it does not disturb the other
        integration's session.
        """
        accounts = self._alexa_media_accounts()
        if not accounts:
            return self.async_abort(reason="no_alexa_media")

        errors: dict[str, str] = {}
        by_entry = {entry.entry_id: (index, entry) for index, entry in accounts}

        if user_input is not None:
            chosen = by_entry.get(user_input[CONF_ACCOUNT])
            if chosen is None:
                # Removed between rendering the form and submitting it.
                return self.async_abort(reason="no_alexa_media")
            index, entry = chosen
            oauth = entry.data[ALEXA_MEDIA_OAUTH]
            email = entry.data.get("email", "")
            url = entry.data.get("url", DEFAULT_DOMAIN)

            self._config[CONF_URL] = url
            self._config[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            credentials = {
                "refresh_token": oauth["refresh_token"],
                "mac_dms": oauth["mac_dms"],
                "device_serial": await self._async_alexa_media_serial(
                    email, url, index
                ),
            }
            try:
                verified_email = await self._async_verify(credentials)
            except AuthError as err:
                _LOGGER.warning("Borrowed credentials rejected: %s", err)
                errors["base"] = "invalid_auth"
            except WorksWithAlexaError as err:
                _LOGGER.warning("Could not verify borrowed credentials: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finalize(
                    credentials, verified_email or email or None
                )

        labels = {
            entry.entry_id: entry.title or entry.data.get("email", entry.entry_id)
            for _, entry in accounts
        }
        default_account = next(iter(labels))
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT, default=default_account): vol.In(labels),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(
            step_id="import_alexa_media", data_schema=schema, errors=errors
        )

    # --- the relay ----------------------------------------------------------

    async def async_step_start_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stand up the relay and hand the browser off to Amazon's sign-in."""
        hass_url = self._config[CONF_HASS_URL]
        # The browser-facing base the relay writes into rewritten URLs. It must
        # end in "/" and carry the full external origin + mount path.
        proxy_base = f"{hass_url}{AUTH_PROXY_PATH}/"

        # Reuse one device fingerprint across every attempt. Minting a fresh one
        # per try makes each retry look like a brand-new device registering on
        # the account -- a velocity signal that triggers Amazon's proof-of-work
        # challenge (threads/002 Q9). Persist it so it survives separate flows.
        store: Store = Store(self.hass, FINGERPRINT_STORAGE_VERSION, FINGERPRINT_STORAGE_KEY)
        fingerprint = self._fingerprint or await store.async_load()

        WorksWithAlexaConfigFlow.proxy = LoginRelay(
            self._config[CONF_URL],
            AUTH_PROXY_PATH,
            proxy_base,
            fingerprint=fingerprint,
        )
        await WorksWithAlexaConfigFlow.proxy.async_start()
        self._fingerprint = WorksWithAlexaConfigFlow.proxy.fingerprint
        # Persist a freshly-minted fingerprint so the next attempt reuses it.
        if fingerprint != self._fingerprint:
            await store.async_save(self._fingerprint)

        if not WorksWithAlexaConfigFlow.proxy_view:
            WorksWithAlexaConfigFlow.proxy_view = WorksWithAlexaProxyView(
                WorksWithAlexaConfigFlow.proxy.handle
            )
        else:
            WorksWithAlexaConfigFlow.proxy_view.handler = (
                WorksWithAlexaConfigFlow.proxy.handle
            )
        self.hass.http.register_view(WorksWithAlexaCallbackView())
        self.hass.http.register_view(WorksWithAlexaConfigFlow.proxy_view)

        callback_url = (
            URL(hass_url)
            .with_path(AUTH_CALLBACK_PATH)
            .with_query({"flow_id": self.flow_id})
        )
        # The browser lands on the relay root, which serves the signin page.
        proxy_target = URL(proxy_base).with_query(
            {"config_flow_id": self.flow_id, "callback_url": str(callback_url)}
        )
        return self.async_external_step(
            step_id="check_proxy", url=str(proxy_target)
        )

    async def async_step_check_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Browser has bounced back; tear down the relay's IP allowlist."""
        if WorksWithAlexaConfigFlow.proxy_view:
            WorksWithAlexaConfigFlow.proxy_view.reset()
        return self.async_external_step_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the captured code for durable credentials and create the entry."""
        proxy = WorksWithAlexaConfigFlow.proxy
        if proxy is None:
            return self.async_abort(reason="proxy_missing")

        try:
            credentials = await proxy.async_finish()
        except AuthError as err:
            _LOGGER.warning("Login did not complete: %s", err)
            return self._retry_user("login_failed")
        except WorksWithAlexaError as err:
            _LOGGER.warning("Login connection error: %s", err)
            return self._retry_user("cannot_connect")
        finally:
            await proxy.async_stop()

        # Identify the account from the session rather than asking the user.
        email = await self._async_identify(credentials)
        return await self._async_finalize(credentials, email)

    # --- shared tail --------------------------------------------------------

    async def _async_finalize(
        self, credentials: dict[str, Any], email: str | None
    ) -> ConfigFlowResult:
        """Claim the account, then let the user pick which devices to bring in."""
        # Prefer the account identity over the device serial as the unique id, so
        # re-registering the same account is recognised as a duplicate. Checked
        # before the device picker so a duplicate aborts immediately rather than
        # after the user has worked through a long list.
        unique_id = email or credentials["device_serial"]
        await self.async_set_unique_id(unique_id)

        if self.source == SOURCE_REAUTH:
            # Re-authenticating replaces the credentials on the existing entry.
            # Treating it as a new entry would abort as already-configured, which
            # is what made reauth impossible to complete.
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_DEVICE_SERIAL: credentials["device_serial"],
                    CONF_REFRESH_TOKEN: credentials["refresh_token"],
                    CONF_MAC_DMS: credentials["mac_dms"],
                },
            )

        self._abort_if_unique_id_configured()

        self._credentials = credentials
        self._email = email
        return await self.async_step_select_devices()

    async def async_step_select_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take all devices, none, or pick through them.

        A form cannot carry its own buttons, but menu options are buttons, so
        the bulk choices live here and the checkbox list sits behind "choose".
        """
        try:
            endpoints = await self._async_endpoints(self._credentials)
        except WorksWithAlexaError as err:
            # The credentials are already proven, so a listing failure is not
            # worth discarding the login over: take everything and let the user
            # narrow it from the options flow later.
            _LOGGER.warning("Could not list devices to choose from: %s", err)
            return self._create_entry()

        self._choices = endpoint_choices(endpoints)
        if not self._choices:
            return self._create_entry()

        return self.async_show_menu(
            step_id="select_devices",
            menu_options=["devices_pick", "devices_all", "devices_none"],
            description_placeholders={"count": str(len(self._choices))},
        )

    async def async_step_devices_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tick the devices to bring in, starting from none."""
        if user_input is not None:
            self._config[CONF_INCLUDE_ENDPOINTS] = user_input[CONF_INCLUDE_ENDPOINTS]
            return self._create_entry()

        return self.async_show_form(
            step_id="devices_pick",
            data_schema=vol.Schema(
                {
                    # Starts empty: opting in beats opting out when the account
                    # carries far more devices than anyone wants in Home
                    # Assistant. Optional so submitting nothing is allowed.
                    vol.Optional(
                        CONF_INCLUDE_ENDPOINTS, default=list
                    ): cv.multi_select(self._choices)
                }
            ),
            description_placeholders={"count": str(len(self._choices))},
        )

    async def async_step_devices_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bring in every device the account offers."""
        self._config[CONF_INCLUDE_ENDPOINTS] = list(self._choices)
        return self._create_entry()

    async def async_step_devices_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the account up with no devices; add them later from options."""
        self._config[CONF_INCLUDE_ENDPOINTS] = []
        return self._create_entry()

    def _create_entry(self) -> ConfigFlowResult:
        """Write the entry from what the flow has gathered."""
        credentials = self._credentials
        email = self._email
        unique_id = email or credentials["device_serial"]

        data = {
            CONF_URL: self._config[CONF_URL],
            CONF_DIRECTED_ID: unique_id,
            CONF_DEVICE_SERIAL: credentials["device_serial"],
            CONF_REFRESH_TOKEN: credentials["refresh_token"],
            CONF_MAC_DMS: credentials["mac_dms"],
        }
        options: dict[str, Any] = {
            CONF_SCAN_INTERVAL: self._config[CONF_SCAN_INTERVAL]
        }
        # Kept so a later reauth can stand the relay back up on the same URL the
        # browser reached the first time. Only the sign-in path collects one.
        if CONF_HASS_URL in self._config:
            options[CONF_HASS_URL] = self._config[CONF_HASS_URL]
        # Absent entirely means "everything", which is what a failed listing
        # falls back to. An empty list means the user deliberately chose none.
        if CONF_INCLUDE_ENDPOINTS in self._config:
            options[CONF_INCLUDE_ENDPOINTS] = self._config[CONF_INCLUDE_ENDPOINTS]

        title = f"Works with Alexa ({email})" if email else "Works with Alexa"
        return self.async_create_entry(title=title, data=data, options=options)

    async def _async_session(self, credentials: dict[str, Any]) -> Any:
        """A live session built from the credentials."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .api import AmazonAlexaSession

        session = AmazonAlexaSession(
            async_get_clientsession(self.hass),
            domain=self._config[CONF_URL],
            device_serial=credentials["device_serial"],
            refresh_token=credentials["refresh_token"],
            mac_dms=credentials["mac_dms"],
        )
        await session.async_setup()
        return session

    async def _async_verify(self, credentials: dict[str, Any]) -> str | None:
        """Drive a live session from the credentials and return the account email.

        Raises :class:`AuthError` if the credentials are not accepted, or
        :class:`WorksWithAlexaError` if Amazon could not be reached.
        """
        session = await self._async_session(credentials)
        return await session.async_get_account_email()

    async def _async_endpoints(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Every endpoint on the account, for the device picker."""
        session = await self._async_session(credentials)
        return await session.async_get_endpoints()

    async def _async_identify(self, credentials: dict[str, Any]) -> str | None:
        """Best-effort account email for the login path.

        Failure here is non-fatal: the relay handshake already proved the
        credentials, so a working login is never thrown away over a cosmetic
        lookup.
        """
        try:
            return await self._async_verify(credentials)
        except WorksWithAlexaError as err:
            _LOGGER.debug("Account identification failed, using serial: %s", err)
            return None

    def _retry_user(self, reason: str) -> ConfigFlowResult:
        """Return to the first form with an error rather than aborting.

        The previous answers are kept as the defaults: this form is shown after
        a failed sign-in, and making the user re-derive their own URL from
        scratch is the wrong moment to do it.
        """
        previous_url = self._config.get(CONF_URL, DEFAULT_DOMAIN)
        previous_hass_url = self._config.get(CONF_HASS_URL) or self._default_hass_url()
        previous_interval = self._config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._config = {}
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=previous_url): vol.In(
                        SUPPORTED_DOMAINS
                    ),
                    vol.Required(
                        CONF_HASS_URL, default=previous_hass_url
                    ): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=previous_interval
                    ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
                }
            ),
            errors={"base": reason},
        )

    # --- reauth -------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._config[CONF_URL] = entry_data.get(CONF_URL, DEFAULT_DOMAIN)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        # Reuse the reauth entry's stored hass_url and scan interval. An entry
        # created by one of the import paths never collected a hass_url, so fall
        # back to this instance's own URL rather than a constant that is only
        # right by luck.
        entry = self._get_reauth_entry()
        self._config[CONF_HASS_URL] = entry.options.get(
            CONF_HASS_URL, self._default_hass_url()
        )
        self._config[CONF_SCAN_INTERVAL] = entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return await self.async_step_start_proxy()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "WorksWithAlexaOptionsFlow":
        return WorksWithAlexaOptionsFlow()


class WorksWithAlexaOptionsFlow(OptionsFlow):
    """Runtime options that need no reauth."""

    def __init__(self) -> None:
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling interval, then the same all/none/pick buttons as setup."""
        if user_input is not None:
            self._scan_interval = user_input[CONF_SCAN_INTERVAL]
            if not self._endpoint_choices():
                # Entry not loaded, so there is no device list to offer. Saving
                # the interval alone must not wipe the stored selection.
                return self._save()
            return await self.async_step_devices()

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choices = self._endpoint_choices()
        return self.async_show_menu(
            step_id="devices",
            menu_options=[
                "devices_keep",
                "devices_pick",
                "devices_all",
                "devices_none",
            ],
            description_placeholders={"count": str(len(choices))},
        )

    async def async_step_devices_keep(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the interval without touching the device selection."""
        return self._save()

    async def async_step_devices_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the selection, starting from what is currently in use."""
        choices = self._endpoint_choices()
        if user_input is not None:
            return self._save(user_input[CONF_INCLUDE_ENDPOINTS])

        selected = self.config_entry.options.get(CONF_INCLUDE_ENDPOINTS)
        if selected is None:
            # No selection recorded means everything is in use today, so that is
            # what the form must show -- otherwise saving would silently drop
            # devices the user never chose to remove.
            default = list(choices)
        else:
            # A stored selection can name endpoints the picker no longer offers:
            # one saved before the list was narrowed to real devices, or a device
            # since removed from the account. Defaults outside the offered set
            # are not selectable, so drop them.
            default = [
                endpoint_id for endpoint_id in choices if endpoint_id in selected
            ]

        return self.async_show_form(
            step_id="devices_pick",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INCLUDE_ENDPOINTS, default=default
                    ): cv.multi_select(choices)
                }
            ),
            description_placeholders={"count": str(len(choices))},
        )

    async def async_step_devices_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._save(list(self._endpoint_choices()))

    async def async_step_devices_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._save([])

    def _save(self, include: list[str] | None = None) -> ConfigFlowResult:
        """Write options, keeping whatever this pass did not touch."""
        options: dict[str, Any] = {
            **self.config_entry.options,
            CONF_SCAN_INTERVAL: self._scan_interval,
        }
        if include is not None:
            options[CONF_INCLUDE_ENDPOINTS] = include
        return self.async_create_entry(data=options)

    def _endpoint_choices(self) -> dict[str, str]:
        """Device labels from the coordinator, or nothing if it is not loaded."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None or not getattr(coordinator, "data", None):
            return {}
        return endpoint_choices(coordinator.data)


class WorksWithAlexaCallbackView(HomeAssistantView):
    """Amazon's redirect lands here and resumes the flow."""

    url = AUTH_CALLBACK_PATH
    name = AUTH_CALLBACK_NAME
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            await hass.config_entries.flow.async_configure(
                flow_id=request.query["flow_id"], user_input=None
            )
        except (KeyError, UnknownFlow) as err:
            raise HTTPBadRequest() from err
        return web.Response(
            headers={"content-type": "text/html"},
            text="<script>window.close()</script>Success! This window can be closed.",
        )


class WorksWithAlexaProxyView(HomeAssistantView):
    """Mounts the relay's request handler on Home Assistant's web server.

    Guarded so it only serves a browser whose request carries an in-progress
    flow id, and only from an IP that presented one recently.
    """

    url = AUTH_PROXY_PATH
    extra_urls = [f"{AUTH_PROXY_PATH}/{{tail:.*}}"]
    name = AUTH_PROXY_NAME
    requires_auth = False
    handler: web.RequestHandler = None
    known_ips: dict[str, datetime.datetime] = {}
    auth_seconds = 300

    def __init__(self, handler: web.RequestHandler) -> None:
        WorksWithAlexaProxyView.handler = handler
        for method in ("get", "post", "delete", "put", "patch", "head", "options"):
            setattr(self, method, self.check_auth())

    @classmethod
    def reset(cls) -> None:
        cls.known_ips = {}

    @classmethod
    def check_auth(cls):
        async def wrapped(request: web.Request, **kwargs):
            hass = request.app["hass"]
            last_seen = cls.known_ips.get(request.remote)
            stale = (
                last_seen is None
                or (datetime.datetime.now() - last_seen).seconds > cls.auth_seconds
            )
            if stale:
                try:
                    flow_id = request.url.query["config_flow_id"]
                except KeyError as err:
                    raise Unauthorized() from err
                if not any(
                    flow["flow_id"] == flow_id
                    for flow in hass.config_entries.flow.async_progress()
                ):
                    raise Unauthorized()
                cls.known_ips[request.remote] = datetime.datetime.now()
            return await cls.handler(request, **kwargs)

        return wrapped

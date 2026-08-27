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
import logging
from typing import Any

import voluptuous as vol
from aiohttp import web
from aiohttp.web_exceptions import HTTPBadRequest
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .auth_proxy import LoginRelay
from .const import (
    AUTH_CALLBACK_NAME,
    AUTH_CALLBACK_PATH,
    AUTH_PROXY_NAME,
    AUTH_PROXY_PATH,
    CONF_DEVICE_SERIAL,
    CONF_DIRECTED_ID,
    CONF_HASS_URL,
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
from .protocol import AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)


class WorksWithAlexaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the login/registration flow."""

    VERSION = 1

    # Class-level so a second flow reuses one relay rather than double-mounting.
    proxy: LoginRelay | None = None
    proxy_view: "WorksWithAlexaProxyView | None" = None

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._fingerprint: dict[str, str] | None = None

    # --- entry point --------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the marketplace domain and the URL the browser will reach."""
        if user_input is not None:
            self._config[CONF_URL] = user_input[CONF_URL]
            self._config[CONF_HASS_URL] = user_input[CONF_HASS_URL].rstrip("/")
            self._config[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            return await self.async_step_start_proxy()

        try:
            default_hass_url = get_url(self.hass, allow_external=False)
        except NoURLAvailableError:
            default_hass_url = DEFAULT_HASS_URL

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_DOMAIN): vol.In(
                    SUPPORTED_DOMAINS
                ),
                vol.Required(CONF_HASS_URL, default=default_hass_url): str,
                vol.Required(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # --- the relay ----------------------------------------------------------

    async def async_step_start_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stand up the relay and hand the browser off to Amazon's sign-in."""
        hass_url = self._config[CONF_HASS_URL]
        # The browser-facing base the relay writes into rewritten URLs. It must
        # end in "/" and carry the full external origin + mount path.
        proxy_base = f"{hass_url}{AUTH_PROXY_PATH}/"

        WorksWithAlexaConfigFlow.proxy = LoginRelay(
            self._config[CONF_URL],
            AUTH_PROXY_PATH,
            proxy_base,
            fingerprint=self._fingerprint,
        )
        await WorksWithAlexaConfigFlow.proxy.async_start()
        self._fingerprint = WorksWithAlexaConfigFlow.proxy.fingerprint

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

        # Identify the account from the session rather than asking the user, and
        # use the directed id as the unique id so an email change cannot orphan
        # the entry.
        directed_id, email = await self._async_identify(credentials)
        await self.async_set_unique_id(directed_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_URL: self._config[CONF_URL],
            CONF_DIRECTED_ID: directed_id,
            CONF_DEVICE_SERIAL: credentials["device_serial"],
            CONF_REFRESH_TOKEN: credentials["refresh_token"],
            CONF_MAC_DMS: credentials["mac_dms"],
        }
        options = {CONF_SCAN_INTERVAL: self._config[CONF_SCAN_INTERVAL]}
        title = f"Works with Alexa ({email})" if email else "Works with Alexa"
        return self.async_create_entry(title=title, data=data, options=options)

    async def _async_identify(
        self, credentials: dict[str, Any]
    ) -> tuple[str, str | None]:
        """Read the account's directed id and email from a live session.

        Failure here is non-fatal: fall back to the device serial as the unique
        id so a working login is never thrown away over a cosmetic lookup.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .api import AmazonAlexaSession

        session = AmazonAlexaSession(
            async_get_clientsession(self.hass),
            domain=self._config[CONF_URL],
            device_serial=credentials["device_serial"],
            refresh_token=credentials["refresh_token"],
            mac_dms=credentials["mac_dms"],
        )
        try:
            await session.async_setup()
            email = await session.async_get_account_email()
        except WorksWithAlexaError as err:
            _LOGGER.debug("Account identification failed, using serial: %s", err)
            return credentials["device_serial"], None
        return email or credentials["device_serial"], email

    def _retry_user(self, reason: str) -> ConfigFlowResult:
        """Return to the first form with an error rather than aborting."""
        self._config = {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_DOMAIN): vol.In(
                        SUPPORTED_DOMAINS
                    ),
                    vol.Required(
                        CONF_HASS_URL, default=DEFAULT_HASS_URL
                    ): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
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
        # Reuse the reauth entry's stored hass_url and scan interval.
        entry = self._get_reauth_entry()
        self._config[CONF_HASS_URL] = entry.options.get(
            CONF_HASS_URL, DEFAULT_HASS_URL
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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

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

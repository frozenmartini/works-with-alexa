"""Browser sign-in and device options for Works with Alexa."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .auth_runtime import new_session
from .auth_watch import auth_event, fingerprint
from .enrollment import DirectEnrollment, InvalidRedirect
from .dev_logging import async_enable_evidence_logging, event
from .identity import account_id, is_email
from .const import (
    CONF_DEVICE_SERIAL, CONF_DIRECTED_ID, CONF_INCLUDE_ENDPOINTS, CONF_INCLUDE_SCENES,
    CONF_MAC_DMS, CONF_REFRESH_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, DEFAULT_DOMAIN,
    DEFAULT_SCAN_INTERVAL, DOMAIN, MARKETPLACES, MIN_SCAN_INTERVAL,
)
from .features import endpoint_choices
from .scenes import scene_choices
from .protocol import AuthError, WorksWithAlexaError

if TYPE_CHECKING:
    from .api import AmazonAlexaSession

_LOGGER = logging.getLogger(__name__)

_LANDING_IMAGE = Path(__file__).with_name("signin_landing_page.jpg")


@cache
def landing_image_uri() -> str:
    """Return Amazon's landing page example as a data URI, reading the file once.

    A data URI needs no static route and no http dependency; the frontend's
    markdown sanitizer allows data:image sources in flow descriptions.
    """
    return "data:image/jpeg;base64," + base64.b64encode(_LANDING_IMAGE.read_bytes()).decode()


def _unmigrated_for(unique_id: str | None, account: str) -> bool:
    """Whether an entry not loaded since 0.1.0 still holds this account's email as its unique id."""
    return is_email(unique_id) and account_id(unique_id) == account


def _checkbox_form(
    flow: ConfigFlow | OptionsFlowWithReload,
    step_id: str,
    key: str,
    choices: dict[str, str],
    default: Any,
    *,
    last_step: bool | None = None,
) -> ConfigFlowResult:
    """One checkbox list, every choice visible: optional, so submitting nothing is allowed."""
    selector = SelectSelector(
        SelectSelectorConfig(
            options=[{"value": value, "label": label} for value, label in choices.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )
    return flow.async_show_form(
        step_id=step_id,
        data_schema=vol.Schema({vol.Optional(key, default=default): selector}),
        description_placeholders={"count": str(len(choices))},
        last_step=last_step,
    )


class WorksWithAlexaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the login/registration flow."""

    VERSION = 1
    # 1.2: the account identity is stored as a one-way key, never the email.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._credentials: dict[str, Any] = {}
        self._account: str | None = None
        self._choices: dict[str, str] = {}
        self._scene_choices: dict[str, str] = {}
        self._endpoints: dict[str, Any] | None = None
        self._direct_enrollment: DirectEnrollment | None = None
        self._direct_credentials: dict[str, Any] | None = None

    # --- entry point --------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start browser sign-in."""
        await async_enable_evidence_logging(self.hass)
        event(_LOGGER, "config.started")
        return await self.async_step_login_direct(user_input)

    async def async_step_login_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the marketplace before opening Amazon directly in a browser."""
        if user_input is not None:
            self._config[CONF_URL] = user_input[CONF_URL]
            self._config[CONF_SCAN_INTERVAL] = user_input.get(
                CONF_SCAN_INTERVAL, self._config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            )
            self._direct_credentials = None
            self._direct_enrollment = DirectEnrollment(user_input[CONF_URL])
            if self.source == SOURCE_REAUTH:
                self._direct_enrollment.use_device_serial(self._get_reauth_entry().data[CONF_DEVICE_SERIAL])
            auth_event("signin.started", method="direct_browser", interactive=True)
            return await self.async_step_direct_redirect()
        fields = {
            vol.Required(CONF_URL, default=self._config.get(CONF_URL, DEFAULT_DOMAIN)):
                vol.In(list(MARKETPLACES)),
        }
        # Reauthentication replaces credentials only; the interval lives in the
        # entry's options and is changed there, so it is not offered here.
        if self.source != SOURCE_REAUTH:
            fields[vol.Optional(
                CONF_SCAN_INTERVAL,
                default=self._config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )] = vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL))
        return self.async_show_form(step_id="login_direct", data_schema=vol.Schema(fields))

    async def async_step_direct_redirect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Register once, then verify and use the signed-in account."""
        enrollment = self._direct_enrollment
        if enrollment is None:
            return await self.async_step_login_direct()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("restart_login"):
                self._direct_enrollment = None
                self._direct_credentials = None
                return await self.async_step_login_direct()
            try:
                if self._direct_credentials is None:
                    code = enrollment.authorization_code(user_input.get("redirect_url", ""))
                    # An isolated client avoids sending another account's cookies.
                    async with aiohttp.ClientSession(
                        cookie_jar=aiohttp.DummyCookieJar(),
                        timeout=aiohttp.ClientTimeout(total=60),
                        headers={"User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148"
                        )},
                    ) as session:
                        self._direct_credentials = await enrollment.async_register(session, code)
                return await self._async_complete_browser_signin(user_input)
            except InvalidRedirect:
                errors["redirect_url"] = "invalid_redirect"
            except AuthError:
                auth_event("signin.failed", level=logging.ERROR, stage="enrollment_or_verification")
                errors["base"] = "direct_login_failed"
            except (WorksWithAlexaError, aiohttp.ClientError, TimeoutError):
                auth_event("signin.retry_required", level=logging.WARNING)
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="direct_redirect",
            data_schema=vol.Schema({
                vol.Optional("redirect_url"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.TEXT, multiline=True,
                )),
                vol.Optional("restart_login", default=False): bool,
            }),
            description_placeholders={
                "login_url": enrollment.login_url,
                "landing_image": await self.hass.async_add_executor_job(landing_image_uri),
            },
            errors=errors,
        )

    async def _async_complete_browser_signin(self, user_input) -> ConfigFlowResult:
        """Verify the account before creating or replacing an entry."""
        credentials = self._direct_credentials
        # One session checks the account and lists the devices; leaving the
        # block releases it and copies any rotation back into the credentials.
        async with self._signed_in(credentials) as session:
            email = await session.async_get_account_email()
            if not email:
                raise AuthError("Amazon did not identify the signed-in account")
            if self.source != SOURCE_REAUTH:
                await self._async_list(session)
        auth_event("signin.verified", method="direct_browser",
                   rt_fingerprint=fingerprint(credentials["refresh_token"]))
        return await self._async_finalize(credentials, email)

    # --- shared tail --------------------------------------------------------

    async def _async_finalize(
        self, credentials: dict[str, Any], email: str
    ) -> ConfigFlowResult:
        """Claim the account, then let the user pick which devices to bring in."""
        # The account identity is the unique id, so re-registering the same
        # account is recognised as a duplicate. Checked before the device picker
        # so a duplicate aborts immediately rather than after the user has
        # worked through a long list. The identity is a one-way key of the
        # email; the email itself is never stored.
        account = account_id(email)
        if self.source == SOURCE_REAUTH:
            # Re-authenticating replaces the credentials on the existing entry.
            # Treating it as a new entry would abort as already-configured, which
            # is what made reauth impossible to complete.
            entry = self._get_reauth_entry()
            # Not loaded since 0.1.0, so not yet migrated: the email is still its
            # unique id. Migration replaces it on the next load.
            unique_id = entry.unique_id if _unmigrated_for(entry.unique_id, account) else account
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            self._signin_completed(credentials, entry_id=entry.entry_id)
            return self.async_update_reload_and_abort(
                entry, data_updates=self._entry_data(credentials, account),
            )

        await self.async_set_unique_id(account, raise_on_progress=False)
        # An entry not loaded since 0.1.0 (a disabled one, say) still holds the
        # email as its unique id, which the key alone would not match.
        if any(
            _unmigrated_for(other.unique_id, account)
            for other in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()

        # The sign-in that reaches the device picker last is kept and any other
        # open one for this account is dropped silently (owner, 2026-10-31).
        for other in self._async_in_progress(
            include_uninitialized=True, match_context={"unique_id": account}
        ):
            # Defensive: a reauth flow cannot match here, since its entry exists
            # and the checks above have already aborted this flow.
            if other["context"]["source"] != SOURCE_REAUTH:
                self.hass.config_entries.flow.async_abort(other["flow_id"])

        self._credentials = credentials
        self._account = account
        return await self.async_step_select_devices()

    def _entry_data(self, credentials: dict[str, Any], account: str) -> dict[str, Any]:
        """The entry's record: the marketplace, the account key and the credentials."""
        return {
            CONF_URL: self._config[CONF_URL],
            CONF_DIRECTED_ID: account,
            CONF_DEVICE_SERIAL: credentials["device_serial"],
            CONF_REFRESH_TOKEN: credentials["refresh_token"],
            CONF_MAC_DMS: credentials["mac_dms"],
            "auth_method": "direct_browser",
            "enrolled_at": credentials["enrolled_at"],
        }

    def _signin_completed(
        self, credentials: dict[str, Any], entry_id: str | None = None
    ) -> None:
        """The one ``signin.completed`` event: a reauth names its entry, a new entry does not."""
        fields = {"entry_id": entry_id} if entry_id else {}
        auth_event("signin.completed", **fields, method="direct_browser",
                   reauth=entry_id is not None,
                   rt_fingerprint=fingerprint(credentials["refresh_token"]))

    async def async_step_select_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take all devices, none, or pick through them.

        A form cannot carry its own buttons, but menu options are buttons, so
        the bulk choices live here and the checkbox list sits behind "choose".
        """
        if self._endpoints is None:
            # The credentials are already proven, so a listing failure is not
            # worth discarding the login over: take everything and let the user
            # narrow it from the options flow later.
            return self._create_entry()

        self._choices = endpoint_choices(self._endpoints)
        if not self._choices:
            return await self.async_step_scenes()

        return self.async_show_menu(
            step_id="select_devices",
            menu_options=["devices_pick", "devices_all", "devices_none"],
            description_placeholders={"count": str(len(self._choices))},
        )

    async def async_step_devices_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tick the devices to expose raw, starting from none."""
        if user_input is not None:
            self._config[CONF_INCLUDE_ENDPOINTS] = user_input[CONF_INCLUDE_ENDPOINTS]
            return await self.async_step_scenes()

        # Starts empty: opting in beats opting out when the account carries
        # far more devices than anyone wants in Home Assistant.
        return _checkbox_form(self, "devices_pick", CONF_INCLUDE_ENDPOINTS, self._choices, list)

    async def async_step_devices_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bring in every device the account offers."""
        self._config[CONF_INCLUDE_ENDPOINTS] = list(self._choices)
        return await self.async_step_scenes()

    async def async_step_devices_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the account up with no devices; add them later from options."""
        self._config[CONF_INCLUDE_ENDPOINTS] = []
        return await self.async_step_scenes()

    async def async_step_scenes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose scenes independently; every scene starts unselected."""
        if user_input is not None:
            self._config[CONF_INCLUDE_SCENES] = user_input.get(CONF_INCLUDE_SCENES, [])
            return self._create_entry()
        if not self._scene_choices:
            return self._create_entry()
        return _checkbox_form(self, "scenes", CONF_INCLUDE_SCENES, self._scene_choices, list)

    def _create_entry(self) -> ConfigFlowResult:
        """Write the entry from what the flow has gathered."""
        credentials = self._credentials
        data = self._entry_data(credentials, self._account)
        options: dict[str, Any] = {
            CONF_SCAN_INTERVAL: self._config[CONF_SCAN_INTERVAL]
        }
        # Absent entirely means "everything", which is what a failed listing
        # falls back to. An empty list means the user deliberately chose none.
        if CONF_INCLUDE_ENDPOINTS in self._config:
            options[CONF_INCLUDE_ENDPOINTS] = self._config[CONF_INCLUDE_ENDPOINTS]

        options[CONF_INCLUDE_SCENES] = self._config.get(CONF_INCLUDE_SCENES, [])

        # No email in the title either: the title is stored with the entry.
        title = "Works with Alexa"
        self._signin_completed(credentials)
        return self.async_create_entry(title=title, data=data, options=options)

    @asynccontextmanager
    async def _signed_in(
        self, credentials: dict[str, Any]
    ) -> AsyncIterator[AmazonAlexaSession]:
        """A live session built from the credentials, released on leaving.

        Raises :class:`AuthError` if the credentials are not accepted, or
        :class:`WorksWithAlexaError` if Amazon could not be reached. On every
        path the session is released first, then any rotation is copied back
        into ``credentials`` -- a reply that landed during a cancelled setup or
        a cancelled release is kept.
        """
        session = new_session(
            self.hass,
            domain=self._config[CONF_URL],
            refresh_token=credentials["refresh_token"],
            mac_dms=credentials["mac_dms"],
        )
        try:
            await session.async_setup()
            yield session
        finally:
            try:
                # Settles and detaches; never raises, so the original error survives.
                await session.async_release()
            finally:
                credentials.update(refresh_token=session.refresh_token, mac_dms=session.mac_dms)

    async def _async_list(self, session: Any) -> None:
        """Every endpoint and scene on the account, for the pickers.

        A listing failure leaves ``_endpoints`` as ``None``: the device picker
        then takes everything.
        """
        try:
            self._endpoints = await session.async_get_endpoints()
        except WorksWithAlexaError as err:
            _LOGGER.warning("Could not list devices to choose from: %s", err)
            self._endpoints = None
            return
        try:
            self._scene_choices = scene_choices(await session.async_get_scenes())
        except WorksWithAlexaError:
            _LOGGER.warning("Could not list Alexa scenes for setup")
            self._scene_choices = {}

    # --- reauth -------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Recover the existing account through browser sign-in."""
        self._config[CONF_URL] = entry_data.get(CONF_URL, DEFAULT_DOMAIN)
        await async_enable_evidence_logging(self.hass)
        auth_event("reauth.flow_started")
        return await self.async_step_login_direct()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "WorksWithAlexaOptionsFlow":
        return WorksWithAlexaOptionsFlow()


class WorksWithAlexaOptionsFlow(OptionsFlowWithReload):
    """Runtime options that need no reauth."""

    def __init__(self) -> None:
        self._pending_include: list[str] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Expose device and scene management immediately after setup."""
        self._scan_interval: int = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        event(_LOGGER, "options.opened", entry_id=self.config_entry.entry_id)
        return self.async_show_menu(
            step_id="init", menu_options=["devices", "polling", "sign_in"],
        )

    async def async_step_sign_in(self, user_input=None) -> ConfigFlowResult:
        """Replace credentials through the account-checked reauth flow."""
        self.config_entry.async_start_reauth(self.hass)
        auth_event("reauth.requested", entry_id=self.config_entry.entry_id, trigger="user")
        return self.async_abort(reason="reauth_started")

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._scan_interval = user_input[CONF_SCAN_INTERVAL]
            return self._save()
        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=self._scan_interval):
                    vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            }),
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit device checkboxes, then continue to scene checkboxes."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None:
            return self.async_show_form(
                step_id="devices", data_schema=vol.Schema({}),
                errors={"base": "entry_unavailable"}, description_placeholders={"count": "0"},
            )
        if user_input is not None:
            self._pending_include = user_input.get(CONF_INCLUDE_ENDPOINTS, [])
            return await self.async_step_scenes()

        await coordinator.async_request_refresh()
        choices = endpoint_choices(coordinator.data or {})

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
        return _checkbox_form(
            self, "devices", CONF_INCLUDE_ENDPOINTS, choices, default, last_step=False
        )

    async def async_step_scenes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Separate scene selection; missing options mean all off."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None or not coordinator.scenes_available:
            # Keep stored choices during an outage or while the entry is unloaded.
            return self._save(self._pending_include)
        choices = scene_choices(coordinator.scenes)
        if user_input is not None:
            return self._save(self._pending_include, user_input.get(CONF_INCLUDE_SCENES, []))
        if not choices:
            return self._save(self._pending_include)
        selected = self.config_entry.options.get(CONF_INCLUDE_SCENES, [])
        return _checkbox_form(
            self, "scenes", CONF_INCLUDE_SCENES, choices,
            [key for key in choices if key in selected], last_step=True,
        )

    def _save(self, include: list[str] | None = None, scenes: list[str] | None = None) -> ConfigFlowResult:
        """Write options, keeping whatever this pass did not touch."""
        options: dict[str, Any] = {
            **self.config_entry.options,
            CONF_SCAN_INTERVAL: self._scan_interval,
        }
        if include is not None:
            options[CONF_INCLUDE_ENDPOINTS] = include
        if scenes is not None:
            options[CONF_INCLUDE_SCENES] = scenes
        event(_LOGGER, "options.saved", entry_id=self.config_entry.entry_id,
              selected_devices=len(options.get(CONF_INCLUDE_ENDPOINTS, [])),
              selected_scenes=len(options.get(CONF_INCLUDE_SCENES, [])),
              scan_interval=self._scan_interval)
        # Only a loaded entry is reloaded. Saved while the entry is not loaded, or
        # in the middle of a reload, the options are stored and apply at its next setup.
        self.automatic_reload = self.config_entry.state is ConfigEntryState.LOADED
        return self.async_create_entry(data=options)

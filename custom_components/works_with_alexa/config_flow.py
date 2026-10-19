"""Browser sign-in and device options for Works with Alexa."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
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

from .auth_watch import auth_event, fingerprint
from .enrollment import DirectEnrollment, InvalidRedirect
from .dev_logging import async_enable_evidence_logging, event
from .identity import account_id, is_email
from .const import (
    CONF_DEVICE_SERIAL, CONF_DIRECTED_ID, CONF_INCLUDE_ENDPOINTS, CONF_INCLUDE_SCENES,
    CONF_MAC_DMS, CONF_REFRESH_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, DEFAULT_DOMAIN,
    DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL, SUPPORTED_DOMAINS,
)
from .models import produces_entities, is_controllable
from .scenes import scene_choices
from .protocol import ApiConnectionError, AuthError, WorksWithAlexaError

_LOGGER = logging.getLogger(__name__)


def checkbox_selector(choices: dict[str, str]) -> SelectSelector:
    """Show all device/scene choices as a visible list of checkboxes."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[{"value": value, "label": label} for value, label in choices.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )


def endpoint_choices(endpoints: dict[str, Any]) -> dict[str, str]:
    """Endpoint id -> label, for the device picker.

    Sorted by the name the user sees, since the ids are opaque. Endpoints
    yielding only sensors are marked read-only; capability rules decide this.
    """
    labelled: list[tuple[str, str, str]] = []
    seen: dict[str, int] = {}
    for endpoint in endpoints.values():
        # Most of an Alexa account is not devices -- Echo speakers, phone apps,
        # scene triggers. Offering an endpoint that yields nothing would be an
        # invitation to tick it and get an empty device.
        if endpoint.display_category == "SCENE_TRIGGER" or not produces_entities(endpoint):
            continue
        # Sort on the stripped name: some names on a real account carry leading
        # whitespace, which would otherwise sort them ahead of everything.
        name = endpoint.friendly_name.strip()
        label = name
        if endpoint.manufacturer:
            label = f"{label} ({endpoint.manufacturer})"
        if not is_controllable(endpoint):
            label = f"{label} - read-only"
        # Names are not unique on an Alexa account. Two identical rows of
        # checkboxes are unpickable, so disambiguate the repeats.
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} #{seen[label]}"
        labelled.append((name.lower(), endpoint.endpoint_id, label))

    return {endpoint_id: label for _, endpoint_id, label in sorted(labelled)}


class WorksWithAlexaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the login/registration flow."""

    VERSION = 1
    # 1.2: the account identity is stored as a one-way key, never the email.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._credentials: dict[str, Any] = {}
        self._email: str | None = None
        self._choices: dict[str, str] = {}
        self._scene_choices: dict[str, str] = {}
        self._direct_enrollment: DirectEnrollment | None = None
        self._direct_credentials: dict[str, Any] | None = None
        self._legacy_reauth: tuple[dict[str, Any], str, dict[str, Any]] | None = None
        self._legacy_confirmed = False

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
            self._legacy_reauth = None
            self._legacy_confirmed = False
            self._config[CONF_URL] = user_input[CONF_URL]
            self._config[CONF_SCAN_INTERVAL] = user_input.get(
                CONF_SCAN_INTERVAL, self._config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            )
            self._direct_credentials = None
            self._direct_enrollment = DirectEnrollment(user_input[CONF_URL])
            if self.source == SOURCE_REAUTH:
                self._direct_enrollment.use_device_serial(self._get_reauth_entry().data[CONF_DEVICE_SERIAL])
            auth_event("signin.started", method="direct_browser", interactive=True,
                       mfa_observed="unknown")
            return await self.async_step_direct_redirect()
        fields = {
            vol.Required(CONF_URL, default=self._config.get(CONF_URL, DEFAULT_DOMAIN)):
                vol.In(SUPPORTED_DOMAINS),
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
            except OSError:
                auth_event("signin.save_failed", level=logging.ERROR)
                errors["base"] = "signin_save_failed"
        return self.async_show_form(
            step_id="direct_redirect",
            data_schema=vol.Schema({
                vol.Optional("redirect_url"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.TEXT, multiline=True,
                )),
                vol.Optional("restart_login", default=False): bool,
            }),
            description_placeholders={"login_url": enrollment.login_url},
            errors=errors,
        )

    async def _async_complete_browser_signin(self, user_input) -> ConfigFlowResult:
        """Verify the account before creating or replacing an entry."""
        credentials = self._direct_credentials
        self._config["auth_method"] = "direct_browser"
        report = credentials["registration_expiry_report"]
        self._config["enrolled_at"] = report["received_at_utc"]
        email = await self._async_verify(credentials)
        if not email:
            raise AuthError("Amazon did not identify the signed-in account")
        auth_event("signin.verified", method="direct_browser",
                   rt_fingerprint=fingerprint(credentials["refresh_token"]),
                   mfa_observed=self._config.get("mfa_observed", "unknown"))
        return await self._async_finalize(credentials, email)

    # --- shared tail --------------------------------------------------------

    async def _async_finalize(
        self, credentials: dict[str, Any], email: str | None
    ) -> ConfigFlowResult:
        """Claim the account, then let the user pick which devices to bring in."""
        # Prefer the account identity over the device serial as the unique id, so
        # re-registering the same account is recognised as a duplicate. Checked
        # before the device picker so a duplicate aborts immediately rather than
        # after the user has worked through a long list. The identity is a
        # one-way key of the email; the email itself is never stored.
        account = account_id(email) if email else None
        unique_id = account or credentials["device_serial"]
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if entry.unique_id == entry.data.get(CONF_DEVICE_SERIAL):
                if not email:
                    raise AuthError("Amazon did not identify the signed-in account")
                expected = await self._async_legacy_account(entry)
                if expected is None:
                    self._legacy_reauth = (credentials, email, dict(entry.data))
                    return await self.async_step_confirm_legacy_account()
                if expected != account:
                    return self.async_abort(reason="wrong_account")
                # The serial remains the HA identity; directed_id stores the
                # verified account identity for subsequent reauthentication.
                unique_id = entry.unique_id
            elif is_email(entry.unique_id) and account_id(entry.unique_id) == account:
                # Not loaded since 0.1.0, so not yet migrated: the email is
                # still its unique id. Migration replaces it on the next load.
                unique_id = entry.unique_id
        await self.async_set_unique_id(unique_id)

        if self.source == SOURCE_REAUTH:
            # Re-authenticating replaces the credentials on the existing entry.
            # Treating it as a new entry would abort as already-configured, which
            # is what made reauth impossible to complete.
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            auth_event("signin.completed", entry_id=self._get_reauth_entry().entry_id,
                       method=self._config.get("auth_method", "existing_credentials"), reauth=True,
                       mfa_observed=self._config.get("mfa_observed", "unknown"),
                       rt_fingerprint=fingerprint(credentials["refresh_token"]))
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_URL: self._config[CONF_URL],
                    CONF_DEVICE_SERIAL: credentials["device_serial"],
                    CONF_REFRESH_TOKEN: credentials["refresh_token"],
                    CONF_MAC_DMS: credentials["mac_dms"],
                    CONF_DIRECTED_ID: account,
                    "auth_method": self._config.get("auth_method", "existing_credentials"),
                    "enrolled_at": self._config.get("enrolled_at"),
                },
            )

        # An entry not loaded since 0.1.0 (a disabled one, say) still holds the
        # email as its unique id, which the key alone would not match.
        if account and any(
            is_email(other.unique_id) and account_id(other.unique_id) == account
            for other in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()

        self._credentials = credentials
        self._email = email
        return await self.async_step_select_devices()

    async def _async_legacy_account(self, entry) -> str | None:
        """Establish legacy ownership from stored identity or old credentials.

        Returns the account key (see :func:`account_id`), or None if the
        account cannot be established and the user has to confirm it.
        """
        known = entry.data.get(CONF_DIRECTED_ID)
        if known and known != entry.unique_id:
            return account_id(known) if is_email(known) else known
        pending = getattr(self, "_legacy_reauth", None)
        if getattr(self, "_legacy_confirmed", False) and pending and dict(entry.data) == pending[2]:
            return account_id(pending[1])
        before = dict(entry.data)
        old_credentials = {
            key: before[key] for key in (CONF_URL, CONF_DEVICE_SERIAL, CONF_REFRESH_TOKEN)
        }
        old_credentials[CONF_MAC_DMS] = before.get(CONF_MAC_DMS, {})
        try:
            email = await self._async_verify(old_credentials)
        except AuthError as err:
            if not err.root_token_rejected:
                raise ApiConnectionError("The previous account could not be verified; retry") from err
            email = None
        finally:
            # Verification can rotate the old registration even if its web
            # session fails afterward. Do not overwrite a concurrent sign-in.
            if dict(entry.data) == before:
                self.hass.config_entries.async_update_entry(entry, data={
                    **entry.data,
                    CONF_REFRESH_TOKEN: old_credentials[CONF_REFRESH_TOKEN],
                    CONF_MAC_DMS: old_credentials[CONF_MAC_DMS],
                })
        if dict(entry.data) != {
            **before, CONF_REFRESH_TOKEN: old_credentials[CONF_REFRESH_TOKEN],
            CONF_MAC_DMS: old_credentials[CONF_MAC_DMS],
        }:
            raise WorksWithAlexaError("The entry changed during account verification; retry sign-in")
        if not email:
            return None
        account = account_id(email)
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DIRECTED_ID: account}
        )
        return account

    async def async_step_confirm_legacy_account(self, user_input=None) -> ConfigFlowResult:
        """Require explicit account binding when legacy ownership is unavailable."""
        pending = self._legacy_reauth
        if self.source != SOURCE_REAUTH or pending is None:
            return self.async_abort(reason="wrong_account")
        entry = self._get_reauth_entry()
        if entry.unique_id != entry.data[CONF_DEVICE_SERIAL] or dict(entry.data) != pending[2]:
            return self.async_abort(reason="wrong_account")
        errors = {}
        if user_input is not None:
            if user_input.get("confirm_account") is True:
                self._legacy_confirmed = True
                return await self._async_finalize(pending[0], pending[1])
            errors["base"] = "account_confirmation_required"
        return self.async_show_form(
            step_id="confirm_legacy_account",
            data_schema=vol.Schema({vol.Required("confirm_account", default=False): bool}),
            description_placeholders={"account": pending[1]},
            errors=errors,
        )

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

        schema: dict[Any, Any] = {
            # Starts empty: opting in beats opting out when the account
            # carries far more devices than anyone wants in Home
            # Assistant. Optional so submitting nothing is allowed.
            vol.Optional(CONF_INCLUDE_ENDPOINTS, default=list): checkbox_selector(
                self._choices
            ),
        }
        return self.async_show_form(
            step_id="devices_pick",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": str(len(self._choices))},
        )

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
        return self.async_show_form(
            step_id="scenes",
            data_schema=vol.Schema({
                vol.Optional(CONF_INCLUDE_SCENES, default=list): checkbox_selector(self._scene_choices),
            }),
            description_placeholders={"count": str(len(self._scene_choices))},
        )

    def _create_entry(self) -> ConfigFlowResult:
        """Write the entry from what the flow has gathered."""
        credentials = self._credentials
        unique_id = account_id(self._email) if self._email else credentials["device_serial"]

        data = {
            CONF_URL: self._config[CONF_URL],
            CONF_DIRECTED_ID: unique_id,
            CONF_DEVICE_SERIAL: credentials["device_serial"],
            CONF_REFRESH_TOKEN: credentials["refresh_token"],
            CONF_MAC_DMS: credentials["mac_dms"],
            "auth_method": self._config.get("auth_method", "existing_credentials"),
            "enrolled_at": self._config.get("enrolled_at"),
        }
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
        auth_event("signin.completed", method=data["auth_method"], reauth=False,
                   mfa_observed=self._config.get("mfa_observed", "unknown"),
                   rt_fingerprint=fingerprint(credentials["refresh_token"]))
        return self.async_create_entry(title=title, data=data, options=options)

    async def _async_session(self, credentials: dict[str, Any]) -> Any:
        """A live session built from the credentials."""
        from homeassistant.helpers.aiohttp_client import async_create_clientsession

        from .api import AmazonAlexaSession

        session = AmazonAlexaSession(
            async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar(),
                                       timeout=aiohttp.ClientTimeout(total=60)),
            domain=credentials.get(CONF_URL, self._config[CONF_URL]),
            device_serial=credentials["device_serial"],
            refresh_token=credentials["refresh_token"],
            mac_dms=credentials["mac_dms"],
        )
        try:
            await session.async_setup()
        except BaseException:
            session.detach_client()
            raise
        finally:
            credentials.update(refresh_token=session.refresh_token, mac_dms=session.mac_dms)
        return session

    async def _async_verify(self, credentials: dict[str, Any]) -> str | None:
        """Drive a live session from the credentials and return the account email.

        Raises :class:`AuthError` if the credentials are not accepted, or
        :class:`WorksWithAlexaError` if Amazon could not be reached.
        """
        session = await self._async_session(credentials)
        try:
            return await session.async_get_account_email()
        finally:
            credentials.update(refresh_token=session.refresh_token, mac_dms=session.mac_dms)
            session.detach_client()

    async def _async_endpoints(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Every endpoint on the account, for the device picker."""
        session = await self._async_session(credentials)
        try:
            endpoints = await session.async_get_endpoints()
            try:
                self._scene_choices = scene_choices(await session.async_get_scenes())
            except WorksWithAlexaError:
                _LOGGER.warning("Could not list Alexa scenes for setup")
                self._scene_choices = {}
            return endpoints
        finally:
            credentials.update(refresh_token=session.refresh_token, mac_dms=session.mac_dms)
            session.detach_client()

    # --- reauth -------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._config[CONF_URL] = entry_data.get(CONF_URL, DEFAULT_DOMAIN)
        await async_enable_evidence_logging(self.hass)
        auth_event("reauth.flow_started")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        """Recover the existing account through browser sign-in."""
        self._config[CONF_SCAN_INTERVAL] = self._get_reauth_entry().options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return await self.async_step_login_direct(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "WorksWithAlexaOptionsFlow":
        return WorksWithAlexaOptionsFlow()


class WorksWithAlexaOptionsFlow(OptionsFlow):
    """Runtime options that need no reauth."""

    def __init__(self) -> None:
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._pending_include: list[str] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Expose device and scene management immediately after setup."""
        self._scan_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        if user_input is not None:
            return await self.async_step_polling(user_input)
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
            return await self._async_choose_scenes(user_input.get(CONF_INCLUDE_ENDPOINTS, []))

        await coordinator.async_request_refresh()
        choices = self._endpoint_choices()

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

        schema: dict[Any, Any] = {
            vol.Optional(CONF_INCLUDE_ENDPOINTS, default=default): checkbox_selector(
                choices
            ),
        }

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": str(len(choices))},
            last_step=False,
        )

    async def _async_choose_scenes(self, include: list[str] | None = None) -> ConfigFlowResult:
        self._pending_include = include
        return await self.async_step_scenes()

    async def async_step_scenes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Separate scene selection; missing options mean all off."""
        if self._pending_include is None and (user_input is None or CONF_INCLUDE_SCENES not in user_input):
            coordinator = getattr(self.config_entry, "runtime_data", None)
            if coordinator is not None:
                await coordinator.async_request_refresh()
            if coordinator is None or not coordinator.scenes_available:
                return self.async_show_form(
                    step_id="scenes", data_schema=vol.Schema({}),
                    errors={"base": "entry_unavailable"}, description_placeholders={"count": "0"},
                )
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
        return self.async_show_form(
            step_id="scenes",
            data_schema=vol.Schema({
                vol.Optional(CONF_INCLUDE_SCENES, default=[key for key in choices if key in selected]):
                    checkbox_selector(choices),
            }),
            description_placeholders={"count": str(len(choices))},
            last_step=True,
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
        return self.async_create_entry(data=options)

    def _endpoints(self) -> dict[str, Any]:
        """The coordinator's endpoints, or nothing if the entry is not loaded."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None or not getattr(coordinator, "data", None):
            return {}
        return coordinator.data

    def _endpoint_choices(self) -> dict[str, str]:
        """Labels for the device picker."""
        return endpoint_choices(self._endpoints())

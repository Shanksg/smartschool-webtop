"""Config flow for the SmartSchool (Webtop) integration.

Initial setup collects the credential. Reauthentication validates replacement
credentials before updating and reloading the existing entry.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
    UnknownEntry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api.client import WebtopClient
from .api.exceptions import ApiError, RequestFailed, TokenExpired
from .const import (
    CONF_BIO_LOGIN,
    CONF_DEVICE_ID,
    CONF_IS_MOBILE,
    CONF_MESSAGES_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_USER,
    CONF_UNIQUE_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .students import known_students, normalize_name

_LOGGER = logging.getLogger(__name__)


def _validate_credentials(data: dict[str, Any]) -> list[tuple[str, str]] | None:
    """Validate in an executor with a clean, short-lived client.

    Raises if the credential is rejected. Otherwise returns (student_id, name)
    for the students it can see, or None if the list could not be read: the
    list is only evidence about which account this is, never a reason to fail.
    """
    client = WebtopClient("")
    try:
        token = client.login_by_bio(
            bio_login=data[CONF_BIO_LOGIN],
            unique_id=data[CONF_UNIQUE_ID],
            selected_user=data.get(CONF_SELECTED_USER, ""),
            device_id=data.get(CONF_DEVICE_ID, ""),
            is_mobile=data.get(CONF_IS_MOBILE, True),
        )
        if not token or not client.check_token():
            raise TokenExpired("Replacement credential was rejected")
        try:
            students = client.get_students()
        except (ApiError, RequestFailed, TokenExpired):
            return None
        return [(s.student_id, s.name or "") for s in students if s.student_id] or None
    finally:
        client.close()


# Reauth-form-only checkbox shown with the "wrong_account" warning.
CONF_CONFIRM_ACCOUNT = "confirm_account"

def _known_students(hass: HomeAssistant, entry_id: str) -> tuple[set[str], set[str]]:
    """(stable keys, normalized names) of the students this entry has devices for."""
    known = known_students(hass, entry_id)
    return set(known), {name for name in known.values() if name}


def _looks_like_another_account(
    students: list[tuple[str, str]] | None, known: tuple[set[str], set[str]]
) -> bool:
    """True only with positive evidence: known students, and none match.

    Encrypted student ids can change when the school year rolls over, so a
    matching name counts as the same account too.
    """
    known_ids, known_names = known
    if not students or not (known_ids or known_names):
        return False
    ids = {sid for sid, _ in students}
    names = {normalize_name(name) for _, name in students if normalize_name(name)}
    return ids.isdisjoint(known_ids) and names.isdisjoint(known_names)


STEP_USER_SCHEMA = vol.Schema(
    {
        # bio_login is a bearer credential (valid ~1 year); mask it in the UI.
        vol.Required(CONF_BIO_LOGIN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_UNIQUE_ID): str,
        vol.Optional(CONF_SELECTED_USER, default=""): str,
        vol.Optional(CONF_DEVICE_ID, default=""): str,
        vol.Optional(CONF_IS_MOBILE, default=True): bool,
    }
)

# Required fields that must be non-empty after trimming.
_REQUIRED_NONEMPTY = (CONF_BIO_LOGIN, CONF_UNIQUE_ID)


class SmartSchoolConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the SmartSchool config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SmartSchoolOptionsFlow:
        """Options: polling interval and inbox on/off."""
        return SmartSchoolOptionsFlow()

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle HA's reauth request without echoing the old credential."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacements and preserve the entry and entity identities."""
        try:
            entry = self._get_reauth_entry()
        except UnknownEntry:
            # The entry was removed while the reauth prompt was open.
            return self.async_abort(reason="reauth_entry_missing")

        errors: dict[str, str] = {}
        suggested = dict(entry.data)
        if user_input is not None:
            cleaned = {
                key: value.strip() if isinstance(value, str) else value
                for key, value in user_input.items()
            }
            # A form-only answer, never saved into the entry.
            confirmed = bool(cleaned.pop(CONF_CONFIRM_ACCOUNT, False))
            suggested.update(cleaned)
            for field in _REQUIRED_NONEMPTY:
                if not cleaned.get(field):
                    errors[field] = "required"
            if not errors:
                # uniqueId identifies a browser installation, not an account.
                # It may change during recovery; preserve the entry_id used
                # by entities, but reject another entry's device credential.
                if any(
                    other.entry_id != entry.entry_id
                    and cleaned[CONF_UNIQUE_ID] in (
                        other.unique_id, other.data.get(CONF_UNIQUE_ID)
                    )
                    for other in self._async_current_entries()
                ):
                    errors["base"] = "already_configured"
                else:
                    replacement = {**entry.data, **cleaned}
                    try:
                        students = await self.hass.async_add_executor_job(
                            _validate_credentials, replacement
                        )
                    except (ApiError, TokenExpired):
                        errors["base"] = "invalid_auth"
                    except RequestFailed:
                        errors["base"] = "cannot_connect"
                    except Exception as err:
                        # Never expose exception messages, payloads or secrets:
                        # log only the exception type, so a genuine bug is
                        # still visible without leaking content.
                        _LOGGER.error(
                            "Unexpected error validating SmartSchool credentials (%s)",
                            type(err).__name__,
                        )
                        errors["base"] = "unknown"
                    else:
                        if not confirmed and _looks_like_another_account(
                            students, _known_students(self.hass, entry.entry_id)
                        ):
                            # None of this entry's students: probably another
                            # account, which would swap in different children
                            # and orphan the existing devices. Warn, and let the
                            # user confirm (e.g. ids changed with the school
                            # year) rather than lock them out.
                            errors["base"] = "wrong_account"
                        else:
                            # Reloads even when nothing changed (the credential
                            # may simply have recovered), the helper's default.
                            return self.async_update_reload_and_abort(
                                entry, data=replacement, unique_id=cleaned[CONF_UNIQUE_ID]
                            )

        suggested.pop(CONF_BIO_LOGIN, None)
        suggested.pop(CONF_CONFIRM_ACCOUNT, None)
        schema = STEP_USER_SCHEMA
        if errors.get("base") == "wrong_account":
            schema = STEP_USER_SCHEMA.extend(
                {vol.Optional(CONF_CONFIRM_ACCOUNT, default=False): bool}
            )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: capture the loginByBio credential."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Trim pasted input; empty strings pass vol's type check but are
            # unusable, and an empty unique_id would collide across accounts.
            cleaned = {
                k: (v.strip() if isinstance(v, str) else v)
                for k, v in user_input.items()
            }
            for field in _REQUIRED_NONEMPTY:
                if not cleaned.get(field):
                    errors[field] = "required"

            if not errors:
                await self.async_set_unique_id(cleaned[CONF_UNIQUE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="SmartSchool", data=cleaned)

            # Re-show the form with what the user typed, but never suggest the
            # masked bearer credential back into the UI - make them re-enter it.
            suggested = {
                k: v for k, v in user_input.items() if k != CONF_BIO_LOGIN
            }
            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(
                    STEP_USER_SCHEMA, suggested
                ),
                errors=errors,
            )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                max=MAX_SCAN_INTERVAL,
                step=5,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_MESSAGES_ENABLED, default=True): bool,
    }
)


class SmartSchoolOptionsFlow(OptionsFlowWithReload):
    """Polling interval and inbox on/off; the entry reloads when they change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    # NumberSelector yields a float; the coordinator wants minutes.
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_MESSAGES_ENABLED: bool(user_input[CONF_MESSAGES_ENABLED]),
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, dict(self.config_entry.options)
            ),
        )

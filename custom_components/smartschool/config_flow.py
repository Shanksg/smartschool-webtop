"""Config flow for the SmartSchool (Webtop) integration.

Initial setup collects the credential. Reauthentication validates replacement
credentials before updating and reloading the existing entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import (
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
    CONF_SELECTED_USER,
    CONF_UNIQUE_ID,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult


def _validate_credentials(data: dict[str, Any]) -> None:
    """Validate in an executor with a clean, short-lived client."""
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
    finally:
        client.close()


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

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle HA's reauth request without echoing the old credential."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacements and preserve the entry and entity identities."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None or entry.domain != DOMAIN:
            return self.async_abort(reason="reauth_entry_missing")

        errors: dict[str, str] = {}
        suggested = dict(entry.data)
        if user_input is not None:
            cleaned = {
                key: value.strip() if isinstance(value, str) else value
                for key, value in user_input.items()
            }
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
                        await self.hass.async_add_executor_job(
                            _validate_credentials, replacement
                        )
                    except (ApiError, TokenExpired):
                        errors["base"] = "invalid_auth"
                    except RequestFailed:
                        errors["base"] = "cannot_connect"
                    except Exception:
                        # Never expose exception messages, payloads or secrets.
                        errors["base"] = "unknown"
                    else:
                        if (
                            replacement == dict(entry.data)
                            and entry.unique_id == cleaned[CONF_UNIQUE_ID]
                        ):
                            # Older HA helpers only reload changed entries.
                            self.hass.config_entries.async_schedule_reload(entry.entry_id)
                            return self.async_abort(reason="reauth_successful")
                        return self.async_update_reload_and_abort(
                            entry, data=replacement, unique_id=cleaned[CONF_UNIQUE_ID]
                        )

        suggested.pop(CONF_BIO_LOGIN, None)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, suggested),
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

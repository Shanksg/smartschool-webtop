"""Config flow for the SmartSchool (Webtop) integration.

Phase 1: collect the loginByBio credential and store it. Live validation of
the credential (test-minting a token) and an options/re-auth flow come in a
later phase.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BIO_LOGIN,
    CONF_DEVICE_ID,
    CONF_IS_MOBILE,
    CONF_SELECTED_USER,
    CONF_UNIQUE_ID,
    DOMAIN,
)

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

"""Config flow for the SmartSchool (Webtop) integration.

Phase 1: collect the loginByBio credential and store it. Live validation of
the credential (test-minting a token) and an options/re-auth flow come in a
later phase.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

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
        vol.Required(CONF_BIO_LOGIN): str,
        vol.Required(CONF_UNIQUE_ID): str,
        vol.Optional(CONF_SELECTED_USER, default=""): str,
        vol.Optional(CONF_DEVICE_ID, default=""): str,
        vol.Optional(CONF_IS_MOBILE, default=True): bool,
    }
)


class SmartSchoolConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the SmartSchool config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: capture the loginByBio credential."""
        if user_input is not None:
            # One account per config entry.
            await self.async_set_unique_id(user_input[CONF_UNIQUE_ID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="SmartSchool", data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

"""The SmartSchool (Webtop) integration.

Phase 2: a data-update coordinator that mints its own token via bioLogin and
fetches homework + messages. Entities (the sensor platform) arrive in Phase 3.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SmartSchoolCoordinator

# Sensor platform is forwarded in Phase 3.
PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartSchool from a config entry."""
    coordinator = SmartSchoolCoordinator(hass, entry)
    # Raises ConfigEntryAuthFailed (-> reauth) or ConfigEntryNotReady (-> retry)
    # as appropriate; only proceeds once a first fetch succeeds.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded

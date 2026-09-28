"""The SmartSchool (Webtop) integration.

A data-update coordinator mints its own token via bioLogin and fetches homework
+ messages; the sensor platform exposes them as native Home Assistant entities.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SmartSchoolCoordinator
from .events import history_store
from .todo import todo_store

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartSchool from a config entry."""
    coordinator = SmartSchoolCoordinator(hass, entry)
    # History must be in place before the first poll decides what is new.
    await coordinator.async_load_history()
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
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            # Stop polling before persisting history: Home Assistant only runs
            # the coordinator's own shutdown after this function returns, and
            # a poll finishing in between must not announce or write anything.
            await coordinator.async_shutdown()
            # Persist history, so a reload reads current data.
            await coordinator.async_flush_history()
            # The client owns its own requests.Session (connection pool); close
            # it in the executor so a reload does not leak it.
            await hass.async_add_executor_job(coordinator.async_shutdown_client)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's stored history and to-do state when it is removed."""
    await history_store(hass, entry.entry_id).async_remove()
    await todo_store(hass, entry.entry_id).async_remove()

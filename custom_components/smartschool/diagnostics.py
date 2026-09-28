"""Diagnostics for the SmartSchool (Webtop) integration.

What a bug report needs: configuration shape, polling health, which homework
source answered, and how much history is stored. Nothing that identifies the
account or its children and no school content: the credential fields are
redacted, students appear only as "student_1", "student_2", ..., and exceptions
are reported by type (their text can carry server or school content).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BIO_LOGIN,
    CONF_DEVICE_ID,
    CONF_SELECTED_USER,
    CONF_UNIQUE_ID,
    DOMAIN,
)

TO_REDACT = {CONF_BIO_LOGIN, CONF_UNIQUE_ID, CONF_SELECTED_USER, CONF_DEVICE_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    diagnostics: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "state": entry.state.value,
            # The entry's unique_id is the browser install id.
            "unique_id": REDACTED if entry.unique_id else None,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": None,
    }
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return diagnostics

    exception = coordinator.last_exception
    info: dict[str, Any] = {
        "last_update_success": coordinator.last_update_success,
        "last_exception": type(exception).__name__ if exception else None,
        "update_interval_minutes": (
            coordinator.update_interval.total_seconds() / 60
            if coordinator.update_interval
            else None
        ),
        "history": coordinator.history_stats(),
        "students": [],
        "inbox": None,
    }
    data = coordinator.data
    if data is not None:
        info["students"] = [
            {
                "student": f"student_{index}",
                "homework_items": len(data.homework.get(data.key_of(student), [])),
                "full_window": data.full_window.get(data.key_of(student)),
            }
            for index, student in enumerate(data.students, start=1)
        ]
        info["inbox"] = {
            "enabled": data.messages_enabled,
            "fresh": data.messages_fresh,
            "messages": len(data.messages),
            "unread": sum(1 for message in data.messages if not message.has_read),
        }
    diagnostics["coordinator"] = info
    return diagnostics

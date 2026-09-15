"""Detect new items in successful polls and publish local HA events.

History is in memory until persistent storage lands. The first successful
snapshot of each student/inbox seeds history silently, including after reload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE

if TYPE_CHECKING:
    from .coordinator import SmartSchoolData


class SmartSchoolEvents:
    """Track each config entry independently; never log content or raw data."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._homework: dict[tuple[str, bool], set[str]] = {}
        self._sources: dict[str, bool] = {}
        self._messages: set[str] | None = None

    @callback
    def async_process(self, data: SmartSchoolData) -> None:
        """Publish only new identities, after establishing reliable baselines."""
        for student in data.students:
            student_id = student.student_id
            full = data.full_window.get(student_id, False)
            bucket = (student_id, full)
            seen = self._homework.setdefault(bucket, set())
            # Sources use different date identities. Seed every transition to
            # avoid announcing existing work when fallback starts or recovers.
            notify = self._sources.get(student_id) == full
            self._sources[student_id] = full
            for item in data.homework.get(student_id, []):
                key = item.identity()
                if key in seen:
                    continue
                seen.add(key)
                if notify:
                    self._fire(
                        EVENT_NEW_HOMEWORK,
                        f"{self.entry_id}_student_{student_id}",
                        {
                            "student_id": student_id,
                            "student_name": student.name,
                            "item_id": key,
                            **item.as_dict(),
                            "date_is_synthetic": item.date_is_synthetic,
                        },
                    )

        # Retained data from a failed inbox request is not a baseline. The
        # first real inbox response must be silent even after earlier outages.
        if not data.messages_fresh:
            return
        notify = self._messages is not None
        if self._messages is None:
            self._messages = set()
        for message in data.messages:
            key = message.identity()
            if key in self._messages:
                continue
            self._messages.add(key)
            if notify:
                self._fire(
                    EVENT_NEW_MESSAGE,
                    f"{self.entry_id}_inbox",
                    {"item_id": key, **message.as_dict()},
                )

    @callback
    def _fire(self, event_type: str, identifier: str, payload: dict) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, identifier)})
        event_data = {"entry_id": self.entry_id, **payload}
        if device is not None:
            event_data["device_id"] = device.id
        self.hass.bus.async_fire(event_type, event_data)

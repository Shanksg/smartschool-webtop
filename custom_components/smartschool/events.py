"""Detect new items in successful polls and publish local HA events.

History persists per config entry in Home Assistant's storage, so items that
arrive while Home Assistant is offline are announced at the next poll if they
are still available upstream. With no stored history (a fresh install, an
upgrade from a version without storage, or unreadable storage), the first
successful snapshot of each student/inbox seeds history silently.

Only item identities (hashes) and the date each was last seen are stored -
never school content.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE

if TYPE_CHECKING:
    from .coordinator import SmartSchoolData

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Coalesce the per-poll writes; pending data is flushed on unload and on
# Home Assistant's final write at shutdown.
SAVE_DELAY = 10
# Forget identities absent upstream for this long. The homework window is days,
# so an item that returns after this long is treated as new again.
RETENTION_DAYS = 90

_FULL, _PARTIAL = "full", "partial"


def history_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The per-entry history store (also used to delete it on entry removal)."""
    return Store(
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.history.{entry_id}",
        private=True,
        atomic_writes=True,
    )


def _seen_map(raw: Any) -> dict[str, str]:
    """identity -> ISO last-seen date, dropping anything malformed."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, seen in raw.items():
        if isinstance(key, str) and isinstance(seen, str):
            try:
                date.fromisoformat(seen)
            except ValueError:
                continue
            out[key] = seen
    return out


class SmartSchoolEvents:
    """Track each config entry independently; never log content or raw data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        store: Store[dict[str, Any]] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._store = store if store is not None else history_store(hass, entry_id)
        # (student_id, full_window) -> identity -> last-seen ISO date
        self._homework: dict[tuple[str, bool], dict[str, str]] = {}
        self._sources: dict[str, bool] = {}
        # student_id -> ISO date the student was last in the roster
        self._students: dict[str, str] = {}
        # None until the first fresh inbox response establishes a baseline.
        self._messages: dict[str, str] | None = None
        # Set once history is flushed on unload: a poll that was already in
        # flight must not announce items or write history after that point.
        self._closed = False

    # ------------------------------------------------------------------ storage
    async def async_load(self) -> None:
        """Restore history. Anything unusable means starting fresh (silently)."""
        try:
            data = await self._store.async_load()
        except HomeAssistantError as err:
            # e.g. written by a newer version after a downgrade. (A corrupt
            # file is moved aside and reported by Home Assistant itself.)
            _LOGGER.warning(
                "SmartSchool history could not be loaded (%s); starting fresh",
                type(err).__name__,
            )
            return
        if isinstance(data, dict):
            self._restore(data)

    def _restore(self, data: dict[str, Any]) -> None:
        homework = data.get("homework")
        if isinstance(homework, dict):
            for student_id, buckets in homework.items():
                if not isinstance(student_id, str) or not isinstance(buckets, dict):
                    continue
                for name, full in ((_FULL, True), (_PARTIAL, False)):
                    seen = _seen_map(buckets.get(name))
                    if seen:
                        self._homework[(student_id, full)] = seen
        sources = data.get("sources")
        if isinstance(sources, dict):
            self._sources = {
                sid: full
                for sid, full in sources.items()
                if isinstance(sid, str) and isinstance(full, bool)
            }
        messages = data.get("messages")
        if isinstance(messages, dict):
            self._messages = _seen_map(messages)
        self._students = _seen_map(data.get("students"))
        # Any student known only through homework/sources (no valid date)
        # starts its retention clock now rather than being dropped.
        today = dt_util.now().date().isoformat()
        for student_id in {sid for sid, _ in self._homework} | set(self._sources):
            self._students.setdefault(student_id, today)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        homework: dict[str, dict[str, dict[str, str]]] = {}
        for (student_id, full), seen in self._homework.items():
            homework.setdefault(student_id, {})[_FULL if full else _PARTIAL] = dict(seen)
        return {
            "homework": homework,
            "sources": dict(self._sources),
            "students": dict(self._students),
            "messages": None if self._messages is None else dict(self._messages),
        }

    async def async_flush(self) -> None:
        """Write history now and stop tracking (on unload).

        A reload then reads current data, and a removal is not undone by a late
        delayed save; this also cancels any delayed save still pending.
        """
        self._closed = True
        await self._store.async_save(self._data_to_save())

    # ------------------------------------------------------------------ polling
    @callback
    def async_process(self, data: SmartSchoolData) -> None:
        """Publish only new identities, after establishing reliable baselines."""
        if self._closed:
            return
        today = dt_util.now().date().isoformat()
        changed = False
        try:
            for student in data.students:
                student_id = student.student_id
                if self._students.get(student_id) != today:
                    self._students[student_id] = today
                    changed = True
                full = data.full_window.get(student_id, False)
                seen = self._homework.setdefault((student_id, full), {})
                # Sources use different date identities. Seed every transition
                # to avoid announcing existing work when fallback starts or
                # recovers (including across a restart).
                previous = self._sources.get(student_id)
                notify = previous == full
                if previous != full:
                    self._sources[student_id] = full
                    changed = True
                for item in data.homework.get(student_id, []):
                    key = item.identity()
                    if key in seen:
                        if seen[key] != today:
                            seen[key] = today
                            changed = True
                        continue
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
                    # Record only after a successful publish, so a failed
                    # item is retried on a later poll if it is still present.
                    seen[key] = today
                    changed = True

            # Retained data from a failed inbox request is not a baseline. The
            # first real inbox response must be silent even after outages.
            if data.messages_fresh:
                notify = self._messages is not None
                if self._messages is None:
                    self._messages = {}
                    changed = True
                for message in data.messages:
                    key = message.identity()
                    if key in self._messages:
                        if self._messages[key] != today:
                            self._messages[key] = today
                            changed = True
                        continue
                    if notify:
                        self._fire(
                            EVENT_NEW_MESSAGE,
                            f"{self.entry_id}_inbox",
                            {"item_id": key, **message.as_dict()},
                        )
                    self._messages[key] = today
                    changed = True

            if self._prune(today):
                changed = True
        finally:
            # Persist what was recorded even if a publish failed part-way, so
            # items already announced are not announced again after a restart.
            if changed:
                self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _prune(self, today: str) -> bool:
        """Drop identities and students unseen for RETENTION_DAYS."""
        cutoff = (date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)).isoformat()
        pruned = False
        for seen in self._homework.values():
            for key in [k for k, last in seen.items() if last < cutoff]:
                del seen[key]
                pruned = True
        # A student out of the roster for the whole retention period is
        # forgotten, so a later reappearance seeds silently like a new student.
        # A brief absence keeps their baseline.
        for student_id in [s for s, last in self._students.items() if last < cutoff]:
            del self._students[student_id]
            self._sources.pop(student_id, None)
            self._homework.pop((student_id, True), None)
            self._homework.pop((student_id, False), None)
            pruned = True
        if self._messages:
            for key in [k for k, last in self._messages.items() if last < cutoff]:
                del self._messages[key]
                pruned = True
        return pruned

    @callback
    def _fire(self, event_type: str, identifier: str, payload: dict) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, identifier)})
        event_data = {"entry_id": self.entry_id, **payload}
        if device is not None:
            event_data["device_id"] = device.id
        self.hass.bus.async_fire(event_type, event_data)

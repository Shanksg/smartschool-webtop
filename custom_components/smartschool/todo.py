"""To-do list platform: one list per student, one item per homework assignment.

Items come from SmartSchool and cannot be created, renamed or deleted here.
They can be ticked off: an item is "completed" by default once its (real) due
date has passed, and ticking or un-ticking overrides that. The overrides are
stored per config entry as item fingerprints and statuses only - never school
content - and are kept across polls and restarts.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.models import HomeworkItem, Student
from .const import DOMAIN
from .coordinator import SmartSchoolCoordinator, SmartSchoolData
from .students import DEVICE_NAME_PREFIX, device_identifier

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Forget a tick for an item not seen upstream for this long.
RETENTION_DAYS = 90


def todo_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The per-entry tick-state store (also used to delete it on entry removal)."""
    return Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.todo.{entry_id}", private=True, atomic_writes=True
    )


def _due(item: HomeworkItem) -> date | None:
    """The item's due date, or None when unknown (a synthetic, stamped-today date)."""
    if item.date_is_synthetic or not item.date:
        return None
    try:
        return date.fromisoformat(item.date[:10])
    except ValueError:
        return None


def _default_status(item: HomeworkItem, today: date) -> TodoItemStatus:
    due = _due(item)
    if due is not None and due < today:
        return TodoItemStatus.COMPLETED
    return TodoItemStatus.NEEDS_ACTION


def _description(item: HomeworkItem) -> str:
    lines = [item.homework.strip()] if item.homework else []
    if item.teacher:
        lines.append(item.teacher.strip())
    return "\n".join(line for line in lines if line)


class TodoState:
    """Ticked / un-ticked overrides: student key -> identity -> {status, seen}."""

    def __init__(self, store: Store[dict[str, Any]]) -> None:
        self._store = store
        self._overrides: dict[str, dict[str, dict[str, str]]] = {}

    async def async_load(self) -> None:
        try:
            data = await self._store.async_load()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "SmartSchool to-do state could not be loaded (%s); starting fresh",
                type(err).__name__,
            )
            return
        overrides = data.get("overrides") if isinstance(data, dict) else None
        if not isinstance(overrides, dict):
            return
        valid = {status.value for status in TodoItemStatus}
        for student, items in overrides.items():
            if not isinstance(student, str) or not isinstance(items, dict):
                continue
            for identity, entry in items.items():
                if (
                    isinstance(identity, str)
                    and isinstance(entry, dict)
                    and entry.get("status") in valid
                    and isinstance(entry.get("seen"), str)
                ):
                    self._overrides.setdefault(student, {})[identity] = {
                        "status": entry["status"],
                        "seen": entry["seen"],
                    }

    def status(self, student: str, item: HomeworkItem, today: date) -> TodoItemStatus:
        entry = self._overrides.get(student, {}).get(item.identity())
        if entry is not None:
            return TodoItemStatus(entry["status"])
        return _default_status(item, today)

    async def async_set(
        self, student: str, item: HomeworkItem, status: TodoItemStatus, today: date
    ) -> None:
        identity = item.identity()
        if status == _default_status(item, today):
            # Back to what the date implies: no override needed.
            items = self._overrides.get(student, {})
            items.pop(identity, None)
            if not items:
                self._overrides.pop(student, None)
        else:
            self._overrides.setdefault(student, {})[identity] = {
                "status": status.value,
                "seen": today.isoformat(),
            }
        await self._async_save()

    @callback
    def async_touch(self, student: str, items: list[HomeworkItem], today: date) -> bool:
        """Refresh 'seen' for overridden items still upstream; prune stale ones."""
        changed = False
        stamp = today.isoformat()
        present = {item.identity() for item in items}
        seen = self._overrides.get(student, {})
        for identity in present & set(seen):
            if seen[identity]["seen"] != stamp:
                seen[identity]["seen"] = stamp
                changed = True
        cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
        for student_key in list(self._overrides):
            items_ = self._overrides[student_key]
            for identity in [i for i, e in items_.items() if e["seen"] < cutoff]:
                del items_[identity]
                changed = True
            if not items_:
                del self._overrides[student_key]
        if changed:
            self._store.async_delay_save(self._data, 10)
        return changed

    def _data(self) -> dict[str, Any]:
        return {
            "overrides": {
                student: {identity: dict(entry) for identity, entry in items.items()}
                for student, items in self._overrides.items()
            }
        }

    async def _async_save(self) -> None:
        # User actions are rare: write immediately so a tick is never lost.
        await self._store.async_save(self._data())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one homework to-do list per student."""
    coordinator: SmartSchoolCoordinator = hass.data[DOMAIN][entry.entry_id]
    state = TodoState(todo_store(hass, entry.entry_id))
    await state.async_load()

    known: set[str] = set()

    @callback
    def _add_new_students() -> None:
        data = coordinator.data
        if data is None:
            return
        new: list[HomeworkTodoList] = []
        for student in data.students:
            key = data.key_of(student)
            if key in known:
                continue
            known.add(key)
            new.append(HomeworkTodoList(coordinator, entry.entry_id, student, key, state))
        if new:
            async_add_entities(new)

    _add_new_students()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))


class HomeworkTodoList(CoordinatorEntity[SmartSchoolCoordinator], TodoListEntity):
    """A student's homework as a to-do list (tick-off only)."""

    _attr_has_entity_name = True
    _attr_name = "Homework"
    _attr_icon = "mdi:clipboard-text"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(
        self,
        coordinator: SmartSchoolCoordinator,
        entry_id: str,
        student: Student,
        key: str,
        state: TodoState,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._state = state
        dev = device_identifier(entry_id, key)
        self._attr_unique_id = f"{dev}_todo"
        # Same device as the student's homework sensors.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev)},
            name=f"{DEVICE_NAME_PREFIX}{student.name}".strip(),
            manufacturer="SmartSchool",
            model="Homework Tracker",
        )
        self._attr_todo_items = self._build_items()

    def _homework(self) -> list[HomeworkItem]:
        data: SmartSchoolData | None = self.coordinator.data
        return data.homework.get(self._key, []) if data else []

    def _build_items(self) -> list[TodoItem]:
        today = dt_util.now().date()
        homework = sorted(self._homework(), key=lambda h: (h.date or "", h.subject or ""))
        self._state.async_touch(self._key, homework, today)
        items: list[TodoItem] = []
        seen: set[str] = set()
        for item in homework:
            uid = item.identity()
            if uid in seen:
                continue  # identical entries collapse, as for events
            seen.add(uid)
            items.append(
                TodoItem(
                    summary=item.subject or "Homework",
                    uid=uid,
                    status=self._state.status(self._key, item, today),
                    due=_due(item),
                    description=_description(item) or None,
                )
            )
        return items

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_todo_items = self._build_items()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._key in data.homework

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Only ticking off / un-ticking is supported."""
        source = next((h for h in self._homework() if h.identity() == item.uid), None)
        current = next((t for t in self.todo_items or [] if t.uid == item.uid), None)
        if source is None or current is None:
            raise ServiceValidationError("That homework item is no longer listed by SmartSchool")
        if item.summary != current.summary:
            raise ServiceValidationError(
                "SmartSchool homework can't be renamed here; only ticking it off is saved"
            )
        if item.status is not None and item.status != current.status:
            await self._state.async_set(
                self._key, source, TodoItemStatus(item.status), dt_util.now().date()
            )
        self._attr_todo_items = self._build_items()
        self.async_write_ha_state()

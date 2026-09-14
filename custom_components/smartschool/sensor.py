"""Sensor platform for the SmartSchool (Webtop) integration.

Exposes, off the coordinator, per-student homework sensors and account-level
inbox sensors. Long rendered lists live in the `details` attribute, since a
Home Assistant sensor state is capped at 255 characters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.models import HomeworkItem, Message, Student
from .const import DOMAIN
from .coordinator import SmartSchoolCoordinator, SmartSchoolData

INBOX_ID = "inbox"


def _today() -> str:
    # HA's configured timezone, not the host clock: a UTC container with a
    # local timezone set would otherwise misclassify assignments around
    # midnight as due yesterday/tomorrow.
    return dt_util.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# per-student homework sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class HomeworkSensorDescription(SensorEntityDescription):
    """Describes a per-student homework sensor.

    `full_window` is True when the snapshot is the full multi-day PupilCard
    window, False when it is the today-only dashboard fallback. Window-dependent
    values return None (state "unknown") during the fallback rather than
    under-reporting the week as if it were complete.
    """

    value_fn: Callable[[list[HomeworkItem], bool], Any]
    attrs_fn: Callable[[list[HomeworkItem], bool], dict[str, Any]] | None = None


def _today_items(items: list[HomeworkItem]) -> list[HomeworkItem]:
    today = _today()
    return [h for h in items if (h.date or "")[:10] == today]


def _render_list(items: list[HomeworkItem], header: str) -> str:
    lines = [header]
    for hw in sorted(items, key=lambda h: (h.date or "", h.subject or "")):
        date = (hw.date or "")[:10]
        lines.append(f"{hw.subject} ({date}) - {hw.teacher}")
        lines.append(f"  {hw.homework}")
    return "\n".join(lines)


def _render_homework(items: list[HomeworkItem], full_window: bool) -> str:
    # Today-first, matching the standalone contract: show today's items when any
    # are due, and fall back to the whole visible window only when nothing is
    # due today - and only when we actually have the full window.
    todays = _today_items(items)
    if todays:
        return _render_list(todays, "Today's homework:")
    if full_window and items:
        return _render_list(items, "No homework today. This week:")
    if not full_window:
        return "No homework today (this-week view unavailable)"
    return "No homework"


def _details_state(items: list[HomeworkItem], full_window: bool) -> str:
    today = len(_today_items(items))
    if full_window:
        return f"{today} today / {len(items)} this week"
    return f"{today} today"


HOMEWORK_SENSORS: tuple[HomeworkSensorDescription, ...] = (
    HomeworkSensorDescription(
        key="count",
        name="Homework Count",
        icon="mdi:book-open-variant",
        native_unit_of_measurement="items",
        # Today's count is valid from either source.
        value_fn=lambda items, full: len(_today_items(items)),
    ),
    HomeworkSensorDescription(
        key="count_week",
        name="Homework This Week",
        icon="mdi:calendar-week",
        native_unit_of_measurement="items",
        # The dashboard fallback only has today, so the week is unknown then.
        value_fn=lambda items, full: len(items) if full else None,
    ),
    HomeworkSensorDescription(
        key="count_upcoming",
        name="Homework Upcoming",
        icon="mdi:calendar-arrow-right",
        native_unit_of_measurement="items",
        value_fn=lambda items, full: (
            sum(1 for h in items if (h.date or "")[:10] >= _today()) if full else None
        ),
    ),
    HomeworkSensorDescription(
        key="details",
        name="Homework Details",
        icon="mdi:text-box-multiple",
        # State stays short (HA caps it at 255); the full list is an attribute.
        value_fn=_details_state,
        attrs_fn=lambda items, full: {"text": _render_homework(items, full)},
    ),
)


# ---------------------------------------------------------------------------
# account-level inbox sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class MessageSensorDescription(SensorEntityDescription):
    """Describes an inbox sensor."""

    value_fn: Callable[[list[Message]], Any]
    attrs_fn: Callable[[list[Message]], dict[str, Any]] | None = None


def _render_messages(messages: list[Message]) -> str:
    if not messages:
        return "No messages"
    lines: list[str] = []
    for m in sorted(messages, key=lambda m: m.sent_at or "", reverse=True):
        mark = " " if m.has_read else "*"
        date = (m.sent_at or "")[:16].replace("T", " ")
        clip = "  [attachment]" if m.has_files else ""
        lines.append(f"{mark} {m.subject}{clip}")
        lines.append(f"    {m.sender or 'Unknown'} - {date}")
    return "\n".join(lines)


def _latest_subject(messages: list[Message]) -> str:
    if not messages:
        return "No messages"
    newest = max(messages, key=lambda m: m.sent_at or "")
    return (newest.subject or "(no subject)")[:250]


MESSAGE_SENSORS: tuple[MessageSensorDescription, ...] = (
    MessageSensorDescription(
        key="unread",
        name="Messages Unread",
        icon="mdi:email-alert",
        native_unit_of_measurement="messages",
        value_fn=lambda msgs: sum(1 for m in msgs if not m.has_read),
    ),
    MessageSensorDescription(
        key="total",
        name="Messages Total",
        icon="mdi:email-multiple",
        native_unit_of_measurement="messages",
        value_fn=lambda msgs: len(msgs),
    ),
    MessageSensorDescription(
        key="latest",
        name="Latest Message",
        icon="mdi:email-open",
        value_fn=_latest_subject,
    ),
    MessageSensorDescription(
        key="details",
        name="Messages Details",
        icon="mdi:text-box-multiple",
        value_fn=lambda msgs: (
            f"{sum(1 for m in msgs if not m.has_read)} unread / {len(msgs)} total"
        ),
        attrs_fn=lambda msgs: {"text": _render_messages(msgs)},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartSchool sensors from a config entry."""
    coordinator: SmartSchoolCoordinator = hass.data[DOMAIN][entry.entry_id]

    # The inbox is account-level: one set per entry, added once.
    async_add_entities(MessageSensor(coordinator, entry.entry_id, desc) for desc in MESSAGE_SENSORS)

    # Homework sensors are per student, and the roster is re-discovered on every
    # coordinator update. Add sensors for students as they appear (including a
    # student linked after setup) instead of only from the first snapshot.
    known: set[str] = set()

    @callback
    def _add_new_students() -> None:
        data = coordinator.data
        if data is None:
            return
        new_entities: list[SensorEntity] = []
        for student in data.students:
            if student.student_id in known:
                continue
            known.add(student.student_id)
            for desc in HOMEWORK_SENSORS:
                new_entities.append(HomeworkSensor(coordinator, entry.entry_id, student, desc))
        if new_entities:
            async_add_entities(new_entities)

    _add_new_students()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_students))


class HomeworkSensor(CoordinatorEntity[SmartSchoolCoordinator], SensorEntity):
    """A per-student homework sensor."""

    _attr_has_entity_name = True
    entity_description: HomeworkSensorDescription

    def __init__(
        self,
        coordinator: SmartSchoolCoordinator,
        entry_id: str,
        student: Student,
        description: HomeworkSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._student_id = student.student_id
        # Scope ids by the config entry: two accounts must not share a device
        # or collide in the entity registry, even for the same student id.
        dev = f"{entry_id}_student_{self._student_id}"
        self._attr_unique_id = f"{dev}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev)},
            name=f"SmartSchool - {student.name}".strip(),
            manufacturer="SmartSchool",
            model="Homework Tracker",
        )

    def _items(self) -> list[HomeworkItem]:
        data: SmartSchoolData | None = self.coordinator.data
        if not data:
            return []
        return data.homework.get(self._student_id, [])

    def _full_window(self) -> bool:
        data: SmartSchoolData | None = self.coordinator.data
        if not data:
            return True
        # Default True: an unknown provenance is treated as a complete window.
        return data.full_window.get(self._student_id, True)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._items(), self._full_window())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._items(), self._full_window())

    @property
    def available(self) -> bool:
        # Available while the coordinator is healthy and still knows this
        # student (it may drop from a later discovery).
        return (
            super().available
            and self.coordinator.data is not None
            and self._student_id in self.coordinator.data.homework
        )


class MessageSensor(CoordinatorEntity[SmartSchoolCoordinator], SensorEntity):
    """An account-level inbox sensor."""

    _attr_has_entity_name = True
    entity_description: MessageSensorDescription

    def __init__(
        self,
        coordinator: SmartSchoolCoordinator,
        entry_id: str,
        description: MessageSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # The inbox device/unique ids are otherwise constant across entries;
        # scope them by the config entry so a second account cannot merge into
        # or overwrite the first account's inbox.
        dev = f"{entry_id}_{INBOX_ID}"
        self._attr_unique_id = f"{dev}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev)},
            name="SmartSchool - Messages",
            manufacturer="SmartSchool",
            model="Message Inbox",
        )

    def _messages(self) -> list[Message]:
        data: SmartSchoolData | None = self.coordinator.data
        return data.messages if data else []

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._messages())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._messages())

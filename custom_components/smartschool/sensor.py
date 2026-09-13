"""Sensor platform for the SmartSchool (Webtop) integration.

Exposes, off the coordinator, per-student homework sensors and account-level
inbox sensors. Long rendered lists live in the `details` attribute, since a
Home Assistant sensor state is capped at 255 characters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import HomeworkItem, Message, Student
from .const import DOMAIN
from .coordinator import SmartSchoolCoordinator, SmartSchoolData

INBOX_ID = "inbox"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# per-student homework sensors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class HomeworkSensorDescription(SensorEntityDescription):
    """Describes a per-student homework sensor."""

    value_fn: Callable[[list[HomeworkItem]], Any]
    attrs_fn: Callable[[list[HomeworkItem]], dict[str, Any]] | None = None


def _render_homework(items: list[HomeworkItem]) -> str:
    if not items:
        return "No homework"
    lines: list[str] = []
    for hw in sorted(items, key=lambda h: (h.date or "", h.subject or "")):
        date = (hw.date or "")[:10]
        lines.append(f"{hw.subject} ({date}) - {hw.teacher}")
        lines.append(f"  {hw.homework}")
    return "\n".join(lines)


HOMEWORK_SENSORS: tuple[HomeworkSensorDescription, ...] = (
    HomeworkSensorDescription(
        key="count",
        name="Homework Count",
        icon="mdi:book-open-variant",
        native_unit_of_measurement="items",
        value_fn=lambda items: sum(1 for h in items if (h.date or "")[:10] == _today()),
    ),
    HomeworkSensorDescription(
        key="count_week",
        name="Homework This Week",
        icon="mdi:calendar-week",
        native_unit_of_measurement="items",
        value_fn=lambda items: len(items),
    ),
    HomeworkSensorDescription(
        key="count_upcoming",
        name="Homework Upcoming",
        icon="mdi:calendar-arrow-right",
        native_unit_of_measurement="items",
        value_fn=lambda items: sum(1 for h in items if (h.date or "")[:10] >= _today()),
    ),
    HomeworkSensorDescription(
        key="details",
        name="Homework Details",
        icon="mdi:text-box-multiple",
        # State stays short (HA caps it at 255); the full list is an attribute.
        value_fn=lambda items: (
            f"{sum(1 for h in items if (h.date or '')[:10] == _today())} today"
            f" / {len(items)} this week"
        ),
        attrs_fn=lambda items: {"text": _render_homework(items)},
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

    entities: list[SensorEntity] = []
    for student in coordinator.data.students:
        for desc in HOMEWORK_SENSORS:
            entities.append(HomeworkSensor(coordinator, student, desc))
    for desc in MESSAGE_SENSORS:
        entities.append(MessageSensor(coordinator, desc))

    async_add_entities(entities)


class HomeworkSensor(CoordinatorEntity[SmartSchoolCoordinator], SensorEntity):
    """A per-student homework sensor."""

    _attr_has_entity_name = True
    entity_description: HomeworkSensorDescription

    def __init__(
        self,
        coordinator: SmartSchoolCoordinator,
        student: Student,
        description: HomeworkSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._student_id = student.student_id
        dev = f"student_{self._student_id}"
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

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._items())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._items())

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
        description: MessageSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{INBOX_ID}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, INBOX_ID)},
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

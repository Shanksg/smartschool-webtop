"""Persistent event history (Phase 7): restarts, storage failures, pruning."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

import custom_components.smartschool as integration  # noqa: E402
from custom_components.smartschool import events as events_mod  # noqa: E402
from custom_components.smartschool.api.models import HomeworkItem, Message, Student  # noqa: E402
from custom_components.smartschool.const import EVENT_NEW_HOMEWORK, EVENT_NEW_MESSAGE  # noqa: E402
from custom_components.smartschool.coordinator import SmartSchoolData  # noqa: E402
from custom_components.smartschool.events import (  # noqa: E402
    RETENTION_DAYS,
    SAVE_DELAY,
    SmartSchoolEvents,
)

OLD = HomeworkItem("Math", "Exercise 1", "2026-09-15")
NEW = HomeworkItem("Math", "Exercise 2", "2026-09-16")
NOTICE = Message("Existing notice", sender="School", sent_at="2026-09-15T08:00:00")
FRESH_NOTICE = Message("New notice", sender="School", sent_at="2026-09-16T08:00:00")


def snapshot(homework=(), messages=(), *, full=True, fresh=True, students=("student-a",)):
    return SmartSchoolData(
        [Student(s, "Example student") for s in students],
        {s: list(homework) for s in students},
        list(messages),
        {s: full for s in students},
        messages_fresh=fresh,
    )


@pytest.fixture
def today(monkeypatch):
    """Set the date the tracker sees: today("2026-09-20")."""
    def set_day(day):
        monkeypatch.setattr(events_mod.dt_util, "now", lambda: datetime.fromisoformat(day))
    set_day("2026-09-20")
    return set_day


@pytest.fixture
def hass(monkeypatch):
    registry = Mock()
    registry.async_get_device.return_value = SimpleNamespace(id="device-a")
    monkeypatch.setattr(events_mod.dr, "async_get", lambda hass: registry)
    return SimpleNamespace(bus=Mock())


def start(hass, store):
    """A fresh load of the integration: new tracker, history read from store."""
    tracker = SmartSchoolEvents(hass, "entry-a", store=store)
    asyncio.run(tracker.async_load())
    return tracker


def fired(hass, event_type=None):
    calls = hass.bus.async_fire.call_args_list
    return [c.args[1] for c in calls if event_type is None or c.args[0] == event_type]


# ---------------------------------------------------------------- restarts
def test_fresh_install_first_poll_is_silent(hass, make_store, today):
    tracker = start(hass, make_store())
    tracker.async_process(snapshot([OLD], [NOTICE]))
    assert fired(hass) == []
    tracker.async_process(snapshot([OLD, NEW], [NOTICE, FRESH_NOTICE]))
    assert len(fired(hass, EVENT_NEW_HOMEWORK)) == 1
    assert len(fired(hass, EVENT_NEW_MESSAGE)) == 1


def test_items_that_arrived_while_offline_fire_after_restart(hass, make_store, today):
    store = make_store()
    start(hass, store).async_process(snapshot([OLD], [NOTICE]))
    assert fired(hass) == []

    # Home Assistant restarts; meanwhile one homework item and one message arrived.
    after = start(hass, store)
    after.async_process(snapshot([OLD, NEW], [NOTICE, FRESH_NOTICE]))
    homework = fired(hass, EVENT_NEW_HOMEWORK)
    messages = fired(hass, EVENT_NEW_MESSAGE)
    assert [e["item_id"] for e in homework] == [NEW.identity()]
    assert [e["item_id"] for e in messages] == [FRESH_NOTICE.identity()]


def test_restart_does_not_repeat_already_announced_items(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD]))
    tracker.async_process(snapshot([OLD, NEW]))
    assert len(fired(hass)) == 1
    start(hass, store).async_process(snapshot([OLD, NEW]))
    assert len(fired(hass)) == 1


def test_inbox_without_a_baseline_stays_silent_after_restart(hass, make_store, today):
    # Only failed inbox polls before the restart: no inbox baseline was stored.
    store = make_store()
    start(hass, store).async_process(snapshot([OLD], [NOTICE], fresh=False))
    assert store.data["messages"] is None
    start(hass, store).async_process(snapshot([OLD], [NOTICE, FRESH_NOTICE]))
    assert fired(hass, EVENT_NEW_MESSAGE) == []


def test_source_switch_across_restart_seeds_silently(hass, make_store, today):
    store = make_store()
    start(hass, store).async_process(snapshot([OLD], full=True))
    # After the restart PupilCard is unavailable; the dashboard fallback uses a
    # different date identity, so its items must not look new.
    fallback = HomeworkItem("Math", "Exercise 1", "2026-09-20", date_is_synthetic=True)
    start(hass, store).async_process(snapshot([fallback], full=False))
    assert fired(hass) == []


# ---------------------------------------------------------------- storage failures
@pytest.mark.parametrize("data", [
    [],
    "not-a-dict",
    {"homework": "x", "sources": [], "messages": 5, "students": None},
    {"homework": {"student-a": {"full": {"id": 7, 8: "2026-09-01", "ok": "not-a-date"}}},
     "sources": {"student-a": "yes"}},
])
def test_malformed_storage_is_ignored_and_first_poll_is_silent(hass, make_store, today, data):
    tracker = start(hass, make_store(data=data))
    tracker.async_process(snapshot([OLD], [NOTICE]))
    assert fired(hass) == []


def test_unreadable_storage_starts_fresh_and_logs_type_only(hass, make_store, today, caplog):
    store = make_store(error=HomeAssistantError("private-storage-detail"))
    tracker = start(hass, store)
    assert "HomeAssistantError" in caplog.text
    assert "private-storage-detail" not in caplog.text
    tracker.async_process(snapshot([OLD]))
    assert fired(hass) == []


def test_valid_parts_of_partly_malformed_storage_are_kept(hass, make_store, today):
    store = make_store()
    start(hass, store).async_process(snapshot([OLD]))
    store.data["messages"] = "garbage"  # damaged inbox part only
    start(hass, store).async_process(snapshot([OLD, NEW]))
    assert [e["item_id"] for e in fired(hass, EVENT_NEW_HOMEWORK)] == [NEW.identity()]


def test_storage_holds_hashes_not_content(hass, make_store, today):
    store = make_store()
    start(hass, store).async_process(snapshot([OLD], [NOTICE]))
    dumped = repr(store.data)
    for text in ("Math", "Exercise 1", "Existing notice", "School", "Example student"):
        assert text not in dumped


# ---------------------------------------------------------------- pruning / retention
def test_identities_absent_past_retention_are_pruned(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD]))
    assert OLD.identity() in store.data["homework"]["student-a"]["full"]

    today("2027-01-01")  # > RETENTION_DAYS later, OLD gone upstream
    tracker.async_process(snapshot([NEW]))
    kept = store.data["homework"]["student-a"]["full"]
    assert OLD.identity() not in kept and NEW.identity() in kept


def test_present_items_are_never_pruned(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD]))
    today("2027-01-01")
    tracker.async_process(snapshot([OLD]))
    assert store.data["homework"]["student-a"]["full"][OLD.identity()] == "2027-01-01"
    assert fired(hass) == []


def test_brief_roster_absence_keeps_the_baseline(hass, make_store, today):
    tracker = start(hass, make_store())
    tracker.async_process(snapshot([], students=("student-a",)))
    tracker.async_process(snapshot([], students=("student-b",)))  # a missing from one poll
    tracker.async_process(snapshot([NEW], students=("student-a",)))
    assert [e["student_id"] for e in fired(hass, EVENT_NEW_HOMEWORK)] == ["student-a"]


def test_present_student_with_no_homework_keeps_the_baseline(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([]))
    start(hass, store).async_process(snapshot([NEW]))
    assert len(fired(hass, EVENT_NEW_HOMEWORK)) == 1


def test_student_gone_past_retention_is_forgotten(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD], students=("student-a",)))
    today("2027-01-01")
    tracker.async_process(snapshot([], students=("student-b",)))
    assert "student-a" not in store.data["students"]
    assert "student-a" not in store.data["sources"]
    assert "student-a" not in store.data["homework"]
    # Reappearing much later is treated like a new student: silent seed.
    tracker.async_process(snapshot([NEW], students=("student-a",)))
    assert fired(hass) == []


def test_old_messages_are_pruned(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot(messages=[NOTICE]))
    today("2027-01-01")
    tracker.async_process(snapshot(messages=[FRESH_NOTICE]))
    assert list(store.data["messages"]) == [FRESH_NOTICE.identity()]


def test_retention_is_ninety_days():
    assert RETENTION_DAYS == 90


# ---------------------------------------------------------------- writes
def test_saves_are_delayed_and_only_when_something_changed(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD], [NOTICE]))
    assert store.saves == 1 and store.delays == [SAVE_DELAY]
    tracker.async_process(snapshot([OLD], [NOTICE]))  # same day, nothing new
    assert store.saves == 1
    today("2026-09-21")  # last-seen dates move once per day
    tracker.async_process(snapshot([OLD], [NOTICE]))
    assert store.saves == 2


def test_failed_publish_still_saves_what_was_announced(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD]))
    first, second = HomeworkItem("Math", "A"), HomeworkItem("Math", "B")
    hass.bus.async_fire.side_effect = [None, RuntimeError("bus down")]
    with pytest.raises(RuntimeError):
        tracker.async_process(snapshot([OLD, first, second]))
    hass.bus.async_fire.reset_mock(side_effect=True)

    # Restart before the next poll: `first` was announced, `second` was not.
    start(hass, store).async_process(snapshot([OLD, first, second]))
    assert [e["item_id"] for e in fired(hass)] == [second.identity()]


def test_flush_writes_immediately(hass, make_store, today):
    store = make_store()
    tracker = start(hass, store)
    tracker.async_process(snapshot([OLD]))
    store.data = None
    asyncio.run(tracker.async_flush())
    assert OLD.identity() in store.data["homework"]["student-a"]["full"]


def test_real_store_round_trip_is_private_and_per_entry(tmp_path, monkeypatch, today):
    """Write through Home Assistant's real Store, then read it back cold."""
    import json
    import os

    from homeassistant.core import HomeAssistant

    monkeypatch.setattr(events_mod.dr, "async_get", lambda hass: Mock(
        async_get_device=Mock(return_value=None)))

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        try:
            first = SmartSchoolEvents(hass, "entry-a")
            await first.async_load()
            first.async_process(snapshot([OLD], [NOTICE]))
            await first.async_flush()

            path = tmp_path / ".storage" / "smartschool.history.entry-a"
            on_disk = json.loads(path.read_text())
            mode = os.stat(path).st_mode & 0o777

            fired_events = []
            hass.bus.async_listen(EVENT_NEW_HOMEWORK, fired_events.append)
            second = SmartSchoolEvents(hass, "entry-a")
            await second.async_load()
            second.async_process(snapshot([OLD, NEW], [NOTICE]))
            await hass.async_block_till_done()
            other = events_mod.history_store(hass, "entry-b").path
            return on_disk, mode, [e.data["item_id"] for e in fired_events], other
        finally:
            await hass.async_stop(force=True)

    on_disk, mode, fired_ids, other_path = asyncio.run(scenario())
    assert on_disk["key"] == "smartschool.history.entry-a"
    assert OLD.identity() in on_disk["data"]["homework"]["student-a"]["full"]
    assert mode == 0o600, "history is written with private permissions"
    assert fired_ids == [NEW.identity()], "restart detects the item added in between"
    assert other_path.endswith("smartschool.history.entry-b")


# ---------------------------------------------------------------- integration setup wiring
def test_setup_loads_history_before_the_first_refresh(monkeypatch):
    order = []

    class Coordinator:
        def __init__(self, hass, entry):
            pass

        async def async_load_history(self):
            order.append("load")

        async def async_config_entry_first_refresh(self):
            order.append("refresh")

    monkeypatch.setattr(integration, "SmartSchoolCoordinator", Coordinator)
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
    )
    asyncio.run(integration.async_setup_entry(hass, SimpleNamespace(entry_id="entry-a")))
    assert order == ["load", "refresh"]


def test_unload_flushes_history_before_closing(monkeypatch):
    order = []
    coordinator = SimpleNamespace(
        async_flush_history=AsyncMock(side_effect=lambda: order.append("flush")),
        async_shutdown_client=lambda: order.append("close"),
    )

    async def executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        data={"smartschool": {"entry-a": coordinator}},
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        async_add_executor_job=executor,
    )
    assert asyncio.run(integration.async_unload_entry(hass, SimpleNamespace(entry_id="entry-a")))
    assert order == ["flush", "close"]


def test_removing_the_entry_deletes_its_history(monkeypatch, make_store):
    store = make_store(data={"homework": {}})
    monkeypatch.setattr(integration, "history_store", lambda hass, entry_id: store)
    asyncio.run(integration.async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id="entry-a")))
    assert store.removed is True

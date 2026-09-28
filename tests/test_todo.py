"""Homework to-do lists: items, default status, ticking off, persistence."""

import asyncio
from datetime import date, datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components import todo as ha_todo  # noqa: E402
from homeassistant.components.todo import TodoItemStatus, TodoListEntityFeature  # noqa: E402
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402

from custom_components.smartschool import todo as todo_mod  # noqa: E402
from custom_components.smartschool.api.models import HomeworkItem, Student  # noqa: E402
from custom_components.smartschool.coordinator import SmartSchoolData  # noqa: E402

TODAY = "2026-09-20"
PAST = HomeworkItem("Math", "Exercise 1", "2026-09-10", teacher="Ms. T")
DUE = HomeworkItem("English", "Unit 2", TODAY, teacher="Mr. Y")
LATER = HomeworkItem("History", "Chapter 4", "2026-09-25")
FALLBACK = HomeworkItem("Science", "Summary", TODAY, date_is_synthetic=True)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    def set_day(day):
        monkeypatch.setattr(todo_mod.dt_util, "now", lambda: datetime.fromisoformat(day))
    set_day(TODAY)
    return set_day


class StubCoordinator:
    last_update_success = True

    def __init__(self, homework, students=("s1",)):
        self.data = SmartSchoolData(
            [Student(student_id=s, name="Dana") for s in students],
            {s: list(homework) for s in students}, [], {s: True for s in students},
        )

    def async_add_listener(self, *args, **kwargs):
        return lambda: None


def todo_list(homework, store, key="s1"):
    state = todo_mod.TodoState(store)
    asyncio.run(state.async_load())
    coordinator = StubCoordinator(homework)
    entity = todo_mod.HomeworkTodoList(coordinator, "entry-a", coordinator.data.students[0], key, state)
    entity.async_write_ha_state = Mock()
    return entity


def by_uid(entity):
    return {item.uid: item for item in entity.todo_items}


def service(entity, **data):
    """Run Home Assistant's real todo.update_item handler against the entity."""
    call = SimpleNamespace(data=data)
    asyncio.run(ha_todo._async_update_todo_item(entity, call))


# ---------------------------------------------------------------- items
def test_items_map_homework(make_store):
    items = by_uid(todo_list([LATER, DUE, PAST, FALLBACK], make_store()))
    past = items[PAST.identity()]
    assert (past.summary, past.due, past.description) == ("Math", date(2026, 9, 10), "Exercise 1\nMs. T")
    assert items[LATER.identity()].description == "Chapter 4"
    assert items[FALLBACK.identity()].due is None, "a stamped-today date is not a due date"


def test_items_sorted_by_date_and_duplicates_collapse(make_store):
    entity = todo_list([LATER, PAST, DUE, DUE], make_store())
    assert [i.summary for i in entity.todo_items] == ["Math", "English", "History"]


def test_default_status_follows_the_due_date(make_store):
    items = by_uid(todo_list([PAST, DUE, LATER, FALLBACK], make_store()))
    assert items[PAST.identity()].status == TodoItemStatus.COMPLETED
    assert items[DUE.identity()].status == TodoItemStatus.NEEDS_ACTION
    assert items[LATER.identity()].status == TodoItemStatus.NEEDS_ACTION
    assert items[FALLBACK.identity()].status == TodoItemStatus.NEEDS_ACTION


def test_only_ticking_off_is_supported(make_store):
    entity = todo_list([DUE], make_store())
    assert entity.supported_features == TodoListEntityFeature.UPDATE_TODO_ITEM


# ---------------------------------------------------------------- ticking off (real service handler)
def test_tick_off_is_saved_immediately_and_survives_a_restart(make_store):
    store = make_store()
    entity = todo_list([DUE], store)
    service(entity, item=DUE.identity(), status="completed")
    assert by_uid(entity)[DUE.identity()].status == TodoItemStatus.COMPLETED
    assert store.saves >= 1 and store.delays == [], "written immediately, not delayed"
    entity.async_write_ha_state.assert_called()

    restarted = todo_list([DUE], store)
    assert by_uid(restarted)[DUE.identity()].status == TodoItemStatus.COMPLETED


def test_untick_a_past_item_is_kept(make_store):
    store = make_store()
    service(todo_list([PAST], store), item=PAST.identity(), status="needs_action")
    assert by_uid(todo_list([PAST], store))[PAST.identity()].status == TodoItemStatus.NEEDS_ACTION


def test_back_to_the_default_removes_the_override(make_store):
    store = make_store()
    entity = todo_list([DUE], store)
    service(entity, item=DUE.identity(), status="completed")
    service(entity, item=DUE.identity(), status="needs_action")
    assert store.data["overrides"] == {}


def test_rename_is_refused_and_nothing_saved(make_store):
    store = make_store()
    entity = todo_list([DUE], store)
    with pytest.raises(ServiceValidationError):
        service(entity, item=DUE.identity(), rename="Something else")
    assert store.saves == 0
    assert by_uid(entity)[DUE.identity()].summary == "English"


def test_due_date_and_description_edits_are_refused_by_home_assistant(make_store):
    entity = todo_list([DUE], make_store())
    with pytest.raises(ServiceValidationError):
        service(entity, item=DUE.identity(), due_date=date(2026, 12, 1))
    with pytest.raises(ServiceValidationError):
        service(entity, item=DUE.identity(), description="edited")


def test_unknown_item_is_refused(make_store):
    with pytest.raises(ServiceValidationError):
        service(todo_list([DUE], make_store()), item="no-such-item", status="completed")


def test_item_that_left_upstream_cannot_be_ticked(make_store):
    entity = todo_list([DUE], make_store())
    listed = list(entity.todo_items)
    entity.coordinator.data.homework["s1"] = []  # gone upstream, list not refreshed yet
    entity._attr_todo_items = listed
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_update_todo_item(listed[0]))


# ---------------------------------------------------------------- storage
def test_storage_holds_fingerprints_and_status_not_content(make_store):
    store = make_store()
    service(todo_list([DUE], store), item=DUE.identity(), status="completed")
    dumped = json.dumps(store.data)
    for text in ("English", "Unit 2", "Mr. Y", "Dana"):
        assert text not in dumped
    assert store.data["overrides"]["s1"][DUE.identity()]["status"] == "completed"


def test_stale_ticks_are_pruned_present_ones_refreshed(make_store, today):
    store = make_store()
    service(todo_list([DUE, LATER], store), item=DUE.identity(), status="completed")
    service(todo_list([DUE, LATER], store), item=LATER.identity(), status="completed")
    today("2027-01-15")  # > RETENTION_DAYS later; only LATER still listed
    todo_list([LATER], store)
    kept = store.data["overrides"]["s1"]
    assert DUE.identity() not in kept
    assert kept[LATER.identity()]["seen"] == "2027-01-15"


@pytest.mark.parametrize("data", [
    [], "x", {"overrides": "x"},
    {"overrides": {"s1": {"id": {"status": "bogus", "seen": TODAY}}}},
    {"overrides": {"s1": {"id": "not-a-dict"}}},
])
def test_malformed_state_is_ignored(make_store, data):
    items = by_uid(todo_list([DUE], make_store(data=data)))
    assert items[DUE.identity()].status == TodoItemStatus.NEEDS_ACTION


def test_unreadable_state_starts_fresh_and_logs_type_only(make_store, caplog):
    entity = todo_list([PAST], make_store(error=HomeAssistantError("private-detail")))
    assert "HomeAssistantError" in caplog.text and "private-detail" not in caplog.text
    assert by_uid(entity)[PAST.identity()].status == TodoItemStatus.COMPLETED


# ---------------------------------------------------------------- entity / setup
def test_entity_joins_the_students_device(make_store):
    entity = todo_list([DUE], make_store(), key="A")
    assert entity._attr_unique_id == "entry-a_student_A_todo"
    assert entity._attr_device_info["identifiers"] == {("smartschool", "entry-a_student_A")}
    assert entity._attr_device_info["name"] == "SmartSchool - Dana"


def test_unavailable_when_student_drops(make_store):
    entity = todo_list([DUE], make_store())
    entity.coordinator.data.homework.pop("s1")
    assert entity.available is False


def test_refresh_rebuilds_items(make_store):
    entity = todo_list([DUE], make_store())
    entity.coordinator.data.homework["s1"] = [DUE, LATER]
    entity.async_write_ha_state = Mock()
    entity._handle_coordinator_update()
    assert set(by_uid(entity)) == {DUE.identity(), LATER.identity()}


def test_setup_adds_one_list_per_student_by_stable_key(monkeypatch, make_store):
    coordinator = StubCoordinator([DUE], students=("A2", "B"))
    coordinator.data.keys = {"A2": "A", "B": "B"}  # A2 is a rotated id for key A
    coordinator.data.homework = {"A": [DUE], "B": []}
    store = make_store()
    monkeypatch.setattr(todo_mod, "todo_store", lambda hass, entry_id: store)
    listeners, added = [], []
    coordinator.async_add_listener = lambda cb, *a, **k: (listeners.append(cb) or (lambda: None))

    class Entry:
        entry_id = "entry-a"

        def async_on_unload(self, unsub):
            pass

    hass = SimpleNamespace(data={"smartschool": {"entry-a": coordinator}})
    asyncio.run(todo_mod.async_setup_entry(hass, Entry(), lambda new, *a, **k: added.extend(new)))
    assert {e._attr_unique_id for e in added} == {"entry-a_student_A_todo", "entry-a_student_B_todo"}
    for cb in listeners:  # later polls do not duplicate lists
        cb()
    assert len(added) == 2


# ---------------------------------------------------------------- unload
def stale_state(make_store):
    """State with a tick last seen yesterday, so the next touch schedules a save."""
    store = make_store(data={"overrides": {"s1": {DUE.identity(): {"status": "completed", "seen": "2026-09-19"}}}})
    state = todo_mod.TodoState(store)
    asyncio.run(state.async_load())
    return store, state


def test_flush_writes_pending_change_then_stops_writing(make_store):
    store, state = stale_state(make_store)
    assert state.async_touch("s1", [DUE], date(2026, 9, 20)) is True  # delayed save pending
    saves = store.saves
    asyncio.run(state.async_flush())
    assert store.saves == saves + 1, "pending change written immediately"

    # Closed: nothing is written afterwards.
    assert state.async_touch("s1", [DUE], date(2026, 9, 21)) is False
    asyncio.run(state.async_set("s1", DUE, TodoItemStatus.NEEDS_ACTION, date(2026, 9, 21)))
    assert store.saves == saves + 1


def test_flush_without_pending_change_writes_nothing(make_store):
    store = make_store()
    state = todo_mod.TodoState(store)
    asyncio.run(state.async_load())
    asyncio.run(state.async_flush())
    assert store.saves == 0 and store.data is None


def test_setup_flushes_state_on_unload(monkeypatch, make_store):
    coordinator = StubCoordinator([DUE])
    monkeypatch.setattr(todo_mod, "todo_store", lambda hass, entry_id: make_store())
    on_unload = []

    class Entry:
        entry_id = "entry-a"

        def async_on_unload(self, func):
            on_unload.append(func)

    hass = SimpleNamespace(data={"smartschool": {"entry-a": coordinator}})
    asyncio.run(todo_mod.async_setup_entry(hass, Entry(), lambda new, *a, **k: None))
    assert any(getattr(f, "__name__", "") == "async_flush" for f in on_unload)


def test_real_store_removal_is_not_undone_by_a_pending_save(tmp_path, today):
    """HA's real Store: flush cancels the delayed write, so a removal stays removed."""
    from homeassistant.core import HomeAssistant

    async def scenario():
        hass = HomeAssistant(str(tmp_path))
        try:
            store = todo_mod.todo_store(hass, "entry-a")
            await store.async_save({"overrides": {"s1": {DUE.identity(): {
                "status": "completed", "seen": "2026-09-19"}}}})
            state = todo_mod.TodoState(store)
            await state.async_load()
            state.async_touch("s1", [DUE], date(2026, 9, 20))
            pending_before = store._delay_handle is not None
            await state.async_flush()                            # unload
            await todo_mod.todo_store(hass, "entry-a").async_remove()  # removal
            path = tmp_path / ".storage" / "smartschool.todo.entry-a"
            return pending_before, store._delay_handle, path.exists()
        finally:
            await hass.async_stop(force=True)

    pending_before, handle_after, exists = asyncio.run(scenario())
    assert pending_before is True, "the scenario really had a delayed save pending"
    assert handle_after is None, "flush cancelled it"
    assert exists is False

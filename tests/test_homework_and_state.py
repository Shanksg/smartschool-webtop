"""Offline tests for homework extraction, identity and state diffing."""

import json
from pathlib import Path

import pytest

from smartschool.homework import extract, from_dashboard, from_pupilcard
from smartschool.models import HomeworkItem
from smartschool.state import HomeworkState

# ----------------------------------------------------------------------
# PupilCard shape (this repo's original endpoint)
# ----------------------------------------------------------------------
PUPILCARD_DATA = [
    {
        "date": "2026-09-03T00:00:00",
        "hoursData": [
            {
                "scheduale": [
                    {
                        "subject_name": "מתמטיקה",
                        "teacher": "כהן רותי",
                        "homeWork": "תרגילים 1-10 בעמוד 45",
                        "descClass": "שיעור 3",
                    },
                    {
                        "subject_name": "אנגלית",
                        "teacher": "לוי דן",
                        "homeWork": "",  # no homework -> must be skipped
                    },
                ]
            },
            {
                "scheduale": [
                    {
                        "subject_name": "היסטוריה",
                        "teacher": "מזרחי אבי",
                        "homeWork": "  לקרוא פרק 4  ",  # whitespace must be trimmed
                    }
                ]
            },
        ],
    }
]


def test_pupilcard_extracts_only_real_homework():
    items = from_pupilcard(PUPILCARD_DATA)
    assert len(items) == 2
    assert items[0].subject == "מתמטיקה"
    assert items[0].homework == "תרגילים 1-10 בעמוד 45"
    assert items[0].teacher == "כהן רותי"
    # trimmed
    assert items[1].homework == "לקרוא פרק 4"


def test_pupilcard_tolerates_junk():
    assert from_pupilcard(None) == []
    assert from_pupilcard({}) == []
    assert from_pupilcard([{"hoursData": [{"scheduale": [{}]}]}]) == []
    assert from_pupilcard(["not a dict"]) == []


def test_extract_via_envelope():
    body = {"status": True, "data": PUPILCARD_DATA}
    assert len(extract(body, source="pupilcard")) == 2


def test_extract_rejects_unknown_source():
    with pytest.raises(ValueError):
        extract({"data": []}, source="nope")


# ----------------------------------------------------------------------
# dashboard shape (dashboard/GetHomeWork) - walker must find nested homework
# ----------------------------------------------------------------------
def test_dashboard_walker_finds_nested_homework():
    data = {
        "days": [
            {
                "date": "2026-09-03",
                "lessons": [
                    {"subject": "מדעים", "teacher": "בר דוד", "homework": "סיכום ניסוי"},
                    {"subject": "ספורט", "homework": "לא הוזן"},  # sentinel, skip
                    {"subject": "עברית", "homework": ""},
                ],
            }
        ]
    }
    items = from_dashboard(data)
    assert len(items) == 1
    assert items[0].subject == "מדעים"
    assert items[0].homework == "סיכום ניסוי"
    assert items[0].date == "2026-09-03"


def test_dashboard_handles_capitalised_keys():
    items = from_dashboard([{"Subject": "אמנות", "HomeWork": "ציור", "Date": "2026-09-03"}])
    assert len(items) == 1
    assert items[0].subject == "אמנות"


# ----------------------------------------------------------------------
# identity - the re-notify bug
# ----------------------------------------------------------------------
def test_identity_is_stable_for_same_assignment():
    a = HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")
    b = HomeworkItem(
        subject="מתמטיקה", homework="עמוד 45", date="2026-09-03T00:00:00", teacher="someone else"
    )
    # Same assignment, different date precision and teacher -> same identity,
    # so it is not re-notified.
    assert a.identity() == b.identity()


def test_identity_differs_for_different_homework():
    a = HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")
    b = HomeworkItem(subject="מתמטיקה", homework="עמוד 46", date="2026-09-03")
    assert a.identity() != b.identity()


# ----------------------------------------------------------------------
# state diffing
# ----------------------------------------------------------------------
def test_diff_reports_new_then_stays_quiet(tmp_path: Path):
    state = HomeworkState(tmp_path / "state.json")
    items = [HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")]

    new, _ = state.diff("דני", items)
    assert len(new) == 1, "first sighting must be reported"

    new, _ = state.diff("דני", items)
    assert new == [], "unchanged homework must not re-notify"


def test_diff_does_not_renotify_across_a_day_boundary(tmp_path: Path):
    """Regression: the old hash folded in a date stamped as 'today', so every
    item looked new each morning."""
    state = HomeworkState(tmp_path / "state.json")
    item = HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")

    assert len(state.diff("דני", [item])[0]) == 1
    # Same upstream assignment seen again on a later run.
    assert state.diff("דני", [item])[0] == []


def test_diff_detects_a_second_assignment(tmp_path: Path):
    state = HomeworkState(tmp_path / "state.json")
    first = HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")
    second = HomeworkItem(subject="אנגלית", homework="Unit 2", date="2026-09-03")

    state.diff("דני", [first])
    new, _ = state.diff("דני", [first, second])
    assert [i.subject for i in new] == ["אנגלית"]


def test_state_survives_a_save_load_cycle(tmp_path: Path):
    path = tmp_path / "state.json"
    item = HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")

    s1 = HomeworkState(path)
    s1.diff("דני", [item])
    s1.save()

    s2 = HomeworkState(path)
    assert s2.diff("דני", [item])[0] == [], "persisted state must suppress re-notify"


def test_state_recovers_from_corrupt_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = HomeworkState(path)
    assert state.diff("דני", [])[0] == []


def test_state_writes_readable_unicode(tmp_path: Path):
    path = tmp_path / "state.json"
    state = HomeworkState(path)
    state.diff("דני", [HomeworkItem(subject="מתמטיקה", homework="עמוד 45", date="2026-09-03")])
    state.save()
    raw = path.read_text(encoding="utf-8")
    assert "מתמטיקה" in raw, "Hebrew must not be escaped to \\uXXXX"
    json.loads(raw)


# ----------------------------------------------------------------------
# real dashboard/GetHomeWork shape, captured live on 2026-09-03
# ----------------------------------------------------------------------
DASHBOARD_LIVE = {
    "allowToViewThis": True,
    "dataTable": [
        {
            "lesson": "מדעי המחשב",
            "teacher": "כהן דוד",
            "hourNum": "1",
            "status": "Done",
            "homeworkData": "",  # no homework -> skip
            "lessonSubject": "נושא שיעור לדוגמה",
            "attachedFiles": [],
        },
        {
            "lesson": "שפה",
            "teacher": "מזרחי שרה",
            "hourNum": "1",
            "status": "noDataYet",
            "homeworkData": None,  # null -> skip
            "lessonSubject": None,
            "attachedFiles": [],
        },
        {
            "lesson": "חשבון",
            "teacher": "אברהם רותי",
            "hourNum": "3",
            "status": "Done",
            "homeworkData": "עמוד 12 תרגילים 1-5",
            "attachedFiles": [],
        },
    ],
}


def test_dashboard_live_shape_picks_up_homeworkdata():
    items = from_dashboard(DASHBOARD_LIVE, default_date="2026-09-03")
    assert len(items) == 1, "only the row with homeworkData text counts"
    it = items[0]
    assert it.subject == "חשבון"
    assert it.teacher == "אברהם רותי"
    assert it.homework == "עמוד 12 תרגילים 1-5"
    assert it.date == "2026-09-03", "rows carry no date, so today is stamped on"


def test_dashboard_stamps_today_when_no_default_given():
    from datetime import datetime

    items = from_dashboard(DASHBOARD_LIVE)
    assert items[0].date == datetime.now().strftime("%Y-%m-%d")


def test_dashboard_empty_when_no_homework_anywhere():
    payload = {"dataTable": [{"lesson": "שפה", "homeworkData": "", "teacher": "x"}]}
    assert from_dashboard(payload) == []

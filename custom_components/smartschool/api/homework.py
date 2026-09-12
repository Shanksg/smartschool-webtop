"""Homework extraction.

Two response shapes, both verified against the live API on 2026-09-03:

  * PupilCard/GetPupilLessonsAndHomework - a dated multi-day (week) view.
    Primary source. Its February 'view is blocked' turned out to be a
    dead-token symptom, not a permanent block.
  * dashboard/GetHomeWork - today only, homework text in `homeworkData`, no
    per-row date. Fallback.

Both extractors are pure functions over already-parsed JSON, so they are
testable offline with fixtures - unlike the old parse_homework_from_text(),
which needed a live browser and matched against a hardcoded list of 16 Hebrew
subject names.
"""

import os
from datetime import datetime
from typing import Any, Dict, List

import logging

_LOGGER = logging.getLogger(__name__)

from .models import HomeworkItem

# Teachers routinely type "there is none" into the homework field rather than
# leaving it empty. Those are not assignments and must not be pushed.
# Override with NO_HOMEWORK_VALUES (comma-separated) if a teacher invents a
# new way of saying nothing.
DEFAULT_NO_HOMEWORK_VALUES = (
    "אין",
    "אין שיעורי בית",
    "אין שעורי בית",
    "לא הוזן",
    "ללא",
    "ללא שיעורי בית",
    "none",
    "n/a",
    "-",
    "--",
    ".",
)


def _no_homework_values() -> frozenset:
    raw = os.getenv("NO_HOMEWORK_VALUES", "")
    if raw.strip():
        return frozenset(v.strip().casefold() for v in raw.split(",") if v.strip())
    return frozenset(v.casefold() for v in DEFAULT_NO_HOMEWORK_VALUES)


def is_no_homework(text: str) -> bool:
    """True when the text is a teacher's way of saying 'nothing assigned'."""
    if not text:
        return True
    normalised = " ".join(text.split()).strip().strip(".!:;־-").casefold()
    return not normalised or normalised in _no_homework_values()


def _clean(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def from_pupilcard(data: Any) -> List[HomeworkItem]:
    """Extract from PupilCard/GetPupilLessonsAndHomework.

    Shape: [ {date, hoursData: [ {scheduale: [ {homeWork, subject_name, ...} ]} ]} ]
    ('scheduale' is the server's spelling.)
    """
    items: List[HomeworkItem] = []
    if not isinstance(data, list):
        return items

    for day in data:
        if not isinstance(day, dict):
            continue
        date = _clean(day.get("date"))
        for hour in day.get("hoursData") or []:
            if not isinstance(hour, dict):
                continue
            for entry in hour.get("scheduale") or []:
                if not isinstance(entry, dict):
                    continue
                text = _clean(entry.get("homeWork"))
                if is_no_homework(text):
                    continue
                items.append(
                    HomeworkItem(
                        date=date,
                        subject=_clean(entry.get("subject_name")) or "Unknown",
                        teacher=_clean(entry.get("teacher")) or "Unknown",
                        homework=text,
                        description=_clean(entry.get("descClass")),
                    )
                )
    return items


def from_dashboard(data: Any, *, default_date: str = "") -> List[HomeworkItem]:
    """Extract from dashboard/GetHomeWork.

    Verified against the live API: data.dataTable[] holds one row per lesson
    for *today*, with the homework text in `homeworkData`. Rows carry no date,
    so `default_date` (today) is stamped on.

    Kept as a tolerant walker rather than a strict dataTable reader because
    this endpoint is the fallback, and its shape has already changed once.
    """
    items: List[HomeworkItem] = []
    default_date = default_date or datetime.now().strftime("%Y-%m-%d")

    # Live GetHomeWork rows look like:
    #   {"lesson": "מדעי המחשב", "teacher": "...", "homeworkData": "",
    #    "lessonSubject": "...", "hourNum": "1", "status": "Done"}
    # There is no per-row date: the endpoint returns *today* only.
    hw_keys = ("homeworkData", "homeWork", "homework", "HomeWork", "Homework")
    subj_keys = ("lesson", "subject_name", "subjectName", "subject", "Subject", "Lesson")
    teacher_keys = ("teacher", "Teacher", "teacherName", "TeacherName")
    date_keys = ("date", "Date", "lessonDate", "LessonDate", "dueDate", "DueDate")

    def pick(d: Dict[str, Any], keys) -> str:
        for k in keys:
            v = _clean(d.get(k))
            if v:
                return v
        return ""

    def walk(node: Any, inherited_date: str = "", inherited_subject: str = "") -> None:
        """Recurse, carrying context down from ancestors.

        The date usually sits on the enclosing day object while the homework
        text sits on a nested lesson, so a homework item has to inherit the
        nearest date above it rather than only reading its own keys.
        """
        if isinstance(node, dict):
            date = pick(node, date_keys) or inherited_date
            subject = pick(node, subj_keys) or inherited_subject

            text = pick(node, hw_keys)
            if not is_no_homework(text):
                items.append(
                    HomeworkItem(
                        date=date,
                        subject=subject or "Unknown",
                        teacher=pick(node, teacher_keys) or "Unknown",
                        homework=text,
                        description=pick(node, ("descClass", "description", "Description")),
                    )
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, date, subject)
        elif isinstance(node, list):
            for entry in node:
                walk(entry, inherited_date, inherited_subject)

    walk(data, default_date)
    if items:
        _LOGGER.debug(f"dashboard/GetHomeWork yielded {len(items)} homework item(s)")
    return items


def extract(body: Dict[str, Any], *, source: str) -> List[HomeworkItem]:
    """Extract homework from a full API envelope for the named source."""
    data = body.get("data") if isinstance(body, dict) else None
    if source == "pupilcard":
        return from_pupilcard(data)
    if source == "dashboard":
        return from_dashboard(data)
    raise ValueError(f"Unknown homework source: {source!r}")

"""Data models.

Student replaces the hardcoded student_params dict that used to be frozen in
token_cache.json (studyYear 2026 / periodID 7949 / 'מחצית א`'). Those values go
stale every school year; InitDashboard hands them to us at runtime instead.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Student:
    """A student as returned by dashboard/InitDashboard.

    `student_id` is the server's *encrypted* id (base64-ish blob) and is what
    every downstream endpoint wants as `id`.
    """

    student_id: str
    name: str = ""
    class_code: Optional[int] = None
    class_number: Optional[int] = None
    study_year: Optional[int] = None
    school_name: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "Student":
        """Build from an InitDashboard entry, tolerating key-casing drift."""

        def pick(*names):
            for n in names:
                if data.get(n) not in (None, ""):
                    return data[n]
            return None

        first = pick("firstName", "FirstName") or ""
        last = pick("lastName", "LastName") or ""
        name = pick("studentName", "name", "Name") or f"{first} {last}".strip()

        return cls(
            student_id=pick("id", "Id", "studentId", "StudentId") or "",
            name=name,
            class_code=pick("classCode", "ClassCode"),
            # InitDashboard's childrens[] uses `classNum`; GetHomeWork wants it
            # back as `ClassNumber`.
            class_number=pick("classNum", "classNumber", "ClassNumber", "ClassNum"),
            study_year=pick("studyYear", "StudyYear"),
            school_name=pick("schoolName", "SchoolName") or "",
            raw=data,
        )


@dataclass
class HomeworkItem:
    """One homework assignment.

    `identity()` deliberately excludes any 'when we saw it' notion. The old
    hash_homework() folded `date` into the hash while the scraper stamped
    date=today, so every item looked new every day.
    """

    subject: str
    homework: str
    date: str = ""
    teacher: str = ""
    description: str = ""

    def identity(self) -> str:
        """Stable key used to detect genuinely new homework."""
        import hashlib

        parts = [
            (self.subject or "").strip(),
            (self.homework or "").strip(),
            (self.date or "").strip()[:10],
        ]
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "subject": self.subject,
            "teacher": self.teacher,
            "homework": self.homework,
            "description": self.description,
        }


@dataclass
class TokenState:
    """A webToken plus what we know about its lifetime.

    `pasted_token` records which token.txt value produced this state. Without
    it there is no way to tell a *new* hand-pasted token from the original one
    the cache has since rotated past, and the store would keep reverting to the
    stale file contents.
    """

    token: str
    obtained_at: str = ""
    expires_at: Optional[str] = None
    rotated_count: int = 0
    pasted_token: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
            "rotated_count": self.rotated_count,
            "pasted_token": self.pasted_token,
        }

    def predicted_expiry(self, ttl_hours: float):
        """Best-effort expiry, as a datetime, or None if unknown.

        Measured on 2026-09-03: the webToken cookie carries a 9-hour lifetime
        (issued 15:46:06, Expires 00:46:06). There is no Set-Cookie on any API
        response, so the server never tells us directly - we infer from when
        the token was ingested.

        This is an UPPER BOUND: a token may already have been alive for a while
        in the browser before being pasted here, so the real expiry can be
        earlier. A 401 remains the only authoritative signal.
        """
        from datetime import datetime, timedelta

        if self.expires_at:
            try:
                from email.utils import parsedate_to_datetime

                return parsedate_to_datetime(self.expires_at)
            except (TypeError, ValueError):
                try:
                    return datetime.fromisoformat(self.expires_at)
                except ValueError:
                    pass
        if not self.obtained_at:
            return None
        try:
            return datetime.fromisoformat(self.obtained_at) + timedelta(hours=ttl_hours)
        except ValueError:
            return None

    def minutes_remaining(self, ttl_hours: float):
        """Minutes until the predicted expiry, or None if unknown."""
        from datetime import datetime

        exp = self.predicted_expiry(ttl_hours)
        if exp is None:
            return None
        now = datetime.now(exp.tzinfo) if exp.tzinfo else datetime.now()
        return (exp - now).total_seconds() / 60


@dataclass
class RotationResult:
    """Outcome of one CheckBackgroundToken call.

    `rotated` is the finding that decides the whole architecture: if the server
    hands back a fresh webToken, one manual login lasts indefinitely.
    """

    ok: bool
    rotated: bool
    new_token: Optional[str] = None
    expires: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    set_cookie: str = ""


@dataclass
class Message:
    """A message in the school inbox (messageBox/GetMessagesInbox).

    Field names on the wire are misleading: `student_F_name` / `student_L_name`
    hold the *sender's* first and last name, not the student's. Verified
    against live data - a message from teacher "כהן דוד" arrives as
    student_F_name="דוד", student_L_name="כהן".
    """

    subject: str
    sender: str = ""
    sent_at: str = ""
    has_read: bool = False
    has_files: bool = False
    type_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "Message":
        def pick(*names):
            for n in names:
                v = data.get(n)
                if v not in (None, ""):
                    return v
            return None

        first = pick("student_F_name", "senderFirstName") or ""
        last = pick("student_L_name", "senderLastName") or ""
        # Hebrew convention puts the family name first when addressing staff.
        sender = f"{last} {first}".strip() or (pick("fromTitle", "userTitle") or "")

        def as_bool(v):
            # The API uses 0/1 ints here, not booleans.
            if isinstance(v, bool):
                return v
            try:
                return bool(int(v))
            except (TypeError, ValueError):
                return False

        return cls(
            subject=(pick("subject", "Subject") or "").strip(),
            sender=sender,
            sent_at=str(pick("sendingDate", "SendingDate") or ""),
            has_read=as_bool(data.get("hasRead")),
            has_files=as_bool(data.get("filesWereAttached")),
            type_id=pick("typeID", "TypeID"),
            raw=data,
        )

    def identity(self) -> str:
        """Stable key for "have we seen this message?".

        Deliberately excludes has_read: marking a message read in the browser
        must not make it look like a new message.
        """
        import hashlib

        parts = [self.subject, self.sender, (self.sent_at or "")[:19]]
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "sent_at": self.sent_at,
            "has_read": self.has_read,
            "has_files": self.has_files,
        }

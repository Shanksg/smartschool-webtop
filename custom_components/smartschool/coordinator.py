"""Data update coordinator for the SmartSchool (Webtop) integration.

The vendored client is synchronous (requests), so every network call runs in
the executor. Auth is self-renewing: a fresh webToken is minted from the
bioLogin credential (no password, no captcha) whenever the current one is
missing or rejected.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.bio import BioCredentials
from .api.client import WebtopClient
from .api.exceptions import ApiError, RequestFailed, TokenExpired
from .api.homework import extract
from .api.models import HomeworkItem, Message, Student
from .const import (
    CONF_BIO_LOGIN,
    CONF_DEVICE_ID,
    CONF_IS_MOBILE,
    CONF_SELECTED_USER,
    CONF_UNIQUE_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=30)
# PupilCard returns a dated multi-day window (preferred); dashboard is today
# only, used as a fallback.
_HOMEWORK_SOURCES = ("pupilcard", "dashboard")


class SmartSchoolData:
    """Snapshot returned by one update: per-student homework + inbox."""

    def __init__(
        self,
        students: list[Student],
        homework: dict[str, list[HomeworkItem]],
        messages: list[Message],
    ) -> None:
        self.students = students
        self.homework = homework  # keyed by student_id
        self.messages = messages


class SmartSchoolCoordinator(DataUpdateCoordinator[SmartSchoolData]):
    """Polls SmartSchool, renewing the token via bioLogin as needed."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self._creds = BioCredentials(
            bio_login=entry.data[CONF_BIO_LOGIN],
            unique_id=entry.data.get(CONF_UNIQUE_ID, ""),
            selected_user=entry.data.get(CONF_SELECTED_USER, ""),
            device_id=entry.data.get(CONF_DEVICE_ID, ""),
            is_mobile=entry.data.get(CONF_IS_MOBILE, True),
        )
        self._client: WebtopClient | None = None

    async def _async_update_data(self) -> SmartSchoolData:
        """Fetch homework and messages (all blocking work in the executor)."""
        return await self.hass.async_add_executor_job(self._fetch)

    # ------------------------------------------------------------------
    # everything below runs in a worker thread
    # ------------------------------------------------------------------
    def _mint_client(self) -> WebtopClient:
        """Mint a fresh webToken from the bioLogin credential.

        loginByBio must be sent from a clean session (no stale webToken cookie),
        or the server returns an immediately-invalid token.
        """
        client = WebtopClient("")
        try:
            token = client.login_by_bio(
                bio_login=self._creds.bio_login,
                device_id=self._creds.device_id,
                selected_user=self._creds.selected_user,
                unique_id=self._creds.unique_id,
                is_mobile=self._creds.is_mobile,
                mode=self._creds.mode,
            )
        except (ApiError, RequestFailed, TokenExpired) as err:
            raise ConfigEntryAuthFailed(f"bioLogin renewal failed: {err}") from err
        if not token:
            raise ConfigEntryAuthFailed("bioLogin credential was rejected")
        _LOGGER.debug("Minted a fresh webToken via bioLogin")
        return client

    def _ensure_client(self) -> WebtopClient:
        if self._client is None or not self._client.check_token():
            self._client = self._mint_client()
        return self._client

    def _fetch(self) -> SmartSchoolData:
        client = self._ensure_client()
        try:
            students = client.get_students()
            homework = {s.student_id: self._fetch_homework(client, s) for s in students}
            messages = client.get_messages_inbox()
        except TokenExpired:
            # Token died mid-cycle - mint once and retry the whole pass.
            self._client = self._mint_client()
            client = self._client
            students = client.get_students()
            homework = {s.student_id: self._fetch_homework(client, s) for s in students}
            messages = client.get_messages_inbox()
        except (ApiError, RequestFailed) as err:
            raise UpdateFailed(str(err)) from err

        return SmartSchoolData(students=students, homework=homework, messages=messages)

    def _fetch_homework(self, client: WebtopClient, student: Student) -> list[HomeworkItem]:
        """Try each homework source; a genuine outage propagates as UpdateFailed."""
        last_error: Exception | None = None
        for source in _HOMEWORK_SOURCES:
            try:
                if source == "dashboard":
                    body = client.get_homework(student)
                else:
                    params = self._pupilcard_params(student)
                    if not params:
                        continue
                    body = client.get_homework_pupilcard(params)
            except TokenExpired:
                raise
            except (ApiError, RequestFailed) as err:
                last_error = err
                continue
            return extract(body, source=source)

        if last_error:
            raise UpdateFailed(f"all homework sources failed: {last_error}")
        return []

    @staticmethod
    def _pupilcard_params(student: Student) -> dict[str, Any] | None:
        if student.class_code is None or not student.student_id:
            return None
        return {
            "classCode": student.class_code,
            "moduleID": 11,
            "studentID": student.student_id,
            "studentName": student.name,
            "studyYear": student.study_year,
            "viewType": 0,
            "weekIndex": 0,
        }

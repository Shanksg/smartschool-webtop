"""Synchronous Webtop (SmartSchool) API client.

The endpoint map was derived from the SmartSchool web client, built on
`requests` so it drops straight into the `schedule`-based daemon without an
async bridge. Two deliberate choices:

  * No password login(). SmartSchool gates LoginByUserNameAndPassword behind a
    reCAPTCHA checkbox, so username/password cannot mint a token unattended.
    We take a webToken (pasted, or minted via loginByBio) and work from there.
  * refresh_token() calls dashboard/CheckBackgroundToken - the endpoint the
    real SPA polls every 30 minutes - and reports whether the server handed
    back a rotated cookie.
"""

import json
import re
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from .exceptions import ApiError, RequestFailed, TokenExpired
from .models import Message, RotationResult, Student

DEFAULT_BASE_URL = "https://webtopserver.smartschool.co.il"
WEB_ORIGIN = "https://webtop.smartschool.co.il"


class WebtopClient:
    """Cookie-authenticated Webtop client.

    Auth is a single cookie, `webToken`. Every endpoint below is a POST and
    needs nothing else - notably no captcha, which is why a pasted token gets
    us full read access even though login is blocked.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        verify_tls: bool = True,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        # The old code set verify=False everywhere, which silently disabled TLS
        # checking against a site that has a perfectly good certificate.
        self._session.verify = verify_tls
        self._session.headers.update(
            {
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json, text/plain, */*",
                "language": "he",
                "Origin": WEB_ORIGIN,
                "Referer": WEB_ORIGIN + "/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
            }
        )
        self.set_token(token)

    # ------------------------------------------------------------------
    # token plumbing
    # ------------------------------------------------------------------
    def set_token(self, token: str) -> None:
        """Install a webToken, URL-decoding it if it was copied encoded."""
        from urllib.parse import unquote

        if token and "%" in token:
            token = unquote(token)
            logger.debug("URL-decoded the supplied token")
        self._token = (token or "").strip()
        self._session.cookies.set("webToken", self._token, domain=".smartschool.co.il")

    @property
    def token(self) -> str:
        return self._token

    def _current_cookie_token(self) -> Optional[str]:
        """Read webToken back out of the jar (the server may have rotated it)."""
        for c in self._session.cookies:
            if c.name == "webToken" and c.value:
                return c.value
        return None

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _post(self, path: str, payload: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.post(url, json=payload or {}, timeout=self._timeout)
        except requests.exceptions.Timeout as e:
            raise RequestFailed(f"{path} timed out after {self._timeout}s") from e
        except requests.exceptions.RequestException as e:
            raise RequestFailed(f"{path} request error: {e}") from e

        # 401 is how the live server now reports a dead token, and the body is
        # not JSON. The previous code called raise_for_status() and let the
        # resulting HTTPError fall into a bare `except Exception`, so an
        # expired token surfaced as a generic "Failed to get homework".
        if resp.status_code == 401:
            raise TokenExpired(f"{path} returned HTTP 401 - webToken is expired or revoked")
        if resp.status_code >= 400:
            raise RequestFailed(f"{path} failed with HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def _post_json(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST and unwrap the standard {status,data,errorDescription} envelope."""
        resp = self._post(path, payload)
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise RequestFailed(f"{path} did not return JSON: {resp.text[:200]}") from e

        if not isinstance(body, dict):
            raise RequestFailed(f"{path} returned unexpected payload type {type(body).__name__}")

        if body.get("status") is not True:
            desc = body.get("errorDescription")
            # 'view is blocked' is the wall this project hit in February; keep it
            # legible rather than collapsing it into a generic failure.
            raise ApiError(
                f"{path} returned status=false (errorDescription={desc!r})",
                error_description=desc,
                error_id=body.get("errorId"),
                payload=body,
            )
        return body

    # ------------------------------------------------------------------
    # token lifecycle
    # ------------------------------------------------------------------
    def refresh_token(self) -> RotationResult:
        """Poll dashboard/CheckBackgroundToken and report any rotation.

        The SPA calls this every ~30 minutes (checkLastToken() compares against
        a 1_800_000 ms window). If the server answers with a fresh webToken in
        Set-Cookie, the session slides forward and we never need another login.
        """
        before = self._current_cookie_token()
        try:
            resp = self._post("server/api/dashboard/CheckBackgroundToken", {})
        except TokenExpired:
            raise
        set_cookie = resp.headers.get("Set-Cookie", "") or ""

        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError):
            body = {}

        after = self._current_cookie_token()
        rotated = bool(after and after != before)
        if rotated:
            self._token = after
            logger.info("Server rotated the webToken (sliding session confirmed)")

        # Note: do not split Set-Cookie on ',' - HTTP dates contain one
        # ("expires=Fri, 04 Sep 2026 ..."). Match the webToken attribute
        # directly instead, taking everything up to the next ';'.
        expires = None
        match = re.search(
            r"webToken=[^;]*;[^,]*?expires=([^;]+)", set_cookie, re.IGNORECASE
        )
        if match:
            expires = match.group(1).strip()

        ok = body.get("status") is True or bool(body.get("data"))
        return RotationResult(
            ok=ok,
            rotated=rotated,
            new_token=after if rotated else None,
            expires=expires,
            raw=body if isinstance(body, dict) else {},
            set_cookie=set_cookie,
        )

    def login_by_bio(
        self,
        *,
        bio_login: str,
        device_id: str,
        selected_user: str = "",
        unique_id: str = "",
        is_mobile: bool = False,
        mode: str = "2",
    ) -> Optional[str]:
        """POST user/loginByBio - mint a fresh webToken with no password or captcha.

        This is the flow the real SPA uses to restore a session on a return
        visit. Extracted from the site bundle:

            postData("api/user/loginByBio", true, {
              id: <bioLogin>, param1: "1"|"2", param2: deviceId,
              param3: localStorage.selectedUser, param4: uniqueId,
              param5: isMobile })

        `mode` is param1: "2" is the remember-me path the bundle calls
        loadRememberMe(); "1" is the biometric-prompt path. The credentials
        live in IndexedDB (database "WebTop", store "storage") under the keys
        bioLogin / deviceId / uniqueId, mirrored to a `bioLogin` cookie with a
        365-day lifetime (setItemShortTime(..., 525600)).

        NOT USABLE on the account this was developed against: the login page
        renders no remember-me checkbox, and the browser reported
        cookieKeys={"uniqueId":true,"deviceId":false,"bioLogin":false,
        "SavedUser":false} with an empty IndexedDB - so there is no credential
        to replay. Kept because the endpoint is real and other institutions
        may provision it; the monitor never calls it.

        Returns the new token, or None when the server declines. On success the
        token is installed on this client.
        """
        body = self._post_json(
            "server/api/user/loginByBio",
            {
                "id": bio_login,
                "param1": mode,
                "param2": device_id,
                "param3": selected_user or "",
                "param4": unique_id or "",
                "param5": str(bool(is_mobile)).lower(),
            },
        )
        data = body.get("data") or {}
        if not isinstance(data, dict):
            logger.warning(f"loginByBio returned {type(data).__name__}, expected an object")
            return None

        token = data.get("token")
        if not token:
            logger.warning(f"loginByBio succeeded but returned no token; keys={list(data)}")
            return None

        self.set_token(token)
        logger.info("loginByBio minted a fresh webToken")
        return token

    def check_token(self) -> bool:
        """Lightweight liveness probe via dashboard/CheckToken.

        Replaces the old validate_token(), which unconditionally returned True
        and therefore never let an expired token fall through to a refresh.
        """
        try:
            self._post_json("server/api/dashboard/CheckToken", {})
            return True
        except TokenExpired:
            return False
        except ApiError as e:
            logger.warning(f"CheckToken returned status=false: {e.error_description!r}")
            return False

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def init_dashboard(self) -> Dict[str, Any]:
        """POST dashboard/InitDashboard - the source of runtime student params."""
        return self._post_json("server/api/dashboard/InitDashboard", {})

    def get_students(self) -> List[Student]:
        """Discover students and their class params dynamically.

        This is what removes the frozen studyYear/periodID from the old
        token_cache.json: whatever the school year is, the server tells us.
        """
        body = self.init_dashboard()
        payload = body.get("data")
        entries: List[Dict[str, Any]] = []

        if isinstance(payload, list):
            entries = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            # Newer responses wrap the list; look for the first list of dicts
            # that smells like students rather than guessing a single key.
            # The live server uses 'childrens' (sic).
            for key in ("childrens", "students", "Students", "pupils", "Pupils", "children"):
                if isinstance(payload.get(key), list):
                    entries = [e for e in payload[key] if isinstance(e, dict)]
                    break
            if not entries:
                for value in payload.values():
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        if any(k in value[0] for k in ("id", "Id", "classCode", "ClassCode")):
                            entries = value
                            break
                else:
                    logger.warning(
                        f"InitDashboard payload had no recognisable student list; keys={list(payload.keys())}"
                    )

        return [Student.from_api(e) for e in entries]

    def get_linked_students(self) -> List[Dict[str, Any]]:
        """POST user/GetMultipleUsersForUser - siblings on a parent account."""
        body = self._post_json("server/api/user/GetMultipleUsersForUser", {})
        data = body.get("data") or []
        return data if isinstance(data, list) else []

    def switch_student(self, student_id: str, saved_user: str = "") -> Dict[str, Any]:
        """POST user/ChangeUser - move the session to another linked student.

        This endpoint does return a freshly minted token, so it looks like a
        way to rotate the session without logging in. Do NOT rely on it:
        tested on 2026-09-03 it succeeded once and then refused every
        subsequent call with status=false and every error field null - the same
        signature the captcha-blocked login returns - so it is rate limited or
        single-shot. The monitor never calls this automatically; it would risk
        disturbing a working session for an unreliable gain.
        """
        body = self._post_json(
            "server/api/user/ChangeUser",
            {
                "StudentId": student_id,
                "institutionCode": None,
                "savedUser": saved_user,
                "userType": None,
            },
        )
        data = body.get("data") or {}
        if isinstance(data, dict) and data.get("token"):
            self.set_token(data["token"])
            logger.info("Session switched to another student; token updated")
        return data

    def get_homework(self, student: Student) -> Dict[str, Any]:
        """POST dashboard/GetHomeWork - the dashboard homework endpoint."""
        return self._post_json(
            "server/api/dashboard/GetHomeWork",
            {
                "id": student.student_id,
                "ClassCode": student.class_code,
                "ClassNumber": student.class_number,
            },
        )

    def get_homework_pupilcard(self, student_params: Dict[str, Any]) -> Dict[str, Any]:
        """POST PupilCard/GetPupilLessonsAndHomework - this repo's original endpoint.

        Kept for the Phase 3 A/B test: in February this returned
        'view is blocked' while the dashboard endpoint was never tried.
        """
        return self._post_json(
            "server/api/PupilCard/GetPupilLessonsAndHomework", student_params
        )

    def get_messages_inbox(
        self,
        *,
        page_id: int = 1,
        label_id: int = 0,
        has_read: Optional[bool] = None,
        search_query: str = "",
    ) -> List[Message]:
        """POST messageBox/GetMessagesInbox - school messages.

        `has_read=None` returns everything, which is what the monitor wants:
        it tracks what it has *seen*, independently of what has been read in
        the browser.
        """
        body = self._post_json(
            "server/api/messageBox/GetMessagesInbox",
            {
                "PageId": page_id,
                "LabelId": label_id,
                "HasRead": has_read,
                "SearchQuery": search_query,
            },
        )
        data = body.get("data") or []
        if not isinstance(data, list):
            logger.warning(
                f"GetMessagesInbox returned {type(data).__name__}, expected a list"
            )
            return []
        return [Message.from_api(m) for m in data if isinstance(m, dict)]

    def get_discipline_events(self, student: Student) -> Dict[str, Any]:
        """POST dashboard/GetPupilDiciplineEvents (server's spelling)."""
        return self._post_json(
            "server/api/dashboard/GetPupilDiciplineEvents",
            {"id": student.student_id, "ClassCode": student.class_code},
        )

    def get_unread_notifications(self) -> Dict[str, Any]:
        """POST Menu/GetPreviewUnreadNotifications."""
        return self._post_json("server/api/Menu/GetPreviewUnreadNotifications", {})

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as e:  # pragma: no cover - closing should never break a run
            logger.debug(f"Error closing HTTP session: {e}")

    def __enter__(self) -> "WebtopClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

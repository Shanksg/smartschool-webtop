"""The monitor daemon.

Flow, once a token has been pasted:

    every TOKEN_ROTATE_MINUTES : CheckBackgroundToken -> persist any rotation
    at each SCHEDULES time     : discover students -> fetch homework ->
                                 diff -> MQTT + notify

There is no login step. SmartSchool gates login behind a reCAPTCHA checkbox,
so on TokenExpired the monitor notifies the user once and keeps probing until a
fresh token appears in token.txt.
"""

import sys
import time
from typing import List, Optional

import schedule
from loguru import logger

from .client import WebtopClient
from .config import Config, Paths
from .exceptions import ApiError, RequestFailed, SmartSchoolError, TokenExpired, TokenMissing
from .homework import extract
from .models import HomeworkItem, Student
from .notifiers import Notifier
from .bio import BioCredentials
from .session import TokenStore
from .state import SeenState

# Order verified against the live API on 2026-09-03:
#   pupilcard -> returns a dated multi-day (week) view; this is what the
#                monitor wants, and it works fine with a valid token. The
#                February 'view is blocked' was a dead-token symptom.
#   dashboard -> works, but returns *today* only with no per-row dates, so it
#                is the fallback.
HOMEWORK_SOURCES = ("pupilcard", "dashboard")

# The inbox is account-level, not per-student, so it gets one state bucket.
INBOX_BUCKET = "__inbox__"


def setup_logging(log_dir) -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        f"{log_dir}/smartschool-monitor.log",
        rotation="20 MB",
        retention="7 days",
        level="DEBUG",
        encoding="utf-8",
    )


class Monitor:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.config.paths.ensure()
        self.store = TokenStore(self.config.paths.config_dir)
        self.state = SeenState(self.config.paths.state_file, label="homework")
        self.messages_state = SeenState(
            self.config.paths.messages_state_file, label="message"
        )
        self.notifier = Notifier(self.config.notifiers, self.config.mqtt)
        self.token_state = None
        self.client: Optional[WebtopClient] = None
        self._expiry_notified = False
        self._expiring_notified = False
        # Display names seen via runtime discovery, so expiry state can be
        # published for MQTT-only setups where config.yaml lists no students.
        self._known_names: set = set()

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """Load the current token and build a client. False if none usable.

        When no token has been pasted or cached, a configured bioLogin
        credential can still mint the very first token - this is what makes the
        credentials-only setup in EXTRACT_BIO.md work without any token.txt.
        """
        try:
            self.token_state = self.store.load()
        except TokenMissing as e:
            if self.renew_via_bio():
                logger.info("No pasted token; minted the initial token via bioLogin")
                return True
            logger.error(str(e))
            return False

        self.client = WebtopClient(
            self.token_state.token,
            timeout=20.0,
            verify_tls=self.config.verify_tls,
        )
        self._expiring_notified = False

        left = self.token_state.minutes_remaining(self.config.token_ttl_hours)
        if left is not None:
            logger.info(
                f"Token predicted to expire in ~{left/60:.1f}h "
                f"(assuming a {self.config.token_ttl_hours:g}h lifetime)"
            )
        return True

    def renew_via_bio(self) -> bool:
        """Mint a fresh webToken from the stored bioLogin credential.

        This is the automated-renewal path: user/loginByBio needs no password
        and no captcha, and the bioLogin credential lasts ~1 year, so a single
        browser login keeps the monitor running unattended. Returns True and
        installs the new token on success; False if no credential is configured
        or the server declines (credential expired/revoked -> manual paste).
        """
        creds = BioCredentials.load(self.config.paths.config_dir)
        if not creds:
            return False

        # loginByBio does not need a live token; build a client if we lack one.
        client = self.client or WebtopClient("", verify_tls=self.config.verify_tls)
        try:
            new_token = client.login_by_bio(
                bio_login=creds.bio_login,
                device_id=creds.device_id,
                selected_user=creds.selected_user,
                unique_id=creds.unique_id,
                is_mobile=creds.is_mobile,
                mode=creds.mode,
            )
        except (TokenExpired, ApiError, RequestFailed) as e:
            logger.warning(f"bioLogin renewal failed: {e}")
            return False

        if not new_token:
            return False

        logger.info("Renewed the webToken automatically via bioLogin")
        self.client = client
        self.token_state = self.store.save_renewed(new_token)
        self._expiry_notified = False
        self._expiring_notified = False
        return True

    def _handle_expired(self) -> None:
        """On a dead token, try automated bioLogin renewal first; only if that
        fails fall back to notifying for a manual paste.

        The client is dropped so the next cycle re-reads token.txt, which is
        what lets a hand-pasted replacement be picked up with no restart.
        """
        if self.renew_via_bio():
            return  # renewed; nothing to notify

        if self.client:
            self.client.close()
        self.client = None
        self.token_state = None

        if not self._expiry_notified:
            self.notifier.notify_token_expired(str(self.config.paths.token_file))
            self._expiry_notified = True

        # Publish the expired status for every student we know about - the ones
        # in config.yaml AND the ones discovered at runtime - so an MQTT-only
        # setup (no config.yaml) still flips token_status away from "ok".
        names = {s.get("name") for s in self.config.students if s.get("name")}
        names |= self._known_names
        for name in names:
            self.notifier.publish_discovery(name)
            # Update only token status; do NOT clobber the retained homework
            # with an empty set that no fetch established.
            self.notifier.publish_token_status(name, "expired")

    # ------------------------------------------------------------------
    def token_minutes_left(self) -> Optional[float]:
        if not self.token_state:
            return None
        return self.token_state.minutes_remaining(self.config.token_ttl_hours)

    def warn_if_expiring(self) -> None:
        """Handle an approaching expiry.

        Prefer automated bioLogin renewal - if it succeeds there is nothing to
        warn about. Only when renewal is unavailable or fails do we notify for a
        manual paste. Prediction is an upper bound (see
        TokenState.predicted_expiry), so the 401 path remains the backstop.
        """
        left = self.token_minutes_left()
        if left is None:
            return
        if left > self.config.token_warn_minutes:
            return

        # Expiry is near. Try to renew silently before bothering the user.
        if self.renew_via_bio():
            logger.info("Renewed via bioLogin ahead of expiry")
            return

        if not self._expiring_notified and left > 0:
            self.notifier.notify_token_expiring(
                str(self.config.paths.token_file), left
            )
            self._expiring_notified = True

    # ------------------------------------------------------------------
    def pick_up_new_paste(self) -> None:
        """Switch to a token pasted while the current one is still valid.

        connect() only reads the store when there is no client, so a token
        pasted before the old one 401s would otherwise be ignored until it
        fails. Reload the store each rotation and swap the client when the
        active token changed - this is the documented "picked up within one
        cycle" behaviour.
        """
        if not self.token_state:
            return
        try:
            latest = self.store.load()
        except TokenMissing:
            return
        if latest.token != self.token_state.token:
            logger.info("Picked up a newly pasted token from token.txt")
            self.token_state = latest
            if self.client:
                self.client.close()
            self.client = WebtopClient(
                latest.token, timeout=20.0, verify_tls=self.config.verify_tls
            )
            self._expiry_notified = False
            self._expiring_notified = False

    def rotate_token(self) -> bool:
        """Poll CheckBackgroundToken and persist a rotated token.

        Returns False when the token is dead.
        """
        if not self.client and not self.connect():
            return False

        self.pick_up_new_paste()

        try:
            result = self.client.refresh_token()
        except TokenExpired:
            logger.error("Token rejected during rotation check")
            self._handle_expired()
            return False
        except (RequestFailed, SmartSchoolError) as e:
            logger.warning(f"Rotation check failed (will retry): {e}")
            return True  # transport hiccup, not a dead token

        if result.rotated and result.new_token:
            self.token_state = self.store.record_rotation(
                self.token_state, result.new_token, result.expires
            )
        else:
            logger.debug(
                f"No rotation this cycle (ok={result.ok}); token still accepted"
            )
        self._expiry_notified = False
        self.warn_if_expiring()
        return True

    # ------------------------------------------------------------------
    def fetch_homework(self, student: Student) -> List[HomeworkItem]:
        """Try each homework source until one yields a usable response."""
        last_error = None
        for source in HOMEWORK_SOURCES:
            try:
                if source == "dashboard":
                    body = self.client.get_homework(student)
                else:
                    params = self._pupilcard_params(student)
                    if not params:
                        continue
                    body = self.client.get_homework_pupilcard(params)
            except TokenExpired:
                raise
            except ApiError as e:
                logger.warning(
                    f"{source} endpoint returned status=false "
                    f"(errorDescription={e.error_description!r})"
                )
                last_error = e
                continue
            except RequestFailed as e:
                logger.warning(f"{source} endpoint failed: {e}")
                last_error = e
                continue

            items = extract(body, source=source)
            logger.info(f"{source} endpoint returned {len(items)} homework item(s)")
            if items:
                return items
            # A valid but empty response is a real answer, not a failure.
            logger.info(f"{source} endpoint responded with no homework")
            return items

        # Every source errored. Do NOT return [] - the caller would treat an
        # outage as "no homework", overwrite the student's seen state with
        # nothing, and re-notify every assignment once the endpoint recovers.
        if last_error:
            raise last_error
        return []

    def _pupilcard_params(self, student: Student) -> Optional[dict]:
        """Build PupilCard params from runtime discovery.

        Runtime data always wins over anything in config.yaml. This matters:
        a stale student_params block (old classCode, old encrypted studentID,
        last year's studyYear) makes the endpoint answer 'view is blocked',
        which reads like a permission problem but is really a mismatched
        request. Verified on 2026-09-03 - the same call succeeded with
        runtime params and failed with the frozen ones.
        """
        if student.class_code is not None and student.student_id:
            return {
                "classCode": student.class_code,
                "moduleID": 11,
                "studentID": student.student_id,
                "studentName": student.name,
                "studyYear": student.study_year,
                "viewType": 0,
                "weekIndex": 0,
            }

        # Fall back to config only for a POSITIVELY matched student. Never use
        # an unmatched entry: a parent account can discover several children
        # while only one has fallback params, and borrowing them would fetch
        # one child's homework under another child's name. An unmatched student
        # returns None and relies on the dashboard fallback instead.
        for entry in self.config.students:
            sp = entry.get("student_params")
            if not sp:
                continue
            if sp.get("studentID") == student.student_id or entry.get("name") == student.name:
                logger.warning(
                    "Falling back to matched student_params from config.yaml; "
                    "these go stale every school year and can cause 'view is blocked'"
                )
                return sp
        return None

    # ------------------------------------------------------------------
    def check_messages(self) -> None:
        """Fetch the inbox, notify about anything newly seen.

        Separate from check_once() so a failure here never costs us the
        homework check - the inbox is a bonus, homework is the job.
        """
        if not self.config.messages_enabled:
            return
        if not self.client and not self.connect():
            return

        logger.info("Checking school messages")
        try:
            messages = self.client.get_messages_inbox()
        except TokenExpired:
            logger.error("Token rejected while fetching messages")
            self._handle_expired()
            return
        except (ApiError, RequestFailed) as e:
            logger.error(f"Could not fetch messages: {e}")
            return

        logger.info(f"Inbox returned {len(messages)} message(s)")

        first_run = self.messages_state.is_first_run_for(INBOX_BUCKET)
        new_messages, dropped = self.messages_state.diff(INBOX_BUCKET, messages)

        self.notifier.publish_messages_discovery()
        self.notifier.publish_messages_state(messages)

        if first_run and new_messages and self.config.seed_quietly:
            logger.info(
                f"First inbox run: recorded {len(new_messages)} existing "
                "message(s) without notifying (set SEED_QUIETLY=0 to notify)"
            )
        elif new_messages:
            if not self.notifier.notify_new_messages(new_messages):
                # Delivery failed - forget these so they retry next cycle.
                self.messages_state.unsee(INBOX_BUCKET, new_messages)
        else:
            logger.info("No new messages")
        if dropped:
            logger.debug(f"Pruned {dropped} message(s) no longer in the inbox")

        self.messages_state.save()

    # ------------------------------------------------------------------
    def check_all(self) -> None:
        """One scheduled pass: homework, then messages."""
        self.check_once()
        self.check_messages()

    # ------------------------------------------------------------------
    def check_once(self) -> None:
        """One full pass: discover students, fetch, diff, notify."""
        if not self.client and not self.connect():
            return

        logger.info("Starting homework check")
        try:
            students = self.client.get_students()
        except TokenExpired:
            logger.error("Token rejected while discovering students")
            self._handle_expired()
            return
        except (ApiError, RequestFailed) as e:
            logger.error(f"Could not discover students: {e}")
            return

        if not students:
            logger.warning("No students returned by InitDashboard")
            return

        self._expiry_notified = False

        for student in students:
            name = self.config.display_name_for(student)
            self._known_names.add(name)
            try:
                items = self.fetch_homework(student)
            except TokenExpired:
                logger.error(f"Token rejected while fetching homework for {name}")
                self._handle_expired()
                return
            except (ApiError, RequestFailed) as e:
                # A fetch outage - leave this student's seen state and MQTT
                # untouched so recovery does not re-notify existing homework.
                logger.error(f"Homework fetch failed for {name} ({e}); skipping this student")
                continue

            # Seed silently the first time we see a student: the endpoint
            # returns a six-day window, so announcing all of it would fire a
            # burst of "new" homework that is simply pre-existing.
            first_run = self.state.is_first_run_for(name)
            new_items, dropped = self.state.diff(name, items)
            self.notifier.publish_discovery(name)
            self.notifier.publish_state(
                name,
                items,
                token_status="ok",
                token_minutes_left=self.token_minutes_left(),
            )

            if first_run and new_items and self.config.seed_quietly:
                logger.info(
                    f"First run for {name}: recorded {len(new_items)} existing "
                    "assignment(s) without notifying (set SEED_QUIETLY=0 to notify)"
                )
            elif new_items:
                if not self.notifier.notify_new_homework(name, new_items):
                    # Delivery failed - forget these so they retry next cycle
                    # instead of being permanently suppressed.
                    self.state.unsee(name, new_items)
            else:
                logger.info(f"No new homework for {name}")
            if dropped:
                logger.debug(f"Pruned {dropped} stale entry/entries for {name}")

        self.state.save()
        logger.info("Homework check complete")

    # ------------------------------------------------------------------
    def start(self) -> None:
        logger.info("Starting SmartSchool Homework Monitor (manual-token mode)")

        if not self.connect():
            logger.error(
                "No token available. Paste a webToken into "
                f"{self.config.paths.token_file} and restart."
            )
            return

        for when in self.config.schedules:
            schedule.every().day.at(when).do(self.check_all)
            logger.info(f"Scheduled homework check at {when}")

        schedule.every(self.config.rotate_minutes).minutes.do(self.rotate_token)
        logger.info(f"Scheduled token rotation every {self.config.rotate_minutes} min")

        self.rotate_token()
        self.check_all()

        while True:
            try:
                schedule.run_pending()
                time.sleep(30)
            except KeyboardInterrupt:
                logger.info("Monitor stopped by user")
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(60)

        self.close()

    def close(self) -> None:
        if self.client:
            self.client.close()
        self.notifier.close()


def main() -> int:
    paths = Paths()
    paths.ensure()
    setup_logging(paths.log_dir)
    monitor = Monitor(Config(paths))
    try:
        monitor.start()
        return 0
    except TokenMissing as e:
        logger.error(str(e))
        return 2
    except Exception as e:
        logger.exception(f"Monitor failed to start: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

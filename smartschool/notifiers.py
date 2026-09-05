"""Notification fan-out: Apprise channels plus Home Assistant MQTT entities.

Carried over from smartschool_monitor_v2.py, with the changes that mattered:
  * paho-mqtt 2.x callback API (the old mqtt.Client() raises a
    DeprecationWarning on 2.x and is removed in 3.x)
  * the dead _send_webhook_notification() helper is gone - Apprise already
    speaks json:// and jsons://
  * a token_expired() notification, so a dead token tells you in Home
    Assistant instead of only appearing in the log
"""

import hashlib
import json
import os
from datetime import datetime
from typing import List, Optional

from loguru import logger

from .models import HomeworkItem, Message

try:
    import apprise

    APPRISE_AVAILABLE = True
except ImportError:  # pragma: no cover
    APPRISE_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:  # pragma: no cover
    MQTT_AVAILABLE = False


def device_id_for(student_name: str) -> str:
    """Stable ASCII device id - Hebrew names cannot go in an MQTT topic."""
    name_hash = hashlib.md5(student_name.encode("utf-8")).hexdigest()[:8]
    return f"student_{name_hash}"


class Notifier:
    """Sends to every configured Apprise URL and mirrors state into MQTT."""

    def __init__(self, notifiers: str = "", mqtt_config: Optional[dict] = None):
        self.apobj = None
        self.mqtt_client = None
        self._discovery_sent = set()
        # Last full state payload published per student, so token status can be
        # updated on expiry without clobbering the retained homework fields.
        self._last_state: dict = {}
        self._setup_apprise(notifiers or os.getenv("NOTIFIERS", ""))
        self._setup_mqtt(mqtt_config or {})

    # ------------------------------------------------------------------
    def _setup_apprise(self, notifiers_str: str) -> None:
        if not notifiers_str:
            logger.warning("No NOTIFIERS configured - no push notifications will be sent")
            return
        if not APPRISE_AVAILABLE:
            logger.error("apprise is not installed - cannot send notifications")
            return

        self.apobj = apprise.Apprise()
        for url in notifiers_str.split(","):
            url = url.strip()
            if not url:
                continue
            if self.apobj.add(url):
                # Never log the URL itself; these carry webhook tokens and
                # MQTT passwords, and this log file is long-lived.
                logger.info(f"Added notifier: {url.split('://', 1)[0]}://...")
            else:
                logger.error(f"Rejected notifier (bad format): {url.split('://', 1)[0]}://...")

    def _setup_mqtt(self, cfg: dict) -> None:
        broker = cfg.get("broker") or os.getenv("MQTT_BROKER", "")
        if not broker:
            logger.info("MQTT_BROKER not set - skipping Home Assistant MQTT entities")
            return
        if not MQTT_AVAILABLE:
            logger.error("paho-mqtt is not installed - cannot publish MQTT entities")
            return

        try:
            port = int(cfg.get("port") or os.getenv("MQTT_PORT", "1883"))
            user = cfg.get("user") or os.getenv("MQTT_USER", "")
            password = cfg.get("password") or os.getenv("MQTT_PASS", "")

            # paho-mqtt 2.x requires an explicit callback API version.
            if hasattr(mqtt, "CallbackAPIVersion"):
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id="smartschool_monitor",
                )
            else:  # paho-mqtt 1.x
                client = mqtt.Client(client_id="smartschool_monitor")

            if user and password:
                client.username_pw_set(user, password)

            client.connect(broker, port, 60)
            client.loop_start()
            self.mqtt_client = client
            logger.info(f"MQTT connected to {broker}:{port}")
        except Exception as e:
            logger.error(f"Failed to set up MQTT: {e}")
            self.mqtt_client = None

    # ------------------------------------------------------------------
    def publish_discovery(self, student_name: str) -> None:
        """Publish Home Assistant MQTT discovery configs (once per student)."""
        if not self.mqtt_client or student_name in self._discovery_sent:
            return

        dev = device_id_for(student_name)
        device_info = {
            "identifiers": [f"smartschool_{dev}"],
            "name": f"SmartSchool - {student_name}",
            "manufacturer": "SmartSchool Monitor",
            "model": "Homework Tracker",
        }
        state_topic = f"smartschool/{dev}/state"

        sensors = [
            ("count", "Homework Count", "{{ value_json.count }}", "mdi:book-open-variant", None),
            ("count_week", "Homework This Week", "{{ value_json.count_week }}", "mdi:calendar-week", None),
            ("count_upcoming", "Homework Upcoming", "{{ value_json.count_upcoming }}", "mdi:calendar-arrow-right", None),
            ("details", "Homework Details", "{{ value_json.details }}", "mdi:text-box-multiple", None),
            ("last_check", "Last Check", "{{ value_json.last_check }}", "mdi:clock-check", "timestamp"),
            ("token_status", "Token Status", "{{ value_json.token_status }}", "mdi:key-variant", None),
            ("token_hours_left", "Token Hours Left", "{{ value_json.token_hours_left }}", "mdi:key-clock", None),
        ]

        try:
            for key, label, template, icon, device_class in sensors:
                config = {
                    "name": f"SmartSchool {student_name} {label}",
                    "unique_id": f"smartschool_{dev}_{key}",
                    "state_topic": state_topic,
                    "value_template": template,
                    "icon": icon,
                    "device": device_info,
                }
                if device_class:
                    config["device_class"] = device_class
                if key == "details":
                    # Home Assistant caps a sensor STATE at 255 chars, but the
                    # rendered list is far longer. Keep the state a short
                    # summary and expose the full text as a `text` attribute.
                    config["value_template"] = (
                        "{{ value_json.count }} today / {{ value_json.count_week }} this week"
                    )
                    config["json_attributes_topic"] = state_topic
                    config["json_attributes_template"] = (
                        "{{ {'text': value_json.details} | tojson }}"
                    )
                self.mqtt_client.publish(
                    f"homeassistant/sensor/smartschool_{dev}_{key}/config",
                    json.dumps(config, ensure_ascii=False),
                    retain=True,
                )
            self._discovery_sent.add(student_name)
            logger.info(f"Published MQTT discovery for {student_name}")
        except Exception as e:
            logger.error(f"Failed to publish MQTT discovery: {e}")

    def publish_state(
        self,
        student_name: str,
        homework: List[HomeworkItem],
        *,
        token_status: str = "ok",
        token_minutes_left: Optional[float] = None,
    ) -> None:
        """Publish today's homework snapshot for the student's HA entities."""
        if not self.mqtt_client:
            return

        dev = device_id_for(student_name)
        today = datetime.now().strftime("%Y-%m-%d")
        todays = [hw for hw in homework if (hw.date or "")[:10] == today]
        upcoming = [hw for hw in homework if (hw.date or "")[:10] >= today]

        def render(items, heading):
            lines = [heading, ""]
            for idx, hw in enumerate(
                sorted(items, key=lambda h: (h.date or "", h.subject or "")), 1
            ):
                date = (hw.date or "")[:10]
                lines.append(f"{idx}. {hw.subject} ({date}) - {hw.teacher}")
                text = hw.homework
                lines.append(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
                lines.append("")
            return "\n".join(lines)

        # Falling back to the whole visible window keeps the sensor useful on a
        # day with nothing due, rather than reading "no homework" while two
        # assignments sit in the week.
        if todays:
            details = render(todays, f"Today's homework for {student_name}:")
        elif homework:
            details = render(homework, f"No homework today ({today}). This week:")
        else:
            details = f"No homework for today ({today})"

        payload = {
            "count": len(todays),
            "count_week": len(homework),
            "count_upcoming": len(upcoming),
            "details": details,
            "last_check": datetime.now().isoformat(),
            "token_status": token_status,
            "token_hours_left": (
                round(token_minutes_left / 60, 1) if token_minutes_left is not None else None
            ),
        }

        self._last_state[student_name] = dict(payload)
        try:
            self.mqtt_client.publish(
                f"smartschool/{dev}/state",
                json.dumps(payload, ensure_ascii=False),
                retain=True,
            )
            logger.info(f"Published MQTT state for {student_name}: {len(todays)} item(s) today")
        except Exception as e:
            logger.error(f"Failed to publish MQTT state: {e}")

    def publish_token_status(
        self, student_name: str, status: str, *, token_minutes_left: Optional[float] = None
    ) -> None:
        """Update only the token status, preserving the last homework payload.

        On expiry we must not overwrite the retained homework with count=0 /
        "No homework" - no fetch established that. Re-publish the last-known
        payload with just token_status changed; fall back to a minimal payload
        if nothing has been published yet (e.g. expiry on the first run).
        """
        if not self.mqtt_client:
            return
        dev = device_id_for(student_name)
        payload = dict(self._last_state.get(student_name) or {})
        payload.setdefault("count", 0)
        payload.setdefault("count_week", 0)
        payload.setdefault("count_upcoming", 0)
        payload.setdefault("details", "")
        payload["last_check"] = payload.get("last_check") or datetime.now().isoformat()
        payload["token_status"] = status
        payload["token_hours_left"] = (
            round(token_minutes_left / 60, 1)
            if token_minutes_left is not None
            else payload.get("token_hours_left")
        )
        self._last_state[student_name] = payload
        try:
            self.mqtt_client.publish(
                f"smartschool/{dev}/state",
                json.dumps(payload, ensure_ascii=False),
                retain=True,
            )
            logger.info(f"Published token_status={status} for {student_name}")
        except Exception as e:
            logger.error(f"Failed to publish token status: {e}")

    # ------------------------------------------------------------------
    def notify_new_homework(self, student_name: str, new_items: List[HomeworkItem]) -> bool:
        """Notify about every newly detected assignment, whatever its date.

        Returns whether the caller may commit these items as seen: True when
        delivered or when there is nothing to deliver to; False only on an
        actual send failure, so the caller can retry them next cycle.

        The previous behaviour filtered to homework dated exactly today, which
        silently dropped both of the real cases: an assignment a teacher enters
        for tomorrow, and one entered late for yesterday. The endpoint returns
        a six-day window (past, today and future), so a today-only filter threw
        most of it away.
        """
        if not self.apobj or len(self.apobj) == 0:
            logger.warning("No notifiers configured - skipping notification")
            return True  # nothing to deliver to; do not retry forever

        if not new_items:
            logger.info("No new homework to notify about")
            return True

        today = datetime.now().strftime("%Y-%m-%d")
        # Chronological, so a digest reads in order.
        items = sorted(new_items, key=lambda hw: (hw.date or "", hw.subject or ""))

        lines = [f"New homework for {student_name}:", ""]
        for idx, hw in enumerate(items, 1):
            date = (hw.date or "")[:10]
            when = " (today)" if date == today else ""
            lines.append(f"{idx}. {hw.subject} - {date}{when}")
            lines.append(f"   Teacher: {hw.teacher}")
            text = hw.homework
            lines.append(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
            lines.append("")

        count = len(items)
        title = (
            f"SmartSchool Homework - {student_name}"
            if count == 1
            else f"SmartSchool Homework - {student_name} ({count} new)"
        )
        return self._send(title, "\n".join(lines))

    def notify_new_messages(self, messages: List[Message]) -> bool:
        """Notify about newly seen inbox messages, newest first.

        Returns whether the caller may commit these as seen (see
        notify_new_homework).
        """
        if not self.apobj or len(self.apobj) == 0:
            logger.warning("No notifiers configured - skipping message notification")
            return True
        if not messages:
            logger.info("No new messages to notify about")
            return True

        # Newest first: unlike homework, the most recent message is the point.
        items = sorted(messages, key=lambda m: m.sent_at or "", reverse=True)

        lines = ["New school messages:", ""]
        for idx, m in enumerate(items, 1):
            date = (m.sent_at or "")[:16].replace("T", " ")
            flags = []
            if m.has_files:
                flags.append("has attachment")
            if not m.has_read:
                flags.append("unread")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"{idx}. {m.subject}{suffix}")
            lines.append(f"   From: {m.sender or 'Unknown'} - {date}")
            lines.append("")

        count = len(items)
        title = (
            "SmartSchool - new message"
            if count == 1
            else f"SmartSchool - {count} new messages"
        )
        return self._send(title, "\n".join(lines))

    def publish_messages_discovery(self, device_key: str = "inbox") -> None:
        """Discovery for the account-level inbox sensors.

        The inbox belongs to the account, not a student, so it gets its own
        device rather than hanging off one child.
        """
        if not self.mqtt_client or device_key in self._discovery_sent:
            return

        device_info = {
            "identifiers": [f"smartschool_{device_key}"],
            "name": "SmartSchool - Messages",
            "manufacturer": "SmartSchool Monitor",
            "model": "Message Inbox",
        }
        state_topic = f"smartschool/{device_key}/state"
        sensors = [
            ("unread", "Messages Unread", "{{ value_json.unread }}", "mdi:email-alert"),
            ("total", "Messages Total", "{{ value_json.total }}", "mdi:email-multiple"),
            ("latest", "Latest Message", "{{ value_json.latest }}", "mdi:email-open"),
            ("details", "Messages Details", "{{ value_json.details }}", "mdi:text-box-multiple"),
            ("last_check", "Messages Last Check", "{{ value_json.last_check }}", "mdi:clock-check"),
        ]
        try:
            for key, label, template, icon in sensors:
                config = {
                    "name": f"SmartSchool {label}",
                    "unique_id": f"smartschool_{device_key}_{key}",
                    "state_topic": state_topic,
                    "value_template": template,
                    "icon": icon,
                    "device": device_info,
                }
                if key == "last_check":
                    config["device_class"] = "timestamp"
                if key == "details":
                    # HA caps a sensor state at 255 chars; keep the state short
                    # and put the full rendered list in a `text` attribute.
                    config["value_template"] = (
                        "{{ value_json.unread }} unread / {{ value_json.total }} total"
                    )
                    config["json_attributes_topic"] = state_topic
                    config["json_attributes_template"] = (
                        "{{ {'text': value_json.details} | tojson }}"
                    )
                self.mqtt_client.publish(
                    f"homeassistant/sensor/smartschool_{device_key}_{key}/config",
                    json.dumps(config, ensure_ascii=False),
                    retain=True,
                )
            self._discovery_sent.add(device_key)
            logger.info("Published MQTT discovery for message inbox")
        except Exception as e:
            logger.error(f"Failed to publish message discovery: {e}")

    def publish_messages_state(
        self, messages: List[Message], *, device_key: str = "inbox"
    ) -> None:
        """Publish inbox counts and a rendered list."""
        if not self.mqtt_client:
            return

        items = sorted(messages, key=lambda m: m.sent_at or "", reverse=True)
        unread = [m for m in items if not m.has_read]

        if items:
            lines = []
            for idx, m in enumerate(items, 1):
                mark = "*" if not m.has_read else " "
                date = (m.sent_at or "")[:16].replace("T", " ")
                clip = "  [attachment]" if m.has_files else ""
                lines.append(f"{mark} {idx}. {m.subject}{clip}")
                lines.append(f"     {m.sender or 'Unknown'} - {date}")
            details = "\n".join(lines)
            # HA sensor states are capped at 255 chars; keep the headline short.
            latest = items[0].subject[:240] or "(no subject)"
        else:
            details = "No messages"
            latest = "No messages"

        payload = {
            "unread": len(unread),
            "total": len(items),
            "latest": latest,
            "details": details,
            "last_check": datetime.now().isoformat(),
        }
        try:
            self.mqtt_client.publish(
                f"smartschool/{device_key}/state",
                json.dumps(payload, ensure_ascii=False),
                retain=True,
            )
            logger.info(
                f"Published MQTT message state: {len(items)} total, {len(unread)} unread"
            )
        except Exception as e:
            logger.error(f"Failed to publish message state: {e}")

    def notify_token_expiring(self, token_file: str, minutes_left: float) -> None:
        """Warn before the token dies, so it can be replaced without a gap.

        The monitor cannot re-authenticate (reCAPTCHA), so a warning ahead of
        time is the difference between a seamless swap and a silent outage.
        """
        body = (
            f"The SmartSchool webToken expires in about {minutes_left:.0f} minutes.\n\n"
            "Replace it before then to avoid missing a homework check:\n"
            "1. Log in at https://webtop.smartschool.co.il\n"
            "2. DevTools (F12) -> Application -> Cookies -> copy 'webToken'\n"
            f"3. Paste it into {token_file}\n\n"
            "The monitor picks it up within one cycle; no restart needed."
        )
        logger.warning(f"Token expires in ~{minutes_left:.0f} min - warning the user")
        self._send("SmartSchool - token expiring soon", body)

    def notify_token_expired(self, token_file: str) -> None:
        """Tell the user their token died and how to fix it.

        SmartSchool requires a reCAPTCHA checkbox on login, so the monitor
        cannot recover on its own - a human has to paste a new token.
        """
        body = (
            "The SmartSchool webToken has expired and the monitor cannot log in "
            "automatically (SmartSchool requires a reCAPTCHA checkbox).\n\n"
            "To restore monitoring:\n"
            "1. Log in at https://webtop.smartschool.co.il\n"
            "2. DevTools (F12) -> Application -> Cookies -> copy 'webToken'\n"
            f"3. Paste it into {token_file}\n"
        )
        logger.error("Token expired - notifying user to paste a fresh token")
        self._send("SmartSchool Monitor - token expired", body)

    def _send(self, title: str, body: str) -> bool:
        """Send via Apprise, reporting whether delivery actually succeeded.

        Apprise signals failure by return value rather than raising, so
        ignoring it turns a misconfigured webhook into silence - the worst
        outcome for a notifier whose whole job is to tell you something.
        """
        if not self.apobj or len(self.apobj) == 0:
            return False
        try:
            logger.info(f"Sending notification: {title}")
            ok = self.apobj.notify(body=body, title=title)
            if ok:
                logger.info("Notification delivered")
            else:
                logger.error(
                    "Apprise reported delivery failure - check the NOTIFIERS "
                    "URLs and that the target is reachable"
                )
            return bool(ok)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False

    def close(self) -> None:
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception as e:  # pragma: no cover
                logger.debug(f"Error closing MQTT client: {e}")

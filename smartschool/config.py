"""Configuration and path resolution.

Runs identically as a container (/app/config) and as a plain local process
(./config), since the deployment target is still open.
"""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml
from loguru import logger


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default


class Paths:
    """Resolves config/log locations for container vs local runs."""

    def __init__(self, root: Path = None):
        if root is not None:
            base = Path(root)
            self.config_dir = base / "config"
            self.log_dir = base / "logs"
        elif Path("/app/config").exists():
            self.config_dir = Path("/app/config")
            self.log_dir = Path("/app/logs")
        else:
            self.config_dir = Path("./config")
            self.log_dir = Path("./logs")

        self.config_file = self.config_dir / "config.yaml"
        self.state_file = self.config_dir / "homework_state.json"
        self.messages_state_file = self.config_dir / "messages_state.json"
        self.token_file = self.config_dir / "token.txt"

    def ensure(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


class Config:
    """Runtime settings, from config.yaml plus environment overrides."""

    def __init__(self, paths: Paths = None):
        self.paths = paths or Paths()
        self.students: List[Dict[str, Any]] = []
        self.load()

        # Schedule of full homework checks.
        self.schedules = [
            s.strip()
            for s in os.getenv("SCHEDULES", "12:00,16:00,20:00").split(",")
            if s.strip()
        ]
        # How often to poke CheckBackgroundToken. The SPA uses 30 minutes; we
        # go under that so a rotation is never missed by a few seconds.
        self.rotate_minutes = _env_int("TOKEN_ROTATE_MINUTES", 20)
        self.notifiers = os.getenv("NOTIFIERS", "")
        # On the very first sighting of a student, record the visible six-day
        # window without notifying. Prevents a burst of "new" homework that is
        # actually pre-existing. Set SEED_QUIETLY=0 to announce it instead.
        self.seed_quietly = os.getenv("SEED_QUIETLY", "1").lower() not in ("0", "false", "no")
        # Inbox monitoring is opt-out; it uses the same token and adds one
        # request per check.
        self.messages_enabled = os.getenv("MESSAGES_ENABLED", "1").lower() not in ("0", "false", "no")
        # Measured 2026-09-03: the webToken cookie has a 9-hour lifetime.
        self.token_ttl_hours = float(os.getenv("TOKEN_TTL_HOURS", "9") or 9)
        # Warn this far ahead of the predicted expiry, so a token can be
        # replaced before a check actually fails.
        self.token_warn_minutes = _env_int("TOKEN_WARN_MINUTES", 45)
        self.verify_tls = os.getenv("VERIFY_TLS", "1").lower() not in ("0", "false", "no")
        self.mqtt = {
            "broker": os.getenv("MQTT_BROKER", ""),
            "port": os.getenv("MQTT_PORT", "1883"),
            "user": os.getenv("MQTT_USER", ""),
            "password": os.getenv("MQTT_PASS", ""),
        }

    def load(self) -> None:
        """Read config.yaml if present.

        Student identity now comes from InitDashboard at runtime, so this file
        is optional - it only supplies display-name overrides and any manual
        student_params needed for the legacy PupilCard endpoint.
        """
        if not self.paths.config_file.exists():
            logger.info(f"No {self.paths.config_file} - relying on runtime student discovery")
            return
        try:
            with open(self.paths.config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self.students = data.get("students") or []
            logger.info(f"Loaded {len(self.students)} student entry/entries from config")
        except (OSError, yaml.YAMLError) as e:
            logger.error(f"Failed to read {self.paths.config_file}: {e}")

    def display_name_for(self, student) -> str:
        """Prefer a name from config.yaml, else whatever the API reported."""
        for entry in self.students:
            sid = entry.get("student_id")
            if sid and sid == student.student_id:
                return entry.get("name") or student.name
        if len(self.students) == 1 and self.students[0].get("name"):
            return self.students[0]["name"]
        return student.name or "Unknown"

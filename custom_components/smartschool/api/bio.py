"""Biometric / remember-me credential for unattended token renewal.

The webToken expires (9 hours, or ~6 months with remember-me). The `bioLogin`
credential does not expire for ~1 year, and `user/loginByBio` mints a fresh
webToken from it with no password and no captcha. So one browser login that
produces a bioLogin lets the monitor renew itself for a year.

Verified working 2026-09-03: replaying a browser-registered bioLogin with
param1="2", param2="" (empty deviceId) and param5="true" (isMobile) returned a
valid token. The exact captured request was the key - a self-registered
credential (via writeBio) is accepted by isBioExist but rejected by
loginByBio; only the browser-registered one works.

Credentials live in config/bio_credentials.json (gitignored), extracted once
from the browser. See the docstring of scripts/extract_bio.md / bio_test.py.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import logging

_LOGGER = logging.getLogger(__name__)


@dataclass
class BioCredentials:
    """The five values user/loginByBio needs, plus the mode."""

    bio_login: str
    unique_id: str
    selected_user: str = ""
    device_id: str = ""
    is_mobile: bool = True
    mode: str = "2"

    @classmethod
    def load(cls, config_dir: Path) -> Optional["BioCredentials"]:
        """Read config/bio_credentials.json, or None if absent/invalid.

        Absent is normal - it just means auto-renewal is not set up and the
        monitor falls back to manual token pasting.
        """
        path = Path(config_dir) / "bio_credentials.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _LOGGER.warning(f"bio_credentials.json unreadable ({e}); auto-renew disabled")
            return None

        # Valid JSON is not necessarily an object; a list or null would crash
        # the .get() calls below.
        if not isinstance(data, dict):
            _LOGGER.warning("bio_credentials.json is not a JSON object; auto-renew disabled")
            return None

        bio = data.get("bioLogin")
        if not bio:
            _LOGGER.warning("bio_credentials.json has no bioLogin; auto-renew disabled")
            return None

        return cls(
            bio_login=bio,
            unique_id=data.get("uniqueId") or data.get("wt_uid") or "",
            selected_user=data.get("selectedUser") or data.get("SavedUser") or "",
            device_id=data.get("deviceId") or "",
            # The working request had isMobile=true; default to that, but honour
            # an explicit false.
            is_mobile=bool(data.get("isMobile", True)),
            mode=str(data.get("mode", "2")),
        )

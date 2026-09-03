"""Homework state tracking - what have we already told the user about?

The bug this fixes: the old hash_homework() hashed the whole item dict
including `date`, while the Playwright scraper stamped date=today on every
item. So each midnight every assignment produced a brand-new hash and got
re-notified. Identity now comes from HomeworkItem.identity(), which is built
from subject + homework text + the assignment's own date.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from loguru import logger

from .models import HomeworkItem


class SeenState:
    """Record of items already seen, bucketed by a key, persisted as JSON.

    Used for both homework (bucketed per student) and inbox messages
    (bucketed under a single inbox key). Any item with `identity()` and
    `as_dict()` works.
    """

    def __init__(self, state_file: Path, label: str = "homework"):
        self.state_file = Path(state_file)
        self.label = label
        self._state: Dict[str, Dict[str, dict]] = {}
        self.load()

    def load(self) -> None:
        if not self.state_file.exists():
            self._state = {}
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._state = data if isinstance(data, dict) else {}
            logger.info(f"Loaded {self.label} state for {len(self._state)} bucket(s)")
        except (OSError, ValueError) as e:
            logger.error(f"Failed to load {self.label} state ({e}); starting fresh")
            self._state = {}

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"Failed to save {self.label} state: {e}")

    def is_first_run_for(self, bucket: str) -> bool:
        """True when we have never recorded anything for this student.

        Used to seed state silently instead of announcing every assignment in
        the visible six-day window the first time the monitor sees a student.
        """
        return bucket not in self._state

    def diff(
        self, bucket: str, items: List
    ) -> Tuple[List, int]:
        """Record the current homework set and return the items that are new.

        Also prunes anything no longer present upstream, so a re-added
        assignment notifies again (which is the behaviour you want) while a
        still-listed one stays quiet.
        """
        seen = self._state.setdefault(bucket, {})
        current: Dict[str, dict] = {}
        new_items: List[HomeworkItem] = []

        for item in items:
            key = item.identity()
            current[key] = {
                "detected_at": seen.get(key, {}).get("detected_at") or datetime.now().isoformat(),
                "item": item.as_dict(),
            }
            if key not in seen:
                new_items.append(item)
                logger.info(f"New {self.label} detected for {bucket}: {getattr(item, 'subject', '?')}")

        dropped = len(seen) - len([k for k in seen if k in current])
        self._state[bucket] = current
        return new_items, dropped


# Back-compat alias: the class started life as HomeworkState.
HomeworkState = SeenState

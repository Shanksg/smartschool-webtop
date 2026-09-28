"""Stable per-student keys.

SmartSchool identifies a student by an *encrypted* id, and that id is not
permanent: it can change when the school year rolls over (a stale encrypted
studentID stopped working after a student moved up a grade). Devices, entities
and event history must not follow it, or every child would get new entities
each year and the old ones would be orphaned.

So each student gets a stable key: the first encrypted id the integration saw
for them. Existing installs therefore keep their entity ids. When a student's
encrypted id changes, the new id is mapped back to the old key by name - but
only when exactly one known student with that name has disappeared, so an
ambiguous case becomes a new student rather than a wrong merge.

API requests always use the student's current encrypted id; only what Home
Assistant stores (device/entity ids, event history) uses the key.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

# sensor.py names each student's device "SmartSchool - <name>".
DEVICE_NAME_PREFIX = "SmartSchool - "


def normalize_name(name: str | None) -> str:
    """Whitespace- and case-insensitive form of a student name ("" if none)."""
    return " ".join((name or "").split()).casefold()


def device_identifier(entry_id: str, key: str) -> str:
    """The student device identifier used by sensor.py and events.py."""
    return f"{entry_id}_student_{key}"


def known_students(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    """key -> normalized name for the student devices this entry already has.

    device.name is the name the integration set; a user's rename lives in
    name_by_user and does not affect it.
    """
    prefix = device_identifier(entry_id, "")
    known: dict[str, str] = {}
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        keys = [
            identifier[len(prefix):]
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier.startswith(prefix)
        ]
        if not keys:
            continue  # the inbox device, or not ours
        name = (device.name or "").strip()
        if name.startswith(DEVICE_NAME_PREFIX.strip()):
            name = name[len(DEVICE_NAME_PREFIX.strip()):]
        for key in keys:
            known[key] = normalize_name(name)
    return known


def assign_keys(
    students: Iterable[tuple[str, str]], known: dict[str, str]
) -> dict[str, str]:
    """Map each current (encrypted id, name) to a stable key.

    - an id that already is a known key keeps it;
    - otherwise, if exactly one known key with the same (non-empty) name is
      not in use by this snapshot, the id inherits that key (id rotation);
    - otherwise the id becomes a new key.
    """
    current = [(sid, normalize_name(name)) for sid, name in students if sid]
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for sid, _ in current:
        if sid in known:
            mapping[sid] = sid
            taken.add(sid)
    current_ids = {sid for sid, _ in current}
    # How many still-unmatched current students share each name, counted once
    # up front: two same-name students must both stay unmerged, not have the
    # second inherit a key once the first has been dealt with.
    unmatched_names: dict[str, int] = {}
    for sid, name in current:
        if sid not in mapping and name:
            unmatched_names[name] = unmatched_names.get(name, 0) + 1
    for sid, name in current:
        if sid in mapping:
            continue
        candidates = [
            key
            for key, known_name in known.items()
            if name
            and known_name == name
            and key not in taken
            and key not in current_ids
        ]
        if len(candidates) == 1 and unmatched_names.get(name) == 1:
            mapping[sid] = candidates[0]
            taken.add(candidates[0])
        else:
            mapping[sid] = sid
            taken.add(sid)
    return mapping

"""HACS / hassfest packaging invariants. No Home Assistant needed."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "smartschool"


def _manifest():
    return json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))


def test_single_integration_under_custom_components():
    # HACS installs exactly one integration directory per repository.
    dirs = [p.name for p in (ROOT / "custom_components").iterdir()
            if p.is_dir() and not p.name.startswith((".", "__"))]
    assert dirs == ["smartschool"]


def test_manifest_has_custom_integration_keys():
    m = _manifest()
    for key in ("domain", "name", "codeowners", "config_flow", "documentation",
                "iot_class", "issue_tracker", "requirements", "version"):
        assert key in m, key
    assert m["domain"] == INTEGRATION.name


def test_manifest_key_order_matches_hassfest():
    # hassfest: domain, name, then the remaining keys alphabetically.
    keys = list(_manifest())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_manifest_does_not_require_packages_core_already_pins():
    # requests ships with Home Assistant and is pinned by its constraints;
    # requiring a different range makes the integration fight core on install.
    reqs = [r.split("=")[0].split(">")[0].split("<")[0].strip().lower()
            for r in _manifest()["requirements"]]
    assert "requests" not in reqs and "urllib3" not in reqs


def test_hacs_json_matches_manifest():
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == _manifest()["name"]
    year, month, *_ = (int(x) for x in hacs["homeassistant"].split("."))
    # The reauth flow uses _get_reauth_entry() and relies on
    # async_update_reload_and_abort reloading unchanged entries; the
    # coordinator passes config_entry= explicitly.
    assert (year, month) >= (2026, 9)


def test_ci_floor_matches_hacs_minimum():
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    floor = hacs["homeassistant"]
    assert f'ha: "{floor}"' in workflow, "CI must test the declared minimum version"

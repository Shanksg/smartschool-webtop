"""Skip Home Assistant tests when the installed core is below the supported floor.

The integration targets the minimum version in hacs.json. An older core (for
example one left over in a local dev environment) is unsupported and may not
even import the integration, so its failures would be noise. CI runs these
tests on the floor and on the newest core. The standalone daemon's tests never
need Home Assistant and always run.
"""

import json
from pathlib import Path

import pytest

_HA_TEST_MODULES = {
    "test_config_flow.py",
    "test_coordinator.py",
    "test_events.py",
    "test_history.py",
    "test_options_diagnostics.py",
    "test_sensor.py",
}


def _floor() -> tuple[int, ...]:
    hacs = json.loads((Path(__file__).parent.parent / "hacs.json").read_text(encoding="utf-8"))
    return tuple(int(x) for x in hacs["homeassistant"].split("."))


def _installed() -> tuple[int, ...] | None:
    try:
        from homeassistant.const import __version__
    except ImportError:
        return None  # not installed: each module's importorskip reports it
    return tuple(int(x) for x in __version__.split(".")[:3] if x.isdigit())


def _unsupported_reason() -> str | None:
    installed, floor = _installed(), _floor()
    if installed is None or installed >= floor:
        return None
    return (
        f"Home Assistant {'.'.join(map(str, installed))} is below the supported "
        f"minimum {'.'.join(map(str, floor))} (hacs.json); integration tests not collected"
    )


def pytest_ignore_collect(collection_path, config):
    if collection_path.name in _HA_TEST_MODULES and _unsupported_reason():
        return True
    return None


def pytest_report_header(config):
    reason = _unsupported_reason()
    return f"WARNING: {reason}" if reason else None


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store.

    A delayed save is applied immediately (recording the delay), which is what
    matters for tests: what would be on disk after the write lands.
    """

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.delays: list[float] = []
        self.saves = 0
        self.removed = False

    async def async_load(self):
        if self.error is not None:
            raise self.error
        return self.data

    def async_delay_save(self, data_func, delay=0):
        self.delays.append(delay)
        self.data = data_func()
        self.saves += 1

    async def async_save(self, data):
        self.data = data
        self.saves += 1

    async def async_remove(self):
        self.data = None
        self.removed = True


@pytest.fixture
def make_store():
    """Factory: make_store(data=None, error=None) -> FakeStore."""
    return FakeStore

"""Tests for automated token renewal via bioLogin.

The webToken expires; the bioLogin credential (config/bio_credentials.json)
lasts ~1 year and mints fresh tokens with no password or captcha. Verified
against the live API 2026-09-03.
"""

import json
from pathlib import Path

import pytest

from smartschool.bio import BioCredentials
from smartschool.exceptions import ApiError, RequestFailed, TokenExpired
from smartschool.session import TokenStore


# ----------------------------------------------------------------------
# BioCredentials loading
# ----------------------------------------------------------------------
def _write_creds(tmp_path, **overrides):
    data = {
        "bioLogin": "U2FsdGVkX1-CREDENTIAL",
        "uniqueId": "uid-123",
        "selectedUser": "sel-user",
        "deviceId": "",
        "isMobile": True,
    }
    data.update(overrides)
    (tmp_path / "bio_credentials.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_returns_none_when_absent(tmp_path):
    assert BioCredentials.load(tmp_path) is None


def test_load_returns_none_without_biologin(tmp_path):
    (tmp_path / "bio_credentials.json").write_text('{"uniqueId": "x"}', encoding="utf-8")
    assert BioCredentials.load(tmp_path) is None


def test_load_tolerates_corrupt_json(tmp_path):
    (tmp_path / "bio_credentials.json").write_text("{not json", encoding="utf-8")
    assert BioCredentials.load(tmp_path) is None


def test_load_reads_all_fields(tmp_path):
    _write_creds(tmp_path)
    c = BioCredentials.load(tmp_path)
    assert c.bio_login == "U2FsdGVkX1-CREDENTIAL"
    assert c.unique_id == "uid-123"
    assert c.selected_user == "sel-user"
    assert c.device_id == ""
    assert c.is_mobile is True
    assert c.mode == "2"


def test_load_defaults_ismobile_true(tmp_path):
    """The verified-working request had isMobile=true."""
    _write_creds(tmp_path)
    (tmp_path / "bio_credentials.json").write_text(
        json.dumps({"bioLogin": "x", "uniqueId": "y"}), encoding="utf-8"
    )
    assert BioCredentials.load(tmp_path).is_mobile is True


def test_load_accepts_wt_uid_alias(tmp_path):
    (tmp_path / "bio_credentials.json").write_text(
        json.dumps({"bioLogin": "x", "wt_uid": "from-alias"}), encoding="utf-8"
    )
    assert BioCredentials.load(tmp_path).unique_id == "from-alias"


# ----------------------------------------------------------------------
# TokenStore.save_renewed
# ----------------------------------------------------------------------
def test_save_renewed_marks_file_token_as_ingested(tmp_path):
    """After renewal, a reload must NOT revert to the stale token.txt."""
    store = TokenStore(tmp_path)
    (tmp_path / "token.txt").write_text("DEAD-PASTED-TOKEN", encoding="utf-8")
    store.load()  # ingest the pasted token

    renewed = store.save_renewed("FRESH-BIO-TOKEN")
    assert renewed.token == "FRESH-BIO-TOKEN"
    assert renewed.pasted_token == "DEAD-PASTED-TOKEN"

    # reload: token.txt still holds the dead token, but the cache wins
    reloaded = store.load()
    assert reloaded.token == "FRESH-BIO-TOKEN", "must not revert to the stale paste"


def test_a_new_paste_still_overrides_a_renewed_token(tmp_path):
    store = TokenStore(tmp_path)
    (tmp_path / "token.txt").write_text("OLD", encoding="utf-8")
    store.load()
    store.save_renewed("BIO-TOKEN")

    # user pastes a genuinely new token
    (tmp_path / "token.txt").write_text("BRAND-NEW-PASTE", encoding="utf-8")
    assert store.load().token == "BRAND-NEW-PASTE"


# ----------------------------------------------------------------------
# Monitor.renew_via_bio
# ----------------------------------------------------------------------
def _monitor(tmp_path, monkeypatch, with_creds=True):
    from smartschool.config import Config, Paths
    from smartschool.monitor import Monitor

    monkeypatch.setenv("NOTIFIERS", "")
    monkeypatch.setenv("MQTT_BROKER", "")
    paths = Paths(root=tmp_path)
    paths.ensure()
    (paths.config_dir / "token.txt").write_text("initial", encoding="utf-8")
    if with_creds:
        _write_creds(paths.config_dir)
    m = Monitor(Config(paths))
    m.connect()
    return m


def test_renew_via_bio_installs_new_token(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    monkeypatch.setattr(m.client, "login_by_bio", lambda **kw: "MINTED-TOKEN")

    assert m.renew_via_bio() is True
    assert m.client.token == "MINTED-TOKEN" or m.token_state.token == "MINTED-TOKEN"
    assert m.token_state.token == "MINTED-TOKEN"


def test_renew_via_bio_passes_the_right_params(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    captured = {}

    def fake(**kw):
        captured.update(kw)
        return "T"

    monkeypatch.setattr(m.client, "login_by_bio", fake)
    m.renew_via_bio()
    assert captured["is_mobile"] is True          # the working value
    assert captured["device_id"] == ""
    assert captured["mode"] == "2"
    assert captured["bio_login"] == "U2FsdGVkX1-CREDENTIAL"


def test_renew_via_bio_false_without_credentials(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch, with_creds=False)
    assert m.renew_via_bio() is False


def test_renew_via_bio_false_when_server_declines(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    monkeypatch.setattr(m.client, "login_by_bio", lambda **kw: None)
    assert m.renew_via_bio() is False


def test_renew_via_bio_false_on_error(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)

    def boom(**kw):
        raise RequestFailed("network")

    monkeypatch.setattr(m.client, "login_by_bio", boom)
    assert m.renew_via_bio() is False


# ----------------------------------------------------------------------
# expiry handling prefers renewal over notifying
# ----------------------------------------------------------------------
def test_handle_expired_renews_instead_of_notifying(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch)
    monkeypatch.setattr(m.client, "login_by_bio", lambda **kw: "RENEWED")

    notified = []
    monkeypatch.setattr(m.notifier, "notify_token_expired", lambda f: notified.append(f))

    m._handle_expired()
    assert notified == [], "must not notify when renewal succeeds"
    assert m.token_state.token == "RENEWED"


def test_handle_expired_notifies_when_renewal_unavailable(tmp_path, monkeypatch):
    m = _monitor(tmp_path, monkeypatch, with_creds=False)
    notified = []
    monkeypatch.setattr(m.notifier, "notify_token_expired", lambda f: notified.append(f))

    m._handle_expired()
    assert len(notified) == 1, "no credential -> fall back to manual-paste notice"
    assert m.client is None

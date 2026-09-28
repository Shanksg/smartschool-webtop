"""Config-flow recovery with synthetic credentials and no real network calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import UnknownEntry  # noqa: E402
from homeassistant.data_entry_flow import AbortFlow  # noqa: E402

from custom_components.smartschool import config_flow as flow_mod
from custom_components.smartschool.api.exceptions import ApiError, RequestFailed, TokenExpired


CREDS = {"bio_login": "old-secret", "unique_id": "browser-a", "selected_user": "",
         "device_id": "", "is_mobile": True}


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setattr("homeassistant.components.persistent_notification.async_dismiss", Mock())
    result = flow_mod.SmartSchoolConfigFlow()
    result.context = {"entry_id": "entry-a", "source": "reauth"}
    # title and update_listeners mirror a real ConfigEntry: HA reads the title
    # for the reauth dialog and update_listeners in async_update_reload_and_abort.
    entry = SimpleNamespace(entry_id="entry-a", domain="smartschool", unique_id="browser-a",
                            title="SmartSchool", data={**CREDS, "future_setting": True},
                            update_listeners=[])
    manager = Mock()
    manager.async_get_known_entry.return_value = entry
    manager.async_entries.return_value = [entry]
    manager.flow.async_progress_by_handler.return_value = []
    # Neutral account evidence by default: validation could not list students
    # and the entry has no student devices, so the same-account check never
    # blocks unless a test sets it up.
    result.hass = SimpleNamespace(config_entries=manager,
                                  async_add_executor_job=AsyncMock(return_value=None))
    result.known_students = set()
    monkeypatch.setattr(flow_mod, "_known_student_ids", lambda hass, entry_id: result.known_students)
    return result


def run(flow, data=None):
    return asyncio.run(flow.async_step_reauth_confirm(data))


def suggested(result, field):
    """The suggested value HA would pre-fill for `field`, or None.

    repr() of a voluptuous schema never includes suggested values (they live
    in each key's description), so secret checks must look here instead.
    """
    key = next(k for k in result["data_schema"].schema if k == field)
    return (key.description or {}).get("suggested_value")


def test_reauth_form_never_suggests_old_or_submitted_secret(flow):
    result = asyncio.run(flow.async_step_reauth(CREDS))
    assert result["step_id"] == "reauth_confirm"
    assert suggested(result, "bio_login") is None
    # Non-secret fields are still pre-filled from the entry.
    assert suggested(result, "unique_id") == "browser-a"
    flow.hass.async_add_executor_job.side_effect = TokenExpired("private-server-data")
    result = run(flow, {**CREDS, "bio_login": "new-secret"})
    assert result["errors"] == {"base": "invalid_auth"}
    # The re-rendered form must not offer back the secret just submitted.
    assert suggested(result, "bio_login") is None
    assert "private-server-data" not in repr(result)
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("field", ["bio_login", "unique_id"])
def test_blank_required_field_does_not_validate_or_save(flow, field):
    result = run(flow, {**CREDS, field: "  "})
    assert result["errors"] == {field: "required"}
    flow.hass.async_add_executor_job.assert_not_called()
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("error,code", [
    (ApiError("private"), "invalid_auth"), (TokenExpired("private"), "invalid_auth"),
    (RequestFailed("private"), "cannot_connect"), (ValueError("private"), "unknown"),
])
def test_validation_errors_preserve_entry_and_remain_retryable(flow, error, code, caplog):
    flow.hass.async_add_executor_job.side_effect = error
    result = run(flow, {**CREDS, "bio_login": "new-secret"})
    assert result["errors"] == {"base": code}
    assert "private" not in repr(result) + caplog.text
    if code == "unknown":
        assert "ValueError" in caplog.text, "type is logged so real bugs stay visible"
    assert flow.hass.config_entries.async_get_known_entry.return_value.data["bio_login"] == "old-secret"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


def test_reauth_updates_original_entry_trims_fields_and_reloads(flow):
    result = run(flow, {**CREDS, "bio_login": " new-secret ", "unique_id": " browser-b "})
    assert result["reason"] == "reauth_successful"
    call = flow.hass.config_entries.async_update_entry.call_args.kwargs
    assert call["entry"].entry_id == "entry-a"
    assert call["entry"].unique_id == "browser-a"
    assert call["unique_id"] == "browser-b"
    assert call["data"] == {**CREDS, "bio_login": "new-secret", "unique_id": "browser-b",
                            "future_setting": True}
    flow.hass.async_add_executor_job.assert_awaited_once_with(flow_mod._validate_credentials, call["data"])
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-a")


def test_unchanged_credentials_still_reload(flow):
    assert run(flow, CREDS)["reason"] == "reauth_successful"
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-a")


def test_other_entry_device_credential_is_rejected(flow):
    manager = flow.hass.config_entries
    manager.async_entries.return_value.append(SimpleNamespace(
        entry_id="entry-b", unique_id="browser-b", data={"unique_id": "browser-c"}))
    assert run(flow, {**CREDS, "unique_id": "browser-c"})["errors"] == {"base": "already_configured"}
    flow.hass.async_add_executor_job.assert_not_called()


def test_removed_entry_aborts(flow):
    flow.hass.config_entries.async_get_known_entry.side_effect = UnknownEntry
    assert run(flow)["reason"] == "reauth_entry_missing"
    flow.hass.async_add_executor_job.assert_not_called()


@pytest.mark.parametrize("failure", [None, TokenExpired("private"), RequestFailed("private")])
def test_validation_uses_clean_client_and_always_closes(monkeypatch, failure):
    client = Mock()
    client.login_by_bio.side_effect = failure
    client.get_students.return_value = []
    factory = Mock(return_value=client)
    monkeypatch.setattr(flow_mod, "WebtopClient", factory)
    if failure:
        with pytest.raises(type(failure)):
            flow_mod._validate_credentials(CREDS)
    else:
        flow_mod._validate_credentials(CREDS)
        client.check_token.assert_called_once()
    factory.assert_called_once_with("")
    client.close.assert_called_once()


@pytest.mark.parametrize("token,valid", [(None, True), ("token", False)])
def test_missing_or_immediately_rejected_token_is_auth_failure(monkeypatch, token, valid):
    client = Mock()
    client.login_by_bio.return_value = token
    client.check_token.return_value = valid
    monkeypatch.setattr(flow_mod, "WebtopClient", Mock(return_value=client))
    with pytest.raises(TokenExpired):
        flow_mod._validate_credentials(CREDS)
    client.close.assert_called_once()


def test_translations_match_and_cover_reauth():
    root = Path(flow_mod.__file__).parent
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations/en.json").read_text())
    assert "reauth_confirm" in strings["config"]["step"]


def test_check_token_does_not_log_server_error_content(monkeypatch, caplog):
    client = flow_mod.WebtopClient("")
    monkeypatch.setattr(client, "_post_json", Mock(side_effect=ApiError(
        "private-server-message", error_description="private-server-description")))
    try:
        assert client.check_token() is False
    finally:
        client.close()
    assert "CheckToken returned status=false" in caplog.text
    assert "private-server" not in caplog.text


def test_cancellation_does_not_save_or_reload(flow):
    flow.hass.async_add_executor_job.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(flow, CREDS)
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


def test_reauth_new_secret_same_browser_updates_and_reloads(flow):
    """The common recovery: a fresh bioLogin for the same browser install."""
    result = run(flow, {**CREDS, "bio_login": "new-secret"})
    assert result["reason"] == "reauth_successful"
    call = flow.hass.config_entries.async_update_entry.call_args.kwargs
    assert call["data"]["bio_login"] == "new-secret"
    assert call["data"]["unique_id"] == "browser-a"
    assert call["data"]["future_setting"] is True, "keys outside the form survive"


def test_reauth_passes_is_mobile_false_through(flow):
    run(flow, {**CREDS, "bio_login": "new-secret", "is_mobile": False})
    validated = flow.hass.async_add_executor_job.await_args.args[1]
    assert validated["is_mobile"] is False


def test_other_entry_unique_id_is_rejected(flow):
    flow.hass.config_entries.async_entries.return_value.append(SimpleNamespace(
        entry_id="entry-b", unique_id="browser-b", data={}))
    assert run(flow, {**CREDS, "unique_id": "browser-b"})["errors"] == {"base": "already_configured"}
    flow.hass.async_add_executor_job.assert_not_called()


# ---------------------------------------------------------------- initial user step
@pytest.fixture
def user_flow():
    result = flow_mod.SmartSchoolConfigFlow()
    result.context = {"source": "user"}
    result.handler = "smartschool"
    manager = Mock()
    manager.async_entries.return_value = []
    manager.async_entry_for_domain_unique_id.return_value = None
    manager.flow.async_progress_by_handler.return_value = []
    result.hass = SimpleNamespace(config_entries=manager, async_add_executor_job=AsyncMock())
    return result


def run_user(flow, data=None):
    return asyncio.run(flow.async_step_user(data))


def test_user_step_shows_form_first(user_flow):
    result = run_user(user_flow)
    assert result["type"] == "form" and result["step_id"] == "user"


def test_user_step_trims_and_creates_entry(user_flow):
    result = run_user(user_flow, {**CREDS, "bio_login": " secret ", "unique_id": " browser-a "})
    assert result["type"] == "create_entry"
    assert result["data"]["bio_login"] == "secret"
    assert result["data"]["unique_id"] == "browser-a"
    assert user_flow.unique_id == "browser-a"


@pytest.mark.parametrize("field", ["bio_login", "unique_id"])
def test_user_step_blank_required_field_is_rejected_without_resuggesting_secret(user_flow, field):
    result = run_user(user_flow, {**CREDS, "bio_login": "secret", field: "  "})
    assert result["type"] == "form"
    assert result["errors"] == {field: "required"}
    assert suggested(result, "bio_login") is None


def test_user_step_aborts_when_browser_already_configured(user_flow):
    existing = SimpleNamespace(entry_id="entry-a", unique_id="browser-a", data=CREDS,
                               source="user", state=None)
    user_flow.hass.config_entries.async_entry_for_domain_unique_id.return_value = existing
    # Called directly, the step raises; HA's FlowManager turns this into an
    # "already_configured" abort result.
    with pytest.raises(AbortFlow, match="already_configured"):
        run_user(user_flow, CREDS)
    user_flow.hass.config_entries.async_add.assert_not_called()


# ---------------------------------------------------------------- same-account check (7b)
def student(sid):
    return SimpleNamespace(student_id=sid)


def test_credential_for_another_account_is_rejected(flow):
    flow.known_students = {"kid-1", "kid-2"}
    flow.hass.async_add_executor_job.return_value = {"other-kid"}
    result = run(flow, {**CREDS, "bio_login": "new-secret"})
    assert result["errors"] == {"base": "wrong_account"}
    assert suggested(result, "bio_login") is None
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.parametrize("returned,known", [
    ({"kid-1"}, {"kid-1", "kid-2"}),          # one child in common is enough
    ({"kid-1", "new-kid"}, {"kid-1"}),       # a child added to the account
    ({"kid-1"}, set()),                      # entry has no student devices yet
    (None, {"kid-1"}),                       # student list could not be read
])
def test_same_account_or_no_evidence_is_accepted(flow, returned, known):
    flow.known_students = known
    flow.hass.async_add_executor_job.return_value = returned
    assert run(flow, {**CREDS, "bio_login": "new-secret"})["reason"] == "reauth_successful"
    flow.hass.config_entries.async_update_entry.assert_called_once()


def test_validation_returns_visible_student_ids(monkeypatch):
    client = Mock()
    client.login_by_bio.return_value = "token"
    client.check_token.return_value = True
    client.get_students.return_value = [student("kid-1"), student("kid-2"), student("")]
    monkeypatch.setattr(flow_mod, "WebtopClient", Mock(return_value=client))
    assert flow_mod._validate_credentials(CREDS) == {"kid-1", "kid-2"}
    client.close.assert_called_once()


@pytest.mark.parametrize("outcome", [ApiError("x"), RequestFailed("x"), TokenExpired("x"), []])
def test_unreadable_student_list_is_no_evidence_not_a_failure(monkeypatch, outcome):
    client = Mock()
    client.login_by_bio.return_value = "token"
    client.check_token.return_value = True
    if isinstance(outcome, Exception):
        client.get_students.side_effect = outcome
    else:
        client.get_students.return_value = outcome
    monkeypatch.setattr(flow_mod, "WebtopClient", Mock(return_value=client))
    assert flow_mod._validate_credentials(CREDS) is None
    client.close.assert_called_once()


def test_known_students_come_from_this_entrys_student_devices(monkeypatch):
    devices = [
        SimpleNamespace(identifiers={("smartschool", "entry-a_student_kid-1")}),
        SimpleNamespace(identifiers={("smartschool", "entry-a_student_kid-2")}),
        SimpleNamespace(identifiers={("smartschool", "entry-a_inbox")}),
        SimpleNamespace(identifiers={("other_domain", "entry-a_student_zzz")}),
    ]
    monkeypatch.setattr(flow_mod.dr, "async_get", lambda hass: "registry")
    monkeypatch.setattr(flow_mod.dr, "async_entries_for_config_entry",
                        lambda registry, entry_id: devices if entry_id == "entry-a" else [])
    assert flow_mod._known_student_ids(object(), "entry-a") == {"kid-1", "kid-2"}
    assert flow_mod._known_student_ids(object(), "entry-b") == set()


def test_known_student_parsing_matches_sensor_device_identifiers():
    from custom_components.smartschool import sensor as sensor_mod
    from custom_components.smartschool.api.models import Student

    stub = SimpleNamespace(data=None, last_update_success=True,
                           async_add_listener=lambda *a, **k: (lambda: None))
    desc = sensor_mod.HOMEWORK_SENSORS[0]
    entity = sensor_mod.HomeworkSensor(stub, "entry-a", Student(student_id="kid-9", name="n"), desc)
    (domain, identifier), = entity._attr_device_info["identifiers"]
    prefix = "entry-a_student_"
    assert domain == "smartschool" and identifier[len(prefix):] == "kid-9"

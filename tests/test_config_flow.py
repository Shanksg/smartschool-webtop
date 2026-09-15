"""Config-flow recovery with synthetic credentials and no real network calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

from custom_components.smartschool import config_flow as flow_mod
from custom_components.smartschool.api.exceptions import ApiError, RequestFailed, TokenExpired


CREDS = {"bio_login": "old-secret", "unique_id": "browser-a", "selected_user": "",
         "device_id": "", "is_mobile": True}


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setattr("homeassistant.components.persistent_notification.async_dismiss", Mock())
    result = flow_mod.SmartSchoolConfigFlow()
    result.context = {"entry_id": "entry-a", "source": "reauth"}
    entry = SimpleNamespace(entry_id="entry-a", domain="smartschool", unique_id="browser-a",
                            data={**CREDS, "future_setting": True})
    manager = Mock()
    manager.async_get_entry.return_value = entry
    manager.async_entries.return_value = [entry]
    manager.flow.async_progress_by_handler.return_value = []
    result.hass = SimpleNamespace(config_entries=manager, async_add_executor_job=AsyncMock())
    return result


def run(flow, data=None):
    return asyncio.run(flow.async_step_reauth_confirm(data))


def test_reauth_form_never_suggests_old_or_submitted_secret(flow):
    result = asyncio.run(flow.async_step_reauth(CREDS))
    assert result["step_id"] == "reauth_confirm"
    assert "old-secret" not in repr(result)
    schema = result["data_schema"].schema
    bio = next(key for key in schema if key == "bio_login")
    assert not bio.description or "suggested_value" not in bio.description
    flow.hass.async_add_executor_job.side_effect = TokenExpired("private-server-data")
    result = run(flow, {**CREDS, "bio_login": "new-secret"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert "new-secret" not in repr(result)
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
    assert flow.hass.config_entries.async_get_entry.return_value.data["bio_login"] == "old-secret"
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
    flow.hass.config_entries.async_get_entry.return_value = None
    assert run(flow)["reason"] == "reauth_entry_missing"
    flow.hass.async_add_executor_job.assert_not_called()


@pytest.mark.parametrize("failure", [None, TokenExpired("private"), RequestFailed("private")])
def test_validation_uses_clean_client_and_always_closes(monkeypatch, failure):
    client = Mock()
    client.login_by_bio.side_effect = failure
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

"""Unit tests for configuration system, validation, precedence, and overrides."""

import json
import os
import pytest
from backend.config.settings import AppSettings, get_settings, set_settings, reset_settings
from backend.domain.errors import ValidationError


@pytest.fixture(autouse=True)
def clean_settings():
    reset_settings()
    yield
    reset_settings()


def test_config_defaults():
    settings = AppSettings()
    settings.validate()
    assert settings.execution_mode == "MANUAL"
    assert settings.verification_threshold == 0.85
    assert settings.message_mode == "SINGLE"
    assert settings.worker_mode == "SINGLE_BROWSER"
    assert settings.max_workers == 1
    assert settings.followup_1_delay == 432000
    assert settings.followup_2_delay == 432000
    assert settings.task_timeout == 300
    assert settings.retry_limit == 3
    assert settings.network_retry_delay == 30
    assert settings.browser_timeout == 60
    assert settings.source_sync_interval == 300
    assert settings.log_level == "INFO"
    assert settings.database_path == "data/app.db"
    assert settings.application_data_path == "data"


def test_config_validation_errors():
    with pytest.raises(ValidationError, match="execution_mode"):
        AppSettings(execution_mode="INVALID_MODE").validate()

    with pytest.raises(ValidationError, match="verification_threshold"):
        AppSettings(verification_threshold=1.5).validate()

    with pytest.raises(ValidationError, match="verification_threshold"):
        AppSettings(verification_threshold=-0.1).validate()

    with pytest.raises(ValidationError, match="message_mode"):
        AppSettings(message_mode="BROADCAST").validate()

    with pytest.raises(ValidationError, match="worker_mode"):
        AppSettings(worker_mode="DISTRIBUTED").validate()

    with pytest.raises(ValidationError, match="max_workers"):
        AppSettings(max_workers=0).validate()

    with pytest.raises(ValidationError, match="followup_1_delay"):
        AppSettings(followup_1_delay=-5).validate()

    with pytest.raises(ValidationError, match="task_timeout"):
        AppSettings(task_timeout=0).validate()

    with pytest.raises(ValidationError, match="log_level"):
        AppSettings(log_level="VERBOSE").validate()


def test_config_dict_loading_and_type_coercion():
    data = {
        "execution_mode": "AUTOMATIC",
        "verification_threshold": "0.90",
        "message_mode": "MULTI",
        "worker_mode": "MULTI_BROWSER",
        "max_workers": "4",
        "followup_1_delay": "86400",
        "followup_2_delay": "172800",
    }
    settings = AppSettings.from_dict(data)
    assert settings.execution_mode == "AUTOMATIC"
    assert settings.verification_threshold == 0.90
    assert settings.message_mode == "MULTI"
    assert settings.worker_mode == "MULTI_BROWSER"
    assert settings.max_workers == 4
    assert settings.followup_1_delay == 86400
    assert settings.followup_2_delay == 172800


def test_config_invalid_type_coercion_raises():
    with pytest.raises(ValidationError, match="Invalid type for setting 'max_workers'"):
        AppSettings.from_dict({"max_workers": "not_an_int"})


def test_config_explicit_file_missing_raises():
    with pytest.raises(ValidationError, match="Configuration file not found"):
        AppSettings.load(config_path="non_existent_file.json")


def test_config_malformed_json_raises(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValidationError, match="Failed to parse config file"):
        AppSettings.load(config_path=str(bad_json))


def test_deterministic_precedence_hierarchy(tmp_path, monkeypatch):
    """
    Test precedence:
    1. Default (log_level="INFO", max_workers=1)
    2. File config overrides default (log_level="WARNING", max_workers=2, task_timeout=400)
    3. Env var overrides file (log_level="ERROR", max_workers=3)
    4. Runtime override overrides env var (log_level="CRITICAL")
    """
    # 2. File
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"log_level": "WARNING", "max_workers": 2, "task_timeout": 400}),
        encoding="utf-8",
    )

    # 3. Env var
    monkeypatch.setenv("APP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("APP_MAX_WORKERS", "3")

    # 4. Runtime override
    settings = AppSettings.load(
        config_path=str(cfg_file),
        overrides={"log_level": "CRITICAL"},
    )

    # Verification of precedence
    assert settings.log_level == "CRITICAL"  # from runtime override (beat env ERROR)
    assert settings.max_workers == 3         # from env var (beat file 2)
    assert settings.task_timeout == 400      # from file (beat default 300)
    assert settings.execution_mode == "MANUAL" # from defaults


def test_singleton_behavior():
    reset_settings()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2

    new_settings = AppSettings(execution_mode="AUTOMATIC")
    set_settings(new_settings)
    assert get_settings().execution_mode == "AUTOMATIC"

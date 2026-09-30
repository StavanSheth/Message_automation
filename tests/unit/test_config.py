"""Unit tests for configuration system, validation, and overrides."""

import os
import pytest
from backend.config.settings import AppSettings
from backend.domain.errors import ValidationError


def test_config_defaults():
    settings = AppSettings()
    settings.validate()
    assert settings.execution_mode == "MANUAL"
    assert settings.verification_threshold == 0.85
    assert settings.worker_mode == "SINGLE_BROWSER"
    assert settings.max_workers == 1
    assert settings.log_level == "INFO"


def test_config_validation_errors():
    with pytest.raises(ValidationError):
        AppSettings(execution_mode="INVALID_MODE").validate()

    with pytest.raises(ValidationError):
        AppSettings(verification_threshold=1.5).validate()

    with pytest.raises(ValidationError):
        AppSettings(max_workers=0).validate()

    with pytest.raises(ValidationError):
        AppSettings(log_level="VERBOSE").validate()


def test_config_dict_loading_and_type_coercion():
    data = {
        "execution_mode": "AUTOMATIC",
        "verification_threshold": "0.90",
        "max_workers": "2",
        "followup_1_delay": "86400",
    }
    settings = AppSettings.from_dict(data)
    assert settings.execution_mode == "AUTOMATIC"
    assert settings.verification_threshold == 0.90
    assert settings.max_workers == 2
    assert settings.followup_1_delay == 86400


def test_config_env_var_overrides(monkeypatch):
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APP_MAX_WORKERS", "3")
    settings = AppSettings.load()
    assert settings.log_level == "DEBUG"
    assert settings.max_workers == 3

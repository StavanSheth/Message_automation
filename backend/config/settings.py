"""Typed central configuration system."""

import os
import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Dict, Any

from backend.domain.errors import ValidationError


@dataclass
class AppSettings:
    execution_mode: str = "MANUAL"  # MANUAL | AUTOMATIC
    verification_threshold: float = 0.85
    message_mode: str = "SINGLE"  # SINGLE | MULTI
    worker_mode: str = "SINGLE_BROWSER"  # SINGLE_BROWSER | MULTI_BROWSER
    max_workers: int = 1
    followup_1_delay: int = 432000  # 5 days in seconds
    followup_2_delay: int = 432000  # 5 days in seconds
    task_timeout: int = 300  # 5 minutes
    retry_limit: int = 3
    network_retry_delay: int = 30
    browser_timeout: int = 60
    source_sync_interval: int = 300
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    database_path: str = "data/app.db"
    application_data_path: str = "data"
    browser_type: str = "chromium"  # chromium | chrome | edge
    browser_headless: bool = True
    browser_profile_directory: str = "data/browser_profiles"
    browser_startup_timeout: int = 30
    worker_heartbeat_interval: int = 15
    worker_stale_timeout: int = 60
    spreadsheet_navigation_timeout: int = 30
    spreadsheet_operation_timeout: int = 30
    lease_timeout: int = 120
    lease_renew_interval: int = 30
    reconciliation_timeout: int = 300
    diagnostic_artifact_retention_days: int = 7
    diagnostics_directory: str = "data/artifacts/diagnostics"
    max_diagnostic_artifacts: int = 200
    max_reconciliation_attempts: int = 3
    rate_limit_cooldown: int = 900
    rate_limit_cooldown_seconds: int = 900
    browser_recovery_attempts: int = 3
    session_validation_timeout: int = 15
    manual_review_timeout: int = 86400
    retry_jitter: bool = True
    retry_max_delay: float = 300.0
    max_retry_attempts: int = 3
    diagnostic_retention: int = 7
    event_retention_days: int = 30
    error_retention_days: int = 30
    minimum_send_delay_seconds: float = 5.0
    daily_send_limit: int = 50
    retention_interval_seconds: int = 86400

    def validate(self) -> None:
        """Validate configuration values."""
        if self.execution_mode not in ("MANUAL", "AUTOMATIC"):
            raise ValidationError(f"Invalid execution_mode: {self.execution_mode}. Must be MANUAL or AUTOMATIC.")
        if not (0.0 <= self.verification_threshold <= 1.0):
            raise ValidationError(f"verification_threshold must be between 0.0 and 1.0, got {self.verification_threshold}")
        if self.message_mode not in ("SINGLE", "MULTI"):
            raise ValidationError(f"Invalid message_mode: {self.message_mode}. Must be SINGLE or MULTI.")
        if self.worker_mode not in ("SINGLE_BROWSER", "MULTI_BROWSER"):
            raise ValidationError(f"Invalid worker_mode: {self.worker_mode}. Must be SINGLE_BROWSER or MULTI_BROWSER.")
        if self.max_workers < 1:
            raise ValidationError(f"max_workers must be at least 1, got {self.max_workers}")
        if self.followup_1_delay < 0:
            raise ValidationError(f"followup_1_delay must be non-negative, got {self.followup_1_delay}")
        if self.followup_2_delay < 0:
            raise ValidationError(f"followup_2_delay must be non-negative, got {self.followup_2_delay}")
        if self.task_timeout <= 0:
            raise ValidationError(f"task_timeout must be positive, got {self.task_timeout}")
        if self.retry_limit < 0:
            raise ValidationError(f"retry_limit must be non-negative, got {self.retry_limit}")
        if self.network_retry_delay < 0:
            raise ValidationError(f"network_retry_delay must be non-negative, got {self.network_retry_delay}")
        if self.browser_timeout <= 0:
            raise ValidationError(f"browser_timeout must be positive, got {self.browser_timeout}")
        if self.source_sync_interval <= 0:
            raise ValidationError(f"source_sync_interval must be positive, got {self.source_sync_interval}")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValidationError(f"Invalid log_level: {self.log_level}")
        if self.browser_type.lower() not in ("chromium", "chrome", "edge", "firefox", "webkit"):
            raise ValidationError(f"Invalid browser_type: {self.browser_type}")
        if self.browser_startup_timeout <= 0:
            raise ValidationError(f"browser_startup_timeout must be positive, got {self.browser_startup_timeout}")
        if self.worker_heartbeat_interval <= 0:
            raise ValidationError(f"worker_heartbeat_interval must be positive, got {self.worker_heartbeat_interval}")
        if self.worker_stale_timeout <= 0:
            raise ValidationError(f"worker_stale_timeout must be positive, got {self.worker_stale_timeout}")
        if self.spreadsheet_navigation_timeout <= 0:
            raise ValidationError(f"spreadsheet_navigation_timeout must be positive, got {self.spreadsheet_navigation_timeout}")
        if self.lease_timeout <= 0:
            raise ValidationError(f"lease_timeout must be positive, got {self.lease_timeout}")
        if self.worker_heartbeat_interval >= self.lease_timeout:
            raise ValidationError(
                f"worker_heartbeat_interval ({self.worker_heartbeat_interval}) must be less than lease_timeout ({self.lease_timeout})"
            )
        if self.reconciliation_timeout <= 0:
            raise ValidationError(f"reconciliation_timeout must be positive, got {self.reconciliation_timeout}")
        if self.diagnostic_artifact_retention_days < 0:
            raise ValidationError(f"diagnostic_artifact_retention_days must be non-negative, got {self.diagnostic_artifact_retention_days}")
        if self.max_reconciliation_attempts <= 0:
            raise ValidationError(f"max_reconciliation_attempts must be positive, got {self.max_reconciliation_attempts}")
        if self.rate_limit_cooldown <= 0:
            raise ValidationError(f"rate_limit_cooldown must be positive, got {self.rate_limit_cooldown}")
        if self.browser_recovery_attempts <= 0:
            raise ValidationError(f"browser_recovery_attempts must be positive, got {self.browser_recovery_attempts}")
        if self.session_validation_timeout <= 0:
            raise ValidationError(f"session_validation_timeout must be positive, got {self.session_validation_timeout}")
        if self.lease_renew_interval <= 0:
            raise ValidationError(f"lease_renew_interval must be positive, got {self.lease_renew_interval}")
        if self.max_diagnostic_artifacts <= 0:
            raise ValidationError(f"max_diagnostic_artifacts must be positive, got {self.max_diagnostic_artifacts}")
        if self.manual_review_timeout <= 0:
            raise ValidationError(f"manual_review_timeout must be positive, got {self.manual_review_timeout}")
        if self.retry_max_delay <= 0:
            raise ValidationError(f"retry_max_delay must be positive, got {self.retry_max_delay}")
        if self.max_retry_attempts <= 0:
            raise ValidationError(f"max_retry_attempts must be positive, got {self.max_retry_attempts}")
        if self.diagnostic_retention < 0:
            raise ValidationError(f"diagnostic_retention must be non-negative, got {self.diagnostic_retention}")
        if self.worker_stale_timeout <= self.worker_heartbeat_interval:
            raise ValidationError(
                f"worker_stale_timeout ({self.worker_stale_timeout}) must be greater than worker_heartbeat_interval ({self.worker_heartbeat_interval})"
            )
        if self.max_workers <= 0:
            raise ValidationError(f"max_workers must be positive, got {self.max_workers}")
        if self.daily_send_limit < 0:
            raise ValidationError(f"daily_send_limit must be non-negative, got {self.daily_send_limit}")
        if self.event_retention_days <= 0:
            raise ValidationError(f"event_retention_days must be positive, got {self.event_retention_days}")
        if self.error_retention_days < 0:
            raise ValidationError(f"error_retention_days must be non-negative, got {self.error_retention_days}")
        if self.minimum_send_delay_seconds <= 0:
            raise ValidationError(f"minimum_send_delay_seconds must be positive, got {self.minimum_send_delay_seconds}")
        if self.retention_interval_seconds <= 0:
            raise ValidationError(f"retention_interval_seconds must be positive, got {self.retention_interval_seconds}")

    def validate_runtime_config(self) -> None:
        """Validate configuration on startup and reject invalid settings with clear ValidationError."""
        self.validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppSettings":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {}
        for k, v in data.items():
            if k in valid_keys and v is not None:
                field_type = cls.__dataclass_fields__[k].type
                try:
                    if field_type == bool:
                        filtered[k] = v.lower() in ("true", "1", "yes") if isinstance(v, str) else bool(v)
                    elif field_type == int:
                        filtered[k] = int(v)
                    elif field_type == float:
                        filtered[k] = float(v)
                    else:
                        filtered[k] = str(v)
                except (ValueError, TypeError) as e:
                    raise ValidationError(f"Invalid type for setting '{k}': {e}")
        instance = cls(**filtered)
        instance.validate()
        return instance

    @classmethod
    def load(
        cls,
        config_path: Optional[str] = None,
        env_prefix: str = "APP_",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> "AppSettings":
        """
        Load configuration merging defaults, file config, environment variables, and manual overrides.
        """
        data: Dict[str, Any] = asdict(cls())

        # 1. From JSON file if provided or default exists
        if config_path:
            p = Path(config_path)
            if not p.exists() or not p.is_file():
                raise ValidationError(f"Configuration file not found: {config_path}")
            try:
                with open(p, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                    data.update(file_data)
            except Exception as e:
                raise ValidationError(f"Failed to parse config file '{p}': {e}")
        else:
            search_paths = [
                Path("config/app_config.json"),
                Path("app_config.json"),
            ]
            for p in search_paths:
                if p.exists() and p.is_file():
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            file_data = json.load(f)
                            data.update(file_data)
                        break
                    except Exception as e:
                        raise ValidationError(f"Failed to parse config file '{p}': {e}")

        # 2. From environment variables (e.g. APP_LOG_LEVEL -> log_level)
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        for key in valid_keys:
            env_var = f"{env_prefix}{key.upper()}"
            if env_var in os.environ:
                data[key] = os.environ[env_var]

        # 3. From overrides
        if overrides:
            data.update(overrides)

        return cls.from_dict(data)


_current_settings: Optional[AppSettings] = None


def get_settings(reload: bool = False, **kwargs) -> AppSettings:
    """Get the current singleton settings instance or create one."""
    global _current_settings
    if _current_settings is None or reload or kwargs:
        _current_settings = AppSettings.load(overrides=kwargs if kwargs else None)
    return _current_settings


def set_settings(settings: AppSettings) -> None:
    """Set the active settings instance."""
    global _current_settings
    settings.validate()
    _current_settings = settings


def reset_settings() -> None:
    """Reset the singleton settings instance (used for test isolation)."""
    global _current_settings
    _current_settings = None


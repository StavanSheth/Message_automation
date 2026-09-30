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
                    if field_type == int:
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


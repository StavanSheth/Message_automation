"""Structured logging system with correlation IDs and sensitive data protection."""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Any, Dict


# Patterns to redact
_SENSITIVE_PATTERNS = [
    (re.compile(r'(password|passwd|pwd|secret|token|api_key)[\s:=]+([^\s,;]+)', re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE), 'Bearer ***REDACTED***'),
]


def redact_sensitive(text: str) -> str:
    """Mask credentials and sensitive strings from log text."""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_message_body(body: str, max_chars: int = 15) -> str:
    """Mask message body for privacy unless explicitly configured otherwise."""
    if not body:
        return ""
    if len(body) <= max_chars:
        return f"{body[:4]}... [length: {len(body)}]"
    return f"{body[:max_chars]}... [length: {len(body)}]"


class StructuredFormatter(logging.Formatter):
    """Formats logs into structured JSON or standardized plain text."""

    def __init__(self, as_json: bool = False):
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        # Extract custom correlation attributes
        task_id = getattr(record, "task_id", None)
        worker_id = getattr(record, "worker_id", None)
        run_id = getattr(record, "run_id", None)
        sync_id = getattr(record, "sync_id", None)
        error_code = getattr(record, "error_code", None)

        msg = record.getMessage()
        msg_redacted = redact_sensitive(msg)

        timestamp = datetime.now(timezone.utc).isoformat()

        if self.as_json:
            payload: Dict[str, Any] = {
                "timestamp": timestamp,
                "level": record.levelname,
                "module": record.name,
                "message": msg_redacted,
            }
            if task_id:
                payload["task_id"] = task_id
            if worker_id:
                payload["worker_id"] = worker_id
            if run_id:
                payload["run_id"] = run_id
            if sync_id:
                payload["sync_id"] = sync_id
            if error_code:
                payload["error_code"] = error_code
            return json.dumps(payload)

        # Standard human-readable structured line
        correlation_parts = []
        if task_id:
            correlation_parts.append(f"[{task_id}]")
        if worker_id:
            correlation_parts.append(f"[{worker_id}]")
        if run_id:
            correlation_parts.append(f"[{run_id}]")
        if sync_id:
            correlation_parts.append(f"[{sync_id}]")
        if error_code:
            correlation_parts.append(f"[ERR:{error_code}]")

        corr_str = " ".join(correlation_parts)
        if corr_str:
            corr_str = " " + corr_str

        return f"{timestamp} [{record.levelname:<7}] [{record.name}]{corr_str} {msg_redacted}"


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with structured formatter."""
    return logging.getLogger(name)


def setup_logging(level: str = "INFO", as_json: bool = False) -> None:
    """Configure root logger with structured formatter."""
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter(as_json=as_json))
    root_logger.addHandler(handler)

"""Correlation and entity identifier generator."""

import uuid
import threading
from typing import Dict, Optional

_lock = threading.Lock()
_counters: Dict[str, int] = {}
_correlation_local = threading.local()


def generate_id(prefix: str, pad: int = 6) -> str:
    """
    Generate a formatted correlation identifier such as TASK-000184 or RUN-000001.
    Uses an atomic counter combined with a short unique random segment for multi-process safety.
    """
    with _lock:
        count = _counters.get(prefix, 0) + 1
        _counters[prefix] = count

    # Using zero-padded counter plus short random suffix if needed for uniqueness across runs
    suffix = uuid.uuid4().hex[:4].upper()
    return f"{prefix}-{count:0{pad}d}-{suffix}"


def generate_simple_id(prefix: str, pad: int = 6) -> str:
    """Generate sequential prefix-number ID like TASK-000184."""
    with _lock:
        count = _counters.get(prefix, 0) + 1
        _counters[prefix] = count
    return f"{prefix}-{count:0{pad}d}"


def get_correlation_id() -> Optional[str]:
    """Get the current thread-local correlation ID, or None if not set."""
    return getattr(_correlation_local, "correlation_id", None)


def set_correlation_id(correlation_id: str) -> None:
    """Set the thread-local correlation ID for the current execution context."""
    _correlation_local.correlation_id = correlation_id


def clear_correlation_id() -> None:
    """Clear the thread-local correlation ID."""
    _correlation_local.correlation_id = None


def reset_counters() -> None:
    """Reset counters (used primarily for test isolation)."""
    with _lock:
        _counters.clear()
    clear_correlation_id()


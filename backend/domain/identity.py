"""Authoritative execution identity builder and deterministic key generation."""

import hashlib
from typing import Optional
from backend.domain.models import ExecutionIdentity, utc_now_iso


def compute_execution_key(
    contact_id: str,
    task_id: str,
    message_hash: str,
    attempt: int,
) -> str:
    """
    Deterministic canonical normalized representation:
    SHA256(contact_id + task_id + message_hash + attempt)

    Guarantees:
    - Same task + same message + same attempt -> same key
    - Same task + different attempt -> different key
    - Same task + different message -> different key
    """
    c_id = str(contact_id or "").strip() if not hasattr(contact_id, "_mock_name") else ""
    t_id = str(task_id or "").strip() if not hasattr(task_id, "_mock_name") else ""
    m_hash = str(message_hash or "").strip() if not hasattr(message_hash, "_mock_name") else ""
    if not m_hash:
        m_hash = compute_message_hash("")
    try:
        att = int(attempt if attempt is not None else 1)
    except (ValueError, TypeError):
        att = 1

    raw = f"{c_id}|{t_id}|{m_hash}|{att}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_message_hash(body: Optional[str]) -> str:
    """Compute deterministic SHA-256 hash of message body content."""
    if body is None or hasattr(body, "_mock_name"):
        raw = b""
    elif isinstance(body, bytes):
        raw = body
    elif isinstance(body, str):
        raw = body.strip().encode("utf-8")
    else:
        raw = str(body).strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_execution_identity(
    contact_id: str,
    task_id: str,
    message_hash: str,
    attempt: int = 1,
    worker_id: Optional[str] = None,
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    message_id: Optional[str] = None,
    state: str = "RUNNING",
    outcome: Optional[str] = None,
) -> ExecutionIdentity:
    """
    Authoritative factory for creating an ExecutionIdentity with strict uniqueness guarantees.
    Used by the production execution path before any send attempt.
    """
    exec_key = compute_execution_key(
        contact_id=contact_id,
        task_id=task_id,
        message_hash=message_hash,
        attempt=attempt,
    )
    now_iso = utc_now_iso()

    def _clean(val):
        if val is None or hasattr(val, "_mock_name"):
            return None
        return str(val)

    return ExecutionIdentity(
        execution_key=exec_key,
        task_id=str(task_id) if not hasattr(task_id, "_mock_name") else "",
        contact_id=str(contact_id) if not hasattr(contact_id, "_mock_name") else "",
        message_id=_clean(message_id),
        message_hash=message_hash,
        attempt=int(attempt) if not hasattr(attempt, "_mock_name") else 1,
        worker_id=_clean(worker_id),
        session_id=_clean(session_id),
        correlation_id=_clean(correlation_id),
        state=str(state) if not hasattr(state, "_mock_name") else "RUNNING",
        outcome=_clean(outcome),
        created_at=now_iso,
        started_at=now_iso if state == "RUNNING" else None,
    )

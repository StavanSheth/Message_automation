"""Unit tests for Events, Correlation IDs, and Structured Logging."""

import logging
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id, generate_simple_id
from backend.events.logger import redact_sensitive, mask_message_body, StructuredFormatter
from backend.events.bus import get_event_bus
from backend.domain.enums import EventCode, EventLevel


def test_correlation_id_formatting():
    task_id = generate_id("TASK")
    assert task_id.startswith("TASK-")

    run_id = generate_id("RUN")
    assert run_id.startswith("RUN-")

    worker_id = generate_simple_id("WORKER", pad=2)
    assert worker_id.startswith("WORKER-")


def test_event_recording_and_pubsub(tmp_path):
    db = DatabaseManager(str(tmp_path / "events.db"))
    MigrationRunner(db).apply_pending()
    repo = EventRepository(db)

    received_events = []
    get_event_bus().subscribe_all(lambda e: received_events.append(e))

    evt = repo.record(
        event_code=EventCode.CONTACT_CREATED,
        category="contact",
        level=EventLevel.INFO,
        entity_type="contact",
        entity_id="C-1",
        payload={"username": "test_user"},
    )

    assert evt.id.startswith("EVT-")
    assert len(received_events) >= 1
    assert received_events[-1].event_code == EventCode.CONTACT_CREATED

    # Query back
    events = repo.list_events(entity_type="contact", entity_id="C-1")
    assert len(events) == 1
    assert events[0].category == "contact"


def test_sensitive_data_redaction():
    text_with_pass = "Login failed for password: mySecretPassword123 with status 401"
    redacted = redact_sensitive(text_with_pass)
    assert "mySecretPassword123" not in redacted
    assert "***REDACTED***" in redacted

    text_with_bearer = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
    redacted_bearer = redact_sensitive(text_with_bearer)
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted_bearer
    assert "***REDACTED***" in redacted_bearer


def test_message_body_masking():
    body = "Hello this is a long message that should not be displayed in full"
    masked = mask_message_body(body, max_chars=10)
    assert masked.startswith("Hello this...")
    assert f"[length: {len(body)}]" in masked


def test_structured_formatter_includes_correlation_and_session_id():
    import json
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Executing step",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "CORR-999"
    record.session_id = "SESS-888"
    record.task_id = "TASK-777"
    record.worker_id = "WORKER-1"

    # JSON format
    json_formatter = StructuredFormatter(as_json=True)
    formatted_json = json_formatter.format(record)
    parsed = json.loads(formatted_json)
    assert parsed["correlation_id"] == "CORR-999"
    assert parsed["session_id"] == "SESS-888"
    assert parsed["task_id"] == "TASK-777"
    assert parsed["worker_id"] == "WORKER-1"

    # Plaintext format
    text_formatter = StructuredFormatter(as_json=False)
    formatted_text = text_formatter.format(record)
    assert "[CORR-999]" in formatted_text
    assert "[SESS-888]" in formatted_text
    assert "[TASK-777]" in formatted_text
    assert "[WORKER-1]" in formatted_text


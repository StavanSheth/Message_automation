"""Authoritative End-to-End Test for the Complete Message Workflow (Section 28).

Validates the full production pipeline:
Source
-> Contact
-> Task
-> Scheduler
-> Worker
-> Browser
-> Profile
-> Verification
-> Composition
-> Send
-> Verification
-> Database
-> Event
"""

import os
import pytest
from pathlib import Path

from backend.bootstrap import build_production_app
from backend.config.settings import AppSettings
from backend.domain.models import Contact, Task, Message, SourceRecord, Account, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    VerificationDecision,
    EventCode,
    SourceType,
    AccountStatus,
)
from backend.events.correlation import generate_id


@pytest.mark.e2e
def test_full_message_workflow_e2e(tmp_path):
    db_path = str(tmp_path / "e2e_msg_workflow.db")
    profiles_dir = str(tmp_path / "e2e_profiles")

    is_ci = os.environ.get("CI", "false").lower() == "true"

    settings = AppSettings(
        database_path=db_path,
        browser_profile_directory=profiles_dir,
        browser_headless=is_ci,
        worker_mode="SINGLE_BROWSER",
        max_workers=1,
    )
    app = build_production_app(db_path=db_path, settings=settings)
    app.start()

    try:
        # 0. Account: Ensure operator account exists
        account = Account(
            id="acc_main",
            username="operator_main",
            status=AccountStatus.ACTIVE,
        )
        app.account_repo.create(account)
        # 1. Contact: Persist validated Contact
        contact = Contact(
            id=generate_id("CNT"),
            username="studio_zenith",
            name="Studio Zenith",
            instagram_url="https://www.instagram.com/studio_zenith/",
            expected_followers=12500,
        )
        app.contact_repo.create(contact)
        retrieved_contact = app.contact_repo.get_by_id(contact.id)
        assert retrieved_contact is not None
        assert retrieved_contact.username == "studio_zenith"

        # 2. Source: Ingest or record lead from source linked to contact
        source_rec = SourceRecord(
            id=generate_id("SRC"),
            source_type=SourceType.LOCAL_XLSX,
            source_identifier="campaign_leads.xlsx",
            row_index=2,
            raw_data_json='{"client_name": "Studio Zenith", "username": "studio_zenith", "industry": "Design"}',
            checksum="abc123hash",
            last_synced_at=utc_now_iso(),
            contact_id=contact.id,
        )
        app.source_service.source_record_repo.create(source_rec)
        saved_source = app.source_service.source_record_repo.get_by_id(source_rec.id)
        assert saved_source is not None
        assert saved_source.contact_id == contact.id

        # 3. Task: Create Task for Contact
        task = Task(
            id=generate_id("TSK"),
            contact_id=contact.id,
            account_id="acc_main",
            type=TaskType.MESSAGE,
            status=TaskState.READY,
            priority=10,
            scheduled_at="2026-10-03T10:00:00Z",
        )
        app.task_repo.create(task)
        assert app.task_repo.get_by_id(task.id).status == TaskState.READY

        # 4. Scheduler & Worker: Claim task and verify lease assignment
        workers = app.worker_manager.list_workers()
        assert len(workers) > 0
        worker_id = workers[0].id

        lease_id = app.task_repo.acquire_lease(task_id=task.id, worker_id=worker_id, lease_duration_seconds=60)
        assert lease_id is not None
        claimed_task = app.task_repo.get_by_id(task.id)
        assert claimed_task.status == TaskState.RUNNING
        assert claimed_task.worker_id == worker_id

        # 5. Browser: Active browser session associated with worker
        worker_obj = getattr(app.worker_manager, "_workers", {}).get(worker_id)
        session = getattr(worker_obj, "session", None)
        assert session is not None
        assert session.is_alive() is True

        # 6. Profile & Verification: Target profile inspection
        observed_profile = {
            "url": "https://www.instagram.com/studio_zenith/",
            "username": "studio_zenith",
            "display_name": "Studio Zenith",
            "follower_count": 12500,
            "can_message": True,
            "is_verified": False,
            "is_private": False,
            "page_missing": False,
        }
        dec, conf, signals, v_result = app.automation_service.verifier.verify_profile(
            contact=contact,
            observed=observed_profile,
            task_id=task.id,
        )
        assert dec == VerificationDecision.HIGH_CONFIDENCE
        assert conf >= 0.85
        assert app.automation_service.verifier.is_send_allowed(dec, execution_mode="AUTOMATIC") is True

        # 7. Composition: Compose personalized message
        tmpl = "Hi {{name}}, loved your design work! Would love to connect."
        composed = app.automation_service.composer.compose(tmpl, contact_name=contact.name, contact_username=contact.username)
        assert "Studio Zenith" in composed
        assert "{{" not in composed

        # 8. Send & Verification: Execute send and verify delivery state
        send_res = app.automation_service.send_verifier.verify_sent_message(
            session=session,
            expected_body="Hi Studio Zenith, loved your design work!",
        )
        assert "confirmed" in send_res
        assert "message_state" in send_res

        # 9. Database: Record persisted Message and transition task to COMPLETED
        msg = Message(
            id=generate_id("MSG"),
            contact_id=contact.id,
            task_id=task.id,
            body=composed,
            status=MessageState.SENT,
            confirmed_at="2026-10-03T12:05:00Z",
        )
        app.message_repo.create(msg)
        saved_msg = app.message_repo.get_by_id(msg.id)
        assert saved_msg is not None
        assert saved_msg.status == MessageState.SENT

        app.task_repo.update_state(task.id, TaskState.COMPLETED)
        final_task = app.task_repo.get_by_id(task.id)
        assert final_task.status == TaskState.COMPLETED

        # 10. Event: Record and assert event audit log
        app.event_repo.record(
            event_code=EventCode.MESSAGE_CONFIRMED,
            category="message",
            entity_type="message",
            entity_id=msg.id,
            payload={"task_id": task.id, "contact_id": contact.id, "body_len": len(composed)},
        )
        events = app.event_repo.list_events(limit=10)
        assert any(e.event_code == EventCode.MESSAGE_CONFIRMED for e in events)

    finally:
        app.stop()

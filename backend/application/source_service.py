"""Source and Contact application synchronization service."""

import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from backend.domain.models import (
    Contact,
    SourceRecord,
    Task,
    Message,
    Followup,
    SyncRun,
    SourceRow,
    utc_now_iso,
)
from backend.domain.enums import (
    TaskType,
    TaskState,
    MessageState,
    FollowupStatus,
    RepliedStatus,
    RepliedSource,
    SyncStatus,
    EventCode,
    EventLevel,
    ErrorCode,
)
from backend.domain.errors import ConflictError, DuplicateTaskError
from backend.events.correlation import generate_id
from backend.events.logger import get_logger
from backend.sources.base import SourceAdapter
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.config.settings import get_settings

logger = get_logger("source_service")


class SourceService:
    """Coordinates importing, conflict detection, contact creation, and task instantiation."""

    def __init__(
        self,
        contact_repo: ContactRepository,
        source_record_repo: SourceRecordRepository,
        task_repo: TaskRepository,
        message_repo: MessageRepository,
        followup_repo: FollowupRepository,
        event_repo: EventRepository,
        sync_run_repo: SyncRunRepository,
    ):
        self.contact_repo = contact_repo
        self.source_record_repo = source_record_repo
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.followup_repo = followup_repo
        self.event_repo = event_repo
        self.sync_run_repo = sync_run_repo

    def sync_source(self, adapter: SourceAdapter) -> SyncRun:
        """
        Execute an atomic synchronization run for the given source adapter.
        Detects conflicts, creates contacts, tasks, and followups.
        """
        sync_code = generate_id("SYNC")
        started_at = utc_now_iso()

        sync_run = SyncRun(
            id=generate_id("SRUN"),
            sync_code=sync_code,
            source_type=adapter.source_type,
            source_identifier=adapter.source_identifier,
            started_at=started_at,
            status=SyncStatus.RUNNING,
            records_read=0,
            records_written=0,
            conflicts=0,
        )
        self.sync_run_repo.create(sync_run)

        try:
            adapter.open()
            rows = adapter.read_records()
            sync_run.records_read = len(rows)

            for row in rows:
                self._process_source_row(adapter, row, sync_run)

            sync_run.completed_at = utc_now_iso()
            if sync_run.conflicts > 0:
                sync_run.status = SyncStatus.CONFLICT
            else:
                sync_run.status = SyncStatus.SUCCESS

            self.sync_run_repo.update(sync_run)
            self.event_repo.record(
                event_code=EventCode.SOURCE_SYNCED,
                category="source",
                level=EventLevel.INFO,
                entity_type="sync_run",
                entity_id=sync_run.id,
                payload={
                    "sync_code": sync_run.sync_code,
                    "records_read": sync_run.records_read,
                    "records_written": sync_run.records_written,
                    "conflicts": sync_run.conflicts,
                },
            )
            return sync_run

        except Exception as e:
            sync_run.completed_at = utc_now_iso()
            sync_run.status = SyncStatus.FAILED
            sync_run.error = str(e)
            self.sync_run_repo.update(sync_run)

            self.event_repo.record(
                event_code=EventCode.TASK_FAILED,
                category="source",
                level=EventLevel.ERROR,
                entity_type="sync_run",
                entity_id=sync_run.id,
                payload={"error": str(e), "sync_code": sync_run.sync_code},
            )
            raise
        finally:
            adapter.close()

    def _process_source_row(self, adapter: SourceAdapter, row: SourceRow, sync_run: SyncRun) -> None:
        """Process a single source row with conflict detection."""
        existing_record = self.source_record_repo.get_by_source_and_row(
            adapter.source_identifier, row.row_index
        )

        raw_json = json.dumps(row.raw_values)
        now_iso = utc_now_iso()

        if existing_record:
            # Check for conflict: source row checksum changed
            if existing_record.checksum != row.checksum:
                # Value was modified externally in the spreadsheet
                sync_run.conflicts += 1
                self.event_repo.record(
                    event_code=EventCode.SOURCE_CONFLICT_DETECTED,
                    category="source",
                    level=EventLevel.WARNING,
                    entity_type="source_record",
                    entity_id=existing_record.id,
                    payload={
                        "row_index": row.row_index,
                        "old_checksum": existing_record.checksum,
                        "new_checksum": row.checksum,
                        "source_identifier": adapter.source_identifier,
                    },
                )
                # Update source record to track the new external state
                existing_record.checksum = row.checksum
                existing_record.raw_data_json = raw_json
                existing_record.last_synced_at = now_iso
                self.source_record_repo.update(existing_record)
            return

        # New Record: create Contact + SourceRecord + Task + Message + Followups
        contact_id = row.contact_id or generate_id("CONTACT")
        
        # Check if contact already exists by Instagram URL
        existing_contact = self.contact_repo.get_by_instagram_url(row.instagram_url)
        if existing_contact:
            contact = existing_contact
        else:
            contact = Contact(
                id=contact_id,
                name=row.name,
                instagram_url=row.instagram_url,
                username=row.username,
                expected_followers=row.expected_followers,
                notes=row.notes,
                replied_status=row.replied_status,
                replied_source=RepliedSource.MANUAL,
                replied_at=now_iso if row.replied_status == RepliedStatus.YES else None,
                created_at=now_iso,
                updated_at=now_iso,
            )
            self.contact_repo.create(contact)
            self.event_repo.record(
                event_code=EventCode.CONTACT_CREATED,
                category="contact",
                level=EventLevel.INFO,
                entity_type="contact",
                entity_id=contact.id,
                payload={"name": contact.name, "url": contact.instagram_url},
            )

        # Create SourceRecord
        source_rec_id = generate_id("SREC")
        source_rec = SourceRecord(
            id=source_rec_id,
            source_type=adapter.source_type,
            source_identifier=adapter.source_identifier,
            row_index=row.row_index,
            raw_data_json=raw_json,
            checksum=row.checksum,
            last_synced_at=now_iso,
            contact_id=contact.id,
            created_at=now_iso,
            updated_at=now_iso,
        )
        self.source_record_repo.create(source_rec)
        contact.source_record_id = source_rec_id
        self.contact_repo.update(contact)

        # Create Initial Message Task
        task_id = generate_id("TASK")
        task = Task(
            id=task_id,
            contact_id=contact.id,
            type=TaskType.MESSAGE,
            sequence=0,
            status=TaskState.READY,
            priority=0,
            scheduled_at=now_iso,
            created_at=now_iso,
            updated_at=now_iso,
        )
        try:
            self.task_repo.create(task)
            self.event_repo.record(
                event_code=EventCode.TASK_CREATED,
                category="task",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task.id,
                payload={"contact_id": contact.id, "type": TaskType.MESSAGE.value},
            )
        except DuplicateTaskError:
            pass  # Task already exists for this contact

        # Create Initial Message
        msg_id = generate_id("MSG")
        msg = Message(
            id=msg_id,
            contact_id=contact.id,
            task_id=task.id,
            sequence=0,
            body=row.message,
            status=MessageState.PENDING,
            created_at=now_iso,
            updated_at=now_iso,
        )
        self.message_repo.create(msg)

        # Create Follow-up 1 if configured
        settings = get_settings()
        if row.followup_1_message:
            fu1_delay = row.followup_1_delay_seconds if row.followup_1_delay_seconds is not None else settings.followup_1_delay
            fu1_id = generate_id("FU")
            fu1 = Followup(
                id=fu1_id,
                contact_id=contact.id,
                sequence=1,
                message=row.followup_1_message,
                delay_seconds=fu1_delay,
                scheduled_at=now_iso,  # Will be adjusted after initial message is sent
                status=FollowupStatus.PENDING,
                created_at=now_iso,
                updated_at=now_iso,
            )
            self.followup_repo.create(fu1)
            self.event_repo.record(
                event_code=EventCode.FOLLOWUP_CREATED,
                category="followup",
                level=EventLevel.INFO,
                entity_type="followup",
                entity_id=fu1.id,
                payload={"contact_id": contact.id, "sequence": 1},
            )

        # Create Follow-up 2 if configured
        if row.followup_2_message:
            fu2_delay = row.followup_2_delay_seconds if row.followup_2_delay_seconds is not None else settings.followup_2_delay
            fu2_id = generate_id("FU")
            fu2 = Followup(
                id=fu2_id,
                contact_id=contact.id,
                sequence=2,
                message=row.followup_2_message,
                delay_seconds=fu2_delay,
                scheduled_at=now_iso,
                status=FollowupStatus.PENDING,
                created_at=now_iso,
                updated_at=now_iso,
            )
            self.followup_repo.create(fu2)
            self.event_repo.record(
                event_code=EventCode.FOLLOWUP_CREATED,
                category="followup",
                level=EventLevel.INFO,
                entity_type="followup",
                entity_id=fu2.id,
                payload={"contact_id": contact.id, "sequence": 2},
            )

        sync_run.records_written += 1

    def update_replied_status(
        self,
        contact_id: str,
        replied_status: RepliedStatus,
        source_adapter: Optional[SourceAdapter] = None,
    ) -> bool:
        """
        Human-driven replied status update.
        If YES:
          - update contact replied status to YES
          - cancel all pending/scheduled followups for this contact with reason REPLIED
          - record events
          - optional write-back to source spreadsheet row
        """
        contact = self.contact_repo.get_by_id(contact_id)
        if not contact:
            return False

        old_status = contact.replied_status
        now_iso = utc_now_iso()

        updated = self.contact_repo.update_replied_status(
            contact_id=contact_id,
            replied_status=replied_status,
            source=RepliedSource.MANUAL,
            replied_at=now_iso if replied_status == RepliedStatus.YES else None,
        )

        if not updated:
            return False

        self.event_repo.record(
            event_code=EventCode.CONTACT_UPDATED,
            category="contact",
            level=EventLevel.INFO,
            entity_type="contact",
            entity_id=contact_id,
            payload={
                "action": "replied_status_changed",
                "old_status": old_status.value,
                "new_status": replied_status.value,
            },
        )

        if replied_status == RepliedStatus.YES:
            cancelled_count = self.followup_repo.cancel_pending_for_contact(
                contact_id=contact_id, cancel_reason="REPLIED"
            )
            if cancelled_count > 0:
                self.event_repo.record(
                    event_code=EventCode.FOLLOWUP_CANCELLED,
                    category="followup",
                    level=EventLevel.INFO,
                    entity_type="contact",
                    entity_id=contact_id,
                    payload={"cancelled_count": cancelled_count, "reason": "REPLIED"},
                )

        # Write-back to source spreadsheet if adapter provided
        if source_adapter and contact.source_record_id:
            srec = self.source_record_repo.get_by_id(contact.source_record_id)
            if srec:
                try:
                    source_adapter.update_record(
                        srec.row_index, {"replied_status": replied_status.value}
                    )
                except Exception as e:
                    logger.warning(f"Failed to write-back replied status to source: {e}")

        return True

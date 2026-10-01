"""Domain entities and data models."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from backend.domain.enums import (
    TaskType,
    TaskState,
    MessageState,
    FollowupStatus,
    RepliedStatus,
    RepliedSource,
    SourceType,
    SourceAccessStatus,
    SyncStatus,
    VerificationDecision,
    WorkerMode,
    WorkerStatus,
    EventLevel,
    EventCode,
    ErrorCode,
    ErrorSeverity,
)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 string format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Contact:
    id: str
    name: str
    instagram_url: str
    source_record_id: Optional[str] = None
    username: Optional[str] = None
    expected_followers: Optional[int] = None
    notes: Optional[str] = None
    replied_status: RepliedStatus = RepliedStatus.UNKNOWN
    replied_source: RepliedSource = RepliedSource.MANUAL
    replied_at: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["replied_status"] = self.replied_status.value if isinstance(self.replied_status, RepliedStatus) else self.replied_status
        d["replied_source"] = self.replied_source.value if isinstance(self.replied_source, RepliedSource) else self.replied_source
        return d


@dataclass
class SourceRecord:
    id: str
    source_type: SourceType
    source_identifier: str
    row_index: int
    raw_data_json: str
    checksum: str
    last_synced_at: str
    contact_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_type"] = self.source_type.value if isinstance(self.source_type, SourceType) else self.source_type
        return d


@dataclass
class Task:
    id: str
    contact_id: str
    type: TaskType
    sequence: int = 0
    status: TaskState = TaskState.CREATED
    priority: int = 0
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempt_count: int = 0
    worker_id: Optional[str] = None
    last_error_id: Optional[str] = None
    lock_token: Optional[str] = None
    locked_at: Optional[str] = None
    lease_id: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_acquired_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    message_hash: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value if isinstance(self.type, TaskType) else self.type
        d["status"] = self.status.value if isinstance(self.status, TaskState) else self.status
        return d


@dataclass
class Message:
    id: str
    contact_id: str
    body: str
    task_id: Optional[str] = None
    sequence: int = 0
    status: MessageState = MessageState.PENDING
    attempted_at: Optional[str] = None
    confirmed_at: Optional[str] = None
    result_code: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, MessageState) else self.status
        return d


@dataclass
class Followup:
    id: str
    contact_id: str
    sequence: int
    message: str
    delay_seconds: int
    scheduled_at: str
    status: FollowupStatus = FollowupStatus.PENDING
    sent_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, FollowupStatus) else self.status
        return d


@dataclass
class VerificationResult:
    id: str
    contact_id: str
    confidence: float
    decision: VerificationDecision
    signals_json: str
    task_id: Optional[str] = None
    ocr_text: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value if isinstance(self.decision, VerificationDecision) else self.decision
        return d


@dataclass
class AutomationRun:
    id: str
    run_code: str
    status: str
    started_at: str = field(default_factory=utc_now_iso)
    worker_id: Optional[str] = None
    task_id: Optional[str] = None
    ended_at: Optional[str] = None
    metadata_json: Optional[str] = None


@dataclass
class WorkerRecord:
    id: str
    worker_code: str
    mode: WorkerMode
    status: WorkerStatus
    current_task_id: Optional[str] = None
    last_heartbeat: Optional[str] = None
    metadata_json: Optional[str] = None


@dataclass
class BrowserSession:
    id: str
    profile_path: str
    status: str
    started_at: str = field(default_factory=utc_now_iso)
    worker_id: Optional[str] = None
    closed_at: Optional[str] = None


@dataclass
class Event:
    id: str
    timestamp: str
    level: EventLevel
    category: str
    event_code: EventCode
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    payload_json: Optional[str] = None
    task_id: Optional[str] = None
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["level"] = self.level.value if isinstance(self.level, EventLevel) else self.level
        d["event_code"] = self.event_code.value if isinstance(self.event_code, EventCode) else self.event_code
        return d


@dataclass
class ReconciliationRecord:
    id: str
    task_id: str
    message_id: Optional[str] = None
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    state: str = "PENDING"
    reason: str = ""
    observed_state: Optional[str] = None
    resolution: Optional[str] = None
    resolution_source: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    resolved_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ManualReviewItem:
    id: str
    task_id: str
    contact_id: str
    reason: str
    current_state: str
    evidence_json: Optional[str] = None
    recommended_action: Optional[str] = None
    status: str = "PENDING"
    created_at: str = field(default_factory=utc_now_iso)
    resolved_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ErrorRecord:
    id: str
    code: ErrorCode
    message: str
    severity: ErrorSeverity
    retryable: bool
    attempt: int = 1
    task_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    resolved_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["code"] = self.code.value if isinstance(self.code, ErrorCode) else self.code
        d["severity"] = self.severity.value if isinstance(self.severity, ErrorSeverity) else self.severity
        return d


@dataclass
class SyncRun:
    id: str
    sync_code: str
    source_type: SourceType
    source_identifier: str
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: Optional[str] = None
    status: SyncStatus = SyncStatus.RUNNING
    records_read: int = 0
    records_written: int = 0
    conflicts: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_type"] = self.source_type.value if isinstance(self.source_type, SourceType) else self.source_type
        d["status"] = self.status.value if isinstance(self.status, SyncStatus) else self.status
        return d


@dataclass
class HardwareCapabilities:
    cpu_count: int
    total_ram_gb: float
    available_ram_gb: float
    gpu_available: bool
    gpu_vendor: Optional[str] = None
    gpu_name: Optional[str] = None
    vram_gb: float = 0.0
    single_browser_available: bool = True
    multi_browser_available: bool = False
    recommended_max_workers: int = 1


@dataclass
class SourceRow:
    """Normalized representation of a single spreadsheet row during import."""
    row_index: int
    name: str
    instagram_url: str
    message: str
    replied_status: RepliedStatus
    contact_id: Optional[str] = None
    username: Optional[str] = None
    expected_followers: Optional[int] = None
    followup_1_message: Optional[str] = None
    followup_1_delay_seconds: Optional[int] = None
    followup_2_message: Optional[str] = None
    followup_2_delay_seconds: Optional[int] = None
    notes: Optional[str] = None
    raw_values: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""


@dataclass
class ExecutionIdentity:
    execution_key: str
    task_id: str
    contact_id: str
    message_id: Optional[str] = None
    message_hash: Optional[str] = None
    attempt: int = 1
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    state: str = "CREATED"
    outcome: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RateLimitCooldown:
    id: str
    scope: str
    reason: str
    error_code: str
    detected_at: str
    cooldown_until: str
    account_id: Optional[str] = None
    detected_by_worker: Optional[str] = None
    detected_by_session: Optional[str] = None
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticArtifact:
    id: str
    task_id: Optional[str] = None
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    timestamp: str = field(default_factory=utc_now_iso)
    artifact_type: str = "ERROR_METADATA"
    file_path: Optional[str] = None
    page_url: Optional[str] = None
    page_title: Optional[str] = None
    error_code: Optional[str] = None
    reason: Optional[str] = None
    retention_until: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)



"""Execution context carrying run-scoped metadata and correlation IDs."""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from backend.domain.models import utc_now_iso
from backend.events.correlation import generate_id, get_correlation_id, set_correlation_id


@dataclass
class ExecutionContext:
    """
    Execution-scoped context carried across tasks, workers, browser sessions, and events.
    Does NOT store sensitive credentials, passwords, or tokens.
    """
    run_id: str = field(default_factory=lambda: generate_id("RUN"))
    task_id: Optional[str] = None
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    source_id: Optional[str] = None
    correlation_id: str = field(default_factory=lambda: get_correlation_id() or generate_id("CORR"))
    started_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Synchronize global correlation context
        set_correlation_id(self.correlation_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "correlation_id": self.correlation_id,
            "started_at": self.started_at,
            "metadata": self.metadata,
        }

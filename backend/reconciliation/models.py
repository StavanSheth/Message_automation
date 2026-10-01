"""Reconciliation models and outcome definitions."""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from backend.domain.models import utc_now_iso
from backend.domain.enums import ReconciliationState, ReconciliationResolution


@dataclass
class ReconciliationContext:
    """Contextual evidence passed into the reconciliation resolver."""
    task_id: str
    message_id: Optional[str] = None
    worker_id: Optional[str] = None
    session_id: Optional[str] = None
    reason: str = ""
    observed_state: Optional[str] = None
    last_known_step: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationResult:
    """Outcome of resolving an ambiguous execution state."""
    resolution: ReconciliationResolution
    resolution_source: str
    details: str = ""
    target_task_state: str = ""
    target_message_state: str = ""
    can_retry: bool = False

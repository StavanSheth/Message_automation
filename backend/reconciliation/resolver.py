"""Reconciliation resolver inspecting evidence and applying policy."""

from typing import Optional, Dict, Any
from backend.reconciliation.models import ReconciliationContext, ReconciliationResult
from backend.reconciliation.policies import ReconciliationPolicy
from backend.domain.enums import ReconciliationResolution
from backend.events.logger import get_logger

logger = get_logger("reconciliation_resolver")


class ReconciliationResolver:
    """
    Evaluates ambiguous execution evidence (DOM verification, crash dumps, composer state)
    to determine whether a message was sent or requires human intervention.
    """

    def __init__(
        self,
        task_repo: Optional[Any] = None,
        message_repo: Optional[Any] = None,
        event_repo: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.event_repo = event_repo

    def resolve(
        self,
        context: ReconciliationContext,
        verification_confirmed: Optional[bool] = None,
        composer_empty: Optional[bool] = None,
        has_error_banner: bool = False,
        source: str = "INSPECTION",
    ) -> ReconciliationResult:
        """Resolve the reconciliation context using available evidence."""
        policy_eval = ReconciliationPolicy.evaluate(
            cause=context.reason,
            verification_confirmed=verification_confirmed,
            composer_empty=composer_empty,
            has_error_banner=has_error_banner,
        )

        resolution = policy_eval["resolution"]
        logger.info(
            f"Reconciliation resolved for task {context.task_id}: {resolution.value} ({policy_eval['details']})"
        )

        return ReconciliationResult(
            resolution=resolution,
            resolution_source=source,
            details=policy_eval["details"],
            target_task_state=policy_eval["target_task_state"].value,
            target_message_state=policy_eval["target_message_state"].value,
            can_retry=policy_eval["can_retry"],
        )

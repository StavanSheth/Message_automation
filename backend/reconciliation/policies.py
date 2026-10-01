"""Reconciliation policies mapping failure causes to resolution guidelines."""

from typing import Dict, Any
from backend.domain.enums import ReconciliationResolution, TaskState, MessageState


class ReconciliationPolicy:
    """
    Core policy rule: AMBIGUOUS SEND != FAILED SEND.
    Never automatically resend an ambiguous message.
    """

    @staticmethod
    def evaluate(
        cause: str,
        verification_confirmed: bool | None,
        composer_empty: bool | None = None,
        has_error_banner: bool = False,
    ) -> Dict[str, Any]:
        """
        Evaluate reconciliation inputs and return resolution and state mappings.
        - verification_confirmed is True -> CONFIRMED_SENT -> TaskState.COMPLETED, MessageState.SENT
        - verification_confirmed is False and composer_empty is False -> CONFIRMED_NOT_SENT -> RETRY_ALLOWED
        - verification_confirmed is False and composer_empty is True -> MANUAL_REVIEW (ambiguous whether sent or lost)
        - indeterminate -> MANUAL_REVIEW
        """
        if verification_confirmed is True:
            return {
                "resolution": ReconciliationResolution.CONFIRMED_SENT,
                "target_task_state": TaskState.COMPLETED,
                "target_message_state": MessageState.SENT,
                "can_retry": False,
                "details": "Delivery verified in message thread",
            }

        if has_error_banner and verification_confirmed is False:
            return {
                "resolution": ReconciliationResolution.CONFIRMED_NOT_SENT,
                "target_task_state": TaskState.RETRY_WAIT,
                "target_message_state": MessageState.FAILED,
                "can_retry": True,
                "details": "Send failure banner detected; message was confirmed not sent",
            }

        if composer_empty is False and verification_confirmed is False:
            return {
                "resolution": ReconciliationResolution.RETRY_ALLOWED,
                "target_task_state": TaskState.READY,
                "target_message_state": MessageState.PENDING,
                "can_retry": True,
                "details": "Composer still retained content and no message appeared in thread",
            }

        # Any ambiguous condition must fail-safe to MANUAL_REVIEW
        return {
            "resolution": ReconciliationResolution.MANUAL_REVIEW,
            "target_task_state": TaskState.MANUAL_REVIEW,
            "target_message_state": MessageState.RECONCILIATION,
            "can_retry": False,
            "details": f"Ambiguous send outcome ({cause}); requires manual operator review to prevent double-send",
        }

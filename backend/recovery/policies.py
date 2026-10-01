"""Recovery policies mapping task execution states to safe recovery actions."""

from backend.domain.enums import TaskState


class TaskRecoveryPolicy:
    """
    Defines the authoritative rule for transitioning orphaned or crashed tasks:
    - READY: can resume directly
    - RUNNING: must transition to INTERRUPTED (not directly back to READY)
    - SENDING: must enter RECONCILIATION (potential double-send risk)
    - VERIFYING: must enter RECONCILIATION (delivery unconfirmed)
    - RECONCILING / RECONCILIATION: resolver required, never reset to READY
    """

    @staticmethod
    def get_recovery_target(current_state: TaskState) -> TaskState:
        if current_state in (TaskState.READY, TaskState.QUEUED):
            return TaskState.READY

        if current_state == TaskState.RUNNING:
            return TaskState.INTERRUPTED

        if current_state in (TaskState.SENDING, TaskState.VERIFYING):
            return TaskState.RECONCILING

        if current_state == TaskState.RECONCILING:
            return TaskState.RECONCILING

        return TaskState.MANUAL_REVIEW

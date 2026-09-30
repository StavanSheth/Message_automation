"""Domain exception classes and error models."""

from typing import Optional
from backend.domain.enums import ErrorCode, ErrorSeverity


class AutomationError(Exception):
    """Base exception for all domain and automation errors."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        retryable: bool = False,
        task_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.severity = severity
        self.retryable = retryable
        self.task_id = task_id

    def to_dict(self) -> dict:
        return {
            "code": self.code.value if isinstance(self.code, ErrorCode) else str(self.code),
            "message": self.message,
            "severity": self.severity.value if isinstance(self.severity, ErrorSeverity) else str(self.severity),
            "retryable": self.retryable,
            "task_id": self.task_id,
        }


class DuplicateTaskError(AutomationError):
    def __init__(self, message: str, task_id: Optional[str] = None):
        super().__init__(
            code=ErrorCode.DUPLICATE_TASK,
            message=message,
            severity=ErrorSeverity.LOW,
            retryable=False,
            task_id=task_id,
        )


class ConflictError(AutomationError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.SYNC_CONFLICT,
            message=message,
            severity=ErrorSeverity.HIGH,
            retryable=False,
        )


class SourceAccessError(AutomationError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.ACCESS_PROHIBITED):
        super().__init__(
            code=code,
            message=message,
            severity=ErrorSeverity.HIGH,
            retryable=False,
        )


class ValidationError(AutomationError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.INVALID_DATA,
            message=message,
            severity=ErrorSeverity.LOW,
            retryable=False,
        )


class TaskStateError(AutomationError):
    def __init__(self, message: str, task_id: Optional[str] = None):
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            message=message,
            severity=ErrorSeverity.MEDIUM,
            retryable=False,
            task_id=task_id,
        )

"""Task repository with atomic locking, duplicate protection, and state transitions."""

import sqlite3
from typing import Optional, List, Set
from backend.repositories.base import BaseRepository
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskType, TaskState
from backend.domain.errors import DuplicateTaskError, TaskStateError


# Valid state transitions from Document 3
VALID_TRANSITIONS: dict[TaskState, Set[TaskState]] = {
    TaskState.CREATED: {TaskState.VALIDATING, TaskState.CANCELLED},
    TaskState.VALIDATING: {TaskState.QUEUED, TaskState.SKIPPED, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.QUEUED: {TaskState.READY, TaskState.CANCELLED},
    TaskState.READY: {TaskState.RUNNING, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.COMPLETED,
        TaskState.RETRY_WAIT,
        TaskState.MANUAL_REVIEW,
        TaskState.SKIPPED,
        TaskState.CANCELLED,
        TaskState.RECONCILING,
        TaskState.INTERRUPTED,
        TaskState.FAILED,
    },
    TaskState.RETRY_WAIT: {TaskState.READY, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.RECONCILING: {TaskState.COMPLETED, TaskState.READY, TaskState.MANUAL_REVIEW, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.INTERRUPTED: {TaskState.RECONCILING, TaskState.READY, TaskState.MANUAL_REVIEW, TaskState.CANCELLED, TaskState.QUEUED},
    TaskState.MANUAL_REVIEW: {TaskState.READY, TaskState.CANCELLED, TaskState.SKIPPED, TaskState.COMPLETED},
    TaskState.COMPLETED: set(),
    TaskState.SKIPPED: set(),
    TaskState.CANCELLED: set(),
    TaskState.FAILED: {TaskState.READY},  # Allows manual retry
}


class TaskRepository(BaseRepository):
    """Data access repository for automation tasks."""

    def create(self, task: Task) -> Task:
        """
        Create a new task with strict duplicate prevention.
        Enforces uniqueness on (contact_id, type, sequence).
        """
        type_val = task.type.value if isinstance(task.type, TaskType) else task.type
        status_val = task.status.value if isinstance(task.status, TaskState) else task.status

        with self.db.transaction() as conn:
            # Check existing active task
            check_cursor = conn.execute(
                """
                SELECT id FROM tasks 
                WHERE contact_id = ? AND type = ? AND sequence = ?;
                """,
                (task.contact_id, type_val, task.sequence),
            )
            existing = check_cursor.fetchone()
            if existing:
                raise DuplicateTaskError(
                    f"A task of type {type_val} sequence {task.sequence} already exists for contact {task.contact_id}.",
                    task_id=existing[0],
                )

            query = """
                INSERT INTO tasks (
                    id, contact_id, type, sequence, status, priority,
                    scheduled_at, started_at, completed_at, attempt_count,
                    worker_id, last_error_id, lock_token, locked_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """
            params = (
                task.id,
                task.contact_id,
                type_val,
                task.sequence,
                status_val,
                task.priority,
                task.scheduled_at,
                task.started_at,
                task.completed_at,
                task.attempt_count,
                task.worker_id,
                task.last_error_id,
                task.lock_token,
                task.locked_at,
                task.created_at,
                task.updated_at,
            )
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError as e:
                raise DuplicateTaskError(
                    f"Duplicate task constraint violated: {e}",
                    task_id=task.id,
                )

        return task

    def get_by_id(self, task_id: str) -> Optional[Task]:
        """Fetch a task by ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def get_by_contact_and_type(
        self, contact_id: str, task_type: TaskType, sequence: int = 0
    ) -> Optional[Task]:
        """Fetch a task by contact, type, and sequence."""
        conn = self.db.get_connection()
        type_val = task_type.value if isinstance(task_type, TaskType) else task_type
        cursor = conn.execute(
            "SELECT * FROM tasks WHERE contact_id = ? AND type = ? AND sequence = ?;",
            (contact_id, type_val, sequence),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def get_by_contact_id(self, contact_id: str) -> List[Task]:
        """Fetch all tasks for a contact."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM tasks WHERE contact_id = ? ORDER BY sequence ASC, created_at ASC;",
            (contact_id,),
        )
        return [self._row_to_task(row) for row in cursor.fetchall()]

    def update_state(
        self,
        task_id: str,
        new_state: TaskState,
        worker_id: Optional[str] = None,
        last_error_id: Optional[str] = None,
        enforce_transition: bool = True,
    ) -> Task:
        """
        Update a task's state according to the state machine.
        """
        now_iso = utc_now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,))
            row = cursor.fetchone()
            if not row:
                raise TaskStateError(f"Task with id '{task_id}' not found.", task_id=task_id)

            current_state = TaskState(row["status"])
            if enforce_transition:
                allowed = VALID_TRANSITIONS.get(current_state, set())
                if new_state not in allowed:
                    raise TaskStateError(
                        f"Illegal state transition from {current_state.value} to {new_state.value} for task {task_id}.",
                        task_id=task_id,
                    )

            started_at = row["started_at"]
            completed_at = row["completed_at"]

            if new_state == TaskState.RUNNING and started_at is None:
                started_at = now_iso
            elif new_state in (TaskState.COMPLETED, TaskState.SKIPPED, TaskState.CANCELLED, TaskState.FAILED):
                completed_at = now_iso

            assigned_worker = worker_id if worker_id is not None else row["worker_id"]
            error_id = last_error_id if last_error_id is not None else row["last_error_id"]

            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    started_at = ?,
                    completed_at = ?,
                    worker_id = ?,
                    last_error_id = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (
                    new_state.value,
                    started_at,
                    completed_at,
                    assigned_worker,
                    error_id,
                    now_iso,
                    task_id,
                ),
            )

        updated_task = self.get_by_id(task_id)
        assert updated_task is not None
        return updated_task

    def claim_task(
        self,
        task_id: str,
        worker_id: str,
        lock_token: str,
    ) -> bool:
        """
        Atomically claim a task for execution by a worker.
        Only succeeds if task is in READY or QUEUED state and not already locked.
        """
        now_iso = utc_now_iso()
        query = """
            UPDATE tasks SET
                status = ?,
                worker_id = ?,
                lock_token = ?,
                locked_at = ?,
                started_at = COALESCE(started_at, ?),
                attempt_count = attempt_count + 1,
                updated_at = ?
            WHERE id = ?
              AND status IN ('READY', 'QUEUED')
              AND (lock_token IS NULL OR lock_token = '');
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                query,
                (
                    TaskState.RUNNING.value,
                    worker_id,
                    lock_token,
                    now_iso,
                    now_iso,
                    now_iso,
                    task_id,
                ),
            )
            return cursor.rowcount > 0

    def release_task(self, task_id: str, lock_token: str) -> bool:
        """Release a task lock."""
        now_iso = utc_now_iso()
        query = """
            UPDATE tasks SET
                lock_token = NULL,
                locked_at = NULL,
                updated_at = ?
            WHERE id = ? AND lock_token = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now_iso, task_id, lock_token))
            return cursor.rowcount > 0

    def list_ready(self, limit: int = 50) -> List[Task]:
        """List tasks that are ready for execution ordered by priority DESC and scheduled_at ASC."""
        conn = self.db.get_connection()
        query = """
            SELECT * FROM tasks
            WHERE status = 'READY'
              AND (lock_token IS NULL OR lock_token = '')
            ORDER BY priority DESC, scheduled_at ASC, created_at ASC
            LIMIT ?;
        """
        cursor = conn.execute(query, (limit,))
        return [self._row_to_task(row) for row in cursor.fetchall()]

    def list_interrupted(self) -> List[Task]:
        """List tasks marked as INTERRUPTED or still RUNNING after an abnormal termination."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM tasks WHERE status = 'INTERRUPTED' ORDER BY updated_at ASC;"
        )
        return [self._row_to_task(row) for row in cursor.fetchall()]

    def mark_running_as_interrupted(self) -> int:
        """
        Crash recovery hook: on application startup, mark any tasks left in RUNNING as INTERRUPTED.
        Returns the number of tasks transitioned.
        """
        now_iso = utc_now_iso()
        query = """
            UPDATE tasks SET
                status = 'INTERRUPTED',
                lock_token = NULL,
                locked_at = NULL,
                updated_at = ?
            WHERE status = 'RUNNING';
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now_iso,))
            return cursor.rowcount

    def count_by_status(self) -> dict[str, int]:
        """Count tasks grouped by status."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            contact_id=row["contact_id"],
            type=TaskType(row["type"]),
            sequence=row["sequence"],
            status=TaskState(row["status"]),
            priority=row["priority"],
            scheduled_at=row["scheduled_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            attempt_count=row["attempt_count"],
            worker_id=row["worker_id"],
            last_error_id=row["last_error_id"],
            lock_token=row["lock_token"],
            locked_at=row["locked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

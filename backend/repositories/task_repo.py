"""Task repository with atomic locking, duplicate protection, and state transitions."""

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Set
from backend.repositories.base import BaseRepository
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskType, TaskState
from backend.domain.errors import DuplicateTaskError, TaskStateError
from backend.events.correlation import generate_id


# Valid state transitions from Document 3 + Phase 3 pipeline
VALID_TRANSITIONS: dict[TaskState, Set[TaskState]] = {
    TaskState.CREATED: {TaskState.VALIDATING, TaskState.CANCELLED},
    TaskState.VALIDATING: {TaskState.QUEUED, TaskState.READY, TaskState.RUNNING, TaskState.SKIPPED, TaskState.CANCELLED, TaskState.FAILED, TaskState.MANUAL_REVIEW, TaskState.RETRY_WAIT},
    TaskState.QUEUED: {TaskState.READY, TaskState.CANCELLED},
    TaskState.READY: {TaskState.RUNNING, TaskState.VALIDATING, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.VALIDATING,
        TaskState.SENDING,
        TaskState.COMPLETED,
        TaskState.RETRY_WAIT,
        TaskState.MANUAL_REVIEW,
        TaskState.SKIPPED,
        TaskState.CANCELLED,
        TaskState.RECONCILING,
        TaskState.INTERRUPTED,
        TaskState.FAILED,
    },
    TaskState.SENDING: {
        TaskState.VERIFYING,
        TaskState.COMPLETED,
        TaskState.RECONCILING,
        TaskState.FAILED,
        TaskState.INTERRUPTED,
    },
    TaskState.VERIFYING: {
        TaskState.COMPLETED,
        TaskState.RECONCILING,
        TaskState.FAILED,
        TaskState.INTERRUPTED,
        TaskState.MANUAL_REVIEW,
    },
    TaskState.RETRY_WAIT: {TaskState.READY, TaskState.CANCELLED, TaskState.FAILED, TaskState.MANUAL_REVIEW},
    TaskState.RECONCILING: {TaskState.COMPLETED, TaskState.READY, TaskState.MANUAL_REVIEW, TaskState.CANCELLED, TaskState.FAILED, TaskState.RETRY_WAIT},
    TaskState.INTERRUPTED: {TaskState.RECONCILING, TaskState.READY, TaskState.MANUAL_REVIEW, TaskState.CANCELLED, TaskState.QUEUED},
    TaskState.MANUAL_REVIEW: {TaskState.READY, TaskState.CANCELLED, TaskState.SKIPPED, TaskState.COMPLETED, TaskState.FAILED},
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
                    lease_id, lease_owner, lease_expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                task.lease_id,
                task.lease_owner or task.worker_id,
                task.lease_expires_at,
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
            if enforce_transition and new_state != current_state:
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
        lease_duration_seconds: int = 120,
    ) -> bool:
        """
        Atomically claim a task for execution by a worker.
        Delegates to the authoritative acquire_lease API.
        """
        lid = self.acquire_lease(
            task_id=task_id,
            worker_id=worker_id,
            lease_duration_seconds=lease_duration_seconds,
            lease_id=lock_token,
        )
        return lid is not None

    def release_task(self, task_id: str, lock_token: str) -> bool:
        """Release a task lock and lease."""
        return self.release_lease(task_id=task_id, lease_id=lock_token)

    def unlock_task(self, task_id: str) -> bool:
        """Unconditionally release a task lock and lease (e.g. on worker crash recovery)."""
        now_iso = utc_now_iso()
        query = """
            UPDATE tasks SET
                lock_token = NULL,
                locked_at = NULL,
                lease_id = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now_iso, task_id))
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

    def acquire_lease(
        self,
        task_id: str,
        worker_id: str,
        lease_duration_seconds: int = 120,
        lease_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Atomically acquire a persistent lease on a task for a worker.
        Only succeeds if task is in READY or QUEUED state and lease is unowned or expired.
        Returns the unique lease_id on success, None on failure.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=lease_duration_seconds)).isoformat()
        assigned_lease_id = lease_id or generate_id("LEASE")

        query = """
            UPDATE tasks SET
                status = 'RUNNING',
                worker_id = ?,
                lock_token = COALESCE(lock_token, ?),
                locked_at = COALESCE(locked_at, ?),
                lease_id = ?,
                lease_owner = ?,
                lease_acquired_at = ?,
                lease_expires_at = ?,
                started_at = COALESCE(started_at, ?),
                attempt_count = attempt_count + 1,
                updated_at = ?
            WHERE id = ?
              AND status IN ('READY', 'QUEUED')
              AND (lease_id IS NULL OR lease_expires_at < ?);
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                query,
                (
                    worker_id,
                    assigned_lease_id,
                    now_iso,
                    assigned_lease_id,
                    worker_id,
                    now_iso,
                    expires_iso,
                    now_iso,
                    now_iso,
                    task_id,
                    now_iso,
                ),
            )
            if cursor.rowcount > 0:
                return assigned_lease_id
        return None

    def renew_lease(
        self,
        task_id: str,
        lease_id: str,
        worker_id: str,
        lease_duration_seconds: int = 120,
    ) -> bool:
        """Renew an actively held lease. Only the lease owner can renew before expiry."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=lease_duration_seconds)).isoformat()

        query = """
            UPDATE tasks SET
                lease_expires_at = ?,
                updated_at = ?
            WHERE id = ? AND (lease_id = ? OR lock_token = ?) AND (lease_owner = ? OR worker_id = ?) AND lease_expires_at > ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (expires_iso, now_iso, task_id, lease_id, lease_id, worker_id, worker_id, now_iso))
            return cursor.rowcount > 0

    def release_lease(
        self,
        task_id: str,
        lease_id: str,
        worker_id: Optional[str] = None,
    ) -> bool:
        """Release a held task lease and lock."""
        now_iso = utc_now_iso()
        if worker_id is not None:
            query = """
                UPDATE tasks SET
                    lock_token = NULL,
                    locked_at = NULL,
                    lease_id = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND (lease_id = ? OR lock_token = ?) AND (lease_owner = ? OR worker_id = ?);
            """
            params = (now_iso, task_id, lease_id, lease_id, worker_id, worker_id)
        else:
            query = """
                UPDATE tasks SET
                    lock_token = NULL,
                    locked_at = NULL,
                    lease_id = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND (lease_id = ? OR lock_token = ?);
            """
            params = (now_iso, task_id, lease_id, lease_id)

        with self.db.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount > 0

    def is_lease_valid(
        self,
        task_id: str,
        lease_id: str,
        worker_id: Optional[str] = None,
    ) -> bool:
        """Check whether the lease is active and optionally owned by worker_id."""
        now_iso = utc_now_iso()
        conn = self.db.get_connection()
        if worker_id is not None:
            cursor = conn.execute(
                """
                SELECT id FROM tasks
                WHERE id = ?
                  AND (lease_id = ? OR lock_token = ?)
                  AND (lease_owner = ? OR worker_id = ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at > ?);
                """,
                (task_id, lease_id, lease_id, worker_id, worker_id, now_iso),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id FROM tasks
                WHERE id = ?
                  AND (lease_id = ? OR lock_token = ?)
                  AND (lease_expires_at IS NULL OR lease_expires_at > ?);
                """,
                (task_id, lease_id, lease_id, now_iso),
            )
        return cursor.fetchone() is not None

    def recover_expired_lease(self, task_id: str) -> Optional[Task]:
        """
        Safely recover a task whose lease has expired according to the strict state policy:
        READY/QUEUED -> READY (clears lease)
        RUNNING -> INTERRUPTED (clears lease)
        SENDING -> RECONCILING (clears lease, prevents duplicate sends)
        VERIFYING -> RECONCILING (clears lease, unconfirmed outcome)
        Never silently convert SENDING -> READY.
        """
        now_iso = utc_now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None

            lease_expires_at = row["lease_expires_at"]
            if not lease_expires_at or lease_expires_at >= now_iso:
                return None

            current_status = row["status"]
            if current_status in ("READY", "QUEUED"):
                new_status = TaskState.READY.value
            elif current_status == "RUNNING":
                new_status = TaskState.INTERRUPTED.value
            elif current_status in ("SENDING", "VERIFYING"):
                new_status = TaskState.RECONCILING.value
            else:
                new_status = current_status

            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    lock_token = NULL,
                    locked_at = NULL,
                    lease_id = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND lease_expires_at < ?;
                """,
                (new_status, now_iso, task_id, now_iso),
            )
        return self.get_by_id(task_id)

    def recover_expired_leases(self) -> List[Task]:
        """
        Find and safely recover all tasks whose leases have expired according to the state policy:
        READY/QUEUED -> READY
        RUNNING -> INTERRUPTED
        SENDING/VERIFYING -> RECONCILING
        """
        now_iso = utc_now_iso()
        conn = self.db.get_connection()
        cursor = conn.execute(
            """
            SELECT id FROM tasks
            WHERE lease_expires_at IS NOT NULL
              AND lease_expires_at < ?
              AND status IN ('READY', 'QUEUED', 'RUNNING', 'SENDING', 'VERIFYING');
            """,
            (now_iso,),
        )
        task_ids = [row[0] for row in cursor.fetchall()]
        recovered = []
        for tid in task_ids:
            task = self.recover_expired_lease(tid)
            if task:
                recovered.append(task)
        return recovered

    def count_by_status(self) -> dict[str, int]:
        """Count tasks grouped by status."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        keys = row.keys() if hasattr(row, "keys") else []
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
            lease_id=row["lease_id"] if "lease_id" in keys else None,
            lease_owner=row["lease_owner"] if "lease_owner" in keys else None,
            lease_acquired_at=row["lease_acquired_at"] if "lease_acquired_at" in keys else None,
            lease_expires_at=row["lease_expires_at"] if "lease_expires_at" in keys else None,
            message_hash=row["message_hash"] if "message_hash" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

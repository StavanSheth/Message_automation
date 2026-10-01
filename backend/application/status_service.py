"""Operational status service reporting real-time system, worker, task, and message states."""

from typing import Dict, Any, Optional
from backend.database.manager import DatabaseManager
from backend.domain.enums import SystemState, WorkerStatus


class StatusService:
    """
    Operational Dashboard Data Layer querying real runtime and database state:
    - Top-level system state
    - Worker pool and health distribution
    - Browser sessions
    - Task, message, follow-up, reconciliation, and manual review breakdowns
    - Account and cooldown status
    """

    def __init__(
        self,
        db: DatabaseManager,
        control_service: Optional[Any] = None,
        worker_manager: Optional[Any] = None,
        browser_manager: Optional[Any] = None,
        account_repo: Optional[Any] = None,
        cooldown_repo: Optional[Any] = None,
    ):
        self.db = db
        self.control_service = control_service
        self.worker_manager = worker_manager
        self.browser_manager = browser_manager
        self.account_repo = account_repo
        self.cooldown_repo = cooldown_repo

    def get_system_status(self) -> Dict[str, Any]:
        """Aggregate comprehensive system operational status directly from DB and live services."""
        conn = self.db.get_connection()

        # 1. System state
        state_val = (
            self.control_service.state.value
            if self.control_service and hasattr(self.control_service, "state")
            else SystemState.STOPPED.value
        )

        # 2. Worker breakdown
        worker_summary = {
            "total": 0,
            "active": 0,
            "idle": 0,
            "busy": 0,
            "paused": 0,
            "quarantined": 0,
            "degraded": 0,
            "stopped": 0,
            "crashed": 0,
        }
        if self.worker_manager and hasattr(self.worker_manager, "list_workers"):
            records = self.worker_manager.list_workers()
            worker_summary["total"] = len(records)
            for r in records:
                st = r.status.value.lower() if hasattr(r.status, "value") else str(r.status).lower()
                if st in worker_summary:
                    worker_summary[st] += 1
                if st in ("idle", "busy"):
                    worker_summary["active"] += 1
        else:
            try:
                cur = conn.execute("SELECT status, COUNT(*) FROM workers GROUP BY status;")
                for row in cur.fetchall():
                    st = row[0].lower()
                    if st in worker_summary:
                        worker_summary[st] = row[1]
                worker_summary["total"] = sum(v for k, v in worker_summary.items() if k != "total" and k != "active")
                worker_summary["active"] = worker_summary.get("idle", 0) + worker_summary.get("busy", 0)
            except Exception:
                pass

        # 3. Browser breakdown
        browser_summary = {"active_sessions": 0, "alive": 0}
        if self.browser_manager and hasattr(self.browser_manager, "list_active_sessions"):
            active_s = self.browser_manager.list_active_sessions()
            browser_summary["active_sessions"] = len(active_s)
            browser_summary["alive"] = sum(1 for s in active_s if s.is_alive())

        # 4. Tasks counts
        task_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status;")
            for row in cur.fetchall():
                task_counts[row[0]] = row[1]
        except Exception:
            pass

        # 5. Messages counts
        message_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM messages GROUP BY status;")
            for row in cur.fetchall():
                message_counts[row[0]] = row[1]
        except Exception:
            pass

        # 6. Follow-up counts
        followup_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM followups GROUP BY status;")
            for row in cur.fetchall():
                followup_counts[row[0]] = row[1]
        except Exception:
            pass

        # 7. Reconciliations
        reconciliation_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT state, COUNT(*) FROM reconciliations GROUP BY state;")
            for row in cur.fetchall():
                reconciliation_counts[row[0]] = row[1]
        except Exception:
            pass

        # 8. Manual reviews
        manual_review_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM manual_reviews GROUP BY status;")
            for row in cur.fetchall():
                manual_review_counts[row[0]] = row[1]
        except Exception:
            pass

        # 9. Cooldowns
        active_cooldowns_count = 0
        if self.cooldown_repo:
            try:
                active_cooldowns_count = len(self.cooldown_repo.list_active_cooldowns())
            except Exception:
                pass

        # 10. Accounts
        account_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status;")
            for row in cur.fetchall():
                account_counts[row[0]] = row[1]
        except Exception:
            pass

        return {
            "system_state": state_val,
            "workers": worker_summary,
            "browser": browser_summary,
            "tasks": task_counts,
            "messages": message_counts,
            "followups": followup_counts,
            "reconciliations": reconciliation_counts,
            "manual_reviews": manual_review_counts,
            "accounts": account_counts,
            "active_cooldowns": active_cooldowns_count,
        }

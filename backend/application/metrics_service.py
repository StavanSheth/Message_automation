"""Operational metrics service computing business and reliability metrics from persisted data."""

from typing import Dict, Any, Optional
from datetime import datetime, timezone
from backend.database.manager import DatabaseManager


class MetricsService:
    """
    Computes performance, reliability, and volume metrics from persisted DB records and events.
    Does not rely on volatile in-memory counters.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    def get_metrics(self) -> Dict[str, Any]:
        """Aggregate counts, rates, and duration averages from the database."""
        conn = self.db.get_connection()

        # 1. Tasks metrics
        cur = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed,
                AVG(attempt_count) as avg_retries
            FROM tasks;
            """
        )
        task_row = cur.fetchone()
        tasks_created = task_row[0] or 0
        tasks_completed = task_row[1] or 0
        tasks_failed = task_row[2] or 0
        avg_retries = round(float(task_row[3] or 1.0), 2)

        # 2. Messages metrics
        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'SENT' THEN 1 ELSE 0 END) as sent,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'RECONCILIATION' THEN 1 ELSE 0 END) as reconciled
            FROM messages;
            """
        )
        msg_row = cur.fetchone()
        messages_sent = msg_row[0] or 0
        messages_failed = msg_row[1] or 0
        messages_reconciled = msg_row[2] or 0

        # 3. Follow-up metrics
        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'SENT' THEN 1 ELSE 0 END) as sent,
                SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) as cancelled
            FROM followups;
            """
        )
        fu_row = cur.fetchone()
        followups_sent = fu_row[0] or 0
        followups_cancelled = fu_row[1] or 0

        # 4. Manual reviews
        cur = conn.execute("SELECT COUNT(*) FROM manual_reviews;")
        manual_reviews = cur.fetchone()[0] or 0

        # 5. Cooldowns & Rate Limits
        cur = conn.execute("SELECT COUNT(*) FROM rate_limit_cooldowns;")
        rate_limits = cur.fetchone()[0] or 0

        # 6. Event counts for failure classifications
        event_counts = {
            "browser_crashes": 0,
            "worker_crashes": 0,
            "authentication_failures": 0,
            "retries": 0,
        }
        try:
            cur = conn.execute(
                """
                SELECT event_code, COUNT(*)
                FROM events
                WHERE event_code IN ('BROWSER_CRASHED', 'WORKER_CRASHED', 'LOGIN_REQUIRED', 'TASK_RETRY_SCHEDULED')
                GROUP BY event_code;
                """
            )
            for row in cur.fetchall():
                code = row[0]
                cnt = row[1]
                if code == "BROWSER_CRASHED":
                    event_counts["browser_crashes"] = cnt
                elif code == "WORKER_CRASHED":
                    event_counts["worker_crashes"] = cnt
                elif code == "LOGIN_REQUIRED":
                    event_counts["authentication_failures"] = cnt
                elif code == "TASK_RETRY_SCHEDULED":
                    event_counts["retries"] = cnt
        except Exception:
            pass

        # 7. Average execution time for completed tasks (seconds)
        avg_exec_time = 0.0
        try:
            cur = conn.execute(
                """
                SELECT started_at, completed_at
                FROM tasks
                WHERE status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL;
                """
            )
            durations = []
            for row in cur.fetchall():
                try:
                    s_dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    c_dt = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
                    durations.append((c_dt - s_dt).total_seconds())
                except Exception:
                    continue
            if durations:
                avg_exec_time = round(sum(durations) / len(durations), 2)
        except Exception:
            pass

        # 8. Derived rates
        total_finished_messages = messages_sent + messages_failed + messages_reconciled
        success_rate = round((messages_sent / total_finished_messages * 100.0), 2) if total_finished_messages > 0 else 0.0
        failure_rate = round((messages_failed / total_finished_messages * 100.0), 2) if total_finished_messages > 0 else 0.0
        reconciliation_rate = round((messages_reconciled / total_finished_messages * 100.0), 2) if total_finished_messages > 0 else 0.0

        return {
            "tasks_created": tasks_created,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "messages_sent": messages_sent,
            "messages_failed": messages_failed,
            "messages_reconciled": messages_reconciled,
            "manual_reviews": manual_reviews,
            "rate_limits": rate_limits,
            "retries": event_counts["retries"],
            "browser_crashes": event_counts["browser_crashes"],
            "worker_crashes": event_counts["worker_crashes"],
            "authentication_failures": event_counts["authentication_failures"],
            "followups_sent": followups_sent,
            "followups_cancelled": followups_cancelled,
            "success_rate_percent": success_rate,
            "failure_rate_percent": failure_rate,
            "reconciliation_rate_percent": reconciliation_rate,
            "average_execution_time_seconds": avg_exec_time,
            "average_retry_count": avg_retries,
        }

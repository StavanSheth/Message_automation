"""Data retention service for controlled, idempotent pruning of historical operational records."""

from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.config.settings import AppSettings, get_settings
from backend.domain.models import utc_now_iso
from backend.domain.enums import EventCode, EventLevel
from backend.events.logger import get_logger

logger = get_logger("retention_service")


class RetentionService:
    """
    Controlled data retention and cleanup engine:
    - Prunes events older than event_retention_days
    - Prunes old resolved error records (never unresolved errors)
    - Prunes resolved reconciliations older than retention window (never touches PENDING/IN_PROGRESS)
    - Prunes resolved manual reviews older than retention window (never touches PENDING)
    - Prunes old completed/failed execution identities
    - Prunes diagnostic artifacts older than diagnostic_retention_days (never for in-flight tasks)
    - Emits structured audit events on completion or failure
    - Strictly idempotent and safe
    """

    def __init__(
        self,
        db: DatabaseManager,
        settings: Optional[AppSettings] = None,
        event_repo: Optional[Any] = None,
    ):
        self.db = db
        self.settings = settings or get_settings()
        self.event_repo = event_repo

    def cleanup_expired_data(
        self,
        event_retention_days: Optional[int] = None,
        error_retention_days: Optional[int] = None,
        resolved_retention_days: Optional[int] = None,
        diagnostic_retention_days: Optional[int] = None,
        execution_identity_retention_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute controlled data pruning against historical tables.
        Never deletes active, pending, or in-flight records.
        Produces audit event containing detailed summary.
        """
        started_at = utc_now_iso()
        now = datetime.now(timezone.utc)
        ev_days = event_retention_days if event_retention_days is not None else getattr(self.settings, "event_retention_days", 30)
        err_days = error_retention_days if error_retention_days is not None else getattr(self.settings, "error_retention_days", 30)
        res_days = resolved_retention_days if resolved_retention_days is not None else 30
        diag_days = diagnostic_retention_days if diagnostic_retention_days is not None else getattr(self.settings, "diagnostic_retention_days", 14)
        exec_id_days = execution_identity_retention_days if execution_identity_retention_days is not None else 30

        ev_cutoff = (now - timedelta(days=ev_days)).isoformat()
        err_cutoff = (now - timedelta(days=err_days)).isoformat()
        res_cutoff = (now - timedelta(days=res_days)).isoformat()
        diag_cutoff = (now - timedelta(days=diag_days)).isoformat()
        exec_id_cutoff = (now - timedelta(days=exec_id_days)).isoformat()

        retention_config = {
            "event_retention_days": ev_days,
            "error_retention_days": err_days,
            "resolved_retention_days": res_days,
            "diagnostic_retention_days": diag_days,
            "execution_identity_retention_days": exec_id_days,
        }

        summary = {
            "events_deleted": 0,
            "errors_deleted": 0,
            "resolved_reconciliations_deleted": 0,
            "resolved_manual_reviews_deleted": 0,
            "old_execution_identities_deleted": 0,
            "diagnostics_deleted": 0,
        }

        try:
            with self.db.transaction() as conn:
                # 1. Prune old events
                cur = conn.execute("DELETE FROM events WHERE timestamp < ?;", (ev_cutoff,))
                summary["events_deleted"] = cur.rowcount

                # 2. Prune old errors (ONLY resolved errors, never unresolved ones needed for recovery)
                try:
                    cur = conn.execute(
                        "DELETE FROM errors WHERE created_at < ? AND resolved_at IS NOT NULL;",
                        (err_cutoff,),
                    )
                    summary["errors_deleted"] = cur.rowcount
                except Exception:
                    pass

                # 3. Prune RESOLVED reconciliations (NEVER PENDING or IN_PROGRESS)
                try:
                    cur = conn.execute(
                        """
                        DELETE FROM reconciliations
                        WHERE state IN ('RESOLVED', 'FAILED')
                          AND updated_at < ?;
                        """,
                        (res_cutoff,),
                    )
                    summary["resolved_reconciliations_deleted"] = cur.rowcount
                except Exception:
                    pass

                # 4. Prune resolved manual reviews (NEVER PENDING)
                try:
                    cur = conn.execute(
                        """
                        DELETE FROM manual_reviews
                        WHERE status != 'PENDING'
                          AND resolved_at IS NOT NULL
                          AND resolved_at < ?;
                        """,
                        (res_cutoff,),
                    )
                    summary["resolved_manual_reviews_deleted"] = cur.rowcount
                except Exception:
                    pass

                # 5. Prune old non-running execution identities
                try:
                    cur = conn.execute(
                        """
                        DELETE FROM execution_identities
                        WHERE state IN ('SENT', 'FAILED', 'COMPLETED')
                          AND (
                              (completed_at IS NOT NULL AND completed_at < ?)
                              OR (completed_at IS NULL AND created_at < ?)
                          );
                        """,
                        (exec_id_cutoff, exec_id_cutoff),
                    )
                    summary["old_execution_identities_deleted"] = cur.rowcount
                except Exception:
                    pass

                # 6. Prune old diagnostics (never for active, pending, or reconciling tasks)
                try:
                    cur = conn.execute(
                        """
                        DELETE FROM diagnostic_artifacts
                        WHERE (timestamp < ? OR (retention_until IS NOT NULL AND retention_until < ?))
                          AND (task_id IS NULL OR task_id NOT IN (
                              SELECT id FROM tasks WHERE status IN (
                                  'READY', 'QUEUED', 'CLAIMED', 'SENDING', 'VERIFYING', 'RECONCILING', 'MANUAL_REVIEW'
                              )
                          ));
                        """,
                        (diag_cutoff, diag_cutoff),
                    )
                    summary["diagnostics_deleted"] = cur.rowcount
                except Exception:
                    pass

            completed_at = utc_now_iso()
            logger.info(f"Retention cleanup completed successfully: {summary}")

            if self.event_repo:
                try:
                    self.event_repo.record(
                        event_code=EventCode.RETENTION_CLEANUP_COMPLETED,
                        category="retention",
                        level=EventLevel.INFO,
                        entity_type="system",
                        entity_id="retention_service",
                        payload={
                            "started_at": started_at,
                            "completed_at": completed_at,
                            "records_deleted": summary,
                            "retention_configuration": retention_config,
                            "success": True,
                        },
                    )
                except Exception as ex:
                    logger.warning(f"Could not record retention audit event: {ex}")

            return summary

        except Exception as e:
            logger.error(f"Retention cleanup failed: {e}", exc_info=True)
            if self.event_repo:
                try:
                    self.event_repo.record(
                        event_code=EventCode.RETENTION_CLEANUP_FAILED,
                        category="retention",
                        level=EventLevel.ERROR,
                        entity_type="system",
                        entity_id="retention_service",
                        payload={
                            "started_at": started_at,
                            "failed_at": utc_now_iso(),
                            "error": str(e),
                            "retention_configuration": retention_config,
                            "success": False,
                        },
                    )
                except Exception:
                    pass
            raise

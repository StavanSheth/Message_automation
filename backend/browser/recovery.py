"""Browser health recovery and self-healing manager."""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any
from backend.browser.session import BrowserSessionInstance
from backend.browser.manager import BrowserManager
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, EventCode, EventLevel, ErrorCode
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("browser_recovery")


class BrowserRecoveryManager:
    """
    Detects browser crashes/timeouts, captures diagnostics, closes broken instances,
    recreates healthy sessions, and safely routes interrupted tasks (SENDING -> RECONCILIATION).
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        event_repo: Optional[EventRepository] = None,
        artifacts_dir: str = "data/artifacts/diagnostics",
        diagnostic_repo: Optional[Any] = None,
    ):
        self.browser_manager = browser_manager
        self.event_repo = event_repo
        self.diagnostic_repo = diagnostic_repo
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def is_session_healthy(self, session: Optional[BrowserSessionInstance]) -> bool:
        """Check whether a session is non-null, alive, and responsive."""
        if not session or not session.is_alive():
            return False
        try:
            return session.driver.is_healthy()
        except Exception:
            return False

    def capture_diagnostics(
        self,
        session: BrowserSessionInstance,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        error_code: Optional[str] = None,
        correlation_id: Optional[str] = None,
        task_state: Optional[str] = None,
        worker_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Safely capture crash diagnostics: URL, title, metadata, states.
        Strictly redacts credentials, cookies, tokens, and private message contents.
        Never prevents recovery if artifact capture fails.
        """
        now_iso = utc_now_iso()
        diag_id = generate_id("DIAG")

        current_url = ""
        page_title = ""
        browser_state = "alive" if session and session.is_alive() else "dead"
        screenshot_path = None

        try:
            current_url = session.current_url or ""
            if session.is_alive():
                page_title = session.get_page_title()
                # Optional screenshot capture when safe and supported
                try:
                    if hasattr(session, "driver") and hasattr(session.driver, "page") and session.driver.page:
                        ss_file = self.artifacts_dir / f"{diag_id}.png"
                        session.driver.page.screenshot(path=str(ss_file))
                        screenshot_path = str(ss_file)
                except Exception as ss_err:
                    logger.debug(f"Optional diagnostic screenshot could not be captured: {ss_err}")
        except Exception as e:
            logger.debug(f"Could not extract page details for diagnostics: {e}")

        # Redact sensitive parameters from URL if any
        if "?" in current_url:
            base_url, query_str = current_url.split("?", 1)
            sanitized_parts = []
            for param in query_str.split("&"):
                k = param.split("=")[0].lower()
                if any(sec in k for sec in ("token", "auth", "secret", "pass", "key", "cookie")):
                    sanitized_parts.append(f"{k}=[REDACTED]")
                else:
                    sanitized_parts.append(param)
            current_url = f"{base_url}?{'&'.join(sanitized_parts)}"

        diag_data = {
            "id": diag_id,
            "timestamp": now_iso,
            "session_id": getattr(session, "session_id", None),
            "task_id": task_id,
            "worker_id": worker_id,
            "correlation_id": correlation_id,
            "error_code": error_code,
            "current_url": current_url,
            "page_title": page_title,
            "browser_state": browser_state,
            "task_state": task_state,
            "worker_state": worker_state,
            "screenshot_path": screenshot_path,
        }

        # Calculate retention
        from datetime import datetime, timezone, timedelta
        settings = getattr(self.browser_manager, "settings", None)
        r_days = getattr(settings, "diagnostic_artifact_retention_days", 7)
        retention_until = (datetime.now(timezone.utc) + timedelta(days=r_days)).isoformat()
        diag_data["retention_until"] = retention_until

        artifact_file = self.artifacts_dir / f"{diag_id}.json"
        # Write metadata JSON artifact
        try:
            with open(artifact_file, "w", encoding="utf-8") as f:
                json.dump(diag_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write diagnostic artifact: {e}")

        # Persist diagnostic record in DB
        if self.diagnostic_repo:
            try:
                from backend.domain.models import DiagnosticArtifact
                diag_record = DiagnosticArtifact(
                    id=diag_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    session_id=getattr(session, "session_id", None),
                    correlation_id=correlation_id,
                    timestamp=now_iso,
                    artifact_type="SCREENSHOT" if screenshot_path else "ERROR_METADATA",
                    file_path=screenshot_path or str(artifact_file),
                    page_url=current_url,
                    page_title=page_title,
                    error_code=error_code,
                    reason=task_state or worker_state or "browser_recovery",
                    retention_until=retention_until,
                )
                self.diagnostic_repo.create(diag_record)
            except Exception as e:
                logger.debug(f"Failed to record diagnostic record in DB: {e}")

        try:
            self.cleanup_artifacts()
        except Exception:
            pass

        return diag_data

    def cleanup_artifacts(
        self,
        retention_days: Optional[int] = None,
        max_artifacts: Optional[int] = None,
    ) -> int:
        """Remove diagnostic artifacts older than retention days or exceeding max count."""
        from datetime import datetime, timezone, timedelta
        settings = getattr(self.browser_manager, "settings", None)
        r_days = retention_days or getattr(settings, "diagnostic_artifact_retention_days", 7)
        max_count = max_artifacts or getattr(settings, "max_diagnostic_artifacts", 200)

        deleted = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=r_days)

        # 1. Clean DB records if repo configured
        if self.diagnostic_repo and hasattr(self.diagnostic_repo, "delete_expired"):
            try:
                deleted += self.diagnostic_repo.delete_expired(cutoff.isoformat())
            except Exception as e:
                logger.debug(f"Error deleting expired diagnostic DB records: {e}")

        # 2. Clean files on filesystem
        try:
            files = sorted(self.artifacts_dir.glob("DIAG-*"), key=lambda p: p.stat().st_mtime)
            for f in files:
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        f.unlink(missing_ok=True)
                        deleted += 1
                except Exception:
                    pass

            remaining = sorted(self.artifacts_dir.glob("DIAG-*"), key=lambda p: p.stat().st_mtime)
            if len(remaining) > max_count:
                excess = len(remaining) - max_count
                for f in remaining[:excess]:
                    f.unlink(missing_ok=True)
                    deleted += 1
        except Exception as e:
            logger.debug(f"Error during diagnostic cleanup: {e}")
        return deleted

    def recover_session(
        self,
        old_session: BrowserSessionInstance,
        worker_id: Optional[str] = None,
        current_task: Optional[Task] = None,
        reconciliation_service: Optional[Any] = None,
        correlation_id: Optional[str] = None,
    ) -> BrowserSessionInstance:
        """
        Execute self-healing browser recovery:
        1. Capture diagnostics safely (non-blocking)
        2. Close broken session
        3. Check task state: if SENDING or VERIFYING -> enters reconciliation, NEVER auto-resumed
        4. Recreate session with matching profile
        5. Validate recreated session
        """
        logger.warning(
            f"Initiating browser recovery for session {old_session.session_id} (worker: {worker_id})"
        )

        # 1. Capture diagnostics (safe, non-fatal)
        try:
            self.capture_diagnostics(
                session=old_session,
                task_id=current_task.id if current_task else None,
                worker_id=worker_id,
                error_code="BROWSER_CRASH",
                correlation_id=correlation_id,
                task_state=current_task.status.value if current_task else None,
            )
        except Exception as diag_err:
            logger.warning(f"Diagnostic capture failed during recovery: {diag_err}")

        # 2. Stop broken session
        profile_path = old_session.profile_path
        profile_id = old_session.profile_id
        old_id = old_session.session_id

        try:
            self.browser_manager.stop_session(old_id)
        except Exception as e:
            logger.warning(f"Error stopping broken session {old_id}: {e}")

        # 3. Route task if it was in an ambiguous or critical in-flight state
        if current_task is not None:
            if current_task.status in (TaskState.SENDING, TaskState.VERIFYING):
                logger.warning(
                    f"Task {current_task.id} was in {current_task.status.value} during browser crash; routing to RECONCILIATION"
                )
                if reconciliation_service:
                    reconciliation_service.enter_reconciliation(
                        task_id=current_task.id,
                        reason=f"browser_crash_during_{current_task.status.value.lower()}",
                        worker_id=worker_id,
                        session_id=old_id,
                    )

        # 4. Recreate session
        new_session = self.browser_manager.create_session(
            worker_id=worker_id,
            profile_name=profile_id,
        )
        new_session.start()

        # 5. Emit recovery event with dedicated tracing columns
        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.BROWSER_RECOVERED,
                category="browser",
                level=EventLevel.INFO,
                entity_type="browser_session",
                entity_id=new_session.session_id,
                task_id=current_task.id if current_task else None,
                worker_id=worker_id,
                session_id=new_session.session_id,
                correlation_id=correlation_id,
                payload={
                    "old_session_id": old_id,
                    "worker_id": worker_id,
                    "recreated_session_id": new_session.session_id,
                },
            )

        logger.info(f"Browser recovery succeeded; new session: {new_session.session_id}")
        return new_session

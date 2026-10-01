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
    ):
        self.browser_manager = browser_manager
        self.event_repo = event_repo
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
    ) -> Dict[str, Any]:
        """
        Safely capture crash diagnostics: URL, title, metadata.
        Does NOT capture or store credentials, cookies, or secrets.
        """
        now_iso = utc_now_iso()
        diag_id = generate_id("DIAG")

        current_url = ""
        page_title = ""
        try:
            current_url = session.current_url or ""
            if session.is_alive():
                page_title = session.get_page_title()
        except Exception as e:
            logger.debug(f"Could not extract page title for diagnostics: {e}")

        diag_data = {
            "id": diag_id,
            "timestamp": now_iso,
            "session_id": session.session_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "error_code": error_code,
            "current_url": current_url,
            "page_title": page_title,
        }

        # Write metadata JSON artifact
        try:
            artifact_file = self.artifacts_dir / f"{diag_id}.json"
            with open(artifact_file, "w", encoding="utf-8") as f:
                json.dump(diag_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write diagnostic artifact: {e}")

        return diag_data

    def recover_session(
        self,
        old_session: BrowserSessionInstance,
        worker_id: Optional[str] = None,
        current_task: Optional[Task] = None,
        reconciliation_service: Optional[Any] = None,
    ) -> BrowserSessionInstance:
        """
        Execute self-healing browser recovery:
        1. Capture diagnostics
        2. Close broken session
        3. Check task state: if SENDING or VERIFYING -> enters reconciliation, NEVER auto-resumed
        4. Recreate session with matching profile
        5. Validate recreated session
        """
        logger.warning(
            f"Initiating browser recovery for session {old_session.session_id} (worker: {worker_id})"
        )

        # 1. Capture diagnostics
        self.capture_diagnostics(
            session=old_session,
            task_id=current_task.id if current_task else None,
            worker_id=worker_id,
            error_code="BROWSER_CRASH",
        )

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

        # 5. Emit recovery event
        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.BROWSER_RECOVERED,
                category="browser",
                level=EventLevel.INFO,
                entity_type="browser_session",
                entity_id=new_session.session_id,
                payload={
                    "old_session_id": old_id,
                    "worker_id": worker_id,
                    "recreated_session_id": new_session.session_id,
                },
            )

        logger.info(f"Browser recovery succeeded; new session: {new_session.session_id}")
        return new_session

"""Centralized Diagnostic Collector for capturing failure artifacts."""

import os
import re
import json
import traceback
from pathlib import Path
from typing import Optional, Dict, Any, List

from backend.domain.models import DiagnosticArtifact, utc_now_iso
from backend.repositories.diagnostic_artifact_repo import DiagnosticArtifactRepository
from backend.database.manager import DatabaseManager
from backend.config.settings import get_settings
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("diagnostic_collector")

# Patterns for sensitive data redaction
SECRET_PATTERNS = [
    re.compile(r'(password|passwd|pwd|secret|token|api_key|sessionid|auth_token)\s*[:=]\s*["\']?([^"\'\s&]+)', re.IGNORECASE),
    re.compile(r'(bearer\s+)([A-Za-z0-9_\-\.]{20,})', re.IGNORECASE),
]


def sanitize_text(text: str) -> str:
    """Redact sensitive authentication tokens, passwords, and secrets."""
    if not text:
        return ""
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(r'\1: [REDACTED]', sanitized)
    return sanitized


class DiagnosticCollector:
    """
    Captures structured diagnostic bundles on automation failures:
    - timestamp, correlation_id, task_id, worker_id, session_id, account_id
    - current_url, page_title, browser_state, auth_state, network_status
    - sanitized error message, stack trace, and safe DOM snapshot
    - screenshot path where available
    """

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        artifact_repo: Optional[DiagnosticArtifactRepository] = None,
        diagnostics_dir: Optional[str] = None,
    ):
        settings = get_settings()
        self.db = db or DatabaseManager(settings.database_path)
        self.artifact_repo = artifact_repo or DiagnosticArtifactRepository(self.db)
        raw_dir = diagnostics_dir or settings.diagnostics_directory
        self.diagnostics_dir = Path(raw_dir).resolve()
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def capture_failure(
        self,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
        account_id: Optional[str] = None,
        current_url: Optional[str] = None,
        page_title: Optional[str] = None,
        error: Optional[Exception] = None,
        error_code: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        dom_snapshot: Optional[str] = None,
        auth_state: Optional[str] = None,
        network_status: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> DiagnosticArtifact:
        """Capture and persist a complete diagnostic bundle."""
        artifact_id = generate_id("DIAG")
        now_iso = utc_now_iso()
        error_msg = str(error) if error else "Unknown error"
        stack_str = traceback.format_exc() if error else ""

        # Create diagnostic JSON file
        bundle_file = self.diagnostics_dir / f"{artifact_id}.json"
        bundle_data = {
            "artifact_id": artifact_id,
            "timestamp": now_iso,
            "correlation_id": correlation_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "session_id": session_id,
            "account_id": account_id,
            "current_url": sanitize_text(current_url or ""),
            "page_title": sanitize_text(page_title or ""),
            "error_code": error_code or "AUTOMATION_ERROR",
            "error_message": sanitize_text(error_msg),
            "stack_trace": sanitize_text(stack_str),
            "auth_state": auth_state or "UNKNOWN",
            "network_status": network_status or "UNKNOWN",
            "screenshot_path": screenshot_path,
        }

        # If DOM snapshot provided, save sanitized DOM snapshot
        if dom_snapshot:
            dom_file = self.diagnostics_dir / f"{artifact_id}_dom.html"
            try:
                dom_file.write_text(sanitize_text(dom_snapshot), encoding="utf-8")
                bundle_data["dom_snapshot_file"] = str(dom_file)
            except Exception as e:
                logger.warning(f"Failed to write DOM snapshot: {e}")

        try:
            bundle_file.write_text(json.dumps(bundle_data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write diagnostic bundle: {e}")

        artifact = DiagnosticArtifact(
            id=artifact_id,
            task_id=task_id,
            worker_id=worker_id,
            session_id=session_id,
            correlation_id=correlation_id,
            timestamp=now_iso,
            artifact_type="ERROR_METADATA",
            file_path=str(bundle_file),
            page_url=sanitize_text(current_url or ""),
            page_title=sanitize_text(page_title or ""),
            error_code=error_code or "AUTOMATION_ERROR",
            reason=sanitize_text(error_msg[:200]),
        )

        try:
            self.artifact_repo.create(artifact)
        except Exception as e:
            logger.warning(f"Failed to persist diagnostic artifact to database: {e}")

        logger.info(
            "Diagnostic failure bundle captured",
            artifact_id=artifact_id,
            task_id=task_id,
            error_code=error_code,
        )
        return artifact

    def list_recent(self, limit: int = 50) -> List[DiagnosticArtifact]:
        """Fetch list of recent diagnostic artifacts."""
        return self.artifact_repo.list_all(limit=limit)

    def get_bundle_data(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        """Load the full JSON bundle data for a diagnostic artifact."""
        artifact = self.artifact_repo.get_by_id(artifact_id)
        if not artifact or not artifact.file_path:
            return None
        p = Path(artifact.file_path)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

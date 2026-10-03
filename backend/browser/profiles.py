"""Browser profile management with isolation and path safety."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List

from backend.domain.models import utc_now_iso
from backend.domain.errors import ValidationError
from backend.browser.browser_types import BrowserType
from backend.config.settings import get_settings


@dataclass
class BrowserProfile:
    profile_id: str
    profile_name: str
    browser_type: BrowserType
    profile_path: str
    status: str = "ACTIVE"
    created_at: str = field(default_factory=utc_now_iso)
    last_used_at: str = field(default_factory=utc_now_iso)


class BrowserProfileManager:
    """
    Manages isolated browser profile directories.
    Enforces profile paths outside sensitive project code directories.
    """

    def __init__(self, base_directory: Optional[str] = None):
        settings = get_settings()
        raw_dir = base_directory or settings.browser_profile_directory
        self.base_directory = Path(raw_dir).resolve()
        self._profiles: Dict[str, BrowserProfile] = {}
        self._validate_base_directory(self.base_directory)
        self.base_directory.mkdir(parents=True, exist_ok=True)
        self.discover_existing_profiles()

    def _validate_base_directory(self, path: Path) -> None:
        """Ensure profile path is safe and not placed inside code or document folders."""
        forbidden_subdirs = {"backend", "tests", "document", "docs", ".git"}
        for parent in [path] + list(path.parents):
            if parent.name.lower() in forbidden_subdirs:
                raise ValidationError(
                    f"Browser profile directory cannot reside within '{parent.name}': {path}"
                )

    def create_or_get_profile(
        self, profile_name: str, browser_type: BrowserType = BrowserType.CHROMIUM
    ) -> BrowserProfile:
        """Create or return an isolated browser profile."""
        if ".." in profile_name or "/" in profile_name or "\\" in profile_name:
            raise ValidationError(f"Profile path traversal detected: {profile_name}")

        clean_name = "".join(c for c in profile_name if c.isalnum() or c in ("-", "_")).strip()
        if not clean_name:
            raise ValidationError(f"Invalid profile name: '{profile_name}'")

        profile_id = f"prof_{clean_name}"
        if profile_id in self._profiles:
            prof = self._profiles[profile_id]
            prof.last_used_at = utc_now_iso()
            return prof

        profile_path = (self.base_directory / clean_name).resolve()
        # Ensure profile path does not escape base directory
        try:
            profile_path.relative_to(self.base_directory)
        except ValueError:
            raise ValidationError(f"Profile path traversal detected: {profile_name}")

        self._validate_base_directory(profile_path)
        profile_path.mkdir(parents=True, exist_ok=True)

        now_iso = utc_now_iso()
        profile = BrowserProfile(
            profile_id=profile_id,
            profile_name=clean_name,
            browser_type=browser_type,
            profile_path=str(profile_path),
            status="ACTIVE",
            created_at=now_iso,
            last_used_at=now_iso,
        )
        self._profiles[profile_id] = profile
        return profile

    def create_profile(
        self, profile_name: str, browser_type: BrowserType = BrowserType.CHROMIUM
    ) -> BrowserProfile:
        return self.create_or_get_profile(profile_name, browser_type)

    def delete_profile(self, profile_id: str) -> bool:
        """Remove profile from tracking."""
        if profile_id in self._profiles:
            del self._profiles[profile_id]
            return True
        return False

    def get_profile(self, profile_id: str) -> Optional[BrowserProfile]:
        return self._profiles.get(profile_id)

    def list_profiles(self) -> List[BrowserProfile]:
        return list(self._profiles.values())

    def discover_existing_profiles(self) -> List[BrowserProfile]:
        """
        Scan the base directory for profile folders that persist from previous runs.
        Re-registers them in the in-memory cache for reuse.
        """
        discovered: List[BrowserProfile] = []
        if not self.base_directory.is_dir():
            return discovered

        for entry in self.base_directory.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                profile_id = f"prof_{entry.name}"
                if profile_id not in self._profiles:
                    now_iso = utc_now_iso()
                    profile = BrowserProfile(
                        profile_id=profile_id,
                        profile_name=entry.name,
                        browser_type=BrowserType.CHROMIUM,
                        profile_path=str(entry.resolve()),
                        status="ACTIVE",
                        created_at=now_iso,
                        last_used_at=now_iso,
                    )
                    self._profiles[profile_id] = profile
                discovered.append(self._profiles[profile_id])

        return discovered

    def deactivate_profile(self, profile_id: str) -> bool:
        """Mark a profile as inactive without deleting files."""
        profile = self._profiles.get(profile_id)
        if not profile:
            return False
        profile.status = "INACTIVE"
        return True

    def _get_lock_file(self, profile_path: Path) -> Path:
        return profile_path / ".profile.lock"

    def _get_account_file(self, profile_path: Path) -> Path:
        return profile_path / ".account_id"

    def is_locked(self, profile_id: str) -> bool:
        """Check if profile has an active lock held by a live process."""
        profile = self.get_profile(profile_id)
        if not profile:
            return False
        p = Path(profile.profile_path)
        lock_file = self._get_lock_file(p)
        if not lock_file.exists():
            return False
        try:
            import json
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = data.get("pid")
            from backend.browser.driver import is_pid_alive
            if pid and not is_pid_alive(pid):
                # Stale lock from crashed process
                lock_file.unlink(missing_ok=True)
                return False
            return True
        except Exception:
            return False

    def acquire_lock(
        self, profile_id: str, worker_id: str, account_id: Optional[str] = None
    ) -> bool:
        """
        Acquire an exclusive lock on a browser profile for an assigned worker.
        Prevents multiple workers or accounts from accessing the same profile simultaneously.
        """
        profile = self.get_profile(profile_id)
        if not profile:
            raise ValidationError(f"Profile '{profile_id}' not found")
        p = Path(profile.profile_path)
        lock_file = self._get_lock_file(p)
        import json

        if lock_file.exists():
            try:
                data = json.loads(lock_file.read_text(encoding="utf-8"))
                holding_worker = data.get("worker_id")
                holding_account = data.get("account_id")
                holding_pid = data.get("pid")
                from backend.browser.driver import is_pid_alive
                if holding_pid and is_pid_alive(holding_pid):
                    if holding_worker == worker_id and (holding_account == account_id or not account_id):
                        return True
                    raise ValidationError(
                        f"Profile '{profile_id}' is locked by worker '{holding_worker}' (PID {holding_pid}) "
                        f"for account '{holding_account}'. Simultaneous access is forbidden."
                    )
                else:
                    # Clean up stale lock
                    lock_file.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                lock_file.unlink(missing_ok=True)

        # Validate account ownership if account file exists
        acc_file = self._get_account_file(p)
        if account_id:
            if acc_file.exists():
                bound_acc = acc_file.read_text(encoding="utf-8").strip()
                if bound_acc and bound_acc != account_id:
                    raise ValidationError(
                        f"Profile '{profile_id}' belongs to account '{bound_acc}', cannot be used by '{account_id}'"
                    )
            else:
                acc_file.write_text(account_id, encoding="utf-8")

        lock_data = {
            "profile_id": profile_id,
            "worker_id": worker_id,
            "account_id": account_id,
            "pid": os.getpid(),
            "locked_at": utc_now_iso(),
        }
        lock_file.write_text(json.dumps(lock_data), encoding="utf-8")
        return True

    def release_lock(self, profile_id: str, worker_id: Optional[str] = None) -> bool:
        """Release exclusive lock on profile if held by worker or unconditionally."""
        profile = self.get_profile(profile_id)
        if not profile:
            return False
        p = Path(profile.profile_path)
        lock_file = self._get_lock_file(p)
        if not lock_file.exists():
            return True
        try:
            if worker_id:
                import json
                data = json.loads(lock_file.read_text(encoding="utf-8"))
                if data.get("worker_id") != worker_id:
                    return False
            lock_file.unlink(missing_ok=True)
            return True
        except Exception:
            lock_file.unlink(missing_ok=True)
            return True

    def validate_profile(
        self,
        profile_id: str,
        expected_account_id: Optional[str] = None,
        current_worker_id: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Comprehensive profile validation:
        - profile exists
        - profile writable
        - profile not corrupted
        - profile belongs to account
        - profile isn't locked by another worker
        """
        profile = self.get_profile(profile_id)
        if not profile:
            return False, "profile_does_not_exist"
        p = Path(profile.profile_path)
        if not p.is_dir():
            return False, "profile_directory_missing"

        # Check writeability and corruption
        test_file = p / ".profile_health_check"
        try:
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except Exception as e:
            return False, f"profile_directory_unwritable: {e}"

        # Check account ownership
        acc_file = self._get_account_file(p)
        if expected_account_id and acc_file.exists():
            bound_acc = acc_file.read_text(encoding="utf-8").strip()
            if bound_acc and bound_acc != expected_account_id:
                return False, f"account_mismatch: profile bound to {bound_acc}, expected {expected_account_id}"

        # Check lock
        if self.is_locked(profile_id):
            if current_worker_id:
                try:
                    import json
                    lock_data = json.loads(self._get_lock_file(p).read_text(encoding="utf-8"))
                    if lock_data.get("worker_id") != current_worker_id:
                        return False, f"locked_by_worker_{lock_data.get('worker_id')}"
                except Exception:
                    pass
            else:
                return False, "profile_locked"

        return True, None

    def validate_profile_usability(self, profile_id: str) -> bool:
        """Validate that persistent profile directory exists and is usable across restarts."""
        ok, _ = self.validate_profile(profile_id)
        return ok


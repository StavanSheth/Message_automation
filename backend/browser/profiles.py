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

    def validate_profile_usability(self, profile_id: str) -> bool:
        """Validate that persistent profile directory exists and is usable across restarts."""
        profile = self.get_profile(profile_id)
        if not profile:
            return False
        p = Path(profile.profile_path)
        if not p.is_dir():
            return False
        # Check readability and writeability
        test_file = p / ".profile_health_check"
        try:
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return True
        except Exception:
            return False


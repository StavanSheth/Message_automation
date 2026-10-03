"""Deterministic browser runtime detection, executable discovery, and diagnostic validation."""

import os
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from backend.browser.browser_types import BrowserEngine, BrowserType
from backend.events.logger import get_logger

logger = get_logger("browser_runtime")

# Standard Windows and Linux installation paths for Chrome and Edge
CHROME_CANDIDATE_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/google-chrome",
]

EDGE_CANDIDATE_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]

CHROMIUM_CANDIDATE_PATHS = [
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]


@dataclass
class BrowserRuntimeDiagnostic:
    playwright_installed: bool
    configured_engine: str
    resolved_engine: str
    executable_path: Optional[str]
    executable_exists: bool
    can_launch: bool
    detected_chrome: Optional[str] = None
    detected_edge: Optional[str] = None
    detected_chromium: Optional[str] = None
    error_message: Optional[str] = None
    actionable_fix: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class BrowserRuntimeValidator:
    """
    Validates browser automation runtime before launching:
    Python Playwright -> Browser binary -> Executable path -> Launch capability
    Never silently falls back between incompatible engines without explicit reporting.
    """

    @staticmethod
    def is_playwright_installed() -> bool:
        try:
            import playwright
            return True
        except ImportError:
            return False

    @staticmethod
    def find_chrome() -> Optional[str]:
        # 1. Environment variable override
        env_path = os.environ.get("CHROME_EXECUTABLE_PATH") or os.environ.get("BROWSER_EXECUTABLE_PATH")
        if env_path and os.path.isfile(env_path):
            return str(Path(env_path).resolve())

        # 2. Known candidates
        for c in CHROME_CANDIDATE_PATHS:
            if c and os.path.isfile(c):
                return str(Path(c).resolve())

        # 3. Path lookup
        which_chrome = shutil.which("chrome") or shutil.which("google-chrome")
        if which_chrome and os.path.isfile(which_chrome):
            return str(Path(which_chrome).resolve())

        return None

    @staticmethod
    def find_edge() -> Optional[str]:
        for c in EDGE_CANDIDATE_PATHS:
            if c and os.path.isfile(c):
                return str(Path(c).resolve())
        which_edge = shutil.which("msedge") or shutil.which("microsoft-edge")
        if which_edge and os.path.isfile(which_edge):
            return str(Path(which_edge).resolve())
        return None

    @staticmethod
    def find_chromium() -> Optional[str]:
        for c in CHROMIUM_CANDIDATE_PATHS:
            if c and os.path.isfile(c):
                return str(Path(c).resolve())
        which_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
        if which_chromium and os.path.isfile(which_chromium):
            return str(Path(which_chromium).resolve())
        return None

    @classmethod
    def validate_runtime(
        cls,
        engine: BrowserEngine = BrowserEngine.PLAYWRIGHT_CHROMIUM,
        custom_executable_path: Optional[str] = None,
    ) -> BrowserRuntimeDiagnostic:
        """
        Validate browser runtime prerequisites deterministically.
        Returns full diagnostic result with actionable instructions on failure.
        """
        playwright_ok = cls.is_playwright_installed()
        chrome_path = cls.find_chrome()
        edge_path = cls.find_edge()
        chromium_path = cls.find_chromium()

        target_exec = custom_executable_path or os.environ.get("CHROME_EXECUTABLE_PATH") or os.environ.get("BROWSER_EXECUTABLE_PATH")
        resolved_engine = engine.value if isinstance(engine, BrowserEngine) else str(engine)

        if not playwright_ok:
            return BrowserRuntimeDiagnostic(
                playwright_installed=False,
                configured_engine=str(engine),
                resolved_engine=resolved_engine,
                executable_path=None,
                executable_exists=False,
                can_launch=False,
                detected_chrome=chrome_path,
                detected_edge=edge_path,
                detected_chromium=chromium_path,
                error_message="Playwright Python package is not installed.",
                actionable_fix="Run 'pip install playwright' followed by 'playwright install chromium'.",
            )

        # Resolve executable path based on engine
        if engine in (BrowserEngine.SYSTEM_CHROME, BrowserEngine.CHROME if hasattr(BrowserEngine, "CHROME") else "chrome"):
            target_exec = target_exec or chrome_path
            resolved_engine = BrowserEngine.SYSTEM_CHROME.value
            if not target_exec:
                return BrowserRuntimeDiagnostic(
                    playwright_installed=True,
                    configured_engine=str(engine),
                    resolved_engine=resolved_engine,
                    executable_path=None,
                    executable_exists=False,
                    can_launch=False,
                    detected_chrome=None,
                    detected_edge=edge_path,
                    detected_chromium=chromium_path,
                    error_message="Google Chrome executable not found on this system.",
                    actionable_fix="Install Google Chrome from https://www.google.com/chrome or set CHROME_EXECUTABLE_PATH environment variable.",
                )
        elif engine in (BrowserEngine.SYSTEM_EDGE,):
            target_exec = target_exec or edge_path
            resolved_engine = BrowserEngine.SYSTEM_EDGE.value
            if not target_exec:
                return BrowserRuntimeDiagnostic(
                    playwright_installed=True,
                    configured_engine=str(engine),
                    resolved_engine=resolved_engine,
                    executable_path=None,
                    executable_exists=False,
                    can_launch=False,
                    detected_chrome=chrome_path,
                    detected_edge=None,
                    detected_chromium=chromium_path,
                    error_message="Microsoft Edge executable not found on this system.",
                    actionable_fix="Install Microsoft Edge or configure PLAYWRIGHT_CHROMIUM.",
                )
        else:
            # Default: PLAYWRIGHT_CHROMIUM
            resolved_engine = BrowserEngine.PLAYWRIGHT_CHROMIUM.value
            # For Playwright Chromium, bundled browser is managed by playwright
            target_exec = target_exec or chromium_path or chrome_path

        # If a specific executable path was provided or resolved, verify it exists
        exec_exists = True
        if target_exec:
            exec_exists = os.path.isfile(target_exec)
            if not exec_exists:
                return BrowserRuntimeDiagnostic(
                    playwright_installed=True,
                    configured_engine=str(engine),
                    resolved_engine=resolved_engine,
                    executable_path=target_exec,
                    executable_exists=False,
                    can_launch=False,
                    detected_chrome=chrome_path,
                    detected_edge=edge_path,
                    detected_chromium=chromium_path,
                    error_message=f"Configured browser executable not found: {target_exec}",
                    actionable_fix=f"Verify path '{target_exec}' or unset BROWSER_EXECUTABLE_PATH to use auto-detection.",
                )

        return BrowserRuntimeDiagnostic(
            playwright_installed=True,
            configured_engine=str(engine),
            resolved_engine=resolved_engine,
            executable_path=target_exec,
            executable_exists=exec_exists,
            can_launch=True,
            detected_chrome=chrome_path,
            detected_edge=edge_path,
            detected_chromium=chromium_path,
            error_message=None,
            actionable_fix=None,
            details={
                "os": os.name,
                "chrome": chrome_path,
                "edge": edge_path,
            },
        )

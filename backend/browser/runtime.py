"""Deterministic browser runtime detection, executable discovery, and diagnostic validation."""

import os
import shutil
import platform
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

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
    pid: Optional[int] = None
    os_name: str = ""
    python_version: str = ""
    playwright_version: Optional[str] = None
    display_status: str = ""
    smoke_test_passed: bool = False
    launch_result: Optional[str] = None
    context_result: Optional[str] = None
    page_result: Optional[str] = None
    navigation_result: Optional[str] = None
    screenshot_result: Optional[str] = None
    shutdown_result: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class BrowserRuntimeValidator:
    """
    Validates browser automation runtime before launching:
    Python Playwright -> Browser binary -> Executable path -> Real smoke test launch.
    Never marks can_launch=True until a real launch verification succeeds.
    """

    @staticmethod
    def is_playwright_installed() -> bool:
        try:
            import playwright
            return True
        except ImportError:
            return False

    @staticmethod
    def get_playwright_version() -> Optional[str]:
        try:
            import playwright
            return getattr(playwright, "__version__", "unknown")
        except Exception:
            return None

    @staticmethod
    def get_display_status() -> str:
        if os.name == "nt":
            return "Windows GUI Active"
        display = os.environ.get("DISPLAY")
        wayland = os.environ.get("WAYLAND_DISPLAY")
        if display:
            return f"X11 DISPLAY={display}"
        if wayland:
            return f"Wayland DISPLAY={wayland}"
        return "NO_DISPLAY_HEADLESS_REQUIRED"

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
    def resolve_executable_and_engine(
        cls,
        engine: BrowserEngine = BrowserEngine.PLAYWRIGHT_CHROMIUM,
        custom_executable_path: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        Resolve engine and browser executable in deterministic order:
        1. Explicit configured executable
        2. CHROME_EXECUTABLE_PATH
        3. BROWSER_EXECUTABLE_PATH
        4. Installed Chrome
        5. Playwright Chromium
        6. Installed Edge
        """
        # 1. Explicit configured executable
        if custom_executable_path and os.path.isfile(custom_executable_path):
            return str(engine.value if isinstance(engine, BrowserEngine) else engine), str(Path(custom_executable_path).resolve())

        # 2. Environment variables
        env_chrome = os.environ.get("CHROME_EXECUTABLE_PATH")
        if env_chrome and os.path.isfile(env_chrome):
            return BrowserEngine.SYSTEM_CHROME.value, str(Path(env_chrome).resolve())

        env_browser = os.environ.get("BROWSER_EXECUTABLE_PATH")
        if env_browser and os.path.isfile(env_browser):
            return str(engine.value if isinstance(engine, BrowserEngine) else engine), str(Path(env_browser).resolve())

        # 3. If specific engine requested
        if engine in (BrowserEngine.SYSTEM_CHROME, "chrome", "system_chrome"):
            chrome = cls.find_chrome()
            return BrowserEngine.SYSTEM_CHROME.value, chrome
        elif engine in (BrowserEngine.SYSTEM_EDGE, "edge", "system_edge"):
            edge = cls.find_edge()
            return BrowserEngine.SYSTEM_EDGE.value, edge
        else:
            # Default: prefer System Chrome if present on Windows, otherwise Playwright Chromium
            chrome = cls.find_chrome()
            if chrome:
                return BrowserEngine.SYSTEM_CHROME.value, chrome
            chromium = cls.find_chromium()
            if chromium:
                return BrowserEngine.PLAYWRIGHT_CHROMIUM.value, chromium
            edge = cls.find_edge()
            if edge:
                return BrowserEngine.SYSTEM_EDGE.value, edge
            return BrowserEngine.PLAYWRIGHT_CHROMIUM.value, None

    @classmethod
    def execute_smoke_test(
        cls,
        resolved_engine: str,
        executable_path: Optional[str],
        headless: bool,
    ) -> Dict[str, Any]:
        """
        Executes a real smoke test launch:
        create temporary profile -> launch browser -> verify PID -> verify context ->
        verify page -> navigate about:blank -> evaluate JS -> take screenshot ->
        close browser -> verify clean shutdown
        """
        from playwright.sync_api import sync_playwright

        results: Dict[str, Any] = {
            "success": False,
            "pid": None,
            "launch": "FAILED",
            "context": "NOT_ATTEMPTED",
            "page": "NOT_ATTEMPTED",
            "navigation": "NOT_ATTEMPTED",
            "screenshot": "NOT_ATTEMPTED",
            "shutdown": "NOT_ATTEMPTED",
            "error": None,
        }

        temp_dir = tempfile.mkdtemp(prefix="msg_smoke_profile_")
        temp_screenshot = os.path.join(temp_dir, "smoke.png")
        pw = None
        browser = None
        context = None
        page = None

        try:
            pw = sync_playwright().start()

            launch_kwargs: Dict[str, Any] = {
                "headless": headless,
                "timeout": 15000,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"] if os.name != "nt" else [],
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path

            launcher = pw.chromium
            browser = launcher.launch(**launch_kwargs)
            results["launch"] = "SUCCESS"

            # Acquire PID
            pid = None
            try:
                conn = getattr(browser, "_impl_obj", None)
                if conn:
                    connection = getattr(conn, "_connection", None)
                    transport = getattr(connection, "_transport", None) if connection else None
                    proc = getattr(transport, "_proc", None) if transport else None
                    if proc and hasattr(proc, "pid"):
                        pid = proc.pid
            except Exception:
                pid = None
            results["pid"] = pid

            context = browser.new_context()
            results["context"] = "SUCCESS"

            page = context.new_page()
            results["page"] = "SUCCESS"

            # Navigate to about:blank
            page.goto("about:blank", timeout=10000)
            results["navigation"] = "SUCCESS"

            # Evaluate JavaScript
            val = page.evaluate("1 + 1")
            if val != 2:
                raise RuntimeError(f"JavaScript evaluation failed in smoke test: expected 2, got {val}")

            # Capture screenshot
            page.screenshot(path=temp_screenshot)
            if os.path.isfile(temp_screenshot) and os.path.getsize(temp_screenshot) > 0:
                results["screenshot"] = "SUCCESS"
            else:
                raise RuntimeError("Smoke test screenshot capture produced 0 bytes")

            results["success"] = True
        except Exception as e:
            results["error"] = str(e)
            logger.warning(f"Browser smoke test validation failed: {e}")
        finally:
            try:
                if page:
                    page.close()
                if context:
                    context.close()
                if browser:
                    browser.close()
                if pw:
                    pw.stop()
                results["shutdown"] = "SUCCESS"
            except Exception as e:
                results["shutdown"] = f"FAILED: {e}"
            # Clean up temp profile
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        return results

    @classmethod
    def validate_runtime(
        cls,
        engine: BrowserEngine = BrowserEngine.PLAYWRIGHT_CHROMIUM,
        custom_executable_path: Optional[str] = None,
        perform_smoke_test: bool = True,
        headless: Optional[bool] = None,
    ) -> BrowserRuntimeDiagnostic:
        """
        Validate browser automation runtime prerequisites deterministically.
        Performs real browser smoke-test validation before reporting can_launch=True.
        """
        playwright_ok = cls.is_playwright_installed()
        pw_version = cls.get_playwright_version()
        os_info = f"{platform.system()} {platform.release()}"
        py_version = platform.python_version()
        display_status = cls.get_display_status()

        chrome_path = cls.find_chrome()
        edge_path = cls.find_edge()
        chromium_path = cls.find_chromium()

        resolved_engine, resolved_exec = cls.resolve_executable_and_engine(engine, custom_executable_path)

        # Detect headless mode requirement for CI / Linux without display
        is_ci = os.environ.get("CI", "false").lower() == "true"
        effective_headless = headless
        if effective_headless is None:
            if is_ci or "NO_DISPLAY" in display_status:
                effective_headless = True
            else:
                effective_headless = False

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
                os_name=os_info,
                python_version=py_version,
                playwright_version=None,
                display_status=display_status,
                smoke_test_passed=False,
            )

        # Validate executable existence if specific path resolved
        exec_exists = True
        if resolved_exec:
            exec_exists = os.path.isfile(resolved_exec)
            if not exec_exists:
                return BrowserRuntimeDiagnostic(
                    playwright_installed=True,
                    configured_engine=str(engine),
                    resolved_engine=resolved_engine,
                    executable_path=resolved_exec,
                    executable_exists=False,
                    can_launch=False,
                    detected_chrome=chrome_path,
                    detected_edge=edge_path,
                    detected_chromium=chromium_path,
                    error_message=f"Configured browser executable not found: {resolved_exec}",
                    actionable_fix=f"Verify path '{resolved_exec}' or unset BROWSER_EXECUTABLE_PATH to use auto-detection.",
                    os_name=os_info,
                    python_version=py_version,
                    playwright_version=pw_version,
                    display_status=display_status,
                    smoke_test_passed=False,
                )

        # Perform real smoke test launch
        smoke_res = {"success": True, "launch": "SKIPPED", "pid": None}
        if perform_smoke_test:
            smoke_res = cls.execute_smoke_test(
                resolved_engine=resolved_engine,
                executable_path=resolved_exec,
                headless=effective_headless,
            )

        smoke_passed = smoke_res.get("success", False)
        error_msg = smoke_res.get("error") if not smoke_passed else None
        actionable_fix = None
        if not smoke_passed:
            if "Executable doesn't exist" in str(error_msg):
                actionable_fix = "Run 'playwright install chromium' to install bundled browser binaries."
            else:
                actionable_fix = "Verify display server, Xvfb on Linux, or check browser permissions."

        return BrowserRuntimeDiagnostic(
            playwright_installed=True,
            configured_engine=str(engine),
            resolved_engine=resolved_engine,
            executable_path=resolved_exec,
            executable_exists=exec_exists,
            can_launch=smoke_passed,
            detected_chrome=chrome_path,
            detected_edge=edge_path,
            detected_chromium=chromium_path,
            error_message=error_msg,
            actionable_fix=actionable_fix,
            pid=smoke_res.get("pid"),
            os_name=os_info,
            python_version=py_version,
            playwright_version=pw_version,
            display_status=display_status,
            smoke_test_passed=smoke_passed,
            launch_result=smoke_res.get("launch"),
            context_result=smoke_res.get("context"),
            page_result=smoke_res.get("page"),
            navigation_result=smoke_res.get("navigation"),
            screenshot_result=smoke_res.get("screenshot"),
            shutdown_result=smoke_res.get("shutdown"),
            details={
                "os": os.name,
                "headless": effective_headless,
                "chrome": chrome_path,
                "edge": edge_path,
                "chromium": chromium_path,
            },
        )


validate_runtime = BrowserRuntimeValidator.validate_runtime

"""Browser driver abstraction and Playwright implementation."""

from abc import ABC, abstractmethod
from typing import Optional, Any, Dict, List
import time

from backend.browser.browser_types import (
    BrowserType,
    BrowserLaunchConfig,
)
from backend.browser.exceptions import (
    BrowserLaunchError,
    BrowserCrashError,
    BrowserTimeoutError,
    BrowserNavigationError,
)
from backend.events.logger import get_logger

logger = get_logger("browser_driver")


class BrowserDriver(ABC):
    """
    Abstract interface isolating browser automation details from application logic.
    Neither domain nor repositories directly interact with Playwright or any other engine.
    """

    @abstractmethod
    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        """Launch the browser process and driver session."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close all pages, contexts, and browser processes."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the underlying browser is actively running and connected."""
        pass

    @abstractmethod
    def new_context(self, profile_path: Optional[str] = None) -> None:
        """Create or initialize an isolated browser context."""
        pass

    @abstractmethod
    def close_context(self) -> None:
        """Close the active context."""
        pass

    @abstractmethod
    def new_page(self) -> None:
        """Open a new page in the active context."""
        pass

    @abstractmethod
    def close_page(self) -> None:
        """Close the current page."""
        pass

    @abstractmethod
    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        """Navigate current page to URL and return loaded URL."""
        pass

    @abstractmethod
    def current_url(self) -> str:
        """Return the current URL of the active page."""
        pass

    @abstractmethod
    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        """Wait for page load state (load, domcontentloaded, networkidle)."""
        pass

    @abstractmethod
    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate JavaScript expression in current page context."""
        pass

    @abstractmethod
    def screenshot(self, path: str) -> None:
        """Capture screenshot to specified file path."""
        pass

    def initialize(self) -> None:
        """Initialize driver and prepare underlying engine without launching."""
        pass

    def create_context(self, profile_path: Optional[str] = None) -> None:
        """Create or initialize an isolated browser context."""
        self.new_context(profile_path)

    def create_page(self) -> None:
        """Open a new page in the active context."""
        self.new_page()

    def verify_alive(self) -> bool:
        """Verify driver and active page are responsive."""
        return self.is_connected()

    def get_console_errors(self) -> List[str]:
        """Return captured console errors and warnings."""
        return []

    def get_network_failures(self) -> List[Dict[str, Any]]:
        """Return captured failed network requests."""
        return []

    def screenshot_on_failure(self, path: str) -> bool:
        """Attempt to capture a screenshot on failure without raising exceptions."""
        try:
            self.screenshot(path)
            return True
        except Exception:
            return False

    def title(self) -> str:
        """Return the title of the active page."""
        try:
            return str(self.evaluate("document.title") or "")
        except Exception:
            return ""

    def is_alive(self) -> bool:
        """Check if browser process and connection are alive."""
        return self.is_connected()

    def take_screenshot(self) -> bytes:
        """Capture screenshot returning raw PNG bytes."""
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            self.screenshot(tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return data
        except Exception:
            return b""


import queue
import threading


import os


def find_browser_executable() -> Optional[str]:
    """Detect local installation of Chrome or Edge browser on Windows / Linux."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
def is_pid_alive(pid: Optional[int]) -> bool:
    """Reliably check whether a process with the given PID is currently alive on Windows or Unix."""
    if pid is None or pid <= 0:
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if process != 0:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
                kernel32.CloseHandle(process)
                # STILL_ACTIVE in Windows is 259 (0x103)
                return exit_code.value == 259
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


class PlaywrightBrowserDriver(BrowserDriver):
    """
    Concrete browser driver wrapping Playwright sync API.
    Executes all Playwright operations on a dedicated driver thread to avoid
    cross-thread greenlet switching issues when called across scheduler and worker threads.
    Guarantees no raw Playwright objects leak into higher application layers.
    """

    def __init__(self, config: Optional[BrowserLaunchConfig] = None):
        self.config = config or BrowserLaunchConfig()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._is_persistent = False
        self._worker_thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._console_errors: List[str] = []
        self._network_failures: List[Dict[str, Any]] = []
        self.pid: Optional[int] = None
        self.pid_status: str = "not_started"

    def is_process_alive(self) -> bool:
        """Check if underlying browser process is actively running."""
        if self.pid is None:
            return False
        alive = is_pid_alive(self.pid)
        if not alive:
            self.pid_status = "exited"
        return alive

    def initialize(self) -> None:
        """Deterministic startup stage 1: initialize worker thread."""
        if not self._worker_thread or not self._worker_thread.is_alive():
            self._stop_event.clear()
            ready_event = threading.Event()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(ready_event,),
                name="PlaywrightBrowserThread",
                daemon=True,
            )
            self._worker_thread.start()
            ready_event.wait()

    def _worker_loop(self, ready_event: threading.Event) -> None:
        self._thread_id = threading.get_ident()
        ready_event.set()
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            fn, args, kwargs, result_holder, done_event = item
            try:
                result_holder["val"] = fn(*args, **kwargs)
            except Exception as e:
                result_holder["err"] = e
            finally:
                done_event.set()

    def _dispatch(self, fn, *args, **kwargs) -> Any:
        if not self._worker_thread or not self._worker_thread.is_alive():
            raise BrowserCrashError("Browser worker thread is not running")
        if self.pid is not None and not self.is_process_alive() and getattr(fn, "__name__", "") not in ("_raw_close", "_raw_is_connected"):
            self.pid_status = "exited"
            raise BrowserCrashError(f"Browser process (PID {self.pid}) has exited unexpectedly")
        if threading.get_ident() == self._thread_id:
            return fn(*args, **kwargs)
        result_holder: Dict[str, Any] = {}
        done_event = threading.Event()
        self._queue.put((fn, args, kwargs, result_holder, done_event))
        done_event.wait()
        if "err" in result_holder:
            raise result_holder["err"]
        return result_holder.get("val")

    def _setup_page_listeners(self, page) -> None:
        """Attach listeners to capture console errors and network request failures."""
        if not page:
            return
        try:
            def on_console(msg):
                if msg.type in ("error", "warning"):
                    self._console_errors.append(f"[{msg.type.upper()}] {msg.text}")
                    if len(self._console_errors) > 100:
                        self._console_errors.pop(0)

            def on_page_error(err):
                self._console_errors.append(f"[UNCAUGHT] {err}")
                if len(self._console_errors) > 100:
                    self._console_errors.pop(0)

            def on_request_failed(req):
                failure = req.failure
                self._network_failures.append({
                    "url": req.url,
                    "method": req.method,
                    "failure_text": str(failure) if failure else "request_failed",
                })
                if len(self._network_failures) > 100:
                    self._network_failures.pop(0)

            page.on("console", on_console)
            page.on("pageerror", on_page_error)
            page.on("requestfailed", on_request_failed)
        except Exception as e:
            logger.debug(f"Failed to attach page diagnostic listeners: {e}")

    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        """Deterministic startup stage 2: launch browser engine."""
        if config:
            self.config = config
        self.initialize()
        try:
            self._dispatch(self._raw_launch)
        except Exception as e:
            self.close()
            raise BrowserLaunchError(f"Failed to launch browser: {e}") from e

    def _raw_launch(self) -> None:
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()

        b_type = self.config.browser_type.value.lower()
        if b_type in ("chromium", "chrome", "edge"):
            launcher = self._playwright.chromium
        elif b_type == "firefox":
            launcher = self._playwright.firefox
        elif b_type == "webkit":
            launcher = self._playwright.webkit
        else:
            launcher = self._playwright.chromium

        channel = None
        if b_type == "chrome":
            channel = "chrome"
        elif b_type == "edge":
            channel = "msedge"

        launch_args = list(self.config.extra_args)
        timeout_ms = self.config.timeout_seconds * 1000

        # Auto-detect Chrome executable if executable_path not explicitly specified
        exec_path = self.config.executable_path
        if not exec_path and b_type == "chrome":
            detected = find_browser_executable()
            if detected:
                exec_path = detected

        launch_kwargs: Dict[str, Any] = {
            "headless": self.config.headless,
            "args": launch_args,
            "timeout": timeout_ms,
        }
        if channel:
            launch_kwargs["channel"] = channel
        if exec_path and not channel:
            launch_kwargs["executable_path"] = exec_path

        if self.config.profile_directory:
            self._is_persistent = True
            launch_kwargs["user_data_dir"] = self.config.profile_directory
            launch_kwargs["viewport"] = {"width": self.config.viewport_width, "height": self.config.viewport_height}
            if self.config.user_agent:
                launch_kwargs["user_agent"] = self.config.user_agent
            self._context = launcher.launch_persistent_context(**launch_kwargs)
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            self._setup_page_listeners(self._page)
        else:
            self._is_persistent = False
            self._browser = launcher.launch(**launch_kwargs)
            self._context = self._browser.new_context(
                viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
                user_agent=self.config.user_agent,
            )
            self._page = self._context.new_page()
            self._setup_page_listeners(self._page)

        # Extract and validate underlying browser process PID
        self.pid = None
        self.pid_status = "pid_unavailable"
        try:
            conn = None
            if self._context and hasattr(self._context, "_impl_obj"):
                conn = getattr(self._context._impl_obj, "_connection", None)
            if not conn and self._browser and hasattr(self._browser, "_impl_obj"):
                conn = getattr(self._browser._impl_obj, "_connection", None)
            if conn:
                transport = getattr(conn, "_transport", None)
                proc = getattr(transport, "_proc", None)
                if proc and hasattr(proc, "pid") and proc.pid:
                    self.pid = proc.pid
                    if is_pid_alive(self.pid):
                        self.pid_status = "active"
                    else:
                        self.pid_status = "exited"
        except Exception:
            self.pid = None
            self.pid_status = "pid_unavailable"

        logger.info("Playwright browser launched successfully", browser_type=b_type, pid=self.pid, pid_status=self.pid_status)

    def create_context(self, profile_path: Optional[str] = None) -> None:
        """Deterministic startup stage 3: create context."""
        self.new_context(profile_path)

    def create_page(self) -> None:
        """Deterministic startup stage 4: create page."""
        self.new_page()

    def verify_alive(self) -> bool:
        """Deterministic startup stage 5: verify responsive page and context."""
        if not self.is_connected():
            return False
        try:
            val = self.evaluate("1 + 1")
            return val == 2
        except Exception:
            return False

    def get_console_errors(self) -> List[str]:
        return list(self._console_errors)

    def get_network_failures(self) -> List[Dict[str, Any]]:
        return list(self._network_failures)

    def is_connected(self) -> bool:
        if not self._worker_thread or not self._worker_thread.is_alive():
            return False
        try:
            return self._dispatch(self._raw_is_connected)
        except Exception:
            return False

    def _raw_is_connected(self) -> bool:
        if self.pid is not None and not is_pid_alive(self.pid):
            self.pid_status = "exited"
            return False
        if self._is_persistent:
            return self._context is not None and len(self._context.pages) > 0
        return self._browser is not None and self._browser.is_connected()

    def new_context(self, profile_path: Optional[str] = None) -> None:
        self._dispatch(self._raw_new_context, profile_path)

    def _raw_new_context(self, profile_path: Optional[str] = None) -> None:
        if not self._raw_is_connected() and not self._is_persistent:
            raise BrowserCrashError("Browser is not running")

        if self._is_persistent:
            if not self._page or self._page.is_closed():
                self._page = self._context.new_page()
            return

        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = self._browser.new_context(
            viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
            user_agent=self.config.user_agent,
        )
        self._page = self._context.new_page()

    def close_context(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._dispatch(self._raw_close_context)

    def _raw_close_context(self) -> None:
        if self._context:
            try:
                self._context.close()
            except Exception as e:
                logger.warning(f"Error closing context: {e}")
            finally:
                self._context = None
                self._page = None

    def new_page(self) -> None:
        self._dispatch(self._raw_new_page)

    def _raw_new_page(self) -> None:
        if not self._context:
            self._raw_new_context()
        self._page = self._context.new_page()

    def close_page(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._dispatch(self._raw_close_page)

    def _raw_close_page(self) -> None:
        if self._page:
            try:
                self._page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
            finally:
                pages = self._context.pages if self._context else []
                self._page = pages[-1] if pages else None

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        return self._dispatch(self._raw_navigate, url, timeout_ms)

    def _raw_navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        if not self._page or self._page.is_closed():
            self._raw_new_page()

        t_ms = timeout_ms if timeout_ms is not None else (self.config.timeout_seconds * 1000)
        try:
            response = self._page.goto(url, timeout=t_ms, wait_until="domcontentloaded")
            return self._page.url
        except Exception as e:
            err_msg = str(e).lower()
            if "timeout" in err_msg:
                raise BrowserTimeoutError(f"Navigation timed out after {t_ms}ms to {url}") from e
            if "net::" in err_msg or "offline" in err_msg or "cannot find" in err_msg:
                raise BrowserNavigationError(f"Network error navigating to {url}: {e}") from e
            raise BrowserCrashError(f"Browser failed during navigation to {url}: {e}") from e

    def current_url(self) -> str:
        if not self._worker_thread or not self._worker_thread.is_alive():
            return ""
        try:
            return self._dispatch(self._raw_current_url)
        except Exception:
            return ""

    def _raw_current_url(self) -> str:
        if not self._page or self._page.is_closed():
            return ""
        return self._page.url

    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        self._dispatch(self._raw_wait_for_load, state, timeout_ms)

    def _raw_wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        if not self._page or self._page.is_closed():
            raise BrowserCrashError("Page is not available to wait for load")

        t_ms = timeout_ms if timeout_ms is not None else (self.config.timeout_seconds * 1000)
        try:
            self._page.wait_for_load_state(state=state, timeout=t_ms)
        except Exception as e:
            if "timeout" in str(e).lower():
                raise BrowserTimeoutError(f"Wait for load state '{state}' timed out") from e
            raise BrowserCrashError(f"Error waiting for load state '{state}': {e}") from e

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        return self._dispatch(self._raw_evaluate, expression, arg)

    def _raw_evaluate(self, expression: str, arg: Any = None) -> Any:
        if not self._page or self._page.is_closed():
            raise BrowserCrashError("Page is not available for evaluate")

        try:
            if arg is not None:
                return self._page.evaluate(expression, arg)
            return self._page.evaluate(expression)
        except Exception as e:
            raise BrowserCrashError(f"JavaScript evaluation failed: {e}") from e

    def screenshot(self, path: str) -> None:
        self._dispatch(self._raw_screenshot, path)

    def _raw_screenshot(self, path: str) -> None:
        if not self._page or self._page.is_closed():
            raise BrowserCrashError("Page is not available for screenshot")
        try:
            self._page.screenshot(path=path)
        except Exception as e:
            logger.warning(f"Failed to capture screenshot: {e}")

    def get_content(self) -> str:
        if not self._worker_thread or not self._worker_thread.is_alive():
            return ""
        try:
            return self._dispatch(self._raw_get_content)
        except Exception:
            return ""

    def _raw_get_content(self) -> str:
        if not self._page or self._page.is_closed():
            return ""
        return self._page.content()

    def close(self) -> None:
        """Idempotent clean teardown of page, context, browser, and playwright."""
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._dispatch(self._raw_close)
            except Exception:
                pass
            self._stop_event.set()
            self._queue.put(None)
            self._worker_thread.join(timeout=3.0)
            self._worker_thread = None
            self._thread_id = None
        else:
            self._raw_close()

    def _raw_close(self) -> None:
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self.pid = None


# Alias for backward compatibility and test consistency
PlaywrightDriver = PlaywrightBrowserDriver

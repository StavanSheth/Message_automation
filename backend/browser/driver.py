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

    @abstractmethod
    def get_content(self) -> str:
        """Return full HTML content of current page."""
        pass


class PlaywrightBrowserDriver(BrowserDriver):
    """
    Concrete browser driver wrapping Playwright sync API.
    Guarantees no raw Playwright objects leak into higher application layers.
    """

    def __init__(self, config: Optional[BrowserLaunchConfig] = None):
        self.config = config or BrowserLaunchConfig()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._is_persistent = False

    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        if config:
            self.config = config

        try:
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

            if self.config.profile_directory:
                self._is_persistent = True
                self._context = launcher.launch_persistent_context(
                    user_data_dir=self.config.profile_directory,
                    headless=self.config.headless,
                    channel=channel,
                    args=launch_args,
                    timeout=timeout_ms,
                    viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
                    user_agent=self.config.user_agent,
                )
                pages = self._context.pages
                self._page = pages[0] if pages else self._context.new_page()
            else:
                self._is_persistent = False
                self._browser = launcher.launch(
                    headless=self.config.headless,
                    channel=channel,
                    args=launch_args,
                    timeout=timeout_ms,
                )
                self._context = self._browser.new_context(
                    viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
                    user_agent=self.config.user_agent,
                )
                self._page = self._context.new_page()

            logger.info("Playwright browser launched successfully", browser_type=b_type)

        except Exception as e:
            self.close()
            raise BrowserLaunchError(f"Failed to launch browser: {e}") from e

    def is_connected(self) -> bool:
        if self._is_persistent:
            return self._context is not None and len(self._context.pages) > 0
        return self._browser is not None and self._browser.is_connected()

    def new_context(self, profile_path: Optional[str] = None) -> None:
        if not self.is_connected() and not self._is_persistent:
            raise BrowserCrashError("Browser is not running")

        if self._is_persistent:
            # Persistent context is already single-session
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
        if self._context:
            try:
                self._context.close()
            except Exception as e:
                logger.warning(f"Error closing context: {e}")
            finally:
                self._context = None
                self._page = None

    def new_page(self) -> None:
        if not self._context:
            self.new_context()
        self._page = self._context.new_page()

    def close_page(self) -> None:
        if self._page:
            try:
                self._page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
            finally:
                self._page = None

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        if not self._page or self._page.is_closed():
            self.new_page()

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
        if not self._page or self._page.is_closed():
            return ""
        return self._page.url

    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
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
        if not self._page or self._page.is_closed():
            raise BrowserCrashError("Page is not available for evaluate")

        try:
            if arg is not None:
                return self._page.evaluate(expression, arg)
            return self._page.evaluate(expression)
        except Exception as e:
            raise BrowserCrashError(f"JavaScript evaluation failed: {e}") from e

    def screenshot(self, path: str) -> None:
        if not self._page or self._page.is_closed():
            raise BrowserCrashError("Page is not available for screenshot")
        try:
            self._page.screenshot(path=path)
        except Exception as e:
            logger.warning(f"Failed to capture screenshot: {e}")

    def get_content(self) -> str:
        if not self._page or self._page.is_closed():
            return ""
        return self._page.content()

    def close(self) -> None:
        """Idempotent clean teardown of page, context, browser, and playwright."""
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

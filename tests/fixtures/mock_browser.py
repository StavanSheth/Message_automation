"""Mock browser driver and session fixtures for deterministic offline testing."""

from typing import Optional, Any, List, Dict
from backend.browser.driver import BrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig, SessionStatus
from backend.browser.session import BrowserSessionInstance


class MockBrowserDriver(BrowserDriver):
    """Deterministic mock browser driver for unit tests. No real browser launched."""

    def __init__(self):
        self._launched = False
        self._connected = False
        self._current_url = ""
        self._page_content = "<html><body></body></html>"
        self._has_context = False
        self._has_page = False
        self._evaluate_results: Dict[str, Any] = {}
        self.navigate_history: List[str] = []

    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        self._launched = True
        self._connected = True
        self._has_context = True
        self._has_page = True

    def close(self) -> None:
        self._launched = False
        self._connected = False
        self._has_context = False
        self._has_page = False

    def is_connected(self) -> bool:
        return self._connected

    def new_context(self, profile_path: Optional[str] = None) -> None:
        self._has_context = True
        self._has_page = True

    def close_context(self) -> None:
        self._has_context = False
        self._has_page = False

    def new_page(self) -> None:
        self._has_page = True

    def close_page(self) -> None:
        self._has_page = False

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        self._current_url = url
        self.navigate_history.append(url)
        return url

    def current_url(self) -> str:
        return self._current_url

    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        pass

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        # Return pre-configured results or sensible defaults
        if expression in self._evaluate_results:
            return self._evaluate_results[expression]
        # Default: return empty dict for object queries, True for boolean queries
        if "return" in expression and "true" in expression.lower():
            return True
        return {}

    def screenshot(self, path: str) -> None:
        pass

    def get_content(self) -> str:
        return self._page_content

    def set_evaluate_result(self, expression: str, result: Any) -> None:
        """Pre-configure a return value for a specific evaluate expression."""
        self._evaluate_results[expression] = result

    def set_page_content(self, content: str) -> None:
        self._page_content = content


def create_mock_session(
    session_id: str = "test-session-001",
    worker_id: Optional[str] = "test-worker-001",
    auto_start: bool = True,
) -> BrowserSessionInstance:
    """Create a BrowserSessionInstance backed by MockBrowserDriver."""
    driver = MockBrowserDriver()
    session = BrowserSessionInstance(
        session_id=session_id,
        driver=driver,
        worker_id=worker_id,
    )
    if auto_start:
        session.start()
    return session

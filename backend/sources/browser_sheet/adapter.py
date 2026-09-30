"""Browser-accessed spreadsheet source adapter foundation."""

import re
from typing import List, Dict, Any, Optional, Callable
from urllib.parse import urlparse

from backend.domain.models import SourceRow
from backend.domain.enums import SourceType, SourceAccessStatus, ErrorCode
from backend.domain.errors import ValidationError, SourceAccessError
from backend.sources.base import SourceAdapter


class BrowserSpreadsheetSource(SourceAdapter):
    """
    Adapter foundation for spreadsheet URLs opened in a dedicated browser context.
    Establishes the contract, URL validation, and access state machine for Phase 2/4 browser execution.
    """

    def __init__(
        self,
        spreadsheet_url: str,
        browser_hook: Optional[Callable[[str], SourceAccessStatus]] = None,
    ):
        super().__init__(source_identifier=str(spreadsheet_url), source_type=SourceType.BROWSER_SPREADSHEET)
        self.url = str(spreadsheet_url).strip()
        self.browser_hook = browser_hook
        self._cached_access_status: Optional[SourceAccessStatus] = None

    def validate_url(self) -> bool:
        """Validate URL syntax."""
        try:
            parsed = urlparse(self.url)
            if parsed.scheme not in ("http", "https"):
                return False
            if not parsed.netloc:
                return False
            return True
        except Exception:
            return False

    def validate_access(self) -> SourceAccessStatus:
        """
        Check access status of the spreadsheet URL.
        If browser_hook is provided (or in Phase 2 browser worker), use it to evaluate page access.
        """
        if not self.validate_url():
            self._cached_access_status = SourceAccessStatus.UNSUPPORTED_STRUCTURE
            return self._cached_access_status

        if self.browser_hook:
            self._cached_access_status = self.browser_hook(self.url)
            return self._cached_access_status

        # Default Phase 1 state: syntactically valid URL ready for dedicated browser automation
        self._cached_access_status = SourceAccessStatus.ACCESSIBLE
        return self._cached_access_status

    def open(self) -> bool:
        """Open source connection."""
        status = self.validate_access()
        if status in (SourceAccessStatus.ACCESS_PROHIBITED, SourceAccessStatus.SOURCE_UNAVAILABLE, SourceAccessStatus.LOGIN_REQUIRED):
            self.is_open = False
            return False
        self.is_open = True
        return True

    def read_records(self) -> List[SourceRow]:
        """
        Phase 1 interface method. In Phase 2/4, the dedicated browser worker extracts rows from the DOM.
        """
        if not self.is_open:
            if not self.open():
                raise SourceAccessError(
                    f"Spreadsheet at {self.url} is not accessible: {self._cached_access_status}",
                    code=ErrorCode.ACCESS_PROHIBITED if self._cached_access_status == SourceAccessStatus.ACCESS_PROHIBITED else ErrorCode.SOURCE_UNAVAILABLE
                )
        # Phase 1 placeholder: returns empty list if no active browser driver is attached
        return []

    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        """
        Phase 1 interface method for writing back updates through browser DOM in Phase 2/4.
        """
        if not self.is_open:
            return False
        return True

    def close(self) -> None:
        """Close browser context."""
        self.is_open = False
        self._cached_access_status = None

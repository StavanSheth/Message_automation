"""Browser-accessed spreadsheet source adapter foundation."""

from typing import List, Dict, Any, Optional, Callable, Protocol
from urllib.parse import urlparse

from backend.domain.models import SourceRow
from backend.domain.enums import SourceType, SourceAccessStatus, ErrorCode
from backend.domain.errors import ValidationError, SourceAccessError
from backend.sources.base import SourceAdapter


class BrowserSpreadsheetDriver(Protocol):
    """Protocol for Phase 2/4 Playwright browser spreadsheet automation."""
    def check_access(self, url: str) -> SourceAccessStatus: ...
    def read_sheet(self, url: str) -> List[SourceRow]: ...
    def update_cell(self, url: str, row_index: int, updates: Dict[str, Any]) -> bool: ...


class BrowserSpreadsheetSource(SourceAdapter):
    """
    Adapter foundation for spreadsheet URLs opened in a dedicated browser context.
    Establishes the contract, URL validation, and access state machine for Phase 2/4 browser execution.
    """

    def __init__(
        self,
        spreadsheet_url: str,
        browser_hook: Optional[Callable[[str], SourceAccessStatus]] = None,
        driver: Optional[BrowserSpreadsheetDriver] = None,
    ):
        super().__init__(source_identifier=str(spreadsheet_url), source_type=SourceType.BROWSER_SPREADSHEET)
        self.url = str(spreadsheet_url).strip()
        self.browser_hook = browser_hook
        self.driver = driver
        self._cached_access_status: Optional[SourceAccessStatus] = None

    def validate_url(self) -> bool:
        """Validate URL syntax and supported spreadsheet domain."""
        from backend.sources.browser_sheet.validators import validate_spreadsheet_url, UrlValidationResult
        result, _ = validate_spreadsheet_url(self.url)
        return result == UrlValidationResult.VALID_SOURCE

    def validate_access(self) -> SourceAccessStatus:
        """
        Check access status of the spreadsheet URL.
        Distinguishes:
        - UNSUPPORTED_STRUCTURE: invalid URL format or unsupported domain
        - ACCESSIBLE: browser confirmed page access
        - LOGIN_REQUIRED: authentication wall encountered
        - ACCESS_PROHIBITED: permissions / 403 denied
        - SOURCE_UNAVAILABLE: host unreachable or browser driver not attached
        """
        from backend.sources.browser_sheet.validators import validate_spreadsheet_url, UrlValidationResult
        val_result, _ = validate_spreadsheet_url(self.url)
        if val_result != UrlValidationResult.VALID_SOURCE:
            self._cached_access_status = SourceAccessStatus.UNSUPPORTED_STRUCTURE
            return self._cached_access_status

        if self.driver:
            self._cached_access_status = self.driver.check_access(self.url)
            return self._cached_access_status

        if self.browser_hook:
            self._cached_access_status = self.browser_hook(self.url)
            return self._cached_access_status

        # Without an attached browser driver or access hook, live accessibility cannot be assumed
        self._cached_access_status = SourceAccessStatus.SOURCE_UNAVAILABLE
        return self._cached_access_status

    def open(self) -> bool:
        """Open source connection."""
        status = self.validate_access()
        if status != SourceAccessStatus.ACCESSIBLE:
            self.is_open = False
            return False
        self.is_open = True
        return True

    def read_records(self) -> List[SourceRow]:
        """
        Read spreadsheet rows via browser driver.
        Raises SourceAccessError if no driver is attached or source is not accessible.
        Does NOT silently return empty list when unimplemented.
        """
        if not self.driver:
            raise SourceAccessError(
                "Browser spreadsheet driver is not attached. Browser-based extraction is deferred to Phase 2.",
                code=ErrorCode.SOURCE_UNAVAILABLE,
            )

        if not self.is_open:
            if not self.open():
                raise SourceAccessError(
                    f"Spreadsheet at {self.url} is not accessible: {self._cached_access_status}",
                    code=ErrorCode.ACCESS_PROHIBITED if self._cached_access_status == SourceAccessStatus.ACCESS_PROHIBITED else ErrorCode.SOURCE_UNAVAILABLE,
                )

        return self.driver.read_sheet(self.url)

    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        """
        Write back updates through browser driver.
        Raises SourceAccessError if no driver is attached.
        Does NOT falsely return True without performing an update.
        """
        if not self.driver:
            raise SourceAccessError(
                "Browser spreadsheet driver is not attached. Browser-based write-back is deferred to Phase 2.",
                code=ErrorCode.SOURCE_UNAVAILABLE,
            )

        if not self.is_open:
            return False

        return self.driver.update_cell(self.url, row_index, updates)

    def close(self) -> None:
        """Close browser context."""
        self.is_open = False
        self._cached_access_status = None

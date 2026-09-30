"""Browser-accessed spreadsheet source subsystem."""

from backend.sources.browser_sheet.validators import (
    UrlValidationResult,
    validate_spreadsheet_url,
    map_validation_to_access_status,
)
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource

__all__ = [
    "UrlValidationResult",
    "validate_spreadsheet_url",
    "map_validation_to_access_status",
    "SpreadsheetStructureValidator",
    "BrowserSpreadsheetSource",
]

"""Unit tests for browser spreadsheet URL validation, access classification, driver reading, and write-back."""

import pytest
from unittest.mock import MagicMock

from backend.domain.enums import SourceAccessStatus, RepliedStatus
from backend.domain.errors import ValidationError, ConflictError, SourceAccessError
from backend.sources.browser_sheet.validators import (
    validate_spreadsheet_url,
    UrlValidationResult,
    map_validation_to_access_status,
)
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.sources.browser_sheet.driver import PlaywrightSpreadsheetDriver
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource
from tests.fixtures.mock_browser import create_mock_session, MockBrowserDriver


def test_url_validation_google_sheets():
    valid, err = validate_spreadsheet_url("https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit")
    assert valid == UrlValidationResult.VALID_SOURCE
    assert err is None


def test_url_validation_excel_online():
    valid, err = validate_spreadsheet_url("https://onedrive.live.com/edit.aspx?resid=ABC!123")
    assert valid == UrlValidationResult.VALID_SOURCE
    assert err is None


def test_url_validation_arbitrary_domain_allowed():
    valid, err = validate_spreadsheet_url("https://my-custom-portal.org/sheet")
    assert valid == UrlValidationResult.VALID_SOURCE
    assert err is None


def test_url_validation_unsupported_domain_with_whitelist():
    valid, err = validate_spreadsheet_url("https://malicious-site.com/not_a_sheet", enforce_whitelist=True)
    assert valid == UrlValidationResult.UNSUPPORTED_SOURCE
    assert err is not None


def test_url_validation_malformed():
    valid, err = validate_spreadsheet_url("not_a_url")
    assert valid == UrlValidationResult.INVALID_URL


def test_spreadsheet_structure_validator_valid_headers():
    headers = ["Name", "Instagram URL", "Message", "Replied", "Expected Followers"]
    canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(headers)
    assert canonical_map["name"] == 0
    assert canonical_map["instagram_url"] == 1
    assert canonical_map["message"] == 2


def test_spreadsheet_structure_validator_missing_required():
    headers = ["First Name", "Age", "City"]
    with pytest.raises(ValidationError):
        SpreadsheetStructureValidator.validate_headers(headers)


def test_spreadsheet_structure_validator_parse_row():
    headers = ["Name", "Instagram URL", "Message", "Replied"]
    _, col_map = SpreadsheetStructureValidator.validate_headers(headers)
    row = SpreadsheetStructureValidator.parse_row(
        row_cells=["Alice", "https://instagram.com/alice", "Hello!", "NO"],
        row_index=2,
        idx_to_canonical=col_map,
    )
    assert row is not None
    assert row.name == "Alice"
    assert row.instagram_url == "https://instagram.com/alice"
    assert row.replied_status == RepliedStatus.NO
    assert row.row_index == 2


def test_driver_cache_invalidation_on_url_change():
    session = create_mock_session("SESS-DRV1")
    session.start()
    driver = PlaywrightSpreadsheetDriver(session)

    # First URL
    driver._current_url = "https://docs.google.com/spreadsheets/d/111/edit"
    driver._cached_headers = ["Name", "Instagram URL", "Message"]

    # Open different URL must invalidate cache
    driver._invalidate_cache()
    assert len(driver._cached_headers) == 0
    assert len(driver._canonical_to_col) == 0


def test_write_back_raises_conflict_on_mismatch():
    session = create_mock_session("SESS-DRV2")
    session.start()
    driver = PlaywrightSpreadsheetDriver(session)
    driver._cached_headers = ["Name", "Instagram URL", "Message", "Replied"]
    driver._canonical_to_col = {"replied": 3}
    driver._current_url = "https://docs.google.com/spreadsheets/d/test/edit"

    # Mock evaluate: click succeeds, type succeeds, read_cell returns old value
    def mock_eval(expr, args=None):
        if "click_result" in expr or "MouseEvent" in expr:
            return {"success": True}
        if "newVal" in expr or "KeyboardEvent" in expr:
            return True
        if "document.querySelectorAll('table tr')" in expr:
            return "NO"  # Returned old value instead of target 'YES'
        return True

    session.driver.evaluate = mock_eval

    with pytest.raises(ConflictError):
        driver.update_cell("https://docs.google.com/spreadsheets/d/test/edit", 2, {"replied": "YES"})

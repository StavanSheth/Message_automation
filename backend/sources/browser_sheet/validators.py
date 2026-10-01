"""URL and source validation for browser-accessed spreadsheets."""

from urllib.parse import urlparse
from enum import Enum
from typing import Tuple, Optional
import re

from backend.domain.enums import SourceAccessStatus


class UrlValidationResult(str, Enum):
    VALID_SOURCE = "VALID_SOURCE"
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"


SUPPORTED_DOMAINS = (
    "docs.google.com",
    "sheets.google.com",
    "drive.google.com",
    "onedrive.live.com",
    "sharepoint.com",
    "office.live.com",
    "office.com",
    "excel.office.com",
    "localhost",
    "127.0.0.1",
    "example.com",
    "testserver",
)


def validate_spreadsheet_url(url: str, enforce_whitelist: bool = False) -> Tuple[UrlValidationResult, Optional[str]]:
    """
    Validate spreadsheet URL syntax, protocol, and optionally domain.
    Returns (UrlValidationResult, error_message).

    By default (enforce_whitelist=False), ANY syntactically valid URL is accepted
    so Chrome can inspect access, login walls, and DOM table structure directly.
    """
    if not url or not isinstance(url, str):
        return UrlValidationResult.INVALID_URL, "URL cannot be empty"

    stripped = url.strip()
    try:
        parsed = urlparse(stripped)
    except Exception as e:
        return UrlValidationResult.INVALID_URL, f"Malformed URL syntax: {e}"

    if not parsed.scheme:
        return UrlValidationResult.INVALID_URL, "URL missing scheme"

    if parsed.scheme.lower() == "file":
        return UrlValidationResult.VALID_SOURCE, None

    if not parsed.netloc:
        return UrlValidationResult.INVALID_URL, "URL missing network host"

    # Protocol check: allow https, or http for local/test hosts
    is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1", "testserver")
    if parsed.scheme.lower() not in ("https", "http"):
        return UrlValidationResult.INVALID_URL, f"Unsupported URL protocol: {parsed.scheme}"

    if parsed.scheme.lower() != "https" and not is_local:
        return UrlValidationResult.INVALID_URL, "Spreadsheet URL must use secure HTTPS protocol"

    host = (parsed.hostname or "").lower()

    # For Google Docs explicitly, reject non-spreadsheet paths
    if ("docs.google.com" in host or "sheets.google.com" in host) and parsed.path:
        if not re.search(r"/spreadsheets/d/|/forms/|/spreadsheets", parsed.path):
            return UrlValidationResult.UNSUPPORTED_SOURCE, "Google Docs URL is not a spreadsheet"

    if enforce_whitelist:
        matched = any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
        if not matched:
            return UrlValidationResult.UNSUPPORTED_SOURCE, f"Unsupported spreadsheet domain: '{host}'"

    return UrlValidationResult.VALID_SOURCE, None


def map_validation_to_access_status(result: UrlValidationResult) -> SourceAccessStatus:
    """Map URL validation result to domain SourceAccessStatus."""
    if result == UrlValidationResult.VALID_SOURCE:
        return SourceAccessStatus.ACCESSIBLE
    if result == UrlValidationResult.INVALID_URL:
        return SourceAccessStatus.UNSUPPORTED_STRUCTURE
    return SourceAccessStatus.UNSUPPORTED_STRUCTURE

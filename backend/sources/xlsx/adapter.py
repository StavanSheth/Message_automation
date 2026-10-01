"""Local XLSX Spreadsheet source adapter implementation."""

import hashlib
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

import openpyxl
from openpyxl.workbook import Workbook

from backend.domain.models import SourceRow
from backend.domain.enums import SourceType, SourceAccessStatus, RepliedStatus, ErrorCode
from backend.domain.errors import ValidationError, SourceAccessError, ConflictError
from backend.sources.base import SourceAdapter


HEADER_ALIASES: Dict[str, str] = {
    "contact id": "contact_id",
    "contact_id": "contact_id",
    "id": "contact_id",
    "name": "name",
    "full name": "name",
    "target name": "name",
    "instagram url": "instagram_url",
    "instagram_url": "instagram_url",
    "ig url": "instagram_url",
    "profile url": "instagram_url",
    "instagram link": "instagram_url",
    "url": "instagram_url",
    "username": "username",
    "ig username": "username",
    "handle": "username",
    "expected followers": "expected_followers",
    "expected_followers": "expected_followers",
    "followers": "expected_followers",
    "message": "message",
    "initial message": "message",
    "msg": "message",
    "follow-up 1 message": "followup_1_message",
    "followup 1 message": "followup_1_message",
    "follow up 1 message": "followup_1_message",
    "follow-up 1": "followup_1_message",
    "followup 1": "followup_1_message",
    "follow-up 1 delay": "followup_1_delay_seconds",
    "followup 1 delay": "followup_1_delay_seconds",
    "follow up 1 delay": "followup_1_delay_seconds",
    "follow-up 2 message": "followup_2_message",
    "followup 2 message": "followup_2_message",
    "follow up 2 message": "followup_2_message",
    "follow-up 2": "followup_2_message",
    "followup 2": "followup_2_message",
    "follow-up 2 delay": "followup_2_delay_seconds",
    "followup 2 delay": "followup_2_delay_seconds",
    "follow up 2 delay": "followup_2_delay_seconds",
    "replied": "replied_status",
    "replied status": "replied_status",
    "has replied": "replied_status",
    "notes": "notes",
    "note": "notes",
    "comments": "notes",
}


def normalize_header(header: str) -> Optional[str]:
    """Normalize header text using alias lookup."""
    if not header:
        return None
    cleaned = re.sub(r"\s+", " ", str(header).strip().lower())
    return HEADER_ALIASES.get(cleaned, cleaned)


def calculate_row_checksum(row_data: Dict[str, Any]) -> str:
    """Compute deterministic SHA256 checksum of canonical row values."""
    sorted_items = sorted((k, str(v)) for k, v in row_data.items() if v is not None)
    canonical_repr = json.dumps(sorted_items)
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


class LocalXlsxSource(SourceAdapter):
    """Adapter for importing and updating local .xlsx spreadsheet workbooks."""

    def __init__(self, file_path: str):
        super().__init__(source_identifier=str(file_path), source_type=SourceType.LOCAL_XLSX)
        self.file_path = Path(file_path)
        self._wb: Optional[Workbook] = None
        self._header_col_map: Dict[str, int] = {}  # mapped_field -> 1-based column index

    def open(self) -> bool:
        """Open the workbook."""
        status = self.validate_access()
        if status != SourceAccessStatus.ACCESSIBLE:
            self.is_open = False
            return False
        try:
            self._wb = openpyxl.load_workbook(self.file_path, data_only=True)
            self.is_open = True
            return True
        except Exception as e:
            self.is_open = False
            raise SourceAccessError(f"Failed to open Excel file '{self.file_path}': {e}", code=ErrorCode.SOURCE_UNAVAILABLE)

    def validate_access(self) -> SourceAccessStatus:
        """Validate that the file exists, has correct extension, and is readable."""
        if not self.file_path.exists():
            return SourceAccessStatus.SOURCE_UNAVAILABLE
        if self.file_path.suffix.lower() != ".xlsx":
            return SourceAccessStatus.UNSUPPORTED_STRUCTURE
        try:
            with open(self.file_path, "rb") as f:
                header_bytes = f.read(4)
                # XLSX files are ZIP archives starting with PK\x03\x04
                if header_bytes != b"PK\x03\x04":
                    return SourceAccessStatus.UNSUPPORTED_STRUCTURE
            return SourceAccessStatus.ACCESSIBLE
        except PermissionError:
            return SourceAccessStatus.ACCESS_PROHIBITED
        except Exception:
            return SourceAccessStatus.SOURCE_UNAVAILABLE

    def read_records(self) -> List[SourceRow]:
        """Read and validate all data rows from the active sheet."""
        if not self.is_open or self._wb is None:
            if not self.open():
                raise SourceAccessError(f"Cannot read from closed or unavailable source: {self.file_path}")

        sheet = self._wb.active
        if sheet is None:
            raise ValidationError("Excel workbook contains no active sheet.")

        # 1. Parse Header Row
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=False), None)
        if not header_row:
            raise ValidationError("Excel workbook header row is empty.")

        self._header_col_map.clear()
        col_to_field: Dict[int, str] = {}

        for col_idx, cell in enumerate(header_row, start=1):
            val = cell.value
            if val is not None:
                norm_key = normalize_header(str(val))
                if norm_key:
                    self._header_col_map[norm_key] = col_idx
                    col_to_field[col_idx] = norm_key

        # Validate mandatory headers: name, instagram_url, message
        required_fields = ["name", "instagram_url", "message"]
        missing = [rf for rf in required_fields if rf not in self._header_col_map]
        if missing:
            raise ValidationError(
                f"Missing required columns in Excel sheet: {', '.join(missing)}. "
                f"Found columns: {list(self._header_col_map.keys())}"
            )

        # 2. Parse Data Rows
        rows: List[SourceRow] = []
        for row_idx, row_cells in enumerate(sheet.iter_rows(min_row=2, values_only=False), start=2):
            raw_values: Dict[str, Any] = {}
            has_any_data = False

            for col_idx, cell in enumerate(row_cells, start=1):
                field_name = col_to_field.get(col_idx)
                if field_name:
                    cell_val = cell.value
                    if cell_val is not None:
                        has_any_data = True
                    raw_values[field_name] = cell_val

            if not has_any_data:
                continue  # Skip entirely empty rows

            # Validate row required fields
            name_val = str(raw_values.get("name") or "").strip()
            url_val = str(raw_values.get("instagram_url") or "").strip()
            msg_val = str(raw_values.get("message") or "").strip()

            if not name_val:
                raise ValidationError(f"Row {row_idx}: 'Name' is required and cannot be empty.")
            if not url_val:
                raise ValidationError(f"Row {row_idx}: 'Instagram URL' is required and cannot be empty.")
            if not msg_val:
                raise ValidationError(f"Row {row_idx}: 'Message' is required and cannot be empty.")

            # Parse optional followers
            followers: Optional[int] = None
            if raw_values.get("expected_followers") is not None:
                try:
                    followers = int(raw_values["expected_followers"])
                except (ValueError, TypeError):
                    followers = None

            # Parse replied status
            replied_raw = str(raw_values.get("replied_status") or "").strip().upper()
            if replied_raw in ("YES", "TRUE", "Y", "1"):
                replied_status = RepliedStatus.YES
            elif replied_raw in ("NO", "FALSE", "N", "0"):
                replied_status = RepliedStatus.NO
            else:
                replied_status = RepliedStatus.UNKNOWN

            # Parse follow-up delays (defaulting to None if absent)
            def parse_delay(val: Any) -> Optional[int]:
                if val is None or str(val).strip() == "":
                    return None
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return None

            fu1_delay = parse_delay(raw_values.get("followup_1_delay_seconds"))
            fu2_delay = parse_delay(raw_values.get("followup_2_delay_seconds"))

            # Contact ID
            cid_val = str(raw_values.get("contact_id") or "").strip() or None
            username_val = str(raw_values.get("username") or "").strip() or None
            notes_val = str(raw_values.get("notes") or "").strip() or None
            fu1_msg = str(raw_values.get("followup_1_message") or "").strip() or None
            fu2_msg = str(raw_values.get("followup_2_message") or "").strip() or None

            # Checksum
            checksum = calculate_row_checksum(raw_values)

            source_row = SourceRow(
                row_index=row_idx,
                name=name_val,
                instagram_url=url_val,
                message=msg_val,
                replied_status=replied_status,
                contact_id=cid_val,
                username=username_val,
                expected_followers=followers,
                followup_1_message=fu1_msg,
                followup_1_delay_seconds=fu1_delay,
                followup_2_message=fu2_msg,
                followup_2_delay_seconds=fu2_delay,
                notes=notes_val,
                raw_values=raw_values,
                checksum=checksum,
            )
            rows.append(source_row)

        return rows

    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        """
        Update specific cells in the spreadsheet workbook with transactional read-back verification:
        1. Read current value
        2. Write value
        3. Save workbook to disk
        4. Re-open and read back cell value
        5. Compare and raise ConflictError on mismatch
        """
        # For updates, open with data_only=False to preserve formulas
        wb = openpyxl.load_workbook(self.file_path)
        sheet = wb.active
        if sheet is None:
            return False

        # Read headers
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=False), None)
        if not header_row:
            return False

        field_to_col: Dict[str, int] = {}
        for col_idx, cell in enumerate(header_row, start=1):
            if cell.value is not None:
                norm_key = normalize_header(str(cell.value))
                if norm_key:
                    field_to_col[norm_key] = col_idx

        # If an updated field has no column, append a new header column
        max_col = sheet.max_column
        written_fields: Dict[str, int] = {}
        for field_name, value in updates.items():
            col_idx = field_to_col.get(field_name)
            if col_idx is None:
                max_col += 1
                sheet.cell(row=1, column=max_col, value=field_name.replace("_", " ").title())
                col_idx = max_col
                field_to_col[field_name] = col_idx

            sheet.cell(row=row_index, column=col_idx, value=value)
            written_fields[field_name] = col_idx

        wb.save(self.file_path)
        wb.close()

        # Step 4 & 5: Read back and compare for persistence verification
        verify_wb = openpyxl.load_workbook(self.file_path, data_only=True)
        verify_sheet = verify_wb.active
        try:
            for field_name, expected_val in updates.items():
                c_idx = written_fields[field_name]
                read_val = verify_sheet.cell(row=row_index, column=c_idx).value
                # Normalize string representations for comparison
                str_read = str(read_val or "").strip()
                str_expected = str(expected_val if expected_val is not None else "").strip()
                if str_read != str_expected:
                    raise ConflictError(
                        f"XLSX write-back verification failed for row {row_index} col '{field_name}'. "
                        f"Expected '{str_expected}', read back '{str_read}'."
                    )
        finally:
            verify_wb.close()

        return True

    def close(self) -> None:
        """Close workbook."""
        if self._wb:
            try:
                self._wb.close()
            except Exception:
                pass
            self._wb = None
        self.is_open = False

"""Browser spreadsheet structure parsing and canonical row validation."""

from typing import List, Dict, Any, Optional, Tuple
import re

from backend.domain.models import SourceRow
from backend.domain.enums import RepliedStatus
from backend.domain.errors import ValidationError
from backend.sources.xlsx.adapter import (
    HEADER_ALIASES,
    normalize_header,
    calculate_row_checksum,
)


class SpreadsheetStructureValidator:
    """Validates spreadsheet table headers and transforms rows into canonical SourceRow entities."""

    REQUIRED_FIELDS = {"instagram_url"}

    @classmethod
    def validate_headers(cls, raw_headers: List[str]) -> Tuple[Dict[str, int], Dict[int, str]]:
        """
        Validate header row.
        Returns:
            canonical_to_index: Dict[canonical_field_name, 0-based column index]
            index_to_canonical: Dict[0-based column index, canonical_field_name]
        Raises:
            ValidationError on empty headers, duplicate columns, or missing required fields.
        """
        if not raw_headers:
            raise ValidationError("Spreadsheet header row is completely empty")

        canonical_to_idx: Dict[str, int] = {}
        idx_to_canonical: Dict[int, str] = {}
        seen_canonical: set = set()

        for idx, raw in enumerate(raw_headers):
            if raw is None or str(raw).strip() == "":
                continue
            normalized = normalize_header(str(raw))
            if not normalized:
                continue

            if normalized in seen_canonical:
                col_key = f"{normalized}_{idx}"
            else:
                col_key = normalized
                seen_canonical.add(normalized)
                canonical_to_idx[normalized] = idx

            idx_to_canonical[idx] = col_key

        # Verify required columns exist (either instagram_url or username)
        if "instagram_url" not in canonical_to_idx and "username" not in canonical_to_idx:
            missing = cls.REQUIRED_FIELDS - set(canonical_to_idx.keys())
            raise ValidationError(
                f"Spreadsheet structure missing mandatory required column(s): {sorted(list(missing))}"
            )

        return canonical_to_idx, idx_to_canonical

    @classmethod
    def parse_row(
        cls,
        row_cells: List[Any],
        row_index: int,
        idx_to_canonical: Dict[int, str],
    ) -> Optional[SourceRow]:
        """
        Parse raw cell row into canonical SourceRow.
        Returns None if row is completely empty.
        """
        raw_values: Dict[str, Any] = {}
        row_data: Dict[str, Any] = {}

        is_empty = True
        for col_idx, cell_value in enumerate(row_cells):
            val = str(cell_value).strip() if cell_value is not None else ""
            if val != "":
                is_empty = False
            raw_values[f"col_{col_idx}"] = val

            field_name = idx_to_canonical.get(col_idx)
            if field_name:
                row_data[field_name] = val
                raw_values[field_name] = val

        if is_empty or not any(row_data.values()):
            return None

        # Clean Instagram URL
        ig_url = row_data.get("instagram_url", "").strip()
        if not ig_url and row_data.get("username"):
            un = str(row_data["username"]).strip().lstrip("@")
            if un:
                ig_url = f"https://www.instagram.com/{un}/"

        if not ig_url:
            return None

        # Clean name
        name_val = row_data.get("name", "").strip()
        if not name_val:
            if row_data.get("username"):
                name_val = str(row_data["username"]).strip().lstrip("@")
            else:
                parts = [p for p in ig_url.rstrip("/").split("/") if p and "instagram.com" not in p]
                name_val = parts[-1] if parts else "Target"

        # Clean message (if empty, default)
        msg = row_data.get("message", "").strip()
        if not msg:
            msg = f"Hello {name_val}, hope you are doing well!"

        # Resolve Replied status
        replied_raw = str(row_data.get("replied_status", "")).strip().upper()
        if replied_raw in ("YES", "TRUE", "1", "Y", "REPLIED"):
            replied_status = RepliedStatus.YES
        elif replied_raw in ("NO", "FALSE", "0", "N"):
            replied_status = RepliedStatus.NO
        else:
            replied_status = RepliedStatus.UNKNOWN

        # Extract numeric follower count if present
        followers_val = None
        if "expected_followers" in row_data and row_data["expected_followers"]:
            digits = re.sub(r"[^\d]", "", str(row_data["expected_followers"]))
            if digits:
                followers_val = int(digits)

        # Parse follow up delays
        fu1_delay = None
        if "followup_1_delay_seconds" in row_data and row_data["followup_1_delay_seconds"]:
            digits = re.sub(r"[^\d]", "", str(row_data["followup_1_delay_seconds"]))
            if digits:
                fu1_delay = int(digits)

        fu2_delay = None
        if "followup_2_delay_seconds" in row_data and row_data["followup_2_delay_seconds"]:
            digits = re.sub(r"[^\d]", "", str(row_data["followup_2_delay_seconds"]))
            if digits:
                fu2_delay = int(digits)

        fu1_msg = row_data.get("followup_1_message") or None
        if not fu1_msg and row_data.get("followup_1"):
            fu1_raw = str(row_data["followup_1"]).strip()
            if fu1_raw.lower() not in ("done", "not applicable", "pending", "n/a", "na", "-", "0"):
                fu1_msg = fu1_raw

        # Build rich notes from extra metadata fields
        notes_parts = []
        if row_data.get("notes"):
            notes_parts.append(str(row_data["notes"]).strip())
        if row_data.get("industry"):
            notes_parts.append(f"Industry: {row_data['industry']}")
        if row_data.get("google_maps_link"):
            notes_parts.append(f"Maps: {row_data['google_maps_link']}")
        if row_data.get("website_link"):
            notes_parts.append(f"Website: {row_data['website_link']}")
        if row_data.get("remarks"):
            notes_parts.append(f"Remarks: {row_data['remarks']}")
        if row_data.get("followup_1"):
            notes_parts.append(f"Follow Up 1: {row_data['followup_1']}")

        final_notes = " | ".join(notes_parts) if notes_parts else None
        checksum = calculate_row_checksum(row_data)

        return SourceRow(
            row_index=row_index,
            name=name_val,
            instagram_url=ig_url,
            message=msg,
            replied_status=replied_status,
            contact_id=row_data.get("contact_id") or None,
            username=row_data.get("username") or None,
            expected_followers=followers_val,
            followup_1_message=fu1_msg,
            followup_1_delay_seconds=fu1_delay,
            followup_2_message=row_data.get("followup_2_message") or None,
            followup_2_delay_seconds=fu2_delay,
            notes=final_notes,
            raw_values=raw_values,
            checksum=checksum,
        )

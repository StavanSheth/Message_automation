"""Security utilities for path validation, PII masking, and sanitization."""

import os
from pathlib import Path
from typing import Optional


def sanitize_filename(filename: str) -> str:
    """Strip directory traversal components from user-supplied filename."""
    base = os.path.basename(filename)
    # Remove any dangerous characters
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ")
    return cleaned.strip()


def validate_sandboxed_path(base_dir: str, target_path: str) -> Path:
    """
    Ensure target_path resolves strictly within base_dir.
    Prevents path traversal vulnerabilities.
    """
    resolved_base = Path(base_dir).resolve()
    resolved_target = Path(target_path).resolve()

    base_str = str(resolved_base)
    if not base_str.endswith(os.sep):
        base_str += os.sep

    target_str = str(resolved_target)
    if not (target_str.startswith(base_str) or target_str == str(resolved_base)):
        raise ValueError(f"Path traversal detected: {target_path} is outside {base_dir}")

    return resolved_target


def mask_pii(val: Optional[str], visible_chars: int = 4) -> str:
    """Mask personally identifiable text for logs/previews."""
    if not val:
        return ""
    if len(val) <= visible_chars:
        return "***"
    return f"{val[:2]}***{val[-2:]}"

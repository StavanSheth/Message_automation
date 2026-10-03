"""Diagnostics package for capturing, storing, and reviewing failure artifacts."""

from backend.diagnostics.collector import DiagnosticCollector, sanitize_text

__all__ = ["DiagnosticCollector", "sanitize_text"]

"""Source adapter interface and base abstractions."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

from backend.domain.models import SourceRow, SyncRun
from backend.domain.enums import SourceAccessStatus, SourceType


class SourceAdapter(ABC):
    """Abstract base class for all data source adapters (Local XLSX, Browser Spreadsheet)."""

    def __init__(self, source_identifier: str, source_type: SourceType):
        self.source_identifier = source_identifier
        self.source_type = source_type
        self.is_open: bool = False

    @abstractmethod
    def open(self) -> bool:
        """Open and prepare access to the data source."""
        pass

    @abstractmethod
    def validate_access(self) -> SourceAccessStatus:
        """Verify whether the data source is accessible."""
        pass

    @abstractmethod
    def read_records(self) -> List[SourceRow]:
        """Read and normalize all data rows from the source."""
        pass

    @abstractmethod
    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        """Write back updates to the specified row in the source."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Release any open handles, files, or browser pages."""
        pass

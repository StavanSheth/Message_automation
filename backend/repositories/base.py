"""Base repository class with shared database operations."""

from typing import Optional
from backend.database.manager import DatabaseManager


class BaseRepository:
    """Base class providing database connectivity to repositories."""

    def __init__(self, db: DatabaseManager):
        self.db = db

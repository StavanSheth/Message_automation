"""Append-oriented Event repository."""

import sqlite3
import json
from typing import Optional, List, Dict, Any
from backend.repositories.base import BaseRepository
from backend.domain.models import Event, utc_now_iso
from backend.domain.enums import EventLevel, EventCode
from backend.events.bus import get_event_bus
from backend.events.correlation import generate_id


class EventRepository(BaseRepository):
    """Data access repository for immutable, append-oriented system events."""

    def record(
        self,
        event_code: EventCode,
        category: str,
        level: EventLevel = EventLevel.INFO,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Event:
        """Append an event to the persistent event log and notify subscribers."""
        if event_id is None:
            event_id = generate_id("EVT")
        if timestamp is None:
            timestamp = utc_now_iso()

        if payload is not None:
            task_id = task_id or payload.get("task_id")
            worker_id = worker_id or payload.get("worker_id")
            session_id = session_id or payload.get("session_id")
            correlation_id = correlation_id or payload.get("correlation_id")

        if task_id is None and entity_type == "task":
            task_id = entity_id

        payload_json = json.dumps(payload) if payload is not None else None
        level_val = level.value if isinstance(level, EventLevel) else level
        code_val = event_code.value if isinstance(event_code, EventCode) else event_code

        query = """
            INSERT INTO events (
                id, timestamp, level, category, entity_type, entity_id,
                event_code, payload_json, task_id, worker_id, session_id, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            event_id,
            timestamp,
            level_val,
            category,
            entity_type,
            entity_id,
            code_val,
            payload_json,
            task_id,
            worker_id,
            session_id,
            correlation_id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)

        event = Event(
            id=event_id,
            timestamp=timestamp,
            level=EventLevel(level_val),
            category=category,
            entity_type=entity_type,
            entity_id=entity_id,
            event_code=EventCode(code_val),
            payload_json=payload_json,
            task_id=task_id,
            worker_id=worker_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )

        # Notify event bus
        get_event_bus().publish(event)
        return event

    def list_events(
        self,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        category: Optional[str] = None,
        level: Optional[EventLevel] = None,
        task_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Event]:
        """Query events with filtering."""
        conn = self.db.get_connection()
        clauses = []
        params = []

        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if level:
            clauses.append("level = ?")
            params.append(level.value if isinstance(level, EventLevel) else level)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if worker_id:
            clauses.append("worker_id = ?")
            params.append(worker_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM events {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?;"
        params.extend([limit, offset])

        cursor = conn.execute(query, tuple(params))
        return [self._row_to_event(row) for row in cursor.fetchall()]

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        keys = row.keys()
        return Event(
            id=row["id"],
            timestamp=row["timestamp"],
            level=EventLevel(row["level"]),
            category=row["category"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            event_code=EventCode(row["event_code"]),
            payload_json=row["payload_json"],
            task_id=row["task_id"] if "task_id" in keys else None,
            worker_id=row["worker_id"] if "worker_id" in keys else None,
            session_id=row["session_id"] if "session_id" in keys else None,
            correlation_id=row["correlation_id"] if "correlation_id" in keys else None,
        )


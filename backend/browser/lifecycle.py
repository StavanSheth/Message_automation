"""Deterministic browser lifecycle management and process cleanup."""

import atexit
from typing import Dict, List, Optional
from backend.browser.session import BrowserSessionInstance
from backend.browser.browser_types import SessionStatus
from backend.events.logger import get_logger

logger = get_logger("browser_lifecycle")


class BrowserLifecycleManager:
    """
    Guarantees deterministic lifecycle transitions and orphaned process cleanup.
    Registers atexit handlers to ensure browsers close on application shutdown or crash.
    """

    def __init__(self):
        self._active_sessions: Dict[str, BrowserSessionInstance] = {}
        atexit.register(self.cleanup_all)

    def register_session(self, session: BrowserSessionInstance) -> None:
        """Register a session for lifecycle management."""
        self._active_sessions[session.session_id] = session

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session."""
        self._active_sessions.pop(session_id, None)

    def stop_session(self, session_id: str) -> None:
        """Stop and safely clean up a specific session."""
        session = self._active_sessions.get(session_id)
        if session:
            try:
                session.stop()
            except Exception as e:
                logger.warning(f"Error stopping session {session_id}: {e}")
            finally:
                self.unregister_session(session_id)

    def recover_session(self, session_id: str) -> BrowserSessionInstance:
        """Attempt to restart a crashed session."""
        session = self._active_sessions.get(session_id)
        if not session:
            raise KeyError(f"Session {session_id} not registered")

        logger.info("Initiating recovery for crashed session", session_id=session_id)
        session.restart()
        return session

    def cleanup_all(self) -> None:
        """Clean up all active sessions on shutdown."""
        if not self._active_sessions:
            return

        logger.info(f"Cleaning up {len(self._active_sessions)} active browser sessions on shutdown")
        for session_id, session in list(self._active_sessions.items()):
            try:
                session.stop()
            except Exception as e:
                logger.warning(f"Error stopping session {session_id} during cleanup: {e}")
        self._active_sessions.clear()

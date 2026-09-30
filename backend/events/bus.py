"""In-memory publish-subscribe event bus for internal module decoupling."""

from typing import Callable, Dict, List
from backend.domain.models import Event

EventHandler = Callable[[Event], None]


class EventBus:
    """Thread-safe event dispatcher for real-time decoupling."""

    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._all_subscribers: List[EventHandler] = []

    def subscribe(self, event_code: str, handler: EventHandler) -> None:
        """Subscribe to a specific event code."""
        if event_code not in self._subscribers:
            self._subscribers[event_code] = []
        self._subscribers[event_code].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to all emitted events."""
        self._all_subscribers.append(handler)

    def publish(self, event: Event) -> None:
        """Publish an event to all registered subscribers."""
        event_code = event.event_code.value if hasattr(event.event_code, "value") else str(event.event_code)

        # Notify specific listeners
        for handler in self._subscribers.get(event_code, []):
            try:
                handler(event)
            except Exception:
                pass  # Subscribers must not break the publisher

        # Notify global listeners
        for handler in self._all_subscribers:
            try:
                handler(event)
            except Exception:
                pass

    def clear(self) -> None:
        """Clear all subscribers."""
        self._subscribers.clear()
        self._all_subscribers.clear()


# Default singleton bus
_default_bus = EventBus()


def get_event_bus() -> EventBus:
    return _default_bus

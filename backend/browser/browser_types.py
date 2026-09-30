"""Typed structures and enums for browser management."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List
from backend.domain.models import utc_now_iso


class BrowserType(str, Enum):
    CHROMIUM = "chromium"
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class BrowserStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    CRASHED = "CRASHED"
    UNAVAILABLE = "UNAVAILABLE"


class SessionStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    CRASHED = "CRASHED"
    UNAVAILABLE = "UNAVAILABLE"


class PageStatus(str, Enum):
    DETACHED = "DETACHED"
    NAVIGATING = "NAVIGATING"
    LOADED = "LOADED"
    CRASHED = "CRASHED"
    CLOSED = "CLOSED"


@dataclass
class BrowserLaunchConfig:
    browser_type: BrowserType = BrowserType.CHROMIUM
    headless: bool = True
    timeout_seconds: int = 30
    profile_directory: Optional[str] = None
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: Optional[str] = None
    extra_args: List[str] = field(default_factory=list)


@dataclass
class BrowserHealthResult:
    healthy: bool
    browser_connected: bool
    context_available: bool
    page_available: bool
    current_url: Optional[str] = None
    latency_ms: float = 0.0
    error_code: Optional[str] = None
    details: Optional[str] = None


@dataclass
class BrowserSessionInfo:
    session_id: str
    worker_id: Optional[str]
    browser_type: BrowserType
    profile_id: Optional[str]
    status: SessionStatus
    created_at: str = field(default_factory=utc_now_iso)
    last_activity_at: str = field(default_factory=utc_now_iso)
    current_url: Optional[str] = None

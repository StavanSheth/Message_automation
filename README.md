# Instagram Browser Automation System

Production-grade, failure-safe, observable, and recoverable automation engine for Instagram messaging and follow-ups.

## Architecture

- **Domain**: Pure business models, errors, and strict state machine rules.
- **Repositories**: SQLite-backed repositories with transactional integrity and atomic lease locking.
- **Application Services**: ExecutionService, FollowupService, TaskService, SourceService, ApplicationLifecycleManager.
- **Workers & Scheduler**: Autonomous worker instances, persistent task leases, background scheduler loop.
- **Browser Automation**: Playwright session management, Instagram profile navigation, verification, and single-send messaging.
- **Reconciliation & Recovery**: Automatic safe reconciliation for ambiguous send outcomes and crash recovery.

## Development

```bash
pip install -e ".[dev]"
pytest
```

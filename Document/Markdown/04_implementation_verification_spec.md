# Document 4 — Implementation & Verification Specification

## 1. Implementation Order

Build in this dependency order:

```text
1. Project foundation/configuration
2. SQLite schema/migrations
3. Repository layer
4. Domain/state models
5. Task manager
6. Scheduler
7. Source adapters
8. Dashboard
9. Worker manager
10. Instagram browser adapter
11. Verification/OCR
12. Result detector
13. Follow-up engine
14. Recovery engine
15. Live events
16. Source write-back
17. Packaging
18. Full test suite
```

Every layer must remain independently testable.

## 2. Required Interfaces

### Source

```text
SourceAdapter
- open()
- validate_access()
- read_records()
- update_record()
- sync()
- close()
```

### Task

```text
TaskRepository
- create()
- get()
- claim()
- update_state()
- release()
- list_ready()
- list_interrupted()
```

### Scheduler

```text
Scheduler
- start()
- pause()
- resume()
- tick()
- schedule_followup()
- cancel_followups()
```

### Browser

```text
BrowserWorker
- start()
- stop()
- health()
- open_profile()
- screenshot()
- current_state()
```

### Instagram

```text
InstagramAdapter
- check_login()
- open_profile()
- extract_profile()
- check_message_availability()
- prepare_message()
- send_message()
- detect_result()
- inspect_conversation()
```

### Verification

```text
VerificationService
- extract_signals()
- calculate_confidence()
- decide()
```

### Recovery

```text
RecoveryService
- reconcile_interrupted()
- reconcile_unknown_send()
- recover_worker()
```

## 3. Database Acceptance Rules

The database must guarantee:
- unique contact identity;
- unique active task for a logical task sequence;
- valid state transitions;
- timestamps in a consistent timezone;
- transactional updates;
- no task marked complete without result;
- no follow-up created from an unsuccessful prerequisite.

## 4. Transaction Rules

Examples:

### Successful message
One transaction should persist:
- task result;
- message status;
- timestamp;
- automation event;
- follow-up schedule.

### Reply YES
One transaction should persist:
- contact replied state;
- cancellation of pending follow-ups;
- event;
- source-sync request.

Partial updates must be recoverable.

## 5. Dashboard Acceptance Criteria

### Overview
Must update live without full-page refresh.

### Live worker
Must show:
- worker status;
- task;
- stage;
- elapsed time;
- last event.

### Queue
Must support:
- filtering;
- sorting;
- retry;
- cancel;
- pause/resume;
- reschedule.

### Contact
Must show complete timeline.

### Attention
Must show actionable failures and required user actions.

## 6. Source Acceptance Criteria

### Local Excel
- import workbook;
- detect headers;
- validate required columns;
- preserve original values;
- map rows to stable contact IDs;
- write controlled status updates;
- detect conflicting edits.

### Browser spreadsheet
- accept arbitrary URL;
- open in dedicated Chrome;
- determine access;
- if inaccessible: `ACCESS_PROHIBITED`;
- if accessible: read/write through browser;
- retain source URL;
- show sync status.

The implementation must not assume a particular vendor merely because the URL looks familiar.

## 7. Instagram Acceptance Criteria

### Login
- already logged in → worker proceeds;
- not logged in → `LOGIN_REQUIRED`;
- no password collection.

### Verification
- supplied profile is opened;
- visible signals extracted;
- confidence calculated;
- low/ambiguous result blocks sending.

### Manual mode
No send occurs without explicit approval.

### Automatic mode
Send occurs only after configured verification criteria pass.

### Result detection
Every send attempt must resolve to:
- confirmed;
- unavailable;
- failed;
- unknown/manual review.

## 8. Follow-Up Acceptance Criteria

### Example

Initial:
```text
SENT at T0
```

Configured:
```text
F/U1 = +5 days
F/U2 = +5 days after F/U1
```

At T0 + 5 days:
- if `Replied = YES` → no F/U1;
- if `NO` → F/U1 becomes due;
- if `UNKNOWN` → follow configured human-review policy.

After F/U1:
- schedule F/U2;
- at F/U2 due, check `Replied` again.

Changing `Replied` to `YES` must cancel pending follow-ups.

## 9. Recovery Tests

### Test A — Power interruption
1. Start task.
2. Interrupt application.
3. Restart.
4. Verify `RUNNING` becomes `INTERRUPTED`.
5. Reconcile.
6. Verify no blind duplicate send.

### Test B — Network loss
1. Start task.
2. Disconnect network.
3. Verify new tasks stop.
4. Restore network.
5. Verify health check.
6. Verify queue resumes.

### Test C — Browser crash
1. Start worker.
2. Kill browser.
3. Verify worker failure.
4. Restart/recover.
5. Reconcile task.

### Test D — Login expiration
1. Remove session.
2. Start task.
3. Verify `LOGIN_REQUIRED`.
4. Verify task remains pending.
5. Log in.
6. Verify controlled resume.

### Test E — DM unavailable
1. Open profile where messaging is unavailable.
2. Verify `DM_NOT_AVAILABLE`.
3. Verify task is not endlessly retried.

### Test F — Unknown send
1. Trigger send.
2. Prevent confirmation.
3. Verify `UNKNOWN_RESULT`.
4. Restart.
5. Verify reconciliation before retry.

### Test G — Reply
1. Initial message sent.
2. Set `Replied = YES`.
3. Verify future follow-ups cancelled.
4. Set source update.
5. Verify event recorded.

### Test H — Duplicate
1. Create same logical task twice.
2. Verify only one active task exists.

## 10. Performance

The system must:
- keep UI responsive while workers operate;
- avoid continuous OCR;
- OCR only when needed;
- avoid busy-loop polling;
- use event-driven updates where practical;
- limit concurrent browsers by hardware;
- release screenshots/temporary resources after use;
- avoid loading entire large spreadsheets repeatedly.

## 11. Logging

Levels:
- DEBUG
- INFO
- WARNING
- ERROR
- CRITICAL

Every automation task gets a correlation ID.

Example:

```text
TASK-000184
WORKER-02
RUN-000912
```

Logs must allow reconstruction of:
- what started;
- what was detected;
- what decision was made;
- what action occurred;
- what result occurred;
- why a retry/manual review happened.

## 12. Observability

Metrics:
- tasks/minute;
- success count;
- failure count;
- retry count;
- manual-review count;
- average verification time;
- average task time;
- worker uptime;
- browser crashes;
- network interruptions;
- source sync failures.

## 13. Packaging

Application should have:
- installer;
- application data directory;
- database directory;
- browser profile directory;
- logs directory;
- temporary files directory.

Example:

```text
AppData/
├── database/
├── browser_profiles/
├── logs/
├── cache/
├── screenshots/
└── config/
```

Do not place mutable runtime data beside immutable application binaries.

## 14. Configuration Profiles

Support:
- manual sending;
- automatic sending;
- single browser;
- multi-browser;
- global follow-up defaults;
- per-contact follow-up overrides;
- verification thresholds.

Settings must be persisted.

## 15. Definition of Done

The implementation is considered complete only when:

### Functional
- source import works;
- source browser access works;
- contacts are persisted;
- verification works;
- manual approval works;
- automatic mode works;
- initial message workflow works;
- follow-up 1 works;
- follow-up 2 works;
- manual replied status works;
- dashboard works;
- history works.

### Recovery
- network recovery works;
- power/application restart recovery works;
- browser crash recovery works;
- unknown-send reconciliation works;
- login-required handling works.

### Data integrity
- duplicate tasks prevented;
- transactions implemented;
- source conflicts detected;
- follow-up cancellation reliable.

### Hardware
- NVIDIA >= 2 GB exposes single/multi choice;
- no qualifying NVIDIA GPU exposes single mode;
- worker count is resource bounded.

### Safety/compliance boundary
- no CAPTCHA bypass;
- no credential harvesting;
- no anti-detection implementation;
- no private-data bypass;
- no attempt to circumvent platform restrictions.

## 16. Final Verification

Before release run:

```text
Unit tests
→ Integration tests
→ Browser-worker tests
→ Source-sync tests
→ Recovery tests
→ UI tests
→ Packaging test
→ Clean-machine test
→ Long-running stability test
```

A production build must pass all mandatory acceptance tests before being considered complete.

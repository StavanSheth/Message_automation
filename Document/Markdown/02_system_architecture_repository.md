# Document 2 — System Architecture & Repository Specification

## 1. Architecture

Use a hybrid architecture:

```text
Desktop Shell / Launcher
        │
        ├── Local Web Dashboard
        │
        └── Local Backend
                │
                ├── Database
                ├── Task Manager
                ├── Scheduler
                ├── Follow-up Engine
                ├── Spreadsheet Source Adapter
                ├── Instagram Worker Manager
                ├── Verification Engine
                ├── OCR/Vision Engine
                ├── Recovery Manager
                └── Event/Logging System

Instagram Worker
        │
        └── Dedicated Chrome Profile
```

Recommended stack:
- Frontend: React + TypeScript.
- Desktop shell: lightweight desktop wrapper or Python/PySide6 launcher as selected during implementation.
- Backend/control plane: Python.
- Browser automation: Playwright.
- OCR: PaddleOCR or Tesseract.
- Image processing: OpenCV.
- Database: SQLite.
- Spreadsheet parsing: openpyxl/pandas.
- Local communication: HTTP/WebSocket or equivalent typed local IPC.
- Packaging: desktop installer with isolated application data directory.

The architecture must keep browser automation independent from the UI process.

## 2. Repository

```text
project/
├── apps/
│   ├── dashboard/
│   │   ├── pages/
│   │   ├── components/
│   │   ├── hooks/
│   │   ├── services/
│   │   └── types/
│   └── desktop/
│
├── backend/
│   ├── api/
│   ├── application/
│   ├── domain/
│   ├── repositories/
│   ├── database/
│   ├── scheduler/
│   ├── tasks/
│   ├── workers/
│   ├── automation/
│   │   └── instagram/
│   ├── verification/
│   ├── vision/
│   ├── result_detection/
│   ├── followups/
│   ├── sources/
│   │   ├── xlsx/
│   │   └── browser_sheet/
│   ├── recovery/
│   ├── health/
│   ├── events/
│   ├── config/
│   └── security/
│
├── database/
│   ├── migrations/
│   └── seeds/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── recovery/
│   └── fixtures/
│
├── scripts/
├── packaging/
├── config/
└── docs/
```

## 3. Module Contracts

### Dashboard
Responsibilities:
- display state;
- send user commands;
- subscribe to live events;
- never directly automate Chrome.

### Source Manager
Interface:

```text
open_source()
validate_access()
read_rows()
detect_schema()
write_updates()
sync_changes()
close_source()
```

Two adapters:
1. `LocalXlsxSource`
2. `BrowserSpreadsheetSource`

### BrowserSpreadsheetSource

Input:
- arbitrary user-provided URL.

Workflow:

```text
URL
 ↓
Open dedicated source Chrome/page
 ↓
Load
 ↓
Access available?
 ├── NO → ACCESS_PROHIBITED
 └── YES
       ↓
Detect supported sheet/table structure
       ↓
Read/write through browser UI
```

No platform API is assumed.

### Contact Manager
Responsibilities:
- normalize contact;
- maintain stable contact ID;
- map source row to contact;
- prevent duplicate contact/task creation.

### Task Manager
Responsibilities:
- create;
- queue;
- claim;
- lock;
- complete;
- retry;
- cancel;
- reschedule;
- reconcile interrupted tasks.

### Scheduler
Responsibilities:
- find due tasks;
- create follow-up tasks;
- honor paused state;
- honor replied state;
- prevent duplicate scheduling.

### Worker Manager
Responsibilities:
- launch/stop workers;
- detect available hardware;
- choose single/multi mode;
- maintain worker heartbeat;
- recover crashed workers.

### Instagram Worker
Responsibilities:
- use dedicated persistent Chrome profile;
- verify login state;
- navigate supplied profile;
- capture visible state;
- call verification;
- perform configured user-approved/automatic workflow;
- detect result;
- return structured result.

### Verification Engine

Inputs:
- expected contact data;
- current Instagram visible data.

Output:

```text
confidence
signals[]
decision
reason
```

The engine must not send by itself.

### OCR/Vision
Responsibilities:
- screenshot preprocessing;
- OCR;
- region detection;
- text extraction;
- optional image similarity.

OCR is a supporting layer, not the sole identity mechanism.

### Result Detector
Convert observed UI state into structured results:

```text
SUCCESS
DM_NOT_AVAILABLE
LOGIN_REQUIRED
CHALLENGE_REQUIRED
PROFILE_NOT_FOUND
PROFILE_MISMATCH
SEND_FAILED
NETWORK_ERROR
TIMEOUT
UNKNOWN
```

### Recovery Manager
Responsibilities:
- restart worker;
- retry eligible task;
- reconcile interrupted task;
- move uncertain states to manual attention;
- preserve evidence/events.

## 4. Database

Minimum entities:

```text
contacts
source_records
tasks
messages
followups
automation_runs
workers
browser_sessions
verification_results
events
errors
sync_runs
settings
```

### contacts

```text
id
source_record_id
name
instagram_url
username
expected_followers
notes
replied_status
replied_source
replied_at
created_at
updated_at
```

### tasks

```text
id
contact_id
type
status
priority
scheduled_at
started_at
completed_at
attempt_count
worker_id
last_error_id
created_at
updated_at
```

Task types:
- `MESSAGE`
- `FOLLOW_UP_1`
- `FOLLOW_UP_2`

### messages

```text
id
contact_id
task_id
sequence
body
status
attempted_at
confirmed_at
result_code
created_at
```

### followups

```text
id
contact_id
sequence
message
delay_seconds
scheduled_at
status
sent_at
cancelled_at
cancel_reason
```

### verification_results

```text
id
contact_id
task_id
confidence
decision
signals_json
ocr_text
created_at
```

### events

```text
id
timestamp
level
category
entity_type
entity_id
event_code
payload_json
```

### errors

```text
id
task_id
code
message
severity
retryable
attempt
created_at
resolved_at
```

## 5. Source Synchronization

Database is the execution source of truth.

For local Excel:
```text
XLSX → import → DB
DB → controlled write-back → XLSX
```

For browser spreadsheet:
```text
Sheet URL → Chrome → read → DB
DB → Chrome sheet UI → write-back
```

The application must track:
- source version/hash where available;
- last sync;
- row identity;
- last written value;
- conflict state.

A source update must not silently overwrite a newer human edit.

## 6. Live Communication

Use event-driven updates.

Backend emits events such as:

```text
TASK_CREATED
TASK_STARTED
VERIFICATION_STARTED
VERIFICATION_COMPLETED
MESSAGE_ATTEMPTED
MESSAGE_CONFIRMED
TASK_FAILED
TASK_RETRY_SCHEDULED
FOLLOWUP_CREATED
FOLLOWUP_CANCELLED
WORKER_STARTED
WORKER_STOPPED
LOGIN_REQUIRED
ACCESS_PROHIBITED
NETWORK_OFFLINE
RECOVERY_STARTED
RECOVERY_COMPLETED
```

Dashboard subscribes to these events.

## 7. Browser Profiles

Use isolated persistent browser profiles.

Concept:

```text
app_data/
└── browser_profiles/
    └── instagram/
        ├── account_1/
        └── ...
```

The actual profile layout may be simplified if only one account is supported initially.

Do not store passwords.

## 8. Worker Modes

### Single
One Instagram browser worker.

### Multi
Multiple independent browser workers only when qualifying NVIDIA GPU exists and user selects multi mode.

Before launching workers:
- inspect RAM;
- inspect CPU;
- inspect VRAM;
- apply configured maximum;
- reserve memory headroom;
- refuse unsafe worker count.

Workers must have independent task locks.

## 9. Modular Execution

Every service should be startable independently where meaningful.

Examples:
- dashboard only;
- database/source management only;
- scheduler only;
- Instagram worker only;
- OCR verification only;
- follow-up engine only.

A module must declare dependencies rather than assuming the whole application exists.

## 10. Configuration

Central typed configuration:

```text
execution_mode
verification_threshold
message_mode
worker_mode
max_workers
followup_1_delay
followup_2_delay
task_timeout
retry_limit
network_retry_delay
browser_timeout
source_sync_interval
log_level
```

Secrets/passwords must not be stored in source files.

## 11. Security and Privacy

- Local database access controlled by OS/application permissions.
- No Instagram passwords stored.
- Avoid logging message bodies unless explicitly enabled.
- Redact sensitive values from errors.
- Browser session data stored in application-controlled profile directory.
- Provide clear data deletion/export controls.

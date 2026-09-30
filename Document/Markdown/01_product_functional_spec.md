# Document 1 — Product & Functional Specification

## 1. Purpose

Build a modular desktop/web-hybrid system for managing user-provided contact/message data and executing Instagram browser workflows through an already logged-in Chrome session.

The system must:

- Accept local Excel files.
- Accept any user-provided spreadsheet URL by opening that URL in a dedicated Chrome browser.
- Use the opened spreadsheet itself as the editable source when it is accessible.
- Never require an Instagram API key.
- Use Chrome/browser interaction for Instagram.
- Verify the supplied Instagram profile before messaging.
- Support manual approval before sending OR automatic sending according to user settings.
- Maintain a durable local database as the automation state/source of truth.
- Support configurable follow-up 1 and follow-up 2 workflows.
- Require a human to update `Replied = YES/NO/UNKNOWN`; no automated reply detection.
- Recover safely from network loss, browser crashes, power interruption, session expiry, OCR failures and ambiguous outcomes.
- Be modular so individual modules can be used without the full application.

## 2. Product Boundary

### Supported platform
- Instagram only.

### Supported data sources
1. Local `.xlsx`.
2. Any spreadsheet/web-sheet URL supplied by the user.
3. The URL is opened in a dedicated Chrome instance.
4. If the page cannot be accessed because authentication/permission is required, mark the source as `ACCESS_PROHIBITED` and notify the user.
5. No assumption may be made that every URL is Google Sheets; source detection is browser/page based.

### Instagram authentication
- The user is expected to already be logged in.
- The system must detect whether the Instagram session is usable.
- If login is required, stop the Instagram worker and notify the user.
- Never collect or persist the user's Instagram password.

## 3. Operating Modes

### Verification mode
Every message task first performs profile verification.

Verification checks may include:
- supplied profile URL;
- visible username;
- visible display name;
- supplied expected follower information;
- bio/visible profile text;
- profile image/other visible signals when configured.

Follower count is supporting evidence, not a primary identity key.

### Sending mode A — Manual approval
1. Verify profile.
2. Display verification result and confidence.
3. Wait for user approval.
4. Send only after approval.
5. Persist the decision and result.

### Sending mode B — Automatic
1. Verify profile.
2. Apply configured confidence/verification policy.
3. If the task passes policy, proceed automatically.
4. If the result is ambiguous, stop and require review.

The system must never treat low-confidence identity as an automatic send.

## 4. Core User Workflow

```text
Create/import source
        ↓
Validate rows
        ↓
Create/update contacts
        ↓
Create message tasks
        ↓
Queue
        ↓
Instagram verification
        ↓
Verification result
        ↓
Manual approval OR automatic policy
        ↓
Send attempt
        ↓
Result detection
        ↓
Persist result
        ↓
Schedule follow-up
        ↓
Human updates Replied
        ↓
Cancel/continue follow-up workflow
```

## 5. Dashboard

### Overview
Display live:
- total contacts;
- pending tasks;
- ready tasks;
- running tasks;
- completed tasks;
- failed tasks;
- skipped tasks;
- manual-attention tasks;
- follow-ups due;
- follow-ups scheduled;
- interrupted/recovery tasks;
- current workers;
- browser health;
- network health;
- source sync status.

### Live Automation
For every worker:
- worker ID;
- status;
- browser status;
- current contact;
- current task;
- current stage;
- verification confidence;
- elapsed time;
- last event;
- error/attention state.

### Queue
Columns:
- task ID;
- contact;
- task type;
- scheduled time;
- priority;
- status;
- attempts;
- last result.

Actions:
- pause;
- resume;
- retry;
- cancel;
- reschedule;
- open contact;
- open source row.

### Contacts
Show:
- identity data;
- source row;
- Instagram URL/username;
- message;
- initial-message status;
- follow-up 1 status/date;
- follow-up 2 status/date;
- replied status;
- last result;
- notes;
- history.

### Follow-ups
Filters:
- due today;
- overdue;
- scheduled;
- waiting for reply;
- completed;
- skipped;
- cancelled.

### Attention Center
Show:
- Instagram login required;
- source access prohibited;
- CAPTCHA/challenge/manual intervention;
- ambiguous verification;
- browser failure;
- network failure;
- unknown send result;
- source synchronization conflict;
- interrupted tasks.

### History
Immutable event timeline for:
- imports;
- edits;
- verification;
- approval;
- send attempt;
- send result;
- follow-up creation;
- follow-up cancellation;
- retry;
- failure;
- manual changes;
- recovery.

## 6. Data Model Presented to Users

Minimum spreadsheet fields:

| Field | Required | Purpose |
|---|---|---|
| Contact ID | Recommended | Stable identity |
| Name | Yes | Expected person/business |
| Instagram URL | Yes | Target profile |
| Username | Optional | Verification |
| Expected Followers | Optional | Supporting verification signal |
| Message | Yes | Initial message |
| Follow-up 1 Message | Optional | First follow-up |
| Follow-up 1 Delay | Configurable | Scheduling |
| Follow-up 2 Message | Optional | Second follow-up |
| Follow-up 2 Delay | Configurable | Scheduling |
| Replied | Yes | `UNKNOWN/YES/NO` |
| Notes | Optional | Human notes |

The system may add/manage operational columns such as status/date/time, but the database remains authoritative for execution state.

## 7. Follow-up Rules

### Initial message
On successful send:
- mark initial message `SENT`;
- record exact timestamp;
- schedule follow-up 1 if configured.

### Follow-up 1
Before execution:
- contact must not be marked `Replied = YES`;
- initial message must have succeeded;
- follow-up 1 must be due;
- task must not already be completed/cancelled.

After successful send:
- mark follow-up 1 `SENT`;
- record timestamp;
- schedule follow-up 2 if configured.

### Follow-up 2
Before execution:
- `Replied` must not be `YES`;
- follow-up 1 must have succeeded;
- follow-up 2 must be due.

If `Replied = YES`:
- cancel pending follow-up tasks;
- record cancellation reason `REPLIED`.

If `Replied = UNKNOWN`:
- do not infer `NO`;
- follow the configured human-review policy.

## 8. Replied Field

Allowed values:
- `UNKNOWN`
- `YES`
- `NO`

Metadata:
- source: `MANUAL`;
- changed_at;
- changed_by;
- previous value.

Automatic reply detection is explicitly out of scope.

## 9. Platform Result Handling

The system must distinguish:
- message sent/confirmed;
- message unavailable;
- account does not accept messages;
- send failed;
- network failure;
- login required;
- challenge/CAPTCHA/manual intervention;
- profile unavailable;
- profile mismatch;
- unknown result.

Example:
If Instagram visibly indicates that the account does not accept messages, mark:
`SKIPPED / DM_NOT_AVAILABLE`.
Do not endlessly retry.

If the platform displays an automated response, record it as a platform response/event; do not interpret it as a human reply and do not automatically set `Replied = YES`.

## 10. Modular Product Principle

Each module must expose clear interfaces and operate independently where technically meaningful.

Core modules:
- Dashboard;
- Spreadsheet Source;
- Contact Manager;
- Task Manager;
- Scheduler;
- Follow-up Manager;
- Instagram Browser Worker;
- Verification Engine;
- OCR/Vision;
- Result Detector;
- Recovery Manager;
- Event/Log Manager;
- System Health;
- Settings.

No module should directly reach into another module's database tables when a defined service/repository interface exists.

## 11. GPU / Worker Selection

On startup:
- detect NVIDIA GPU and available VRAM;
- if NVIDIA VRAM >= 2 GB, expose both `SINGLE_BROWSER` and `MULTI_BROWSER` modes;
- if no qualifying NVIDIA GPU exists, only `SINGLE_BROWSER` mode is available.

Multi-browser worker count must still be bounded by:
- available RAM;
- CPU;
- VRAM;
- configured worker maximum.

The presence of a 2 GB GPU does not imply unlimited concurrency.

## 12. Non-Goals

- No Instagram API dependency.
- No Instagram password storage.
- No automated reply interpretation.
- No CAPTCHA bypass.
- No anti-detection/fingerprint-evasion system.
- No proxy rotation intended to bypass platform restrictions.
- No account creation/farming.
- No private-profile data collection beyond information legitimately visible through the logged-in browser.

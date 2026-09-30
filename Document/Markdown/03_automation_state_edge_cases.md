# Document 3 — Automation, State Machine & Edge-Case Specification

## 1. Fundamental Rule

The system must be **fail-closed**.

If it cannot confidently determine:
- who the target is;
- whether the task is still valid;
- whether a previous send happened;
- whether the browser state is usable;

it must not blindly send again.

Move the task to `MANUAL_REVIEW` or a retry state as appropriate.

## 2. Task State Machine

```text
CREATED
  ↓
VALIDATING
  ↓
QUEUED
  ↓
READY
  ↓
RUNNING
 ├── SUCCESS → COMPLETED
 ├── RETRYABLE_ERROR → RETRY_WAIT → READY
 ├── MANUAL_REVIEW → MANUAL_REVIEW
 ├── SKIPPED → SKIPPED
 ├── CANCELLED → CANCELLED
 └── UNKNOWN_RESULT → RECONCILING
                         ├── CONFIRMED → COMPLETED
                         ├── NOT_DONE → READY
                         └── UNKNOWN → MANUAL_REVIEW
```

## 3. Message State Machine

```text
PENDING
 ↓
VERIFYING
 ↓
VERIFIED
 ↓
AWAITING_APPROVAL      (manual mode)
 ↓
APPROVED
 ↓
SENDING
 ├── CONFIRMED → SENT
 ├── FAILED → RETRY/FAILED_FINAL
 ├── UNAVAILABLE → SKIPPED
 └── UNKNOWN → RECONCILIATION
```

Automatic mode bypasses `AWAITING_APPROVAL` only when configured and verification passes.

## 4. Verification

Required before every initial message and any follow-up where profile state needs reconfirmation.

Signals:
- profile URL;
- username;
- display name;
- expected follower count/range;
- visible bio;
- other configured visible attributes;
- optional profile-image similarity.

Example weighted policy:

```text
URL exact match          strongest
Username exact match     strong
Name match               strong
Bio/visible text         supporting
Follower count           weak/supporting
Image similarity         supporting
```

Weights must be configurable.

Do not claim identity from follower count alone.

## 5. Confidence Decisions

Example configurable thresholds:

```text
HIGH_CONFIDENCE
MEDIUM_CONFIDENCE
LOW_CONFIDENCE
```

The exact numerical threshold is a setting, not hard-coded into the architecture.

Example behavior:

```text
HIGH → automatic mode may proceed
MEDIUM → manual review
LOW → stop
```

Manual mode always requires explicit user approval regardless of confidence.

## 6. Instagram Login

At worker startup:

```text
Open Instagram
 ↓
Check authenticated state
 ├── authenticated → continue
 └── unauthenticated → LOGIN_REQUIRED
```

`LOGIN_REQUIRED`:
- pause affected tasks;
- notify user;
- do not retry continuously;
- resume only after login state is confirmed.

## 7. Profile Errors

### PROFILE_NOT_FOUND
- mark task skipped;
- record reason;
- no automatic retry.

### PROFILE_MISMATCH
- stop immediately;
- never send;
- require correction/review.

### OCR_LOW_CONFIDENCE
- retry OCR once/twice with different preprocessing;
- if still ambiguous → manual review.

### UI_CHANGED
- capture screenshot/evidence;
- mark `MANUAL_REVIEW`;
- do not guess button locations.

## 8. Message Availability

If UI indicates:
- account cannot receive messages;
- messaging unavailable;
- message action unavailable;

then:

```text
status = SKIPPED
reason_code = DM_NOT_AVAILABLE
retryable = false
```

Do not repeatedly retry a deterministic unavailability state.

## 9. Message Send Failure

Classify:

### Retryable
- transient network failure;
- page timeout;
- browser communication failure.

### Non-retryable
- account cannot receive message;
- profile unavailable;
- permanent platform restriction;
- invalid task.

### Unknown
The click/action occurred but confirmation cannot be established.

Unknown is not equivalent to failure.

## 10. Unknown Send Result

Example:

```text
Message entered
 ↓
Send action triggered
 ↓
Browser crashes / network disappears
 ↓
No confirmation
```

Set:

```text
UNKNOWN_RESULT
```

On recovery:
1. reopen the target;
2. inspect conversation/profile state;
3. determine whether the message appears sent;
4. if confirmed → `SENT`;
5. if clearly absent → retry;
6. if still uncertain → manual review.

Never blindly resend an unknown message.

## 11. Network Failure

States:

```text
ONLINE
DEGRADED
OFFLINE
```

When offline:
- stop starting new browser actions;
- allow current operation to settle if safe;
- queue tasks;
- exponential/backoff health checks;
- resume after connectivity confirmation.

No busy-loop retries.

## 12. Power Failure / Application Crash

On startup:
1. load database;
2. find tasks in `RUNNING`;
3. mark them `INTERRUPTED`;
4. inspect automation run metadata;
5. reconcile before retrying.

Never automatically assume an interrupted message was not sent.

## 13. Browser Crash

Worker manager:
1. mark worker unavailable;
2. preserve task state;
3. restart browser if safe;
4. restore persistent session;
5. reconcile interrupted task;
6. resume only after health check.

## 14. CAPTCHA / Challenge

If a challenge/CAPTCHA/manual verification is detected:
- stop affected worker;
- set `MANUAL_REVIEW`;
- notify user;
- never attempt CAPTCHA bypass;
- wait for user resolution.

## 15. Automated Platform Response

If a business/account presents an automated response:
- store visible response/event;
- do not treat it as a human reply;
- do not set `Replied = YES`;
- allow human to decide `Replied`.

## 16. Reply Workflow

Only human-controlled status:

```text
UNKNOWN
YES
NO
```

If user changes:

```text
NO → YES
```

then:
- cancel pending follow-up 1/2 tasks;
- record event;
- write back to source;
- update dashboard.

If:

```text
YES → NO
```

require explicit confirmation because it may reactivate follow-up scheduling.

## 17. Duplicate Prevention

Before task execution:
- acquire task lock;
- verify task has no completed equivalent;
- verify contact/sequence has not already succeeded;
- verify no active duplicate task.

Unique logical key:

```text
(contact_id, task_type, sequence)
```

for the relevant lifecycle.

## 18. Follow-Up Scheduling

Initial send success:
```text
create FOLLOW_UP_1 if configured
```

Follow-up 1 success:
```text
create FOLLOW_UP_2 if configured
```

Before each follow-up:
```text
if replied == YES:
    cancel
elif task invalid:
    skip
elif due:
    execute
else:
    wait
```

Delays are configurable per contact or globally, depending on product settings.

## 19. Retry Policy

Each error has:
- retryable;
- maximum attempts;
- delay;
- backoff;
- final action.

Example:

```text
attempt 1 → wait
attempt 2 → longer wait
attempt 3 → manual review/failed final
```

Never retry deterministic failures indefinitely.

## 20. Source Access Failure

For spreadsheet URL:

```text
OPEN_URL
 ↓
ACCESS_CHECK
 ├── accessible → continue
 ├── login required → USER_ACTION_REQUIRED
 ├── permission denied → ACCESS_PROHIBITED
 └── unreachable → SOURCE_UNAVAILABLE
```

The dashboard must clearly distinguish these states.

## 21. Spreadsheet Conflict

If a human changes a row while the system is working:
- detect changed source value where possible;
- compare against last synchronized value;
- do not silently overwrite;
- create `SYNC_CONFLICT`;
- require resolution for important fields.

## 22. Manual Stop/Pause

Pause:
- stop starting new tasks;
- allow safe current operation to settle;
- retain queue.

Stop:
- request graceful worker shutdown;
- persist all states;
- reconcile any uncertain task.

## 23. Worker Hardware Edge Cases

If user selects multi-browser but hardware/resources are insufficient:
- show estimated supported worker count;
- allow reducing worker count;
- do not force launch.

If NVIDIA >= 2 GB:
- expose `SINGLE` and `MULTI`.

If no qualifying NVIDIA GPU:
- expose `SINGLE` only.

RAM/CPU remain limiting factors.

## 24. Event Evidence

For important failures, store:
- timestamp;
- task ID;
- worker ID;
- error code;
- current URL if safe;
- screenshot path/reference where appropriate;
- OCR text only when permitted;
- browser state;
- retry decision.

Avoid storing unnecessary sensitive content.

## 25. Edge-Case Matrix

| Case | State | Action |
|---|---|---|
| Login required | LOGIN_REQUIRED | Pause + notify |
| Access denied | ACCESS_PROHIBITED | Stop source |
| Network down | OFFLINE | Pause queue |
| Browser crash | INTERRUPTED | Recover + reconcile |
| Power loss | INTERRUPTED | Reconcile |
| Profile missing | SKIPPED | No retry |
| Profile mismatch | MANUAL_REVIEW | Never send |
| OCR ambiguous | MANUAL_REVIEW | Never guess |
| DM unavailable | SKIPPED | No repeated retry |
| Send transient failure | RETRY_WAIT | Retry |
| Send unknown | RECONCILING | Verify before retry |
| CAPTCHA | MANUAL_REVIEW | User action |
| Reply YES | FOLLOWUPS_CANCELLED | Record |
| Reply NO | Continue | Schedule when due |
| Reply UNKNOWN | WAIT | Human decision |
| Duplicate task | SKIPPED | Preserve existing |
| Sheet permission denied | ACCESS_PROHIBITED | Notify |
| Sheet changed externally | SYNC_CONFLICT | Resolve |
| UI unexpected | MANUAL_REVIEW | Preserve evidence |

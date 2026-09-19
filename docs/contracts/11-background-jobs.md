# 11 — Background-job status contract

**Deliverable 9.** What a unit of background work is, what states it can be in,
what a client is told about it, and what it must persist so that a crash, a
deploy, an AI outage or a second click cannot lose or duplicate work.

Most of this is **shipped** and works: `pipeline_jobs`, atomic claiming with a
lease, retries with backoff, dead-letter, `pause`/`drain_paused` for AI outages,
`recover_stalled` with CAS + safety margin + `reclaim_count`, dedupe keys, and the
live read (`GET /api/queues/ai`). This document pins that behaviour as a contract
and adds the four things it is missing: **progress**, **checkpoints**,
**cancellation**, and an **idempotency key column**.

---

## 1. The row

`pipeline_jobs` (shipped columns marked ✓, new marked **new**):

| Column | Type | Meaning |
|---|---|---|
| `id` ✓ | PK | the receipt the client keeps |
| `user_id` ✓ | FK, index | tenant — a worker only ever touches the row's own tenant |
| `pipeline` ✓ | String(20) | `QUEUE_PIPELINES`: `discovery` \| `application` \| `email` \| `funding` \| `ai` \| **`extraction`** \| **`onboarding`** \| **`browser_session`** |
| `task` **new** | String(40), nullable | the handler sub-kind (`generate_resume`, `tag_resume`, `extract_profile`). Today it lives in `payload.task`; it is promoted to a column because the live view, the metrics and the UI all read it |
| `job_id` ✓ | FK `jobs.id`, nullable | |
| `entity_type` / `entity_id` **new** | String(32) / Integer, nullable | the subject when it is not a job (`application`, `resume_document`, `discovery_run`, `onboarding_session`) — so a page can find its row without knowing the payload |
| `status` ✓ | String(16) | `QUEUE_STATUSES` |
| `priority` ✓ | Integer 1..9 | 1 = highest |
| `attempts` ✓ / `max_attempts` ✓ | Integer | **failure** budget only |
| `reclaim_count` ✓ | Integer | lease-expiry reclaims |
| `paused_count` ✓ *(in payload)* | Integer | AI-outage pauses — **moved to a column** so it is queryable and cannot be lost by a payload rewrite |
| `dedupe_key` ✓ | String(300) | `UNIQUE(user_id, dedupe_key)` |
| `idempotency_key` **new** | String(64), nullable | the client's `Idempotency-Key`; `UNIQUE(user_id, idempotency_key)` |
| `correlation_id` **new** | String(64), nullable | ties a run to the request/trigger that started it and to its events |
| `parent_job_id` **new** | FK self, nullable | a job that spawned sub-work (a discovery run that enqueues form detection) |
| `progress` **new** | JSON | §4 |
| `checkpoint` **new** | JSON | §5 |
| `payload` ✓ | JSON | inputs + `result` on completion |
| `scheduled_at` ✓ | DateTime, index | when it becomes claimable |
| `lease_expires_at` ✓ / `locked_by` ✓ | | the lease |
| `created_at` ✓ / `updated_at` ✓ / `started_at` **new** / `finished_at` ✓ | DateTime | |
| `error` ✓ | Text | last error message; `error_code` **new** from `ERROR_CODES` |
| `cancelled_at` **new** / `cancelled_by` **new** | DateTime / String(20) | |

Indexes: `ix_pipeline_jobs_claim (pipeline, status, scheduled_at)` ✓,
`INDEX(user_id, status, priority, created_at)` **new** (the live read),
`INDEX(user_id, pipeline, entity_type, entity_id)` **new** (find my row).

---

## 2. Statuses

`QUEUE_STATUSES` = `queued`, `processing`, `paused`, `needs_input`, `done`,
`failed`, `dead`, `cancelled`.

| Status | Meaning | Live? | Terminal? |
|---|---|---|---|
| `queued` | waiting for a worker; `scheduled_at` may be in the future (backoff) | yes | no |
| `processing` | a worker holds the lease | yes | no |
| `paused` | parked by a **transient AI outage**; resumes on its own; spends no attempt | yes | no |
| `needs_input` | parked until the user answers; the `result` says which fields | yes | no |
| `done` | the handler returned successfully — **the outcome is `result.status`, not `done`** | no | yes |
| `failed` | the handler raised; retries may remain (a `failed` row with attempts left goes back to `queued`) | no | no |
| `dead` | a budget is exhausted (failures, pauses or reclaims) | no | yes |
| `cancelled` | the user stopped it | no | yes |

`LIVE_STATUSES` (shipped) = `queued`, `processing`, `paused`, `needs_input` —
what the live feed shows. `RECENT_FINISHED_WINDOW` (shipped) = 30 minutes of
terminal rows, so a page refreshed mid-run still sees the outcome.

---

## 3. Status machine

| # | From | To | Actor / trigger | Guard | Side effects |
|---|---|---|---|---|---|
| Q1 | — | `queued` | `user` / `system_scheduler` / `system_api` — `enqueue()` | pipeline known; dedupe free, or the existing row is terminal (then it is **reused and reset**: `attempts = 0`, `reclaim_count = 0`, `paused_count = 0`, lease cleared) | receipt `{queued, duplicate, pipeline_job_id, queue_status, idempotency_key}`; `job.enqueued` |
| Q2 | `queued` | *(nothing)* | duplicate `enqueue()` with the same dedupe key while the row is live | — | returns `None` → the router answers `duplicate: true` **with** the live `pipeline_job_id` (`enqueue_or_existing`, shipped) |
| Q3 | `queued` | `processing` | `system_worker` — `claim()` / `claim_item()` | `scheduled_at <= now`; CAS `UPDATE … WHERE status='queued'` so exactly one worker wins | `locked_by`, `lease_expires_at = now + worker_lease_seconds`, `started_at`; **`attempts` untouched** |
| Q4 | `processing` | `done` | `system_worker` — `complete()` | handler returned | `finished_at`, lease cleared, `payload.result` written, `progress.percent = 100` |
| Q5 | `processing` | `queued` | `system_worker` — `fail(retryable=True)` | `attempts + 1 < max_attempts` | `attempts += 1`, `scheduled_at = now + backoff` (`min(600, base**attempts * 5)`), `error`/`error_code` stored |
| Q6 | `processing` | `dead` | `system_worker` — `fail()` with the budget exhausted, or `fail(retryable=False)` | `attempts >= max_attempts` | `finished_at`, `jobhunter_queue_dead_total`, notification `automation_failed` for auto runs |
| Q7 | `processing` | `paused` | `system_worker` — `pause()` on a transient AI outage | `paused_count < AI_PAUSE_MAX` (12) | `paused_count += 1`, backoff, **no attempt spent** |
| Q8 | `paused` | `dead` | `system_worker` — `pause()` with the pause budget exhausted | `paused_count > AI_PAUSE_MAX` | `finished_at`, error names the outage count |
| Q9 | `paused` | `queued` | `system_watchdog` — probe green (`drain_paused`), honouring the backoff; or `user` — one-click resume (`force=True`) | row is `paused` | `scheduled_at = now` |
| Q10 | `processing` | `needs_input` | `system_worker` — `needs_input()` | the handler needs the user | `result` written (the plan + `missing_fields` + `input_request_id`), lease cleared |
| Q11 | `needs_input` | `queued` | `user` — answers submitted | an input request exists and is `pending` | payload merged with the answers, `attempts = 0`, **`mode` preserved** (an `execute` run comes back as `execute` — shipped `_requeue_application`) |
| Q12 | `processing` | `queued` | `system_worker` — `recover_stalled()`, lease expired beyond the safety margin | CAS `WHERE status='processing' AND lease_expires_at < cutoff`; `attempts < max_attempts` and `reclaim_count < max_reclaims` | `reclaim_count += 1` (atomic SQL increment), lease cleared, per-job metric + log |
| Q13 | `processing` | `dead` | `system_worker` — `recover_stalled()` with a budget exhausted | `attempts >= max_attempts` **or** `reclaim_count >= max_reclaims` | crash-loop guard fires instead of bouncing forever |
| Q14 | any non-terminal | `cancelled` | `user` — `POST /api/queues/{id}/cancel`; or `system_api` — the owning aggregate was deleted | not `processing` **while an irreversible step is in flight** (see below) | `cancelled_at`, `cancelled_by`, lease released, `finished_at` |
| Q15 | `dead`, `failed`, `cancelled` | `queued` | `user` (retry from Queues) or `owner` (`POST /api/ops/queue/retry-dead`, shipped) | quota available | **budgets reset** (`attempts`, `reclaim_count`, `paused_count`) — a retry is a fresh run, not a continuation |

**Cancellation while `processing`** is a two-step: the row is marked
`cancelling` inside `progress` and the worker checks it at the next checkpoint.
An irreversible step (a browser mid-submission) is never interrupted; the cancel
takes effect after it, and the response says so
(`409 application_not_submittable` for that specific case,
[07 §4.4](07-application-state-machine.md)).

---

## 4. Progress contract

`progress` is written by the worker at every checkpoint and returned verbatim:

```json
{
  "step": "scoring",
  "step_index": 3,
  "steps_total": 7,
  "percent": 45,
  "message": "Scoring 84 candidates against your profile",
  "unit": { "done": 12, "total": 84, "label": "candidates" },
  "cancelling": false,
  "updated_at": "2026-09-18T14:03:22Z"
}
```

Rules:

1. `step` is from `QUEUE_PROGRESS_STEPS[pipeline]` — a fixed vocabulary per
   pipeline, so the UI can render a stepper without knowing the handler.
2. `percent` is an integer 0..100, **monotonic within one attempt**. It may reset
   to a lower value only when the attempt changes (a retry), and then
   `progress.attempt` is part of the payload.
3. `unit` is optional and always a real count of real things — never a fake
   percentage derived from elapsed time.
4. `message` is one human sentence with no ids the user cannot see.
5. A worker that cannot report progress writes nothing rather than writing a
   guess: an absent `progress` means "we do not know", and the UI shows the
   status chip only.
6. Progress is written **in the same transaction as the checkpoint**, so a stale
   progress bar with a fresh checkpoint (or vice versa) cannot exist.

The shipped in-memory `_upload_progress` map in `app/api/routers/resumes.py`
becomes a *convenience view* of this column: `GET /api/resume/progress` keeps its
shape (`{step, detail, progress, updated_at}`) but is documented as best-effort,
and the durable answer is the queue row.

---

## 5. Checkpoint contract (resumability)

`checkpoint` is what makes a long-running operation resumable rather than
restartable. It is pipeline-specific in content and uniform in shape:

```json
{
  "step": "persisting",
  "attempt": 1,
  "updated_at": "2026-09-18T14:03:22Z",
  "cursor": { … },
  "completed_units": ["greenhouse", "lever"],
  "failed_units": { "workday": "http_503" },
  "pending_units": ["ashby"],
  "counters": { "fetched": 84, "inserted": 31, "duplicates": 12 },
  "pending_event_ids": ["6f1c…"],
  "resume_token": "…"
}
```

Requirements per pipeline:

| Pipeline | Checkpoint after | `cursor` | Re-execution rule |
|---|---|---|---|
| `discovery` | every source, every persisted batch | per-source page/offset | skip `completed_units`; re-fetch only `pending_units`; duplicates are counted, not errors ([05 §5](05-normalized-job.md)) |
| `extraction` | text extracted, layout extracted, model answered, guardrails run, provenance written | `step` | a completed step is not repeated; the model answer is cached in `checkpoint` until the row is written |
| `application` | resume chosen, form detected, fields mapped, plan built, browser launched, form filled, submitted, receipt recorded | `step` + `submission_token_hash` | **never re-submit**: a `submitted` receipt aborts the run ([07 §7](07-application-state-machine.md)) |
| `email` | recipient resolved, draft written, compliance checked, handed to SMTP, bounce/open recorded | `step` | a send with a recorded `message_id` is not repeated |
| `funding` | per provider, per persisted company | provider cursor | upsert by `(user_id, name_normalized)` |
| `ai` | per task | — | tasks are small; the checkpoint is the event-id dedupe |

Three global rules:

1. **Write the checkpoint before the side effect it authorises, or in the same
   transaction as it.** A checkpoint claiming work that did not happen causes
   lost work; a side effect with no checkpoint causes duplicate work. Where the
   two cannot be one transaction (a browser submission), the sequence is: mint the
   token → checkpoint it → act → record the receipt.
2. **Side effects are created after the expensive step succeeds, or upserted by a
   unique key.** This is the shipped invariant that makes `pause → resume` free
   (see the `job_queue.pause` docstring) and it is a hard requirement for new
   handlers.
3. **`pending_event_ids` makes event writing idempotent** across a retry
   ([09 §4](09-application-events.md)).

---

## 6. Trigger receipt (the shape every async endpoint returns)

```json
{
  "queued": true,
  "duplicate": false,
  "pipeline_job_id": 918,
  "queue_status": "queued",
  "idempotency_key": "3f2a…",
  "dedupe_key": "apply:412",
  "correlation_id": "8f1c…",
  "entity": { "type": "application", "id": 77 },
  "poll": { "endpoint": "/api/queues/ai", "interval_ms": 4000 },
  "message": "Application queued — the status below updates live and survives a refresh."
}
```

Rules:

1. Every endpoint that starts long-running work returns **202** with this
   receipt. (Shipped endpoints return 200 with a subset; they gain the missing
   keys additively — no key is removed.)
2. `duplicate: true` still returns the live `pipeline_job_id` — the shipped
   v2.2.8 fix, and the reason a double click shows status instead of nothing.
3. `poll` tells the client where to look and how often, so cadence is a contract
   rather than a per-page constant.
4. `entity` lets a page find its row after a refresh without `sessionStorage`.
5. Synchronous, cheap, user-fixable failures stay synchronous and return their
   real status (`400 profile_missing`, `422 no_description`, `402
   upgrade_required`) — a click should hear about those immediately (the shipped
   `POST /api/resumes/generate` rule).

---

## 7. Live read and polling discipline

`GET /api/queues/ai` (shipped) returns
`{counts, items[], recent[], processing_now, server_time}` with rows serialised by
`serialize_item()`:

```json
{
  "id": 918, "pipeline": "application", "task": null, "job_id": 412,
  "status": "needs_input", "priority": 1,
  "attempts": 0, "max_attempts": 3, "reclaim_count": 0, "paused_count": 0,
  "error": "", "error_code": null,
  "created_at": "…", "updated_at": "…", "scheduled_at": "…", "finished_at": null,
  "progress": { … }, "payload": { … }, "result": { … }
}
```

Contract for consumers:

| Rule | Detail |
|---|---|
| Cadence | 4 s for the live queue (shipped `AI_QUEUE_POLL_MS`), 20 s for the Queues page, 25–30 s for other polling pages. A new page picks from this list; it does not invent a number. |
| Visibility | poll only while the tab is visible; re-read immediately on `visibilitychange` → visible; clear timers and listeners on unmount (the shipped `useAIQueue`). |
| Failure | a failed read keeps the last snapshot and marks it `stale`; it never zeroes the counters. |
| Derivation | UI state is a pure function of the row (`deriveApplyState`, `deriveDiscoveryState`, …). Nothing is remembered between renders. |
| Heavy payloads | `serialize_item` strips `context`/`board_tokens` from list reads; `GET /api/pipelines/jobs/{id}` returns the full row for one item. |
| `result` | the handler's return value; **the outcome lives here**, not in `status` (§2). |

---

## 8. Budgets (three, independent — shipped, pinned)

| Budget | Column | Cap | Counts | Exhaustion |
|---|---|---|---|---|
| failures | `attempts` | `max_attempts` (default 3) | handler exceptions only | `dead` |
| outages | `paused_count` | `AI_PAUSE_MAX` (12) | transient AI pauses | `dead` |
| crash reclaims | `reclaim_count` | `worker_max_reclaims` (5) | lease expiries | `dead` |

Claiming, pausing and recovering never touch `attempts`. Mixing these budgets is
the v2.2.2 bug; the contract is the fix, stated so it cannot be re-broken.

---

## 9. Retention, ops and metrics

* Terminal rows are kept `queue_retention_days` (default 30) and then pruned,
  **except** rows referenced by an `application_events`/`discovery_runs` row.
* `payload` may be compacted after `RECENT_FINISHED_WINDOW` (drop large context
  blobs, keep `result`), because the live feed reads it.
* Owner-only ops: `GET /api/ops/queue/*`, retry-dead (shipped), cancel-any,
  re-recover stalled.
* Metrics keep the shipped names (`jobhunter_queue_enqueued_total`,
  `_completed_total`, `_retries_total`, `_paused_total`, `_resumed_total`,
  `_dead_total`, `_recovered_total`, `_reclaimed_total`, `_depth`) and add
  `_cancelled_total` and `_checkpoint_seconds` (histogram, per pipeline). Every
  counter is labelled with `pipeline` and never with a tenant identifier.

---

## 10. Invariants (tests)

1. A claim never increments `attempts`; a failure always does, before the budget
   check (shipped `test_queue_attempts_semantics.py` — extended to cover
   `paused_count` as a column).
2. Two workers claiming the same row: exactly one gets it (CAS rowcount).
3. `recover_stalled` with a live-but-slow job (lease expired by less than the
   safety margin) does nothing.
4. Re-running a terminal row resets all three budgets; re-running a live row is a
   no-op that returns the row.
5. `enqueue` with the same `dedupe_key` while live → `duplicate: true` and the
   same `pipeline_job_id`.
6. `enqueue` with the same `idempotency_key` and a different payload →
   `409 idempotency_conflict`; with the same payload → the stored response.
7. A handler that raises after a checkpoint resumes from it: the completed units
   are not repeated (asserted with a fake source that counts calls).
8. `progress.percent` never decreases within one attempt.
9. Cancelling a `processing` row does not interrupt an irreversible step; the row
   ends `cancelled` only after the step completes, or `done` if the step finished
   the work.
10. Every row is tenant-scoped in every query; `live_view` for user A never
    contains a row of user B (extend `test_auth_and_tenancy.py`).

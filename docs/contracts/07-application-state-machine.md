# 07 — Application state machine

**Deliverable 6.** The application becomes a first-class entity with an explicit
lifecycle. Today the same information is smeared across four places:
`jobs.status`, `jobs.extra` (`forms`, `autofill_plan`, `autofill_result`,
`resume_decision`, `answers`), `pipeline_jobs` (execution), and
`user_input_requests` (the block). Nothing can answer *"how many applications did
we submit last week, through which portal, with which resume, and what happened
next?"* without reading a JSON blob per job.

The shipped behaviour is preserved: `jobs.status` stays, as a **projection**
(§5), and `POST /api/jobs/{id}/apply` keeps its receipt shape
([11 §3](11-background-jobs.md)).

---

## 1. Entities

```
applications                 one row per (job, candidate) attempt at a relationship
  ├── application_attempts   one row per execution (prepare/submit) — retries do not reuse
  │      └── pipeline_jobs   the durable work item for that attempt        [11]
  ├── application_field_answers   every answer we hold, with ambiguity + reuse scope [08]
  ├── application_events          append-only timeline                      [09]
  └── resume_documents            what was attached                          [03]
```

Why attempts are separate: a submission is at-most-once, but *preparation* is
repeatable, and an application that failed on Tuesday and succeeded on Thursday
must not look like one execution. The attempt row carries the lease, the
checkpoint, the receipt and the screenshot reference; the application row carries
the relationship and the outcome.

---

## 2. `applications`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id` | FK, index | no | tenant |
| `job_id` | FK `jobs.id`, index | no | |
| `persona_id` | FK `personas.id` | yes | |
| `state` | String(28) | no | `APPLICATION_STATES` |
| `phase` | String(16) | no | `APPLICATION_STATE_PHASE[state]` — stored so a board can group by phase without a map lookup |
| `attempt_count` | Integer | no | how many attempts have run |
| `current_attempt_id` | FK `application_attempts.id` | yes | |
| `blocked_code` | String(40) | yes | `APPLICATION_BLOCKED_CODES` — set exactly when `phase = blocked` |
| `blocked_detail` | JSON | no | `{message, fields[], consent, credential_id, policy_id, retry_after}` |
| `is_provisional` | Boolean | no | `true` when the state came from an inference (a parsed recruiter email) and awaits confirmation |
| `provisional_evidence` | JSON | no | `Provenance` for the inference |
| `resume_document_id` | FK `resume_documents.id` | yes | what was (or would be) attached |
| `resume_decision` | String(20) | yes | `master` \| `selected` \| `reused` \| `generated` \| `polished` (the shipped `choose_resume` outcomes) |
| `portal_type` | String(30) | yes | `greenhouse`, `lever`, `workday`, … `custom` |
| `portal_domain` | String(200) | yes | the domain automation is allowed to act on |
| `submission_channel` | String(20) | yes | `SUBMISSION_CHANNELS` |
| `submission_token` | String(64) | yes | server-minted at-most-once token (§7) |
| `external_receipt_id` | String(200) | yes | the portal's own confirmation id/reference, when it gave one |
| `policy_id` | FK `automation_policies.id` | yes | the policy in force when the run started ([10](10-automation-policy.md)) |
| `consent_snapshot` | JSON | no | `{automation: iso\|null, outreach: iso\|null, terms: iso, data_processing: iso, disclosure_version}` — frozen at submission, because consent can be withdrawn later and the record must say what was true *then* |
| `fields_mapped` / `fields_total` | Integer | no | from the plan |
| `missing_required_count` | Integer | no | |
| `ambiguous_count` | Integer | no | how many answers are not `resolved` ([08](08-application-fields.md)) |
| `dry_run` | Boolean | no | `true` when nothing was sent to the portal |
| `last_error_code` / `last_error` | | yes | |
| `queued_at`, `prepared_at`, `approved_at`, `submitted_at`, `closed_at` | DateTime | yes | business timestamps — never inferred from `updated_at` |
| `outcome_at` | DateTime | yes | when a tracking state was recorded |
| `expires_at` | DateTime | yes | when an un-submitted application goes `expired` |
| `created_at` / `updated_at` | DateTime | no/yes | |

Constraints: **partial unique** `UNIQUE(user_id, job_id) WHERE state NOT IN
(TERMINAL)` — one live application per job (PostgreSQL partial index; on SQLite
the writer enforces it inside the transaction that creates the row, which is the
pattern `erasure.py` already relies on for FK enforcement differences);
`INDEX(user_id, state, updated_at)`; `INDEX(user_id, persona_id, phase)`;
`INDEX(user_id, submitted_at)`.

---

## 3. `application_attempts`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id`, `application_id` | FK, index | no | |
| `attempt` | Integer | no | 1-based; `UNIQUE(user_id, application_id, attempt)` |
| `kind` | String(16) | no | `prepare` \| `execute` \| `resubmit` \| `verify` |
| `mode` | String(16) | no | the shipped payload `mode`: `prepare` \| `execute` |
| `state` | String(28) | no | the application state this attempt ended in |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | |
| `idempotency_key` | String(64) | yes | |
| `dedupe_key` | String(300) | no | `apply:{job_id}` for prepare, `submit:{application_id}:{attempt}` for execute |
| `plan` | JSON | no | the autofill plan (`build_autofill_plan` output) — immutable per attempt |
| `form_schema` | JSON | no | the detected form schema, with `detection_source` and `confidence` |
| `result` | JSON | no | the execution result (`execute_autofill` output) |
| `evidence` | JSON | no | `[{kind: "screenshot"\|"portal_response"\|"email", locator, sha256, captured_at}]` |
| `credential_id` | FK `vault_entries.id` | yes | which vault entry was used — the **id**, never the secret |
| `trigger` | String(16) | no | `QUEUE_TRIGGERS` |
| `started_at` / `finished_at` | DateTime | yes | |
| `created_at` | DateTime | no | |

A new attempt is created by: a retry after `failed`, a re-run after the user
answers a blocked field **when the previous attempt already executed** (a
`needs_input` attempt that never ran the browser is resumed in place), and an
explicit resubmission after `submitted_unverified`.

---

## 4. The state machine

`phase` groups states; the table lists every legal transition. **Actor** is
mandatory — a transition nobody initiated does not exist.

### 4.1 Intake → preparation

| # | From | To | Actor / trigger | Guard | Side effects | Event | Audit |
|---|---|---|---|---|---|---|---|
| A1 | *(none)* | `queued` | `user` — `POST /api/jobs/{id}/apply`; or `system_scheduler` — auto mode `application_prep` | entitlements (`can_run_automation`, `applications_per_month`, `automation_runs_per_month`); no live application for the job; profile exists | create `applications` + `application_attempts(attempt 1)`; enqueue `pipeline=application`; freeze `consent_snapshot` and `policy_id`; projection `jobs.status = queued` | `application.created`, `application.queued`, `job.enqueued` | `job.apply_queued` *(shipped)* |
| A2 | `queued` | `preparing` | `system_worker` — claim | lease taken | `started_at`; checkpoint `{step: choosing_resume}` | `application.preparation_started`, `job.claimed` | — |
| A3 | `preparing` | `preparing` | `system_worker` — resume chosen | `choose_resume` returned an id | `resume_document_id`, `resume_decision` | `application.resume_selected` | — |
| A4 | `preparing` | `preparing` | `system_worker` — form detected | `detection_source ∈ {html, ats_api, heuristic, unavailable}` | `form_schema` on the attempt; `portal_type`, `portal_domain` | `application.form_detected` | — |
| A5 | `preparing` | `preparing` | `system_worker` — fields mapped | plan built | `fields_mapped`, `fields_total`, `application_field_answers` rows written ([08](08-application-fields.md)) | `application.fields_mapped`, one `application.field_ambiguous` per non-`resolved` answer | — |

### 4.2 Blocked

All `blocked_*` states set `blocked_code`, `blocked_detail` and the projection
`jobs.status = needs_input`, create/refresh a `user_input_requests` row (shipped)
or the equivalent approval/consent ask, emit `application.blocked`, and notify
(`kind = action_required`). All are exited by exactly one actor.

| # | To | Actor / trigger | Guard | `blocked_code` | Exit transition |
|---|---|---|---|---|---|
| A6 | `blocked_input` | `system_worker` — a required field has no value or is ambiguous | ≥1 answer with `resolution = unresolved` | `missing_required_field`, `ambiguous_field`, `restricted_field_needs_choice` | A12 (user answers) |
| A7 | `blocked_resume_approval` | `system_worker` — the tailored resume is `pending_approval`, or the fact guard failed | resume state ∈ {`pending_approval`, `rejected`} | `resume_pending_approval`, `resume_fact_guard_failed` | A13 (approve) / A20 (cancel) |
| A8 | `blocked_consent` | `system_worker` or `system_api` at run time — the automation disclosure is not (or no longer) accepted | `consents.automation_accepted_at` missing | `consent_automation_missing` | A14 (consent granted) |
| A9 | `blocked_credential` | `system_worker` — the portal needs a login and the vault has no usable entry | `requires_login` and no decryptable entry | `credential_missing`, `credential_unreadable` | A15 (credential stored) |
| A10 | `blocked_policy` | `system_worker` — the automation policy forbids this run | company blocked, portal not allowed, score below floor, daily limit reached | `policy_company_blocked`, `portal_domain_not_allowed`, `policy_score_below_floor`, `policy_daily_limit` | A16 (policy changed / explicit override by the user) |
| A11 | `blocked_quota` | `system_api` — entitlement exhausted at run time | `enforce()` raises | `quota_exhausted` | A17 (period rollover / upgrade) |

Note A8's timing: an intent queued *before* a consent was withdrawn still runs,
but the consent chain is re-checked **at execution time** — that is the shipped
behaviour of `execute_application`, and the contract makes it explicit. Revoking
consent therefore stops a queued submission without cancelling the row: it lands
in `blocked_consent`.

### 4.3 Unblock

| # | From | To | Actor / trigger | Guard | Side effects | Event |
|---|---|---|---|---|---|---|
| A12 | `blocked_input` | `queued` | `user` — `POST /api/jobs/{id}/input` (shipped) or `POST /api/applications/{id}/answers` | every required answer now `resolution ≠ unresolved`; no `restricted` field left at `policy_default` without an explicit user choice | answers stored with `reuse_scope`; `user_input_requests.status = completed`; attempt resumed (same attempt when it never executed, new attempt when it did); `blocked_code` cleared | `application.input_provided`, `application.unblocked`, `job.enqueued` |
| A13 | `blocked_resume_approval` | `queued` | `user` — `POST /api/resumes/{id}/approve` | resume `approved`; fact guard passed | `resume_document_id` confirmed | `resume.approved`, `application.unblocked` |
| A14 | `blocked_consent` | `queued` | `user` — `POST /api/account/consent {automation: true}` | consent timestamp set | `consent_snapshot` refreshed | `consent.accepted`, `application.unblocked` |
| A15 | `blocked_credential` | `queued` | `user` — `POST /api/vault` | entry stored for `portal_domain` | `credential_id` on the attempt; **no secret in the event payload** | `application.credential_reused`, `application.unblocked` |
| A16 | `blocked_policy` | `queued` | `user` — policy update, or `POST /api/applications/{id}/override` with a reason | the specific blocker is lifted; an override is recorded with `actor = user` and a reason | new attempt | `automation.policy_changed`, `application.unblocked` |
| A17 | `blocked_quota` | `queued` | `system_scheduler` — period rollover; or `user` — upgrade | quota available | new attempt | `application.unblocked` |

### 4.4 Review → submission

| # | From | To | Actor / trigger | Guard | Side effects | Event | Audit |
|---|---|---|---|---|---|---|---|
| A18 | `preparing` | `prepared` | `system_worker` — plan complete, `mode = prepare` **or** automation cannot submit (Playwright absent, `AUTOFILL_ENABLED` false, `AUTOFILL_DRY_RUN` true) | no unresolved answers; resume approved | `dry_run = true`; `prepared_at`; projection `ready_to_apply`; the plan is downloadable/reviewable | `application.plan_ready`, `application.dry_run_completed` | `job.prepared` |
| A19 | `prepared` | `awaiting_approval` | `system_api` — the policy requires review before submit (`require_review_before_submit = true`, the default for `mode = prepare`) | — | notification `action_required` | `application.plan_ready` | — |
| A20 | `awaiting_approval` | `submitting` | `user` — `POST /api/applications/{id}/submit` | `mode = execute` resolved at run time: `AUTOFILL_ENABLED` ∧ ¬`AUTOFILL_DRY_RUN` ∧ user `allow_auto_submit` ∧ automation consent ∧ entitlement `can_use_autofill` | **mint `submission_token`** (§7); CAS `awaiting_approval → submitting`; new attempt `kind = execute` | `application.approved`, `application.submission_started` | `job.submit_approved` |
| A21 | `prepared` | `submitting` | `system_worker` — `mode = execute` and the policy allows auto-submit | as A20, evaluated at run time | as A20 | `application.submission_started` | `application.submitted_auto` |
| A22 | `submitting` | `submitted` | `external_portal` — the portal accepted the submission, observed by the worker | `result.status = submitted` ∧ `result.submitted = true` | `submitted_at`, `submission_channel = automation`, `external_receipt_id` when the portal gave one, evidence (screenshot sha256); projection `applied`; `jobs.applied_at`; usage charged | `application.submitted` | `application.submitted` *(shipped sensitive action)* |
| A23 | `submitting` | `submitted_unverified` | `system_worker` — the browser finished but there is **no** confirmation signal (no receipt id, no confirmation page marker, no confirmation email within `verify_window_minutes`) | evidence inconclusive | `is_provisional = true`; notification asks the user to confirm; a `verify` attempt may be queued | `application.submission_unverified` | `application.submitted_unverified` |
| A24 | `submitted_unverified` | `submitted` | `user` — `POST /api/applications/{id}/confirm` | — | `is_provisional = false`, `submission_channel` unchanged, `external_receipt_id` if the user pastes one | `application.outcome_recorded` | `job.applied` |
| A25 | `submitted_unverified` | `failed` | `user` — "it did not go through" | — | `last_error_code = submission_unconfirmed` | `application.submission_failed` | — |
| A26 | `submitting` | `failed` | `external_portal` / `system_worker` — navigation blocked, domain not allowed, autofill unavailable, portal error | — | `last_error`, evidence kept; projection `failed`; **retryable** → A27 | `application.submission_failed` | `job.apply_failed` |
| A27 | `failed` | `queued` | `user` — `POST /api/jobs/{id}/retry` (shipped) | attempts budget left; quota available | **new attempt**; `attempt_count += 1` | `application.retried`, `job.enqueued` | `job.apply_retried` |
| A28 | `prepared`, `awaiting_approval` | `submitted` | `user` — `POST /api/jobs/{id}/mark-applied` (shipped) | the user says they applied by hand | `submission_channel = manual_user`, `submitted_at`, `dry_run = true` stays (we did not send it) | `application.marked_applied_manually` | `job.applied` *(shipped)* |
| A29 | any non-terminal | `cancelled` | `user` — `POST /api/applications/{id}/cancel` | — | queue item `cancelled`; nothing further runs | `application.cancelled` | `application.cancelled` |
| A30 | any non-terminal except `submitting` | `expired` | `system_scheduler` — `expires_at` passed | not `submitting` (a submission in flight is never expired out from under the browser) | projection `skipped` | `application.expired` | — |

`submitting` is deliberately **not** cancellable: a half-typed portal form is
worse than a completed one. Cancellation during `submitting` is refused with
`409 application_not_submittable` and offered again once the attempt finishes.

### 4.5 Tracking (post-submission)

| # | From | To | Actor / trigger | Guard | `is_provisional` | Event |
|---|---|---|---|---|---|---|
| A31 | `submitted` | `interview_scheduled` | `user` — record outcome; or `system_worker` — an inbound email classified as an interview invite | for the inferred path: classifier confidence ≥ threshold **and** the message is linked to the application | `true` when inferred | `application.outcome_recorded` |
| A32 | `submitted`, `interview_scheduled` | `rejected_by_employer` | `user`; or inferred from a rejection email | as A31 | `true` when inferred | `application.outcome_recorded` |
| A33 | `submitted`, `interview_scheduled` | `offer_received` | `user` **only** | an offer is never inferred | `false` | `application.outcome_recorded` |
| A34 | `submitted`, `submitted_unverified` | `withdrawn` | `user` | — | `false` | `application.withdrawn` |
| A35 | any `is_provisional = true` state | same state, `is_provisional = false` | `user` — confirm | — | `false` | `application.outcome_recorded` |
| A36 | any `is_provisional = true` state | previous state | `user` — reject the inference | — | `false` | `application.outcome_recorded` |

An inferred outcome never silently replaces a user-stated one: `actor_type =
user` beats `system_worker`, and the provisional row keeps its
`provisional_evidence` so the user can see what we thought we read.

### 4.6 Terminal states

`APPLICATION_TERMINAL_STATES` = `rejected_by_employer`, `offer_received`,
`withdrawn`, `expired`, `skipped`, `cancelled`. `failed` is **not** terminal (it
is retryable); `submitted` is not terminal (tracking continues). Nothing leaves a
terminal state — a re-application to the same posting creates a new
`applications` row (allowed because the unique index is partial over non-terminal
states) with `previous_application_id` set.

---

## 5. Projections (backwards compatibility)

| Canonical | Legacy `jobs.status` | Legacy `JobEvent.stage` | Queue `status` |
|---|---|---|---|
| `not_started` | `discovered` | `discovered` | — |
| `queued` | `queued` | `queued` | `queued` |
| `preparing` | `preparing` | `prepared` / `preparing` | `processing` |
| `blocked_input` | `needs_input` | `needs_input` | `needs_input` |
| `blocked_resume_approval` | `needs_input` | `resume_generated` | `needs_input` |
| `blocked_consent` / `blocked_credential` / `blocked_policy` / `blocked_quota` | `needs_input` | `blocked` | `needs_input` |
| `prepared` / `awaiting_approval` | `ready_to_apply` | `ready_to_apply` | `done` |
| `submitting` | `preparing` | `applying` *(event only)* | `processing` |
| `submitted` / `submitted_unverified` | `applied` | `applied` | `done` |
| `interview_scheduled` / `offer_received` | `applied` | `outcome` | — |
| `rejected_by_employer` | `rejected` | `outcome` | — |
| `failed` | `failed` | `failed` | `failed` / `dead` |
| `withdrawn` / `expired` / `skipped` / `cancelled` | `skipped` | `skipped` | `cancelled` / `done` |

The projection is written **in the same transaction** as the state change, by the
single writer (`app/services/application_service.py`, proposed). `JOB_STATUS_PROJECTION`
in `app/contracts/vocabulary.py` is the mapping; `lib/contracts.ts` mirrors it.

Two shipped values are **not** in the projection because nothing has ever written
them: `applying` and `emailed`. They are documented here as *removed from the
vocabulary* rather than *reserved*, and the places that referenced them
(`models.Job.status` comment, `GET /api/analytics/funnel`, the Jobs page filter)
are corrected — see CHANGELOG 2.2.18.

---

## 6. Execution layer mapping

`applications.state` (business) vs `pipeline_jobs.status` (execution) are
different axes and must not be conflated:

| Queue status | What it says | Application states it can coexist with |
|---|---|---|
| `queued` | waiting for a worker | `queued`, `blocked_*` (after an unblock re-queues) |
| `processing` | a worker holds the lease | `preparing`, `submitting` |
| `paused` | an AI outage parked it | `preparing` (unchanged) |
| `needs_input` | the worker parked it for the user | `blocked_*` |
| `done` | the handler returned | `prepared`, `submitted`, `submitted_unverified`, `failed` *(a failed autofill is a successful handler run)* |
| `failed` / `dead` | the handler broke / budget exhausted | `failed` |
| `cancelled` | the user stopped it | `cancelled` |

The rule that falls out of this and must be respected by the UI: **a queue row
going `done` does not mean the application succeeded.** The outcome is
`result.status`, mapped through §5 — which is exactly what
`frontend/src/lib/aiWork.ts:deriveApplyState` already does.

---

## 7. Submission idempotency (at-most-once)

Submitting twice is the one irreversible mistake this system can make. Four
independent guards:

1. **CAS on the state.** `UPDATE applications SET state='submitting',
   submission_token=:token WHERE id=:id AND user_id=:uid AND state IN
   ('prepared','awaiting_approval')` — if `rowcount != 1`, somebody else is
   already submitting; return the existing row, do not launch a browser.
2. **`submission_token`.** Minted server-side (`secrets.token_urlsafe(24)`),
   unique, recorded before the browser runs, and sent to the worker in the queue
   payload. A worker that finds a token already `submitted` refuses to run.
3. **Receipt check.** Before submitting, the attempt re-reads the application:
   `state ∈ {submitted, submitted_unverified}` or `external_receipt_id` set ⇒
   abort with `409 application_already_submitted`. After submitting, the receipt
   is written in the same transaction as the state change.
4. **Dedupe key `submit:{application_id}:{attempt}`** on the queue row, so a
   double click, a retried HTTP request or a scheduler race cannot enqueue two
   submissions.

Plus the transport guard: the SPA sends `Idempotency-Key` on
`POST /api/applications/{id}/submit`, and a replay returns the stored response
with `Idempotency-Replayed: true` ([01 §8](01-conventions.md)).

**Crash semantics.** If the worker dies between "the browser pressed submit" and
"the receipt was written", the lease expires, the reaper re-queues, and the new
worker finds `state = submitting` with a live token. It must **not** re-submit.
It runs a `verify` attempt: reload the portal, look for a confirmation
("application received", reference id, confirmation email). Verified →
`submitted`; contradicted → `failed` (retryable); inconclusive →
`submitted_unverified` with `is_provisional = true` and a notification asking the
user. `recover_stalled`'s safety margin (`worker_reaper_safety_seconds`, default
300 s) exists precisely so this window is not entered while the original worker
is still alive.

---

## 8. Ownership, lifecycle, retention

| Entity | Single writer | Readers | Retention |
|---|---|---|---|
| `applications` | `app/services/application_service.py` (proposed; today `apply_flow.py` + the jobs router) | board, drawer, analytics, notifications, export | life of the account |
| `application_attempts` | same | audit, "why did it fail?", ops | life of the account; screenshots pruned after `screenshot_retention_days` |
| `application_field_answers` | same | the input queue, reuse at the next application | life of the account; `restricted` values encrypted |
| `application_events` | same (via `record_application_event`) | timeline UI, support, audit | append-only; pruned after `event_retention_days` except state-changing events |

---

## 9. Invariants (tests)

1. One live (non-terminal) application per `(user_id, job_id)`.
2. `phase == APPLICATION_STATE_PHASE[state]`, always.
3. `phase = blocked` ⇔ `blocked_code IS NOT NULL`; every other phase ⇒ `NULL`.
4. `submitted_at IS NOT NULL` ⇔ `state ∈ {submitted, submitted_unverified,
   interview_scheduled, rejected_by_employer, offer_received}`.
5. `submission_channel = automation` ⇒ `dry_run = false` ∧ `consent_snapshot.automation` set ∧ evidence present.
6. `state = submitting` has at most one concurrent attempt with a live lease.
7. No transition out of a terminal state; a second application to the same job is
   a new row with `previous_application_id`.
8. `jobs.status` always equals `job_status_for(applications.state)` for the live
   application (and `discovered` when there is none).
9. Every transition appends exactly one `application_events` row with the next
   `sequence`, the actor, and the audit action where §4 names one.
10. No event payload, audit detail or log line contains a credential, an OTP or a
    `restricted` field value ([01 §7](01-conventions.md)).
11. An inferred tracking state always has `is_provisional = true` and non-empty
    `provisional_evidence`.

---

## 10. The browser session state machine (v2.2.21)

An *assisted session* is the observable half of an application attempt: the
browser run that fills a form and stops for a human. It is its own table
(`application_sessions`) with its own state machine, because an application state
answers "where is this application?" while a session answers "what is the browser
doing right now, and who has to act next?".

```
created ──launching──▶ preparing ──▶ active ──▶ completed
   │            │                      │  ▲
   │            └──────────────┬───────┘  │ resume (checkpoint validated)
   ▼                           ▼          │
cancelled                awaiting_user / paused ──resuming──┘
   ▲                           ▲
   └──── expired ◀── TTL ──────┘        failed
```

| State | Phase | Meaning | Terminal |
|---|---|---|---|
| `created` | new | row exists, nothing opened | |
| `launching` | starting | browser/context is being created | |
| `preparing` | starting | page observed, plan/credential being resolved | |
| `active` | running | the pass may type (only `autofill` verdicts) | |
| `awaiting_user` | waiting | a human step is pending; a queue item exists | |
| `paused` | waiting | the user paused it themselves | |
| `resuming` | running | checkpoint validated, the next pass is starting | |
| `completed` | done | every fillable field is done — **without** a submit | yes |
| `expired` | expired | TTL passed; re-authentication required | yes |
| `failed` | failed | the portal could not be driven | yes |
| `cancelled` | cancelled | the user stopped the run | yes |

Transition rules that matter:

1. `created → awaiting_user` and `created → paused` are legal: the first page a
   session sees can legitimately be a sign-in or bot-check screen, i.e. a pause
   before any field exists.
2. `awaiting_user`/`paused → resuming` **only** through checkpoint validation
   (job, URL, employer, application identity, TTL); otherwise the session stays
   where it is and `last_checkpoint_failure` records the code.
3. `expired → resuming` is impossible: it goes through `reauthenticate`, which
   drops the persisted browser state, grants a fresh TTL and issues a new `login`
   handoff.
4. No transition out of a terminal state. A second attempt on the same job is a
   new session row, never a reopened one.
5. One live session per `(user, job)`; `BROWSER_SESSION_MAX_LIVE` caps how many
   one account may hold at once (`429 session_limit_reached` beyond it).
6. Every state change appends to `checkpoint.steps` (bounded by
   `MAX_SESSION_EVENTS`) and writes a `job_events` row, so the UI can show the
   run without reading page content.
7. `application_sessions.user_id` scopes every read and write: another tenant's
   session is `404`, never a merge, and the on-disk profile reference is resolved
   under the artifact root (a value that tries to escape it is refused).

## 11. The application tracking state machine (v2.2.22)

`jobs.status` is a queue column: it says what the *product* is doing about a job
right now. It cannot say what happened to the application. Tracking is a separate,
lightweight lifecycle layer — two tables, one vocabulary, no CRM:

| Table | Cardinality | Holds |
|---|---|---|
| `application_tracking` | one row per `(user_id, job_id)` | the current state, its phase, the first-occurrence timestamps, and the snapshots frozen when the record opened |
| `application_tracking_events` | append-only | every fact that ever happened: state changes, notes, follow-ups, corrections, snapshot refreshes |

Nothing is overwritten in place. The current state is a *projection* of the last
event; the timeline is the record of truth.

```
not_applied ──applied──▶ applied ──┬──▶ recruiter_response ─┐
     │                             │                        │
     └──────────────┐              └──▶ interview_scheduled ┤
                    │                        ▲   │  (self-loop = reschedule)
                    │                        │   ▼
                    │               interview_completed ───┤
                    │                        ▲   │  (self-loop = another round)
                    │                        └───┘         ▼
                    │                                offer_received
                    ▼                                      │
                withdrawn ◀────────────────────────────────┘
                    ▲
     rejected_by_employer   (terminal)
```

| State | Phase | `jobs.status` projection | Meaning | Terminal |
|---|---|---|---|---|
| `not_applied` | pre_application | `discovered` | a record exists before anything went out (a suggestion, a note) | |
| `applied` | applied | `applied` | the application is out — by the queue or by the person | |
| `recruiter_response` | in_conversation | `applied` | the employer answered; no interview yet | |
| `interview_scheduled` | interviewing | `applied` | a slot exists (a reschedule is a new event, not an edit) | |
| `interview_completed` | interviewing | `applied` | the interview happened; the self-assessment is the person's, not the product's | |
| `offer_received` | closed | `applied` | an offer is on the table | |
| `rejected_by_employer` | closed | `rejected` | the employer said no | yes |
| `withdrawn` | closed | `skipped` | the person stopped | yes |

### 11.1 Origin — who is claiming this

Every event, and the state it produced, carries an `state_origin`. The column is
the honesty mechanism: a fact the product saw is not the same fact as one the
person typed, and a guess read out of an inbox is neither.

| Origin | Means | May reach |
|---|---|---|
| `system_observed` | the queue ran and saw it (a submission landed, a board status moved) | any state except the four trusted-only ones |
| `user_reported` | the person said so, in the product | any state |
| `verified_integration` | a separate, working integration — a calendar connection, a portal API | any state |
| `email_inferred` | a heuristic read of an inbox | `recruiter_response` **only** |

`TRACKING_TRUSTED_ONLY_STATES` = `interview_scheduled`, `interview_completed`,
`offer_received`, `rejected_by_employer`. Reaching one of them with
`email_inferred` origin is refused with `422 origin_not_trusted`. **There is no
email-parsing path in this release** — the value exists so the refusal is
enforced by the vocabulary rather than by the absence of a caller, and so a future
integration has a truthful origin to write with instead of borrowing
`system_observed`. An interview is counted when somebody recorded one, never when
a subject line looked promising.

What an inbox heuristic *may* do is propose. `record_email_suggestion` stores an
`application.suggestion_recorded` event with `is_provisional = true`, keeps the
record's state where it was, and surfaces a confirm/dismiss pair in the UI
(`actions[]` keys `confirm_suggestion` / `dismiss_suggestion`). Confirming runs
the normal transition with `user_reported` origin — the fact becomes true because
a person said so, and the provisional evidence stays in the timeline as the
reason they were asked.

### 11.2 Transition rules that matter

1. The table above is the only source of legal moves
   (`TRACKING_TRANSITIONS`). Anything else is `409 invalid_state_transition`
   with the allowed list in the body — the UI renders the answer from the same
   list, so a button that cannot work is disabled rather than broken.
2. Self-loops are legal for `recruiter_response`, `interview_scheduled` and
   `interview_completed`: a second recruiter email, a reschedule and another
   interview round are all new facts, and each one appends.
3. `offer_received → withdrawn` is the only move out of an offer. Accepting an
   offer is out of scope for v1 (there is nothing to track after it), and
   declining is the person withdrawing.
4. Terminal states accept **no** transition — not even back to `applied`. Getting
   one wrong is a *correction*, which is rule 5.
5. A correction writes a new `application.state_corrected` event with
   `is_correction = true`, `corrected_event_id` pointing at the event being
   amended, and a reason of at least three characters (`422
   correction_reason_required`). The old event is never deleted or edited: the
   timeline shows what was believed and when it changed. A correction cannot
   reopen a terminal state — it records the truth about what already happened.
6. Replays are idempotent by fact, not by request. Each event carries a nullable
   `dedupe_key` naming the fact (`trk:{id}:{event_type}:{state}:{minute}`), and
   every write accepts an `idempotency_key` (kept per record, bounded at 64). A
   duplicate returns `200` with `duplicate: true` and an unchanged timeline —
   never `409`, and never a second row.
7. The dedupe hit is checked **before** transition validation, and the
   origin-trust check **before** it too. A retry of a terminal state therefore
   answers "already recorded", not "illegal move".
8. The projection into `jobs.status` never downgrades a status the queue owns
   (`queued`, `preparing`, `needs_input`): a tracker moving back to `not_applied`
   must not erase an automation mid-run. When the projection lands on `applied`
   it also carries `applied_at` if the job row has none — a board row that says
   "applied" with no date is invisible to every date filter.
9. Timestamps are first-occurrence columns: `applied_at`,
   `first_response_at`, `interview_scheduled_at`, `interview_completed_at`,
   `outcome_at` are written once and never rewritten by a later transition, so
   "days from applied to first interview" stays a real measurement even after a
   correction. `interview_at` is the exception — it is the *next* slot, so a
   reschedule moves it.
10. `application_tracking.user_id` scopes every read and write. Another tenant's
    record is `404`, never a merge. Both tables are in the deletion-integrity
    child list, so erasing an account erases its history too.

### 11.3 Snapshots — what the decision was made with

A record freezes what was true when it opened: `match_id`, `match_score`,
`match_band`, `match_explanation`, `artifact_kind`, `artifact_id`,
`artifact_version`, `artifact_label`. Both ids are plain integers, **not**
foreign keys: a match result or a packet can be deleted or re-scored later
without breaking the timeline, and the snapshot keeps answering "which version of
my resume got this interview?".

A snapshot can be refreshed explicitly (`POST …/{id}/snapshot`, no body), which
appends an `application.snapshot_recorded` event carrying the old and new
values. It is never refreshed implicitly — silently moving a score under a
historical record would make the report unanswerable.

### 11.4 Follow-ups and notifications

A follow-up is a date the person owes themselves: `follow_up_at`, an optional
note, and `follow_up_reason` (`after_application`, `after_interview`,
`after_rejection`, `user_defined`). The scheduler sweep turns due, un-notified
follow-ups into notifications of kind `application_follow_up` (see
`docs/contracts/12` §7), one per record per due date, and stamps `notified_at`.
Reading `GET /api/application-tracking/follow-ups` runs the same sweep, so the
agenda is never stale by more than the request that opened it. Marking a
follow-up done writes `follow_up_completed_at` and appends an event.

### 11.5 Reporting

`GET /api/application-tracking/report?group_by=…` groups by `source`, `role`,
`match_band`, `score_bucket`, `artifact_version`, `artifact_kind`, `channel` and
`state`. Rules:

- The denominator is `applied_at IS NOT NULL` — a record that never went out is
  not a failed application.
- `interviews` counts records that reached `interview_scheduled`,
  `interview_completed` or `offer_received`. `conversion_rate` is
  interviews ÷ applied, and is `null` (rendered `—`, never `0%`) when the group
  is empty.
- Every group carries `by_origin` — `{system_observed: n, user_reported: n,
  verified_integration: n}` — so a source that only ever produces
  user-reported interviews is visible as such.
- The window is `coalesce(applied_at, created_at)` between `from`/`to`, so a
  record opened today about an application sent last month still lands in the
  month it was sent.
- `GET /api/analytics/performance` reads the same timeline. The interview count
  there used to be a guess (`applied AND score >= 75`); it now reports
  `interview_data: "application_tracking"` and the frontend dropped the
  "(estimated)" label, because the number is recorded rather than modelled.

The report ships its own disclaimer: a match score is an estimated fit, not an
interview probability, and nothing in these numbers was inferred from email.

### 11.6 Invariants (tests)

`backend/tests/test_application_tracking.py` covers: every legal transition;
every illegal one (`409` + allowed list); corrections (event appended, reason
required, terminal states never reopened); duplicate events (idempotent replay,
`dedupe_key`, `idempotency_key`); origin trust (`email_inferred` refused for the
four states, allowed for `recruiter_response`); system-observed vs user-reported
bookkeeping; snapshot immutability and explicit refresh; follow-up creation,
sweep notification and completion; the projection rules including the
queue-owned-status guard; report grouping across all eight dimensions with
correct denominators and `null` rates; per-tenant isolation; and deletion
integrity for both tables.

### 11.7 Outcome reporting (v2.4)

`docs/REPORTING.md` is the methodology; this section is the contract other
implementations must satisfy.

**Reads.** `GET /api/analytics/outcomes` (a full report),
`GET /api/analytics/outcomes/weekly` (`last_7_days` | `this_week` only) and
`GET /api/analytics/outcomes/export` (`csv` | `json`). The dashboard's
`weekly_report` block (`GET /api/me/dashboard`) is the same calculation as
`/outcomes/weekly`, so the two surfaces cannot disagree about "this week".

**Windows.** Whole local calendar days in the account's timezone, half-open
(`start_utc ≤ t < end_utc`), echoed back with local dates and the boundary rule.
A date-only `until` means "through the end of that local day". Named ranges come
from `REPORT_RANGES`; an unknown range or timezone is a `422`, never a silent
fallback.

**Provenance.** Every metric carries `provenance ∈ REPORT_PROVENANCE_KINDS`:
`observed` (counted from a row), `user_reported` (the user asserted it),
`derived` (arithmetic) or `estimated` (a model's opinion — the match score). No
surface may render an `estimated` number as a counted fact.

**Sufficiency.** Every ratio carries a `sample` (`n`, `minimum`, `state`,
`label`) and is `null` when the sample is below `MIN_SAMPLE_SIZES`; the state is
one of `REPORT_SUFFICIENCY`. The thresholds ship in the response
(`definitions.minimum_sample_sizes`), so a reader can check the rule that gated a
number.

**Trend.** `REPORT_TRENDS` — `improving` | `declining` | `flat` |
`not_enough_data`, with `not_enough_data` distinct from `flat`. A direction
requires the minimum sample in both periods.

**Calibration.** `calibration.available` is false and
`calibration.interview_probability` is `null` until the recorded outcomes and a
registered calibrated model version satisfy the gate; `interview_probability()`
raises while they do not. A match score is never a probability.

**Privacy.** Every query is scoped by the authenticated identity; no endpoint
accepts a tenant parameter. `privacy.scope` is `self_only`, and export — whose
requirements are re-checked against the document at call time
(`export_privacy`) — writes an `analytics.report_exported` audit row before the
body is produced.

**Reproducibility.** `report_version` versions the calculations;
`calculation_id` hashes the inputs that can change the answer; group ordering is
deterministic. Identical inputs, identical output apart from `generated_at`.

**Invariants (tests).** `backend/tests/test_outcome_reporting.py` covers date
ranges and half-open boundaries, timezone day boundaries and DST, empty data,
minimum-sample labelling, observed-vs-estimated provenance, the calibration
refusal, the trend directions, the failure/correction/coverage metrics,
reproducibility and stable ordering, per-tenant scoping of both the report and
the export, and the export's privacy gate. The SPA side is pinned by
`frontend/src/lib/__tests__/reporting.test.ts` and
`frontend/src/components/__tests__/OutcomeReport.test.tsx`.

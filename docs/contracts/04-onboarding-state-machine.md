# 04 — Onboarding state machine

**Deliverable 3.** Nothing like this exists in the codebase today: the closest
things are `User.consents`, the `Profile` row created by an upload, and
`GET /api/auth/status.bootstrap_required`. Onboarding is therefore a new
aggregate — but it is a **derived** one, which is the whole design.

---

## 1. Design principle: the state is a function of the gates

The onboarding state is *not* a field somebody remembers to update. It is
computed from the gate checklist:

```
state = first incomplete BLOCKING gate's state, else
        first incomplete OPTIONAL gate's state, else
        "ready"
```

Consequences, all of which are requirements:

* **Resumable by construction.** A crash, a deploy, a second device or a support
  session all recompute the same state from stored facts. There is no
  half-advanced wizard to repair.
* **The browser cannot be the source of truth** ([01 §10](01-conventions.md)).
  The SPA reads `GET /api/onboarding/status` and renders it; it stores nothing.
* **Steps can be completed in any order** and out of order stays correct: the
  state names the *first* thing still owed, not the last thing done.
* **Nothing is silently skipped.** An optional gate is completed either by
  satisfying it or by an explicit `skipped` transition with `actor = user`.

`state` is persisted as well as derived — because a state change is an event
(`onboarding.state_changed`) and because "what did the user see on Tuesday" must
be answerable — but the persisted value is **always** recomputed and reconciled
on read. If they disagree, the derived value wins and the row is corrected in
the same request (and a warning is logged: the disagreement is a bug somewhere).

---

## 2. Gates

| # | `key` | Satisfied when | Blocking | Completing actor | Evidence stored |
|---|---|---|---|---|---|
| 1 | `account` | a `users` row exists and is active | yes | `system_api` (bootstrap/register) | `users.created_at` |
| 2 | `consents` | `consents.terms_accepted_at` **and** `consents.data_processing_accepted_at` are set | yes | `user` — `POST /api/account/consent` | consent timestamps + `audit_logs` (`consent.accepted`) |
| 3 | `resume` | a `resume_documents` row with `role = master`, `state ∈ {approved, pending_approval}` and a `resume_extractions` row in `succeeded` | yes | `user` uploads, `system_worker` extracts | `resume_document_id`, `extraction_id`, `pipeline_job_id` |
| 4 | `profile_review` | a `candidate_profiles` row in `active`, i.e. every `review_required` provenance row is `confirmed` \| `corrected` \| `rejected` \| `deferred`, and **no** `restricted` row is `deferred` | yes | `user` — `POST /api/profile/review/resolve` | `profile_id`, per-field `reviewed_at` |
| 5 | `preferences` | `preferences.target_roles` non-empty **and** at least one `preferences.locations` entry **and** `compensation.currency` set **and** `eligibility.work_authorization ≠ "unknown"` | yes | `user` — `PUT /api/onboarding/preferences` | `profile_id` version that carries them |
| 6 | `discovery_setup` | ≥1 source in `settings.scraping.sources`, **or** the user explicitly skipped | no | `user` — `PUT /api/settings` or `POST /api/onboarding/skip` | selected source ids |
| 7 | `automation_policy` | an `automation_policies` row exists with an explicit `mode` (default `suggest`, chosen not inherited) | no | `user` — `PUT /api/automation/policy` | `automation_policy_id`, `mode` |

Gate 3's two halves (upload, extraction) are why the state machine has an async
state: the user's part ends in a second, the system's part may take a minute, may
pause on an AI outage, and must survive both.

`eligibility.work_authorization` is `sensitive`, so gate 5 always produces at
least one field the user typed themselves — onboarding cannot be completed
entirely by accepting AI guesses. That is intentional.

---

## 3. States

`ONBOARDING_STATES`, in the order the derivation walks them:

| State | Meaning | Entered by | Blocking? |
|---|---|---|---|
| `not_started` | no session row | — | |
| `account_created` | user exists, nothing else done | gate 1 complete | yes |
| `consents_required` | terms/data-processing not accepted | gate 2 incomplete | yes |
| `awaiting_resume` | no master document yet | gate 3 incomplete, nothing in flight | yes |
| `resume_processing` | an extraction is queued/running/paused | gate 3 in flight (`pipeline_job_id` live) | yes |
| `extraction_blocked` | the extraction failed or was rejected | gate 3 blocked | yes |
| `profile_review_required` | fields need a human | gate 4 incomplete | yes |
| `preferences_required` | stated preferences missing | gate 5 incomplete | yes |
| `discovery_setup` | optional: sources not chosen | gate 6 incomplete | no |
| `automation_policy_optional` | optional: no explicit automation mode | gate 7 incomplete | no |
| `ready` | every blocking gate satisfied | derivation | terminal (success) |
| `abandoned` | the user (or an owner) paused onboarding explicitly | explicit transition | terminal (reversible) |

`resume_processing` and `extraction_blocked` are the same gate in two
situations, and they must not be collapsed: one says "wait", the other says "do
something". The SPA renders `extraction_blocked` with the reason and a Retry
button, `resume_processing` with the queue row's progress.

---

## 4. Transitions

Every row has an initiating actor or system event — there is no "the state
changes when it's done".

| # | From | To | Actor / trigger | Guard | Side effects | Event | Audit action |
|---|---|---|---|---|---|---|---|
| T1 | `not_started` | `account_created` | `system_api` — `POST /api/auth/{bootstrap,register}` | user row committed | session row created, gate 1 `completed` | `onboarding.started` | `auth.bootstrap` / `auth.register` (shipped) |
| T2 | `account_created` | `consents_required` | `system_api` — first `GET /api/onboarding/status` (reconciliation) | gate 2 incomplete | — | `onboarding.state_changed` | — |
| T3 | `consents_required` | `awaiting_resume` | `user` — `POST /api/account/consent` with `terms` + `data_processing` | both timestamps set | gate 2 `completed`; automation/outreach consents remain optional and are re-checked at use time ([10 §6](10-automation-policy.md)) | `onboarding.gate_completed`, `onboarding.state_changed` | `consent.accepted` (shipped) |
| T4 | `awaiting_resume` | `resume_processing` | `user` — `POST /api/resume/upload` | file stored, extraction row `pending`, queue item enqueued | gate 3 `in_progress`; `pipeline_job_id` recorded on the session | `resume.uploaded`, `extraction.started`, `onboarding.state_changed` | `resume.uploaded` (shipped) |
| T5 | `resume_processing` | `profile_review_required` | `system_worker` — extraction `succeeded` **and** ≥1 `review_required` field | profile version created in `draft`/`review_required` | gate 3 `completed`, gate 4 `in_progress`; notification `profile_review_required` | `extraction.succeeded`, `onboarding.gate_completed`, `onboarding.state_changed` | `extraction.succeeded` |
| T6 | `resume_processing` | `preferences_required` | `system_worker` — extraction `succeeded`, nothing needs review | profile version `active` (non-sensitive fields `auto_accepted`) | gates 3 **and** 4 `completed`; notification `onboarding_step_required` | `extraction.succeeded`, `profile.review_completed`, `onboarding.state_changed` | `extraction.succeeded` |
| T7 | `resume_processing` | `extraction_blocked` | `system_worker` — extraction `failed` \| `rejected`; or `system_watchdog` — `paused_count > AI_PAUSE_MAX` (dead-lettered) | queue item terminal (`dead`/`failed`) or extraction `rejected` | gate 3 `blocked` with `blocked` = `{code, message, retryable, retry_after}`; notification `onboarding_step_required` (`severity = error`) | `extraction.failed` \| `extraction.rejected_by_guardrail`, `onboarding.gate_blocked` | `extraction.failed` |
| T8 | `extraction_blocked` | `resume_processing` | `user` — `POST /api/onboarding/retry` (or a new upload) | new extraction attempt (`attempt + 1`), new queue item | gate 3 `in_progress`; `blocked` cleared | `extraction.started`, `onboarding.state_changed` | `onboarding.retried` |
| T9 | `extraction_blocked` | `awaiting_resume` | `user` — `DELETE /api/resume-documents/{id}` (remove the unparseable file) | no live extraction | gate 3 `pending` | `resume.deleted`, `onboarding.state_changed` | `resume.deleted` (shipped) |
| T10 | `profile_review_required` | `preferences_required` | `user` — `POST /api/profile/review/resolve` | §2 gate-4 condition | gate 4 `completed`; profile `active`; stale match results flagged `profile_changed` | `profile.review_completed`, `onboarding.gate_completed`, `onboarding.state_changed` | `profile.review_completed` |
| T11 | `preferences_required` | `discovery_setup` | `user` — `PUT /api/onboarding/preferences` | §2 gate-5 condition; creates a new profile version when values change | gate 5 `completed`; search context invalidated (`invalidate_context`) | `profile.field_confirmed`×n, `onboarding.gate_completed`, `onboarding.state_changed` | `profile.updated` |
| T12 | `discovery_setup` | `automation_policy_optional` | `user` — `PUT /api/settings` (≥1 source) **or** `POST /api/onboarding/skip {gate:"discovery_setup"}` | optional gates accept `skipped` | gate 6 `completed` \| `skipped` | `onboarding.gate_completed` \| `onboarding.gate_skipped` | `onboarding.gate_skipped` |
| T13 | `automation_policy_optional` | `ready` | `user` — `PUT /api/automation/policy` **or** `POST /api/onboarding/skip {gate:"automation_policy"}` | a policy row exists with an explicit `mode`, or skipped (default `suggest` recorded) | gate 7 `completed` \| `skipped`; `completed_at` set; notification `onboarding.completed` | `onboarding.gate_completed`, `onboarding.completed` | `onboarding.completed` |
| T14 | `discovery_setup` | `ready` | `system_api` — reconciliation when gate 7 was completed earlier out of order | all blocking gates done, no optional gate incomplete | `completed_at` set | `onboarding.completed` | `onboarding.completed` |
| T15 | any non-terminal | `abandoned` | `user` — `POST /api/onboarding/abandon`; or `owner` — `POST /api/ops/onboarding/{user_id}/abandon` | — | `abandoned_at`; auto-mode scheduler ignores the user; queued onboarding items cancelled | `onboarding.abandoned` | `onboarding.abandoned` |
| T16 | `abandoned` | *(recomputed)* | `user` — `POST /api/onboarding/resume` | — | `abandoned_at` cleared; state re-derived from the gates (it lands wherever the user actually got to) | `onboarding.resumed` | `onboarding.resumed` |
| T17 | `ready` | *(unchanged)* | any actor | onboarding is complete; further profile edits are ordinary profile updates, not onboarding | gate rows keep their `completed_at`; a later profile edit does **not** reopen onboarding | — | — |

Forbidden transitions: anything out of `ready` except T15-equivalent abandonment
by an `owner` (a completed onboarding is not undone by editing a profile), and
any transition initiated by `external_*` actors — onboarding is between the user
and us.

---

## 5. `onboarding_sessions`

One row per user (not per attempt).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK, `UNIQUE` | no | tenant; one session per account |
| `state` | String(32) | no | `ONBOARDING_STATES` — persisted projection of the derivation |
| `state_since` | DateTime | no | when the current state was entered |
| `checklist` | JSON | no | `{gate_key: {status, required, blocking, completed_at, skipped_at, blocked_at, evidence{}, blocked{}}}` — `status ∈ ONBOARDING_GATE_STATES` |
| `profile_id` | FK `candidate_profiles.id` | yes | the version being reviewed/used |
| `resume_document_id` | FK `resume_documents.id` | yes | |
| `extraction_id` | FK `resume_extractions.id` | yes | |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | the in-flight async step, so the SPA can attach to the live row |
| `automation_policy_id` | FK `automation_policies.id` | yes | |
| `blocked` | JSON | no | `{code, message, retryable, retry_after, since}` — `{}` when not blocked |
| `progress` | JSON | no | `{gates_required, gates_completed, gates_skipped, percent}` — `percent` is integer, monotonic within a state |
| `version` | Integer | no | optimistic concurrency: every writer does `UPDATE … WHERE id=? AND version=?` and retries the derivation on conflict |
| `started_at` | DateTime | no | |
| `completed_at` | DateTime | yes | when `ready` was first reached (never rewritten) |
| `abandoned_at` | DateTime | yes | |
| `updated_at` | DateTime | no | |

## 6. `onboarding_events`

Append-only, same shape as `application_events` ([09](09-application-events.md))
with `aggregate = "onboarding"`. It exists because onboarding is the one journey
support will be asked about ("the user says they uploaded a resume on Tuesday and
nothing happened"), and `audit_logs` records *security* actions, not progress.

| Column | Type | Meaning |
|---|---|---|
| `id`, `user_id`, `session_id` | | tenant-scoped |
| `sequence` | Integer | monotonic per session — ordering that does not depend on a second-granularity timestamp |
| `event_type` | String(60) | `EVENT_TYPES` (`onboarding.*`, `extraction.*`, `profile.*`, `resume.*`) |
| `gate` | String(32), nullable | which gate the event belongs to |
| `state_from` / `state_to` | String(32), nullable | for `onboarding.state_changed` |
| `actor_type`, `actor_id`, `actor_label` | | who initiated it |
| `pipeline_job_id` | Integer, nullable | the durable work item, when there is one |
| `request_id` | String(64) | correlates with the HTTP request and the logs |
| `payload` | JSON | event-specific, typed in [09](09-application-events.md) |
| `severity` | String(20) | `NOTIFICATION_SEVERITIES` |
| `message` | String(400) | human sentence |
| `occurred_at`, `recorded_at` | DateTime | when it happened / when we stored it |

`UNIQUE(user_id, session_id, sequence)`, `INDEX(user_id, occurred_at)`.

---

## 7. Blocked codes

`blocked.code` is from this list (never free text), and each has exactly one fix:

| Code | Cause | `retryable` | Fix the SPA offers |
|---|---|---|---|
| `no_readable_text` | scanned image / empty PDF | no | "Upload a text PDF or DOCX" + re-upload |
| `unsupported_media_type` | wrong file type | no | re-upload |
| `payload_too_large` | over `MAX_UPLOAD_MB` | no | re-upload smaller |
| `ai_unavailable` | provider unreachable, breaker open | yes | "Retry now" + `retry_after` |
| `ai_blocked` | no key / bad key / billing | no | deep link to Settings → AI |
| `guardrail_failed` | the model's answer was refused | no (with the same inputs) | show `issues[]`, offer re-upload |
| `fact_guard_failed` | extracted facts not present in the source text | no | show `violations[]` |
| `profile_missing` | extraction produced nothing usable | no | re-upload |
| `consent_required` | a required disclosure was withdrawn mid-flow | no | deep link to `/account?tab=consents` |
| `quota_exhausted` | plan limit on resume parses | no | upgrade hint (`402`) |

A blocked state is **not** a failure of the session: the checklist keeps every
completed gate, and unblocking resumes from there.

---

## 8. API

Full shapes in [13 §1](13-api-response-shapes.md):

* `GET /api/onboarding/status` — state, gates, blocked, progress, next action.
* `POST /api/onboarding/retry` — re-run the blocked async gate.
* `POST /api/onboarding/skip` — complete an optional gate with `skipped`.
* `PUT /api/onboarding/preferences` — gate 5.
* `POST /api/onboarding/abandon` / `POST /api/onboarding/resume` — T15/T16.

Every one of them returns the *whole* status document after applying the change,
so the client never has to merge a partial answer into its own idea of the state.

---

## 9. Invariants (tests)

1. `state` equals the derivation over `checklist` on every read.
2. Exactly one session row per user; `completed_at` is written once.
3. No gate is `completed` without its evidence fields set.
4. `ready` requires gates 1–5 `completed`; gates 6–7 are `completed` or `skipped`.
5. A `skipped` transition always has `actor_type = user` — the system never skips
   on the user's behalf.
6. `resume_processing` requires a live `pipeline_job_id`
   (`QUEUE_LIVE_STATUSES`); when that row goes terminal without a matching
   transition, the reconciliation on read fixes the state (this is the crash
   path, and it is why T7 exists).
7. Cross-tenant reads are `404`; `owner`-only transitions are refused for
   `member` with `403 forbidden`.
8. Every transition appends exactly one `onboarding_events` row with the next
   `sequence`.

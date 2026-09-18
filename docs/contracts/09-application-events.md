# 09 — Application event schema

**Deliverable 7.** The append-only timeline of one application: what happened, to
which state it moved, who made it happen, and what proves it.

Shipped today: `job_events` (`stage`, `status`, `message`, `meta`) written by
`app/services/events.record_job_event`, served by
`GET /api/jobs/{id}/events`. It is a good log and a weak contract — `stage` is
free text, `meta` is untyped, there is no actor, no sequence, no correlation to
the queue row, and no state transition recorded. `application_events` keeps the
same table shape and adds the five things a timeline needs.

---

## 1. Relationship to the other event tables

| Table | Scope | Purpose | Retention |
|---|---|---|---|
| `application_events` | one application | the product timeline the user reads | prunable after `event_retention_days`, except state-changing rows |
| `job_events` *(shipped)* | one job | board-level log; kept, and written **in addition** for one minor version so nothing that reads it breaks | as today |
| `domain_events` ([12](12-events-and-notifications.md)) | tenant | the integration/audit bus: notifications, analytics, webhooks | as today |
| `audit_logs` *(shipped)* | tenant | security-relevant actions, append-only, survives erasure detached | as today |

One application transition writes **one** `application_events` row, and — via the
same call — one `domain_events` row and, when the action is in
`audit.SENSITIVE_ACTIONS`, one `audit_logs` row. Three rows, one transaction, one
`event_id` shared by all three so they can be correlated.

---

## 2. `application_events`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `event_id` | String(36) | no | UUIDv4 — the correlation id shared with `domain_events`/`audit_logs`; `UNIQUE(user_id, event_id)` makes a retry of the writer idempotent |
| `user_id` | FK, index | no | tenant |
| `application_id` | FK, index | no | |
| `job_id` | FK `jobs.id` | yes | denormalised: the timeline must survive the application row and be queryable per job |
| `attempt_id` | FK `application_attempts.id` | yes | |
| `sequence` | Integer | no | monotonic per application, gapless; `UNIQUE(user_id, application_id, sequence)` |
| `event_type` | String(60) | no | `EVENT_TYPES`, `application.*` |
| `state_from` | String(28) | yes | `APPLICATION_STATES` |
| `state_to` | String(28) | yes | `APPLICATION_STATES`; `null` for non-transition events (`fields_mapped`) |
| `phase` | String(16) | yes | `APPLICATION_STATE_PHASE[state_to]` |
| `actor_type` | String(20) | no | `ACTOR_TYPES` — **never null, never "unknown"** |
| `actor_id` | Integer | yes | `users.id` when the actor is a person |
| `actor_label` | String(320) | no | `user.email` or `system`/`worker:<id>` — the shipped `audit.actor` convention |
| `trigger` | String(16) | no | `QUEUE_TRIGGERS` — was this a click, a schedule, a retry, a recovery? |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | the queue row executing it |
| `request_id` | String(64) | yes | the HTTP request id, when the actor went through the API |
| `idempotency_key` | String(64) | yes | the client's key, when there was one |
| `severity` | String(20) | no | `NOTIFICATION_SEVERITIES` — drives the chip colour, as `job_events.status` does today |
| `message` | String(400) | no | one human sentence, already localised-neutral (no ids the user cannot see) |
| `payload` | JSON | no | typed per event type (§3) |
| `evidence` | JSON | no | `[{kind, locator, sha256, captured_at}]` — screenshot, portal response, email id |
| `occurred_at` | DateTime | no | when it happened |
| `recorded_at` | DateTime | no | when we stored it (differs for a recovered/backfilled event) |

Indexes: `INDEX(user_id, application_id, sequence)`, `INDEX(user_id, job_id,
occurred_at)`, `INDEX(user_id, event_type, occurred_at)` (analytics: "how many
submissions failed on a domain block last week?").

**Append-only.** No update, no delete, except erasure (which keeps the row and
removes `payload` values for `restricted` fields).

---

## 3. Typed payloads

`payload` is not a free-form dict: each event type has a schema, and a writer that
adds a key adds it here. Unknown keys must be ignored by readers.

| `event_type` | `payload` |
|---|---|
| `application.created` | `{job_id, title, company, portal_type, source, persona_id, mode, trigger}` |
| `application.queued` | `{pipeline_job_id, dedupe_key, priority, mode, idempotency_key, duplicate}` |
| `application.preparation_started` | `{pipeline_job_id, attempt, checkpoint:{step}}` |
| `application.resume_selected` | `{resume_document_id, resume_decision, display_name, jd_similarity, reused_from_job_id, fact_guard:{passed, violations[]}}` |
| `application.form_detected` | `{portal_type, portal_domain, requires_login, detection_source, confidence, field_count, ai_used}` |
| `application.fields_mapped` | `{fields_total, fields_mapped, missing_required_count, ambiguous_count, by_resolution:{profile: n, unresolved: n, …}}` |
| `application.field_ambiguous` | `{field_key, field_label, ambiguity, ambiguity_detail, candidates:[{value, label, band, origin}], required}` |
| `application.input_requested` | `{input_request_id, fields:[{field_key, field_label, field_type, required, ambiguity, sensitivity}], blocking}` |
| `application.input_provided` | `{input_request_id, fields:[field_key], resolutions:{field_key: resolution}, actor:"user"}` — **names, never values** |
| `application.credential_created` | `{credential_id, domain, origin:"auto", username_masked}` |
| `application.credential_reused` | `{credential_id, domain, last_used_at}` |
| `application.plan_ready` | `{portal_type, fields_total, fields_mapped, dry_run, mode, review_required, screenshot:{locator, sha256}}` |
| `application.approved` | `{approved_by:"user", submission_token_present:true, consent_snapshot:{automation, terms, data_processing}}` |
| `application.rejected_by_user` | `{reason, resume_document_id}` |
| `application.blocked` | `{blocked_code, message, fields[], consent, credential_id, policy_id, retry_after, input_request_id}` |
| `application.unblocked` | `{blocked_code, resolved_by, resolution}` |
| `application.submission_started` | `{attempt, submission_token_hash, portal_domain, expected_domains[], credential_id, dry_run}` |
| `application.submitted` | `{submitted_at, submission_channel, external_receipt_id, fields_filled, evidence:[{kind:"screenshot", sha256}], confirmation_signal}` |
| `application.submission_unverified` | `{reason, checked:[confirmation_page, receipt_id, confirmation_email], verify_window_minutes, verify_attempt_id}` |
| `application.submission_failed` | `{error_code, error, stage, portal_domain, screenshot:{sha256}, retryable}` |
| `application.dry_run_completed` | `{fields_filled, fields_total, screenshot:{locator, sha256}, reason:"autofill_disabled"\|"playwright_missing"\|"dry_run_config"}` |
| `application.marked_applied_manually` | `{note_present:true, submission_channel:"manual_user"}` — the note itself is stored on the application, not in the event |
| `application.outcome_recorded` | `{outcome, origin:"user"\|"email_inferred", is_provisional, evidence:[{kind:"email_message", locator}], confidence}` |
| `application.withdrawn` | `{reason, by:"user"}` |
| `application.expired` | `{expires_at, days_idle}` |
| `application.cancelled` | `{by:"user", was_state, pipeline_job_id}` |
| `application.retried` | `{previous_attempt, new_attempt, previous_error_code, idempotency_key}` |
| `application.dead_lettered` | `{attempts, max_attempts, reclaim_count, paused_count, error_code, error}` |

Payload rules:

1. **No secrets, no `restricted` values, no free-text answers.** Field *names*,
   counts, hashes and ids. `application.input_provided` lists
   `["salaryExpectation", "workAuthorization"]`, never the values — the shipped
   `job.input_submitted` audit detail already behaves this way, and the events
   follow it.
2. **Hashes for artefacts, not artefacts.** A screenshot is
   `{locator, sha256}`; the file stays in `screenshot_dir` with its own retention.
3. **`submission_token` is never stored in an event** — only
   `submission_token_hash`, so a leaked event row cannot be replayed.
4. Every payload key is `snake_case`; values that are vocabulary come from
   `app/contracts/vocabulary.py`.
5. A payload is **immutable**: a correction is a new event, not an edit.

---

## 4. Sequence, ordering and dedupe

* `sequence` is allocated in the same transaction as the state change
  (`SELECT COALESCE(MAX(sequence),0)+1 … WHERE application_id = ?` inside the
  write, or a per-application counter column updated with a CAS). Ordering never
  depends on `occurred_at`, which has second granularity.
* The writer is **idempotent on `event_id`**: a retried transaction (or a
  re-executed queue item) that reuses an `event_id` is a no-op. Queue handlers
  therefore carry the `event_id` they intend to write in their checkpoint
  ([11 §6](11-background-jobs.md)).
* `occurred_at` is when the thing happened (the portal accepted the form);
  `recorded_at` is when the row landed. A recovered event has
  `recorded_at > occurred_at` and that gap is meaningful.
* Consumers paginate by `sequence` (keyset), not by `OFFSET`.

---

## 5. Mapping from the shipped `job_events.stage`

So nothing that renders the timeline today breaks, and so the migration has an
explicit table ([14 §4](14-migration-strategy.md)):

| `job_events.stage` *(shipped)* | `application_events.event_type` | `severity` from `job_events.status` |
|---|---|---|
| `discovered` | *(stays on `job_events`; not an application event)* | as written |
| `queued` | `application.queued` | `info` |
| `prepared` | `application.plan_ready` | `info` |
| `resume_reused` | `application.resume_selected` (`resume_decision = reused`) | `info` |
| `resume_generated` | `application.resume_selected` (`resume_decision = generated`) | `info`/`warning` on fact-guard flags |
| `form_detected` | `application.form_detected` | `success` when `detection_source = html`, else `warning` |
| `needs_input` | `application.input_requested` | `warning` |
| `user_input` | `application.input_provided` | `success` |
| `ready_to_apply` | `application.dry_run_completed` | `info` |
| `applied` | `application.submitted` or `application.marked_applied_manually` (from `meta.confirmed_by`) | `success` |
| `failed` | `application.submission_failed` | `error` |
| `skipped` | `application.cancelled` (user) / `application.expired` (system) | `info` |
| `retry` | `application.retried` | `info` |

`actor_type` for backfilled rows is derived: `meta.confirmed_by = "user"` →
`user`; a `pipeline_job_id` in `meta` → `system_worker`; otherwise `system_api`
with `trigger = recovery` and a `payload.backfilled = true` flag, because a
guess dressed as a fact is exactly what this contract set exists to prevent.

---

## 6. API

`GET /api/applications/{id}/events?after_sequence=0&limit=100` →
[13 §5](13-api-response-shapes.md). `GET /api/jobs/{id}/events` keeps working and
returns the shipped shape plus, for one minor version, the union of application
events for the job's live application.

---

## 7. Invariants (tests)

1. `sequence` is gapless and unique per application; two writers racing produce
   one row each with consecutive sequences (unique constraint + retry).
2. Every state transition in [07 §4](07-application-state-machine.md) produces
   exactly one event with matching `state_from`/`state_to`.
3. Every event has a non-null `actor_type` from `ACTOR_TYPES` and a
   `trigger` from `QUEUE_TRIGGERS`.
4. A state-changing event's `state_to` equals the application's state at
   `recorded_at` — the timeline and the row cannot disagree.
5. No payload contains a key named `password`, `otp`, `token`, `value` for a
   `restricted` field, or any string matching a credential pattern (a scanning
   test over fixtures).
6. Replaying a handler with the same checkpoint writes no duplicate event
   (`event_id` idempotency).
7. Cross-tenant reads are `404`; `after_sequence` from another tenant's
   application is ignored, not honoured.
8. Pruning never removes a row whose `state_to IS NOT NULL`.

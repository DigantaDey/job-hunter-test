# 12 — Event and notification contract

**Deliverable 10.** Two things, deliberately separate:

* a **domain event** — an immutable statement that something happened, for
  machines (notifications, analytics, audit, future webhooks);
* a **notification** — a message to one user, for humans, with a route and a
  dismissal state.

Shipped today: `notifications` (`kind`, `title`, `body`, `link`, `read`, `meta`),
`job_events`, `email_events`, `audit_logs`, and the per-run dedupe in
`handlers._notify_run`. There is no domain event table, so every consumer
re-derives "what happened" from a table that was built for another purpose.

---

## 1. `domain_events`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `event_id` | String(36) | no | UUIDv4; `UNIQUE(user_id, event_id)` — the dedupe key for writers and consumers |
| `user_id` | FK, index | no | tenant (`null` only for platform events, which are a separate table, not this one) |
| `event_type` | String(60) | no | `EVENT_TYPES` |
| `schema_version` | String(12) | no | the payload's version, e.g. `1.0` — additive changes bump the minor, a renamed key bumps the major |
| `aggregate_type` | String(24) | no | `AGGREGATE_TYPES`: `application` \| `job` \| `resume_document` \| `profile` \| `discovery_run` \| `match_result` \| `onboarding_session` \| `automation_policy` \| `email` \| `notification` \| `queue_job` \| `persona` \| `user` |
| `aggregate_id` | Integer | no | |
| `sequence` | Integer | yes | monotonic **per aggregate** — ordering that does not depend on timestamps; `UNIQUE(user_id, aggregate_type, aggregate_id, sequence)` |
| `actor_type` | String(20) | no | `ACTOR_TYPES` |
| `actor_id` | Integer | yes | |
| `actor_label` | String(320) | no | |
| `trigger` | String(16) | no | `QUEUE_TRIGGERS` |
| `correlation_id` | String(64) | yes | one user journey (a click and everything it caused) |
| `causation_id` | String(36) | yes | the `event_id` that caused this one |
| `pipeline_job_id` | Integer | yes | |
| `request_id` | String(64) | yes | |
| `severity` | String(20) | no | `NOTIFICATION_SEVERITIES` |
| `payload` | JSON | no | typed per event type ([09 §3](09-application-events.md) for applications; §2 below for the rest) |
| `dedupe_key` | String(200) | yes | for consumer-side suppression (a notification per run, not per event) |
| `occurred_at` | DateTime | no | index — `(user_id, occurred_at)` |
| `recorded_at` | DateTime | no | |
| `published_at` | DateTime | yes | when consumers were told; `null` = pending |

Append-only. Keyset pagination by `(occurred_at, id)` or by `sequence` per
aggregate. Retention `event_retention_days` (default 365) with the erasure rule
from [01 §7](01-conventions.md): rows survive, values are removed.

### Delivery semantics

| Property | Contract |
|---|---|
| Delivery | **at-least-once.** A consumer must dedupe on `event_id`. |
| Ordering | guaranteed **per aggregate** via `sequence`; not across aggregates. |
| Idempotence of writers | the writer stores the intended `event_id` in its checkpoint before acting ([11 §5](11-background-jobs.md)), so a retry cannot double-emit. |
| Failure | emitting is best-effort *after* the state change commits; a failed emit is retried by a sweeper (`published_at IS NULL AND recorded_at < now - 60s`) and never rolls back the state change. |
| Consumers in-process | notifications, analytics counters, persona memory, funding linkage. Out-of-process webhooks are **not** in this contract version; the envelope is shaped so they can be added without a rewrite. |

---

## 2. Event vocabulary by namespace

The full list is `EVENT_TYPES` in `app/contracts/vocabulary.py`. Namespaces and
their payload owners:

| Namespace | Emitted by | Payload defined in | Notes |
|---|---|---|---|
| `onboarding.*` | `onboarding_service` | [04](04-onboarding-state-machine.md) | `state_changed` carries `{from, to, gate}` |
| `profile.*`, `extraction.*` | `profile_service`, extraction handler | [02](02-candidate-profile.md), [03](03-resume-and-extraction.md) | one event per field, never per recomputation |
| `resume.*` | `resume_service` | [03](03-resume-and-extraction.md) | `uploaded`, `generated`, `approved`, `rejected`, `polished`, `archived`, `deleted` |
| `discovery.*` | `discovery` handler | [05](05-normalized-job.md) | `run_completed` carries the run report's summary blocks only, not the job rows |
| `match.*` | scoring | [06](06-match-result.md) | `high_score` is what the notification layer consumes |
| `application.*` | `application_service` | [09](09-application-events.md) | the richest namespace |
| `application.*` (tracking) ✓ shipped | `app/services/application_tracking.py` | [07 §11](07-application-state-machine.md) | `tracking_opened`, `state_changed`, `state_corrected`, `interview_reported`, `interview_completed`, `response_received`, `rejection_received`, `offer_received`, `withdrawn`, `note_added`, `follow_up_scheduled`, `follow_up_completed`, `attribution_updated`, `snapshot_recorded`, `suggestion_recorded` |
| `automation.*` | `auto_scheduler`, policy service | [10](10-automation-policy.md) | `run_skipped` carries the shipped `skipped_*` reason |
| `job.*` | `job_queue` | [11](11-background-jobs.md) | queue mechanics: `enqueued`, `claimed`, `paused`, `resumed`, `progressed`, `checkpointed`, `completed`, `failed`, `reclaimed`, `dead_lettered`, `cancelled` |
| `notification.*` | notification service | §3 | `created`, `read`, `dismissed` |

Naming rules: `<aggregate>.<past_tense_verb>`; no `*.updated` (say what changed);
no `*.started` unless a matching `*.succeeded`/`*.failed` exists; a state change
is `<aggregate>.state_changed` **plus** the specific event, never instead of it.

---

## 3. `notifications`

Shipped columns kept (`kind`, `title`, `body`, `link`, `read`, `meta`), plus:

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id`, `user_id`, `created_at` ✓ | | | |
| `kind` ✓ | String(40) | no | `NOTIFICATION_KINDS` — also the **preference key**: users mute kinds, not instances |
| `severity` **new** | String(20) | no | `NOTIFICATION_SEVERITIES`; `action_required` is what the badge counts |
| `title` ✓ / `body` ✓ | | | ≤ 200 / ≤ 2000 chars (shipped truncation) |
| `link` ✓ | String(500) | no | an **SPA route** (`/jobs`, `/queues`, `/resumes/41`), never an external URL |
| `action` **new** | JSON | no | `{label, route, method, entity_type, entity_id}` — the one click that resolves it ("Answer 3 fields" → `/queues?input=12`) |
| `dedupe_key` **new** | String(200) | yes | `UNIQUE(user_id, kind, dedupe_key)` — one notification per (run, kind), the shipped `auto_notified` payload marker promoted to a constraint |
| `source_event_id` **new** | String(36) | yes | the `domain_events.event_id` that caused it |
| `channels` **new** | JSON | no | `NOTIFICATION_CHANNELS` actually used; `email` only with consent (§5) |
| `read_at` **new** | DateTime | yes | `read` ✓ stays as the boolean the SPA already uses |
| `dismissed_at` **new** | DateTime | yes | dismissed ≠ read: an `action_required` notification can be dismissed without being resolved, and the underlying `blocked_*` state still shows in `GET /api/actions/required` |
| `expires_at` **new** | DateTime | yes | a stale alert disappears instead of nagging |
| `meta` ✓ | JSON | no | must include `pipeline_job_id` when the notification came from a run (shipped) |

Indexes: `ix_notifications_user_read (user_id, read, created_at)` ✓,
`INDEX(user_id, severity, created_at)` **new**,
`INDEX(user_id, kind, dedupe_key)` **new** (unique).

### Kinds, and what each obliges

| `kind` | Severity default | Trigger event | `link` | Preference key | Muted by default? |
|---|---|---|---|---|---|
| `high_match` | `info` | `match.high_score` (auto runs) | `/jobs` | `high_match` ✓ shipped | no |
| `funding_match` | `info` | `discovery.run_completed` on a funded company | `/funding` | — | no |
| `action_required` | `action_required` | `application.blocked`, `application.input_requested`, `resume.pending_approval` | `/queues` | — | **no, and cannot be muted** |
| `onboarding_step_required` | `action_required` | `onboarding.gate_blocked`, `onboarding.state_changed` into a blocking gate | `/onboarding` | — | cannot be muted |
| `profile_review_required` | `action_required` | `profile.field_extracted` with `review_required` | `/profile/review` | — | cannot be muted |
| `resume_ready` | `success` | `resume.generated`, `extraction.succeeded` | `/resumes` | — | no |
| `application_submitted` | `success` | `application.submitted` | `/applications/{id}` | — | no |
| `application_outcome` | `info` | `application.outcome_recorded` (inferred) | `/applications/{id}` | `new_replies` | no |
| `application_follow_up` ✓ shipped | `info` | the tracking follow-up sweep ([07 §11.4](07-application-state-machine.md)) | `/tracking?tracking_id={id}` | — | no |
| `automation_failed` | `error` | `job.dead_lettered`, `job.failed` with the budget gone | `/queues` | `automation_failures` ✓ shipped | no |
| `quota_exhausted` | `warning` | `automation.quota_exhausted` | `/billing` | — | deduped per period (shipped) |
| `consent_expiring` | `warning` | `disclosure.version_changed` | `/account?tab=consents` | — | no |
| `email_reply` | `info` | an inbound message classified as a reply | `/emails` | `new_replies` ✓ shipped | no |
| `weekly_summary` | `info` | the shipped `POST /api/notifications/generate-summary` and its scheduled equivalent | `/analytics` | `weekly_summary` ✓ shipped | no |
| `security` | `error` | `auth.*`, `vault.*`, `consent.revoked`, `account.deleted` | `/account` | — | **cannot be muted** |

Rules:

1. `action_required` and `security` are not user-mutable preferences — a product
   that lets you mute "we could not apply for you" is a product that silently
   does nothing.
2. One notification per (run, kind): `dedupe_key = "{pipeline_job_id}:{kind}"`.
   The shipped `auto_notified` payload list stays as the fast path; the unique
   constraint is the backstop for two workers racing.
3. A notification always names a server-owned entity and links to a route that
   renders it — never to a state that only the click knew about.
4. `title`/`body` are sentences, not codes; the code is in `meta.code` for
   programmatic use.
5. `body` for `high_match` keeps the shipped honesty rule: a run whose surfaced
   rows are not all `score_source = ai` appends "Scored by keyword overlap — open
   a job for the AI verdict."
6. `application_follow_up` is written once per (record, due date): the sweep
   stamps `application_tracking.follow_up_notified_at` and treats it as the dedupe
   marker, so a restart, a second worker or a repeated sweep notifies once. The
   body names the last *recorded* status together with its origin ("Last recorded
   status: Interview completed (user reported)"), because a reminder must not
   imply the product knows something nobody told it. `meta` carries
   `tracking_id`, `job_id`, `state`, `due_at`, `reason: "follow_up_due"` and
   `overdue_days`.

---

## 4. Notification state machine

| # | From | To | Actor / trigger | Guard | Event |
|---|---|---|---|---|---|
| N1 | — | created (`read = false`) | `system_worker` / `system_api` — a triggering event, preference on, dedupe free | kind not muted, or kind is unmutable | `notification.created` |
| N2 | unread | read | `user` — `POST /api/notifications/{id}/read` (shipped) or `read-all` | — | `notification.read` |
| N3 | any | dismissed | `user` — `DELETE /api/notifications/{id}` (shipped) | — | `notification.dismissed` |
| N4 | any | *(hidden)* | `system_scheduler` — `expires_at` passed | — | — (no event; expiry is not news) |
| N5 | unresolved `action_required` | auto-resolved | `system_worker` — the underlying block cleared (the user answered, the consent was granted) | the application left its `blocked_*` state | `notification.dismissed` with `meta.reason = "resolved"` |

N5 matters: a queue of stale "needs your input" badges after the user answered is
how a notification centre loses trust. Resolving the block resolves the badge.

---

## 5. Channels

| Channel | Requires | Rules |
|---|---|---|
| `in_app` | nothing | the default; always available |
| `email` | the `outreach` **or** a dedicated `notifications` consent, a verified address, a configured SMTP, the suppression list checked, the daily limit respected, and a one-click unsubscribe | reuses the shipped compliance gate in `services/outreach.py` — a notification email is outreach and is policed like it. `dry_run` defaults to `true`. |

No SMS, no push, no webhook in this contract version. Adding a channel means
adding it to `NOTIFICATION_CHANNELS`, a preference key, a compliance gate and a
test — not a new code path per notification.

---

## 6. Audit actions (the security-relevant subset)

`audit_logs.action` names are already free-form strings; the contract fixes them
as the `AUDIT_ACTIONS` vocabulary (shipped names first — they are in rows already
and are never renamed), and adds the
vocabulary for the actions the new aggregates introduce, following the shipped
`<aggregate>.<verb>` convention:

`onboarding.started` · `onboarding.completed` · `onboarding.abandoned` ·
`onboarding.gate_skipped` · `profile.review_completed` · `profile.updated` ·
`extraction.succeeded` · `extraction.failed` · `resume.uploaded` ✓ ·
`resume.generated` ✓ · `resume.approved` ✓ · `resume.rejected` ✓ ·
`resume.polished` ✓ · `resume.deleted` ✓ · `resume.downloaded` ✓ ·
`discovery.run_triggered` · `discovery.run_cancelled` · `job.apply_queued` ✓ ·
`job.input_submitted` ✓ · `job.applied` ✓ · `job.prepared` ·
`job.submit_approved` · `job.apply_failed` · `job.apply_retried` ·
`application.submitted` ✓ *(sensitive)* · `application.submitted_unverified` ·
`application.cancelled` · `application.outcome_recorded` ·
`automation.policy_updated` · `automation.auto_submit_enabled` *(sensitive)* ·
`automation.auto_submit_disabled` · `automation.policy_deleted` ·
`consent.accepted` ✓ · `consent.revoked` ✓ · `vault.*` ✓ · `auth.*` ✓ ·
`account.*` ✓

Actions in `audit.SENSITIVE_ACTIONS` (prefix match) are never dropped under load.
`automation.auto_submit_enabled` and `application.submitted` join that list:
they are the two moments the system does something irreversible in the user's
name.

---

## 7. API

* `GET /api/notifications` (shipped) gains `severity`, `action`, `channels`,
  `read_at`, `dismissed_at`, `expires_at`, `source_event_id`.
* `GET /api/events?aggregate_type=&aggregate_id=&after_sequence=&limit=` — the
  tenant's domain events, keyset-paginated.
* `GET /api/applications/{id}/events` — [09 §6](09-application-events.md).
* `GET /api/actions/required` — [13 §6](13-api-response-shapes.md), the unified
  "what do you owe us" read that notifications point at.

---

## 8. Invariants (tests)

1. One `domain_events` row per state change; `sequence` gapless per aggregate.
2. Replaying a handler with the same checkpoint emits nothing new (`event_id`
   dedupe).
3. Two runs of the same kind for the same `pipeline_job_id` produce **one**
   notification (unique constraint, not just the payload marker).
4. `action_required` and `security` kinds cannot be muted: a PUT to
   `/api/notifications/preferences` with those keys is a no-op that says so.
5. An email-channel notification without the consent/SMTP/suppression gate is
   refused, exactly as an outreach send is.
6. Every notification's `link` resolves to a route that exists in the SPA
   (a static check against `frontend/src/App.tsx` routes, in the style of
   `test_frontend_api_contract.py`).
7. Resolving a block dismisses its `action_required` notifications (N5).
8. No event payload or audit detail contains a secret or a `restricted` value.
9. Cross-tenant reads are `404`/empty; a consumer cannot subscribe to another
   tenant's events.

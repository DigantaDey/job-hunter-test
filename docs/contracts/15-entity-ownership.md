# 15 — Entity ownership and lifecycle registry

**Acceptance criterion: "All new entities have ownership and lifecycle
definitions."** One row per entity: who may create it, who may change it, who
reads it, when it dies, and what erasure does to it.

The **single-writer rule** is the point of this document. A table with two
writers is a table whose invariants are somebody else's problem; every entity
here names one module that is allowed to `INSERT`/`UPDATE` it (deletes go through
`app/services/erasure.py` for account erasure, and through the named writer for
user-initiated deletes).

---

## 1. Registry

### Identity and access *(shipped)*

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| User | `users` | `app/api/routers/auth.py`, `scripts/create_user.py` | everything (as identity) | bootstrap/register/CLI | the user (name, password, timezone), consents | deleted with the account | row deleted last; `audit_logs` keeps `user_id` detached |
| RefreshToken | `refresh_tokens` | `app/core/auth.py` | auth | login | revocation | revoked/expired; pruned after expiry | deleted |
| ApiKey | `api_keys` | `app/api/routers/auth.py` | auth | user | revocation, `last_used_at` | revoked | deleted |

### Candidate data

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| Persona *(shipped)* | `personas` | `app/services/persona.py` | discovery, scoring, outreach, analytics | first upload (`ensure_default_persona`) or user | user, `record_signal` (memory) | `is_active = false`; `PersonaInUseError` blocks delete while jobs/resumes reference it | deleted (references nulled first) |
| Profile *(shipped)* | `profiles` | `app/api/routers/resumes.py` (upload/update) | scoring, resume generation, autofill, outreach | upload | upload (replace), `PUT /api/profile/{id}` | superseded by the next upload | deleted |
| **Candidate profile** | `candidate_profiles` | `app/services/profile_service.py` | scoring, matching, resume generation, autofill mapping, onboarding, export | extraction handler or manual create | new version (never in place) | `archived`; superseded versions kept for the account's life | deleted |
| **Field provenance** | `profile_field_provenance` | `app/services/profile_service.py` | review UI, automation gates, match evidence, export | bulk on version create | review actions, user edits | deleted with its profile version | deleted |
| **Field history** | `profile_field_history` | `app/services/profile_service.py` | audit, "what did we send?" | every provenance change | never | append-only; kept for the account's life | values removed, rows kept |

### Documents and extraction

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| Resume *(shipped)* | `resumes` | `app/services/resume_service.py` | downloads, diff/preview, applications | upload/generate/polish | status, tags, guardrail report | `archived`; delete tombstones references | row + files deleted after references are detached |
| **Resume document** | `resume_documents` | `app/services/resume_service.py` | as above + onboarding + provenance | upload (after the file commits) | state transitions only ([03 §5](03-resume-and-extraction.md)) | `deleted` (tombstone, `profile_snapshot` kept) | row deleted; files removed after commit |
| **Extraction attempt** | `resume_extractions` | the extraction handler (`app/services/resume_parser.py`) | onboarding, review UI, ops | before the model is called | state, tokens, error, report | `succeeded`/`failed`/`rejected`/`superseded`; kept for the account's life | deleted |
| **Extracted field** | `extracted_fields` | the same handler | review UI, provenance | one bulk insert per attempt | never | append-only | deleted (`restricted` values first) |

### Discovery, jobs, matching

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| Job *(shipped)* | `jobs` | identity/description: `app/services/discovery.py`; status: `app/services/application_service.py` (via the projection) | board, drawer, analytics, matching, funding linkage | a discovery run | `last_seen_at`, score projection, status projection, `extra` | pruned after `prune_after_days` unseen **and** a terminal application state; never pruned if applied | deleted |
| **Discovery run** | `discovery_runs` | `app/services/discovery.py` (through the handler) | run history, empty-board banner, ops | `enqueue()` at trigger time | status, checkpoint, report | kept `run_retention_days` (default 180); pruned oldest-first | deleted |
| **Match result** | `match_results` | `app/services/scoring.py` | board ordering, job drawer, notifications, analytics | a scoring call | only `is_current`, `staleness`, `superseded_by_id` | append-only; non-current rows pruned after `match_history_days` (default 180) | deleted |
| Company intel *(shipped)* | `company_intel` | `app/services/company_intel.py` | job drawer | cache miss / refresh | refresh | 30-day cache TTL | deleted |
| Funding company / scan *(shipped)* | `funding_companies`, `funding_scans`, `funding_scan_companies` | `app/services/funding_radar.py` | radar, discovery linkage | a scan | `last_seen_at`, `has_open_positions` | pruned by the radar's own window | deleted (link table via parent) |

### Applications

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| **Application** | `applications` | `app/services/application_service.py` | board projection, progress API, analytics, notifications, export | `POST /api/jobs/{id}/apply` or the scheduler | state transitions only ([07 §4](07-application-state-machine.md)) | terminal states kept for the account's life | deleted |
| **Attempt** | `application_attempts` | same | audit, ops, "why did it fail" | each execution | result, evidence, timestamps | kept; screenshots pruned after 90 days | deleted |
| **Field answer** | `application_field_answers` | same | input queue, reuse, review | field mapping | resolution, reuse counters | kept; `restricted` values encrypted and removed at erasure | values removed, rows kept |
| **Application event** | `application_events` | same (via `record_application_event`) | timeline, analytics, support | every transition | never | append-only; non-state rows prunable after `event_retention_days` | values removed, rows kept |
| User input request *(shipped)* | `user_input_requests` | `app/services/apply_flow.py` → the application service | input queue, Queues page | a blocked mapping | answers, `status = completed` | kept | deleted |
| Vault entry *(shipped)* | `vault_entries` | `app/services/vault.py` | autofill login | user or `credential_for_application` | rotation, `last_used_at` | deleted by the user | deleted (reveal/export audited) |

### Automation, work, events

| Entity | Table | Single writer | Readers | Created by | Updated by | Terminal / retention | Erasure |
|---|---|---|---|---|---|---|---|
| **Automation policy** | `automation_policies` | `app/services/policy_service.py` | scheduler, apply flow, entitlements, Settings | account creation (defaults) | user edits; `expires_at` sweep | deleted only when narrower than `global` | deleted |
| **Policy revision** | `automation_policy_revisions` | same | audit, "what were the limits then?" | every change | never | append-only | values kept, `actor` detached |
| Scheduled run *(shipped)* | `scheduled_runs` | `app/services/auto_scheduler.py` | Settings card, `GET /api/automation`, cadence clock | each sweep decision | state on completion | pruned after 180 days | deleted |
| Pipeline job *(shipped)* | `pipeline_jobs` | `app/services/job_queue.py` | every live view, the worker | `enqueue()` | claim/complete/fail/pause/needs_input/recover/cancel | terminal rows pruned after 30 days unless referenced | deleted |
| **Idempotency key** | `idempotency_keys` | `app/core/idempotency.py` | the API middleware | first request with a key | never (write-once) | 24 h TTL, swept | deleted |
| **Domain event** | `domain_events` | `app/services/events.py` | notifications, analytics, audit, future webhooks | every state change | only `published_at` | append-only; 365 days | values removed, rows kept |
| Notification *(shipped)* | `notifications` | `app/api/routers/notifications.py` (`create_notification`) | bell, badges, `actions/required` | a triggering event | read/dismissed/expiry | deleted by the user or expiry | deleted |
| **Onboarding session** | `onboarding_sessions` | `app/services/onboarding_service.py` | onboarding UI, `actions/required` | account creation | gate completions, reconciliation on read | `ready`/`abandoned`; kept for the account's life | deleted |
| **Onboarding event** | `onboarding_events` | same | support, audit | every transition | never | append-only | values removed, rows kept |

### Outreach, billing, ops *(shipped, unchanged)*

| Entity | Table | Single writer |
|---|---|---|
| Email / email event / opt-out | `emails`, `email_events`, `email_opt_outs` | `app/services/outreach.py` |
| Subscription / billing event | `subscriptions`, `billing_events` | `app/services/billing.py` |
| AI credit ledger / usage counter | `ai_credit_ledger`, `usage_counters` | `app/services/ai_client.py`, `app/core/entitlements.py` |
| Audit log | `audit_logs` | `app/core/audit.py` |
| Error log | `error_logs` | `app/core/logging.py` |
| Settings | `settings` | `app/services/user_settings.py` |
| Interview prep | `interview_preps` | `app/api/routers/interview.py` |

---

## 2. Lifecycle patterns

Every entity above uses one of four lifecycles. Naming the pattern is what makes
a new entity reviewable:

| Pattern | Shape | Examples | Rules |
|---|---|---|---|
| **Mutable singleton** | one row per (tenant, scope), updated in place | `onboarding_sessions`, `automation_policies` (per scope), `profiles` *(shipped)* | `version` column for optimistic concurrency; every change emits an event |
| **Versioned current** | many rows, one `is_current` | `candidate_profiles`, `match_results` | the flip happens in the same transaction as the insert; superseded rows keep their inputs so the past is explainable |
| **Append-only log** | insert-only, immutable | `*_events`, `audit_logs`, `resume_extractions`, `extracted_fields`, `application_attempts`, `automation_policy_revisions`, `profile_field_history` | no update, no delete; `sequence` per aggregate; erasure removes values, not rows |
| **Lifecycle entity** | one row, an explicit state machine | `applications`, `resume_documents`, `discovery_runs`, `pipeline_jobs` | every transition has an actor, a guard, an event and (where relevant) an audit action; terminal states are declared |

---

## 3. What a new entity must declare before it is merged

1. Table name, columns, nullability, defaults, indexes and unique constraints —
   with the `user_id` column and its composite indexes.
2. Its lifecycle pattern (§2) and, for a lifecycle entity, its state machine with
   an actor per transition.
3. Its single writer module.
4. Its readers, and whether it is a denormalisation of something else (then: the
   transaction that keeps them equal, and the reconciliation query).
5. Its events (`EVENT_TYPES`) and, if security-relevant, its audit action.
6. Its retention and its erasure behaviour (deleted / values-removed / kept).
7. Whether its cross-references are nullable — a NOT NULL FK between two owned
   tables breaks erasure ([14 §9](14-migration-strategy.md)).
8. Its vocabulary additions, in **both** `app/contracts/vocabulary.py` and
   `frontend/src/lib/contracts.ts`, plus the doc row.
9. Its tests: tenancy (read as another user → 404), erasure (account deletion
   removes/handles it), invariants from its own contract document.

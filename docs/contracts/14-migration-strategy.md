# 14 — Migration strategy

Every change in this contract set is **additive at the schema level** and
**behind a flag at the behaviour level**. No revision drops a column, renames a
stored value, or removes an endpoint. The plan is the standard
**expand → migrate → contract**, with the contract phase scheduled explicitly
rather than "later".

---

## 1. Where the schema is now

Alembic chain (single head):

```
db63915afc60  initial multi-tenant schema (v2.0.0)
   └─ 9f8b1a2c3d4e  billing and saas monetization
       └─ a1b2c3d4e5f6  personas, scoring provenance, email job context, resume naming
           └─ b2c3d4e5f6a7  funding radar honesty
               └─ c3d4e5f6a7b8  auto-mode scheduled_runs
                   └─ d4e5f6a7b8c9  funding scan history
                       └─ e5f6a7b8c9d0  jobs.company_name_normalized
                           └─ f6a7b8c9d0e1  funding_companies.name_normalized
                               └─ a7b8c9d0e1f2  pipeline_jobs.reclaim_count   ← HEAD
```

Conventions the existing revisions set, and that every new one follows:

* **Idempotent helpers.** `_add_column_if_missing()` (from `a1b2c3d4e5f6`) — a
  re-run of a partially applied revision must not fail.
* **Backfill inside the revision.** `e5f6a7b8c9d0` computes
  `company_name_normalized` for existing rows with the rule *duplicated in the
  migration* rather than imported from the service layer (services import models,
  never the reverse). New backfills do the same.
* **Server defaults for NOT NULL columns** added to a populated table, then the
  default dropped once the backfill is verified.
* **Both dialects.** SQLite (dev/single-user) and PostgreSQL 16 (prod) run the
  same revisions in CI; anything dialect-specific is guarded by
  `op.get_bind().dialect.name`.
* **FKs are `NO ACTION` and enforced** — the test suite turns SQLite FK
  enforcement *on* (v2.2.15), so a new FK must come with its erasure path
  ([15](15-entity-ownership.md)), or `DELETE /api/account` breaks.

---

## 2. Proposed revisions

One revision per phase, each independently deployable and reversible.

| # | Revision (proposed id) | Name | Adds | Backfills | Risk |
|---|---|---|---|---|---|
| R1 | `b8c9d0e1f2a3` | `contracts_vocabulary_and_job_status_truth` | nothing structural — a data-only revision that maps any legacy `jobs.status` value to `JOB_STATUSES` (`applying`→`preparing`, `emailed`→`discovered`, unknown→`discovered`) and records the count in the revision log | yes (rows) | low |
| R2 | `c9d0e1f2a3b4` | `resume_documents_and_extractions` | `resume_documents`, `resume_extractions`, `extracted_fields`; new columns on `resumes` (`sha256`, `text_sha256`, `content_type`, `size_bytes`, `state`, `role`, `fact_guard_report`, `created_by`, `pipeline_job_id`, `archived_at`, `deleted_at`) | copies every `resumes` row into `resume_documents` (`role` from `type`: `master`→`master`, `generated`→`tailored`, `uploaded_polished`→`polished`; `state` from `status`), computes `sha256` from the file when present else `''` | medium (large table copy) |
| R3 | `d0e1f2a3b4c5` | `candidate_profiles_and_provenance` | `candidate_profiles`, `profile_field_provenance`, `profile_field_history` | one `candidate_profiles` row per `profiles` row (`version = 1`, `is_current = true`, `state = active`, `document` from `data` via the field map in §4); one provenance row per non-empty leaf, `origin` from `extraction_source` (`ai`→`resume_extraction`, `user_edited`→`user_corrected`), `confidence = null`, `band = none`, `review_status = confirmed` for `user_edited` and `unreviewed` otherwise, `review_required` computed by `requires_review()` | medium |
| R4 | `e1f2a3b4c5d6` | `onboarding_sessions` | `onboarding_sessions`, `onboarding_events` | one session per user, derived from what already exists: gate 1 `completed`; gate 2 `completed` when both consent timestamps are set; gate 3 `completed` when a master resume with a profile exists; gate 4 `completed` when `profiles.extraction_source = 'user_edited'` **or** no provenance row needs review; gate 5 `completed` when the preference fields are present; state re-derived. Users who are obviously past onboarding land in `ready` with `completed_at = users.created_at` and `payload.backfilled = true` in the first `onboarding_events` row | low |
| R5 | `f2a3b4c5d6e7` | `discovery_runs_and_job_normalization` | `discovery_runs`; `jobs` columns `title_normalized`, `content_hash`, `source_kind`, `role`, `location_detail`, `compensation`, `portal`, `provenance`, `description_sha256`, `jd_hash`, `discovery_run_id`, `first_seen_at`, `last_seen_at` | `first_seen_at = discovered_at`, `last_seen_at = discovered_at`, `title_normalized`/`content_hash`/`description_sha256` computed, `compensation` from `extra.salary` (parse when possible, else `{raw: …, transparency: "unknown"}`), `portal` from `extra.forms`, `provenance.source_kind` from the source registry; one `discovery_runs` row per existing `done` discovery queue item, `report` copied from `payload.result`, `status = completed`/`completed_empty` from `why_empty`, `pipeline_job_id` linked | medium |
| R6 | `a3b4c5d6e7f8` | `match_results` | `match_results` | one row per job with `score > 0`: `scorer` from `score_source` (`ai`→`ai`, else `deterministic`), `scorer_version` = the deployed one at migration time, `profile_id` = the current profile, `profile_sha256`/`job_description_sha256` computed now, `rubric` from `score_detail`, `is_current = true`, `staleness = 'fresh'`, `payload.backfilled = true` | low |
| R7 | `b4c5d6e7f8a9` | `applications_and_answers` | `applications`, `application_attempts`, `application_field_answers`, `application_events`; `pipeline_jobs` columns `task`, `entity_type`, `entity_id`, `idempotency_key`, `correlation_id`, `parent_job_id`, `progress`, `checkpoint`, `paused_count`, `started_at`, `error_code`, `cancelled_at`, `cancelled_by`; `idempotency_keys`; `domain_events`; `automation_policies`, `automation_policy_revisions`; `notifications` columns `severity`, `action`, `dedupe_key`, `source_event_id`, `channels`, `read_at`, `dismissed_at`, `expires_at` | one `applications` row per job whose `status` is not `discovered`/`skipped` (`state` from the projection's inverse, §5), one `application_attempts` row per job with `plan` from `extra.autofill_plan` and `result` from `extra.autofill_result`; `application_field_answers` from the plan's `fields[]` + `user_input_requests.fields[]`; `application_events` from `job_events` via the map in [09 §5](09-application-events.md); `automation_policies` global rows from the shipped settings keys; `pipeline_jobs.task` from `payload.task`, `paused_count` from `payload.paused_count` | **high** — this is the phase that changes what the product reads |
| R8 | `c5d6e7f8a9b0` | `contract_cleanup` | drops nothing; removes the *dual writes* and the compatibility views once §6's window has passed | — | low |

Every revision has a `downgrade()` that reverses it. R7's downgrade is the only
one that can lose information (rows created after the upgrade have no legacy
home), so it is guarded: it refuses to run when
`applications.created_at > revision_date` rows exist unless
`ALLOW_CONTRACT_DOWNGRADE_DATA_LOSS=true` is set.

---

## 3. Phasing and flags

| Phase | What runs | Flag | Exit criterion |
|---|---|---|---|
| **0. Vocabulary** | `app/contracts` + the TS mirror + the drift test + R1 | none (additive code) | CI green; the funnel/board/filters agree with the vocabulary |
| **1. Expand** | R2–R7 applied; new tables written **and** legacy columns still written (dual write) | `CONTRACTS_DUAL_WRITE=true` (default true) | new tables populated for 100% of tenants; a reconciliation job reports zero diffs |
| **2. Read-over** | new reads (`/api/onboarding/status`, `/api/actions/required`, `/api/applications/{id}/progress`, `/api/discovery/runs`, `/api/matches`) served from the new tables; old reads unchanged | `CONTRACTS_READ_NEW=true` per endpoint group | the SPA renders the new reads; a shadow-compare of old vs new read paths logs mismatches |
| **3. Write-over** | the application service becomes the single writer of `applications.state`; `jobs.status` becomes the projection | `CONTRACTS_APPLICATION_ENTITY=true` | no code path writes `jobs.status` directly (asserted by a test that greps for assignments outside the projection helper) |
| **4. Contract** | dual writes removed, compatibility views dropped, R8 | `CONTRACTS_LEGACY_WRITES=false` | one minor version after phase 3, and only after the reconciliation job has been silent for a full release |

Flags are ordinary settings (`app/core/config.py`) with production-safe defaults:
the default of every flag is the behaviour that is already shipped, so deploying
the code without setting a flag changes nothing observable.

---

## 4. Backfill rules

1. **A backfill never invents a fact.** Where the source data does not support a
   value, the backfill writes the honest unknown (`confidence = null`,
   `band = none`, `review_status = unreviewed`, `staleness = fresh` only when the
   inputs really are current) and marks the row `payload.backfilled = true`.
2. **A backfill is resumable and idempotent.** It runs in batches of 500 keyed by
   `id > :cursor`, commits per batch, and can be re-run after a crash
   (`alembic upgrade head` again, or the standalone
   `scripts/backfill_contracts.py --phase R3 --resume`).
3. **A backfill is observable.** Per batch: rows read, rows written, rows skipped,
   elapsed; a final summary row in `error_logs` at `level = info` with
   `pipeline = migration`.
4. **Field map for `profiles.data` → `candidate_profiles.document`** (R3):

| Legacy key | New path | Sensitivity | Note |
|---|---|---|---|
| `name` | `/identity/full_name` (+ split into `first_name`/`last_name`) | `public` | the split is `derived`, `confidence = null` |
| `email` | `/contact/email` | `sensitive` | |
| `phone` | `/contact/phone` | `sensitive` | |
| `location` | `/contact/location/city` | `sensitive` | a bare string becomes `city`; `country_code` stays empty rather than guessed |
| `summary` | `/summary` | `public` | |
| `current_title` | `/current_title` | `public` | |
| `skills[]` | `/skills[]` (`{name}`) | `public` | a string list becomes `{name}` objects |
| `experience[]` | `/experience[]` | `internal` | `title`, `company`, `duration` → `start`/`end` **only when parseable**; otherwise `duration_raw` is kept and both stay `null` (inventing a date is a fabrication) |
| `education[]` | `/education[]` | `internal` | |
| `projects[]`, `links[]` | `/projects[]`, `/contact/links[]` | `public` | `links[]` entries become `{kind, url}` with `kind` inferred from the host (`linkedin.com`→`linkedin`), else `other` |
| `raw_text` | **not** in `document` | — | moves to `resume_documents.text_snapshot`; `meta.raw_text_sha256` points at it |
| `salary_expectation`, `notice_period`, `work_authorization` (when present) | `/compensation/target`, `/availability/notice_period_days`, `/eligibility/work_authorization` | `sensitive` | `review_required = true` for all three, whatever the confidence: they came from a resume, and a resume rarely states them |
| *(absent)* | `/preferences/*` | `internal` | left empty; onboarding gate 5 asks |

5. **`jobs.status` inverse projection** (R7) uses the shipped values only:
   `discovered`→no application row; `queued`→`queued`; `preparing`→`preparing`;
   `needs_input`→`blocked_input`; `ready_to_apply`→`prepared`; `applied`→
   `submitted` (`submission_channel = automation` when `extra.autofill_result.status
   = submitted`, else `manual_user`); `failed`→`failed`; `skipped`→`skipped`.
   Rows whose `extra` contradicts the status get the status's value and a
   `payload.backfill_conflict` note — the status is what the user saw.

---

## 5. Renaming or removing a stored value

The vocabulary will need this eventually. The procedure is fixed:

1. **Add** the new value; write it everywhere; read both.
2. **Backfill** old → new in a data-only revision, in batches, idempotently.
3. **Wait** one minor release with a counter on reads of the old value
   (`jobhunter_legacy_status_reads_total{value=…}`).
4. **Remove** the old value from the vocabulary and add an assertion that no row
   carries it (a test, not a runtime check).
5. Never do steps 1 and 4 in the same release: a rolling deploy means old and new
   code coexist, and old code reading a new value must degrade to "unknown", not
   crash ([01 §9](01-conventions.md)).

This is also why `applying`/`emailed` are **removed** rather than migrated in R1:
no row has ever held them (verified by a `SELECT DISTINCT status` in the revision
and by the drift test), so the only artefacts are a comment, an analytics bucket
and a filter option.

---

## 6. API compatibility window

* A renamed or restructured endpoint keeps the old path **and** the old keys for
  one minor version. The old path returns the new document plus the legacy keys
  (as `GET /api/jobs/discovery/last-run` will do over `discovery_runs`).
* Removing a key from a response is a breaking change and needs the same window
  as removing a column.
* Adding a key is never breaking, and clients must tolerate unknown keys.
* The OpenAPI schema (`/api/openapi.json`) is generated from pydantic models for
  every new endpoint, so `test_frontend_api_contract.py` keeps catching
  body/query mismatches for the new routes automatically.

---

## 7. Zero-downtime and rollback

| Concern | Rule |
|---|---|
| Long locks | no `ALTER TABLE` that rewrites a large table while serving; new columns are nullable or have a server default (PostgreSQL 11+ adds a defaulted column without a rewrite) |
| Big copies | R2/R3/R5/R7 copy rows in a background script, not inside the revision's transaction, when the table exceeds 100k rows; the revision only creates structure and marks the backfill pending |
| Rolling deploy | new code reads old rows (every new column nullable or defaulted) and old code ignores new columns |
| Rollback | each revision's `downgrade()` is tested in CI on a database that has been upgraded *and* populated with fixtures |
| Startup | `init_db()` applies migrations at boot (shipped); a failed migration must fail readiness (`/api/health/ready` reports `migration_state`) rather than serve a half-migrated schema |
| Verification | `scripts/verify_contracts.py --phase N` runs the reconciliation queries (§8) and exits non-zero on any diff; CI runs it against a fixture database after each phase |

---

## 8. Reconciliation (the thing that makes dual-write safe)

While phase 1–3 dual-write, a scheduled job compares the two representations and
reports, per tenant and in total:

| Check | Query sketch |
|---|---|
| board status | `jobs.status != job_status_for(live application.state)` |
| match projection | `jobs.score != current match_results.score` (or `score_source` differs) |
| profile parity | `profiles.data` leaves without a matching `profile_field_provenance` row |
| resume parity | `resumes` rows without a `resume_documents` twin |
| run parity | `pipeline_jobs(pipeline=discovery, status=done)` without a `discovery_runs` row |
| event parity | application transitions without an `application_events` row of the next `sequence` |
| answer parity | `extra.autofill_plan.fields[*].value` differing from `application_field_answers.value` |

Diffs are written to `error_logs` (`level = warning`, `pipeline = contracts`) with
counts only — never values — and the phase does not advance until a full release
is silent.

---

## 9. Erasure, retention and the new tables

`app/services/erasure.py` derives the erasure plan from `Base.metadata`, so:

* every new table with a FK to `users.id` is deleted by `user_id` **automatically**
  — which is why `user_id` is NOT NULL on all of them, including
  `extracted_fields` and `application_events`, where it is technically
  denormalised;
* nullable FKs **between** owned tables are nulled first (the cycle breaker), so
  `applications.resume_document_id`, `match_results.profile_id` and
  `onboarding_sessions.pipeline_job_id` must be nullable — a NOT NULL cross
  reference here would reintroduce the exact `IntegrityError` v2.2.15 removed;
* link tables without `user_id` are cleaned through their parent;
* append-only tables (`*_events`, `audit_logs`) keep the row and lose the values
  ([01 §7](01-conventions.md)).

A new entity therefore has to declare, in
[15-entity-ownership.md](15-entity-ownership.md): its `user_id` column, its
nullable cross-references, and whether it is append-only. The erasure test
(`test_deletion_integrity.py`) is extended with one case per new table.

Retention defaults (settings, per-tenant override where the plan allows):

| Data | Default | Notes |
|---|---|---|
| `pipeline_jobs` terminal rows | 30 days | except rows referenced by an event/run row |
| `domain_events`, `application_events` | 365 days | state-changing rows are never pruned |
| screenshots / autofill evidence | 90 days | the reference survives with its `sha256` |
| superseded `candidate_profiles` | life of account | needed to explain a past application |
| `resume_documents` (archived) | life of account | tombstoned on delete |
| `idempotency_keys` | 24 h | |

---

## 10. Test strategy per phase

| Phase | Tests |
|---|---|
| 0 | `test_contracts_vocabulary.py` (docs ↔ python ↔ TS ↔ the funnel/board/filter), `test_regressions.py` case for the funnel counts |
| 1 | migration up/down on SQLite **and** PostgreSQL with fixtures; backfill idempotency (run twice, assert no change); erasure completeness for every new table |
| 2 | contract tests per endpoint in [13](13-api-response-shapes.md) (golden JSON with volatile keys normalised); tenancy cases per new read |
| 3 | state-machine tests: every transition in [07 §4](07-application-state-machine.md) asserted with its actor, event and projection; submission at-most-once under a simulated crash |
| 4 | the reconciliation queries return zero diffs; the legacy-write greps fail if a direct write reappears |

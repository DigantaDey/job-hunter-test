# 01 — Cross-cutting conventions

Everything in this file applies to every entity, endpoint and event in the rest
of the contract set. Where the existing codebase already has a convention, this
document *pins* it (and says so); where it has two conflicting ones, this
document picks one and defines the migration
([14-migration-strategy.md](14-migration-strategy.md)).

---

## 1. Naming and types

| Rule | Detail |
|---|---|
| JSON keys | `snake_case`. |
| **Exception (frozen)** | keys *inside* `profiles.data`, `resumes.profile_snapshot`, form field `profile_key` and autofill plan entries stay `camelCase` (`firstName`, `salaryExpectation`, `workAuthorization`). They are already persisted in rows and rendered by the SPA; renaming them is a data migration with no user benefit. The canonical list is `KNOWN_FIELDS` in `app/services/form_detector.py` + `PROFILE_PATHS` in `app/services/autofill.py`. |
| Table names | plural `snake_case`. |
| Columns | `snake_case`; booleans are `is_*`/`has_*`/`allow_*`. |
| Foreign keys | `<entity>_id` (`job_id`, `application_id`, `resume_document_id`). |
| Public identifiers | `<entity>_id` in payloads. Never expose a row id of another tenant's row, never accept one as input for scoping. |
| Status/state columns | singular noun: `status` (execution/technical) vs `state` (business lifecycle). A table has **one** of the two meanings; `applications.state` is business, `pipeline_jobs.status` is execution. |
| Vocabulary values | lowercase `snake_case`, from `app/contracts/vocabulary.py`. |
| Event types | `<aggregate>.<past_tense_verb>` — `application.submitted`, never `application.submit`. |
| Audit actions | `<aggregate>.<verb>` in the past or infinitive already used by `app/core/audit.py` (`resume.uploaded`, `job.apply_queued`). |
| Money | integer minor units + explicit `currency` (ISO 4217), or a `*_usd` float only where the shipped code already uses one (`estimated_cost_usd`). Never a bare float called `salary`. |
| Timestamps | see §2. |
| Ids | integer PKs (`id`), matching the existing schema. Event rows additionally carry a UUIDv4 `event_id` for cross-process dedupe. |

Types are the SQLAlchemy 2 annotated style already used in
`backend/app/models/models.py` (`Mapped[...] = mapped_column(...)`), `JSON` for
documents, `DateTime` (naive UTC at rest — see §2), `String(n)` with an explicit
length, `Text` for prose. No new column type may be introduced without a note in
[14-migration-strategy.md](14-migration-strategy.md) about SQLite *and*
PostgreSQL support.

---

## 2. Timestamps

**At rest**: naive UTC (`datetime.utcnow()`), as today — the DB stores no offset
and every writer agrees on UTC.

**On the wire**: ISO-8601 **with an explicit UTC offset**, e.g.
`2026-09-18T14:03:22Z`.

This is a real fix, not a preference. `app/services/auto_scheduler.iso_utc()`
already emits an offset, and `frontend/src/lib/automation.ts` documents the
consequence: *"The API always sends an explicit UTC offset for these, so the
local conversion is correct — which it is not for the naive timestamps most
other endpoints return."* A naive `2026-09-18T14:03:22` parsed by
`new Date(...)` is interpreted in the **browser's** local zone, so the same row
renders up to ±14 h wrong depending on where the user is.

Rules:

1. Every new endpoint serialises datetimes through one helper (`iso_utc`) that
   appends `Z`.
2. `null` means "has not happened"; an absent key means "this contract version
   does not carry it". Never `""`, never `0`, never the epoch.
3. Business timestamps are distinct columns, never inferred from `updated_at`:
   `submitted_at`, `approved_at`, `reviewed_at`, `occurred_at`, `finished_at`.
4. `updated_at` is maintained by the ORM (`onupdate=utcnow`) and must not be
   written by hand.
5. Ordering ties are broken by `id` (monotonic), not by a second-granularity
   timestamp — the pattern already used by `discovery_last_run` and
   `live_view`.
6. Existing endpoints keep returning naive strings until their phase in
   [14](14-migration-strategy.md) migrates them; the SPA must therefore keep
   tolerating both (parse → if no offset, assume UTC).

---

## 3. Tenant isolation

Non-negotiable, and the reason several rules below look paranoid.

1. **Column**: every tenant-owned table has `user_id INTEGER NOT NULL` with an
   index (composite with the columns it is queried by, e.g.
   `ix_applications_user_state (user_id, state)`).
2. **Writer**: the value comes from the authenticated identity
   (`CurrentUser`), never from a request body, query parameter or path segment.
3. **Reader**: every query filters on it. The house pattern is a helper
   (`_job_or_404`, `get_owned_profile`) rather than a filter repeated per call;
   new services must expose the same kind of `_or_404` helper.
4. **Cross-tenant references are refused, not followed**: a `job_id` that
   belongs to someone else is a `404`, not a `403` — do not leak existence.
5. **Link tables** carry `user_id` too when either side does, or an
   `ON DELETE CASCADE` FK to a table that does (the `funding_scan_companies`
   pattern). `app/services/erasure.py` derives the erasure plan from
   `Base.metadata`, so a new table with a FK to `users.id` is deleted
   automatically — a new table *without* one must be listed in
   [15-entity-ownership.md](15-entity-ownership.md) with its erasure rule.
6. **Worker scoping**: a claimed queue item is executed with
   `item.user_id` as the only identity; handlers must never query without it.
7. **Logs and metrics**: `user_id` (a number) and `request_id` are attached to
   log records; restricted values (§7) never appear in either.
8. **No client-supplied tenancy in events**: a domain event's `tenant_id` is
   copied from the row, not from the request.

Verification lives in `backend/tests/test_auth_and_tenancy.py` and
`test_deletion_integrity.py`; every new entity adds a case to both (read as
another user → 404; delete account → row gone).

---

## 4. Pagination and list envelopes

Shipped endpoints return bare arrays with `limit`/`offset` (`GET /api/jobs`).
That stays. **New** list endpoints return an envelope so a client can render a
pager without a second request:

```json
{
  "items": [ … ],
  "page": 1,
  "page_size": 50,
  "total": 318,
  "has_more": true,
  "server_time": "2026-09-18T14:03:22Z"
}
```

* `page` is 1-based, `page_size` clamped (`1..200`, default `50`).
* `total` may be omitted (`null`) when counting would be more expensive than the
  page itself — then `has_more` is authoritative.
* Keyset pagination (`after_id`) is required for append-only tables that grow
  without bound (`application_events`, `domain_events`, `audit_logs`) because
  `OFFSET` over 100k rows is not a page, it is a scan.
* Filters are query parameters with the vocabulary's values; an unknown filter
  value is a `422 validation_error`, never a silent empty list.

---

## 5. Errors

One shape, always with `request_id` (the shipped handlers in `app/main.py`
already do this):

```json
{
  "detail": "Human sentence the SPA may show as-is.",
  "code": "consent_required",
  "message": "Accept the automation disclosure to let JobHunter submit for you.",
  "request_id": "8f1c…",
  "retry_after": 30,
  "fix": "/account?tab=consents",
  "errors": [ { "loc": ["body", "salary_min"], "msg": "…" } ]
}
```

| Status | When | `code` |
|---|---|---|
| 400 | malformed but authenticated input | `validation_error`, `masked_api_key`, `invalid_provider` |
| 401 | no/invalid/expired credential | `not_authenticated`, `credential_mismatch` |
| 402 | plan does not include the capability | `upgrade_required` (+ `capability`, `message` from `UPGRADE_HINTS`) |
| 403 | capability exists but a consent/disclosure is missing | `consent_required` (+ `consent` key) |
| 404 | not found **or** owned by another tenant | `job_not_found`, `application_not_found` |
| 409 | conflicts with stored state | `application_already_submitted`, `idempotency_conflict`, `resume_referenced`, `storage_cap_reached`, `onboarding_already_complete` |
| 413/415 | upload size/type | `payload_too_large`, `unsupported_media_type` |
| 422 | schema-valid but not safe to ship | `guardrail_failed`, `fact_guard_failed`, `field_needs_review`, `restricted_field` |
| 429 | rate/quota | `rate_limited`, `quota_exhausted`, `too_many_attempts` (+ `Retry-After`) |
| 503 | AI provider unavailable | `ai_unavailable`, `ai_paused`, `ai_blocked` (+ `state`, `pausable`, `retry_after_hint`) |

Rules:

1. `code` is from `ERROR_CODES` — a new failure mode adds a code, it does not
   reuse `validation_error`.
2. `detail` is always a string a human can read (the SPA's `apiError()` reads it
   first); structured information goes in the sibling keys.
3. A retriable failure carries `retry_after` (seconds) **and** the
   `Retry-After` header.
4. Never leak: no stack traces, no SQL, no provider keys, no other tenant's ids,
   no restricted field values (§7) — `settings.debug` may add `error` for
   developers, as the shipped 500 handler does, and only in non-production.
5. A *rejected artefact* (fact guard, guardrail) is `422` with `issues[]`, and
   for queued work it is a **result** (`{"status": "rejected", …}`), not a queue
   failure — retrying the same prompt would not change the answer
   (`handlers.handle_generate_resume` already does this).

---

## 6. Provenance, confidence and evidence

One object shape, used by profile fields, extracted fields, match results,
detected forms, contact data and inferred application outcomes. Field names are
fixed; a producer omits what it does not know rather than inventing it.

```json
{
  "source": "resume_extraction",
  "confidence": 0.91,
  "band": "high",
  "extractor": {
    "name": "ai_profile_extractor",
    "version": "1.4.0",
    "model": "gpt-4o-mini",
    "prompt_version": "profile-2026-08-30"
  },
  "evidence": [
    {
      "kind": "text_span",
      "quote": "Senior Embedded Engineer, Wirelane GmbH — Mar 2021 to present",
      "locator": { "document_id": 41, "page": 1, "start": 1180, "end": 1244 },
      "retrieved_at": "2026-09-18T14:03:22Z"
    }
  ],
  "computed_at": "2026-09-18T14:03:22Z",
  "review": { "status": "needs_review", "reviewed_by": null, "reviewed_at": null }
}
```

| Key | Type | Rule |
|---|---|---|
| `source` | enum `PROVENANCE_SOURCES` | mandatory. Where the value came from, not who stored it. |
| `confidence` | float 0..1 \| null | mandatory for anything model- or heuristic-produced; `null` only for `user_provided`/`user_confirmed`/`user_corrected` (a human statement is not a probability). |
| `band` | enum `CONFIDENCE_BANDS` | **derived**, never written by hand: `confidence_band()` — `high ≥ 0.85`, `medium ≥ 0.60`, `low ≥ 0`, `none` otherwise/absent. |
| `extractor` | object \| null | `name`, `version`, `model`, `prompt_version`. Required whenever `source` is `resume_extraction`, `ai_inferred`, `portal_html` with an AI pass, or `heuristic` (so a bad batch can be found and re-run). |
| `evidence[]` | array | at least one entry whenever `confidence` is not null. `kind` ∈ `EVIDENCE_KINDS`: `text_span`, `url`, `document`, `provider_record`, `user_statement`, `portal_response`, `email_message`. `quote` ≤ 500 chars. `locator` points at the artefact (`document_id` + `page` + `start`/`end` char offsets, or `url`, or `provider` + `record_id`). |
| `computed_at` | timestamp | when the value was produced (not when it was stored). |
| `review` | object | `status` ∈ `REVIEW_STATUSES`, plus `reviewed_by` (`user`/`owner`/`system`) and `reviewed_at`. |

**The review rule** (`requires_review(sensitivity, band)`):

```
needs_review  ⇔  sensitivity ∈ {sensitive, restricted}  OR  band ∈ {low, none}
```

Consequences that every implementer must honour:

* a `needs_review` value **may not** be used by automation (autofill, outreach,
  submission) until a human resolves it;
* a `needs_review` value **may** be displayed to its owner, masked per §7;
* `auto_accepted` is only legal for `public`/`internal` values with
  `band = high`;
* `corrected` keeps the previous value in the field's history
  (`profile_field_history`), it does not overwrite it;
* a value whose `source` is `heuristic` is capped at `confidence ≤ 0.5`
  (`band = low`) by definition — the shipped contact-discovery and form
  heuristics already behave this way (`verified=false`,
  `ai_confidence=0.35`).

**Never invent evidence.** If there is no span, URL or record to point at, the
value is `ai_inferred` with `evidence: []` and `confidence ≤ 0.6`, and the
review rule sends it to a human. This is the same principle as the shipped fact
guard (`resume_service.fact_guard_check`) and the anti-fabrication guard in the
funding radar.

---

## 7. Sensitivity, restricted data and secrets

Classified **at write time**, stored on the field's provenance row, and copied
into every payload that carries the value. Reclassifying later is a migration.

| Level | Meaning | Examples | Storage | Display | Logs/audit | Automation |
|---|---|---|---|---|---|---|
| `public` | already public by the user's own act | name, skills, LinkedIn URL, public portfolio | plain | always | allowed | allowed |
| `internal` | personal but not identifying on its own | employment history, education, city, seniority | plain | always | allowed | allowed |
| `sensitive` | identifying or financially/legally relevant | personal email, phone, exact address, DOB, compensation expectation & history, notice period, work authorization, sponsorship need | plain, access-audited | to the owner only; masked in list views | **never the value**; the field *name* is allowed | only after `review.status ∈ {confirmed, corrected, user_provided}` |
| `restricted` | legally protected or credential material | government/national id, passport/visa numbers, EEO self-identification (gender, race/ethnicity, veteran, disability), portal passwords, OTP codes, references' contact details | **encrypted at rest** (per-user HKDF scope, `app/core/security.py`) or, for secrets, only in `vault_entries` | never echoed back; secrets are masked (`••••`) and revealed only through an audited reveal endpoint | **never**, not even the field name for secrets | **never inferred**; only an explicit user choice, per [08](08-application-fields.md) |

Hard rules:

1. **Secrets are not profile fields.** Passwords, OTPs, API keys and SMTP
   credentials live in `vault_entries` / `settings` (encrypted), referenced by
   id. No other table stores one, and no event payload contains one.
2. **EEO answers default to decline.** `policy_default` for
   `EEO_FIELD_KEYS` is `decline_to_answer`; the value is never inferred from a
   name, a photo, a resume or a previous application, and `reuse_scope` defaults
   to `this_application`.
3. **Masking on the wire**: a `restricted` value is returned as `{"masked":
   true, "hint": "•••• 4242"}` unless the caller is the owner *and* asked for a
   reveal through an audited endpoint (`GET /api/vault/{id}/reveal` pattern).
4. **Audit detail is redacted**: `audit_logs.detail` records field *names* and
   counts (`{"fields": ["salaryExpectation", "workAuthorization"]}`), never
   values — which is what `POST /api/jobs/{id}/input` already does.
5. **Export includes everything** (it is the data subject's right); the export
   stream marks each value's sensitivity so a reviewer can see what left the
   building.
6. **Erasure**: `restricted` values are deleted with the row; their *events*
   survive with the value removed (`app/services/erasure.py` detaches rather
   than deletes for append-only tables).

---

## 8. Idempotency

Two mechanisms, both required, because they answer different questions.

### 8.1 `Idempotency-Key` (transport-level, client-generated)

* Header: `Idempotency-Key: <uuid4 or 26-char ULID>`, sent by the SPA on every
  state-changing POST that is not already naturally deduped (apply, discover,
  upload, generate, submit-input, approve).
* Server stores `(user_id, scope, key) → {status_code, response_body, created_at}`
  for **24 h** in `idempotency_keys`.
* A repeat with the **same key and same body** replays the stored response with
  status `200` and header `Idempotency-Replayed: true`.
* A repeat with the **same key and a different body** is
  `409 idempotency_conflict` — never "run it again with new inputs".
* A request still in flight under that key is `409 idempotency_conflict` with
  `retry_after`, because two concurrent executions of one key is exactly the
  double-submit this prevents.
* Keys are per tenant: `UNIQUE(user_id, scope, key)`.

### 8.2 Natural dedupe keys (domain-level, server-generated)

Already implemented by `pipeline_jobs.dedupe_key` (`UNIQUE(user_id,
dedupe_key)`); the contract fixes the *formats* so two engineers cannot invent
two:

| Work | `dedupe_key` format | Notes |
|---|---|---|
| discovery run | `discovery:{persona_id}:{keywords_hash}:{freshness_hours}:{cycle_bucket}` | `keywords_hash = sha256(",".join(sorted(set(keywords))))[:16]`; `cycle_bucket = epoch // freshness_window` so a run is at most one per window per persona. The shipped key uses the first five raw keywords in arrival order, which makes `["a","b"]` and `["b","a"]` different work — that is the bug this format removes. |
| application run | `apply:{job_id}` | one live application run per job (shipped). |
| application submit | `submit:{application_id}:{attempt}` | **at-most-once submission** — see [07 §7](07-application-state-machine.md). |
| resume generation | `generate_resume:{job_id}:{persona_id}:{strict}` | shipped. |
| resume tagging | `tag_resume:{resume_id}` | shipped. |
| extraction | `extract:{resume_document_id}:{extractor_version}` | re-running the same extractor on the same document is a no-op. |
| outreach send | `email:{email_id}` | shipped. |
| funding scan | `funding:{persona_id}:{cycle_bucket}` | |

Job-row identity (discovery idempotency at the data level) is
`UNIQUE(user_id, dedupe_key)` where `dedupe_key = lower(source:external_id)` or
`lower(company_name_normalized:title_normalized)` — shipped — **plus** a stored
`content_hash` (`sha256` of normalised title + company + first 4000 chars of the
description) used to detect the *same posting from a different source*. A run
that re-finds an existing posting increments `report.duplicates_skipped` and
inserts nothing; that counter is part of the run report
([13 §3](13-api-response-shapes.md)).

---

## 9. Unknown values (forward compatibility)

Vocabularies grow. Therefore:

* **Backend**: writing an unknown value is a bug (assert against the
  vocabulary). *Reading* one must not crash — a row written by a newer version
  during a rolling deploy is legal.
* **Frontend**: an unknown `state`/`kind`/`status`/`code` renders the raw
  string in a neutral chip. It must never render blank, never throw, and never
  be silently mapped to a default that means something else. `lib/aiWork.ts`'s
  final fallback branch is the pattern.
* **Consumers of events**: ignore unknown `event_type`s (they are additive);
  never treat an unknown type as an error.

---

## 10. The server is the source of truth

The rule that produced the v2.2.8 refactor (application state moved out of React
component state into `pipeline_jobs.result`), stated as a contract:

1. Every status the user can see is **readable** from a GET endpoint after a
   refresh, on any route, on any device. If a state exists only in a component,
   a `useState`, or a `sessionStorage` blob, it does not exist.
2. Browser storage is allowed for exactly two things: an *optimistic hint*
   (`lib/aiWork.ts` `track()` — an id used to find the server row faster) and
   presentation preferences (theme). Both must be safe to lose.
3. Every long-running operation returns a **receipt** with the durable row id
   (`pipeline_job_id`), and the row carries the final `result` — so the outcome
   survives the request that produced it.
4. Derived UI state is a **pure function of the server rows**
   (`deriveApplyState(row)`), recomputed on every render, never stored.
5. Nothing that gates an action may be decided client-side: consents,
   entitlements, plan limits, automation mode, sensitivity and review status are
   all re-checked server-side on the mutating call, because a queued intent can
   outlive the consent it was created under
   (`apply_flow.execute_application` re-reads the consent chain at run time).
6. File uploads are resumable/restartable through the queue, not through a
   browser-side progress object: the shipped in-memory `_upload_progress` map is
   a *live view* of a request in flight and is explicitly not a record — the
   durable record is the `resume_extractions` row
   ([03](03-resume-and-extraction.md)).

---

## 11. Resumability (summary — detail in [11](11-background-jobs.md))

An operation is resumable when **all four** hold:

1. **Checkpointed** — after each unit of work it persists where it got to
   (`pipeline_jobs.payload.checkpoint`), so a restart continues instead of
   restarting.
2. **Idempotent** — re-executing a completed unit of work has no additional
   effect (dedupe key, unique constraint, or an upsert).
3. **Leased** — exactly one worker owns it at a time
   (`lease_expires_at` + `locked_by`), and the reaper uses a CAS update with a
   safety margin so a slow job is never cloned.
4. **Bounded** — separate budgets for failures (`attempts`/`max_attempts`),
   outages (`paused_count`/`AI_PAUSE_MAX`) and crash reclaims
   (`reclaim_count`/`worker_max_reclaims`), each with a dead-letter outcome.

Side effects are created **after** the expensive step succeeds, or upserted by
unique key — that is what makes `pause → resume` free and safe
(`app/services/job_queue.py` module docstring).

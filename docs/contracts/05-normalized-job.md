# 05 — Normalized job schema

**Deliverable 4.** One posting shape that every source adapter produces, every
consumer reads, and every report aggregates — plus the **discovery run** as a
first-class, resumable entity instead of a JSON blob inside a queue payload.

Shipped today: `app/services/sources/base.Posting` (adapter output), the `jobs`
table (persisted output), and the run report written into
`pipeline_jobs.payload.result` by `app/services/discovery.discover_for_user`.
This contract keeps all three and makes the implicit rules explicit.

---

## 1. Three layers, one shape

| Layer | Object | Owner | Lifetime |
|---|---|---|---|
| **Adapter output** | `Posting` (dataclass) | `app/services/sources/adapters.py` | one fetch; never stored as-is |
| **Normalized posting** | the JSON in §2 | `app/services/discovery.py` | the run report + the persisted row |
| **Persisted job** | `jobs` row (§3) | `app/services/discovery.py` (create), application service (status) | life of the board |

An adapter that cannot fill a field leaves it empty — it never guesses, and it
never invents a `posted_at` (a missing `posted_at` means "freshness unknown", and
`discovery` treats it as inside the window rather than as "now").

---

## 2. The normalized posting (adapter → discovery)

```json
{
  "identity": {
    "source_id": "greenhouse",
    "source_kind": "api",
    "external_id": "4123456007",
    "url": "https://boards.greenhouse.io/techjobs/jobs/4123456007",
    "fetched_at": "2026-09-18T14:03:22Z",
    "adapter_version": "1.7.0",
    "dedupe_key": "greenhouse:4123456007",
    "content_hash": "9f2c…"
  },
  "role": {
    "title": "Senior Embedded Engineer",
    "title_normalized": "senior embedded engineer",
    "seniority": "senior",
    "employment_type": "full_time",
    "categories": ["embedded", "firmware"],
    "skills_required": ["C", "AUTOSAR"],
    "skills_preferred": ["Rust"],
    "years_min": 5,
    "years_max": null,
    "education_required": "bachelors"
  },
  "company": {
    "name": "Wirelane GmbH",
    "name_normalized": "wirelane",
    "website": "https://wirelane.energy",
    "industry": "energy",
    "size": "medium",
    "size_confidence": 0.72,
    "size_source": "provider_api",
    "funding_stage": "series_a"
  },
  "location": {
    "raw": "Berlin, Germany (Hybrid)",
    "city": "Berlin",
    "region": "Berlin",
    "country": "Germany",
    "country_code": "DE",
    "workplace": "hybrid",
    "remote_allowed": true,
    "timezones": ["Europe/Berlin"],
    "confidence": 0.9
  },
  "compensation": {
    "raw": "€75,000 – €95,000",
    "currency": "EUR",
    "period": "year",
    "min": 75000,
    "max": 95000,
    "transparency": "disclosed",
    "equity": null
  },
  "description": {
    "text": "…",
    "text_sha256": "…",
    "jd_hash": "…",
    "language": "en",
    "truncated": false
  },
  "timing": {
    "posted_at": "2026-09-17T09:00:00Z",
    "age_hours": 29,
    "expires_at": null,
    "freshness_relaxed": false
  },
  "portal": {
    "type": "greenhouse",
    "domain": "boards.greenhouse.io",
    "requires_login": false,
    "detection_source": "ats_api",
    "detected_at": "2026-09-18T14:03:22Z",
    "confidence": 0.95
  },
  "flags": { "is_demo": false, "is_synthetic": false, "requires_visa_sponsorship_unknown": true }
}
```

Field rules that adapters must follow:

| Field | Rule |
|---|---|
| `identity.dedupe_key` | `lower("{source_id}:{external_id}")` when the source has a stable id, else `lower("{company.name_normalized}:{role.title_normalized}")` — `Posting.dedupe_key()` already does this. Max 280 chars. |
| `identity.content_hash` | `sha256(title_normalized + "\n" + company.name_normalized + "\n" + description.text[:4000])`. Cross-source duplicate detection; **not** part of the unique constraint (two boards legitimately list the same role). |
| `role.seniority` | `intern` \| `junior` \| `mid` \| `senior` \| `lead` \| `principal` \| `unknown` — derived from the title by `classifier`, never from the description's tone. |
| `role.employment_type` | `full_time` \| `part_time` \| `contract` \| `internship` \| `temporary` \| `freelance` \| `unknown`. |
| `company.name_normalized` | `app/services/company_normalize.normalize_company_name` — lowercase, punctuation stripped, legal-form suffixes dropped. This is the identity used for the SQL-side `?company=` filter and for funding linkage; `company.name` stays the display value. |
| `location.workplace` | `onsite` \| `hybrid` \| `remote` \| `unspecified`. Derived from `raw` + the source's own remote flag; when a source says "Remote" and the text says "Berlin office 3 days/week", the answer is `hybrid` and `confidence` drops. |
| `compensation.transparency` | `disclosed` (numbers in the posting) \| `range_only` \| `estimated` (we computed it) \| `unknown`. **An estimate is never stored as `disclosed`.** |
| `compensation.min/max` | integer minor units? No — integer **major** units with an explicit `currency` and `period` (`year` \| `month` \| `hour` \| `day`). A range with no currency is `raw` only. |
| `timing.posted_at` | `null` when the source does not say. Never "now". |
| `flags.is_demo` | `true` only for the opt-in demo pool (`INCLUDE_DEMO_POOL`); such rows must render `DEMO DATA` and are excluded from reports unless asked for. |

---

## 3. `jobs` — the persisted row

Existing columns are kept (marked *shipped*); new ones are additive.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | *shipped* |
| `user_id` | FK, index | no | tenant — *shipped* |
| `persona_id` | FK `personas.id` | yes | the track this row belongs to — *shipped* |
| `title`, `company`, `location`, `description`, `url` | String/Text | per today | *shipped* — display values |
| `company_name_normalized` | String(200) | no | SQL-side company identity — *shipped* |
| `title_normalized` | String(300) | no | **new** — for dedupe fallback and grouping |
| `source` | String(60) | no | adapter id or demo pool id — *shipped* |
| `source_kind` | String(16) | no | **new** — `api` \| `board` \| `rss` \| `partner` \| `gated` \| `demo` \| `imported` |
| `external_id` | String(200) | yes | *shipped* |
| `dedupe_key` | String(300) | no | `UNIQUE(user_id, dedupe_key)` — *shipped* |
| `content_hash` | String(64) | no | **new** — cross-source duplicate signal, indexed `(user_id, content_hash)` |
| `status` | String(24) | no | the legacy board status, a **projection** of `applications.state` ([07 §5](07-application-state-machine.md)). Values: `JOB_STATUSES`. *shipped column, corrected vocabulary* |
| `score`, `score_reason`, `score_source`, `score_detail` | | | *shipped* — the **latest** match, denormalised for list rendering. The authoritative record is `match_results` ([06](06-match-result.md)). |
| `company_size`, `company_info` | | | *shipped* |
| `role` | JSON | no | **new** — `{seniority, employment_type, categories[], skills_required[], skills_preferred[], years_min, years_max, education_required}` |
| `location_detail` | JSON | no | **new** — §2 `location` object |
| `compensation` | JSON | no | **new** — §2 `compensation` object (today it is `extra.salary`, a string) |
| `portal` | JSON | no | **new** — §2 `portal` object (today it lives in `extra.forms`) |
| `provenance` | JSON | no | **new** — `{source_id, source_kind, adapter_version, fetched_at, external_id, url, raw_payload_ref, is_demo}` |
| `description_sha256` | String(64) | no | **new** — "did the JD change?" ([06 §5](06-match-result.md)) |
| `jd_hash` | String(64) | yes | **new** — the hash resume-reuse already compares (`scoring.jd_similarity` inputs) |
| `discovery_run_id` | FK `discovery_runs.id` | yes | **new** — which run put it on the board |
| `first_seen_at` | DateTime | no | **new** — never rewritten (today `discovered_at` plays this role) |
| `last_seen_at` | DateTime | no | **new** — the most recent run that still returned it; the prune clock (the `funding_companies` pattern) |
| `discovered_at`, `posted_at`, `freshness_hours` | | | *shipped* |
| `applied_at` | DateTime | yes | *shipped* — becomes a projection of `applications.submitted_at` |
| `applied_with_resume_id` | FK | yes | *shipped* |
| `extra` | JSON | yes | *shipped* — escape hatch. **Rule**: a key that survives two releases gets a column. `extra.salary`, `extra.remote`, `extra.forms` are the first three to graduate. |
| `error` | Text | yes | *shipped* — last application error, mirrored from `applications.last_error` |

Constraints (new): `INDEX(user_id, persona_id, status)`,
`INDEX(user_id, content_hash)`, `INDEX(user_id, last_seen_at)`,
`UNIQUE(user_id, dedupe_key)` (*shipped*).

**Lifecycle**: created by discovery (only writer for the identity/description
columns); status updated only by the application service; `last_seen_at` updated
by discovery when a run re-finds the row; pruned by a retention job after
`prune_after_days` without being seen *and* with a terminal application state
(never prune a row the user applied to). Deletion is user-initiated or erasure.

---

## 4. `discovery_runs` — the run is an entity

Today the run report lives in `pipeline_jobs.payload.result`, which means: it is
not queryable, not paginated, it grows the payload of every poll
(`discovery_last_run` has to strip `jobs`/`board_tokens` before it can answer),
and a run has no identity an operator can point at. The queue row stays the
*execution* record; the run row is the *result* record.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id` | FK, index | no | tenant |
| `persona_id` | FK | yes | the track |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | the execution row (1:1) |
| `status` | String(20) | no | `DISCOVERY_RUN_STATES`: `queued` \| `running` \| `paused` \| `completed` \| `completed_empty` \| `failed` \| `dead` \| `cancelled` |
| `trigger` | String(16) | no | `QUEUE_TRIGGERS` — `user` click, `auto` scheduler, `retry` |
| `idempotency_key` | String(64) | yes | the client's key, when it sent one ([01 §8](01-conventions.md)) |
| `dedupe_key` | String(300) | no | `discovery:{persona_id}:{keywords_hash}:{freshness_hours}:{cycle_bucket}` |
| `request` | JSON | no | frozen snapshot of the inputs: `{keywords[], freshness_hours, limit, live_enabled, sources[], board_tokens[]}` — `sources: []` and `sources: null` stay distinct (the v2.2.4 rule) |
| `checkpoint` | JSON | no | resumability (§5) |
| `report` | JSON | no | `{started_at, finished_at, elapsed_seconds, scanned, fresh, inserted, duplicates_skipped, cap_clamped, why_empty?, summary{}, ai_rescore{}, jobs_cap{}, insert_errors[], source_results{}}` |
| `why_empty` | String(32) | yes | denormalised from `report.why_empty` so an empty-board query is an index lookup, not a JSON scan. `DISCOVERY_WHY_EMPTY`; `null` when the run added jobs. |
| `job_count` | Integer | no | rows created (denormalised `report.inserted`) |
| `error_code` / `error_message` | | yes | |
| `created_at` / `started_at` / `finished_at` | DateTime | | |

Constraints: `UNIQUE(user_id, dedupe_key)`, `INDEX(user_id, persona_id, id DESC)`,
`INDEX(user_id, status)`.

`report.summary` and `report.ai_rescore` keep exactly the shipped shapes —
`_run_summary()` and the `ai_rescore` block are already the honest version, and
`GET /api/jobs/discovery/last-run` keeps reading them
([13 §3](13-api-response-shapes.md) defines the new paginated read).

---

## 5. Resumability of a discovery run

A run fans out to N sources, scores, classifies, persists and detects forms. Any
of those can die halfway. The contract:

```json
"checkpoint": {
  "step": "persisting",
  "updated_at": "2026-09-18T14:03:22Z",
  "sources_completed": ["greenhouse", "lever"],
  "sources_failed": { "workday": "http_503" },
  "sources_pending": ["ashby", "remotive"],
  "candidates_fetched": 84,
  "candidates_scored": 84,
  "candidates_ai_scored": 12,
  "rows_persisted": 31,
  "rows_skipped_duplicate": 12,
  "cursor": { "ashby": "page:3" }
}
```

Rules:

1. **Checkpoint after each source and after each persisted row batch** (the
   shipped code already commits per row, which is what makes this possible).
2. **Resume skips completed work**: on re-execution, `sources_completed` are not
   re-fetched and `rows_persisted` are not re-inserted (the unique constraint is
   the backstop, and a conflict increments `duplicates_skipped` rather than
   failing the run).
3. **AI spend is not repeated**: `candidates_ai_scored` is compared against the
   persisted rows' `score_source = ai` count, so a paused run resumes into the
   remaining top slice instead of re-scoring the first twelve.
4. **Pause ≠ failure** ([11 §5](11-background-jobs.md)): a transient AI outage
   pauses the run (`status = paused`) with `checkpoint` intact and spends no
   attempt.
5. **Cap clamping is recorded**: when `jobs_max` leaves fewer slots than `limit`,
   `report.cap_clamped = {requested, allowed}` and the run inserts exactly
   `allowed` rows; a full board is `completed_empty` with
   `why_empty = jobs_cap_reached` (shipped behaviour, now named).
6. **Cancellation**: `POST /api/discovery/runs/{id}/cancel` sets
   `status = cancelled` and the queue row to `cancelled`; already-persisted rows
   stay (they are real jobs the user can see).

---

## 6. Discovery run state machine

| From | To | Actor / trigger | Guard | Side effects | Event |
|---|---|---|---|---|---|
| — | `queued` | `user` (`POST /api/jobs/discover`) or `system_scheduler` (auto mode) | entitlements pass; dedupe key free or the existing row is terminal (then it is reused and reset) | `pipeline_jobs` row; receipt with `pipeline_job_id` | `discovery.run_queued`, `job.enqueued` |
| `queued` | `running` | `system_worker` — claim | lease taken | `started_at`; checkpoint initialised | `discovery.run_started`, `job.claimed` |
| `running` | `running` | `system_worker` — per source / per batch | — | checkpoint updated | `discovery.source_fetched` \| `discovery.source_failed`, `job.checkpointed` |
| `running` | `paused` | `system_worker` — transient AI outage | `paused_count < AI_PAUSE_MAX` | queue paused with backoff; checkpoint kept | `job.paused` |
| `paused` | `running` | `system_watchdog` (probe green) or `user` (one-click resume) | — | `drain_paused` | `job.resumed` |
| `running` | `completed` | `system_worker` — finished, ≥1 row inserted | — | jobs persisted, `last_seen_at` refreshed, funding linkage refreshed, `report` written, notifications for auto runs | `discovery.job_persisted`×n, `discovery.run_completed` |
| `running` | `completed_empty` | `system_worker` — finished, 0 rows inserted | `why_empty` classified from the per-source report | `report.why_empty` + `report.summary` written | `discovery.run_empty` |
| `running` | `failed` | `system_worker` — retryable error, attempts remain | `attempts < max_attempts` | queue re-queued with backoff; run row stays `running`→`queued` until the retry claims it | `job.failed` |
| `running`, `paused` | `dead` | `system_worker` / `system_watchdog` — budget exhausted (`attempts`, `paused_count` or `reclaim_count`) | — | `error_code`, `error_message`; notification `automation_failed` for auto runs | `job.dead_lettered` |
| any non-terminal | `cancelled` | `user` — cancel endpoint | — | queue row `cancelled`; persisted rows stay | `discovery.run_cancelled` |

A run never goes `completed → running`: a re-run reuses the queue row (the
shipped `enqueue` behaviour) but creates a **new** `discovery_runs` row with a new
`cycle_bucket`, so history is a list of runs, not one row that keeps changing.

---

## 7. Invariants (tests)

1. `dedupe_key` is unique per user; a second run that re-finds a posting inserts
   nothing and increments `duplicates_skipped`.
2. `content_hash` collisions across sources produce one row plus an alias note in
   `report.duplicates_skipped`, never two board entries for one job.
3. `company_name_normalized == normalize_company_name(company)` on every row
   (the shipped `before_insert` event pattern, extended to `jobs`).
4. `compensation.transparency = disclosed` requires `min` or `max` **and** a
   `currency`.
5. `flags.is_demo = true` rows are excluded from `report.inserted` counts shown to
   users unless demo data is enabled, and are always labelled.
6. `last_seen_at ≥ first_seen_at`; `first_seen_at` is never updated.
7. Every `jobs` row has a `discovery_run_id` or an explicit
   `provenance.source_kind ∈ {imported, demo}`.
8. A run's `status` and its queue row's `status` agree under the mapping
   (`done`→`completed`/`completed_empty`, `dead`→`dead`, `paused`→`paused`).
9. `why_empty` is present **only** when `job_count = 0` (an absent key means the
   run found jobs — the shipped rule, now a test).

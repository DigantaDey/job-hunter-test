# 13 — API response shapes

**Deliverable 11.** Six reads, specified to the field. Every shape obeys
[01-conventions.md](01-conventions.md): `snake_case`, ISO-8601 UTC with an
explicit offset, typed error codes, tenant scoping from the credential, and
unknown vocabulary values rendered by the client rather than rejected.

All six are **proposed** except where a shipped endpoint is named; where a shipped
endpoint exists, the new keys are additive and the old ones stay for one minor
version ([14 §6](14-migration-strategy.md)).

Common to all: `GET`, bearer token or API key, `200` on success, and the standard
error envelope on failure. Every response that can change while the user looks at
it carries `server_time` so a client can compute ages without trusting its own
clock.

---

## 1. Onboarding status

### `GET /api/onboarding/status`

```json
{
  "session_id": 12,
  "state": "profile_review_required",
  "state_label": "Review your profile",
  "state_since": "2026-09-18T14:03:22Z",
  "progress": {
    "gates_required": 5,
    "gates_completed": 3,
    "gates_skipped": 0,
    "percent": 60
  },
  "checklist": [
    { "key": "account", "label": "Account created", "required": true, "blocking": true,
      "status": "completed", "completed_at": "2026-09-18T13:50:01Z", "evidence": {} },
    { "key": "consents", "label": "Terms & data processing accepted", "required": true,
      "blocking": true, "status": "completed", "completed_at": "2026-09-18T13:51:12Z",
      "evidence": { "terms": "2026-09-18T13:51:12Z", "data_processing": "2026-09-18T13:51:12Z" } },
    { "key": "resume", "label": "Master resume uploaded & extracted", "required": true,
      "blocking": true, "status": "completed", "completed_at": "2026-09-18T14:02:40Z",
      "evidence": { "resume_document_id": 41, "extraction_id": 7, "field_count": 38 } },
    { "key": "profile_review", "label": "Extracted profile reviewed", "required": true,
      "blocking": true, "status": "in_progress", "completed_at": null,
      "evidence": { "profile_id": 9, "required": 6, "resolved": 2, "deferred": 0 } },
    { "key": "preferences", "label": "Search & compensation preferences", "required": true,
      "blocking": true, "status": "pending", "completed_at": null, "evidence": {} },
    { "key": "discovery_setup", "label": "Job sources chosen", "required": false,
      "blocking": false, "status": "pending", "completed_at": null, "evidence": {} },
    { "key": "automation_policy", "label": "Automation mode chosen", "required": false,
      "blocking": false, "status": "pending", "completed_at": null, "evidence": {} }
  ],
  "blocked": {
    "code": null, "message": null, "retryable": false, "retry_after": null, "since": null
  },
  "next_action": {
    "gate": "profile_review",
    "label": "Review 4 extracted fields",
    "route": "/profile/review",
    "method": "GET",
    "entity": { "type": "profile", "id": 9 }
  },
  "live_work": {
    "pipeline_job_id": 918,
    "status": "processing",
    "progress": { "step": "extracting_profile", "percent": 55, "message": "Contacting the model…" }
  },
  "consents": {
    "terms": "2026-09-18T13:51:12Z",
    "data_processing": "2026-09-18T13:51:12Z",
    "automation": null,
    "outreach": null
  },
  "completed_at": null,
  "abandoned_at": null,
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `state` | `ONBOARDING_STATES` | derived server-side on every read ([04 §1](04-onboarding-state-machine.md)) |
| `state_label` | string | the shipped-label convention: the server supplies copy so two clients cannot word it differently |
| `checklist[].status` | `ONBOARDING_GATE_STATES` | `pending` \| `in_progress` \| `blocked` \| `completed` \| `skipped` |
| `checklist[].evidence` | object | gate-specific; empty `{}` when there is none. Never contains a sensitive value |
| `blocked.code` | `ERROR_CODES`-adjacent list from [04 §7](04-onboarding-state-machine.md) | `null` when not blocked — **not** an empty string |
| `next_action` | object \| null | the single thing to do next; `null` only in `ready`/`abandoned` |
| `live_work` | object \| null | the in-flight queue row, so the SPA attaches to real progress instead of a spinner of its own |
| `consents` | object | timestamps or `null` — mirrors `GET /api/account/consent` |

Errors: `404 onboarding_not_started` (no session row and no user — cannot happen
for an authenticated caller, but defined), `401 not_authenticated`.

### The writes

| Endpoint | Body | Returns |
|---|---|---|
| `POST /api/onboarding/retry` | `{gate?: string}` | the whole status document; `202` when it enqueued work |
| `POST /api/onboarding/skip` | `{gate: string, reason?: string}` | the whole status document; `422 gate_not_satisfied` if the gate is `required` |
| `PUT /api/onboarding/preferences` | the `preferences`/`compensation`/`availability`/`eligibility` objects from [02 §2.2](02-candidate-profile.md) | the whole status document + the new `profile_id` |
| `POST /api/onboarding/abandon` | — | the whole status document, `state = abandoned` |
| `POST /api/onboarding/resume` | — | the whole status document, state re-derived |

Every write returns the **full** document. A partial answer forces the client to
merge, and merging is how a browser becomes the source of truth.

---

## 2. Profile review

### `GET /api/profile/review?status=needs_review&sensitivity=sensitive&limit=50`

```json
{
  "profile_id": 9,
  "profile_version": 2,
  "state": "review_required",
  "summary": { "required": 6, "resolved": 2, "deferred": 0, "remaining": 4,
               "by_sensitivity": { "public": 0, "internal": 2, "sensitive": 4, "restricted": 0 } },
  "fields": [
    {
      "provenance_id": 301,
      "path": "/contact/phone",
      "field_key": "phone",
      "label": "Phone number",
      "value": null,
      "value_masked": "+49 ••• •••• 118",
      "value_type": "string",
      "sensitivity": "sensitive",
      "origin": "resume_extraction",
      "confidence": 0.62,
      "band": "medium",
      "review_status": "needs_review",
      "review_required": true,
      "ambiguous": true,
      "ambiguity": "ambiguous_value",
      "candidates": [
        { "value": "+49 151 0000 118", "label": "From your resume, page 1",
          "origin": "resume_extraction", "confidence": 0.62, "band": "medium",
          "evidence": [ { "kind": "text_span",
                          "quote": "Berlin · +49 151 0000 118 · diganta@example.com",
                          "locator": { "document_id": 41, "page": 1, "start": 88, "end": 141 } } ] },
        { "value": "+49 151 0000 118", "label": "Repeated in the footer",
          "origin": "resume_extraction", "confidence": 0.55, "band": "low",
          "evidence": [ { "kind": "text_span", "quote": "+49 151 0000 118",
                          "locator": { "document_id": 41, "page": 2, "start": 12, "end": 30 } } ] }
      ],
      "evidence": [ { "kind": "text_span", "quote": "…", "locator": { "document_id": 41, "page": 1, "start": 88, "end": 141 } } ],
      "extractor": { "name": "ai_profile_extractor", "version": "1.4.0",
                     "model": "gpt-4o-mini", "prompt_version": "profile-2026-08-30" },
      "used_by": ["phone", "applications:3"],
      "computed_at": "2026-09-18T14:02:40Z",
      "reviewed_at": null
    }
  ],
  "page": 1, "page_size": 50, "total": 4, "has_more": false,
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Notes |
|---|---|
| `fields[].value` | present **only** when `sensitivity ∈ {public, internal}`; otherwise `null` and `value_masked` carries the display form ([01 §7](01-conventions.md)) |
| `fields[].path` | RFC 6901 pointer into `candidate_profiles.document` — the client edits by path, never by guessing a field name |
| `fields[].candidates` | present when `ambiguous = true`; ordered best first; each carries its own provenance and evidence |
| `fields[].used_by` | canonical application field keys and live application ids that depend on this value — so the user can see that correcting it re-blocks an application ([08 §4](08-application-fields.md) F10) |
| `summary` | the counters the onboarding gate reads; always present, even with `fields: []` |

### `POST /api/profile/review/resolve`

```json
{
  "resolutions": [
    { "provenance_id": 301, "action": "confirm", "candidate_index": 0 },
    { "provenance_id": 302, "action": "correct", "value": "+49 151 0000 999" },
    { "provenance_id": 303, "action": "reject" },
    { "provenance_id": 304, "action": "defer" }
  ],
  "idempotency_key": "3f2a…"
}
```

`action` ∈ `REVIEW_ACTIONS`: `confirm` | `correct` | `reject` | `defer`. Response: `200` with

```json
{
  "resolved": 3, "deferred": 1, "rejected": 1,
  "profile_id": 9, "profile_state": "active",
  "remaining": 0,
  "invalidated_answers": [ { "application_id": 77, "field_key": "phone", "reason": "profile_changed" } ],
  "events": [ "profile.field_confirmed", "profile.field_corrected", "profile.review_completed" ],
  "onboarding": { … the full §1 document … }
}
```

Errors: `422 restricted_field` when `action = defer` on a `restricted` path;
`422 field_needs_review` when a value that must be confirmed was not;
`409 idempotency_conflict`; `404` for another tenant's `provenance_id`.

`invalidated_answers` is the honest consequence of a correction: the client must
show that an application went back to `blocked_input`, not discover it later.

---

## 3. Discovery runs

### `GET /api/discovery/runs?persona_id=&status=&limit=20&after_id=`

```json
{
  "items": [
    {
      "run_id": 55,
      "pipeline_job_id": 902,
      "status": "completed",
      "trigger": "user",
      "persona_id": 3,
      "persona_name": "Data Analyst",
      "request": {
        "keywords": ["data analyst", "sql", "python"],
        "freshness_hours": 72,
        "limit": 40,
        "live_enabled": true,
        "sources": ["greenhouse", "lever", "remotive"],
        "sources_explicit": true
      },
      "report": {
        "started_at": "2026-09-18T13:00:00Z",
        "finished_at": "2026-09-18T13:01:12Z",
        "elapsed_seconds": 72.4,
        "scanned": 84, "fresh": 61, "inserted": 31,
        "duplicates_skipped": 12,
        "cap_clamped": null,
        "why_empty": null,
        "summary": {
          "configured_sources": ["greenhouse", "lever", "remotive"],
          "failed_sources": {},
          "needs_credentials": ["smartrecruiters"],
          "demo_pool_enabled": false,
          "freshness_window_hours": 72,
          "fetched": 84, "fresh": 61
        },
        "ai_rescore": { "enabled": true, "skipped": null, "scored": 12 },
        "jobs_cap": { "used": 341, "limit": 1000 },
        "insert_errors": []
      },
      "checkpoint": { "step": "done", "sources_completed": ["greenhouse", "lever", "remotive"],
                      "sources_failed": {}, "rows_persisted": 31 },
      "progress": { "step": "done", "percent": 100 },
      "top_jobs": [
        { "id": 412, "title": "Senior Data Analyst", "company": "Wirelane GmbH",
          "score": 88, "score_source": "ai", "band": "strong", "url": "https://…" }
      ],
      "error": null,
      "created_at": "2026-09-18T13:00:00Z"
    }
  ],
  "page": 1, "page_size": 20, "total": 55, "has_more": true,
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Notes |
|---|---|
| `status` | `DISCOVERY_RUN_STATES`; `completed_empty` is distinct from `completed` so an empty board is a fact, not a zero |
| `report.why_empty` | **absent or `null` when the run inserted jobs** — the shipped rule; exactly one of `DISCOVERY_WHY_EMPTY` otherwise |
| `report.summary` / `report.ai_rescore` | the shipped `_run_summary()` / `ai_rescore` shapes, unchanged |
| `request.sources_explicit` | `true` when the caller sent a list, `false` when `null` meant "use my configuration" — the unset-vs-empty distinction the v2.2.4 fix introduced, made visible |
| `top_jobs` | ≤ 5 rows, denormalised for the list; the full set is `GET /api/jobs?discovery_run_id=` |
| `checkpoint` | present while the run is resumable; `null` after pruning |

### `GET /api/discovery/runs/{run_id}` — the same object plus `report.jobs[]` (all inserted rows) and the full per-source results.

### `POST /api/jobs/discover` *(shipped, extended)*

Response gains `run_id`, `correlation_id`, `idempotency_key` and `poll`:

```json
{ "queued": true, "duplicate": false, "pipeline_job_id": 902, "run_id": 55,
  "queue_status": "queued", "idempotency_key": "3f2a…",
  "correlation_id": "8f1c…", "dedupe_key": "discovery:3:9ab1…:72:29371",
  "keywords": ["data analyst", "sql", "python"], "freshness_hours": 72,
  "sources": ["greenhouse", "lever", "remotive"], "live_enabled": true,
  "poll": { "endpoint": "/api/queues/ai", "interval_ms": 4000 } }
```

### `GET /api/jobs/discovery/last-run` *(shipped, unchanged)*

Keeps its exact response (`{at, added, why_empty?, summary?, ai_rescore?}`) — the
Jobs page and `useDiscoveryLastRun` read it. It becomes a thin view over the newest
`completed`/`completed_empty` run row instead of a scan of queue payloads, which
is what makes it O(1) instead of O(200 rows).

---

## 4. Job matches

### `GET /api/jobs/{job_id}/matches?history=0`

```json
{
  "job_id": 412,
  "title": "Senior Data Analyst",
  "company": "Wirelane GmbH",
  "persona_id": 3,
  "current": {
    "match_id": 880,
    "score": 88.0,
    "band": "strong",
    "score_source": "ai",
    "score_source_label": "AI verdict",
    "confidence": 0.83,
    "reason": "Strong overlap on SQL, Python and stakeholder-facing analysis; AUTOSAR is not required here.",
    "scorer": "ai", "scorer_version": "2.3.0",
    "model": "gpt-4o-mini", "prompt_version": "score-2026-09-01",
    "profile_id": 9, "profile_version": 2,
    "rubric": {
      "weights": { "skills": 0.35, "experience": 0.25, "seniority": 0.15,
                   "domain": 0.10, "location": 0.10, "education": 0.05 },
      "criteria": [
        { "key": "skills", "label": "Skills", "weight": 0.35, "score": 92, "max": 100,
          "scored": true, "reason": "9 of 11 required technologies are evidenced.",
          "evidence": [ { "kind": "text_span", "quote": "AUTOSAR Classic platform, 3 years",
                          "locator": { "document_id": 41, "page": 1, "start": 2210, "end": 2251 } } ],
          "missing": ["dbt"] },
        { "key": "location", "label": "Location", "weight": 0.10, "score": null,
          "max": 100, "scored": false, "reason": "The posting does not state a workplace policy.",
          "missing": [] }
      ],
      "overall": 88, "recommendation": "strong"
    },
    "matched_skills": [ { "name": "SQL", "required": true, "confidence": 0.95,
                          "evidence": [ { "kind": "text_span", "quote": "…" } ] } ],
    "missing_skills": [ { "name": "dbt", "required": false, "severity": "minor", "remediable": true } ],
    "staleness": "fresh",
    "flags": { "plan_gated": false, "ai_error": null, "upgrade_hint": null },
    "guardrail_report": { "passed": true, "issues": [] },
    "computed_at": "2026-09-18T13:01:05Z",
    "expires_at": "2026-10-02T13:01:05Z"
  },
  "history": [],
  "actions": {
    "rescore": { "allowed": true, "route": "/api/jobs/412/intelligence?refresh=1", "method": "GET",
                 "costs_quota": true },
    "apply": { "allowed": true, "route": "/api/jobs/412/apply", "method": "POST" }
  },
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Notes |
|---|---|
| `current` | `null` when the job has never been scored (`score_source = unscored`) — the client renders "Not scored", never `0` |
| `score_source_label` | server-supplied copy for the provenance chip, so a keyword estimate cannot be rendered as an AI verdict by a lazy client |
| `rubric.criteria[].score = null` | unscored criterion; excluded from the weighted mean and shown as "not stated" ([06 §2](06-match-result.md)) |
| `staleness` | `MATCH_STALENESS`; anything but `fresh` comes with `actions.rescore.allowed = true` |
| `flags.plan_gated` | `true` when the plan withheld the AI scorer — with `upgrade_hint` |
| `history` | only with `?history=1`; newest first; each entry is the `current` shape minus `rubric` |

### `GET /api/matches?persona_id=&min_score=&band=&staleness=&sort=score&limit=50&page=1`

Paginated envelope of `{job_id, title, company, score, band, score_source,
staleness, computed_at, application_state}` — the cross-job "best matches" read.
It joins the denormalised `jobs.score*` columns (one row per job, no N+1) and is
therefore eventually-consistent with `match_results` by exactly one transaction
(the projection is written in the same transaction — [06 §8](06-match-result.md)).

### `GET /api/jobs/{job_id}/intelligence` *(shipped, unchanged)*

Keeps its response (it is the per-job scoring endpoint the SPA already calls, and
its `rubric`/`score_source`/`ai_error`/`upgrade_hint` keys are the origin of the
shape above). It becomes the *writer* of `match_results` rows and returns
`match_id` additively.

---

## 5. Application progress

### `GET /api/applications/{application_id}/progress`

```json
{
  "application_id": 77,
  "job": { "id": 412, "title": "Senior Data Analyst", "company": "Wirelane GmbH", "url": "https://…" },
  "persona_id": 3,
  "state": "blocked_input",
  "state_label": "Needs your input",
  "phase": "blocked",
  "job_status": "needs_input",
  "is_provisional": false,
  "attempt_count": 1,
  "current_attempt": {
    "attempt_id": 141, "attempt": 1, "kind": "prepare", "mode": "prepare",
    "pipeline_job_id": 918, "queue_status": "needs_input",
    "progress": { "step": "awaiting_input", "step_index": 6, "steps_total": 11,
                  "percent": 55, "message": "3 required fields need your answer",
                  "unit": { "done": 12, "total": 15, "label": "fields" },
                  "updated_at": "2026-09-18T14:04:10Z" },
    "attempts": 0, "max_attempts": 3, "paused_count": 0, "reclaim_count": 0,
    "started_at": "2026-09-18T14:03:30Z", "finished_at": null
  },
  "blocked": {
    "code": "missing_required_field",
    "message": "3 required fields need your answer.",
    "fields": [
      { "field_key": "salaryExpectation", "field_label": "Expected compensation",
        "field_type": "text", "required": true, "sensitivity": "sensitive",
        "ambiguity": "ambiguous_value", "resolution": "unresolved",
        "candidates": [ { "value": "85000", "label": "Your stated target (confirmed)",
                          "band": "high", "origin": "user_confirmed" } ] },
      { "field_key": "workAuthorization", "field_label": "Are you authorized to work in Germany?",
        "field_type": "select", "required": true, "sensitivity": "sensitive",
        "ambiguity": "ambiguous_option", "resolution": "unresolved",
        "options": ["Yes", "No", "Yes, but I require sponsorship"],
        "candidates": [ { "value": "citizen", "label": "Your profile says: citizen",
                          "band": "medium", "origin": "user_confirmed",
                          "warning": "No portal option matches this exactly." } ] },
      { "field_key": "raw:how_did_you_hear", "field_label": "How did you hear about us?",
        "field_type": "textarea", "required": false, "sensitivity": "internal",
        "ambiguity": "missing", "resolution": "unresolved", "candidates": [] }
    ],
    "input_request_id": 12,
    "retry_after": null,
    "consent": null,
    "credential_id": null,
    "policy_id": 5
  },
  "plan": {
    "portal_type": "greenhouse", "portal_domain": "boards.greenhouse.io",
    "requires_login": false, "detection_source": "html", "detection_confidence": 0.94,
    "fields_total": 15, "fields_mapped": 12,
    "missing_required_count": 2, "ambiguous_count": 2,
    "resume": { "resume_document_id": 58, "role": "tailored", "state": "approved",
                "display_name": "Diganta-Dey-Wirelane-Data-Analyst.pdf",
                "decision": "generated",
                "download": { "pdf": "/api/resumes/58/download-url?format=pdf" } },
    "dry_run": true,
    "generated_at": "2026-09-18T14:04:05Z"
  },
  "timeline": [
    { "sequence": 1, "event_type": "application.created", "state_to": "queued",
      "actor_type": "user", "severity": "info", "message": "Application created from the Jobs board.",
      "occurred_at": "2026-09-18T14:03:22Z" },
    { "sequence": 6, "event_type": "application.input_requested", "state_to": "blocked_input",
      "actor_type": "system_worker", "severity": "warning",
      "message": "2 required fields need your input: Expected compensation, Work authorization.",
      "occurred_at": "2026-09-18T14:04:10Z" }
  ],
  "actions": [
    { "key": "answer", "label": "Answer 2 fields", "method": "POST",
      "route": "/api/applications/77/answers", "primary": true },
    { "key": "cancel", "label": "Cancel application", "method": "POST",
      "route": "/api/applications/77/cancel", "primary": false,
      "disabled": false, "disabled_reason": null },
    { "key": "mark_applied", "label": "I applied manually", "method": "POST",
      "route": "/api/jobs/412/mark-applied", "primary": false }
  ],
  "timestamps": { "queued_at": "2026-09-18T14:03:22Z", "prepared_at": null,
                  "approved_at": null, "submitted_at": null, "outcome_at": null,
                  "expires_at": "2026-10-02T14:03:22Z" },
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Notes |
|---|---|
| `state` / `phase` / `job_status` | the canonical state, its phase, and the legacy board projection — all three, so a client migrating off `jobs.status` can compare them |
| `current_attempt` | the execution view; `queue_status` is the `pipeline_jobs.status`, which is **not** the outcome ([11 §2](11-background-jobs.md)) |
| `blocked` | present exactly when `phase = blocked`; `fields` are the actionable ones only (required or ambiguous) |
| `blocked.fields[].candidates[].warning` | set for `ambiguous_option` — the pre-selection is a guess and must look like one |
| `plan.dry_run` | `true` unless a real submission happened; the UI must not say "applied" when this is `true` |
| `timeline` | the last ≤ 20 events, oldest first; paginate with `GET /api/applications/{id}/events?after_sequence=` |
| `actions[]` | server-computed, with `disabled`/`disabled_reason` — the client renders buttons it does not have to reason about (this is where "may I cancel a submission in flight?" lives) |
| `is_provisional` | `true` for an inferred outcome; the UI must show "we think" and offer confirm/reject |

Related writes (all return this document):

| Endpoint | Body |
|---|---|
| `POST /api/applications/{id}/answers` | `{answers: {field_name: value \| {candidate_index: n}}, reuse_scopes: {field_name: scope}, idempotency_key}` |
| `POST /api/applications/{id}/submit` | `{idempotency_key}` — CAS-guarded, at-most-once ([07 §7](07-application-state-machine.md)) |
| `POST /api/applications/{id}/confirm` | `{external_receipt_id?, note?}` — resolves `submitted_unverified` |
| `POST /api/applications/{id}/outcome` | `{outcome, occurred_at?, evidence?}` — `interview_scheduled` \| `rejected_by_employer` \| `offer_received` |
| `POST /api/applications/{id}/cancel` | `{reason?}` |
| `POST /api/applications/{id}/override` | `{blocker, reason}` — lifts a `blocked_policy` with an explicit user decision |

Errors: `404 application_not_found`, `409 application_already_submitted`,
`409 application_not_submittable`, `403 consent_required`, `402 upgrade_required`,
`429 quota_exhausted`, `422 restricted_field`.

Compatibility: `POST /api/jobs/{id}/apply`, `/input`, `/retry`, `/skip`,
`/mark-applied` and `GET /api/jobs/{id}/events` keep their shipped shapes and
gains `application_id` additively.

---

## 6. User-action-required states

One read that answers *"what is waiting for me?"* across every aggregate. Today
this is four reads (`/api/user-input-queue`, `/api/notifications?unread_only`,
`/api/resumes` filtered by `pending`, `/api/automation`) and the client has to
merge them — which is exactly how something gets forgotten.

### `GET /api/actions/required?kind=&limit=50`

```json
{
  "count": 4,
  "by_kind": { "application_input": 1, "resume_approval": 1, "consent": 1, "profile_review": 1 },
  "oldest_at": "2026-09-17T09:12:00Z",
  "items": [
    {
      "action_id": "app:77:input",
      "kind": "application_input",
      "severity": "action_required",
      "title": "2 fields need your answer",
      "detail": "Wirelane GmbH — Senior Data Analyst: Expected compensation, Work authorization.",
      "entity": { "type": "application", "id": 77 },
      "job": { "id": 412, "title": "Senior Data Analyst", "company": "Wirelane GmbH" },
      "state": "blocked_input",
      "blocked_code": "missing_required_field",
      "route": "/applications/77",
      "primary_action": { "label": "Answer", "method": "GET", "route": "/api/applications/77/progress" },
      "alternate_actions": [ { "label": "Cancel application", "method": "POST", "route": "/api/applications/77/cancel" } ],
      "fields": [ { "field_key": "salaryExpectation", "field_label": "Expected compensation",
                    "sensitivity": "sensitive", "ambiguity": "ambiguous_value" } ],
      "created_at": "2026-09-18T14:04:10Z",
      "expires_at": "2026-10-02T14:03:22Z",
      "notification_id": 512,
      "pipeline_job_id": 918
    },
    {
      "action_id": "resume:58:approval",
      "kind": "resume_approval",
      "severity": "action_required",
      "title": "A tailored resume needs your approval",
      "detail": "Diganta-Dey-Wirelane-Data-Analyst.pdf — the fact guard passed.",
      "entity": { "type": "resume_document", "id": 58 },
      "job": { "id": 412, "title": "Senior Data Analyst", "company": "Wirelane GmbH" },
      "state": "pending_approval",
      "route": "/resumes",
      "primary_action": { "label": "Review & approve", "method": "GET", "route": "/api/resumes/58/preview" },
      "alternate_actions": [ { "label": "Reject", "method": "POST", "route": "/api/resumes/58/reject" } ],
      "created_at": "2026-09-18T14:03:55Z", "expires_at": null,
      "notification_id": 511, "pipeline_job_id": 918
    },
    {
      "action_id": "consent:automation",
      "kind": "consent",
      "severity": "action_required",
      "title": "Automation needs your consent",
      "detail": "1 application is paused until you accept the automation disclosure.",
      "entity": { "type": "user", "id": 1 },
      "state": "blocked_consent",
      "blocked_code": "consent_automation_missing",
      "route": "/account?tab=consents",
      "primary_action": { "label": "Review disclosure", "method": "GET", "route": "/api/account/disclosures" },
      "created_at": "2026-09-17T09:12:00Z", "expires_at": null
    },
    {
      "action_id": "profile:9:review",
      "kind": "profile_review",
      "severity": "action_required",
      "title": "4 extracted fields need review",
      "detail": "Phone, email, expected salary and work authorization came from your resume.",
      "entity": { "type": "profile", "id": 9 },
      "state": "review_required",
      "route": "/profile/review",
      "primary_action": { "label": "Review fields", "method": "GET", "route": "/api/profile/review" },
      "created_at": "2026-09-18T14:02:40Z", "expires_at": null
    }
  ],
  "resolved_recently": [
    { "action_id": "app:76:input", "kind": "application_input",
      "resolved_at": "2026-09-18T13:40:00Z", "resolution": "answered" }
  ],
  "server_time": "2026-09-18T14:05:00Z"
}
```

| Field | Notes |
|---|---|
| `action_id` | a **stable, derived** id (`<kind>:<entity_id>[:<sub>]`) — the client can diff two reads without storing anything |
| `kind` | `ACTION_KINDS`: `application_input` \| `resume_approval` \| `consent` \| `profile_review` \| `credential` \| `policy_override` \| `submission_confirm` \| `onboarding_gate` \| `quota` \| `security` |
| `severity` | `action_required` for everything here except `quota`/`security`, which carry their own |
| `state` / `blocked_code` | the canonical state of the entity that is waiting, from the vocabulary — never a message the client has to parse |
| `route` | SPA route; `primary_action.route` | API endpoint. Both, because the badge navigates and the button acts. |
| `fields` | only for `application_input`/`profile_review`; names and labels, never values |
| `notification_id` | the notification that announced it, so marking one read can mark the other |
| `expires_at` | when the ask goes away by itself (an application expiring) |
| `resolved_recently` | ≤ 10 items resolved in the last 30 min — the same window as the queue's `recent`, so a page that refreshes after answering sees the resolution rather than a silent disappearance |

Sources, in one query each (no client-side merge):
`applications` where `phase = blocked` or `state = awaiting_approval` or
`is_provisional = true`; `resume_documents` where `state = pending_approval`;
`candidate_profiles` where `state = review_required`; `users.consents` where a
required disclosure is missing **and** something is waiting on it;
`automation_policies` where an explicit choice is owed; entitlement rows where a
quota is exhausted with work parked behind it.

Ordering: `severity` (`action_required` first), then `created_at` ascending —
oldest debt first, because a new ask is visible anyway.

Errors: `401` only. An empty answer is `{count: 0, items: []}` — never a `404`.

Compatibility: `GET /api/user-input-queue` (shipped) keeps its shape and becomes
the `application_input` subset of this read.

---

## 7. Cross-cutting response rules

1. **Every document is self-contained.** A response that describes a state
   includes the labels, the actions and the routes needed to render and resolve
   it. A client must not need a second call to know what to show.
2. **Null vs absent.** `null` = "this did not happen"; absent = "this contract
   version does not carry it". Both are meaningful; `""` and `0` are never used
   for either.
3. **Labels come from the server** (`state_label`, `score_source_label`) so two
   clients cannot word the same fact differently — but the raw value is always
   present too, so an unknown value is still renderable ([01 §9](01-conventions.md)).
4. **No secrets, no restricted values** anywhere in these payloads; masked forms
   only, with `masked: true` where a client might otherwise render the mask as a
   value.
5. **Counts are denormalisations** and are recomputed on write; when a client
   needs certainty it reads the list.
6. **`server_time` on every polling response.**
7. **Write endpoints return the document they changed**, plus the events they
   emitted, so the client can update without a re-read — and a re-read must
   produce the same document (read-after-write consistency within a tenant).

---

## 8. Application tracking *(shipped, v2.2.22)*

State machine, origins and rules: [07 §11](07-application-state-machine.md).
Every endpoint is under `/api/application-tracking` and scoped to the caller;
another tenant's record is `404`.

### `GET /api/application-tracking/{tracking_id}?timeline_limit=50`

The document. Self-contained by rule 1 of §7: labels, transitions and actions
arrive with it, so the page renders and acts without a second call.

```jsonc
{
  "tracking_id": 1, "job_id": 1,
  "job": { "id": 1, "title": "Senior Backend Engineer", "company": "FinCo",
           "url": "https://jobs.lever.co/finco/1", "status": "applied", "source": "lever" },
  "state": "interview_completed", "state_label": "Interview completed",
  "phase": "interviewing",      "phase_label": "Interviewing",
  "job_status": "applied",                    // the board projection, 07 §11
  "state_origin": "user_reported",            // who claims the current state
  "is_provisional": false, "provisional_evidence": null,
  "attribution": { "source": "lever", "channel": "manual_user", "role_family": "Backend Engineer" },
  "snapshots": {
    "match":    { "score": 88.0, "band": "strong", "score_source": "ai",
                  "scorer_version": null, "match_id": null },
    "artifact": { "kind": "none", "id": null, "version": null, "label": null, "sha256": null }
  },
  "timestamps": { "applied_at": "…", "first_response_at": null, "interview_scheduled_at": "…",
                  "interview_at": "…", "interview_completed_at": "…", "outcome_at": "…",
                  "created_at": "…", "updated_at": "…" },
  "durations_days": { "applied_to_first_response": null, "applied_to_interview": 0,
                      "applied_to_outcome": 0 },
  "follow_up": { "due_at": "…", "note": "Send the thank-you note", "notified_at": "…",
                 "completed_at": null, "overdue": true, "pending": true },
  "counts": { "events": 4, "corrections": 1, "last_sequence": 4 },
  "note": "They offered",
  "transitions": { "allowed": ["interview_scheduled", "…"], "allowed_labels": ["Interview scheduled", "…"],
                   "terminal": false },
  "timeline": [ /* oldest first — see below */ ],
  "actions": [
    { "key": "interview", "label": "I got an interview", "method": "POST",
      "route": "/api/application-tracking/1/interview",
      "primary": true, "disabled": false, "disabled_reason": null }
  ],
  "server_time": "2026-09-19T23:10:03Z",
  "exists": true
}
```

One timeline row:

```jsonc
{
  "id": 2, "event_id": "2b7a6737-…", "sequence": 2,
  "event_type": "application.interview_reported",
  "state_from": "applied", "state_from_label": "Applied",
  "state_to": "interview_scheduled", "state_to_label": "Interview scheduled",
  "phase": "interviewing",
  "origin": "user_reported",            // the honesty column
  "actor_type": "user", "actor_label": "smoke@example.com", "trigger": "user",
  "severity": "success",
  "message": "Interview scheduled on 2026-09-23 23:05 UTC (video) Hiring manager",
  "note": "",
  "payload": { "interview_at": "…", "format": "video", "round": "Hiring manager",
               "follow_up_at": "…", "note_present": false },
  "evidence": [ { "kind": "user_statement", "locator": "user", "captured_at": "…" } ],
  "is_correction": false, "correction_of_event_id": null, "correction_reason": "",
  "is_provisional": false,
  "follow_up_at": "…", "occurred_at": "…", "recorded_at": "…", "late_reported": false
}
```

`actions[]` is **server-owned**: the client renders the buttons it is given, in
order, and honours `disabled`/`disabled_reason`. Keys: `interview`,
`interview_completed`, `response`, `offer`, `rejected`, `withdraw`, `note`,
`follow_up`, `correct`, `attribution`, `follow_up_done`,
`confirm_suggestion`/`dismiss_suggestion` (provisional records only),
`open_job`, plus `applied` on a `not_applied` record. A key that is not legal
right now is either absent or `disabled: true` with a reason — the client never
derives legality from the state string.

### `GET /api/application-tracking?state=&source=&q=&page=1&page_size=50&sort=updated`

```jsonc
{ "items": [ /* documents, timeline trimmed */ ], "page": 1, "page_size": 50,
  "total": 2, "has_more": false,
  "counts": { "not_applied": 0, "applied": 1, "recruiter_response": 0,
              "interview_scheduled": 0, "interview_completed": 1,
              "offer_received": 0, "rejected_by_employer": 0, "withdrawn": 0 },
  "states": ["not_applied", "…"], "state_labels": { "applied": "Applied", "…": "…" },
  "server_time": "…" }
```

`counts` is the whole board, not the page — the column headers stay true while
the list is filtered.

### `GET /api/application-tracking/job/{job_id}`

Same document shape with `exists: false`, `state: "not_applied"` and an empty
timeline when the job is untracked, so the job drawer can render "Track this
application" without a second branch or a `404` handler.

### The writes

`POST /api/application-tracking` (open, requires `job_id`),
`…/{id}/status`, `…/{id}/interview`, `…/{id}/interview-complete`,
`…/{id}/response`, `…/{id}/rejection`, `…/{id}/offer`, `…/{id}/withdraw`,
`…/{id}/note`, `…/{id}/follow-up`, `…/{id}/follow-up/complete`,
`…/{id}/correction`, `…/{id}/attribution`, `…/{id}/snapshot` (no body),
`…/{id}/suggestion/confirm`, `…/{id}/suggestion/dismiss`.
`GET …/{id}/events?after_sequence=0&limit=100` pages the timeline on its own.

All of them return rule 7's shape — the document they changed plus what they
emitted — so the client updates in place:

```jsonc
{ "ok": true,
  "duplicate": false,        // true ⇒ a replay: nothing was written
  "state_changed": true,
  "event": { /* the timeline row above */ },
  "events": [ /* every row this call appended, in sequence order */ ],
  "document": { /* the full document */ },
  "state": "interview_scheduled", "phase": "interviewing",
  "server_time": "…" }
```

Errors carry the code in `detail.code` and the reason in `detail.message`:
`409 invalid_state_transition` (with `detail.allowed`), `422 origin_not_trusted`,
`422 correction_reason_required`, `422 validation_error` (unknown enum value,
with `detail.allowed`), `404 not_found`. A replay is **not** an error.

### `GET /api/application-tracking/report?group_by=source&from=&to=&origin=&state=`

```jsonc
{ "group_by": "source",
  "window": { "since": null, "until": null },
  "filters": { "origin": null, "state": null, "include_unapplied": false },
  "totals": { "tracked": 2, "applied": 2, "responses": 0, "interviews": 1,
              "offers": 0, "rejections": 0, "withdrawn": 0, "provisional": 0,
              "corrections": 1,
              "application_to_interview_rate": 50.0,
              "applied_to_response_rate": 0.0,
              "interview_to_offer_rate": 0.0,
              "avg_match_score": 70.0, "avg_days_to_interview": 0.0,
              "by_origin": { "user_reported": 1, "system_observed": 1 } },
  "groups": [ { "key": "lever", "label": "lever", "records": 1, /* the same
                 measures per group */ "by_origin": { "user_reported": 1 } } ],
  "disclaimer": "Counts come from the tracking timeline: an interview is only ever
                 counted when you recorded one (or a verified integration did).
                 Nothing here is inferred from email, and a match score is an
                 estimated fit, not an interview probability.",
  "server_time": "…" }
```

Rates are `null` when the denominator is `0` (`interview_to_offer_rate` with no
interviews, `avg_days_to_interview` with none reached) — §7 rule 2: `null` means
"did not happen", and the client renders `—`, never `0%`. An unknown `group_by`
is `422 validation_error` with the allowed keys, not an empty list.

### `GET /api/application-tracking/follow-ups?days=14`

```jsonc
{ "items": [ /* documents with a pending or recently completed follow-up */ ],
  "sweep": { "due": 1, "notified": 1, "skipped": 0 },   // this read ran the sweep
  "notification_kind": "application_follow_up",
  "server_time": "…" }
```

### `GET /api/application-tracking/meta`

The vocabulary, for a client that wants to render forms from the server instead
of shipping a copy: `states`, `state_labels`, `phases`, `origins`, `transitions`,
`terminal_states`, `interview_states`, `manual_sources`, `channels`,
`interview_formats`, `self_assessments`, `response_channels`,
`report_group_keys`, `counts`, `disclaimer`. The SPA mirrors the same values in
`frontend/src/lib/contracts.ts`, and a parity test fails the build if the two
lists drift.

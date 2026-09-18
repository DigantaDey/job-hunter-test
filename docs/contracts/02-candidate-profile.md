# 02 — Candidate profile entities

**Deliverable 1.** Defines who the candidate is, how each fact about them is
stored, where that fact came from, how sure we are, and what a human must
confirm before automation may act on it.

Shipped today: `profiles` (one JSON row per user), `personas` (search tracks),
`Profile.extraction_source` (`ai` | `user_edited`). This contract keeps both and
adds the versioning and provenance that the review flow
([13 §2](13-api-response-shapes.md)) and the automation gates
([08](08-application-fields.md)) need.

---

## 1. Entity map

```
users ─┬─ personas (a search track)                      [shipped]
       ├─ candidate_profiles (a versioned profile)        [proposed; extends profiles]
       │    ├─ profile_field_provenance (per field)       [proposed]
       │    │    └─ profile_field_history (per change)    [proposed]
       │    └─ resume_documents (source of the extraction) [03]
       └─ automation_policies                              [10]
```

One **user** owns many **personas** (tracks: "Data Analyst", "Data Scientist").
A **candidate profile** is the person's record; a persona *selects and weights*
parts of it (target role, keywords, preferences) but never contradicts it.
Facts live once, on the profile — a persona-scoped fact is a preference, not a
different truth about the person.

---

## 2. `candidate_profiles` — the versioned profile

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK `users.id`, index | no | tenant |
| `persona_id` | FK `personas.id` | yes | `null` = the person's base profile; set = a track-specific overlay (only `preferences` may differ, see §2.3) |
| `version` | Integer | no | monotonic per `user_id`, starts at 1 |
| `state` | String(20) | no | `PROFILE_STATES`: `draft` → `review_required` → `active` → `superseded` → `archived` |
| `is_current` | Boolean | no | exactly one `true` per `(user_id, persona_id)`; enforced by the writer in one transaction |
| `document` | JSON | no | the canonical profile document (§2.1) |
| `document_sha256` | String(64) | no | hash of the canonicalised document — lets a match result detect "profile changed" ([06 §5](06-match-result.md)) |
| `source_document_id` | FK `resume_documents.id` | yes | the resume this version was extracted from (`null` for a hand-built profile) |
| `source_extraction_id` | FK `resume_extractions.id` | yes | the extraction attempt that produced it |
| `review` | JSON | no | `{required: int, resolved: int, deferred: int, completed_at: str\|null}` — the counters the onboarding gate reads ([04 §3](04-onboarding-state-machine.md)) |
| `extraction_source` | String(20) | no | legacy column kept for compatibility: `ai` \| `user_edited` \| `manual` \| `imported` |
| `created_at` / `updated_at` | DateTime | no/yes | |
| `activated_at` | DateTime | yes | when it became `is_current` |
| `superseded_at` | DateTime | yes | when a newer version replaced it |
| `archived_at` | DateTime | yes | |

Constraints: `UNIQUE(user_id, persona_id, version)`; partial unique index on
`(user_id, persona_id) WHERE is_current` (PostgreSQL) / writer-enforced
transaction on SQLite; `INDEX(user_id, state)`.

### 2.1 The canonical profile document

`document` is a JSON object with these top-level keys and **no others** (an
unknown key is rejected at write time — a typo must not silently create a field
nothing reads). Every leaf that can be extracted carries provenance in
`profile_field_provenance`, keyed by its RFC 6901 JSON Pointer
(`/experience/0/company`).

```json
{
  "identity": {
    "full_name": "Diganta Dey",
    "first_name": "Diganta",
    "last_name": "Dey",
    "preferred_name": "",
    "pronouns": ""
  },
  "contact": {
    "email": "diganta@example.com",
    "phone": "+49 151 0000000",
    "location": { "city": "Berlin", "region": "Berlin", "country": "Germany", "country_code": "DE" },
    "links": [ { "kind": "linkedin", "url": "https://linkedin.com/in/…" },
               { "kind": "github",   "url": "https://github.com/…" } ]
  },
  "summary": "Embedded engineer with 6 years in automotive…",
  "current_title": "Senior Embedded Engineer",
  "seniority": "senior",
  "total_years_experience": 6.5,
  "skills": [ { "name": "C", "level": "expert", "last_used": "2026" } ],
  "experience": [
    { "title": "Senior Embedded Engineer", "company": "Wirelane GmbH",
      "company_name_normalized": "wirelane",
      "location": "Berlin, DE", "start": "2021-03", "end": null,
      "is_current": true, "bullets": ["…"] }
  ],
  "education": [ { "degree": "B.Tech", "field": "Electronics", "school": "…", "year": "2018", "location": "IN" } ],
  "projects":      [ { "name": "…", "role": "…", "start": "2023", "end": "2024", "url": "", "bullets": [] } ],
  "certifications":[ { "name": "…", "issuer": "…", "issued": "2024", "expires": null, "credential_id": "" } ],
  "languages":     [ { "language": "English", "level": "C1" } ],
  "preferences":   { … §2.2 … },
  "compensation":  { … §2.2 … },
  "availability":  { … §2.2 … },
  "eligibility":   { … §2.2 … },
  "meta": {
    "raw_text_sha256": "…",
    "source_document_id": 41,
    "profile_schema_version": "1.0.0"
  }
}
```

Type rules: dates are `YYYY` / `YYYY-MM` / `YYYY-MM-DD` strings (a resume says
"Mar 2021", and inventing a day is a fabrication); `end: null` +
`is_current: true` means "present"; numbers are numbers, not `"6.5 years"`.

The shipped `profiles.data` shape (`name`, `email`, `phone`, `location`,
`summary`, `skills[]`, `experience[]`, `education[]`, `projects[]`, `links[]`,
`current_title`, `raw_text`) maps into this document 1:1 — see
[14 §4](14-migration-strategy.md) for the field-by-field backfill. `raw_text`
moves out of the document onto `resume_documents.text_snapshot` (it is a property
of the file, not of the person) and is referenced by `meta.raw_text_sha256`.

### 2.2 Preferences, compensation, availability, eligibility

These are **user-stated**, never extracted: the model may *propose* them (with
`source: ai_inferred`, `band: low`, `review.status: needs_review`) but a
proposal is not a preference until the user confirms it. This is the gate that
makes `preferences` an onboarding step rather than a guess.

```json
"preferences": {
  "target_roles": ["Embedded Engineer", "Firmware Engineer"],
  "industries": ["automotive", "energy"],
  "locations": [ { "city": "Berlin", "country_code": "DE", "remote_policy": "hybrid" } ],
  "remote_policy": "hybrid",
  "company_sizes": ["startup", "medium"],
  "company_stages": ["seed", "series_a"],
  "excluded_companies": ["acme"],
  "excluded_keywords": ["defense"],
  "min_match_score": 60,
  "commute_minutes_max": 45
},
"compensation": {
  "currency": "EUR", "period": "year",
  "minimum": 70000, "target": 85000,
  "current": null, "equity_required": false
},
"availability": {
  "notice_period_days": 60, "start_date": "2026-12-01",
  "employment_types": ["full_time"], "open_to_contract": false
},
"eligibility": {
  "work_authorization": "citizen",
  "authorized_countries": ["DE"],
  "sponsorship_required": false,
  "relocation_willing": "within_country",
  "security_clearance": null
}
```

| Field | Sensitivity | Why |
|---|---|---|
| `preferences.*` | `internal` | shapes discovery; not identifying |
| `compensation.minimum` / `target` | `sensitive` | answered on portals; a wrong value costs money |
| `compensation.current` | `sensitive` | often illegal to ask for; **never** sent to a portal unless the user explicitly answers that question ([08 §4](08-application-fields.md)) |
| `availability.notice_period_days` | `sensitive` | a wrong value is a broken promise |
| `eligibility.work_authorization`, `sponsorship_required`, `authorized_countries` | `sensitive` | legally loaded; portal option lists rarely match our vocabulary → `ambiguous_option` by default |
| `eligibility.security_clearance` | `restricted` | treat like a government identifier |
| `identity.pronouns`, `contact.links` | `public` / `sensitive` | pronouns are shared only when the user confirms |

### 2.3 Persona overlays

A persona-scoped profile row (`persona_id` set) may only differ in
`preferences`, `compensation.minimum`/`target` and `meta`. Everything else is
copied from the base version at creation and re-copied when the base is
superseded — a track never has its own employment history. Enforced by the
writer, asserted by a test.

---

## 3. `profile_field_provenance` — one row per fact

This is the table that satisfies *"include source provenance for extracted
profile fields"* and *"include confidence and evidence for AI-extracted data"*.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK, index | no | tenant |
| `profile_id` | FK `candidate_profiles.id`, index | no | the version this provenance belongs to |
| `path` | String(400) | no | RFC 6901 JSON Pointer into `document` (`/experience/0/company`, `/preferences/target_roles/0`) |
| `value_hash` | String(64) | no | `sha256` of the canonical JSON value — cheap change detection without storing the value twice |
| `value_preview` | String(200) | yes | display-safe excerpt; **`null`** when `sensitivity ≥ sensitive` |
| `origin` | String(24) | no | `PROVENANCE_SOURCES` |
| `sensitivity` | String(12) | no | `SENSITIVITY_LEVELS` |
| `confidence` | Float | yes | `null` for user-stated values |
| `band` | String(8) | no | derived: `confidence_band(confidence)` |
| `evidence` | JSON | no | `Provenance.evidence[]` ([01 §6](01-conventions.md)); `[]` only when `origin` is user-stated |
| `extractor` | JSON | yes | `{name, version, model, prompt_version}` |
| `source_document_id` | FK `resume_documents.id` | yes | which file it came from |
| `source_extraction_id` | FK `resume_extractions.id` | yes | which attempt produced it |
| `ambiguity` | String(24) | yes | `AMBIGUITY_KINDS` when the document itself was ambiguous (two dates, two emails) |
| `review_status` | String(16) | no | `REVIEW_STATUSES` |
| `review_required` | Boolean | no | stored result of `requires_review(sensitivity, band)` — indexed, so "what needs review" is one query |
| `reviewed_by` | String(20) | yes | `user` \| `owner` \| `system` |
| `reviewed_at` | DateTime | yes | |
| `needs_answer_for` | JSON | no | application field keys that depend on this value (`["salaryExpectation"]`) — lets an answer invalidate the right provenance rows |
| `created_at` / `updated_at` | DateTime | no/yes | |

Constraints: `UNIQUE(user_id, profile_id, path)`; `INDEX(user_id, profile_id,
review_status)`; `INDEX(user_id, review_required)` (the review queue read).

Lifecycle: created with the profile version (bulk insert, one transaction);
updated **only** by a review action or a user edit, each of which appends a
`profile_field_history` row; deleted only with its profile version (erasure).

## 4. `profile_field_history` — append-only change trail

| Column | Type | Meaning |
|---|---|---|
| `id`, `user_id` | | tenant-scoped |
| `provenance_id` | FK | the field |
| `path` | String(400) | denormalised so history survives a field row's deletion |
| `previous_value_hash` | String(64) | what it was |
| `previous_preview` | String(200), nullable | `null` when `sensitivity ≥ sensitive` |
| `new_value_hash` | String(64) | what it became |
| `origin_before` / `origin_after` | String(24) | provenance transition (`resume_extraction` → `user_corrected`) |
| `review_status_before` / `review_status_after` | String(16) | |
| `actor_type` | String(20) | `ACTOR_TYPES` |
| `actor_id` | Integer, nullable | `users.id` when the actor is a person |
| `reason` | String(200) | user-supplied note, or the code (`review.confirmed`, `review.corrected`) |
| `event_id` | UUID String(36) | the domain event this change emitted ([12](12-events-and-notifications.md)) |
| `occurred_at` | DateTime | |

Nothing updates or deletes a history row except erasure (which removes values
and keeps the row, per [01 §7](01-conventions.md)).

---

## 5. Profile state machine

| From | To | Actor / trigger | Guard | Side effects | Event |
|---|---|---|---|---|---|
| — | `draft` | `system_worker` — extraction attempt created a document | bytes stored, extraction queued | `profile_field_provenance` bulk insert with `review_required` computed | `profile.created` |
| `draft` | `review_required` | `system_worker` — extraction succeeded | ≥1 row has `review_required = true` | onboarding gate `profile_review` becomes blocking; notification `profile_review_required` | one `profile.field_extracted` per field, each carrying `review_required` |
| `draft` | `active` | `system_worker` — extraction succeeded | **no** row needs review (all `public`/`internal` + `band = high`) → those rows get `review_status = auto_accepted` | `is_current = true`, previous version `superseded`; match results recomputed for `is_current` jobs | `profile.created` |
| `review_required` | `active` | `user` — `POST /api/profile/review/resolve` | every `review_required` row is `confirmed` \| `corrected` \| `rejected` \| `deferred`, and no `restricted` row is `deferred` | `review.completed_at` set; onboarding gate completes; stale match results marked `profile_changed` | `profile.review_completed` |
| `review_required` | `draft` | `system_worker` — a new extraction supersedes this version | new `source_extraction_id` | old version `archived` | `extraction.superseded` |
| `active` | `superseded` | `system_worker` or `user` — a newer version became current | exactly one `is_current` per `(user_id, persona_id)` | match results for the old `profile_version_id` become `profile_changed`; downstream resumes keep their `profile_snapshot` (immutable) | `profile.version_superseded` |
| `active`, `superseded` | `archived` | `user` — `DELETE /api/profile/{id}` or account erasure | not `is_current` | rows kept for audit, values removed on erasure | `profile.archived` |
| any | `archived` | `system_api` — GDPR erasure | — | see [01 §7](01-conventions.md) | `profile.archived` |

*Note*: `profile.review_required` is not in the event vocabulary because the
state is derived — the emitted event is `profile.field_extracted` per field with
`review_required: true`, and consumers compute the aggregate. That keeps one
event per fact instead of one event per recomputation.

Forbidden: `active → review_required` (a value that becomes doubtful gets a
*new* provenance row with `review_status = needs_review`, and the profile's
`review` counters move; the state stays `active` so the user's board does not
reset), and any transition out of `archived`.

There is deliberately **no aggregate "profile needs review" event**: the state
is derived from the field rows, so the emitted events are the per-field ones
(`profile.field_extracted`, `profile.field_confirmed`, …) and every consumer
computes the aggregate the same way — `review.required - review.resolved -
review.deferred > 0`. One event per fact, never one per recomputation.

---

## 6. Ownership and lifecycle (summary — full registry in [15](15-entity-ownership.md))

| Entity | Single writer | Readers | Retention |
|---|---|---|---|
| `candidate_profiles` | `app/services/profile_service.py` (proposed) — the only module that flips `is_current` | scoring, resume generation, autofill mapping, onboarding, analytics, export/erasure | life of the account; superseded versions kept until erasure |
| `profile_field_provenance` | same service (bulk on create, single-row on review) | review UI, automation gates, match evidence | same as its profile version |
| `profile_field_history` | same service | audit view, "what did we send to this portal?" | append-only; values removed at erasure |
| `personas` | `app/services/persona.py` (shipped) | discovery, scoring, outreach | shipped behaviour unchanged |

---

## 7. Invariants (each one is a test)

1. Exactly one `is_current = true` profile per `(user_id, persona_id)`.
2. Every leaf of `document` that is not `null`/empty has a provenance row; a
   provenance row's `path` resolves in `document`.
3. `band == confidence_band(confidence)` and `review_required ==
   requires_review(sensitivity, band)` — stored, not recomputed per read.
4. No provenance row with `sensitivity = restricted` has a non-null
   `value_preview`.
5. `origin ∈ {user_provided, user_confirmed, user_corrected}` ⇒ `confidence IS
   NULL` and `review_status ∈ {confirmed, corrected}`.
6. `origin ∈ {resume_extraction, ai_inferred, heuristic}` ⇒ `evidence != []` or
   `confidence ≤ 0.6`.
7. A persona-scoped profile's `document` differs from the base only inside
   `preferences`, `compensation.minimum`, `compensation.target`, `meta`.
8. Editing a field appends exactly one `profile_field_history` row and emits
   exactly one domain event.
9. Reading another tenant's profile is a `404` (never `403`, never the row).

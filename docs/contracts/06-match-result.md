# 06 — Match result schema

**Deliverable 5.** A match is a **fact about a pair** — this job and this
candidate, as they both were at a moment in time, computed by a named scorer. It
is not a column on the job.

Shipped today: `jobs.score`, `jobs.score_reason`, `jobs.score_source`,
`jobs.score_detail`, produced by `app/services/scoring.py`
(`preliminary_score_detailed` / `ai_score_detailed` / `score_job`) and written
either by discovery or by `GET /api/jobs/{id}/intelligence`. That is enough to
sort a board; it is not enough to answer *"why was this 78 on Monday and 61 now?"*,
because the inputs (profile version, JD text, scorer version) are not recorded
anywhere.

---

## 1. `match_results`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id` | FK, index | no | tenant |
| `job_id` | FK `jobs.id`, index | no | |
| `persona_id` | FK `personas.id` | yes | the track the match was computed for |
| `profile_id` | FK `candidate_profiles.id` | no | the profile **version** scored ([02](02-candidate-profile.md)) |
| `profile_sha256` | String(64) | no | `candidate_profiles.document_sha256` at scoring time |
| `job_description_sha256` | String(64) | no | `jobs.description_sha256` at scoring time |
| `scorer` | String(24) | no | `deterministic` \| `ai` \| `hybrid` |
| `scorer_version` | String(24) | no | semver of the scoring code/prompt contract |
| `model` | String(120) | yes | provider model id, for `scorer = ai` |
| `prompt_version` | String(60) | yes | |
| `score` | Float | no | 0..100 |
| `band` | String(12) | no | `MATCH_BANDS`: `strong` \| `good` \| `possible` \| `weak` \| `unknown` |
| `confidence` | Float | yes | 0..1 — how much we trust the score itself (`null` for `insufficient_data`) |
| `score_source` | String(24) | no | `SCORE_SOURCES` — the provenance label the UI must render |
| `reason` | Text | no | one human sentence |
| `rubric` | JSON | no | per-criterion breakdown (§2) |
| `matched_skills` | JSON | no | `[{name, required, evidence[], confidence}]` |
| `missing_skills` | JSON | no | `[{name, required, severity, remediable}]` |
| `evidence` | JSON | no | `Provenance.evidence[]` — spans from the JD **and** the resume |
| `guardrail_report` | JSON | no | accuracy verdict for an AI score |
| `flags` | JSON | no | `{plan_gated, ai_error, upgrade_hint, funding_context, insufficient_text}` |
| `is_current` | Boolean | no | the latest verdict for `(user_id, job_id, persona_id)`; exactly one `true` |
| `staleness` | String(20) | no | `MATCH_STALENESS`: `fresh` \| `profile_changed` \| `job_changed` \| `expired` \| `scorer_upgraded` |
| `pipeline_job_id` | FK | yes | the queued work that computed it, when it was batch work |
| `computed_at` | DateTime | no | |
| `expires_at` | DateTime | yes | when the verdict is stale by age alone (`match_ttl_hours`) |
| `superseded_by_id` | FK `match_results.id` | yes | |
| `created_at` | DateTime | no | |

Constraints: `UNIQUE(user_id, job_id, persona_id, profile_sha256,
job_description_sha256, scorer_version)` — the same inputs cannot produce two
rows, which is what makes a resumed or repeated run cheap; `INDEX(user_id,
job_id, is_current)`; `INDEX(user_id, persona_id, score DESC)` for board
ordering; `INDEX(user_id, staleness)`.

Append-only apart from `is_current`, `staleness` and `superseded_by_id`. A
re-score **inserts**, it never updates a score in place — that is the whole point
of the entity.

---

## 2. The rubric

`rubric` is a list, not a map, so order is stable and a criterion can appear with
its weight even when it could not be scored.

```json
{
  "weights": { "skills": 0.35, "experience": 0.25, "seniority": 0.15,
               "domain": 0.10, "location": 0.10, "education": 0.05 },
  "criteria": [
    {
      "key": "skills",
      "label": "Skills",
      "weight": 0.35,
      "score": 82,
      "max": 100,
      "scored": true,
      "reason": "9 of 11 required technologies are evidenced in the resume.",
      "evidence": [
        { "kind": "text_span", "quote": "AUTOSAR Classic platform, 3 years",
          "locator": { "document_id": 41, "page": 1, "start": 2210, "end": 2251 } }
      ],
      "missing": ["Rust"]
    },
    { "key": "location", "label": "Location", "weight": 0.10, "score": null,
      "max": 100, "scored": false,
      "reason": "The posting does not state a workplace policy.", "missing": [] }
  ],
  "overall": 78,
  "recommendation": "good"
}
```

* `weights` is `scoring.RUBRIC_WEIGHTS` (shipped) and is **part of the response**
  — the API already renames it `rubric` for the UI, and that stays.
* A criterion that could not be scored has `score: null`, `scored: false` and a
  `reason`. It is *not* scored 0: an unscored criterion is excluded from the
  weighted mean and the exclusion is visible (`_score_checks` already works this
  way).
* `overall` is the weighted mean over scored criteria, 0..100, and equals
  `match_results.score`.
* `recommendation` equals `band`.

## 3. Bands

| Band | Score | Meaning the UI may state |
|---|---|---|
| `strong` | ≥ 85 | "Apply — this is a close match." |
| `good` | 70–84 | "Worth applying." |
| `possible` | 50–69 | "Possible — check the gaps." |
| `weak` | < 50 | "Weak match." |
| `unknown` | any | `score_source ∈ {pending, rejected, insufficient_data, unscored}` — **no band may be shown as a number** |

The band is derived from the score *and* the source: a `preliminary` 88 is
`strong` but must render with its provenance label ("Keyword estimate"), and a
`pending` row renders `unknown` regardless of the stored 0.

---

## 4. `score_source` — the honesty contract

These values are shipped (`scoring.score_job`, `JobDetail.score_source`); this is
what each one obliges the writer and the renderer to do.

| Value | Set when | Writer obligation | Renderer obligation |
|---|---|---|---|
| `ai` | the model answered **and** the accuracy guardrail passed | store `rubric`, `evidence`, `model`, `prompt_version`, `guardrail_report` | may show the score as a verdict |
| `preliminary` | deterministic keyword/TF-IDF estimate | store the breakdown; free tier stops here | **must** label it "Keyword estimate"; never as an AI score |
| `pending` | the AI was unavailable; nothing was scored | `score = 0`, keep the preliminary breakdown in `rubric` for reference | show "Not scored yet", never `0%` |
| `rejected` | the model answered, the guardrail refused it | store `guardrail_report.issues` | show the refusal, offer a re-score |
| `insufficient_data` | not enough JD or profile text to score honestly | `confidence = null` | show "Not enough text", link to the fix (paste the JD / complete the profile) |
| `funding_context` | a funding-radar posting with no job description | no rubric | label as a funding lead, not a match |
| `unscored` | never attempted | — | show nothing but the state |

---

## 5. Staleness

`staleness` is computed, never guessed:

| Value | Trigger | Who sets it |
|---|---|---|
| `fresh` | default | writer |
| `profile_changed` | `candidate_profiles.document_sha256` for the scored version no longer current, or the user corrected a field the rubric used | `profile_service` on a new version / review resolution |
| `job_changed` | `jobs.description_sha256` differs from the row's | discovery, when a re-fetch changes the JD |
| `expired` | `computed_at < now - match_ttl_hours` | a sweeper (daily), or lazily on read |
| `scorer_upgraded` | `scorer_version` of the row < the deployed scorer | at deploy, by migration/backfill |

A stale row is **still returned** (it is better than nothing) with its
`staleness` and, for `profile_changed`/`scorer_upgraded`, an offered re-score.
Board ordering uses `is_current` rows only.

---

## 6. Match state machine

A match result has no lifecycle of its own beyond `is_current` and `staleness`,
but the *act of matching* does, and it is where the actors live:

| # | From | To | Actor / trigger | Guard | Side effects | Event |
|---|---|---|---|---|---|---|
| M1 | — | row created, `is_current = true` | `system_worker` — discovery run scored a new posting | profile available; plan allows the scorer chosen | previous current row → `is_current = false`, `superseded_by_id` set | `match.computed` |
| M2 | — | row created | `user` — `GET /api/jobs/{id}/intelligence?refresh=1` | entitlement `can_use_advanced_matching` for `scorer = ai`, else `deterministic` + `flags.plan_gated = true` | as M1; quota charged **only** when the model actually ran (the shipped `company_intel` rule applied here) | `match.computed` |
| M3 | `is_current` row | `staleness = profile_changed` | `user` — profile review resolved / new profile version | — | notification only when the change moves a band | `match.stale` |
| M4 | any | `staleness = expired` | `system_scheduler` — daily sweeper | `computed_at` older than TTL | — | `match.stale` |
| M5 | any | `staleness = scorer_upgraded` | `system_api` — deploy backfill | `scorer_version` older than deployed | queues a re-score for `is_current` rows above the plan's re-score budget | `match.stale` |
| M6 | row created | — | `system_worker` — `score ≥ high_match_threshold` and `trigger = auto` | user preference `high_match_alerts` on | notification `high_match` (deduped per run, as `handlers._notify_run` does) | `match.high_score` |
| M7 | `is_current = true` | `is_current = false` | `system_worker` — a newer row for the same pair | same transaction as M1 | — | `match.superseded` |

---

## 7. API

[13 §4](13-api-response-shapes.md) defines `GET /api/jobs/{id}/matches`,
`GET /api/matches` (cross-job, persona-scoped, paginated) and the compatibility
rule for the shipped `score_*` columns on `GET /api/jobs` and
`GET /api/jobs/{id}/intelligence`.

Short version: **list endpoints keep reading the denormalised columns** (one row
per job, no join, no N+1); **detail endpoints read `match_results`** and return
the current row plus its history when asked (`?history=1`).

---

## 8. Invariants (tests)

1. Exactly one `is_current = true` row per `(user_id, job_id, persona_id)`.
2. `band` is derivable from `score` + `score_source`; a stored mismatch is a bug.
3. `score_source = ai` ⇒ `model` and `prompt_version` are set and
   `guardrail_report.passed = true`.
4. `score_source ∈ {pending, rejected, insufficient_data, unscored}` ⇒ `band =
   unknown`.
5. `score_source = preliminary` ⇒ `scorer = deterministic` and `confidence ≤ 0.6`.
6. Every `rubric.criteria[].evidence[].quote` appears in the JD text or the
   profile's source document — an evidence span that does not resolve is a
   fabrication and fails the run.
7. Re-scoring with identical inputs (`profile_sha256`, `job_description_sha256`,
   `scorer_version`) returns the existing row and charges no quota.
8. `jobs.score`/`score_source`/`score_detail` always equal the `is_current`
   match row (the projection is written in the same transaction).
9. A free-tier run produces zero `scorer = ai` rows (the plan gate the shipped
   `handle_discovery` implements by withholding the profile).

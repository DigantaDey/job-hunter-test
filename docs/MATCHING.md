# Multi-stage job matching

Every match in JobHunter AI is an **explained recommendation**, not a number.
The pipeline is deterministic where it can be, uses the AI only where judgment
is needed, and never presents a keyword-overlap estimate as a model verdict —
or an uncalibrated model verdict as a probability.

## Why no single score

A single `match_score` invites applying to everything above a threshold.
This system is optimized for *interview potential*: it spends the user's time
on the few jobs where the evidence says the interview will go well, and it
tells you **why** for every job it does or doesn't recommend.

## The three stages

```
Stage 1  Hard filters        →  eligible | penalized | filtered  (named checks)
Stage 2  Deterministic score →  0-100, band, per-feature contributions
Stage 3  AI evidence review  →  recommendation + evidence + gaps + risks
```

### Stage 1 — hard filters

Nine named checks, each recorded in `match_results.hard_filters.checks` with
status `pass` / `unknown` / `conflict` / `fail`, a human-readable detail, and
the job vs candidate values that drove it:

| Check | Behavior |
|---|---|
| `expired` | `fail` when the posting is expired/closed |
| `duplicate` | `fail` for a same-role/same-company/same-location duplicate, or a role already applied to |
| `location_mismatch` | `conflict` (−20) when workplace is onsite/hybrid and the city differs; remote jobs pass unless the candidate is onsite-only; unknown when neither side states a location |
| `work_authorization` | `fail` when the posting requires authorization the candidate's status may not satisfy; `conflict` (−25) when unclear |
| `sponsorship` | `fail` when the candidate requires sponsorship and the posting states it is not offered; `conflict` (−25) when unstated |
| `employment_type_mismatch` | `fail` for an internship aimed at a senior candidate; `conflict` (−25) for an explicit part-time/contract/temporary/freelance posting on a full-time track |
| `compensation_below_minimum` | `fail` when the posted max is below the candidate's stated minimum; `conflict` (−10) when the range starts below it |
| `seniority_mismatch` | `conflict` (−20, or −10 one level down) when the role is more than a level away |
| `required_skills_missing` | `fail` when none of the required skills are evidenced; `conflict` −8 per missing skill, capped at −24 |

Rules that keep the stage honest:

- **`unknown` never penalizes.** A missing answer is not a mismatch — the
  check is stored as `unknown` with a reason, and the gap stays visible.
- **`conflict` applies an itemized penalty** to the stage-2 score; the job is
  still recommended, with the conflict named in the reason.
- **`fail` filters the job out** of recommendations while the full report is
  stored, so the exclusion is explainable. The score is capped at the top of
  the weak band so the row stays self-consistent.

### Stage 2 — deterministic score

Twelve named features, each with a weight, a 0-100 score, a reason, and
evidence where applicable:

| Feature | Weight | | Feature | Weight |
|---|---|---|---|---|
| `required_skills` | 0.22 | | `industry` | 0.06 |
| `relevant_experience` | 0.16 | | `location` | 0.06 |
| `seniority` | 0.10 | | `remote` | 0.04 |
| `career_trajectory` | 0.10 | | `freshness` | 0.04 |
| `compensation` | 0.08 | | `hiring_signal` | 0.04 |
| `preferred_skills` | 0.06 | | `application_friction` | 0.04 |

Rules that keep the number honest:

- **Unscored, not zero.** If a feature can't be scored from the data at hand
  (e.g. no compensation stated), it is excluded from the average and shown as
  `scored: false` with the reason — never silently counted as a fail.
- **Contributions are stored per feature** (`score × weight`), so the overall
  number always decomposes into its parts and can be re-explained.
- **Bands** (contract 06 §3): `strong` ≥ 85, `good` ≥ 70, `possible` ≥ 50,
  `weak` below; `unknown` when unscored.
- **Stale inputs are flagged, not silently recomputed:** `staleness` is
  `profile_changed`, `job_changed`, or `scorer_upgraded` when the stored
  fingerprint no longer matches the current profile, job, or scorer version.

### Stage 3 — AI evidence review

The LLM sees the stage-1 + stage-2 result plus the *relevant slices* of the
candidate profile (never the whole document) and returns a structured review:

- `recommendation`: `apply` / `apply_with_tailoring` / `hold` / `skip`, with
  `recommendation_reason`. The allowed set is band-restricted (e.g. a `weak`
  match can only be `hold` or `skip`) and the guardrail rejects anything
  outside it.
- `strengths`: claims that **must be present in the candidate data** — the
  accuracy guard verifies each one, so an evidence-backed strength is one the
  system can point at, not a compliment.
- `requirements_review`: each requirement with `basis` (`explicit` in the JD
  vs `inferred`), whether it is `met`, and the `evidence` — the
  inferred-vs-explicit distinction the user asked for.
- `risks`: remaining risk factors.
- `resume_emphasis`: what to lead with for this job.
- `application_action`: concrete next step.

The review runs through the shared `ai_guardrails` (accuracy guard,
temperature cap, output schema validation) and counts against the existing
credit accounting. When the AI is unavailable or its answer is rejected, the
stage-2 result stands alone as `score_source: "preliminary"` — a match never
blocks on the model, and a model output is never required to explain one.

## What a match result contains

`POST /api/jobs/{id}/match` returns (and stores) the full explanation:

- `score`, `band`, `confidence`, `reason` (human-readable, no probabilities)
- `score_source`: `ai` | `preliminary` | `pending` | `rejected` |
  `insufficient_data` | `funding_context` | `unscored` — what the number
  actually is, rendered honestly
- `hard_filters`: every check that ran and the ones that fired
- `rubric`: weights, criteria, overall, band
- `features[]`: per-feature `score`, `weight`, `contribution`, `scored`,
  `reason`, `evidence`
- `matched_skills[]` / `missing_skills[]` with `required` flags and evidence
- `review`: the stage-3 structured output when present
- `score_fingerprint`, `profile_sha256`, `job_description_sha256`,
  `scorer_version`: everything needed to say *why this number, for these
  inputs, with this rubric*

## Versioning and recalibration

- Every row stores the `scorer_version` and the inputs' hashes. Two rows for
  the same job with different versions can be compared directly.
- Re-scoring inserts a new row and supersedes the old one (`superseded_by_id`)
  — history is kept, nothing is overwritten.
- `GET /api/jobs/{id}/match` returns the current row;
  `POST /api/jobs/{id}/match` reuses an identical stored verdict (same
  fingerprint) unless asked to refresh.

## User feedback and the calibration gate

`POST /api/jobs/{id}/match/feedback` records the user's signal:

- `relevant` / `not_relevant` — corrections of the recommendation itself
  (`not_relevant` demotes the job in the best-matches list and stores the
  reason as the correction of record)
- `applied` / `rejected` / `interview` — application outcomes

`GET /api/matches/calibration` answers whether a calibrated interview
probability *could* be offered: the answer is `calibrated: false` for this
release, with the per-scorer-version outcome counts and the minimum needed.
Until the outcome dataset passes the gate, the product only says "estimated
fit" and "recommendation" — the gate exists so the moment enough outcomes
exist, calibration can start *and be said honestly* instead of having been
claimed all along.

## API

| Method & path | Purpose |
|---|---|
| `POST /api/jobs/{id}/match` | run (or reuse) the three stages; `ai: false` skips stage 3, `refresh: true` forces a fresh computation |
| `GET /api/jobs/{id}/match` | current stored result + its feedback |
| `GET /api/matches` | best matches across jobs (`min_score`, `band`, `include_filtered`), corrections demoted, missing required skills carried on every entry |
| `POST /api/jobs/{id}/match/feedback` | record relevant / not relevant / applied / rejected / interview |
| `GET /api/matches/calibration` | the honest calibration gate (outcome counts vs minimum, per scorer version) |

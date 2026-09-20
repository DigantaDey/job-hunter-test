# Outcome reporting

**Contract:** `docs/contracts/07-application-state-machine.md` §11.7 ·
**Code:** `backend/app/services/outcome_report.py` ·
**API:** `GET /api/analytics/outcomes`, `…/outcomes/weekly`, `…/outcomes/export` ·
**Tests:** `backend/tests/test_outcome_reporting.py`,
`frontend/src/lib/__tests__/reporting.test.ts`,
`frontend/src/components/__tests__/OutcomeReport.test.tsx`

A job search is not improved by doing more things. It is improved by applying to
better-fit jobs, hearing back from more of them, and reaching more interviews. So
the report leads with **interviews and meaningful outcomes**, and treats raw
activity — tokens, jobs scanned, queue depth — as infrastructure, not as
progress.

This document is the methodology. It exists because a number without its
definition is an opinion, and because the previous dashboard shipped exactly that
bug: an "Interviews (est)" count built from `applied AND score >= 75`, a scorer
output dressed up as an outcome.

---

## 1. The four rules

| Rule | What it means in the code | Where it is pinned |
|---|---|---|
| **Observed ≠ estimated** | Every number carries `provenance`: `observed`, `user_reported`, `derived` or `estimated`. The only `estimated` metric is "high-fit jobs" (a match score). | `test_every_number_says_whether_it_was_observed_or_estimated` |
| **No interview probability** | `calibration_status()` returns `available: false` with what is missing, and `interview_probability()` *raises*. No surface may compute one. | `test_no_interview_probability_is_reported_before_calibration` |
| **Minimum sample sizes** | Every ratio is gated; a group under the threshold reports `sufficiency: "insufficient"`, a `null` rate and the reason. Counts stay. | `test_a_group_below_the_minimum_sample_is_labelled` |
| **Reproducible** | Versioned methodology, deterministic ordering, and a `calculation_id` hash over exactly the inputs that can change the answer. | `test_the_same_inputs_produce_the_same_numbers` |

---

## 2. Windows, date ranges and timezones

A window is a set of **whole local calendar days** in the caller's timezone, and
it is **half-open** on the wire:

```
start_utc ≤ t < end_utc          (both naive-UTC, as stored)
```

* `range=last_7_days | last_30_days | last_90_days` — today and the N−1 days
  before it, in local time. The window starts at local midnight, so it is stable
  from midnight to midnight rather than sliding with the clock (which is what
  makes two reports comparable, and `calculation_id` stable).
* `range=this_week` — Monday local midnight → **now** (the one partial window:
  "how is this week going" is a week-to-date question).
* `range=last_week` — the previous full Monday–Sunday week.
* `range=custom&since=…&until=…` — explicit bounds. A **date-only** `until` means
  "through the end of that local day": the exclusive instant is the *next* local
  midnight. Timestamps are used as given; a naive timestamp is read as UTC (the
  assumption every writer in this codebase makes).

The timezone defaults to the account's stored `users.timezone` and can be
overridden per request. An unknown IANA zone is a `422` — the report never
silently answers in a different zone, because a day boundary is a local fact.

Both windows are echoed back so a reader can see exactly what was measured:

```jsonc
"window": {
  "range": "last_7_days", "timezone": "Asia/Tokyo",
  "start_utc": "2026-09-13T15:00:00Z", "end_utc": "2026-09-20T15:00:00Z",
  "start_local": "2026-09-14T00:00:00+09:00", "end_local": "2026-09-21T00:00:00+09:00",
  "start_date": "2026-09-14", "end_date": "2026-09-20", "days": 7,
  "boundary": "half-open [start_utc, end_utc)"
}
```

`previous_window` is the same span immediately before `start_utc`, which is what
every trend number is compared against.

---

## 3. Metrics

All metrics are **cohort-based**: the window selects *applications submitted in
that window* (`application_tracking.applied_at`), and every outcome is counted
for that cohort. "20 applications, 4 interviews" therefore means "of the 20
applications sent in this window, 4 have reached an interview" — a statement that
does not move when time passes.

| Metric | Key | Definition | Provenance | Minimum sample |
|---|---|---|---|---|
| Jobs discovered | `jobs_discovered` | `jobs.discovered_at` in the window | observed | — (count) |
| High-fit jobs found | `high_fit_jobs` | discovered jobs scored ≥ `HIGH_FIT_SCORE` (75) | **estimated** | — (count) |
| Applications prepared | `applications_prepared` | distinct jobs with an application packet generated in the window | observed | — (count) |
| Applications submitted | `applications_submitted` | distinct jobs applied to in the window: board (`jobs.applied_at`) ∪ tracking (`application_tracking.applied_at`), unioned by job id | observed | — (count) |
| Applications tracked | `applications_tracked` | submitted applications that have a tracking record — the denominator of every rate | observed | — (count) |
| Recruiter responses | `recruiter_responses` | cohort applications with `first_response_at` | user_reported | 5 applications |
| Interviews | `interviews` | cohort applications in `interview_scheduled` \| `interview_completed` \| `offer_received` | user_reported | 5 applications |
| Interview rate | `interview_rate` | interviews ÷ applications tracked | derived | 5 applications |
| Rejection rate | `rejection_rate` | `rejected_by_employer` ÷ applications tracked | derived | 5 applications |
| Time from posting to application | `time_from_posting_to_application` | days between `jobs.posted_at` and the application date (median, mean, p90) | derived | 5 applications with a posting date |
| User correction rate | `user_correction_rate` | corrections ÷ recorded timeline events | derived | 20 events |
| Application failure rate | `application_failure_rate` | application-pipeline attempts ending `failed`/`dead` ÷ all attempts that finished in the window | derived | 10 attempts |
| Outcome data coverage | `tracking_coverage` | applications tracked ÷ applications submitted | derived | — (count) |

Two things worth stating plainly:

* **Applications without a tracking record are not failures.** They are reported
  in `quality.applications_without_a_tracking_record` and excluded from the rates,
  whose denominator is disclosed. The alternative — assuming they failed — would
  turn a missing integration into a bad conversion rate.
* **The failure rate measures the machinery, not the person.** It is read from
  `pipeline_jobs` (`pipeline='application'`, terminal states), and its note says
  so.

---

## 4. Comparisons and the minimum sample size

Five dimensions, from `REPORT_COMPARISON_DIMENSIONS`:

| Dimension | Groups by |
|---|---|
| `source` | `application_tracking.source` (board id or attributed source) |
| `score_band` | the scorer's band (`strong`/`good`/`possible`/`weak`/`unknown`) |
| `score_range` | score brackets (`90-100`, `80-89`, `70-79`, `60-69`, `0-59`, `unscored`) |
| `role_family` | the shared role-family normaliser |
| `artifact` | the frozen artefact snapshot (`packet:v3`, `resume_document:v2`, `none`, …) |

Every group reports its counts, its sample size and its sufficiency. A group with
fewer than **5** applications gets `interview_rate: null`,
`sufficiency: "insufficient"` and an `insufficient_reason` naming the shortfall.
A "best" group is only named when at least **two** groups are comparable — one
good row is not a pattern.

Sample sizes live in one place (`MIN_SAMPLE_SIZES`) and are returned in
`definitions.minimum_sample_sizes`, so a reader can check the rule that gated a
number instead of guessing it.

---

## 5. Is it working?

`trend` compares the window with the immediately preceding one of the same
length. The verdict is carried by the **interview rate** — the only metric here
that says applications turned into conversations — and a direction is only
claimed when **both** windows hold at least 5 applications.

```
improving          +1.0pp or more
flat               inside ±1.0pp
declining          −1.0pp or more
not_enough_data    either window below the minimum sample
```

`not_enough_data` is a first-class answer, not a zero. The headline says how many
more applications would be needed, and `signal` will still surface an *early
signal* when one comparison group is already large enough to be worth watching —
labelled as early, because it is not a trend.

---

## 6. Calibration — why there is no interview probability

A match score ranks fit. Turning it into "you have a 42% chance of an interview"
requires a model that was fitted on recorded outcomes, validated out-of-sample,
and shown to be better than the base rate. None of that can be honestly claimed
today, so the report says what is missing:

```jsonc
"calibration": {
  "available": false,
  "interview_probability": null,
  "missing": ["recorded_interviews", "calibrated_model_version"],
  "observed": { "recorded_interviews": 3, "score_bands_with_outcomes": 1,
                "share_verified_outcomes": 0.5 },
  "requires": { "recorded_interviews": 30, "score_bands_with_outcomes": 3,
                "share_verified_outcomes": 0.8, "calibrated_model_version": null }
}
```

The gate is data **and** model: at least 30 recorded interviews across at least 3
score ranges, at least 80% of outcomes verified rather than remembered, and a
named calibrated model version. `interview_probability()` raises while any of
them is unmet, so a future caller cannot quietly compute one instead.

---

## 7. Privacy and tenancy

* Every query in `app/services/outcome_report.py` filters on the authenticated
  `user_id`. The endpoints accept no user parameter, so there is no shape of
  request that widens the scope.
* The document carries no free text (no notes, no email bodies), no contact
  details and no third-party personal data. Job titles, companies and sources are
  the account's own rows.
* `privacy` states the scope (`self_only`) and the three negatives
  (`includes_other_users`, `cross_tenant_aggregates`,
  `third_party_personal_data`), and the tests assert one tenant's report contains
  no trace of the other's.

### Export

Export is available **only because those requirements are met**, and the gate is
re-checked at call time against the document that would be written
(`export_privacy(report)`), not against the request:

| Requirement | Check |
|---|---|
| `self_scope_only` | `privacy.scope == "self_only"` |
| `no_other_tenants` | no other-user rows, no cross-tenant aggregates |
| `no_third_party_personal_data` | no contacts, recruiter identities or message bodies |
| `no_free_text_notes` | user notes are never exported |
| `audited` | `analytics.report_exported` is written **before** the body is produced |

`GET /api/analytics/outcomes/export?format=csv|json` returns the metrics, the
grouped comparisons and the methodology header (window, timezone, version,
`calculation_id`). A requirement that is ever unmet turns the endpoint into a
`403 export_not_allowed` naming the unmet check rather than a download that
violates it.

---

## 8. Reproducibility

`REPORT_VERSION` (`1.0.0`) versions the *calculations*. It is bumped whenever a
formula, a denominator or a minimum sample changes, and it appears in every
response, in the CSV header and in the definitions block.

`calculation_id` is a SHA-256 over exactly the inputs that can change the answer:

```
report_version · timezone · range · start_utc · end_utc ·
minimum_sample_sizes · high_fit_score · interview_states
```

Same rows, same window, same report — the test asserts the two responses are
byte-identical apart from `generated_at`/`server_time`. Group ordering is
deterministic (score dimensions read best-fit-first, everything else by sample
size then key), so two runs render the same table in the same order.

---

## 9. The weekly summary

`GET /api/analytics/outcomes/weekly?range=last_7_days|this_week` returns one
sentence, the funnel counts, the trend against the previous week, the sufficiency
of the sample behind the rate, and one concrete next step. The dashboard's
"this week" block (`GET /api/me/dashboard` → `weekly_report`) is the **same
calculation** — same window, same definition of an application, same minimum
sample — so the product surface and the report page cannot disagree.

---

## 10. What this report will never do

* Count an interview that nobody recorded (no email inference, no score
  inference).
* Show a percentage over fewer than the minimum sample, anywhere — not in a
  group, not in a headline, not in a CSV cell.
* Present a match score as a probability, or a probability at all.
* Report tokens, cost or queue depth as a measure of the user's progress (those
  are owner-console concerns).
* Read another account's rows, blend tenants, or export one tenant's data to
  another.

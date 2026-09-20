# Observability & operations — the background workflows

What an operator can see, what an operator is paged for, and what to do about
each alert. Covers every background workflow in the product: onboarding,
resume extraction, discovery, search providers, matching, artifact generation,
browser sessions, user-action-required queues, application submission,
notifications and reports.

Everything here is generated from code, not described from memory:

| Concern | Where it lives |
|---|---|
| Retry classification (the four outcomes) | `backend/app/services/reliability.py` |
| Metric catalog + allowed label values | `reliability.METRIC_CATALOG` (registered into `core/metrics.py`) |
| Log redaction (secrets + personal data) | `backend/app/core/redaction.py`, applied by `core/logging.py` |
| User-action expiry sweep | `reliability.expire_user_actions`, driven by `app.worker.Worker` |
| Queue semantics (leases, budgets, dead-letter) | `backend/app/services/job_queue.py`, `docs/contracts/11-background-jobs.md` |
| Alert rules | [`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml) |

Metrics are exposed as Prometheus text on `GET /api/metrics` (token-protected)
and, when `METRICS_PORT` is set, on a separate internal listener — see
`docs/DEPLOYMENT.md` §7.

---

## 1. Every background job has a retry classification

One classifier (`reliability.classify_failure`) decides where a failure goes.
Handlers do not guess, and the worker does not have per-pipeline special cases
any more — the only rule it used to have was "is it an AI error?".

| Class | Queue outcome | Attempts spent | When |
|---|---|---|---|
| `transient` (outage) | `paused` | **none** | AI timeout / 429 / 5xx / breaker open / budget. Resumed by the watchdog when the probe is green, capped at `AI_PAUSE_MAX` (12) |
| `transient` | `queued` (backoff) | yes | Connection reset, DNS, upstream 5xx, DB `OperationalError`, pool exhaustion, our own deadline |
| `permanent` | `dead` | yes | Deleted row (`job_missing`), rejected credentials (401/403), quota, guardrail/`ValueError`, `TypeError`/`AttributeError` (a bug), unsupported dependency |
| `user_action` | `needs_input` | **none** | A CAPTCHA, an MFA code, a missing answer. **Never retried** — it expires instead (§4) |
| `unknown` | `queued` (backoff) | yes | Unrecognised exception. Retryable because the failure budget bounds it |

`KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are classified
`permanent/cancelled` and are never turned into a retry: a shutdown is not a
failure, and a cancelled task must stay cancelled so the worker can stop.

Every failure is counted once, with a bounded reason:

```promql
# Failure rate by retry class, per pipeline (5-minute window)
sum by (pipeline, kind) (rate(jobhunter_job_failures_total[5m]))
```

A `dead` row is visible to the user: it appears in the Queues page, in
`GET /api/ops/queue/dead` (owner), and — for auto-mode runs — as an
`automation_failed` notification. Nothing fails silently.

---

## 2. Metric catalog

All 26 metrics added for the background workflows, with the *only* label values
each may carry. The code enforces this table: a label value that is not listed
is recorded as `other` (`reliability.bounded_label`), so a caller cannot mint a
new series by passing request data.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `jobhunter_job_failures_total` | counter | `pipeline`, `kind`, `code` | Background-job failures by pipeline, retry class and bounded reason. |
| `jobhunter_onboarding_total` | counter | `stage`, `outcome` | Onboarding steps started, completed and failed. |
| `jobhunter_resume_extraction_seconds` | histogram | `outcome` | Resume extraction wall time, including the model call. |
| `jobhunter_discovery_run_seconds` | histogram | `result` | Discovery run wall time, end to end. |
| `jobhunter_discovery_jobs_found_total` | counter | `source`, `disposition` | Jobs persisted per discovery run, by board and disposition. |
| `jobhunter_source_fetch_outcomes_total` | counter | `source`, `outcome` | Job-board fetch outcomes — the numerator of source failure rate. |
| `jobhunter_search_provider_requests_total` | counter | `provider`, `outcome` | Paid search-provider calls by provider and outcome. |
| `jobhunter_search_provider_duration_seconds` | histogram | `provider` | Search-provider latency, excluding cache hits. |
| `jobhunter_search_provider_cost_microusd_total` | counter | `provider` | Estimated search spend in micro-USD, by provider. |
| `jobhunter_match_generation_seconds` | histogram | `scoring_path` | Match generation wall time by scoring path. |
| `jobhunter_high_fit_matches_total` | counter | `band`, `source` | Matches generated per fit band, by score provenance. |
| `jobhunter_artifact_generation_total` | counter | `artifact`, `outcome` | Generated artifacts (resumes, packets, cover letters, reports). |
| `jobhunter_artifact_generation_seconds` | histogram | `artifact` | Artifact generation wall time. |
| `jobhunter_application_prep_total` | counter | `outcome` | Application preparation outcomes. |
| `jobhunter_browser_session_passes_seconds` | histogram | `outcome` | Assisted browser pass wall time. |
| `jobhunter_user_action_pauses_total` | counter | `kind` | Times a run stopped and handed a step to the human. |
| `jobhunter_user_action_outcomes_total` | counter | `kind`, `outcome` | How user-action-required items ended. |
| `jobhunter_user_action_wait_seconds` | histogram | `kind`, `outcome` | How long a user-action-required item waited before it ended. |
| `jobhunter_user_action_expiry_total` | counter | `scope` | User-action-required states closed by the expiry sweep. |
| `jobhunter_auto_submit_total` | counter | `outcome`, `channel` | Automatic submission decisions and results. |
| `jobhunter_autofill_failures_total` | counter | `reason` | Autofill runs that did not complete, by bounded reason. |
| `jobhunter_dedupe_hits_total` | counter | `scope` | Duplicate work prevented, by the boundary that caught it. |
| `jobhunter_interview_events_total` | counter | `event`, `outcome` | Interview lifecycle events. |
| `jobhunter_report_generation_seconds` | histogram | `kind` | Outcome-report build time. |
| `jobhunter_reports_total` | counter | `kind`, `outcome` | Outcome reports generated. |
| `jobhunter_notifications_total` | counter | `kind`, `outcome` | In-app notifications written, by kind and outcome. |

Pre-existing metrics these sit next to (unchanged names, documented in
`docs/DEPLOYMENT.md` §7): `jobhunter_queue_*` (enqueued / completed / retries /
paused / resumed / dead / recovered / reclaimed / needs_input / depth),
`jobhunter_applications_total`, `jobhunter_autofill_runs_total`,
`jobhunter_application_session_pauses_total`, `jobhunter_source_fetch_total`,
`jobhunter_ai_requests_total`, `jobhunter_worker_*`.

Self-metrics about the registry itself, both gauges published on every scrape
and both labelled only with `metric`:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `jobhunter_registry_series` | gauge | `metric` | Live series currently held for that metric name. Watch for one climbing toward the cap. |
| `jobhunter_registry_evicted_series` | gauge | `metric` | Cumulative series evicted from that metric once `METRICS_MAX_SERIES_PER_METRIC` was hit. Non-zero means a label value was added that the catalog does not expect. |

### Cardinality rules (why there is no `user_id` label)

1. **No user, job, session or tenant identifier is ever a label value.** A
   series per user is a series per signup, and an in-process registry keeps
   every series for the life of the process.
2. **No free text**: not an exception message, not a URL, not a host, not a
   provider error. `reliability.bounded_label` collapses anything unmapped to
   `other` — a rising `other` means someone added a value without adding it to
   the catalog.
3. **Vocabularies come from the contracts**, not from the call site: `source`
   is the adapter registry, `kind` is `USER_ACTION_KINDS`, notification `kind`
   is `NOTIFICATION_KINDS`, match `band` is `MATCH_BANDS`.
4. `METRICS_MAX_SERIES_PER_METRIC` (default 512) is a backstop, not a licence:
   past the cap the least-recently-updated series is evicted and
   `GET /api/ops/status` reports it under `edge.registry.by_metric.*.evicted`.

```promql
# Cardinality health: live series per metric name, and what the cap has dropped.
# Published by the registry itself on every scrape. The "metric" label is
# bounded by the metric names in the source — the one place a label per metric
# name is correct, because the point is to see WHICH metric is growing.
topk(10, jobhunter_registry_series)
sum by (metric) (jobhunter_registry_evicted_series) > 0
```

The same numbers are on `GET /api/ops/status` as JSON at `edge.registry` —
`edge.registry.by_metric.<metric>.{series,evicted}` plus the configured
`edge.registry.max_series_per_metric`.

---

## 3. Documented queries

Each required metric, as the query an operator or a dashboard panel should run.
`$__rate_interval` is Grafana syntax; use `[5m]` elsewhere.

### Onboarding started / completed / failed

```promql
sum by (stage, outcome) (rate(jobhunter_onboarding_total[$__rate_interval]))

# Funnel drop-off: started but never completed, per stage
sum by (stage) (jobhunter_onboarding_total{outcome="started"})
  - sum by (stage) (jobhunter_onboarding_total{outcome=~"completed|blocked"})
```

### Resume extraction duration

```promql
histogram_quantile(0.95,
  sum by (le, outcome) (rate(jobhunter_resume_extraction_seconds_bucket[$__rate_interval])))

# Blocked extractions: the user has to act, the queue will not retry
sum by (outcome) (rate(jobhunter_resume_extraction_seconds_count{outcome="blocked"}[$__rate_interval]))
```

### Discovery jobs found

```promql
sum by (disposition) (rate(jobhunter_discovery_jobs_found_total[$__rate_interval]))

# New jobs per board — the "is one board starving the product?" question
topk(10, sum by (source) (rate(jobhunter_discovery_jobs_found_total{disposition="new"}[$__rate_interval])))
```

### Discovery source failure rate

```promql
sum by (source) (rate(jobhunter_source_fetch_outcomes_total{outcome="error"}[$__rate_interval]))
  /
sum by (source) (rate(jobhunter_source_fetch_outcomes_total[$__rate_interval]))
```

`outcome="empty"` is not a failure: it separates "the board is down" from
"nobody is hiring", which have completely different responses.

### Search provider usage

```promql
sum by (provider, outcome) (rate(jobhunter_search_provider_requests_total[$__rate_interval]))

# Spend, per day, in USD (the counter is micro-USD)
sum by (provider) (increase(jobhunter_search_provider_cost_microusd_total[1d])) / 1e6

# Cache/coalescing effectiveness — paid calls avoided
sum(rate(jobhunter_search_provider_requests_total{outcome=~"cached|coalesced"}[$__rate_interval]))
  / sum(rate(jobhunter_search_provider_requests_total[$__rate_interval]))

histogram_quantile(0.95,
  sum by (le, provider) (rate(jobhunter_search_provider_duration_seconds_bucket[$__rate_interval])))
```

### Match generation duration

```promql
histogram_quantile(0.95,
  sum by (le, scoring_path) (rate(jobhunter_match_generation_seconds_bucket[$__rate_interval])))
```

`scoring_path="cached"` (an idempotent replay) is deliberately its own series: mixing a
millisecond replay with an AI round trip produces a p95 that describes neither.

### High-fit recommendation count

```promql
sum by (band) (rate(jobhunter_high_fit_matches_total{band=~"strong|good"}[$__rate_interval]))

# Provenance mix — a board of estimates is not a board of AI verdicts
sum by (source) (rate(jobhunter_high_fit_matches_total[$__rate_interval]))
```

### Application preparation success

```promql
sum by (outcome) (rate(jobhunter_application_prep_total[$__rate_interval]))

sum(rate(jobhunter_application_prep_total{outcome="ready"}[$__rate_interval]))
  / sum(rate(jobhunter_application_prep_total[$__rate_interval]))
```

### Autofill success / failure

```promql
sum by (reason) (rate(jobhunter_autofill_failures_total[$__rate_interval]))
sum by (result) (rate(jobhunter_autofill_runs_total[$__rate_interval]))
```

### CAPTCHA / MFA pauses

```promql
sum by (kind) (rate(jobhunter_user_action_pauses_total[$__rate_interval]))

# A single portal's bot check tightening shows up as one kind climbing
topk(5, sum by (kind) (increase(jobhunter_user_action_pauses_total[1h])))
```

### User-action completion time

```promql
histogram_quantile(0.9,
  sum by (le, kind) (rate(jobhunter_user_action_wait_seconds_bucket{outcome="completed"}[$__rate_interval])))

# What fraction of asks the user never answers
sum(rate(jobhunter_user_action_outcomes_total{outcome="expired"}[$__rate_interval]))
  / sum(rate(jobhunter_user_action_outcomes_total[$__rate_interval]))
```

The p90 of `outcome="completed"` against `USER_ACTION_TTL_MINUTES` is the number
that decides whether the TTL is right. If the TTL is shorter than the p90, the
product is expiring work its users were still going to do.

### Auto-submit success / failure

```promql
sum by (outcome, channel) (rate(jobhunter_auto_submit_total[$__rate_interval]))

# "Auto-submit is on but nothing is going out"
sum(rate(jobhunter_auto_submit_total{outcome="refused"}[$__rate_interval]))
```

### Duplicate prevention

```promql
sum by (scope) (rate(jobhunter_dedupe_hits_total[$__rate_interval]))
```

A **falling** rate here is the alarm, not a rising one: these boundaries are
what stop a double application, a double email or a double charge. Scopes:
`queue`, `submission`, `action`, `search_cache`, `notification`, `upload`,
`extraction`, `event`.

### Interview events

```promql
sum by (event) (rate(jobhunter_interview_events_total{outcome="ok"}[$__rate_interval]))
```

### Reports and notifications

```promql
histogram_quantile(0.95,
  sum by (le, kind) (rate(jobhunter_report_generation_seconds_bucket[$__rate_interval])))
sum by (kind, outcome) (rate(jobhunter_notifications_total[$__rate_interval]))
```

### Artifact generation

```promql
sum by (artifact, outcome) (rate(jobhunter_artifact_generation_total[$__rate_interval]))
histogram_quantile(0.95,
  sum by (le, artifact) (rate(jobhunter_artifact_generation_seconds_bucket[$__rate_interval])))
```

---

## 4. User-action-required states always expire

Nothing that waits on a human waits forever, and nothing that waits on a human
is retried.

| State | Deadline | On expiry |
|---|---|---|
| `pipeline_jobs.status='needs_input'` | written into the row when it is parked (`payload.user_action.expires_at`), `USER_ACTION_TTL_MINUTES` (default 7 days) | row → `dead` with `user_action_expired`, `automation_failed` notification, `jobhunter_user_action_expiry_total{scope="queue"}` |
| `user_input_requests.status='pending'` | `created_at` + the same TTL | → `expired`, counted, wait time recorded |
| `application_actions` (`pending`/`in_progress`) | its own `expires_at` (the session TTL from the automation policy) | → `expired`, `jobhunter_user_action_outcomes_total{outcome="expired"}` |
| `application_sessions` | `expires_at` (policy TTL, never extended silently) | `expire_session`: purges the persisted cookie jar, closes its actions, requires re-authentication |

Deliberately **not** expired: a drafted resume, a generated packet awaiting
approval, or a written email draft. Those are artifacts the user owns, not
blocking states — expiring them would destroy work.

The sweep is `reliability.expire_user_actions`, run by the worker at boot and
every `USER_ACTION_SWEEP_INTERVAL_SECONDS` (default 900). It is idempotent by
construction: every scope selects only still-open rows and every transition is
terminal, so two workers running it at once find nothing to do.

**Expiry is never a retry.** A stale CAPTCHA or a fortnight-old question is not
something to run again; the row is finished with a reason the user can see and
the next run starts fresh.

```promql
sum by (scope) (rate(jobhunter_user_action_expiry_total[$__rate_interval]))
```

---

## 5. Worker restart safety

A restart must not lose, duplicate or corrupt work. The mechanisms, and the
query that shows each one working:

| Property | Mechanism | Query |
|---|---|---|
| In-flight work survives | every unit of work is a `pipeline_jobs` row; a claim takes a lease (`lease_expires_at`) | `jobhunter_queue_depth` |
| A dead worker's items come back | `recover_stalled` runs at boot and every `WORKER_REAPER_INTERVAL_SECONDS`, CAS-guarded, with a safety margin larger than the longest AI call | `rate(jobhunter_queue_recovered_total[5m])` |
| …without cloning a slow one | the safety margin (`WORKER_REAPER_SAFETY_SECONDS`, default 300) means a job still inside a model call is not re-queued under a live worker | `jobhunter_queue_reclaim_age_seconds` |
| …without bouncing forever | `reclaim_count` ≥ `WORKER_MAX_RECLAIMS` dead-letters a crash-looping row | `rate(jobhunter_queue_dead_total[5m])` |
| Attempts are not spent by a crash | a claim never increments `attempts`; only a recorded handler failure does | `rate(jobhunter_queue_retries_total[5m])` |
| Re-execution is safe | dedupe keys on enqueue, checkpoints per pipeline, upserts by unique key, `pending_event_ids` for events | `sum by (scope) (rate(jobhunter_dedupe_hits_total[5m]))` |
| A submission can never go twice | `application_submissions` ledger, reserved *before* anything is clicked, unique on `(user_id, idempotency_key)` plus a partial unique index on live rows | `rate(jobhunter_dedupe_hits_total{scope="submission"}[5m])` |
| A browser pass resumes, not restarts | the session checkpoint records per-field status; a resumed pass skips fields already filled | `rate(jobhunter_browser_session_passes_seconds_count[5m])` |
| Paused AI work resumes | `paused` rows are drained by the watchdog on a green probe, or by the user's one-click resume | `jobhunter_queue_depth{status="paused"}` |

The three budgets are independent (`attempts` / `paused_count` /
`reclaim_count`); mixing them is the v2.2.2 bug and
`docs/contracts/11-background-jobs.md` §8 pins the rule.

---

## 6. Logs: what is redacted

Every log line passes through `app.core.redaction` on its way out — in both the
JSON and the text formatter, after `%`-interpolation, so a secret that arrived
as a log *argument* is masked exactly like one baked into the format string.
`extra=` fields are walked recursively, and a sensitive *key name*
(`password`, `token`, `mfa_code`, `storage_state`, `cvv`, …) is masked
regardless of what its value looks like.

Masked: provider keys (`sk-…`, `ghp_…`, `xoxb-…`, Stripe), `Bearer`/`Basic`
credentials, `key=value` assignments for the known secret names, JWTs, email
addresses, formatted phone numbers, SSNs and Luhn-valid card numbers.

Deliberately not masked: user ids, job ids, request ids (the correlation keys
an operator needs), IPv4 literals, dates, and contiguous digit runs that are
not payment cards — a redacted timestamp during an incident costs more than it
protects.

The same pattern list backs the audit trail (`core/audit.py`), stored error
messages (`onboarding.safe_error_message`) and AI prompts
(`ai_client._scrub_prompt_secrets`), so a new secret shape is masked on every
channel at once. `backend/tests/test_privacy_redaction.py` and
`backend/tests/test_background_reliability.py` pin all four.

---

## 7. Alerts and runbooks

Alert rules live in [`ops/prometheus/alerts.yml`](../ops/prometheus/alerts.yml)
(not wired into any compose file by default — point your Prometheus at it).
Thresholds are starting points: they are set to catch a workflow that has
*stopped*, not to page on a bad hour.

### `JobHunterWorkerDown` — no worker is running

```yaml
absent(jobhunter_worker_running == 1) or sum(jobhunter_worker_running) < 1
```

*Symptom*: nothing is claimed; the queue grows. *Confirm*: `GET /api/ops/status`
→ `workers.tasks.alive` vs `expected`. *Fix*: restart the worker
(`python -m app.worker`) or the container; check the last log lines for the
crash that killed it. *Notes*: items are not lost — they are rows with a lease,
and recovery re-queues them at boot.

### `JobHunterQueueBacklog` — work is not draining

```yaml
sum(jobhunter_queue_depth{status="queued"}) > 100 for 15m
```

*Confirm*: depth by pipeline, and whether it is one pipeline. *Fix*: if
`jobhunter_queue_dead_total` is flat and `jobhunter_worker_item_errors_total`
is climbing, a handler is failing — read the `code` label on
`jobhunter_job_failures_total`. If the queue is deep but nothing is failing,
raise `WORKER_CONCURRENCY`.

### `JobHunterDeadLetterGrowing` — permanent failures are accumulating

```yaml
sum(rate(jobhunter_queue_dead_total[15m])) > 0.05 for 10m
```

*Confirm*: `sum by (pipeline, code) (rate(jobhunter_job_failures_total{kind="permanent"}[15m]))`.
*Fix by code*: `ai_blocked` → the key is missing/invalid or quota is gone
(Settings → AI, owner console); `not_found` → the row it referenced was
deleted, usually harmless; `auth` → a source credential was revoked;
`bug` → a real defect, file it with the item's `error`. Retry the batch with
`POST /api/ops/queue/retry-dead` (owner) once the cause is fixed.

### `JobHunterTransientFailuresHigh` — a dependency is degraded

```yaml
sum(rate(jobhunter_job_failures_total{kind="transient"}[10m])) > 0.5 for 10m
```

*Confirm*: which `code` — `ai_transient`, `network`, `upstream`, `database`,
`rate_limited`. *Fix*: these self-heal (pause + watchdog, or backoff). Act only
if it persists: `ai_transient` → check the provider status page and the breaker
(`jobhunter_ai_requests_total{status="breaker_open"}`); `database` → connection
pool exhaustion, check `DATABASE_POOL_SIZE` and slow queries; `rate_limited` →
lower the run cadence.

### `JobHunterSourceFailing` — one job board is broken

```yaml
(sum by (source) (rate(jobhunter_source_fetch_outcomes_total{outcome="error"}[15m]))
  / sum by (source) (rate(jobhunter_source_fetch_outcomes_total[15m]))) > 0.5
  for 20m
```

*Confirm*: is it one source or all of them (all → egress/DNS, not the board).
*Fix*: a gated or changed board is disabled in the admin console
(`discovery.live_sources`), not patched at 3am. Users keep getting results from
the other adapters; discovery reports the source as unavailable rather than
silently returning nothing.

### `JobHunterUserActionsExpiring` — asks are timing out unanswered

```yaml
sum(rate(jobhunter_user_action_expiry_total[1h])) > 1 for 30m
```

*Confirm*: which `scope`, and the p90 of
`jobhunter_user_action_wait_seconds_bucket{outcome="completed"}`. *Fix*: if the
completion p90 is near `USER_ACTION_TTL_MINUTES`, the TTL is too short — raise
it. If users are not seeing the asks at all, check
`jobhunter_notifications_total{outcome="failed"}`.

### `JobHunterAutoSubmitRefused` — consent or quota is blocking submissions

```yaml
sum(rate(jobhunter_auto_submit_total{outcome="refused"}[30m])) > 0
  and sum(rate(jobhunter_auto_submit_total{outcome="success"}[30m])) == 0
```

*Confirm*: `jobhunter_automation_policy_*` and the policy's `reason` in the
job's `policy_decision`. *Fix*: this is usually *correct* — a withdrawn consent,
a daily cap, or a confidence gate. Do not "fix" it by widening the policy; tell
the user what is blocking them.

### `JobHunterDedupeStopped` — a duplicate boundary went quiet

```yaml
sum(rate(jobhunter_dedupe_hits_total[1h])) == 0
  and sum(rate(jobhunter_queue_enqueued_total[1h])) > 10
```

*Why this matters*: these counters are the evidence that double execution is
still being prevented. Zero hits while work is flowing means a dedupe key
stopped matching — the failure mode is a *duplicate application*, which is the
one mistake this product cannot take back. *Fix*: check the recent deploys for a
changed `dedupe_key` format, and inspect `application_submissions` for two live
rows on one job.

### `JobHunterArtifactFailures` — generation is broken

```yaml
sum by (artifact) (rate(jobhunter_artifact_generation_total{outcome="failed"}[15m])) > 0.05
  for 10m
```

*Confirm*: which artifact. *Fix*: `resume_pdf`/`resume_docx` → the renderers
(`reportlab`, `python-docx`) or a profile shape they do not expect; `packet` →
usually a guardrail rejection, which is `outcome="rejected"` and not this alert.

### `JobHunterMetricsCardinality` — someone labelled on unbounded data

```yaml
sum by (metric) (jobhunter_registry_evicted_series) > 0
```

*Fix*: `GET /api/ops/status` → `edge.registry.by_metric` shows which metric hit
`METRICS_MAX_SERIES_PER_METRIC`. Find the call site, map the value into the
catalog's vocabulary (or collapse it to `other`), and add the new value to
`METRIC_CATALOG` if it is genuinely bounded.

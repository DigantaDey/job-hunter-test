# Changelog

All notable changes to JobHunter AI are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [2.1.2] — 2026-09-13

The funding radar is now **actually AI-driven**: one grounded model pass decides
relevance, order and the reason — and when that pass cannot happen, the radar
says so instead of handing over a list the model never saw.

### Changed

- **AI ranking is a hard dependency** (`funding_radar.scan_funded_companies`).
  The `if await _ai_enabled():` gate is deleted along with the function: with no
  key (or an invalid/undecryptable one) the scan raises the standard v2.1
  blocked outcome — `reason="no_api_key"` / `"stored_key_unreadable"`,
  `state="blocked_needs_action"`, `status="ai_blocked"`, fix pointing at
  Settings → AI API — **before any provider is called**, so a blocked radar
  burns no outbound requests and no quota. A transient outage
  (timeout/unreachable/429/5xx/breaker) stays pausable: `GET /funding/companies?refresh=true`
  answers `503 status="ai_paused"` + `retry_after_hint`, and the queued
  `handle_funding` item takes the existing worker path (transient → `pause()`,
  blocked → `fail(retryable=False)`). `worker.py` and `job_queue.py` are
  untouched. **No code path returns events the model never ranked.**
- **One grounded AI pass replaces the keyword filter.**
  `funding_sources.ai_rank_events` does relevance-filter + rank + explain in a
  single `chat_completion("funding_scan", …)` (contract marker
  `funding-scan-v2`) and returns `(RankedEvent(event, matched, rank, why),
  diagnostics)`. `why` is one sentence citing the event's own fields, cleaned of
  markdown/HTML and capped; it is stored per row (`meta.why`) and returned by
  `GET /api/funding/companies`. Grounding guardrails: a company name that is not
  in the fetched event set rejects the whole answer (`guardrail_failed` →
  blocked/dead, never a fallback to the unranked list); stage, amount, date,
  source and URL always come from the event, and a model that restates them
  differently is counted in `diagnostics.field_conflicts` and ignored. Corporate
  suffixes are tolerated ("ACME INC." → "Acme") only when the match is
  unambiguous. Events the model did not mention come back `matched=False` —
  silence is never a match. `_matched_keywords` survives **as a provenance label
  only**.
- **The model judges a wider pool than the radar shows**: `min(60, limit × 2)`
  events are fetched and ranked, `limit` are returned — "the freshest 18 were
  all irrelevant" no longer hides a matching event from a week earlier.
- **Provider robustness.** Each provider runs under
  `FUNDING_PROVIDER_TIMEOUT_SECONDS` (new, default `20`) and is retried **once**
  on a transport error — never on a 4xx (`FundingProviderError.status`) and never
  on an SSRF-guard/robots verdict. Providers no longer swallow their own
  failures into an empty list: a non-200, a non-JSON body or a broken import
  export becomes `report.errors[provider_id]`, and a provider that was requested
  but is not configured is reported as `not_configured` (choosing `crunchbase`
  without a licence no longer looks like "nobody raised money").
- **`GET /api/funding/companies` gained `scan_status` + `last_report`**
  (`ok | scan_failed | paused | blocked`) plus `reason` and `scanned`. When
  **every** requested provider fails, the scan returns `[]` with
  `scan_status="scan_failed"`, `reason="provider_errors"` and the per-provider
  errors; partial success stays `ok` with `last_report.errors` visible. The most
  recent report is persisted per user as the settings row
  **`funding.last_report`** (chosen over `FundingCompany.meta` because the
  report matters most exactly when the scan produced no rows to hang it on; it
  is server-written and deliberately not in `WRITABLE_KEYS`, so a client cannot
  forge a green scan) and is stored by both the sync endpoint and the queued
  handler. An explicit `?refresh=true` that cannot be ranked answers with the
  outage shape; an *implicit* refresh (page open) records it and still returns
  the rows already on the radar, so a broken key never blanks the page.
- **Empty-but-legit is a first-class result**: zero relevant matches after the
  AI pass → `scan_status="ok"`, `companies: []`,
  `reason="no_matching_events"`, and the UI reads "Scanned N events, none
  matched your focus." Unrelated events are never returned to fill the gap.
- **Dedupe and stability.** `sync_funding_db` looks rows up by
  `normalize_company_name` (strip + collapse whitespace + casefold) and stores
  the provider's canonical casing from first sight, so "Stripe" then "stripe" is
  one row. `discovered_at` is now **first seen** and is never rewritten; the new
  `funding_companies.last_seen_at` is the prune clock
  (`COALESCE(last_seen_at, discovered_at)`), prune is a single bulk `DELETE`
  served by the new `ix_funding_user_seen` index, and it only runs after a scan
  that succeeded — an outage no longer prunes the radar. Provider `meta` is
  merged rather than replaced (filing URL/CIK survive), while `open_positions`
  is deliberately never carried over from an older scan. Migration
  `b2c3d4e5f6a7` adds and back-fills `last_seen_at`, drops
  `has_open_positions` and collapses the duplicates the old upsert already wrote
  (keeping the earliest row of each group); prune semantics are documented in
  the `funding_radar` module docstring.
- **`process_company` is decided by provider facts, not a dead flag.** If the
  row's `meta` carries real `open_positions` (a newly documented field of the
  `imported` / `crunchbase` / `tracxn` providers) it tracks that posting as a
  Job labelled `positions_source="provider"`; otherwise it goes straight to the
  founder-outreach draft, which still requires approval. Such a Job is scored
  `score_source="funding_context"` /
  `"Scored from funding context — no job description available"` and its stored
  description contains only factual funding-event and provider-posting text.
- **`funding_needs_refresh` uses `FUNDING_REFRESH_HOURS`** (new, default `12`),
  measured from the last scan attempt (`last_report.attempted_at`, falling back
  to the newest row for pre-2.1.2 data) instead of being derived from the
  freshness window.
- **Frontend (`Funding.tsx`)** renders the new contract: the shared
  `AIOutageBanner` for a 503, plus persisted-state cards for `scan_failed`
  (per-provider errors + "Try the scan again"), `paused` ("will resume
  automatically — no unranked list is shown as a result") and `blocked` (link to
  Settings → AI API), a "Partial scan" notice when only some providers failed,
  the AI's `why` on every card, provider provenance (`SEC EDGAR Form D • verified`
  / `synthetic demo data`), real provider-reported openings, and honest empty
  states ("Scanned 34 events, none matched your focus.").

### Removed

- **`FundingCompany.has_open_positions`** — a flag no code path ever set to
  `True`. It left the API response and the UI (where it rendered an "open
  roles" / "no opening" badge and chose the button copy), and the branch it
  gated is now driven by `meta.open_positions`.
- **The fabricated job posting.** `process_company` no longer builds
  `"Open Role at {name}"` described as `"{name} recently raised {stage}…"` and
  no longer feeds that invented text to the AI job scorer
  (`"Engineering role at …"` is gone from the backend).
- **The keyword-string relevance filter** (and its inverted form) from
  `scan_funded_companies`; `keyword_score` is no longer imported there.

### Fixed

Also fixed, all inside the funding-radar surface:

- **Inverted relevance filter** — `relevant = [...]; if relevant: events = relevant`
  meant zero keyword matches *disabled* the filter and returned every unrelated
  event. Relevance is now the model's verdict and "nothing matched" returns
  nothing (`reason="no_matching_events"`).
- **Silent unranked list** — no AI key used to return the raw provider list as
  though it were the product.
- **All-providers-failed looked like an empty radar** — every provider exception
  was swallowed into `report["errors"]` that nothing surfaced; now it is
  `scan_status="scan_failed"` in the response, in `funding.last_report` and in a
  `funding.scan_failed` audit row.
- **Fabricated JD scoring** (above) — a guessed context was scored as if it were
  a real posting.
- **Duplicate rows and churn** — case-sensitive upsert, `discovered_at`
  rewritten on every sync, prune as a per-row `DELETE` loop, one `SELECT` per
  company in `sync_funding_db` (N+1).
- **Refresh interval math** — `window_days * 12 hours` coupled two unrelated
  knobs (a 45-day window meant ~22.5 days between scans).
- **`POST /funding/{name}/process` used `ilike(company_name)`** — `%`/`_` in the
  path acted as wildcards over that user's rows, and the lookup ignored the
  casing normalisation the sync applies. It now matches on the same normalised
  key (with an exact-match fast path).
- **Funding Jobs shared the default `dedupe_key` (`""`)** — two provider
  postings for one user would have collided on `UNIQUE(user_id, dedupe_key)`;
  each now gets `funding_radar:<sha256(company|title)[:24]>`.
- **`include_unverified` was only honoured on the non-refresh branch**, so the
  same page hid unverified rows right after a scan and showed them on the next
  open.
- **An event with no parseable date was stamped `raised_at = utcnow()`** and
  rendered as "raised today"; it is now flagged `raised_at_estimated` in `meta`,
  exposed on the row, and the UI shows "date not disclosed".
- **`refreshed_at` was `max(discovered_at)` of the *filtered* rows** (and with
  first-seen semantics would have gone stale); it now comes from the persisted
  report's `scanned_at`.
- **Misleading copy**: the radar header claimed a fixed "fresh ≤45d" (now the
  user's actual `window_days`), "AI-matched to your profile" is now true, and
  the per-card button no longer promises "Apply usual flow" for a company nobody
  knows is hiring ("Track the open role" only when the provider reported one,
  otherwise "Draft founder outreach"). `Jobs.tsx` learnt a `funding_context`
  score badge so the new provenance does not render as a raw snake_case string.

### Added

- **New env knobs:** `FUNDING_PROVIDER_TIMEOUT_SECONDS` (default `20`) and
  `FUNDING_REFRESH_HOURS` (default `12`), both documented in `.env.example`.
- **Imported-dataset schema:** `careers_url` and `open_positions`
  (`[{title, url, location, summary}]`, a JSON list, or
  `"title::url|title::url"`) — the only route by which real hiring data reaches
  the apply flow.
- **New metrics:** `jobhunter_funding_provider_errors_total{provider}`,
  `jobhunter_funding_provider_retries_total{provider}`,
  `jobhunter_funding_pruned_total`, `jobhunter_funding_ai_matched_total`.
- **New audit action:** `funding.scan_failed` (with `scan_status`, `reason` and
  the per-provider errors).
- **Response fields:** `scan_status`, `last_report`, `reason`, `scanned` on
  `GET /api/funding/companies`; `why`, `rank`, `open_positions`,
  `raised_at_estimated`, `last_seen_at` per company; `positions_source`,
  `score_source` and `url_source` on the `process` result.
- **Tests** (`backend/tests/test_funding_radar.py`, extended in place): blocked
  refresh with no key (no companies, no rows, no provider calls) and the
  implicit-refresh variant that keeps the last good rows; queued blocked and
  guardrail outcomes dead-lettering without persisting a fabricated row; one
  grounded pass proving the unranked path is gone; 5 events → exactly the 2 the
  model matched (and 0 → `no_matching_events`); stage/amount taken from the
  event while the model's conflicting values are recorded; suffix-tolerant
  grounding; unusable/empty model answers surfaced; provider timeout + single
  retry, 4xx with no retry, all-failed `scan_failed` (response + report + audit),
  partial failure staying `ok` with visible errors, requested-but-unconfigured
  providers; `"Stripe"`/`"  stripe "` → one row with merged meta; first-seen
  preservation, prune on `last_seen_at`, undated rows never pruned;
  `funding_needs_refresh` interval math; persisted `last_report`;
  `include_unverified` on refresh; `process_company` drafting a founder email
  when no positions are reported, tracking exactly one honest Job per company
  (idempotent, with its own `dedupe_key`) and the wildcard-free lookup; and the
  alembic upgrade of a populated 2.1.1 database (back-fill, duplicate collapse,
  `DROP COLUMN`, index, reversible downgrade). Wire-level (`real_ai`, scripted
  provider): sync outage → pausable 503 with `retry_after_hint`, and queued
  outage → `paused` → drained → `done` with rows and exactly two wire attempts.

### Found but not fixed

Out of scope for this change (recorded with file:line rather than touched):

- `backend/app/services/user_settings.py:388` — `grouped()` dumps every stored
  settings row into its category bucket (only `ai_workflows` is skipped), so
  server-written rows now surface in `GET /api/settings`: `funding.last_report`
  is readable by the client and simply ignored by the Settings UI. There is no
  read-side allowlist to hide internal/report rows.
- `backend/app/services/user_settings.py:228` — `set_setting()` never consults
  `WRITABLE_KEYS`; the allowlist is enforced only by `apply_updates()` on the
  router path. "A client cannot forge a green scan" therefore rests on the
  router, not on the storage layer, and any future internal caller can write a
  key the API contract calls read-only.
- `backend/app/models/models.py:204` + `backend/app/schemas/schemas.py:63` —
  `jobs.score_source` is a free-form `String(20)` / `Optional[str]` with the
  allowed set documented only in a comment. Nothing validates it, so a new or
  misspelled source renders as a raw snake_case chip in
  `frontend/src/pages/Jobs.tsx` until someone adds a badge entry (this PR had to
  add one for `funding_context`).
- `backend/app/core/metrics.py:110,131,170,201` — pre-existing mypy errors
  (`int(<object>)` overloads, `__exit__ -> bool`, `object` not iterable) that
  are part of the 772-error baseline. Left alone; the A/B for this PR adds zero
  new errors and removes eight.

## [2.1.1] — 2026-09-13

The pipeline queue is now **self-healing**, and the "AI queue" is a **real
durable queue** — the same `pipeline_jobs` table every other pipeline uses,
not an in-memory deque that nothing ever wrote to.

### Added

- **`ai_queue` block in `GET /api/pipelines/stats`** — the stats endpoint the
  Queues page polls. `queue_preview` lists up to 3 in-flight items with
  `pipeline='ai'` (ordered by priority, then arrival) as
  `{id, workflow, priority, status}` with `status` in
  `queued|processing|paused|needs_input`; `processing_now` is the claimed
  item or `null`. Built exclusively from real `pipeline_jobs` rows —
  `tag_resume` work from resume uploads/generation is now visible in the
  "Next in AI queue" panel, and the panel drains to `[]`/`null` when the
  queue is empty.
- **`workers.tasks` in `GET /api/ops/status`** —
  `{"expected": N, "alive": N, "last_crash": {"task", "at", "error"} | null}`
  so operators can see a slot that died and is in respawn backoff.
- **New env knobs:** `WORKER_RESPAWN_BACKOFF_SECONDS` (default `0.25`) and
  `WORKER_CRASH_BACKOFF_MAX_SECONDS` (default `2.0`) — the supervisor's
  capped respawn backoff.
- **New metrics:** `jobhunter_worker_item_errors_total{pipeline=...}` (an item
  whose execution escaped `_run_item`) and
  `jobhunter_worker_task_crashes_total{task=...}` (a worker slot/watchdog that
  died and was respawned).
- **Tests:** loop liveness across a crashing handler, supervisor respawn of a
  dead slot (crash metric + logged traceback), the `ai_queue` stats contract
  (queued → processing → done, empty case, priority ordering), and restart
  durability (a row forced to `processing` with an expired lease is returned
  by `recover_stalled` at startup and completed by the worker — no
  duplicates).

### Changed

- **Self-healing worker slots.** A single bad item can no longer kill a worker
  slot: `_loop` wraps `_run_item`, so any exception is logged with the item
  id, counted (`jobhunter_worker_item_errors_total`) and the loop keeps
  serving — the row is left to the existing lease machinery
  (`recover_stalled` re-queues it once the lease expires). A new **supervisor**
  in `Worker.start()` watches every child task (claim loops + AI watchdog): a
  task that dies is logged once with its traceback, counted
  (`jobhunter_worker_task_crashes_total{task=...}`) and respawned with a small,
  capped backoff; `stop()` suppresses respawns. The old
  `gather(return_exceptions=True)` — which swallowed every crash silently and
  quietly drained capacity to zero — is gone.

### Removed

- **The in-memory `app/services/ai_pipeline.py`.** It was an in-process
  priority deque that (a) no code path ever enqueued into, (b) marked
  executor-less items "done" after a fixed sleep with a fabricated result,
  and (c) evaporated on restart. The AI pipeline is the durable
  `pipeline_jobs` queue — real rows only. The `ai_pipeline.running` hooks in
  `worker.py`/`main.py` went with it.

## [2.1.0] — 2026-09-13

AI becomes a **hard dependency with an honest pause/resume contract**, and the
token budgets that govern every prompt and answer are **per-user settings**.
From now on the product either shows a real AI result or explains precisely
why the model could not answer — it never substitutes a guessed value and
never presents one as AI output.

### Added

- **One central AI-availability signal.** `ai_client.ai_availability()`
  (backed by `ping()` + the breaker + the daily budget) reports exactly three
  states for a user: `online`, `transient_outage` (timeout / unreachable / 429
  / 5xx / breaker open / budget exhausted — retrying later is meaningful) and
  `blocked_needs_action` (no key / invalid key / undecryptable key / provider
  quota / unknown model / plan limit — retrying is pointless until the user
  acts). **Decision: `quota_exceeded` is blocked, not transient** — a 429
  rate window resets on its own, but an account with no credits stays broken
  until topped up. The probe's generic `status_NNN` verdicts are normalised
  into this stable vocabulary (`ai_client.normalise_ping_reason`).
- **Pausable failures.** Synchronous AI calls that hit a transient outage
  return a dedicated 503 (never a generic 500) carrying
  `code=ai_unavailable`, `state=transient_outage`,
  `status="ai_paused"`, `pausable=true`, a `retry_after_hint` and a non-blank
  actionable `detail`, on top of the v2.0.5 `attempts` /
  `possibly_billed` accounting. Blocked failures return
  `state=blocked_needs_action`, `status="ai_blocked"` and a fix pointing at
  Settings → AI API — with exactly one wire attempt (no retry storm on a
  rejected key).
- **Queue pause/resume.** Queued work that hits a transient AI outage is
  **paused** (`job_queue.pause`): re-queued with backoff, never marked failed
  or completed, and the pause does *not* consume an attempt (an outage is not
  the work's fault; after 12 consecutive pauses it is dead-lettered with its
  error, so a pathological case cannot loop forever). A new **AI watchdog**
  (`ai_watchdog.py`, runs with the worker) re-probes on a schedule
  (`AI_WATCHDOG_INTERVAL_SECONDS`, 0 disables) and drains paused work the
  moment the signal is green. `POST /api/settings/ai/resume` is the one-click
  equivalent (force drain). All re-executed work is idempotent: AI calls run
  before any side effects, discovery dedupes by `dedupe_key`, and usage is
  increment-only on actually-inserted rows.
- **Per-user token budgets** (Settings → AI API, same pattern as `ai.timeout`):
  `ai.max_input_tokens` (256–200000) and `ai.max_output_tokens` (64–16000),
  editable by the owner and paid tiers, read-only on free — enforced
  **server-side** (a free-tier PUT is rejected with 403
  `upgrade_required`, not merely hidden). The gateway obeys them as the single
  source of truth: every outgoing `max_tokens` is clamped to the user's
  output ceiling (the PR #29 automatic escalation can never exceed it), and
  every prompt/document spliced into a prompt is clamped to the input budget
  (`ai_client.input_budget_chars` / `fit_prompt_part`). **Over-context
  decision: clamp-with-warning** — an oversized document still produces a
  (shorter) honest request and the ledger flags `input_truncated`; it is
  never rejected.
- **`/api/settings/ai/status`** now reports the three-state `state`,
  `retry_after_hint` and `paused_count` alongside the existing health data.
- **Scripted-provider test harness** (local OpenAI-compatible server in
  `tests/conftest.py`, hermetic `real_ai` convention): mid-run outages,
  recoveries, rejected keys and abnormal model answers — 14 new tests cover
  the pausable 503 (nothing persisted before the AI call succeeds), blocked
  no-retry-storm, pause→recover→complete-without-duplicates, watchdog
  drain-only-when-online, the status/resume endpoints, budget round-trips,
  tier gating, wire-level `max_tokens` clamping, the escalation ceiling and
  over-context clamping.

### Changed

- **Deleted: every heuristic / offline / silent fallback on the AI path.**
  Upload parsing, search-context extraction, resume tailoring/tagging/cover
  letters, cold emails + subject lines, scoring, company-size classification
  (interactive), company intel, interview questions/feedback, form
  detection, funding scans, persona portrait/tracks — none of them catches an
  AI failure and substitutes a regex/keyword/canned answer anymore. Each now
  raises the typed outage (pausable or blocked) or fails the queued item
  honestly. The deterministic helpers that remain are *labelled by
  provenance*, never presented as AI: `deterministic_company_size`
  (bulk-labelling jobs outside the AI top-N of a discovery run — a documented
  cost control, recorded in `company_size_confidence`), `mined_context`
  (the deterministic merge base unioned into *successful* AI extractions, and
  the degenerate answer for an account with no data to read), and
  `diagnostic_preview_extract` (diagnostics/tests only, `degraded=true`).
  Names were changed accordingly (`heuristic_*` → the names above) so no
  function can be mistaken for an AI fallback.
- **`max_tokens` is a named budget per task** — `PARSE_OUTPUT_TOKENS` (4000),
  `SCORING_OUTPUT_TOKENS` (1200), `EMAIL_OUTPUT_TOKENS` (900),
  `QUESTIONS_OUTPUT_TOKENS` (2000) / `FEEDBACK_OUTPUT_TOKENS` (1000),
  `TAILOR_OUTPUT_TOKENS` (3000) / `COVER_LETTER_OUTPUT_TOKENS` (1200) /
  `TAG_OUTPUT_TOKENS` (300), `INTEL_OUTPUT_TOKENS` (800) — each clamped to
  the user's output ceiling by the gateway; no hardcoded `max_tokens=` or
  prompt `[:N]` slice remains on any AI path (the anti-hallucination fact
  ledger's local memory bound `MAX_LEDGER_CHARS` is not a prompt cap).
- **Env defaults:** `AI_MAX_OUTPUT_TOKENS` is now 16000 (the ceiling; per-task
  starting budgets are the named constants above), new `AI_MAX_INPUT_TOKENS`
  (12000) and `AI_WATCHDOG_INTERVAL_SECONDS` (15).
- `AI_API_KEY=` empty no longer means "heuristic mode" — it means the app
  boots in `blocked_needs_action` and says so.

### Fixed

- Services that imported the gateway at module level
  (`classifier`, `keyword_extractor`, `interview_prep`, `company_intel`,
  `resume_generator`) now resolve `chat_completion` at call time, matching
  the rest of the codebase — test doubles (and hot-swapped providers) work
  uniformly.
- `test_company_classification_heuristics` (which asserted the deleted
  classify fallback) is replaced by `test_company_classification_is_the_ai_verdict`
  plus scripted-provider outage coverage.

## [2.0.5] — 2026-09-12

Fixes the "`AI unavailable — nothing was generated` / `unreachable` with a blank
`http_error:` detail" outage reported while AI-parsing an uploaded resume on a
slow free-tier reasoning model (`z-ai/glm-5.3-free`) — even though the provider
dashboard showed the requests arriving and consuming tokens. Seven independent
root causes were found; each one alone misdiagnoses or fails a slow-but-healthy
generation.

### Fixed

- **A slow model was cut off after 30s and misreported as `unreachable`.**
  The gateway waited a flat 30s per attempt (`AI_TIMEOUT`), then labelled the
  resulting `httpx.ReadTimeout` — whose string form is literally `""` — as
  `http_error: ` and mapped that prefix to `unreachable` ("DNS/TLS/connection
  failure"). The default wait is now 300s (deliberate, documented,
  configurable per user in Settings → AI API and via `AI_TIMEOUT`), the
  TCP+TLS connect phase keeps its own short deadline (`AI_CONNECT_TIMEOUT`,
  10s) so genuine connectivity failures still fail fast, and an expired
  generation wait is reported honestly as `timeout` with the model, the wait,
  the attempts spent and a billing caveat — never blank, never `unreachable`.
- **Timeouts were retried blindly, multiplying the user's bill.** Every expired
  wait was retried up to `AI_MAX_RETRIES` (3), so one upload spent — and the
  provider billed — three full generations while the ledger claimed
  `0 tokens • $0.0000`. Read/write waits are no longer retried automatically
  (the provider accepted the request and may still be generating); connect
  failures and 429/5xx still are (the provider never saw those). The ledger
  row for a timed-out call carries `attempts`, `timeout_seconds`,
  `possibly_billed` and `usage_unknown` instead of a silent zero.
- **The reason classifier short-circuited on the `http_error` prefix.**
  `reason_from_message` mapped any `http_error:` message to `unreachable`
  before checking timeout keywords, so even an explicit "timed out" became a
  connectivity failure. Timeout keywords now win (except connect-phase waits,
  which honestly stay `unreachable`), and the gateway passes explicit reasons
  for transport failures instead of relying on string sniffing.
- **The outage probe could overwrite a precise reason with a worse one.**
  `describe_ai_error` let any probe verdict replace the exception's reason —
  a slow model makes the short-deadline probe fail too, turning a correct
  `timeout` into `unreachable`. Only definitive config verdicts
  (`invalid_api_key`, `quota_exceeded`, `model_unavailable`, …) now override,
  and a blank detail falls back to the reason's own message so no failure ever
  renders an empty explanation.
- **The wait budget was env-only and ignored per-user settings.**
  `ai.timeout` (5–1800s) is now a first-class per-user setting with validation,
  resolved per call alongside `max_retries` (which the gateway previously read
  from env only); explicit per-call overrides still win. The hardcoded
  15–90s per-call timeouts (`classify`, `form_detect`, `funding_scan`,
  `tagging`, `resume_gen`) are removed — one generous, configurable knob
  governs every workflow, so no artificial deadline cuts off a healthy
  generation.
- **The browser gave up before the backend did.** Axios waited 30s globally
  and 120s for uploads — both shorter than a legitimate reasoning-model
  generation. AI-backed requests (upload, generate, score, draft, reflect,
  …) now share a documented 10-minute budget (`AI_REQUEST_TIMEOUT_MS`,
  covering two backend attempts plus a repair pass); fast endpoints keep the
  30s fail-fast default.
- **Money honesty.** Timed-out attempts are labelled "may still have been
  billed" in the error detail, the ledger meta and the `timeout` fix text, and
  every failure/success row records the attempts spent.

### Tests

- New `tests/test_ai_timeout_fixes.py` drives the real gateway against a local
  scripted provider: the slow-reasoning upload succeeding end-to-end, the
  honest `timeout` failure (correct reason, non-empty actionable detail,
  attempts + billing caveat in the ledger, exactly one wire attempt), a
  genuine connectivity failure (still `unreachable`, fast, non-blank), the
  per-user timeout knob, the probe-override guard, and the connect-vs-read
  timeout distinction. The PR #29 suite (`test_ai_invalid_json_fixes.py`,
  19 tests) passes unchanged.

## [2.0.4] — 2026-09-12

Fixes the "`AI unavailable — nothing was generated` / `invalid_json: model did
not return valid JSON (preview: '')`" outage reported while AI-processing an
uploaded resume. Six independent root causes were found; each one alone can
make a resume upload fail even though the provider, key and model are all fine.

### Fixed

- **An empty model answer was raised instantly as `invalid_json`.** The gateway
  trusted any HTTP 200: when `choices[0].message.content` came back empty —
  standard behaviour for reasoning models whose "thinking" phase
  (`reasoning_content`) consumed the whole `max_tokens` budget, and a known
  intermittent flake of free-tier providers — the call failed with the
  misleading `invalid_json (preview: '')` and the UI told the user to "retry"
  while nothing retried anything. The gateway now inspects `finish_reason`,
  salvages a complete JSON draft from the reasoning channel, and retries
  automatically with a targeted fix before ever reporting a failure.
- **`finish_reason` was never inspected — truncated JSON was unrecoverable.**
  A resume profile is a large JSON document; when the model ran out of output
  tokens mid-object (`finish_reason=length`) the call failed with
  `invalid_json`. The gateway now escalates `max_tokens` (×2, ×4 when the
  thinking phase ate the budget, capped at 16 000) and retries — and if a
  provider rejects the escalated value as too large, it clamps automatically.
- **`AI_MAX_OUTPUT_TOKENS` defaulted to 1200** — smaller than the JSON a full
  resume profile needs, so parses truncated on perfectly healthy models. The
  default is now 4000 (documented in `.env.example`).
- **Answers that arrive as a parts list were mis-parsed.** Providers returning
  `content` as `[{"type": "text", "text": …}]` were `json.dumps`ed into a JSON
  *array*, whose first element (`{"type": "text", …}`) masqueraded as the
  model's answer and failed the guardrail. Text parts are now joined properly;
  `null` content and legacy `choices[0].text` bodies are also handled.
- **Modern OpenAI-compat models 400 on `max_tokens` / `temperature`.** Only the
  `response_format` fix-up existed. The gateway now also renames to
  `max_completion_tokens`, drops `temperature`, and clamps oversized budgets
  when the provider's 400 names the offending parameter — none of these
  fix-ups consume a retry.
- **Failures were reported dishonestly.** Every 200-but-unusable answer was
  labelled "The model answered with text that is not valid JSON" — wrong and
  useless for an empty or truncated answer. Three precise reasons were added —
  `empty_response`, `truncated_response`, `content_filter` — each with an
  actionable fix rendered by the existing AI-outage banner in the UI.
- **Lenient JSON extraction hardened.** Invisible characters (BOM, zero-width)
  are stripped, the *last* fenced block is preferred (repair loops emit
  several), and the model's final complete draft is salvaged from a reasoning
  trace by scanning balanced objects from the end.
- Usage/credit accounting now sums every wire attempt of a logical call
  (retries included) instead of only the last one.

### Tests

- New `tests/test_ai_invalid_json_fixes.py` (19 tests) drives the real gateway
  against a scripted OpenAI-compatible provider: the empty-content reasoning
  model, the truncation escalation, the response_format drop, the parts-list
  body, the reasoning-channel salvage, all three 400 fix-ups, the honest
  failure reasons, and the end-to-end resume upload surviving a flaky provider
  — plus the upload reporting `empty_response` when the provider never
  produces an answer.

## [2.0.3] — 2026-09-11

Fixes the "AI offline / `invalid_api_key` even after adding an API key in
Settings" defect class. Four independent root causes were found; each one alone
can make the app claim the key is invalid or ignore it entirely.

### Fixed

- **Real AI features never used the key saved in Settings.** Every pipeline
  (scoring, resume parsing, email generation, keyword extraction, …) called the
  AI gateway without user context, so configuration resolved from *env only* —
  the per-user key stored via Settings → AI API was consulted solely by the
  status dot. AI calls therefore failed with `no_api_key` and silently fell
  back to heuristics ("AI still offline") even with a valid key saved. The
  gateway now resolves the ambient request/worker user (the `user_id_var`
  context the auth layer and worker already maintain), so the user's key —
  with owner and env fallback — powers every call, and per-user credit
  accounting/entitlements finally see all operations.
- **The status probe reported `invalid_api_key` for keys that work.** The dot
  was judged purely by `GET {base_url}/models`: providers that restrict or do
  not implement the models listing (Azure-style gateways, scoped keys, some
  proxies) returned 401/403 and the UI declared the key invalid forever. The
  probe now falls back to a minimal `POST /chat/completions` verification
  (that is the endpoint the product uses); only a chat rejection is reported
  as `invalid_api_key`, now including the provider's actual error message in
  `detail`. Results are cached 60s so the 15s UI poll never burns tokens.
- **An undecryptable stored key silently fell back to another key.** If the
  server's `ENCRYPTION_KEY` changed after a key was saved, the settings reader
  swallowed the decrypt failure, pinged with the env key (or none), and the UI
  still showed "Key set: ***" — the app appeared to reject a key it was never
  sending. The status endpoint now reports `stored_key_unreadable` with a
  re-enter-the-key hint, the settings page shows `api_key_error`, and re-saving
  the key recovers immediately.
- **Per-workflow overrides were global and unencrypted.** `ai_workflows` rows
  held plaintext API keys (contrary to the encrypted-at-rest design) in one
  process-global map — one user's override key hijacked every other user's AI
  calls, and `set_workflow_overrides` replaced the whole map per save, so
  other users' overrides silently vanished until restart. Overrides are now
  tenant-scoped (read fresh from the DB per call — also multi-worker safe) and
  encrypted at rest; legacy plaintext rows are migrated on startup.
- **Paste-artifact and masked-placeholder keys were accepted.** `base_url`,
  `model` and `api_key` are trimmed on save, and writing back a masked
  placeholder (`***`, `••••`) is rejected with a clear 400 instead of
  overwriting a working key with the literal mask (a guaranteed provider 401).
- A malformed settings cell (invalid JSON in one row) no longer 500s
  `GET /api/settings`; unreadable rows are skipped and reported.
- `GET /api/settings` and `/api/ops/status` reported `configured`/AI state
  from env-only config; both now reflect the caller's actual (per-user)
  configuration. `/api/health/ready`'s `ai_configured` also counts keys saved
  in the DB.
- Tests are hermetic again: an operator's checkout-level `.env` (created by
  `run.sh`, which ships `METRICS_PORT=9464`) no longer moves the metrics
  endpoint and fails three metrics tests locally.

### Added

- `backend/tests/test_ai_key_resolution.py` — end-to-end regression tests
  against a local fake OpenAI-compatible provider: ambient user-key resolution
  for pipeline calls, two-stage health probe (restricted `/models` + working
  chat), invalid-key reporting with provider detail, undecryptable-key
  surfacing and recovery, masked-placeholder rejection, key trimming, owner
  fallback attribution, and per-user override isolation with
  encryption-at-rest.
- `backend/scripts/fake_openai_provider.py` — a tiny OpenAI-compatible server
  (modes: `normal`, `restricted`, `invalid`) to verify any deployment's AI
  wiring offline; it logs the `Authorization` header it receives so you can
  confirm exactly which key the app sends.
- `GET /api/settings/ai/status` now returns `key_source` (user/owner/env/
  workflow_override), `key_preview` (safe masked form), `detail` (provider
  error message), `probe` and `stored_key_error`; the Settings page surfaces
  all of them in the status banner.

## [2.0.2] — 2026-09-11

Closes the two holes the frontend-contract PR could not reach: the input
queue's *consumption* half (stored answers never fed back into the autofill
plan) and the dedupe no-op that stranded jobs after a parked queue item.

### Fixed

- **User answers were stored but never used on the re-queued run.** The SPA
  and `POST /api/jobs/{id}/input` key answers by the form field `name`
  (e.g. `salary_expectation`), while `build_autofill_plan` looked
  profile-mapped fields up by their canonical `profile_key`
  (e.g. `salaryExpectation`). Every re-run therefore came back
  `needs_input` with the same blank fields — the job looped through the
  input queue forever, silently discarding the user's data each cycle.
  `build_autofill_plan` now accepts answers keyed by either the profile key
  (preferred) or the raw field name, so a submitted answer can no longer be
  orphaned.
- **Answer submissions could silently no-op.** `enqueue()` treats an active
  item with the same dedupe key as a duplicate and returns `None`. Once a
  run parked the application item in `needs_input`, every further answer
  submission was dropped and the job sat `queued` with nothing left to
  process it. `submit_input` now re-activates the parked item with the
  merged answers (resetting its attempt counter, so iterating on answers
  cannot dead-letter the item) and folds newest answers into a not-yet-
  claimed duplicate instead of stacking a second item.
- **The `requeued` flag lied.** It was hard-coded `true` even when no work
  item existed; it now reflects the actual queue state.

### Added

- `test_user_answers_survive_the_requeue_round_trip` — drives the full loop
  (apply → needs_input → answer → worker re-run via `handle_application`)
  and asserts the answer reaches the stored autofill plan and no fresh blank
  input request is created.
- `test_parked_item_is_requeued_with_fresh_answers` — asserts a
  `needs_input`-parked item is re-queued in place (no duplicate, attempts
  reset, fresh payload) and that the re-run completes.

## [2.0.1] — 2026-09-10

Launch-hardening pass: fixes for defects found while booting a fresh checkout
(`./run.sh`), provisioning test accounts and reviewing the production topology.

### Fixed

- **Startup crash with a generated `.env`** — every list-typed setting
  (`CORS_ORIGINS`, `ALLOWED_HOSTS`, `ENABLED_SOURCES`, board tokens …) was handed to
  `json.loads` by `pydantic-settings`, so an empty or comma-separated value aborted
  the process with `SettingsError: error parsing value for field "cors_origins"`.
  List settings are now parsed from the raw string (CSV or JSON) by the app itself.
- **`GET /api/jobs/sources` returned 422** — the route was registered after
  `/jobs/{job_id}`, so the literal segment was matched as an integer path parameter.
- **The SPA never offered registration** — the UI read `registration_open` while
  `/api/auth/status` returned `allow_registration`; both keys are now returned and
  the UI accepts either. The status payload also carries `version` and
  `password_min_length` (the password hint is no longer hard-coded to 10).
- **422 responses crashed their own handler** — pydantic puts the original exception
  in `ctx`, which is not JSON serialisable, so some validation errors returned 500.
  Error bodies are now sanitised and include a human-readable `message`.
- **Password policy mismatch** — the API accepted a 422 from pydantic's floor of 8
  characters before the configured `PASSWORD_MIN_LENGTH` was ever checked; sign-up
  now fails with the real policy and a readable message.
- **Data paths depended on the working directory** — a relative SQLite URL and the
  upload/generated/screenshot/backup directories now resolve next to the backend
  package, so the API, worker and CLI share one database instead of silently
  creating a new one per working directory.
- **Concurrent startup migrations** — the API container (`--workers N`) and the
  worker container both ran Alembic. Postgres migrations now take an advisory lock
  and the compose worker runs with `AUTO_MIGRATE=false`. A failed migration on a
  populated database no longer falls back to `create_all` (which forked the schema).
- **Unauthenticated webhook sink** — `POST /api/track/events` accepted bounce events
  from anyone when `EMAIL_WEBHOOK_TOKEN` was unset; it is now disabled in production
  unless a token is configured, and compares the token in constant time.
- **API key scopes were stored but never enforced** — a key created with
  `["read"]` could queue jobs and send mail. Writes now require the `write` scope
  (keys with no scopes keep working, so existing clients are unaffected).
- **Demo jobs appeared by default** — `INCLUDE_DEMO_POOL` was read with `os.getenv`
  (which ignores `.env`) and defaulted to *on* outside production. It now honours the
  documented setting and is off everywhere by default. Demo ids were also built with
  the salted builtin `hash()`, so every restart produced new dedupe keys and duplicate
  rows; they use a stable SHA-1 digest now.
- Misc: HSTS is sent when the deployment is production/https; unknown `/api/*` paths
  answer JSON 404 instead of the SPA's HTML; the backup CLI takes its defaults from
  Settings (so `BACKUP_DIR` in `.env` works) and the production image now ships
  `postgresql-client` so `pg_dump` backups are possible at all.

### Changed

- **Consent gates now live on the routes** — `require_consent()` was defined but
  never wired, so the "consents gate automation/outreach" claim was only enforced
  per call site. `POST /api/emails/{id}/send` now requires the `outreach`
  disclosure and `POST /api/jobs/{id}/apply` requires the `automation` disclosure
  on the branch that would really submit (both answer 403 `consent_required`,
  naming the disclosure). Dry-run preparation is unaffected, and
  `compliance_report()` still re-checks consent inside `send_email()`, which is
  what protects queued/worker sends that never touch the route.
  ⚠️ API contract change: `/send` without consent was `200 {sent: false, blocked:
  true}` and is now `403`.
- **`METRICS_TOKEN` is required in production** while `METRICS_ENABLED=true`
  (otherwise `/api/metrics` is anonymous *and* exempt from rate limiting). Set
  `METRICS_ENABLED=false` to opt out. The token is now compared in constant time
  and the header form is preferred over `?metrics_token=` (query strings leak
  into access logs).

### Added

- **Metrics on a dedicated internal port** (`METRICS_PORT`, default `9464`,
  `METRICS_HOST` defaulting to loopback). The app process serves `/metrics`
  there and `GET /api/metrics` answers `404 {"code":"metrics_moved"}`, so the
  exposition never has to be reachable from the internet. The listener lives in
  the API/worker process (the registry is in-process state, so a sidecar would
  report nothing); with several uvicorn workers the one that wins the bind
  serves it and the others log a warning and continue.
  `docker-compose.prod.yml` `expose:`s `9464` on the api and worker services
  (never `ports:`), and the image exposes it for `docker run -p` users.

- **`backend/scripts/create_user.py`** — create, reset, promote, deactivate and list
  accounts from the command line (the supported way to provision testers without
  opening registration). Documented in the README's "Accounts & test logins".
- **The SPA explains a refused send** — a blocked or consent-gated send now shows
  *why* (compliance blockers, or the exact missing disclosure plus a button that
  records it and retries) instead of silently leaving the email in `queued`.
- 40 regression tests pinning every defect above (`backend/tests/test_regressions.py`).

## [2.0.0] — 2026-09-11

Production-readiness pass: the v1.2 prototype becomes a deployable, multi-tenant
service. Every engineering blocker from the launch review is closed in code;
everything that depends on the outside world stays behind an explicit, audited
opt-in (dry-run by default).

### Added

- **Authentication & tenancy** — first-run owner bootstrap, JWT access tokens,
  rotating hashed refresh tokens, API keys, role checks, consent records with
  timestamps, per-user scoping on every table and per-user vault key derivation.
- **Real job discovery** — 13 licence-friendly source adapters (Greenhouse,
  Lever, Ashby, Workable, SmartRecruiters, Workday, Remotive, Arbeitnow, Jobicy,
  RemoteOK, Himalayas, The Muse, WeWorkRemotely) with normalisation, dedupe,
  freshness filtering, robots.txt compliance, per-host politeness and caching.
  Credentialed/ToS-gated boards report why they are unavailable instead of
  failing silently.
- **Application automation** — real HTML form detection with ATS fingerprints,
  field mapping from profile/vault/answers, needs-input queue, and Playwright
  autofill behind an optional extra (`requirements-autofill.txt`), dry-run by
  default.
- **Outreach engine** — drafted/approved email bucket, compliance gate enforced
  server-side (suppression list, consents, daily cap, postal address,
  unsubscribe), SMTP with OTP/app-password handling, open pixel, one-click
  unsubscribe and provider webhooks for bounce/complaint suppression.
- **Funding & contacts** — SEC EDGAR provider, optional Crunchbase/Tracxn,
  CSV import; Hunter/Apollo adapters with DNS MX verification; heuristic
  contacts are always labelled unverified.
- **Operations** — Alembic migrations, PostgreSQL support with pooling, durable
  queue with leases/retries/dead letters and a standalone worker,
  `/api/health/{live,ready}`, Prometheus metrics, structured JSON logs with
  request ids, backup script with integrity checks, Docker/compose (dev + prod)
  and a full CI pipeline.
- **Security** — outbound SSRF guard on every fetch (schemes, URL credentials,
  metadata/private/link-local addresses, internal ports, redirect hops), upload
  name sanitisation with path-containment assertions, production configuration
  that refuses to boot on unsafe values.
- **Documents** — `docs/DEPLOYMENT.md`, `docs/SECURITY.md`, `docs/COMPLIANCE.md`,
  and a rewritten `docs/LAUNCH_READINESS.md` assessment.

### Changed

- Dependency stack upgraded and audited: `pip-audit` reports no known
  vulnerabilities (up from 137 across 9 packages) and `npm audit` is clean.
- `python-jose` replaced with `PyJWT` (drops the unmaintained `ecdsa`
  dependency); the frontend moved to `vite@7` and `react-router-dom@7`.
- Failed sends now report a structured status (`rejected` / `dry_run` /
  `needs_otp` / `failed`) instead of a boolean.

### Fixed

- Refresh-token rotation only ever returns a token to its original owner.
- Resume uploads reject content that does not match their extension.
- Test suite modernised for pytest 9 / pytest-asyncio 1.x (no deprecated
  `get_event_loop()` usage).

## [1.2.0]

Single-user prototype: resume upload and parsing, keyword-driven discovery,
resume generation, credential vault, funding radar, cold-email drafting and
application tracking — all local, no authentication, no production hardening.

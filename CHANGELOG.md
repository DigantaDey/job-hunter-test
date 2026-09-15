# Changelog

All notable changes to JobHunter AI are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [2.2.14] — 2026-09-15

**Long-lived process state stops being attacker-resettable, the daily cap stops
moving with the server's timezone, and a broken vault key now fails loudly.**
Five independent defects, all of which degraded quietly: the pre-auth login
window still had a hardcoded 8/300s threshold and an unbounded derived-key
cache next to it; the outreach daily cap measured "today" with `date.today()`,
so it drifted with `TZ` and across DST and disagreed between workers; the MX
probe ran the blocking resolver *on* the event loop, freezing the worker for up
to five seconds per contact; the text log formatter interpolated the
caller-supplied `x-request-id` header raw, so a newline in it forged log
records; and the vault's opportunistic re-key was never committed — while a
ciphertext that no longer decrypted was swallowed into an empty password.

### Fixed

- **A login spray can no longer reset other users' lockouts, and the threshold
  is configuration.** The window is a bounded LRU (`LOGIN_THROTTLE_MAX_KEYS`)
  whose *full* windows are pinned via `evictable`, so a flood of one-attempt
  addresses costs its own entries at any spray size instead of pushing an active
  lockout out; partial windows stay evictable, pinned ones still expire on their
  TTL, and any overflow is reported as `over_capacity`. `MAX_LOGIN_ATTEMPTS` and
  `LOGIN_THROTTLE_WINDOW_SECONDS` are read per call rather than frozen at
  import, so an install being sprayed can tighten them without a code change,
  and the live values are reported on `GET /api/ops/status` under
  `edge.login_throttle`.
- **`security._key_cache` is bounded and expires.** It is keyed on the vault
  scope, which embeds the user id, so the plain dict grew for the whole process
  lifetime. It is now a `BoundedTTLMap` (`KEY_CACHE_MAX_ENTRIES`,
  `KEY_CACHE_TTL_SECONDS`), surfaced as `key_cache` on `GET /api/ops/status`,
  with `clear_key_cache()` for an explicit post-rotation flush — the TTL is what
  stops a rotated `VAULT_KEY` from being shadowed by a stale derivation forever.
- **The daily outreach cap is a UTC timestamp range.** `sent_today()` now
  applies `sent_at >= start AND sent_at < end` over the UTC calendar day
  (`utc_day_bounds`), matching the naive-UTC column `send_email` writes. The old
  `date.today()` midnight moved with the process timezone and jumped on a DST
  change, so the same 24 hours could be charged to two different days — granting
  a second full budget early, or blocking sends long after the budget had rolled
  over. `/api/account/billing-usage` reads the same helper, so "today" means one
  thing everywhere.
- **The MX probe no longer blocks the event loop.** `dns.resolver.resolve` runs
  on a worker thread (`resolve_mx`), bounded by `CONTACT_MX_TIMEOUT_SECONDS` and
  a `CONTACT_MX_MAX_CONCURRENT` limiter, with the thread *abandoned* rather than
  awaited when the budget elapses. Async callers use `verify_email_async()`;
  the sync `verify_email()` still probes MX from a script but declines inside a
  running loop, reporting `mx: "skipped_event_loop"` instead of freezing every
  other task on the worker. An unfinished lookup is still `mx_ok: null` —
  unknown, never invalid.
- **A forged `x-request-id` can no longer mint log records.** The header is
  reduced to a safe character set at the edge (`sanitize_request_id`) before it
  is echoed, logged or stored on an audit row — a raw CR/LF there was also a
  response-splitting primitive — and `sanitize_log_value` escapes C0/C1
  controls and the Unicode line terminators on every value the text formatter
  interpolates, escaping rather than deleting so an attack stays visible in the
  log. The traceback block is indented so a newline inside an exception message
  cannot start a line at column 0. JSON mode was already safe.
- **The vault re-key is committed, and an undecryptable entry is an error.**
  `reveal_password()` now persists its opportunistic re-encryption itself
  (best-effort, logged either way), so an entry upgraded off the legacy global
  key stays upgraded — previously the mutation died with the request and the
  entry became unreadable the moment `VAULT_KEY` was rotated. A ciphertext that
  matches no key raises `VaultDecryptionError` and logs at ERROR instead of
  returning `""`: `GET /api/vault/{id}/reveal` answers `500` with
  `code: "vault_entry_undecryptable"` plus a `vault.reveal_failed` audit row;
  both CSV exports *omit* the entry (never a blank password a browser importer
  would store) and report the count in `X-Vault-Undecryptable` and on the audit
  row; the GDPR export emits `"password": null` with
  `"password_error": "undecryptable"`.

## [2.2.13] — 2026-09-15

**The funding radar's company name stops being a URL, and one scan costs one
unit everywhere.** Two defects: `POST /api/funding/{company_name}/process` put a
raw, user-controlled name in the *path* — unicode, spaces, quotes and slashes
all needed encoding, a trailing slash was a different route, and the lookup it
fed was keyed on a name whose storage uniqueness (`UNIQUE(user_id, name)`)
disagreed with the normalised key every dedupe path already used, so "Acme Inc."
and "acme inc." were two rows the code believed were one. And the same funding
scan was billed differently depending on which button ran it: the synchronous
radar charged `len(companies)` per scan, while the queued path charged at
*trigger* time and never refunded a job that then failed.

### Fixed

- **The company travels in the body.** `POST /api/funding/companies/process`
  takes `{"company_id": …}` or `{"company": "<name>"}`; no character is special,
  no encoding is required, and the route cannot fork on a slash. The SPA sends
  the row's stable id plus its name.
- **`funding_companies.name_normalized` is the stored identity.** New column
  (strip + collapse whitespace + casefold — the app's own
  `normalize_company_name`), kept in sync by an ORM event so no caller can
  forget it, with `UNIQUE(user_id, name_normalized)` replacing
  `uq_funding_user_name`. Migration `f6a7b8c9d0e1` backfills it, collapses
  existing duplicates keeping the **oldest** row (the one carrying the first-seen
  history) and re-points `funding_scan_companies` memberships at the survivor.
  All dedupe and lookup — sync, history linkage, process — read the column.
- **One charge point for a funding scan.** `funding_radar.charge_funding_scan()`
  spends exactly one `funding_companies_per_month` for a scan whose
  `scan_status` is `ok`, and nothing for a paused/blocked/failed one. Both the
  interactive radar and the queued handler call it; `POST /funding/refresh` now
  only *checks* the quota it used to spend. A failed queued scan consumes
  nothing; a successful scan costs the same one unit in either mode.

## [2.2.12] — 2026-09-15

**The request edge is now bounded and the client IP is no longer caller-chosen.**
Five defects, all in the code that runs *before* authentication — where the only
inputs are attacker-controlled: the rate limiter kept a `dict` keyed on the raw
tail of the `Authorization` / `X-API-Key` header and pruned an entry only when
the *same* key came back, so unique random credentials grew it until the API
process was OOM-killed (no valid token needed — pruning happened before auth);
the body-size middleware trusted `Content-Length`, so a `Transfer-Encoding:
chunked` request streamed past it uncapped; `client_ip()` took the **leftmost**
`X-Forwarded-For` entry with no trust boundary *and* the shipped image started
uvicorn with `--forwarded-allow-ips='*'`, so any direct client could pick the IP
that per-IP rate limits, login-failure attribution and `audit_logs.ip` are keyed
on (lock out a victim, hide your trail); `jobhunter_http_requests_total` was
labelled with a host and the registry never evicted anything, so every unique
value created a permanent series; and `WEB_CONCURRENCY` multiplied every
in-process budget by the worker count without saying so.

### Fixed

- **The rate limiter cannot be grown.** Its hit log is an LRU with a hard cap
  (`RATE_LIMIT_MAX_KEYS`, 8192) plus a periodic sweep that reclaims idle windows
  without waiting for the same key to return — eviction is O(1), because an O(n)
  scan for expired entries is per-request work under exactly the flood it exists
  to survive. Keys are HMAC fingerprints (never a slice of a raw header, which
  leaked credential material into a heap-resident dict): a **verified** access
  token → `u:<hmac(user id)>` (one budget per user, wherever they connect from),
  a `jh_` API key → `k:<hmac(key)>` *and* the client-IP bucket (the middleware
  will not spend a DB query per request to verify it, so the credential is
  unproven and the IP budget is what stops a junk-key flood), and anything else —
  no credential, forged, expired, random bytes → `ip:<resolved client IP>`.
  10 000 requests with 10 000 unique `Authorization` values are now **one** IP
  bucket, not 10 000 entries. Counters (keys/evictions/denials) are on
  `GET /api/ops/status` under `edge.rate_limiter`.
- **The body cap is enforced on the wire.** `BodySizeLimitMiddleware` is now pure
  ASGI and wraps the receive channel: `Content-Length` still fast-rejects before
  the app runs, *and* bytes are counted as they arrive, so the first chunk that
  crosses the cap answers `413 {"code":"payload_too_large"}` and unwinds the
  request. The 413 is written on the outer send channel and every later response
  from the downstream app is dropped, so a handler that swallows the unwind (or
  had already started answering) cannot produce a second response. Chunked
  uploads are now capped exactly like sized ones.
- **`X-Forwarded-For` is believed only from a configured proxy.**
  `resolve_client_ip()` walks the header from the **right** and returns the first
  hop that is not itself in `TRUSTED_PROXIES`, and only when the connecting peer
  is one — otherwise the peer address wins and the header is ignored, counted
  (`jobhunter_edge_forwarded_for_total{reason=…}`) and warned about once a minute.
  The Dockerfile now starts uvicorn with
  `--forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"` instead of `'*'`,
  `docker-compose.prod.yml` passes both settings through, and production
  **refuses to start** on `*`, `0.0.0.0/0` or an unparsable entry in either. With
  no proxy configured the peer (`request.client.host`) is the client, which is
  correct for `docker run -p` and fail-closed (never spoofable) behind one.
  `deps.client_ip()` and the audit trail both use the resolver, so
  `audit_logs.ip` records the real actor. Topologies are documented in
  `docs/DEPLOYMENT.md` §3.1.
- **Metric cardinality is bounded by construction.** Inbound HTTP metrics are
  labelled `method` + **route template** + `status` and nothing else — the
  template is now read *after* routing (the middleware used to read
  `scope["route"]` before the router had set it, so every request was labelled
  with a collapsed raw path) and restored to its full path (`/api/jobs/{job_id}`);
  unmatched URLs pass a 64-entry admission list and then count as `other`. The
  outbound client's `host` label collapses to the organisation-level domain of
  endpoints the deployment intends to call (`SOURCE_API_DOMAINS`,
  `OUTBOUND_ALLOWED_HOSTS`, the AI/funding endpoints) with every host pulled out
  of a job feed counted as `other` — per-source detail already lives on
  `jobhunter_source_fetch_total{source=…}`. And the registry itself caps each
  metric at `METRICS_MAX_SERIES_PER_METRIC` (512) series, evicting the least
  recently updated one beyond that, so a future mistake costs observability
  (reported as `edge.registry.evicted`) rather than memory.
- **One worker is the shipped default, and it says why.** `WEB_CONCURRENCY`
  defaults to 1 in the image and is pinned to 1 in `docker-compose.prod.yml`;
  raising it now logs a startup warning naming what it multiplies (rate limits,
  AI token buckets and daily budgets, per-user AI overrides — which otherwise
  reach only the worker that served the save — login throttling, the metrics
  registry, and `RUN_WORKER_IN_API`'s pipeline copies). `docs/DEPLOYMENT.md` §6
  documents the scale-out path (more worker containers, more single-worker API
  replicas) and the shared-limiter change a cross-replica limit actually needs.
- **Login throttling is a bounded LRU.** The per-email attempt window was a plain
  dict with `if len(...) > 2000: clear()` — random addresses grew it, and
  crossing the threshold wiped *everybody's* lockout state. It is now a
  `BoundedTTLMap` (4096 entries, 300s TTL): one noisy address costs its own
  entry, never another account's throttle.

### Added

- `GET /api/ops/status` → `edge`: the rate limiter's key log, the body cap's
  reject counters, the metrics registry's per-metric series/evictions, the
  effective client-IP trust boundary and the worker count.
- Settings: `TRUSTED_PROXIES`, `FORWARDED_ALLOW_IPS`, `WEB_CONCURRENCY`,
  `RATE_LIMIT_MAX_KEYS`, `METRICS_MAX_SERIES_PER_METRIC` (all documented in
  `.env.example`); `jobhunter_edge_forwarded_for_total` and
  `jobhunter_http_payload_too_large_total` metrics.

---

## [2.2.11] — 2026-09-15

**The autofill browser is now behind the SSRF guard, the worker's caches are
bounded, and autofill failures say what they tried.** Three defects in the
long-lived worker: `page.goto(job.url)` navigated to whatever URL a feed or an
import supplied — never checked against `net_guard`, so the headless browser
would load `169.254.169.254` (cloud credentials), `127.0.0.1:<port>` (anything
else in the container) or any RFC1918 host, and `_attempt_login` then typed the
user's vault password into whatever password-looking field that host rendered;
`http._cache` stored whole `httpx.Response` objects keyed by GET-url and evicted
an entry only when the *same* URL was seen again, so a discovery/funding/
company-intel sweep grew it (and the per-host politeness maps, and the DNS
verdict cache) for the whole process lifetime; and a failed autofill reported
"0 fields filled" with no record of which selector matched.

### Fixed

- **Browser navigation is policy-checked before it happens.**
  `net_guard`'s deny-first policy is now a reusable pre-flight
  (`preflight` → `URLVerdict`, with `check_url` / `check_navigation_url` as
  thin raising wrappers, so the rules cannot drift). `execute_autofill` runs
  `preflight_navigation` before Playwright is even launched, in a stricter
  shape: loopback, link-local (`169.254.0.0/16`) and metadata hostnames are
  refused **even when allow-listed**, and `OUTBOUND_ALLOW_PRIVATE` does not
  reach navigation (a private range is navigable only when that exact host is
  in `OUTBOUND_ALLOWED_HOSTS`). A refused URL is recorded on the job as
  `status="blocked"` with the reason — never as an application.
- **Autofill is restricted to the job's own domains.** The domains a run may
  act on come from the job's metadata — its ATS portal domain
  (`form_detector.VAULT_DOMAINS`) or the company website on record
  (`job.company_info.website`, now populated for funding-radar jobs) — and
  never from the posting URL, which is the attacker-supplied value. A posting
  on a host that metadata does not name is refused; a login-requiring form on
  a foreign host is refused before navigation; a redirect off the posting's
  own organisation stops the run.
- **Vault credentials only go to the domain they were issued for.** Injection
  now requires the page's *live* host to be the application domain **and** the
  credential's own vault domain (`login.reason` reports which check failed).
  The same gate covers `password` fields in the form plan — the fill loop was
  a second credential path — so no password is typed on an unverified host.
- **The worker's caches are bounded LRUs** (`app/core/lru.py`): the GET cache
  holds `HTTP_CACHE_MAX_ENTRIES` (128) detached summaries — status + headers +
  parsed body, never the raw `httpx.Response` that pinned its request, stream
  and cookies — and skips bodies over `HTTP_CACHE_MAX_BODY_BYTES`; per-host
  politeness state is capped at `HTTP_HOST_STATE_MAX_ENTRIES` with an
  in-use lock pinned rather than evicted (the politeness guarantee cannot be
  lost to make room); the SSRF guard's DNS verdicts are capped at
  `DNS_CACHE_MAX_ENTRIES`. Entries/evictions/hits are on `GET /api/ops/status`
  under `outbound`. Politeness, `Retry-After` handling and backoff are
  unchanged.
- **Autofill failures are diagnosable.** Every step logs at `DEBUG` which
  selector matched (or that none did, with the selectors tried) and the result
  carries a capped `diagnostics` list plus a `navigation` block (host, landed
  host, domain policy). Field names and selectors only — values, and passwords
  in particular, are never logged (pinned by a test that fails on any leak).

### Tests

- New `tests/test_autofill_navigation_policy.py` (46 tests): `goto` is refused
  for metadata/loopback/link-local/private/internal-port/non-HTTP job URLs with
  the browser never launched; `OUTBOUND_ALLOW_PRIVATE` does not relax the
  browser while an allow-listed private host still works; postings outside the
  company domain, login forms on foreign hosts and off-organisation redirects
  are refused; credentials are injected on the application domain and withheld
  on a lookalike host, an unverified custom portal, or a credential issued for
  another domain; `credential_decision` is pinned as a pure deny-first rule;
  diagnostics/debug logs name the matched selector and never a secret.
- New `tests/test_http_cache_bounds.py` (24 tests): the response cache cannot
  exceed its bound under 4× its cap in distinct URLs and stores no
  `httpx.Response`; hits are served without touching the wire, expire on their
  TTL, and oversized/non-200 bodies are not cached; host state is bounded while
  a held lock survives the sweep; politeness spacing, per-host serialisation,
  `Retry-After` and exhausted-retry behaviour are unchanged; the DNS cache is
  bounded and still caches positive *and* negative verdicts.
- Extended `tests/test_net_guard.py` (pre-flight verdicts, the
  HTTP-vs-navigation asymmetry, hard blocks surviving the allow-list, host /
  registrable-domain matching, DNS cache bound) and
  `tests/test_ops_health_and_privacy.py` (the `outbound` block).
- Full suite green: 628 tests.

## [2.2.10] — 2026-09-15

**Storage caps are real, company-intel bills the work it does, and the hot
endpoints stop loading whole boards.** Four defects in the monetization and
query layers: `resumes_max`/`vault_entries_max` were enforced against
`UsageCounter` rows that nothing ever wrote (and `jobs_max` was not enforced
at all), so free-tier users stored unlimited rows; the company-intel
endpoint charged the free cache-hit path and gave the paid AI run away;
`GET /api/user-input-queue` loaded the user's *entire* job board into a dict
on every poll; and `GET /api/jobs?company=` fetched every row matching the
other filters and filtered in Python, applying LIMIT/OFFSET afterwards. The
list payload also set `has_open_positions` to the `is_funded` flag — a
semantic lie the Jobs UI rendered as an "open roles" chip.

### Fixed

- **Storage caps enforced by actual row count.** `resumes_max`,
  `vault_entries_max` and `jobs_max` are now caps on the rows the user
  actually holds (`entitlements.STORAGE_LIMITS` + `check_storage`),
  enforced at every creation path: resume upload, vault entry create,
  discovery (HTTP trigger 429s on a full board; the worker clamps a
  nearly-full board to its remaining slots and reports
  `why_empty: "jobs_cap_reached"` on a full one — a no-op by design, never
  an over-limit insert), and the funding-radar tracked-job. Over-limit
  answers use the standard `limit_exceeded` 429 shape with the upgrade
  hint, and deleting a row frees a slot immediately because the rows *are*
  the counter. `entitlements_snapshot` now reports the real
  used/limit/remaining for these caps — the usage grid no longer shows a
  permanent "0 used".
- **Company-intel billing follows the work.** `GET /api/company/{name}/intel`
  charges `company_intel_per_month` (and writes the AI ledger) only when the
  model actually builds or refreshes the intel — cache miss, stale cache, or
  explicit `?refresh=true`. A fresh (<30d) cache read is free: it answers
  from the stored row and leaves the counter untouched, including for a user
  already at their monthly limit (the paywall is on the paid work, not on
  the read). The shared fresh-cache predicate (`get_cached_intel`) is the
  single definition of a cache hit for both the billing decision and the AI
  call.
- **`GET /api/user-input-queue` joins instead of loading the board.** The
  pending `UserInputRequest` rows are enriched with title/company/url via a
  single `LEFT JOIN` to `jobs` — the user's whole board (20k rows on Pro+)
  is never fetched into a dict on every poll. Pending rows whose job was
  deleted still render, with empty job fields, exactly as before.
- **`GET /api/jobs?company=` filters in SQL.** New
  `jobs.company_name_normalized` column (migration `e5f6a7b8c9d0`, backfilled
  with the app's own suffix-stripping normalizer, composite
  `(user_id, company_name_normalized)` index) stores the normalized identity
  at create/import time (discovery + funding-radar), so the exact-normalized
  company match runs in SQL with LIMIT/OFFSET applied by the database.
  Display name (`jobs.company`) is unchanged.
- **The `has_open_positions` lie is gone.** A Job row *is* an open position,
  so the field carried no honest meaning — it was the funding flag wearing a
  different label. The field is removed from the list payload and the Jobs
  UI; the funding chip remains, labelled as funding. The real
  `has_open_positions` signal lives on the funding radar's companies.
- **Migration chain runs clean on PostgreSQL.** The initial migration
  created `jobs` with a foreign key to `resumes` before `resumes` existed —
  invisible on SQLite (FKs are not validated at CREATE time), fatal on
  PostgreSQL. Tables now create in FK-safe order, the jobs ↔ resumes cycle
  is closed with a named FK added after both tables exist (batch-mode table
  rebuild on SQLite), and the downgrade breaks the cycle and drops in
  FK-safe order. `tests/conftest.py` resets the schema per dialect (PRAGMA
  on SQLite, `DROP TABLE ... CASCADE` on PostgreSQL).

### Tests

- New suite (`tests/test_storage_caps_and_company_billing.py`): free users
  hitting the resume (10), vault (20) and job (500) caps are blocked with
  the standard 429 while one slot left still works; discovery clamps a
  nearly-full board to exactly its remaining slots and a queued run on a
  full board reports `jobs_cap_reached` without inserting; company-intel
  build charges once, cache hit charges nothing, refresh charges once, and
  a user at quota can still read the fresh cache; SQL-echo assertions prove
  the input queue joins (no standalone board scan) and the company filter
  runs in SQL with LIMIT; the snapshot reports real used/limit; the
  migration backfills a populated previous-head database and is reversible.
- Full suite green on **both** SQLite and PostgreSQL (551 tests each),
  migrations verified fresh + populated + downgrade on both dialects.

## [2.2.9] — 2026-09-15

**Auto-apply's Execute phase is alive, and discovery no longer burns a batch
over one bad row.** The consent/execute pipeline had three silent breaks: no
caller ever passed `allow_submit=True`, so even a fully consented,
queue-approved run always finished as a dry run; re-queuing after a
"needs input" answer demoted the run to `prepare`; and the discovery batch
rolled back *entirely* when one insert conflicted — after the per-job AI
scoring had already been paid for. All three are fixed, with e2e coverage
(stub AI, mocked Playwright).

### Fixed

- **Execute intent is threaded end-to-end.** `POST /jobs/{id}/apply` already
  queued `mode: "execute"` when the user has auto-submit on and accepted the
  automation disclosure; now `handle_application` honours it — it passes
  `allow_submit=True` into `execute_application` for every execute-mode item
  (the handler's absent-mode default, which the auto scheduler relies on, is
  execute). The consent chain is what actually gates submission: the live
  per-user `allow_auto_submit` setting **and** `AUTOFILL_ALLOW_SUBMIT` **and**
  a non-dry-run deployment must all hold at execution time, so an intent
  queued before a consent was revoked (or a flag flipped) still runs — as a
  dry run, never a submit. With the chain green, a confirmed application now
  fills the form and submits it (`status: applied`), instead of always
  parking at `ready_to_apply`.
- **Re-queue preserves the mode.** `_requeue_application` (the
  "answer the input queue" path) no longer re-enqueues as `prepare`: the
  re-queued item keeps its original `mode` (`execute` stays `execute`,
  explicitly — absent mode means execute, the handler's default). When the
  live row is gone, the intent is recovered from the job's most recent
  application item; a job with no history defaults to `prepare`. An
  execute-mode run that paused for a missing field now completes as execute
  after the answer.
- **Discovery: one bad row no longer discards the batch.** Inserts commit
  per row (skip-and-continue): a uniqueness conflict on one candidate is
  recorded in the run report's `insert_errors` (title/company/dedupe_key/
  reason) and the rest of the batch persists — the AI spend for the batch is
  kept. The run's log line reports `insert_errors=N` when it happens.
- **Discovery: dropped a dead sort.** The candidate list was sorted by
  `score` before scoring ever ran — a constant-key no-op (the following
  newest-first sort then reordered everything again). Only the real ordering
  (newest first) remains.
- **Removed the dead `application.profile_snapshot` settings key.** The
  outreach paths read it as a "fallback" profile that was never writable and
  therefore always empty, then fell back to the real `Profile` table anyway.
  They now read the `Profile` table directly; the settings surface is
  unchanged (the key was never in the writable schema).

### Tests

- New end-to-end suite (`tests/test_autofill_execute_pipeline.py`, stub AI +
  mocked Playwright): a consented execute run with `AUTOFILL_ALLOW_SUBMIT`
  reaches `execute_autofill` with `dry_run=False` and submits (job →
  `applied`); without live consent the same queued intent stays a dry run
  (job → `ready_to_apply`, no submit click); an execute job re-queued after
  an input answer resumes as execute and submits; a discovery batch with one
  failing insert still persists the good jobs and reports the failure.

## [2.2.8] — 2026-09-14

**Every AI trigger is now page-agnostic and live.** Starting Auto-apply on a job,
navigating to Funding Radar and coming back — or pressing F5 — used to lose the
"Status: preparing • resume: master" line, because it lived in the page's React
state and the trigger endpoints ran the work inside the request. Now every
trigger only *enqueues* a durable `pipeline_jobs` row and returns a receipt; the
pages derive what they show from that row, polled once for the whole app.

### Added

- **Queue-only triggers.** `POST /jobs/{id}/apply`, `POST /jobs/discover`,
  `POST /resumes/generate`, `POST /emails/generate` and `POST /funding/refresh`
  all answer `{queued: true, pipeline_job_id, duplicate, queue_status, …}` and
  never run the AI in the request. A second click while a row is live returns
  the *existing* row's id (`duplicate: true`) instead of a second run. Tenant
  scope is unchanged — every row is read and written under the caller's tenant.
- **`GET /api/queues/ai`** — one tenant-scoped read for the live layer:
  `counts {queued, processing, paused, needs_input}`, the in-flight `items`,
  the last 30 minutes of outcomes (`recent`, each with the handler's `result`),
  and `processing_now`. `GET /api/pipelines/jobs?status=a,b` accepts a
  comma-separated status list; `GET /api/pipelines/jobs/{id}` returns one row;
  `GET /api/emails/{id}` returns one bucket card.
- **Live layer in the frontend.** `hooks/useAIQueue.ts` polls the read every
  4 s, pauses on `visibilitychange`, coalesces overlapping refreshes and keeps
  the last snapshot when a read fails. `context/AIWorkContext.tsx` is mounted
  in `Layout` and shares one poll with every page; `lib/aiWork.ts` derives
  Queued → Preparing → Applying → Applied / Prepared / Needs input (and the
  resume, discovery, email and funding equivalents) from a row.
- **Header chip on every route** ("N queued • M processing • K paused — AI
  paused, will resume") with a dropdown of the in-flight rows and links to
  Queues and Queues → User Input Needed (`/queues#user-input`).
- **Re-attach after F5.** Trigger receipts are stored in `sessionStorage`
  (`jh.aiwork.tracked`, 6 h TTL); on reload the pages find the row by id and
  keep following it — and, failing that, the newest row for the same job.
- Jobs, Funding, Resumes, Emails, Dashboard and Queues subscribe to the context:
  a status line under each trigger button, the trigger disabled while its row
  is live, the Dashboard's "Running / Open queues" counters and an "AI work in
  flight" list from the live snapshot, and Queues with a "Processing now" card
  plus a visibility-aware 3 s poll. The Resumes preview (including a fact-guard
  rejection) is derived from the row's `result`.
- Tests: `backend/tests/test_live_ai_queue.py` (tenant scope, processing_now,
  comma statuses, needs_input fields, duplicate ids, prepare-mode without a
  browser), `frontend/src/lib/__tests__/aiWork.test.ts` and
  `frontend/src/hooks/__tests__/useAIQueue.test.tsx` (interval, hidden-tab
  pause, stale, coalescing).

### Fixed

- A `needs_input` application row lost the handler's prepared plan: the
  missing fields, the resume decision and the input-request id were dropped
  when the row was parked. They are kept in `result`, so the page can name the
  fields and link straight to the User Input Needed queue.
- Discover and Funding re-scan returned no `pipeline_job_id` on the duplicate
  path, so a second click could not attach to the run already in progress.
- `GET /api/emails/{id}` was shadowed by earlier literal routes.
- Guardrail and fact-guard rejections of a generated resume are recorded as a
  `done` row with `result.status == "rejected"`, not a queue failure that would
  be retried.
- Jobs → resume choice radios were hidden with `display:none` and could not be
  reached by keyboard; they are now `sr-only` with a visible focus ring.
- Focus/target-size fixes inline: logout button, queue filter chips, job list
  rows, Funding focus input, Resumes job select, Queues input forms — all with
  `focus:ring` and `min-h-[44px]`; no hover-only controls remain on these pages.

## [2.2.7] — 2026-09-14

**AI scoring: "unlimited" is now a real setting, and it is the default.** A Pro+
user with 222,757 of 2,000,000 credits left — nowhere near any quota — got
"Score 57 preliminary + AI pending" on a long job description. The AI never
answered: every outgoing `max_tokens` was clamped to a hard-coded 16000 ceiling
that neither the operator nor the user had chosen, the automatic escalation on
truncation was pinned at that same value, and the honest outcome was
`truncated_response`. The preliminary keyword score beside an "AI pending" badge
is a *fallback*, not a verdict, and a paying Pro+ account should never be served
one because of our cap. `AI_MAX_OUTPUT_TOKENS` / `AI_MAX_INPUT_TOKENS` and the
per-user `ai.max_output_tokens` / `ai.max_input_tokens` settings now take **`0` =
unlimited** (no clamp — the provider's own per-model maximum), and `0` is the
shipped default. Pro+ gets unlimited AI credits and an unclamped output budget;
free/pro keep their caps.

### Added

- **`0` = unlimited token budgets.** `AI_MAX_OUTPUT_TOKENS=0` /
  `AI_MAX_INPUT_TOKENS=0` (both new defaults) mean "do not clamp": the gateway
  sends each task's own starting budget, truncates no prompt part, and lets the
  escalation on truncation grow to the provider's maximum — bounded only by a
  new 128,000-token safety stop so a runaway retry loop can never bill a
  multi-million-token request. A positive value still clamps every outgoing
  `max_tokens` *and* the escalation, exactly as before.
- **`ai_max_output_tokens` plan limit** — the per-request output ceiling is now
  a real entitlement (`free`/`pro` 16000, `pro_plus` 0 = unlimited) and is
  applied in `resolve_config_for_user` as a tenant-level cap: it can only
  tighten the resolved budget, so "free still truncates" stays true now that the
  *default* is unlimited. The operator's own account is exempt — their
  setting/env value is the platform default, and clamping it to the free plan
  would put the operator straight back on this bug.
- **Pro+ is unlimited on AI credits** (`ai_credits_per_month: 0`). `is_unlimited()`
  is the single predicate behind it (`0`/`None` → unlimited, booleans never),
  used by `limit_for`, `check_limit` and `entitlements_snapshot`, so a plan can
  never disagree with itself about whether a limit exists.
- **Unlimited surfaces in the API and the UI.** `/api/meta` and
  `public_settings()` publish `ai_max_output_tokens` / `ai_max_input_tokens`
  plus `ai_output_unlimited` / `ai_input_unlimited`; `GET /api/settings` adds
  `max_output_tokens_unlimited`, `max_input_tokens_unlimited` and the platform
  defaults. Settings → AI API accepts `0` in both fields, badges them
  **Unlimited**, explains "0 = provider default (unlimited)", and round-trips
  `0` instead of snapping back to a ceiling. The plan banner, Billing usage grid
  and Dashboard read `limit 0` as *Unlimited* rather than "222757/0 credits" or
  a meter drawn against a fake ceiling.
- **Ledger provenance** — a successful call now records `output_unlimited`,
  `escalation_ceiling` and `input_unlimited` beside `max_tokens` /
  `output_ceiling`, so "was this call clamped?" is answerable from the record.
- **Production config validation** — a positive `AI_MAX_OUTPUT_TOKENS` under 64
  (or `AI_MAX_INPUT_TOKENS` under 256) fails the deploy: that is not a cost cap,
  it is a guarantee that every answer truncates. `0` is always valid.
- **`src/lib/credits.ts`** — the single reading of "this plan has no ceiling"
  for every credit readout (`isUnlimited`, `entryIsUnlimited`, `creditLabel`,
  `usagePercent`). The sidebar banner (`PlanCreditBanner`) and the billing
  usage tile (`UsageCell`) are now exported components built on it, so the
  sidebar, the billing grid and the dashboard cannot disagree — three of them
  used to make the decision inline, one of them truthiness-based. A limit the
  payload did not state is *not* unlimited (claiming so from a hole in the data
  is as false as claiming "0 left"), and a locked feature flag
  (`{allowed: false, limit: 0}`) can never earn the badge.
- **Component tests** — `UnlimitedCredits.test.tsx` renders the real banner and
  the real usage tile with the payload from the bug report and asserts the
  rendering, not a copy of the logic: Pro+ shows `222757 credits used •
  Unlimited` and never `222757/0`, a capped plan still shows `20000/50000
  credits` + `30000 left` + a 40% meter, and an unlimited tile draws no meter.
  Mutation-checked: reverting `isUnlimited` to the original truthiness test
  fails 6 of the 12, dropping the feature-flag guard fails 2, and "everything
  is unlimited" fails 5.
- **Form round-trip test** — `SettingsTokenBudgets.test.tsx` pins `budgetValue`,
  the function that used to be `Number(x) || 16000` and read an explicit
  *Unlimited* as "unset": `0` survives both directions, a real ceiling passes
  through, and only a genuinely empty field falls back to the platform default
  (so a stray backspace cannot silently switch an account to an unmetered
  provider budget). Restoring the old one-liner fails 3 of the 5.
- **Hermetic coverage** — `0` round-trips through `PUT/GET /api/settings` and
  `/api/meta`; a negative value is stored as unlimited, never a 1-token cap; an
  explicit `0` beats a default after it; the plan ceiling resolves 0/16000/16000
  for Pro+/pro/free; an unlimited ceiling lets the escalation grow
  4000 → 16000 → 32000 on the wire; an unlimited input budget sends a 6k-char
  resume whole with `input_truncated: false`; the reported bug end to end
  (Pro+ + a 44k-char JD + a model that thinks three times →
  1200 → 4800 → 19200 → 76800 → `score_source="ai"`); and the capped half of the
  contract (free, same JD, same provider → `truncated_response` →
  explicitly labelled preliminary).

### Fixed

- **`value or default` read "unlimited" as "unset"** in four places —
  `chat_completion`'s explicit-config merge, `get_user_ai_config`,
  `input_budget_chars` and the Settings form's prefill/save. A user who chose
  *Unlimited* silently got the platform ceiling back on the next read, and every
  save re-applied it. All four are first-value-set now (`0` counts as set).
- **An unlimited input budget collapsed to the 1000-char floor.**
  `input_budget_chars` computed `max(1000, 0 * 4)`, so switching the budget off
  would have shredded every prompt on the one path that is meant to be free of
  clamps. It returns `0` (= no cap for `fit_prompt_part`) instead.
- **`limit_for` raised `TypeError` on a `None` plan limit** (`int(None)`);
  `None` is now another spelling of unlimited, and an unparseable value fails
  open rather than bricking the tier at billing time.
- **`entitlements_snapshot` invented `remaining: 999999`** for an unlimited
  limit — a number the Billing grid then rendered as real headroom. Unlimited
  keys report `remaining: null` + `unlimited: true`.
- `ai_max_output_tokens` is excluded from the per-key usage snapshot
  (`NON_CONSUMABLE_LIMITS`): it is a *cap*, not a quota, and the billing grid
  used to render every cap as "0 used".
- `truncated_response` now tells the user what actually fixes it — set Max
  output tokens to `0` (unlimited) in Settings → AI API or `AI_MAX_OUTPUT_TOKENS=0`
  — instead of only pointing at a raised ceiling.

## [2.2.6] — 2026-09-13

**Funding radar: scan through a search engine when one is configured.** With
`RESPECT_ROBOTS_TXT=true` (the default) every funding scan failed out of the
box: the only discovery mechanism was a hard-coded fetch of
`https://efts.sec.gov/LATEST/search-index`, and `efts.sec.gov`'s robots.txt
disallows automated access to it — so production scans reported `scan_failed`
with `PermissionError: robots.txt disallows …` forever. The fix is not a
robots bypass: when an operator configures a web-search provider, scans run
through that API and never touch `efts.sec.gov` at all; when none is
configured, the direct EDGAR path stays exactly as it was (robots-checked,
honestly `scan_failed` on a block — never a 500, never a fake empty radar).

### Added

- **`app.services.funding_search`** — the search backend behind the radar:
  `search(query, *, window_days, limit)` returning
  `[{title, url, snippet, published_at}]`. Providers: `tavily` (the Tavily
  search API, `FUNDING_SEARCH_API_KEY`; an authenticated API RPC, so the
  robots check does not apply, while the SSRF guard still vets every hop) and
  `stub` (deterministic hermetic fixture, no network — events extracted from
  it are labelled `verified=False`, like demo data). Configured via
  `FUNDING_SEARCH_PROVIDER=tavily|stub` + `FUNDING_SEARCH_API_KEY` (empty =
  auto: tavily when a key is set, else the direct sec_edgar path); one shared
  resolver so settings, scans and `/api/meta` can never disagree. Failures
  raise — the scan reports them as `provider_errors.search`; 4xx is never
  retried, transport errors retry once, like every other provider.
- **`search` funding provider** — when a search engine is configured it
  replaces the direct `sec_edgar` slot in every scan resolution (explicit,
  per-user or `auto`). Queries are built deterministically from the
  AI-extracted search context (funding focus → industries → keywords, plus a
  role-angled query; max 3), results are de-duplicated by URL, and a grounded
  AI pass (`funding-extract-v1`) extracts `FundingEvent`s from the snippets:
  the cited row's `url`/`snippet`/`published_at` are the only sources for
  those fields, and a company name that appears in no result's text is
  fabrication — the whole answer is rejected
  (`guardrail_failed`/`blocked_needs_action`), the same trust model as the
  ranking pass. Extracted events then flow through the unchanged workflow:
  window/stage filters, `ai_rank_events`, `sync_funding_db`,
  `persist_funding_scan` (history contract untouched).
- **Search status surfaced** — `GET /api/funding/providers` (and
  `public_settings()`/`/api/meta`) expose a secret-free
  `{provider, configured, hint}` block; the Funding page renders a badge:
  green `search: tavily` when configured, amber `direct EDGAR only` with the
  setup hint when not (search-derived rows link their publication as
  `source`, not `filing`).
- **Hermetic coverage** — stub-search end-to-end drill (C4b) plus unit/API
  tests: the search path makes zero requests to `efts.sec.gov`; robots-blocked
  EDGAR without a key stays `scan_failed` + `provider_errors.sec_edgar`;
  extraction citations and the fabrication guardrail; search-failure
  reporting and retry semantics; provider-endpoint search status.

### Fixed

- `FUNDING_PROVIDER` values are normalised to lowercase at the config edge
  and in scan resolution — `FUNDING_PROVIDER=SEC_EDGAR` (or a per-user
  setting with capital letters) used to resolve to `unknown_provider` and
  report a dead radar instead of running the configured provider.
- `test_alembic_migrations_apply_to_a_fresh_database` rolled back with
  `downgrade -1`, which since v2.2.5 undoes the funding-history migration
  rather than `scheduled_runs` (the same stale-relative-step bug the funding
  history test already documents). The rollback is now pinned to the
  revision.
- CI lint is green again: `ruff check backend` had 15 pre-existing errors in
  `tests/test_live_drill.py` since v2.2.5 (import order, semicolon
  statements, `assert False`), failing the SQLite job at the lint step.
- `test_c4_funding_honesty` no longer leaks its stand-in into the provider
  registry: it removed the *real* `demo` provider with an unconditional
  `PROVIDERS.pop("demo")`, so any module running after it saw
  `unknown_provider` for demo lookups. It now restores the original entry
  (the default suite order had masked this).

## [2.2.5] — 2026-09-13

### Added
- **Funding radar history** — `funding_scans` + `funding_scan_companies` tables persist every scan (`ok` / `scan_failed`) with `provider_errors`, `events_seen`, `companies_found`, `meta`; diff `new`/`lost` via membership sets; `GET /api/funding/history?limit=5` capped at 30; retention 30 scans/user; UI “Recent scans” with snap scroll on mobile and collapse.
- **Funding ↔ jobs linkage** — shared `normalize_company_name` exact match (punct/suffix stripping ≤200 chars); `FundingCompany.has_open_positions` kept fresh both directions (funding sync and discovery persist); `GET /api/jobs` adds `funding` field + `?company=` exact normalized filter; UI chips both directions (funding chip on jobs, company chip on funding → jobs).
- **E2E live drill suite** — in-process `ThreadingHTTPServer` fake OpenAI on `127.0.0.1:0` with modes `ok`/`fail500`/`fail401` (401 on **both** `/models` and `/chat/completions`), exact JSON contracts per workflow, call log (`workflow`+timestamp), marker `live_drill`; session hermetic fixture (tmp SQLite, `INCLUDE_DEMO_POOL=true`, `AI_*` pointed at fake, watchdog interval small, Worker thread) + 7 drills C1-C7 <60s each exercising pause→watchdog→drain, attempt accounting, blocked, funding honesty, discovery top-12, `why_empty`, auto-mode.

### Changed
- **Mobile pass** — viewports ≤768 px: no horizontal scroll at 360/390, chips `flex-wrap`, single-column cards `grid-cols-1`, banner stacking `flex-col` on `sm`, funding history snap/collapse, tap targets `min-h/w ≥44px`, no hover-only interactions (all hover affordances have `focus`/`active`/`click` equivalents).

## [2.2.4] — 2026-09-13

**Discovery: an empty board says why it is empty.** In a live environment without
internet — or with sources misconfigured — a discovery run legitimately finds
nothing, and the run's internal report has always tracked per-source status. But
the product surfaced only "no fresh jobs", so a user could not tell *no sources
enabled* from *every source failed* from *nothing fresh in the window* — three
situations with three different fixes (configure a source, retry after an outage,
widen the window). The opt-in demo pool (`INCLUDE_DEMO_POOL`, off by default in
every environment) was another silent contributor to empty boards whose absence
was never explained.

Emptiness is now explainable: the run classifies itself once, at run time, a new
read endpoint serves that verdict, and the Jobs page's existing empty state
renders it. **Reporting only** — no change to fetching, scoring, quotas, dedupe,
the demo-pool default, the queue, the schema (the report rides in the existing
`payload.result` JSON) or the environment surface.

### Added

- **`why_empty` in the discovery report** — exactly one of
  `no_sources_configured` (nothing was attempted: `report["sources"]["requested"]`
  is empty, which covers both a deliberately empty source selection and
  `live_enabled` off, where the fan-out never runs), `all_sources_failed` (one or
  more sources were requested and **every** one of them is in `errors` — an
  outage, not a quiet market) or `no_fresh_postings` (the fan-out kept something,
  i.e. at least one source in `ok`, and nothing survived the freshness window and
  the dedupe against the board). It is set **only** when the run added zero jobs;
  a run that found jobs has no `why_empty` key at all — not `null`, not `""` — so
  an absent key is the claim "this run found jobs". The vocabulary is a constant
  triple (`WHY_NO_SOURCES_CONFIGURED` / `WHY_ALL_SOURCES_FAILED` /
  `WHY_NO_FRESH_POSTINGS` in `app.services.discovery`), classified in
  `discover_for_user` and never re-derived by a caller, so the queue row, the run's
  log line and the API cannot disagree. There is deliberately no fourth reason for
  "candidates were found but all were already on the board": such a run adds
  nothing *because* the board already has those jobs, so the board is not empty.
  With the demo pool enabled a run always finds jobs, so `why_empty` can never be
  set in that case — pinned by a test.
- **A `summary` block in the same report** —
  `{"configured_sources", "failed_sources", "needs_credentials",
  "demo_pool_enabled", "freshness_window_hours", "fetched", "fresh"}`, every value
  derived from the same per-source report the classification reads (`requested`,
  `errors`, `total`) plus two facts of the run, so the verdict and its accounting
  can never tell different stories. `needs_credentials` is the registry's own
  `unconfigured_source_ids()`, which is what separates "these sources need keys"
  from "the network is down". Pinned decision: `summary` rides on **every** report
  (a successful run's fetched/fresh counts are useful to an operator too); only
  `why_empty` is conditional.
- **`GET /api/jobs/discovery/last-run`** (jobs router, `CurrentUser` + tenant
  scoping exactly like the rest of the jobs API) — the newest *completed* run for
  this user: `pipeline == "discovery"`, `status == "done"`, `payload.result` a
  dict. A dead/failed/paused run has no report and must not produce a banner
  state, and a report-less `done` row is skipped rather than allowed to shadow an
  older completed run. Optional `?persona_id=` filters on the queue item's
  payload **in Python**: JSON subscripting is unreliable across the two supported
  backends (SQLite's `json_extract` vs a PostgreSQL *text* comparison), so a
  SQL-side filter would match on one and silently match nothing on the other — a
  test asserts the emitted SQL contains no `persona_id`, no `json_extract` and no
  `->`. Response: `{"at", "added", "why_empty"?, "summary"?, "ai_rescore"?}`
  (`ai_rescore` passed through for operator transparency), 404 with the standard
  not-found shape when the user has no completed run. Only the summary blocks are
  served — never the full report, which embeds the run's job rows.
- **The empty-board banner in the SPA** — `components/EmptyBoardBanner.tsx`,
  rendered from the Jobs page's existing empty branch (no new modal, no new page).
  Five states in priority order: last-run 404 → "No discovery run yet" + the
  existing Discover-now CTA; `no_sources_configured` → "No job sources are
  enabled" + a link to the Settings source configuration (`/settings#scraping`,
  a real anchor now) + Discover-now; `all_sources_failed` → "All sources failed on
  the last run" + one chip per source (id + truncated error, full text in the
  chip's `title`) + Settings link + retry; `no_fresh_postings` → "Sources are
  healthy, but nothing fresh in the window" + the window in hours + retry;
  fallback (a run exists with no `why_empty` and the board is empty anyway) →
  "Last run found N job(s) on \<date\>; your board is empty" + Discover-now, so the
  UI never renders a reason-less empty state. While the read is in flight — or if
  it fails for any reason other than 404 — the page keeps its previous plain copy
  rather than claiming a reason it has not read. The banner also surfaces
  `needs_credentials` and says when the demo pool is off, so nothing is fabricated
  to fill the board.
- **`hooks/useDiscoveryLastRun.ts`** — the thin read: a 404 is an *answer*
  ("no completed run"), any other failure leaves the state unknown, and the
  persona the board is filtered by is forwarded as `?persona_id=`. It is only
  called while the board is empty, so a full board never pays for it.
- **Frontend component tests, and the runner they need.** The SPA had no test
  infrastructure at all, so `vitest` + `@testing-library/react` + `jsdom` are
  added as devDependencies with `npm run test` and a standalone
  `frontend/vitest.config.ts` (`vite.config.ts` stays free of test-only
  dependencies, so `npm run build` — including the production image build — never
  needs vitest). 20 tests: all five banner states including the fallback and its
  pluralisation, the not-known-yet state, the failed-source chips and their
  truncation, `needs_credentials`, the demo-pool note, the CTA wiring, the
  formatting helpers, and the hook (200 / 404 / 500 / persona forwarding /
  disabled).
- **`backend/tests/test_discovery_empty_board.py`** (28 tests, hermetic — the
  repo's faked source fan-out and adapters, the deterministic AI stand-in, no
  network): the three classifications plus `live_enabled` off; `why_empty` absent
  (not null) when jobs were added, including in the persisted JSON; the demo pool
  never producing a reason; the `summary` block matching the source report key for
  key; `needs_credentials` coming from the registry helper; the endpoint's 404,
  its non-completed-run filter, newest-run selection, tenant isolation, the
  payload-level persona filter and its Python-side SQL, a dead run not shadowing a
  completed one, never serving the full report, and an end-to-end
  trigger → worker → `payload.result` → endpoint pass; and the empty-source-list
  fix at every layer.

### Changed

- **`discover_for_user` returns *the* run report.** The function built two dicts:
  `report` carried `started_at`, `keywords`, `demo_pool` and `freshness_relaxed`
  and was then dropped, while a separate `summary` was persisted. The returned
  report is now that one dict, so the persisted result also carries when the run
  started (what the endpoint serves as `at`), the keywords it ran with, whether the
  demo pool contributed and whether the window was backfilled. Existing keys
  (`scanned`, `fresh`, `inserted`, `sources`, `elapsed_seconds`, `ai_rescore`,
  `jobs`) are unchanged, so every existing reader — `handle_discovery`'s persona
  signal, the auto-mode notification, `GET /api/pipelines/jobs` (which strips
  `payload.result`) — behaves as before.
- **The run's log line carries the verdict**: `discovery: N scanned, M inserted in
  X.Xs (ai_rescore …) why_empty=no_fresh_postings`, with the same key under
  `extra_fields` for JSON logs. An empty board is now diagnosable from the log
  alone, without opening the queue row.
- **`fetch_all`'s report always carries `total`** (the "nothing was attempted"
  early return used to omit it), so a reader of `summary.fetched` never has to
  special-case that run.

### Fixed

- **An explicitly-empty source list was silently upgraded to "all built-in
  sources"** — a related bug found while making emptiness explainable, fixed here
  because it made `no_sources_configured` unreachable. Two `or` fallbacks swallowed
  the difference between *unset* and *explicitly empty*:
  `handle_discovery` did `payload.get("sources") or config["sources"]` and
  `fetch_all` did `sources or available_source_ids()`, so a user who had
  deliberately saved zero sources in `scraping.sources` still got every available
  adapter fetched — and the response of `POST /api/jobs/discover` claimed sources
  the caller had just refused. The config layer clearly intended the distinction
  (`discovery_sources_config` checks `if sources is None`, not `if not sources`).
  **Decision: option (a) — the whole chain now respects an explicit `[]`.**
  `None`/absent → the defaults; `[]` → nothing attempted → `no_sources_configured`.
  `POST /api/jobs/discover` keeps an explicit `[]` in both the queued payload and
  its response, `handlers._payload_sources` is the one place the queue payload's
  key is interpreted (absent/`None` → config, list including empty → verbatim,
  comma-separated string → parsed, anything else → config rather than
  "everything"), and `fetch_all` resolves `None` to the available adapters and
  `[]` to no fan-out at all. Reachability is documented and pinned: the Settings
  page renders `scraping.sources` as read-only chips, so today an explicit `[]` is
  written through `PUT /api/settings` or a `sources: []` discovery request, while
  the switch a user *can* flip in the UI is `live_enabled` — which skips the
  fan-out and lands in the same reason.
- **The run's own clock was computed and thrown away** (see Changed): `started_at`
  never reached the persisted report, so nothing downstream could say *when* a run
  happened. The banner's "Last run found N jobs on \<date\>" and the endpoint's `at`
  both read it now, with the queue row's `finished_at` as the fallback for reports
  written by an older version.

### Unchanged, deliberately

- **No behaviour change in fetching, scoring, quotas, dedupe or the queue**, no
  schema change and no migration (the report lives in the existing JSON), no new
  entitlement, and **no new environment variable** — `.env.example` is untouched.
- **`unconfigured_source_ids()` is wired into `summary.needs_credentials` as-is**,
  and with today's adapter set it always returns `[]`: every key-gated adapter
  (Adzuna, Jooble, USAJOBS) reports `available() == False` when its credentials are
  missing *and* `configured() == available()`, so nothing is ever
  "available but unconfigured". The key is honest either way and starts carrying
  weight the moment an adapter separates the two; the tests pin the wiring, not the
  (currently empty) content.
- **The full report is still persisted per run** in `payload.result` and grows with
  every run (it embeds the run's job rows). The new endpoint deliberately serves
  only the summary blocks; the storage question is noted in the PR and left alone.

## [2.2.3] — 2026-09-13

**Discovery: queued runs get the real AI verdict for the top slice (tier-gated).**
`discover_for_user` has always supported a guardrailed AI verdict for the top
`AI_RESCORE_TOP` (12) candidates — `score_source='ai'` with evidence, and a
*hard* dependency on the model (transient outage → the queued run pauses and
re-executes when the provider is back; needs-action → it dead-letters with the
diagnosis; nothing persisted before the slice, so a paused-and-resumed run
creates exactly the same rows as an uninterrupted one). Its only caller,
`handlers.handle_discovery`, never loaded the user's profile, so
`use_ai=bool(profile_data)` was permanently `False`: **every** discovered job was
a keyword-overlap estimate, the AI top slice and its whole pause contract were
dead code, and the AI verdict existed only per job
(`GET /api/jobs/{job_id}/intelligence`, Pro). Auto-mode notifications compensated
with the honest caveat "Scored by keyword overlap — open a job for the AI
verdict." The handler now loads the profile the same way `/intelligence` does and
gates it on the entitlement `/intelligence` already enforces.

Backend-only plumbing plus report provenance: no schema change, no migration, no
new environment variable, and no new entitlement or quota.

### Added

- **`ai_rescore` in the discovery report** —
  `{"enabled": bool, "skipped": <reason|null>, "scored": <int>}`, where `skipped`
  is exactly one of `no_profile` (no profile row, or one with empty `data`),
  `plan_free` (no `can_use_advanced_matching`) or `null` when the slice ran, and
  `scored` is how many job rows actually persisted with `score_source='ai'`
  (rows, not calls: a candidate the model could not verdict — guardrail
  rejection, no text to score — is not an AI-scored job, and a run whose insert
  lost a uniqueness race created nothing to score). It rides in the queue item's
  `payload.result` alongside `scanned` / `fresh` / `inserted` / `jobs`, so there
  is nothing to migrate. When jobs were found but the slice was skipped, this is
  the only way an operator — or the upcoming empty-board work — can tell *why* a
  board is full of estimates, and the two reasons have different fixes ("upload a
  resume" vs "upgrade the plan").
- **The reason vocabulary is a constant pair**, `AI_RESCORE_NO_PROFILE` /
  `AI_RESCORE_PLAN_FREE` in `app.services.discovery`, so the caller and the
  reporter cannot drift into free text. Precedence is documented and deliberate:
  a missing/empty profile is reported before the plan gate (it is checked first,
  and there is nothing to score against either way).
- **The block is in the run's log line too** — `discovery: N scanned, M inserted
  in X.Xs (ai_rescore enabled=… skipped=… scored=…)` with the same dict under
  `extra_fields.ai_rescore` for JSON logs. `GET /api/pipelines/jobs` strips
  `payload.result` (unchanged), so the row and the log are where it is read.
- **`backend/tests/test_discovery_ai_rescore.py`** (12 tests, hermetic — the
  repo's scripted local provider on the wire paths, the deterministic stand-in
  where no wire is involved, a faked source fan-out, no network): the Pro
  top slice and its report; a free run that completes with **zero** wire calls;
  no-profile and empty-profile; a mid-slice outage that pauses, persists nothing
  and then completes exactly once after the watchdog drains; the v2.2.2 failure
  budget surviving a pause/resume cycle (driven through the worker, not just the
  queue functions); a 401-style needs-action failure dead-lettering with the
  diagnosis; the auto-mode notification with and without the caveat; a run with
  nothing to score; and the SPA's provenance badge still covering both values.

### Changed

- **`handle_discovery` loads the profile and hands it to the batch run.** The
  user's latest `Profile` row (`Profile.user_id == user.id`, newest `created_at`
  first) via `apply_flow.latest_profile` — the identical query
  `GET /api/jobs/{job_id}/intelligence` runs — and its `data` dict is what turns
  the AI top slice on.
- **The AI verdict is tier-gated with the existing entitlement.**
  `can(db, user.id, "can_use_advanced_matching")` decides, and it is a
  *permission* check: no `enforce()`, no new entitlement, no quota, nothing
  consumed. The profile is **withheld wholesale** when the gate is closed rather
  than passed with `use_ai=False`, because the same profile also switches on the
  AI company-size verdict for the top `AI_CLASSIFY_TOP` (5) rows — so a free-tier
  run makes exactly zero AI calls, which is what the test asserts on the wire. A
  free user's board is not a back door around the plan gate `/intelligence`
  enforces.
- **Free-tier runs are otherwise unchanged**: they complete, every row is
  `score_source='preliminary'`, and the estimate is the same "insufficient data"
  default every batch run produced before this release (the profile is not passed
  at all, so the deterministic pre-rank has nothing to overlap against).
- **The batch slice gets the same context the per-job path gives the model** —
  verified at the call site rather than assumed: `db=db`, `user_id=user.id` (so
  the gateway resolves *that* user's key, token budgets and credit ledger) and
  `persona=<the persona_context handle_discovery already builds>`, so a
  persona-scoped run scores against the track. The test asserts the persona block
  reaches the wire prompt.
- **Auto-mode high-match notifications drop the caveat only when it is untrue.**
  `_notify_auto_discovery` already read `score_source` off the surfaced rows; now
  that a queued run can produce `ai` rows, a Pro run whose three highest scorers
  all carry the model's verdict no longer tells the user they were "Scored by
  keyword overlap". A mixed run keeps it — the top slice is 12 rows while the
  notification surfaces the three highest scorers, so "the run AI-scored
  something" is not the same claim as "these three numbers came from the model" —
  and so does an all-preliminary one. No new notification kinds; the stale
  "recorded in the 2.2.0 PR as out-of-scope" comment is gone.
- **README**: the discovery paragraph said postings are "scored (AI or
  calibrated heuristic)" — a claim that was only true per job. It now says what
  the batch does: a deterministic pre-rank for everything, plus the guardrailed
  AI verdict for the top 12 on Pro, with free-tier runs staying on the labelled
  estimate.

### Fixed

- **A related bug found while wiring the slice up: `score_job` labelled a verdict
  the model never gave as `"ai"`.** When there is nothing to score — an empty job
  description (every source adapter defaults a missing one to `""`), or a profile
  with no text in it — `ai_score_detailed` answers from the deterministic
  breakdown *without calling the model* and marks its own detail
  `source="insufficient_data"`, but `score_job` wrapped that in
  `score_source="ai"` with the reason `"AI: Not enough profile or job text to
  score"`. Turning the batch slice on made that reachable at scale: a source that
  returns titles and URLs only would have produced a board of rows badged **"AI
  verified"** in the SPA, an `ai_rescore.scored` count claiming verdicts that
  never happened, and — worst — an auto-mode notification that *dropped* the
  keyword-overlap caveat because its top three rows all said `ai`.
  `score_job` now returns the provenance it has always documented for the case
  (`insufficient_data`), which is also what the free tier's
  `preliminary_score_detailed` already returned for the same input and what the
  SPA's `SourceBadge` already has a chip for ("no data"). Both callers benefit
  (the batch slice and `/intelligence`), no default, rubric, guardrail or
  top-slice size changed, and the existing test that exercises this function
  already accepted `insufficient_data` as a legal answer. Covered by two new
  tests: a whole run with nothing to score reports
  `{"enabled": true, "skipped": null, "scored": 0}` and makes zero scoring calls,
  and `score_job` reports `insufficient_data` for both doors while a real pair is
  still `"ai"`.

### Unchanged, deliberately

- **The two failure postures stay distinct — they were not merged.** The batch
  path is hard: `_score_candidates` calls `score_job(...,
  allow_preliminary=False)`, which raises, and the worker turns a transient
  outage into a pause and a needs-action failure into a dead letter. The per-job
  path stays soft: `/intelligence` returns `ai_error` (never papered over), and
  the free tier gets the preliminary breakdown plus `upgrade_hint`.
- **`score_job`'s defaults, the rubric, the guardrails and `AI_RESCORE_TOP = 12`
  are untouched**, as is the cost-control design: one model call per candidate in
  the top slice, deterministic labelling for the bulk.
- **v2.2.2's queue semantics are untouched and now have an integration-level
  guard**: `attempts` counts handler failures only, `pause()` consumes none, and
  a run that sat out an outage still retries its first real failure and
  dead-letters on the Nth.
- **No new environment variable** (`.env.example` is unchanged), no migration and
  no schema change.

## [2.2.2] — 2026-09-13

**Queue fix: `attempts` counts failures, not claims.** The durable queue
incremented `PipelineJob.attempts` when a worker *claimed* an item
(`job_queue.claim` / `claim_item`), while `fail()` decided retry-vs-dead-letter
by comparing that counter against `max_attempts`. But `pause()` (transient AI
outage → item parked in `paused`) deliberately consumes no attempt, and the
watchdog's `drain_paused` re-queues the item — which is then claimed again and
incremented again. So every pause → resume cycle spent failure budget without
the work ever failing. Both consequences were reproduced: (a) an item with
`max_attempts=3` and **zero** handler failures was claimed five times across
three outage cycles, and (b) it then dead-lettered on its *first* real failure.
In a live outage drill a job was dead-lettered at `attempts=6, max_attempts=3`.

Backend semantics, the UI copy that explains them, and the tests. No new
environment variables, no schema change, no migration — the counter's *meaning*
changed, not its type or its column.

### Fixed

- **`claim()` and `claim_item()` no longer increment `attempts`.** Claiming
  takes a lease (`status='processing'`, `locked_by`, `lease_expires_at`) and
  nothing else. Being handed to a worker is not a failure, so a claim that ends
  in a pause, a crash or a deploy costs the item no budget.
- **`fail()` increments first, then applies the budget.** `attempts` grows by
  one and *then* the check runs: `attempts >= max_attempts` → `dead`, otherwise
  re-queue with backoff exactly as before. `max_attempts=3` now means three
  recorded failures, and the third one is the one that dead-letters — N
  consecutive real failures still dead-letter on the Nth, and a dead item is
  never claimable again.
- **The backoff schedule is unchanged.** It is keyed on the failure just
  recorded, so the progression a caller sees is identical to before (first
  failure → `AI_BACKOFF_BASE ** 1 * 5` s, second → `** 2 * 5` s, capped at
  600 s). Previously the claim supplied that exponent; now the failure does.
  Only pause/resume cycles — which were the bug — behave differently.
- **`POST /api/ops/queue/retry-dead` resets *both* budgets.** It cleared
  `attempts` but left `payload.paused_count` at `AI_PAUSE_MAX + 1`, so an item
  that had dead-lettered on the *pause* cap was re-queued with its outage budget
  already spent and died again on its very next pause — the operator's retry
  looked like a no-op. Same confusion, same fix: the two counters are separate
  and a manual retry starts both from zero.
- **Queues UI: "gave up after N attempts" was the claim count.** A `dead` row
  now says *why* it gave up, from the counter that actually decided it —
  "gave up after 2 of 3 failed attempts", "gave up after 12 AI-outage pauses —
  it never failed", or both when both apply. Under the old copy a
  pause-capped job read "gave up after 0 attempts" (or, before this release,
  after attempts it had never failed).
- **Copy that explains "attempts" now says *failure* attempts.** Labels are
  unchanged; only the explanations are: the `failed` / `paused` / `dead` chip
  hints on Queues ("parking costs no failure attempt"), the dead-row tooltip
  ("Attempts counts handler failures only — being claimed, or parked by an AI
  outage, costs nothing"), the Auto-mode card in Settings ("Retries count
  *failures*: a run parked by an AI outage and resumed spends none of them")
  and the auto-run `paused` note in `frontend/src/lib/automation.ts`.
- **The contract is written down where it is implemented.** The
  `app.services.job_queue` module docstring now has a "Retry accounting"
  section: `attempts` = handler failures, incremented only by `fail()`;
  `paused_count` / `AI_PAUSE_MAX` is the separate outage budget; claims, pauses
  and lease expiries touch neither. `claim()`, `claim_item()`, `fail()`,
  `pause()`, `recover_stalled()` and `enqueue()` docstrings say the same at the
  point of use, and `WORKER_MAX_ATTEMPTS` is documented as a *failure* budget in
  `.env.example` and `app/core/config.py`.

### Unchanged, deliberately

- **`pause()` still consumes no attempt**, and its own safety valve is intact:
  `payload.paused_count`, capped at `AI_PAUSE_MAX = 12`, dead-letters an item
  that sits out too many outages — independently of `attempts`, in both
  directions (an outage never spends failure budget, a failure never spends
  pause budget).
- **`recover_stalled()` keeps its rule**: a stalled item whose *failure* count
  has reached `max_attempts` is dead-lettered on lease expiry, otherwise it is
  re-queued. It never increments `attempts` itself. With the counter now honest
  this branch is reached only by rows whose budget was really spent — including
  pre-v2.2.2 rows whose counter was inflated by claims, which must not be
  resurrected into an endless crash loop. *Trade-off, stated plainly:* an item
  whose handler hard-crashes the worker (so `fail()` is never called) is now
  re-queued on every lease expiry rather than dead-lettered after
  `max_attempts` crashes. That is the point of the fix — a crash is not a
  failure of the work — and the loop is slow and observable rather than silent:
  each cycle waits out `WORKER_LEASE_SECONDS` and lands in
  `jobhunter_queue_recovered_total` / `jobhunter_worker_task_crashes_total`,
  while the pause path keeps its own independent cap.
- **No new environment variables.** `WORKER_MAX_ATTEMPTS` keeps its name and its
  default (3); only its documented meaning is now precise.

### Tests

New hermetic suite `backend/tests/test_queue_attempts_semantics.py` (9 tests,
`job_queue` level — no provider, no network, no worker loop; `claim`,
`claim_item`, `pause`, `drain_paused`, `fail` and `recover_stalled` are called
directly). It is red against the old code and green against the new:

- an item with `max_attempts=3` survives **five** pause/resume cycles with zero
  failures (`attempts` stays 0, 5 claims) and its first real failure still
  returns `retrying`;
- a second variant proves the whole budget survives the outage: 4 pauses, then
  failures 1 and 2 retry and failure 3 dead-letters;
- N consecutive real failures → `dead` on the Nth, `attempts == N`, and neither
  `claim()` nor `claim_item()` will pick the row up again;
- lease expiry on a stalled item: failures below the cap → re-queued with the
  counter untouched (repeatedly — a crash loop with no failures never loses
  budget); failures at/over the cap → `dead` with `finished_at` and
  `error="lease expired"`;
- the pause budget still dead-letters on its own: 13 pauses on an item with
  `max_attempts=50` and `attempts == 0` → `dead` with "paused 12 times during AI
  outages";
- a non-retryable failure is still dead on the first failure (and now records
  `attempts == 1`);
- `POST /api/ops/queue/retry-dead` re-queues a failure-dead and a pause-dead
  item with both counters at zero, and the pause-dead one can sit out another
  outage afterwards (`test_ops_health_and_privacy.py`).

Three existing assertions pinned the old claim-count semantics and were
adjusted — **only** those, and only the numbers, never the outcome under test:

- `test_queue_and_worker.py::test_claim_is_exclusive_and_priority_ordered` —
  `first.attempts == 1` → `== 0` (a claim is not a failure).
- `test_queue_and_worker.py::test_failure_retries_with_backoff_then_dead_letters`
  — after the second claim of a `max_attempts=2` item, `attempts == 2` → `== 1`
  (one recorded failure; the claim adds nothing). The test still ends with the
  second failure dead-lettering the item, now also asserting `attempts == 2`.
- `test_ai_pause_resume.py::test_queued_job_paused_on_outage_then_recovers_without_duplicates`
  — `item.attempts == 1` ("the claim consumed the attempt; the pause must not")
  → `== 0` ("neither the claim nor the pause may consume a failure attempt").
  Its module docstring records the same clarification.

Every other retry/dead-letter expectation in the suite was already written
against failures and passes untouched: 425 backend tests green (415 before this
change — 9 new queue-semantics tests plus the `retry-dead` one), `ruff` clean,
`mypy` delta zero (767 pre-existing errors before and after), frontend
`tsc --noEmit` and `vite build` clean.

## [2.2.1] — 2026-09-13

**Frontend finishing pass over the v2.1–v2.2 surfaces.** The three backend
tickets shipped their own UI; this release audits those four pages against the
live contracts (`GET /api/pipelines/stats`, `/api/pipelines/jobs`,
`/api/funding/companies`, `/api/automation`, plus `/api/dashboard/summary`),
closes the visibility gaps and removes the dead paths the backend landing left
behind. Frontend-only — no queue, scheduler, funding or billing logic changed.

### Added

- **Queues — `paused` and `dead` summary chips.** The per-pipeline chip row now
  renders all seven statuses `queue_stats` returns (`queued processing done
  failed needs_input paused dead`) instead of five. `paused` is amber when
  non-zero (it is the v2.1 headline state), `dead` is grey, and every chip has a
  hover hint saying what the state means.
- **Queues — the amber row explains itself.** A `paused` job row now carries
  "AI outage — will resume automatically" next to its status pill; a `dead` row
  says "gave up after N attempts". Kept to the list row — no separate section.
- **Settings — Auto-mode card stays fresh.** The card re-reads
  `GET /api/automation` (and the AI status banner) every 30 s while the page is
  mounted, cleared on unmount; a failed poll keeps the last read instead of
  blanking the card (verified: consent restored on the server flipped the card
  from "on — not queueing" to "scheduled" within one tick, and a broken poll
  left it untouched). The footnote now says so.
- **Dashboard — paused work is visible on the front door.** When any pipeline
  has paused items the hero shows "N paused — AI outage, will resume
  automatically" (links to Queues) and the "Open queues" pill appends
  "• N paused".

### Changed

- **`next_run: null` is never a date, and never a vague "waiting".** Settings
  renders it as "—" (auto mode off), "blocked — see above" (on but
  `active=false`), or "running now — next set when it finishes" (on and active,
  i.e. that workflow's run is in flight). A `next_run` already in the past
  reads "due — starts at the next sweep" rather than "in 0s". Dashboard renders
  the same null as "off" / "on — blocked" / "running now" using
  `auto_mode_active`, which the summary endpoint already returned but the UI
  ignored.
- **Copy:** the AI pipeline card no longer advertises a "green/red online dot"
  it never had; it describes what it is — a durable priority queue that pauses
  on an AI outage and resumes on its own. Funding's `mined` context label now
  says why it is deterministic ("nothing for the model to read yet").

### Fixed

- **Queues: the "Next in AI queue" panel never rendered.** It read
  `stats.ai.ai_queue`, but `GET /api/pipelines/stats` puts `ai_queue` (and
  `rate_limiter`) at the *top level* of the payload, as siblings of the
  per-pipeline buckets — so the live v2.1.1 durable-queue preview
  (`queue_preview` top-3 + `processing_now`) was silently hidden behind a
  falsy guard on every render. It now reads `stats.ai_queue`, shows an explicit
  "idle — nothing waiting" when empty, flags paused entries as "resumes
  automatically", and shows the row id so it can be matched to the job list.
- **Queues: removed the dead `?? stats[p.id]?.ai_queue?.[k]` fallback.**
  `ai_queue` has no per-status keys, so the branch could never fire; the chip
  reads `stats[p.id]?.[k] ?? 0` only.
- **Dashboard: the "Pipelines (FIFO)" Running / Completed / Failed trio was the
  *application* pipeline only.** `automation.running/completed/failed` in
  `/api/dashboard/summary` are computed from one pipeline, while the label
  claims all of them. The same payload carries `automation.queues` (every
  pipeline's seven buckets), so the trio now totals across pipelines
  (running = queued + processing; failed = failed + dead), falling back to the
  old fields if `queues` is absent.
- **Funding: `context.source` label leaked the raw tag.** The persona merge
  tags the context `ai+persona` / `mined+persona`; the exact-match labels only
  caught bare `ai` / `mined`, so users with a persona (every user — a default
  one is ensured) saw the raw string. Matched on the prefix.
- **Funding: paused / blocked panels now say the rows below are the last
  successful scan** (the `scan_failed` panel already did), so an outage over a
  populated radar is never mistaken for fresh results.

### Contract audit (no change needed)

Every rendered value on Queues, Funding, Settings (auto card) and Dashboard
was traced to a field the live endpoint returns. Confirmed present:
`companies[].{source,verified,why,rank,open_positions,raised_at_estimated}`,
`scan_status/reason/scanned/last_report.{errors,ai.*}/window_days/refreshed_at/
resume/persona/context.persona`; `automation.{can_use,cadence,locked_reason}`
on `GET /api/settings`; `overview.{active,blocking,toggles,sweep_interval_seconds,
last_run,next_run,recent,quota}`; `entitlements.usage.automation_runs_per_month`
and `limits.automation_runs_per_month`; `rate_limiter.{rpm,remaining,throttled}`.
All AI-backed failures on these pages route through `aiOutage()` +
`AIOutageBanner` / `AIStatusBanner`; the auto-mode 403 shows the server's
`detail.message`, never the code. No "v2.1.1 pending" placeholder copy remained.

### State walkthrough

Run on a live app (real worker, stub OpenAI-compatible server for the "online"
leg, unreachable `base_url` for the outage leg): Queues in empty / outage
(3 paused + 1 dead, chips and preview matched `GET /api/pipelines/stats`
exactly) / online (watchdog resumed all three → `done: 3`); Funding in
`scan_failed` (provider error list), `paused` (retry hint), `blocked` ("Fix"
line + Settings link), `ok` + partial provider failure ("Partial scan:
crunchbase (not_configured)") and `ok` + `no_matching_events`; Settings auto
card on free (Locked + upgrade link), Pro+ with `skipped_outage` runs, in-flight
(`next_run: null` → "running now"), consent revoked ("blocked — see above",
`blocking` listed) and the 30 s refresh; Dashboard in each of those.

## [2.2.0] — 2026-09-13

**Auto mode.** Pro and Pro+ can now hand their search to a scheduler that queues
work on a cadence instead of waiting for a click — and every decision it makes,
including every decision *not* to run, is written down where the user can read
it. Until now the promise was a phantom: `can_use_scheduled_workflows` was True
for both paid plans and **nothing consumed it**, `automation_runs_per_month` was
a quota **nothing incremented**, and the Dashboard's automation readout was wired
to that permanently-zero counter. All three are now real.

### Added

- **`backend/app/services/auto_scheduler.py`** — the scheduler, and the only
  place any schedule is written. `AutoScheduler.run()` is the loop,
  `AutoScheduler.sweep()` one pass (callable from tests and by an operator),
  `AutoScheduler.sweep_user()` one user's pass.
  *It only decides when to enqueue.* Every unit of work is a normal
  `pipeline_jobs` row run by the **existing** handlers, so claim/retry/
  pause/dead-letter semantics, per-user AI keys and the v2.1 pause contract
  apply to auto work exactly as they do to a clicked run. `job_queue.py` is
  untouched.
- **The cadence table** (`CADENCE_SECONDS`) — one documented table, read by the
  sweep, `GET /api/settings`, `GET /api/automation` and the dashboard:

  | plan | discovery | funding radar | application prep |
  |---|---|---|---|
  | `pro_plus` | 7 200 s (2 h) | 21 600 s (6 h) | 86 400 s (24 h) |
  | `pro` | 43 200 s (12 h) | 86 400 s (24 h) | — (not on this plan) |
  | `free` | no entry — and the settings gate refuses to store `auto_mode: true` | | |

  Pro deliberately has **no application-prep**: a prepared application spends the
  tailored-resume budget, so the daily automated pass is the Pro+ tier's. Adding a
  row there is the whole change when a cadence is added — no other module
  hardcodes a schedule.
- **"Due" is one rule, used by the writer and the readers** (`is_due` /
  `next_run_at` / `run_clock`): the last auto run **in a finished state**
  (`done | paused | failed | needs_input`, `FINISHED_STATES`) older than the
  cadence. A window that never ran is always due, the comparison is inclusive at
  exactly one cadence, and a *skip* never resets the clock — so work happens as
  soon as its blocker clears instead of a lost day later.
- **One enqueue per cadence window, ever.** The window is
  `cycle_bucket = epoch_seconds // cadence` — taken as **UTC explicitly**, because
  every timestamp in this schema is naive UTC and `datetime.timestamp()` on a
  naive value would shift every window by the host's offset on a non-UTC machine.
  The bucket is written into `scheduled_runs.cycle_bucket` **and** into the
  queue's `dedupe_key` (`auto:{workflow}:{user_id}:{bucket}`), so two sweeps —
  even in two worker processes — create exactly one item.
- **AI gate before enqueue** (`attempt`): `await ai_availability(db, user_id)`;
  any state other than `online` enqueues *nothing* and records
  `skipped_outage` with the provider's reason, a `retry_after_hint` and a hint.
  `ai_availability` is a 60 s-cached probe, so checking costs nothing, and the
  v2.1 pause contract still covers an outage that starts mid-run — queueing work
  we already know will pause is churn, quota burn and a worse queue for everyone.
- **`scheduled_runs`** (model + migration `c3d4e5f6a7b8`, down_revision
  `b2c3d4e5f6a7`): the scheduler's clock, its idempotency guard and its history —
  `user_id, workflow, cycle_bucket, triggered_at, queue_job_id, job_id, state,
  reason, meta`, indexed on `(user_id, workflow, triggered_at)` and
  `(user_id, workflow, cycle_bucket)`. Idempotent (skips when the table exists),
  SQLite- and Postgres-valid, reversible; `reconcile()` folds each open row's
  queue status back into the history at the start of the user's pass
  (`queued → done|paused|needs_input|failed`, `dead` → `failed`), which is what
  advances the clock and releases the in-flight guard — no new hook in the queue.
- **Quota, charged where the work actually happened.** `Worker._run_item` charges
  `automation_runs_per_month` **once, on success, for items whose payload says
  `trigger: "auto"`** — the single place auto runs are counted, because a paused
  or failed run is not work the user got. Manual triggers keep charging it at
  request time (`jobs.py:282/313/426`), so no path counts twice and no path counts
  nothing. Before the enqueue, `quota_block()` checks the run budget *and* the
  per-action budgets a manual click is held to
  (`jobs_discovered_per_month`, `jobs_discovered_per_day`,
  `funding_companies_per_month`, `applications_per_month`) — `handle_funding` now
  charges `funding_companies_per_month` for an auto scan it just ran, exactly as
  the interactive `POST /funding/companies` does, so a schedule can never be a way
  around a cap. Exhaustion stops all queueing (`skipped_quota`) and tells the user
  **once per calendar period** (in-app only, `link=/billing`).
- **What a run costs and how loud it is:** `AUTO_PRIORITY = 6` (behind every
  interactive trigger: discovery 3, funding 5, application 1),
  `AUTO_APPLICATION_PER_PASS = 1` (one job per pass, highest score first — the
  next pass takes the next job), `AUTO_DISCOVERY_LIMIT = 40` (auto is not a
  cheaper way to run a bigger scan). Auto application-prep refuses to touch a job
  that already has an open item (`job_has_open_item`), so an auto pass and a
  manual `POST /jobs/{id}/apply` can never both hold the same application, and it
  marks the job `queued` + records the job event like the manual path does.
- **Keywords are read, never invented.** `discovery_payload` merges
  `scraping.keywords` with the active persona's stored `search_context.keywords`
  (deduped, user's own first, capped at 20) and carries `trigger: "auto"`;
  `funding_payload` mirrors `POST /funding/refresh` and deliberately omits
  `context`, because building it means `search_context(force_refresh=True)` — an
  AI call that belongs *inside* the pausable item, not in the loop that decides
  whether to create one. No sweep calls the model to work out its own input.
- **Workflow toggles are respected**: `SETTING_FOR_WORKFLOW` maps discovery and the
  funding radar to `workflows.ai_for_discovery` and application-prep to
  `workflows.ai_for_resume` (documented there — those are the only two toggles the
  settings surface has; auto mode invents no switch the user cannot find).
  A workflow the user switched off is never scheduled.
- **`GET /api/automation`** (`backend/app/api/routers/automation.py`, registered
  in `app/api/routes.py`, `CurrentUser` + `DbSession` ⇒ 401 unauthenticated and
  no id to forge): `{enabled, can_use, active, schedulable, plan, cadence,
  toggles, consent_ok, sweep_interval_seconds, blocking[],
  last_run{workflow:{at,state,job_id,queue_id,reason}}, next_run{workflow:iso|null},
  recent[≤5], quota:{used,limit,remaining}}`. Its `cadence` is the sweep's table,
  its `can_use` is the settings gate's capability, its due/next clock is
  `is_due`/`next_run_at`, and its `quota.remaining` uses
  `entitlements_snapshot`'s sentinel — one implementation, so no surface can
  drift. `next_run` is `null` (never a fake countdown) while auto mode is off or
  blocked, or while that workflow's item is still open; every timestamp carries an
  **explicit `+00:00`** because a naive UTC string is read as browser-local by
  `new Date()` and an hours-out "next run" is not cosmetic.
- **Settings surface** (`user_settings.py`): a ninth category `automation` with
  `auto_mode` (default `false`, writable on every tier so switching it *off* is
  never blocked). `PUT /api/settings` refuses to **enable** it without
  `can_use_scheduled_workflows` — `403 {code:"upgrade_required", capability,
  message, fields:["auto_mode"]}`, the exact shape and place of the v2.1
  token-limit gate — and `GET /api/settings` now returns
  `automation.can_use`, `automation.cadence` and `automation.locked_reason`, so
  the UI disables precisely what the server would reject.
- **`.env.example` / config**: `AUTO_SCHEDULER_INTERVAL_SECONDS=300` — how often
  the sweep runs, i.e. how sharply a cadence boundary is hit, **never** how often
  a workflow runs. **`0` disables auto mode process-wide**: the child task is not
  spawned, and `GET /api/automation` reports `scheduler_disabled` instead of
  pretending a sweep will happen.
- **Worker**: the scheduler is a **child task of `Worker.start()`** beside the AI
  watchdog, so it inherits the supervisor (crash → logged with traceback,
  counted, respawned with capped backoff) rather than growing a second one;
  `_slot_names()` lists `auto-scheduler` only when it is enabled, so `task_health`
  and the disabled case stay honest.
- **Frontend**: an **Auto mode** card in Settings beside the workflow toggles —
  the switch through the existing `PUT /api/settings` path (with the locked
  `!can_use` state and the 403 message shown as the server's *message*, never as
  a code or a silent snap-back), a read-only cadence table (workflow → cadence →
  last run → next), the last/next clock, the five most recent runs in their real
  states, the run budget line, the consent and AI-outage notices (the shared
  `components/AIBanner.tsx`), and a 30 s poll that exists only while the card is
  mounted. `frontend/src/lib/automation.ts` holds the state→copy map and the
  formatters so Settings and the Dashboard cannot disagree;
  `Dashboard.tsx` gains one auto-mode line fed by the new `automation.auto_mode` /
  `automation.next_run` fields. Skipped cycles are listed, not hidden:
  `skipped_outage` reads **"AI unavailable — will run next cycle"**.
- **Dashboard/ops**: `summary.automation` gains `auto_mode`, `auto_mode_active`
  and `next_run` (same clock as `/api/automation`); existing keys are unchanged.
  A read failure degrades that block to "off" rather than blanking the dashboard.

### Fixed

Also fixed, all inside the surface this release touched:

- **The persona memory fed by queued discovery was dead code
  (`handle_discovery`, `handlers.py:155`).**
  It read `result["job_ids"]`, a key `discover_for_user` has never returned (the
  summary carries `jobs`), so `record_signal` never fired after a queued
  discovery run — the only kind there is — and the track never learned. Now it
  reads the created jobs.
- **`auto_mode` is coerced, not guessed.** `PUT /api/settings` did no boolean
  validation, and `bool("false")` is `True`: a client sending the *string*
  `"false"` would have stored an *enabled* scheduler on a switch that looks off —
  quota burn on a request to stop. `(automation, auto_mode)` now goes through
  `coerce_bool` (real bools, `0/1`, and the truthy/falsy words UI and env style
  produce), anything else is `400 {code:"invalid_setting_value", fields}`, and the
  gate reads the coerced value.
- **The Settings page offered a field that does not exist.** The "Additional
  questions" textarea wrote `general.additional_questions`, which no backend
  category defines and `_meta.writable` filters out — every save silently
  discarded it while the card's own copy claimed all sections were editable. The
  field is gone and the copy now lists the real categories (`… Email, Workflows,
  Automation`).
- **`test_funding_radar.py` asserted a position in the migration chain.** It
  ran `alembic downgrade -1` and expected the *funding-honesty* revision to be
  undone, which was true only while it happened to be head; adding
  `scheduled_runs` made it roll back the wrong migration. It now downgrades to
  `PRE_FUNDING_HONESTY_REVISION` explicitly, i.e. it tests what it says —
  reversibility of that revision — and `test_alembic_migrations_apply_to_a_fresh_database`
  gained `scheduled_runs` (table + columns) plus a real
  downgrade-and-re-upgrade round trip.

### Tests

- `backend/tests/test_auto_scheduler.py` — 26 hermetic tests: the cadence table
  and the boundary-inclusive due clock; `cycle_bucket`/`dedupe_key` window maths
  (UTC, stable inside a window, advancing across it); free-tier `PUT` → 403
  `upgrade_required` and nothing stored vs. a paid round-trip exposing its
  cadence; `"false"`/`"maybe"` coercion; one enqueue per window and the second
  sweep inside it adding nothing; Pro not running on Pro+'s 2 h; missing consent →
  `skipped_no_consent` and the work due the moment consent exists; AI outage →
  zero queue rows, `skipped_outage` with the provider's reason, then enqueue
  after a scripted recovery; a disabled workflow toggle never scheduled; auto
  mode switched off mid-flight leaving the in-flight run to finish while nothing
  new starts; quota exhaustion → no enqueue, `skipped_quota`, **exactly one**
  notification across three sweeps; the finished-run charge (`used 0 → 1`, a
  manual run charging nothing, no duplicated history row); a paused run charging
  nothing; application-prep queueing the top scored `ready_to_apply` job,
  marking it `queued`, taking the next job next window, and recording
  `nothing_ready` when nothing is; one user's corrupt `scraping.freshness_hours`
  failing exactly that workflow (row + `jobhunter_auto_sweep_errors_total`) while
  their other workflow and the next user are untouched; `run()` surviving a sweep
  that raises and counting it; interval `0` never sweeping; the
  `GET /api/automation` contract (including 401, and that another user sees their
  own empty state); the additive dashboard block agreeing with
  `/api/automation`'s next run to the second; both auto notifications
  (threshold, top-3, honest provenance note, per-run dedupe, "only new verified
  rows this run"); and the persona-signal fix above.
- Full backend suite: **415 passing** (389 before this release). Ruff clean;
  mypy A/B: **765 → 767**, the two added lines being
  `Invalid base class "Base"` / `Variable "app.db.Base" is not valid as a type`
  on the new model — the per-class pair every one of the 26 existing models in
  that file already emits — and zero new errors in any other file: the new
  module, the worker hook, the router and the handler changes are all
  mypy-clean to the baseline's standard. `tsc --noEmit` and `npm run build`
  clean.

### Found but not fixed

Out of scope for this change (recorded with file:line rather than touched):

- `backend/app/services/handlers.py:138` — `handle_discovery` calls
  `discover_for_user` **without `profile=`**, so `use_ai=bool(profile_data)` is
  False and *no* queued discovery run — manual or auto — ever asks the model for
  a verdict: `score_source` stays `preliminary` and the AI company-size pass
  never runs. It is what makes an auto discovery pass cheap today; fixing it
  changes the cost of every discovery trigger. The v2.2 notification says
  "Scored by keyword overlap — open a job for the AI verdict" instead of hiding
  it, and `GET /jobs/{id}/intelligence` really does compute the verdict.
- `backend/app/api/routers/funding.py:207` — the queued `POST /funding/refresh`
  enforces `funding_companies_per_month` and nothing in `handle_funding` ever
  charged it for a manual run (only the synchronous `funding.py:168` path does),
  so a clicked queue scan bypasses the cap. Auto runs now charge it; the manual
  queued path is left as shipped, because closing it changes what a paying user
  hits 429 on.
- `backend/app/services/handlers.py:213` — `handle_funding` never reads the
  `persona_id` its payload carries (`POST /api/funding/refresh` sets it and the
  auto payload mirrors that request), so the scan always runs against the *user's*
  settings: with two personas, the second one's funding focus is not what gets
  scanned. The id is stored on the item and dropped.
- `backend/app/worker.py:134` (`_handle_dead_task`) — a child task that exits *cleanly*
  while the worker is running is respawned with backoff, forever. Auto mode
  avoided it by never spawning the task when the interval is `0` (rather than
  spawning a loop that exits), but the underlying respawn-a-dead-slot behaviour
  is still there for any future optional slot.
- `backend/app/api/routers/jobs.py:412` — `applications_per_month` is charged when
  the user confirms an application, not when one is queued, so a queued
  application nobody confirms consumes no budget. Auto mode's pre-check reads
  that same counter and inherits the gap.
- `backend/app/models/models.py:593` — `Notification.kind` is a free-form
  `String(40)` whose value set lives only in a comment, and that comment
  advertises `automation_failed`, which nothing has ever produced. v2.2 adds two
  more kinds (`funding_match`, `quota_exhausted`) through the same untyped field;
  the SPA renders `n.kind` verbatim so nothing breaks, but there is no contract to
  check a new kind against (the same shape as the `jobs.score_source` note filed in
  2.1.2).
- `frontend/src/pages/Settings.tsx:87` — `save()` builds its payload from
  `_meta.writable` and drops anything else with no signal, which is exactly how
  `general.additional_questions` spent a whole release writing a key no category
  defines. Every field the page renders is writable now (the one that was not is
  removed above), but there is no per-field read-only affordance outside the two
  `disabled` cases, so the next field added to the form without also being added
  to `WRITABLE_KEYS` will silently discard input behind a green "Saved ✓". A
  read-side per-field allowlist is the real fix and is not a ~30-line change.

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

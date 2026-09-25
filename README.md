# JobHunter AI v2.0

**A self-hosted, multi-user job-search platform**: it discovers real job postings, scores them
against your profile, keeps your portal credentials in an encrypted vault, prepares (and, when you
explicitly allow it, performs) applications, and runs compliant cold outreach — with an audit trail
for everything it does on your behalf.

Built with FastAPI + SQLAlchemy 2 + Alembic on the backend and React 18 + Vite + Tailwind on the
frontend. PostgreSQL is the production database; SQLite is supported for a single-user install.

```
┌──────────────────────── SPA (React + Vite) ─────────────────────────┐
│ Dashboard · Jobs · Queues · Resume Studio · Email Bucket · Funding   │
│ Vault · Account & privacy · Settings · Logs                          │
└───────────────▲──────────────────────────────────────────────────────┘
                │ JSON API (JWT / API key) — served by the same process
┌───────────────┴──────────────────────────────────────────────────────┐
│ FastAPI                                                              │
│  middleware: request-id → security headers → rate limit → body limit │
│  routers: auth · account · resumes · jobs · vault · emails · funding  │
│           settings · ops · tracking                                  │
│  pipeline worker: discovery · application · email · funding · ai      │
└───┬──────────┬──────────────┬───────────────┬───────────────┬────────┘
    │          │              │               │               │
 Postgres   real job      AI gateway      encrypted        SMTP +
 + Alembic  sources       (rate-limited,  per-user vault   tracking
            (13 live,     per-workflow)   (Fernet, HKDF)    + suppression
            7 gated)
```

---

## 1. What v2.0 adds over the v1.2 demo

The v1.2 build was a working single-user prototype. v2.0 closes the gaps that block real use:

| Area | v1.2 | v2.0 |
|---|---|---|
| Accounts | single implicit user | **multi-user auth** — bootstrap owner, registration policy, JWT access + rotating refresh tokens, API keys, per-user row scoping on every table |
| Secrets | one process-wide Fernet key | **HKDF-derived per-user keys**, encrypted SMTP credentials, `SECRET_KEY`/`ENCRYPTION_KEY` validation, no hardcoded defaults in production |
| Data | `create_all`, SQLite only | **Alembic migrations**, PostgreSQL + SQLite, legacy v1.2 schema adoption, connection pooling, health/readiness probes |
| Pipelines | in-process `asyncio` tasks lost on restart | **durable queue** with leasing, retries/backoff, dead-letter, `python -m app.worker`, startup recovery of stalled items |
| Job sources | 2 keyless APIs + demo pool | **13 live adapters** (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas, The Muse, WeWorkRemotely) with robots.txt compliance, per-host politeness, caching and honest "unavailable" reporting for gated sources |
| Forms | mocked detection | **real HTML form parsing** (labels, required flags, ATS fingerprints) + optional Playwright autofill that is dry-run by default |
| Resume | approve gate | **diff against the master, polished-version upload, fact guard, per-JD reuse decisions** |
| Email | send or not | **compliance gate** (consent, suppression list, daily limit, postal address, unsubscribe URL), dry-run default, open pixel, unsubscribe endpoint, webhook ingestion |
| Funding | AI-synthesised | **SEC EDGAR Form D** (real), optional Crunchbase/Tracxn/imported feeds, synthetic data only when explicitly opted into and labelled |
| Contacts | AI guess | **Hunter/Apollo + DNS MX verification**, heuristic fallbacks always marked `verified=false` |
| Ops | none | structured JSON logs, Prometheus metrics, `/api/health/{live,ready}`, audit trail, usage counters, backup script, CI, hardened container |

---

## 2. Quick start

### Local development

```bash
# backend
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements-dev.txt -r backend/requirements-autofill.txt
python -m playwright install --with-deps chromium
cp .env.example .env                     # fill SECRET_KEY + ENCRYPTION_KEY (see below)
cd backend && PYTHONPATH=. uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

`./run.sh` also installs Python Playwright and Chromium by default. Use
`WITH_AUTOFILL=0 ./run.sh` for a browser-free install, `WITH_LAYA=0 ./run.sh` to omit the
optional local decision engine (Laya) — which is also warmed with a one-off model download
(`LAYA_WARMUP=0 ./run.sh` keeps Laya but skips the download) — or `AUTO_INSTALL=0 ./run.sh`
to skip all installation.
The frontend npm package is not a substitute for the backend Python package. Installing the
browser does not enable automatic submission; see the Assisted Apply settings below.

Generate the two secrets once:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32)); print('ENCRYPTION_KEY=' + secrets.token_hex(32))"
```

Open http://localhost:5173 — the first screen is the **owner bootstrap** form. The first account
created becomes the owner; later accounts need `ALLOW_REGISTRATION=true` (default: closed).

### Accounts & test logins

Three supported ways in — pick whichever fits:

| Goal | How |
|---|---|
| First account (owner) | Open the app and fill in the bootstrap form, or `POST /api/auth/bootstrap` |
| Extra testers, scripted/CI | `python backend/scripts/create_user.py --email tester@example.com --password 'a-long-passphrase'` |
| Self-service sign-up | Set `ALLOW_REGISTRATION=true` in `.env` (then the SPA shows the register link) |

```bash
# create / reset / promote / list — see --help for everything
python backend/scripts/create_user.py --email you@example.com --password 'correct horse battery' --role owner
python backend/scripts/create_user.py --email you@example.com --password 'new-passphrase-123' --reset   # also revokes sessions
python backend/scripts/create_user.py --email you@example.com --password-file ./pw.txt                  # keep it out of shell history
python backend/scripts/create_user.py --email you@example.com --random-password                          # prints it once
python backend/scripts/create_user.py --list                                                             # who exists?
```

Passwords are checked against the configured policy (`PASSWORD_MIN_LENGTH`, default 10). The first
account created on an empty database is always the owner, whether it comes from the API or the CLI.
Local runs use SQLite at `backend/jobhunter.db` (relative paths are pinned next to the backend
package, so the API, the worker and the CLI all open the *same* database regardless of your shell's
working directory).

Production-shaped stack (PostgreSQL + API + worker):

```bash
cp .env.example .env      # ENVIRONMENT=production, set POSTGRES_PASSWORD, secrets, domain
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml run --rm backup   # on-demand backup
```

Full checklist: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## 3. Feature tour

**Discovery** — keyword context is mined from your resume + preferences (optionally by AI), then
each enabled source is queried concurrently. Every posting is normalised to one shape, deduplicated
(`source:external_id`, falling back to `company:title`), scored — a deterministic pre-rank for all
of them, plus the guardrailed AI verdict for the top 12 on Pro (a free-tier run makes no AI calls
and stays on the labelled estimate; every row carries its `score_source` and the run reports
`ai_rescore`) — classified by company size and persisted with its source, salary and remote flags.
Sources that need a key report *why* they are unavailable instead of silently returning nothing.
A run that adds nothing to the board reports *why* it was empty — `no_sources_configured` (nothing
was attempted), `all_sources_failed` (an outage, with the per-source errors) or `no_fresh_postings`
(healthy sources, nothing inside the freshness window) — plus a `summary` of the counts behind that
verdict; `GET /api/jobs/discovery/last-run` serves it and the Jobs page renders it, so an empty board
is never a reason-less "no fresh jobs".

Every run also feeds the **shared job pool** and reads from it: the postings it scanned are folded into
one cross-user corpus (same canonical identity as the board, so a pool row merges with a live row rather
than duplicating it), and the pool offers back live entries this user does not have yet — coverage the
run's own adapters could not produce, at zero fetch cost. Retention is hard: nothing older than
`JOB_POOL_RETENTION_DAYS` (7 by default) survives, and pruning deletes the posting's *content* —
title, description, URL, payload — leaving only an aggregate row (counters, a SHA-256 identity, the
public company name as the churn dimension) in `job_pool_metrics`. The same route is taken the moment a
posting is reported withdrawn, so "deleted jobs keep aggregated metrics only" is the retention rule, not
a second code path. Those aggregates are owner-only: they ride on `GET /api/admin/overview`
(`job_pool`, rendered as **Admin → Shared job pool**) and nothing on the user surface exposes them.
`JOB_POOL_ENABLED=false` restores the pre-pool behaviour exactly — no reads, no writes, no matching.

Finishing onboarding starts matching immediately: the moment the resume extraction lands (or a review
decision activates the profile), the pool's best current matches are written to the board with
deterministic `preliminary` scores — no network, no provider calls, honestly labelled — and the user's
own live discovery run is queued behind them. The hook is idempotent per (profile, document) and never
fails the onboarding step.

**Applications** — a job's apply flow detects the portal (Greenhouse/Lever/Workday/custom), detects
required fields from the real HTML, maps them from your profile, and either:
* reuses a previously approved tailored resume (JD similarity ≥ 0.85),
* generates a new one that must be approved (fact guard: never invents employers, titles or dates),
* creates/reuses a vault credential for the portal,
* asks **you** for anything it cannot determine (the User-Input queue), then re-queues itself.

Submitting through a browser requires the optional Playwright extra and *two* explicit opt-ins
(`AUTOFILL_ENABLED`, `AUTOFILL_ALLOW_SUBMIT`, plus the automation consent). Without them the run is
dry-run: fields are mapped and a screenshot is stored, but nothing is submitted.

**Outreach** — draft → disclosure → compliance check → send. `POST /api/emails/{id}/send` requires the
`outreach` disclosure (403 `consent_required`, and the SPA offers to record it and retry); the
compliance gate then blocks on missing terms, suppressed recipients, invalid addresses, daily
limits, missing SMTP, missing postal address and missing unsubscribe URL. Both layers are enforced
in code — the second one is what protects worker/queued sends that never touch the route. Real sends are off by default (`EMAIL_SENDING_ENABLED=false`,
`EMAIL_DRY_RUN=true`). Opens are tracked with a 1×1 pixel and unsubscribes are honoured immediately
via a one-click endpoint and the suppression list.

**Funding radar** — real Form D filings from SEC EDGAR (no key needed, descriptive User-Agent
required) plus optional Crunchbase/Tracxn/operator-imported feeds. Companies with open roles go to
the application flow; the rest can be cold-emailed. Synthetic demo companies exist but are opt-in
and labelled `DEMO DATA`.

**Vault** — credentials are encrypted with a key derived per user (`HKDF(master, "user:{id}:vault")`)
and rotated on read from the legacy global key. Reveals, exports and deletions are audited; exports
are available in Chrome and Apple Passwords CSV formats.

---

## 4. API surface (highlights)

| Area | Endpoints |
|---|---|
| Auth | `GET /api/auth/status`, `POST /api/auth/{bootstrap,register,login,refresh,logout}`, `GET /api/auth/me`, `POST /api/auth/password`, `GET/POST/DELETE /api/auth/api-keys` |
| Onboarding | `POST /api/onboarding/session`, `GET /api/onboarding/status`, `GET /api/onboarding/sessions/{id}`, `POST /api/onboarding/resume` (non-blocking upload → background extraction), `POST /api/onboarding/retry` |
| Account | `GET /api/account/{disclosures,consent,audit,export,runtime,billing-usage}`, `POST /api/account/consent`, `DELETE /api/account?confirm=<email>` |
| Resumes | `POST /api/resume/upload`, `GET /api/profile/current`, `POST /api/resumes/generate`, `POST /api/resumes/{id}/{approve,reject}`, `GET /api/resumes/{id}/{preview,diff,download}`, `POST /api/resumes/{id}/polish`, `PUT /api/resumes/{id}/tags` |
| Jobs | `POST /api/jobs/discover`, `GET /api/jobs/discovery/last-run`, `GET /api/jobs`, `POST /api/jobs/{id}/{apply,input,mark-applied,retry,skip}`, `GET /api/user-input-queue`, `POST /api/classify/company`, `GET /api/pipelines/{stats,jobs}` |
| Vault | `GET/POST/DELETE /api/vault`, `GET /api/vault/{id}/reveal`, `GET /api/vault/export/{chrome,apple}` (+ signed `{chrome,apple}/url` for browser downloads) |
| Email | `POST /api/emails/generate`, `GET /api/emails`, `POST /api/emails/{id}/{approve,send,check}`, `GET /api/emails/{id}/events`, `GET/POST/DELETE /api/emails/suppressions` |
| Funding | `GET /api/funding/{companies,providers,context}`, `POST /api/funding/refresh`, `POST /api/funding/{name}/process` |
| Me (user surface) | `GET /api/me/dashboard` — the tenant-scoped aggregate the home page renders (profile completeness, discovery status, top matches with reasons, review/attention queues, applications in progress, interview activity, weekly outcome report, follow-up reminders) — and `GET /api/me/assistant`, the sanitized assistant-availability signal (no provider URL, key, model or usage on it) |
| Settings | `GET/PUT /api/settings` (the `ai` category is owner-only: members get `{"configured": bool}` and cannot write it), `GET /api/settings/ai/status` *(owner)*, `GET /api/settings/runtime` *(owner)*, `GET/POST/DELETE /api/ai/config` *(owner)* |
| Admin (owner only) | `GET /api/admin/overview` (sources, queues, usage & cost, failure rates, automation health, shared job pool, AI-log and Laya rollups), `GET /api/admin/audit`, `GET/PUT /api/admin/flags`, `GET /api/admin/users`, `PUT /api/admin/users/{id}/{plan,role}` — every route 403s non-owners with `owner_required` |
| Admin — AI log (owner only) | `GET /api/admin/ai/calls` (list, filter by workflow/status/user), `GET /api/admin/ai/calls/{id}` (**the exact request body per attempt** + answer excerpt), `GET /api/admin/ai/stats`, `GET /api/admin/laya/decisions`, `GET /api/admin/laya/stats` — rendered at **Admin → AI log** (`/admin/ai`); nothing here is reachable from a user surface |
| Ops | `GET /api/health`, `/api/health/live`, `/api/health/ready`, `/api/metrics`, `/api/meta`, `/api/logs`, `/api/dashboard/summary`, `GET /api/ops/status` *(owner)*, `GET/POST /api/ops/queue/*` *(owner)* |
| Tracking | `GET /api/track/open/{token}.png`, `GET/POST /api/track/unsubscribe/{token}`, `POST /api/track/events` |

Interactive docs: `/api/docs`. Machine-readable spec: `/api/openapi.json`.

---

## 5. Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the annotated list. The
owner-only AI log is `AI_LOG_ENABLED` (on by default; `false` restores the previous behaviour
exactly), pruned to `AI_LOG_RETENTION_DAYS` (7) with bodies clipped to `AI_LOG_PROMPT_CHARS`
(20000) and answers to `AI_LOG_RESPONSE_CHARS` (2000) — diagnostics, not accounting: the AI credit
ledger remains the permanent record. The
values you must set in production are `SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`,
`CORS_ORIGINS`, `ALLOWED_HOSTS` and (for real outreach) the `EMAIL_*` block. The config validator
refuses to boot in `ENVIRONMENT=production` with placeholder secrets, wildcard CORS or SQLite
unless you explicitly override with `ALLOW_SQLITE_IN_PROD=true`.

Per-user settings (AI keys, SMTP, sources, thresholds) live in the app under **Settings** and are
stored encrypted where they are secret.

Discovery is bounded by three knobs an operator can tune without a code change:
`MAX_CONCURRENT_FETCHES` (fan-out width), `DISCOVERY_FETCH_TIMEOUT_SECONDS` (per-source budget
inside one run — a slower source is reported as `timeout` in the run report instead of stalling the
run) and `DISCOVERY_AI_CONCURRENCY` (scoring/classification verdicts in flight; the AI gateway's
`AI_MAX_CONCURRENCY` stays the hard bound). `JOB_POOL_*` sizes the shared pool: retention,
prune batch, ingest cap, how many pool candidates a run is offered, and how many jobs the
onboarding instant match may add.

### Local decision engine (Laya)

Ranking, company-size classification and form-field mapping are *typed decisions* (a rubric
score, a choice among declared options), not writing tasks. They can be answered by **Laya** —
a self-hosted, Apache-2.0 decision model (`pip install -r backend/requirements-laya.txt`, or
`WITH_LAYA=1 ./run.sh`) that returns calibrated typed answers in one forward pass and is
structurally unable to return malformed JSON (the main cause of AI failures in ranking). Anything
that must *write* text (tailored resumes, emails, interview prep) always stays on the AI gateway.

`./run.sh` installs **and warms** Laya by default: `backend/scripts/warm_laya.py` pre-downloads
the `english` + `multilingual` checkpoints into the Hugging Face cache once (a marker skips it on
later starts), so decisions work offline and the first one is instant. `LAYA_WARMUP=0` skips the
download; `WITH_LAYA=0` omits the package. Docker images leave Laya out entirely — build with
`--build-arg WITH_LAYA=1` to bake it in (`LAYA_WARMUP=1`, the default there, also bakes the
weights for fully-offline containers; see `docs/DEPLOYMENT.md`).

Routing is owner-configured in **Admin → Local decision engine (Laya)**:

* **Auto** — Laya first; the AI gateway escalates low-confidence answers, and answers everything
  when Laya is not installed (the pre-Laya behaviour, byte for byte);
* **Laya only** — never fall back to the AI gateway for these decisions; an engine outage is
  reported honestly (the same "pending / unavailable" surfaces an AI outage gets);
* **AI only** — Laya is never consulted.

Per-task opt-ins (ranking / classification / field mapping) sit next to the mode, and
`LAYA_ENABLED=false` is the operator ceiling that turns all routing off. Verdicts answered by
Laya carry their own provenance (`score_source=laya`, its own badge in the UI) — never presented
as an AI verdict.

---

## 6. Testing & quality gates

```bash
# backend — hermetic: temp SQLite, no network, no AI key
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q     # 1419 tests

# lint
ruff check backend

# frontend
cd frontend && npx tsc --noEmit && npm run build
```

CI (`.github/workflows/ci.yml`) runs the suite twice (SQLite **and** PostgreSQL 16), applies
Alembic migrations on a fresh database, typechecks and builds the frontend, builds the production
image and audits dependencies (pip-audit + npm audit).

Test coverage includes: auth/tenancy isolation, vault encryption + re-keying, queue leasing and
worker execution, discovery/source adapters, the shared job pool (retention/deletion aggregates,
the owner-only snapshot, instant match and its idempotency), the owner-only AI log (the body stored
is the body sent, failures with reason/status, the Laya status vocabulary, retention, clipping and
masking, member 403 on every route), form detection, autofill planning, resume generation
+ fact guard + diff/polish, outreach compliance gates, suppression/unsubscribe/open tracking,
funding providers (including the anti-fabrication guard), GDPR export/delete under **enforced** foreign keys
(the schema's FKs are checked, not disabled), metrics and health.

---

## 7. Honest limitations

* **LinkedIn, Indeed, Naukri and Instahyre are not scraped.** They prohibit it in their terms and
  block automation; the app reports them as gated rather than pretending. Workers/partners with a
  licence can add an adapter in `app/services/sources/adapters.py`.
* **Browser submission needs Playwright**, which is an optional extra
  (`backend/requirements-autofill.txt` + `playwright install chromium`). Automating an application
  may violate a portal's terms — the automation consent text says exactly that, and the default is
  dry-run.
* **Funding data is Form D-level.** SEC EDGAR gives you company + filing date + link, but Form D
  filings do not disclose round sizes. Paid providers are wired but need your licence key.
* **Contact data quality depends on your provider key.** Without Hunter/Apollo the app only
  proposes role mailboxes and marks them unverified; it never invents a personal address.
* **Email deliverability is your responsibility.** Configure SPF/DKIM/DMARC for the sending domain;
  the app supplies the unsubscribe header/footer, suppression list and rate limiting.
* **The shared job pool shares public postings, not people.** Entries carry no tenant column; the
  per-user link (`job_pool_seen`) is a normal owned row that account erasure deletes, and the pool's
  owner-facing aggregates are counters. A deployment that must not share postings between tenants sets
  `JOB_POOL_ENABLED=false` and gets the pre-pool product.
* **The AI log is owner-only, bounded and scrubber-first.** It stores the request *bodies* we sent
  (never headers, so never the API key) after the gateway's secret scrubber, clipped and pruned on
  write; a member gets 403 on every `/api/admin/ai/*` route. It is a diagnostic aid for the operator,
  not an export surface — the credit ledger stays the accounting record.
* **Compliance sign-off is still a human step.** `docs/COMPLIANCE.md` documents the built-in
  controls and the questions your counsel should answer before you enable automation for real
  users.

---

## 8. Operations

```bash
# apply migrations / inspect state
cd backend && python -m alembic upgrade head && python -m alembic current

# run the pipeline worker separately from the API
cd backend && PYTHONPATH=. python -m app.worker

# nightly backup (SQLite: online backup + integrity check; Postgres: pg_dump | gzip)
python backend/scripts/backup.py --out /backups --keep 14

# scrape metrics
curl -H "Authorization: Bearer $METRICS_TOKEN" http://localhost:8000/api/metrics
```

Health probes: `/api/health/live` (process up), `/api/health/ready` (database + migrations + queue).
Logs are JSON when `LOG_JSON=true`; every record carries `request_id` (a string token) and
`user_id` (a number) when known.

---

## 9. License & disclaimer

No license file is included in this repository; treat it as proprietary until you add one. The
software automates actions on third-party sites — you are responsible for complying with each
site's terms of service and with the anti-spam and privacy laws that apply where you and your
contacts are located.

### Search-engine job discovery

Optional, privacy-minimized Brave Web Search discovery supplements direct ATS
adapters. Results remain discovery leads until their source pages validate them;
search snippets are never job descriptions. Disabled by default. See
[Search discovery setup and operations](docs/SEARCH_DISCOVERY.md) for provider
selection, storage-rights requirements, shared quotas/caching, and separate
provider cost accounting.

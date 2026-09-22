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
`WITH_AUTOFILL=0 ./run.sh` for a browser-free install, or `AUTO_INSTALL=0 ./run.sh`
to skip all installation. The frontend npm package is not a substitute for the
backend Python package. Installing the browser does not enable automatic submission;
see the Assisted Apply settings below.

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
| Admin (owner only) | `GET /api/admin/overview` (sources, queues, usage & cost, failure rates, automation health), `GET /api/admin/audit`, `GET/PUT /api/admin/flags`, `GET /api/admin/users`, `PUT /api/admin/users/{id}/{plan,role}` — every route 403s non-owners with `owner_required` |
| Ops | `GET /api/health`, `/api/health/live`, `/api/health/ready`, `/api/metrics`, `/api/meta`, `/api/logs`, `/api/dashboard/summary`, `GET /api/ops/status` *(owner)*, `GET/POST /api/ops/queue/*` *(owner)* |
| Tracking | `GET /api/track/open/{token}.png`, `GET/POST /api/track/unsubscribe/{token}`, `POST /api/track/events` |

Interactive docs: `/api/docs`. Machine-readable spec: `/api/openapi.json`.

---

## 5. Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the annotated list. The
values you must set in production are `SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`,
`CORS_ORIGINS`, `ALLOWED_HOSTS` and (for real outreach) the `EMAIL_*` block. The config validator
refuses to boot in `ENVIRONMENT=production` with placeholder secrets, wildcard CORS or SQLite
unless you explicitly override with `ALLOW_SQLITE_IN_PROD=true`.

Per-user settings (AI keys, SMTP, sources, thresholds) live in the app under **Settings** and are
stored encrypted where they are secret.

---

## 6. Testing & quality gates

```bash
# backend — hermetic: temp SQLite, no network, no AI key
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q     # 249 tests

# lint
ruff check backend

# frontend
cd frontend && npx tsc --noEmit && npm run build
```

CI (`.github/workflows/ci.yml`) runs the suite twice (SQLite **and** PostgreSQL 16), applies
Alembic migrations on a fresh database, typechecks and builds the frontend, builds the production
image and audits dependencies (pip-audit + npm audit).

Test coverage includes: auth/tenancy isolation, vault encryption + re-keying, queue leasing and
worker execution, discovery/source adapters, form detection, autofill planning, resume generation
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

# Changelog

All notable changes to JobHunter AI are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

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

- **`backend/scripts/create_user.py`** — create, reset, promote, deactivate and list
  accounts from the command line (the supported way to provision testers without
  opening registration). Documented in the README's "Accounts & test logins".
- **The SPA explains a refused send** — a blocked or consent-gated send now shows
  *why* (compliance blockers, or the exact missing disclosure plus a button that
  records it and retries) instead of silently leaving the email in `queued`.
- 35 regression tests pinning every defect above (`backend/tests/test_regressions.py`).

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

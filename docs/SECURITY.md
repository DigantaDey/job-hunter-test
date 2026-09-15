# Security model

Scope: a self-hosted multi-user deployment where each user stores a resume, a profile, portal
credentials and an outbound email identity. The design assumption is **honest defaults**: the app
never silently performs a risky action, and it refuses to start in an insecure production
configuration.

---

## 1. Authentication

| Mechanism | Details |
|---|---|
| Bootstrap | `POST /api/auth/bootstrap` only works while no user exists; that account is the owner. Repeat calls return 409. |
| Login | `POST /api/auth/login` → short-lived JWT access token (default 30 min) + opaque refresh token (default 30 days). Per-email throttle: 10 attempts/minute. |
| Refresh | Refresh tokens are random 43-char values, stored **hashed** (SHA-256); each use rotates the token and revokes the previous one (replay → 401). |
| Registration | `POST /api/auth/register` is closed unless `ALLOW_REGISTRATION=true`; the owner can invite by enabling it temporarily. |
| API keys | `jh_…` keys are shown once, stored hashed, carry a prefix for identification and can be revoked. Sent as `X-API-Key` or `Authorization: Bearer jh_…`. |
| Passwords | bcrypt (>= 5.0) with a SHA-256 pre-hash so >72-byte passphrases work; PBKDF2-SHA256 fallback if bcrypt is unavailable. Minimum length 10, common-password checks, no silent truncation. |
| Dev escape hatch | `AUTH_REQUIRED=false` binds requests to the first user and is **rejected by the config validator in production**. |

Authorisation is per-row: every data table carries `user_id`, and every query filters on the
authenticated user. Tenancy isolation is covered by tests (`tests/test_auth_and_tenancy.py`).

## 2. Secrets & encryption

* `SECRET_KEY` signs JWTs; `ENCRYPTION_KEY` is the master key for stored secrets. Both are validated
  at startup (length, placeholder detection) and refused in production if unchanged.
* Vault credentials and SMTP passwords are encrypted with **per-user keys** derived via
  `HKDF-SHA256(master, info="user:{id}:vault")` — a leaked row for one user cannot be decrypted with
  another user's key, and rotating the master key only requires re-reading (values written under the
  legacy global key are transparently re-encrypted on read).
* Secrets are never returned by the API: settings expose `password_set`/`api_key_set` booleans, and
  per-workflow AI keys are masked (`api_key: ""`, `api_key_set: true`).
* GDPR export deliberately decrypts vault entries **for the owner only** (`GET /api/account/export`),
  and that action is audited.

## 3. Transport & HTTP hardening

* CORS is an explicit allow-list; wildcard + credentials is impossible.
* `TrustedHostMiddleware` is enabled whenever `ALLOWED_HOSTS` is set (required in production).
* Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, CSP, and HSTS in production.
* Request bodies over `MAX_UPLOAD_MB` (+ overhead) are rejected with 413 — on the declared
  `Content-Length` **and** by counting bytes as they stream in, so a `Transfer-Encoding: chunked`
  request cannot bypass the cap. The 413 is written once, and any later response from the handler is
  dropped rather than appended.
* Rate limiting: sliding window, `429` with `Retry-After`, keyed on a **verified identity** — a
  signature-verified access token → `u:<hmac(user id)>` (one budget per user, wherever they connect
  from), an API key → `k:<hmac(key)>` *plus* the client-IP bucket (the key cannot be verified
  without a DB read, so the IP budget is what stops a junk-key flood) — and on the **resolved client
  IP** for everything else. Keys are HMAC fingerprints, never a slice of a raw header, and the log is
  a hard-capped LRU (`RATE_LIMIT_MAX_KEYS`, 8192) with a periodic sweep: it runs *before*
  authentication, so 10 000 requests with 10 000 unique `Authorization` values cost one bucket, not
  10 000 dict entries (`tests/test_edge_hardening.py`).
* The client IP is only taken from `X-Forwarded-For` when the connecting peer is in `TRUSTED_PROXIES`
  (and uvicorn's own resolver is restricted the same way by `FORWARDED_ALLOW_IPS`, which defaults to
  loopback rather than `*`), and then only up to the **rightmost hop that is not itself a proxy** —
  so a direct client cannot choose the address that rate limits, login-failure attribution and
  `audit_logs.ip` are keyed on. A refused header is counted
  (`jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}`) and warned about; `*`/`0.0.0.0/0`
  in either setting fails production startup. See `docs/DEPLOYMENT.md` §3.1.
* Every response echoes `X-Request-ID`; request ids appear in logs and audit rows.
* TLS itself is terminated by your proxy — see `docs/DEPLOYMENT.md` §3.

## 4. Input handling & SSRF surface

* Uploads are validated by extension **and** content magic (PDF/DOCX only) and size; the stored
  name is reduced to a flat `[A-Za-z0-9._-]` basename (no path traversal, no control characters),
  the final path is asserted to stay inside `UPLOAD_DIR`, and files are served only through
  ownership-checked endpoints. `tests/test_upload_hardening.py` pins this behaviour.
* The app fetches URLs for job postings, funding imports and form detection. Every outbound request
  goes through an **SSRF guard** (`app/services/net_guard.py`) that refuses non-`http(s)` schemes,
  credentials embedded in the URL, `localhost`/`*.local`/`*.internal`/metadata hostnames, internal
  service ports (SSH, SMTP, Postgres, MySQL, Redis, Mongo, Elasticsearch, memcached…), and any host
  whose DNS answers are loopback, private, link-local (incl. `169.254.169.254`), CGNAT, unique-local
  or otherwise non-public. The same check runs on every redirect hop via a guarded transport.
  `OUTBOUND_ALLOWED_HOSTS` allow-lists specific hosts; `OUTBOUND_ALLOW_PRIVATE=true` disables the
  address check for an air-gapped install. Fetches still honour `RESPECT_ROBOTS_TXT`, are
  rate-limited per host, use short timeouts and never send credentials. Treat `FUNDING_IMPORT_URL`
  and ATS board tokens as operator-trusted configuration.
* The **browser** is an outbound client too, and a more dangerous one: it renders, follows
  redirects and can be typed into. `execute_autofill` therefore runs the same policy as a
  **pre-flight** (`net_guard.preflight_navigation`) before Playwright is even launched, in a
  stricter shape: loopback, link-local (`169.254.0.0/16`, the cloud-metadata range) and metadata
  hostnames are refused **even when allow-listed**, and `OUTBOUND_ALLOW_PRIVATE` does not apply to
  navigation at all (a private range is reachable by a browser only when that exact host is in
  `OUTBOUND_ALLOWED_HOSTS`). A refused posting URL is recorded as `status="blocked"` on the job —
  never as an application.
* Autofill is additionally **domain-restricted**. The domains a run may act on come from the job's
  *own* metadata (its ATS portal domain, or the company website on record) — never from the posting
  URL, which is the attacker-supplied value. A posting on a host that metadata does not name is
  refused; a redirect off the posting's own organisation stops the run. **Vault credentials are
  typed only when the page's live host is one of those domains *and* the domain the credential was
  issued for** — including for `password` fields in the form plan, not just the login form. A
  mismatch is reported in the run result (`login.reason`) instead of silently filling.
* Autofill failures are diagnosable without leaking anything: every selector decision is logged at
  `DEBUG` and echoed in the result's `diagnostics` (field name, selector, outcome) — values, and
  passwords in particular, are never logged. `tests/test_autofill_navigation_policy.py` pins all of
  this, including that no secret reaches a log line.
* Residual risk: DNS is resolved by the guard and again by the HTTP client, so a hostile resolver
  could answer differently the second time (DNS rebinding). The window is narrow and the default
  (`OUTBOUND_ALLOW_PRIVATE=false`) means a rebinding answer is refused on the next hop; keep the
  process on a network that cannot reach your cloud metadata service for defence in depth.
* All SQL goes through SQLAlchemy bound parameters; there is no string-built SQL.
* Pydantic validates every request body; validation errors return a truncated, structured 422.
* Errors never leak stack traces to clients (`debug=false` → generic 500 + request id).

## 5. Auditing & retention

`audit_logs` records who did what, when, from which IP and request id. Covered actions include:
login success/failure, password change, API-key create/revoke, consent changes, vault
create/reveal/export/delete, resume upload/generate/approve/delete/polish, job applied,
email generated/sent/blocked, settings updates, account export and account deletion. The table is
append-only for the app; only the GDPR eraser touches it (detaching the user id while keeping the
fact that a deletion happened). Users can read their own trail at `GET /api/account/audit`.

## 6. Abuse limits on outbound behaviour

* Outbound email is off by default and gated at two layers — `POST /api/emails/{id}/send` requires the
  `outreach` disclosure (403 `consent_required`), and `compliance_report()` re-checks consent, terms, suppression,
  syntax/MX, daily limit, postal address, unsubscribe URL, SMTP configuration).
* Contact discovery never behaves like a spam cannon: providers are rate-aware, heuristic guesses
  are labelled unverified, and SMTP probing is deliberately not implemented.
* Scraping respects `robots.txt` and enforces a minimum interval per host.
* Automation is dry-run by default and requires two explicit opt-ins plus a recorded consent:
  `POST /api/jobs/{id}/apply` enforces the `automation` disclosure (403 `consent_required`) on the
  branch that would really submit; dry-run preparation stays available without it.
* The worker is a long-lived process that sweeps thousands of *distinct* URLs and hosts, so every
  in-process cache it keeps is a **bounded LRU** (`app/core/lru.py`), not a dict that only grows:
  the outbound GET cache (`HTTP_CACHE_MAX_ENTRIES`, default 128 — storing status + headers + parsed
  body, never the raw `httpx.Response`, and never a body over `HTTP_CACHE_MAX_BODY_BYTES`), the
  per-host politeness state (`HTTP_HOST_STATE_MAX_ENTRIES`, where a lock a coroutine is holding is
  pinned rather than evicted) and the SSRF guard's DNS verdicts (`DNS_CACHE_MAX_ENTRIES`). Entries,
  evictions and hits are on `GET /api/ops/status` under `outbound`, so a cache that stops behaving
  is visible before it becomes an incident.
* The same rule covers the *edge*, whose keys are attacker-chosen by definition: the rate limiter's
  hit log (`RATE_LIMIT_MAX_KEYS`), the login-throttle windows (4 096 entries, TTL'd — a flood of
  random addresses used to wipe everybody's lockout state), the unmatched-path metric labels (64
  shapes, then `other`) and the metrics registry itself (`METRICS_MAX_SERIES_PER_METRIC` series per
  metric, least-recently-updated evicted first). All four report their shape under `edge` on
  `GET /api/ops/status`.

## 7. Known risks & accepted trade-offs

| Risk | Mitigation / status |
|---|---|
| Automating applications may violate a portal's ToS | disclosed in the consent text, off by default, dry-run default; the operator decides |
| AI provider sees resume/JD text | use a provider you trust or leave `AI_API_KEY` empty (heuristics still work) |
| Open tracking pixels are inaccurate (Gmail prefetch) | documented as directional, not proof of reading |
| In-process rate limiter is per-instance | documented; `WEB_CONCURRENCY` ships at 1 and raising it warns at startup (budgets multiply per process). Use a shared limiter if you scale horizontally |
| Single-node file storage | back up the volumes; object storage is a future item |

## 8. Reporting a vulnerability

Open a private security advisory (or email the maintainer listed in the repository) with repro
steps. Please do not open a public issue for anything exploitable. Target acknowledgement: 3
business days; a fix or mitigation plan within 30 days for confirmed high-severity issues.

## 9. Dependency hygiene

* Runtime pins live in `backend/requirements.txt`. `pip-audit -r backend/requirements.txt --strict`
  reports **no known vulnerabilities** for that set (re-verified 2026-09-11) and the same command
  runs in CI.
* `python-jose` was replaced by `PyJWT` so the unmaintained `ecdsa` package is no longer installed;
  password hashing talks to `bcrypt` directly (passlib is deliberately absent — it is incompatible
  with bcrypt >= 5).
* Frontend: `npm audit` is clean after moving to `vite@7` and `react-router-dom@7`;
  `npm audit --omit=dev` gates production dependencies in CI.
* Upgrade policy: when the audit flags a package it is bumped in the same change, and the full test
  suite must stay green on both SQLite and PostgreSQL (CI enforces both).

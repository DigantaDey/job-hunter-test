# JobHunter AI — Launch Readiness Assessment (v2.0.0)

**Date:** 2026-09-10 · **Reviewers:** backend, frontend, AI, security & compliance
**Repo state:** v2.0.0 on `arena/01a08d09-job-hunter-test`

---

## 1. Executive verdict

**Status: production-ready for a self-hosted deployment (single owner or a small invited team),
with the external-world gaps explicitly gated rather than faked.**

v1.2 was a fully-runnable single-user prototype whose remaining blockers were all "integration
reality" problems. v2.0 closes every blocker that is *engineering*, and puts a hard, auditable gate
in front of everything that depends on the outside world (paid data, portal terms, legal review,
optional browser automation). Nothing in the product claims more than it can prove: sources that
need a key report why, guessed contacts are marked unverified, dry runs are labelled, and synthetic
data is opt-in and watermarked.

The three things a launch still depends on that no codebase can supply by itself:

1. **Your legal review** of the consent texts, terms and outreach basis (`docs/COMPLIANCE.md`).
2. **Paid data/credentials you choose to buy** (contact provider, funding provider, AI key) — the
   adapters are implemented and stay disabled without keys.
3. **Portal permission** for automated submission — the feature is dry-run by default and requires
   an explicit, recorded opt-in (`docs/SECURITY.md` §7).

---

## 2. Blocker-by-blocker resolution (the v1.2 §4 list)

| # | v1.2 blocker | v2.0 status | Evidence |
|---|---|---|---|
| 1 | Real job-board ingestion | **Resolved for licence-friendly sources.** 13 live adapters (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas, The Muse, WeWorkRemotely) with normalisation, dedupe, freshness filter, robots.txt compliance, per-host politeness, caching and per-source error reporting. LinkedIn/Indeed/Naukri/Instahyre are **honestly gated** (reported unavailable with a reason). | `app/services/sources/`, `/api/jobs/discover`, `tests/test_sources_and_forms.py` |
| 2 | Real autofill & submit | **Implemented behind an optional extra.** Real HTML form detection (labels, required flags, ATS fingerprints) + Playwright autofill with field mapping from profile/vault/user answers, screenshot capture and dry-run default. Missing browser → `available: false`, never a fake success. | `app/services/form_detector.py`, `app/services/autofill.py`, `requirements-autofill.txt` |
| 3 | Real identity/contact data | **Provider adapters + honest fallbacks.** Hunter → Apollo → role-mailbox heuristics, DNS MX verification (no SMTP probing), every contact carries `source`/`verified`/`confidence`; heuristic guesses are never presented as verified. Clearbit firmographics when configured. | `app/services/contact_discovery.py`, `tests/test_contact_discovery.py` |
| 4 | Multi-user auth & tenancy | **Done.** Bootstrap owner, JWT access + rotating hashed refresh tokens, API keys, per-user row scoping on all 17 tables, per-user vault key derivation, registration policy. | `app/core/auth.py`, `app/core/security.py`, `tests/test_auth_and_tenancy.py` |
| 5 | Security hardening | **Done.** No hardcoded production secrets (config refuses to boot), request auth everywhere except documented public endpoints, security headers, CORS allow-list, TrustedHost, body limits, rate limiting, structured logs, full audit trail, TLS-ready, outbound SSRF guard on every fetch (private/link-local/metadata addresses and internal ports refused, allow-list override). | `app/core/config.py`, `app/core/middleware.py`, `app/core/audit.py`, `docs/SECURITY.md` |
| 6 | Ops & CI | **Done.** Alembic migrations (fresh DB + legacy v1.2 adoption), PostgreSQL support with pooling, durable queue with leases/retries/dead-letter and a standalone worker, `/api/health/{live,ready}`, Prometheus metrics, JSON logs, backup script with integrity check, CI (SQLite + Postgres + migrations + frontend + docker + dependency audit). | `backend/migrations/`, `app/db.py`, `app/services/job_queue.py`, `app/worker.py`, `.github/workflows/ci.yml`, `backend/scripts/backup.py` |
| 7 | Legal/compliance + consent | **Legal review still external** (deliberate). The engineering side is complete: four recorded consents gating automation/outreach, GDPR export and hard delete, email suppression list, unsubscribe endpoint + header, CAN-SPAM postal-address enforcement, dry-run defaults, retention guidance. | `docs/COMPLIANCE.md`, `app/api/routers/account.py`, `app/api/routers/tracking.py` |

---

## 3. The 23 spec requirements — current state

Legend: ✅ implemented · 🟡 implemented with a documented external dependency · ❌ not applicable

| # | Requirement | Status | Note |
|---|---|---|---|
| 1 | Master resume upload (PDF/DOCX) | ✅ | content-sniffed, size-capped |
| 2 | AI profile extraction | ✅ | AI + robust heuristic fallback |
| 3 | Layout extraction | 🟡 | margins/fonts estimated; colours/operator-level parsing approximate |
| 4 | Keyword-driven live job discovery | ✅ | 13 live sources + freshness + dedupe |
| 5 | Grouped editable settings | ✅ | 8 categories, whitelisted keys, audited |
| 6 | Three pipelines, FIFO, durable | ✅ | DB-backed queue + worker + recovery |
| 7 | Credential vault, Chrome/Apple export, delete forever | ✅ | per-user keys, audited |
| 8 | AI autofill, unknown → user-input queue → re-queue | ✅ | real field detection; Playwright optional |
| 9 | Big-platform auto-submit / external redirect | 🟡 | gated by portal terms; dry-run default |
| 10 | Tailored resume + fact guard + approval + polish upload | ✅ | `POST /api/resumes/{id}/polish`, diff vs master |
| 11 | Auto-tagging, editable | ✅ | |
| 12 | Job-site form structure detection | ✅ | real HTML parsing + ATS fingerprints |
| 13 | Scoring + reuse-vs-generate decision | ✅ | similarity ≥ 0.85 reuse, ≥ 65 generate |
| 14 | Rate-limited AI pipeline + status dot | ✅ | Retry-After, jitter, per-workflow breaker + budget |
| 15 | Company size classification | 🟡 | better with Clearbit; heuristic otherwise |
| 16 | Decision-maker discovery + SMTP send with OTP | ✅ | needs_otp handled; provider keys optional |
| 17 | Funding radar → apply or cold email | ✅ | SEC EDGAR real; paid providers optional |
| 18 | Email approval bucket | ✅ | compliance report before every send |
| 19 | Per-workflow AI configuration | ✅ | persisted + masked |
| 20 | Dashboard + job detail/timeline | ✅ | `job_events` timeline per job |
| 21 | Error log + severity | ✅ | level filters, tenant-scoped |
| 22 | Themes (light/dark/system) | ✅ | |
| 23 | Production operation | ✅ | deploy, backup, monitor, migrate — see docs |

---

## 4. Verification (run these yourself)

```bash
# 1. full backend suite (hermetic; 209 tests, ~45 s)
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q

# 2. lint
ruff check backend

# 3. migrations from scratch
cd backend && DATABASE_URL=sqlite:///./verify.db python -m alembic upgrade head

# 4. frontend typecheck + build
cd frontend && npx tsc --noEmit && npm run build

# 5. boot and probe
cd backend && PYTHONPATH=. uvicorn app.main:app --port 8000 &
curl -s localhost:8000/api/health/ready | python -m json.tool
curl -s localhost:8000/api/meta | python -m json.tool

# 6. backup
python backend/scripts/backup.py --out /tmp/jh-backups --keep 3

# 7. dependency audit (CI job "Dependency audit")
pip-audit -r backend/requirements.txt --strict   # no known vulnerabilities
cd frontend && npm audit --omit=dev              # 0 vulnerabilities
```

Test suite covers: auth/tenancy, vault encryption + re-keying + audit, queue leasing/retries/dead
letters, worker execution, source adapters and degradation, outbound URL policy (SSRF), upload
name sanitisation, form detection, autofill planning,
resume generation + fact guard + diff/polish, outreach compliance gates, suppression/unsubscribe/
open tracking, funding providers (incl. the anti-fabrication guard), contact discovery honesty,
GDPR export/delete, metrics, health, migrations.

---

## 5. Recommended next milestones (post-launch)

| Milestone | Contents |
|---|---|
| M5 — Scale | shared (Redis) rate limiter + queue for multi-replica deployments, object storage for resumes/screenshots, per-user file quotas |
| M6 — Depth | richer ATS adapters (Workday auth flow), Playwright form submission with human-in-the-loop confirmation screens, OCR for scanned resumes |
| M7 — Product | billing/subscription enforcement on top of `/api/account/billing-usage`, email digests, weekly progress reports, team/shared pipelines |
| M8 — Assurance | external penetration test, restore-drill automation, load test at 10× expected concurrency, SOC2-style control documentation |

The shipped default for any risky automation stays **off, dry-run, and disclosed** until the operator
deliberately turns it on — and that choice is recorded in the audit trail.

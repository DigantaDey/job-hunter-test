# JobHunter v2.0 — Full Feature Audit (2026-09-11)

> Generated as part of production & monetization upgrade. Every existing feature is inventoried and assessed.

## 1. Architecture Overview

- **Backend**: FastAPI + SQLAlchemy 2 + Alembic, Pydantic v2, PostgreSQL (prod) / SQLite (dev), Uvicorn
- **Frontend**: React 18 + Vite + Tailwind + React Router + Recharts + Lucide
- **Worker**: durable queue `pipeline_jobs` with leasing, retries, dead-letter, stall recovery; in-process or standalone `python -m app.worker`
- **AI**: unified OpenAI-compatible gateway (`ai_client`) with global rate limiter, circuit breaker, retries, budget guard, per-workflow overrides
- **Auth**: JWT access (30m) + rotating refresh (30d) hashed at rest, API keys `jh_` hashed, bcrypt-SHA256 passwords, per-email throttle
- **Security**: per-user vault encryption via HKDF, CORS allow-list, TrustedHost, security headers, body limit, SSRF guard, robots.txt respect, audit logs

## 2. Feature Inventory

| # | Feature | Location | Current Status | Problems | Production Fix | Monetization Opportunity |
|---|---------|----------|----------------|----------|----------------|--------------------------|
| 1 | Authentication (bootstrap, register, login, refresh, logout, password change) | `api/routers/auth.py`, `core/auth.py`, `core/security.py` | Implemented, tested | In-process brute-force map not shared across replicas; no 2FA | Add Redis-backed throttle for multi-replica, add TOTP optional | Free: full auth; Paid: SSO, 2FA, session management |
| 2 | User Accounts & RBAC | `models.User`, `api/routers/account.py` | Implemented (owner/member) | Role only owner/member, no granular RBAC | Keep simple, add entitlement layer | Free: 1 user; Pro: team seats (future) |
| 3 | Resume Management (upload PDF/DOCX, storage, tags, status) | `api/routers/resumes.py`, `services/resume_parser.py` | Implemented | File storage local only, no object storage abstraction | Add S3 abstraction + local fallback, virus scan hook | Free: 3 resumes; Pro: unlimited |
| 4 | Resume Parsing (pdfminer + pypdf + python-docx, heuristic + AI) | `services/resume_parser.py`, `services/ai_client.py` | Implemented with fallback | Heuristic limited, AI prompt not versioned | Add prompt versioning, structured output validation | Free: 10 parses/mo; Pro: 100; Pro+: 1000 |
| 5 | Profile Extraction | `services/resume_parser.py`, `models.Profile` | Implemented | Single profile per user, no versioning | Add profile versions | Same as resume |
| 6 | AI Features (keyword extract, scoring, resume gen, classify, email gen, form detect, funding scan) | `services/ai_client.py`, `services/ai_pipeline.py`, `services/keyword_extractor.py`, `classifier.py` | Implemented, rate-limited, breaker | No per-user token tracking, no cost ceiling per user, prompt injection risk | Add AI credit ledger, per-user limits, prompt sanitization, structured outputs | Core monetization: credits |
| 7 | Job Discovery (13 live adapters + demo pool) | `services/sources/adapters.py`, `services/discovery.py` | 13 live sources, tested | Some sources gated need keys, no scheduler UI | Add scheduled discovery toggle, per-source enable/disable persisted | Free: 100 jobs/mo; Pro: 1000; Pro+: 5000 |
| 8 | Job Aggregation / Normalization | `services/sources/base.py`, `Posting` dataclass | Implemented | Normalization simple | Improve location/salary normalization | Same as discovery |
| 9 | Job Deduplication | `models.Job.dedupe_key`, `discovery.py` | Implemented (source:external_id fallback company:title) | Dedupe key collision possible | Add content hash fallback | N/A |
| 10 | Job Scoring / Matching | `services/scoring.py` | Heuristic + AI, 0-100 score + reason | No breakdown (skills, exp, etc), no transparent intelligence | Implement detailed breakdown per spec | Free: basic score; Pro: advanced intelligence |
| 11 | Job Recommendations / Priority | `ops.py dashboard summary` | Basic high-match count | No recommendation engine | Add recommendation: HIGH/MEDIUM/LOW with why | Pro feature |
| 12 | Application Tracking (status: discovered, queued, needs_input, applying, applied, failed, etc) | `models.Job`, `services/apply_flow.py` | Implemented | Status transitions not state-machine validated | Add state machine + events | Free: unlimited tracking |
| 13 | Application Automation (Prepare → Review → Confirm → Execute) | `services/apply_flow.py`, `services/autofill.py`, `services/form_detector.py` | Dry-run default, Playwright optional, vault auto-create, user-input queue | Browser automation requires manual Playwright install, no screenshot audit in UI | Add screenshots to job events, progress reporting, cancellation | Free: 5 automations/mo; Pro: 50; Pro+: 200 |
| 14 | Browser Automation (autofill) | `services/autofill.py` | Implemented, dry-run by default, consent-gated | No audit trail in UI | Expose autofill plan + screenshots | Premium tier |
| 15 | Outreach / Email (draft, approve, compliance, send, tracking) | `api/routers/emails.py`, `services/outreach.py`, `services/email_pipeline.py` | Implemented with compliance gate (consent, suppression, daily limit, postal, unsubscribe) | Email sending shared infra, no per-user SMTP rate isolation | Enforce per-user daily limits + provider integration | Free: 10/mo; Pro: 100; Pro+: 500 |
| 16 | Email Functionality (SMTP, open pixel, unsubscribe, webhook) | `services/outreach.py`, `api/routers/tracking.py` | Implemented | No bounce handling UI | Add bounce dashboard | Same |
| 17 | Recruiter/Company Functionality (contact discovery) | `services/contact_discovery.py` | Hunter/Apollo + DNS MX + heuristic | Heuristic marked unverified, but no verification UI | Add verified badge + source transparency | Pro: verified contacts |
| 18 | Notifications (job alerts, reminders) | Missing dedicated system, only logs | Not implemented as service | No notification center | Add notification model + preferences + UI | Free: basic; Pro: advanced alerts, weekly summaries |
| 19 | Background Workers / Queues | `services/job_queue.py`, `worker.py`, `services/handlers.py` | Durable queue with leasing, retries, dead-letter | In-process rate limiter not shared | Add Redis option for multi-replica | N/A infra |
| 20 | Retry Systems / Dead-letter | `job_queue.py` fail() + recover_stalled | Implemented | No UI to retry dead | Add Ops retry-dead (exists for owner) + per-user retry | N/A |
| 21 | Credential Vault (per-user HKDF encryption) | `services/vault.py`, `api/routers/vault.py` | Implemented, rotation on read | No key rotation API, no expiry | Add rotation endpoint, audit | Free: 10 creds; Pro: unlimited |
| 22 | Encryption (Fernet per-user, master key) | `core/security.py` | Implemented | No KMS integration | Document rotation procedure, add KMS abstraction | N/A |
| 23 | User Preferences / Settings | `services/user_settings.py`, `api/routers/settings_api.py` | Implemented (AI workflows, app prefs) | No validation for some keys | Add schema validation | N/A |
| 24 | Analytics / Dashboards | `api/routers/ops.py` dashboard/summary, frontend Dashboard.tsx | Basic counts | No interview rate, no performance analytics | Add application intelligence analytics | Free: basic; Pro: advanced analytics |
| 25 | Database (SQLAlchemy, Alembic) | `db.py`, `migrations/` | PostgreSQL + SQLite, migrations work | No connection pooling tuning per env | Tune pool, add read replica support | N/A |
| 26 | API (FastAPI, 9 routers) | `api/routes.py` | Implemented | No versioning | Add /api/v1 prefix alias | N/A |
| 27 | Frontend (React SPA, 10 pages) | `frontend/src/` | Implemented | No landing page, no pricing, no subscription UI, dashboard not command center | Add pricing, billing, analytics, improved dashboard | N/A |
| 28 | Docker Deployment | `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml` | Implemented | No backup cron, no healthcheck in compose | Add healthcheck, backup service | N/A |
| 29 | Monitoring (structured logs, Prometheus metrics) | `core/logging.py`, `core/metrics.py`, `metrics_server.py` | Implemented | Metrics token optional | Require token in prod (already validated) | N/A |
| 30 | Health Checks | `api/routers/ops.py` /health/live/ready | Implemented | No dependency checks for SMTP/AI | Add optional checks | N/A |
| 31 | CI/CD | `.github/workflows/ci.yml` | Backend SQLite + PG, migrations, frontend build, docker, security audit | Security audit continue-on-error | Make fail on high severity | N/A |
| 32 | Funding Radar (SEC EDGAR + optional Crunchbase/Tracxn) | `services/funding_radar.py`, `services/funding_sources.py`, `api/routers/funding.py` | Real EDGAR, demo opt-in | No scheduler UI | Add auto-scan schedule | Free: 20 companies/mo; Pro: 200 |
| 33 | Interview Preparation | Not present as dedicated feature | Missing | Users expect interview prep | Add interview prep service (AI Q&A) | Pro+ feature |
| 34 | Privacy & Data Control (export, delete) | `api/routers/account.py` export/delete | Implemented | No retention policy doc | Add retention settings | N/A |
| 35 | Consent & Compliance | `account.py` disclosures | Implemented (terms, privacy, automation, outreach) | No versioning | Add version tracking | N/A |

## 3. Security Audit Summary

**Strengths**:
- Per-row tenant isolation, tested
- Vault per-user HKDF encryption, not global
- Passwords bcrypt-SHA256, 72-byte limit handled
- Refresh tokens hashed, rotating, one-shot
- API keys hashed, prefix lookup, scopes
- Upload hardening: extension + magic, basename sanitization, path containment
- SSRF guard with DNS private IP checks, redirect guard, blocked ports
- CORS allow-list, no wildcard+credentials
- Security headers (CSP, HSTS prod, nosniff, etc)
- Rate limiting per credential
- Audit trail for security actions
- Secrets never returned via API (masked booleans)
- Body size limit
- No string SQL

**Gaps**:
- No entitlement/usage limit enforcement (abuse risk)
- No per-user AI cost ceiling
- No per-user daily email limit enforced at DB level (only in-memory check?)
- In-process rate limiter not shared across replicas (documented, but need Redis option)
- No 2FA
- No webhook idempotency for billing (will add)
- No virus scan for uploads (add hook)
- No CSP nonce for inline scripts (SPA static, low risk)
- No account lockout after repeated failures beyond throttle (throttle exists)
- No breach detection (add suspicious activity log)

## 4. Monetization Gaps (Critical for SaaS)

- No Plan/Subscription model
- No entitlement centralization (scattered if checks)
- No AI credit ledger
- No usage counters with monthly reset
- No billing provider integration (Stripe/Razorpay)
- No pricing page
- No upgrade triggers in UI
- No trial/grace period
- No invoice/billing history

## 5. UX Gaps

- Dashboard not command center (missing automation, outreach, AI usage, performance)
- No progressive disclosure (beginner vs advanced)
- No onboarding flow
- No landing page positioning as "command center"
- Job scoring UI shows only score + reason, not breakdown
- No application intelligence analytics
- No interview prep
- No notification center

## 6. Reliability Gaps

- Worker crashes handled (lease expiry), but no alerting
- No structured retry for AI timeouts (exists in ai_client)
- No duplicate webhook handling (will add)
- No queue monitoring UI beyond stats

## 7. Current State Classification

**Beta** — feature-complete prototype with production-grade security foundations, durable queues, real job sources, but missing monetization, advanced analytics, and SaaS polish. Not yet ready to charge users.

## 8. Feature Preservation Confirmation

All 35 inventoried features are to be preserved. No removal. Improvements only.

- Automation stays core, but with Prepare→Review→Confirm→Execute and consent gates.
- AI stays core, with credit system, caching, validation.
- Resume customization stays, with fact guard.
- Outreach stays, with compliance.
- Vault stays, with encryption.
- Funding radar stays.
- Etc.

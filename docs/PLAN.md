# JobHunter — Production & Monetization Plan (P0 → P3)

## P0 — Security / Data-loss / Critical Production Issues (Must fix before any launch)

### P0-1: Central Entitlement & Abuse Prevention
- Create `app/core/entitlements.py` with capabilities: can_analyze_job, can_generate_resume, can_generate_cover_letter, can_run_automation, can_send_outreach, can_use_advanced_matching, can_access_analytics, etc.
- Create `app/models/billing.py` or extend models.py with Subscription, Plan, UsageCounter, AICreditLedger, BillingEvent, Notification
- Implement per-user rate limiting + quota enforcement in deps
- Add abuse detection: suspicious activity logging, account throttling
- **Files**: `backend/app/core/entitlements.py`, `backend/app/models/models.py`, `backend/app/api/deps.py`, `backend/app/core/rate_limiter.py`

### P0-2: AI Credit System & Cost Control
- Track every AI operation: workflow, model, prompt tokens estimate, completion tokens, cost, user, timestamp, success/failure
- Implement per-user daily/monthly limits, cost ceilings, retry limits
- Add budget guard per user (not just global)
- Prevent huge bills via pre-check
- **Files**: `backend/app/services/ai_client.py`, `backend/app/models/models.py`, `backend/app/core/entitlements.py`

### P0-3: Multi-User Isolation Regression Tests
- Add automated tests for cross-user access: resumes, jobs, applications, credentials, AI results, outreach, analytics, subscriptions, files
- Existing `test_auth_and_tenancy.py` covers basics, extend
- **Files**: `backend/tests/test_tenancy_extended.py`

### P0-4: Credential Vault Hardening
- Audit encryption at rest, key derivation, rotation, deletion
- Ensure no secrets in logs, API responses, frontend state, error messages
- Add audit logging for reveal/export/delete (already exists, verify)
- **Files**: `backend/app/services/vault.py`, `backend/app/core/security.py`

### P0-5: Input Validation & SSRF Hardening Verification
- Verify all upload endpoints use hardened path checks
- Verify SSRF guard on all outbound fetches
- Add virus scan hook (stub)
- **Files**: `backend/app/services/net_guard.py`, `backend/app/api/routers/resumes.py`

### P0-6: Database & Concurrency Audit
- Verify indexes, constraints, transactions, foreign keys
- Check race conditions: application automation, AI credits, subscription changes, job deduplication, webhook processing
- Add SELECT FOR UPDATE for credit deduction
- **Files**: `backend/app/db.py`, `backend/app/models/models.py`, `backend/app/services/job_queue.py`

---

## P1 — Required Before Charging Users

### P1-1: Subscription System Architecture
- Define Plans: Free, Pro, Pro+ with limits
- Models: Plan (static config), Subscription (user_id, plan, status, trial_end, current_period_start/end, cancel_at_period_end, provider, provider_customer_id, provider_subscription_id)
- Support signup, trial, upgrade, downgrade, cancellation, renewal, failed payment, grace period, webhook idempotency
- Payment provider abstraction: `app/services/billing.py` with Stripe and Razorpay adapters (interface same)
- Webhooks: `/api/billing/webhooks/stripe`, `/api/billing/webhooks/razorpay` with idempotency via BillingEvent dedupe
- **Files**: `backend/app/models/models.py`, `backend/app/services/billing.py`, `backend/app/api/routers/billing.py`, `backend/migrations/versions/*_billing.py`

### P1-2: Entitlement Enforcement Everywhere
- Replace scattered `if user.plan == "pro"` with `entitlements.check(user, capability)`
- Enforce in: job discovery, AI matching, resume tailoring, automation runs, outreach, analytics, funding radar
- Return 402 Payment Required or 429 with upgrade hint when limit hit
- **Files**: `backend/app/core/entitlements.py`, all routers

### P1-3: AI Credit Ledger & Usage API
- Endpoints: GET /api/billing/usage, GET /api/billing/credits, GET /api/account/billing-usage (extend)
- Track tokens, cost, operation
- Show remaining credits in dashboard
- **Files**: `backend/app/api/routers/billing.py`, `backend/app/api/routers/account.py`

### P1-4: Job Matching Intelligence Upgrade
- Keep existing heuristic + AI scoring
- Add transparent breakdown: Overall 87/100, Skills 92, Experience 84, Seniority 90, Location 100, Salary 88, Education 80, Strong matches, Missing/weak, Recommendation HIGH/MEDIUM/LOW with why
- Treat external job data as untrusted (sanitize before AI prompt)
- **Files**: `backend/app/services/scoring.py`, `frontend/src/pages/Jobs.tsx`

### P1-5: Dashboard as Command Center
- Show: Jobs (new matches, high-priority, saved), Applications (prepared, submitted, interviews, rejected, offers), Automation (running, completed, failed), Outreach (sent, replies, follow-ups), AI usage (credits used, remaining), Performance (applications, interviews, response rate)
- **Files**: `frontend/src/pages/Dashboard.tsx`, `backend/app/api/routers/ops.py`

### P1-6: Application Intelligence Analytics
- Calculate: total applications, interviews, interview rate, per-role performance, strongest/weakest skills
- Endpoint: GET /api/analytics/performance
- **Files**: `backend/app/api/routers/analytics.py`, `frontend/src/pages/Dashboard.tsx`

### P1-7: Resume Customization Guard
- Ensure fact guard: never invent jobs, education, certs, skills, achievements, metrics
- Grounded in user-provided info
- **Files**: `backend/app/services/resume_generator.py`

### P1-8: Application Automation Reliability
- Reliability: retries, session handling, timeouts, screenshots/logs, error recovery, cancellation, progress reporting, user confirmation, audit history
- UI: show Application #182 Status Preparing Step Filling experience Next Review & submit; on fail: paused manual action required
- **Files**: `backend/app/services/apply_flow.py`, `backend/app/services/autofill.py`, `frontend/src/pages/Jobs.tsx`, `frontend/src/pages/Queues.tsx`

### P1-9: Outreach Improvements
- Templates, personalization, recruiter targeting, consent tracking, suppression, unsubscribe, sending limits, bounce handling, retry, provider integrations (Gmail/Outlook/SMTP)
- **Files**: `backend/app/services/outreach.py`, `backend/app/api/routers/emails.py`

### P1-10: Landing Page & Pricing
- Position as "Your complete AI-powered job hunting command center" Discover → Analyze → Prepare → Apply → Automate → Reach Out → Track → Improve
- Pricing page with Free/Pro/Pro+ comparison
- **Files**: `frontend/src/pages/Landing.tsx`, `frontend/src/pages/Pricing.tsx`, `frontend/src/App.tsx`

### P1-11: Testing for Monetization
- Tests: auth, authz, isolation, resume processing, job ingestion, scoring, dedup, AI generation, tracking, automation, outreach, notifications, subscriptions, usage limits, payment webhooks, encryption, file uploads
- **Files**: `backend/tests/test_billing_and_entitlements.py`, etc.

---

## P2 — UX / Conversion / Reliability Improvements

### P2-1: UX Progressive Disclosure
- Beginner: "Find jobs" simple; Advanced: configure sources, scoring, automation, outreach, filters, workflows
- Use tabs, dashboards, workflows, onboarding, contextual actions, sensible defaults
- **Files**: `frontend/src/pages/Jobs.tsx`, `frontend/src/components/Layout.tsx`, `frontend/src/pages/Settings.tsx`

### P2-2: Notifications System
- Model: Notification (user_id, kind, title, body, read, created_at)
- Support: high-match alerts, reminders, automation failures, replies, weekly summaries
- Granular controls
- **Files**: `backend/app/models/models.py`, `backend/app/api/routers/notifications.py`, `frontend/src/pages/Notifications.tsx`

### P2-3: Interview Preparation
- New feature: AI-generated interview questions per job, answer suggestions grounded in resume, mock sessions
- Preserve as premium: Pro+ unlimited
- **Files**: `backend/app/services/interview_prep.py`, `backend/app/api/routers/interview.py`, `frontend/src/pages/Interview.tsx`

### P2-4: Company Intelligence
- Deeper research per company (funding, size, tech stack, culture)
- **Files**: `backend/app/services/company_intel.py`

### P2-5: Reliability & Observability
- Structured logging, metrics, health checks, worker monitoring, queue monitoring, error tracking
- Never log passwords, tokens, etc.
- **Files**: `backend/app/core/logging.py`, `backend/app/core/metrics.py`

### P2-6: Privacy & Data Control
- Account deletion, data deletion, data export, credential deletion, retention policy UI
- **Files**: `frontend/src/pages/Account.tsx`, docs

### P2-7: CI/CD Hardening
- Fail build on high-severity vulnerabilities
- **Files**: `.github/workflows/ci.yml`

---

## P3 — Growth / Optimization

### P3-1: Analytics Tracking
- Acquisition, activation, engagement, monetization, outcomes
- **Files**: `backend/app/services/analytics.py`, `frontend/src/lib/analytics.ts`

### P3-2: Prevent Abuse Advanced
- ML-based abuse detection, account throttling, suspicious activity
- **Files**: `backend/app/core/abuse.py`

### P3-3: Production Deployment Docs
- Verify clean install from zero: PostgreSQL, workers, migrations, secrets, backups, restore, TLS, health checks, logging, monitoring, scaling, restart recovery
- **Files**: `docs/DEPLOYMENT.md`, `docs/LAUNCH_READINESS.md`

### P3-4: Pricing Strategy Finalization
- Calculate revenue per user vs AI+infra+email+automation cost
- Recommend pricing India vs International
- **Files**: `docs/PRICING.md`

---

## Implementation Order

1. Create entitlement system + billing models + migration
2. Implement AI credit ledger
3. Enhance scoring with breakdown
4. Create billing router + webhooks + usage endpoints
5. Enforce entitlements in existing routers
6. Frontend: pricing, billing, dashboard, analytics
7. Interview prep + notifications
8. Tests
9. Docs: security score, production readiness scores, final report

## Success Criteria

- Full feature set preserved, no removals
- Central entitlement system, no scattered plan checks
- AI credit system with tracking, limits, cost ceilings
- Subscription architecture with trial, upgrade, downgrade, cancellation, webhooks, idempotency
- Dashboard as command center
- Job intelligence transparent breakdown
- Application automation reliable, auditable, cancellable
- Security audit passed, multi-user isolation proven
- Can answer: Can JobHunter safely be launched to real users and start charging money? With launch checklist.

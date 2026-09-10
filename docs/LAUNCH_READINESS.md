# JobHunter AI — Launch Readiness Assessment

**Date:** 2026-09-10 · **Reviewer roles:** Product Manager, Full-stack Engineer, AI Engineer, UI/UX Designer
**Repo state:** `main` eac48da → `arena/01a08cf2-job-hunter-test` (v1.2.0)

---

## 1. Executive verdict

**Status: NOT launch-ready as a production SaaS — but a strong, coherent, fully-runnable v1.2 proof-of-concept with a correct architecture.**

The codebase is well-structured (FastAPI + SQLAlchemy 2.0 + Pydantic, React + Vite + Tailwind), fully typed, tested (18 passing tests), and every one of the *23 flows* in the original spec has a working vertical slice in the UI + API. The three-pipeline FIFO model, the AI rate limiter, the vault, the email approval bucket, resume tailoring with a JD fact-guard, scoring, company classification, and the funding radar all genuinely work end-to-end.

The gap between "demo" and "launch" is **not** architecture — it is *integration reality*: real job-board scraping, real browser autofill (Playwright), real email discovery (Hunter/Clearbit-class), OAuth session handling, multi-user auth, and legal/compliance review. Those are hard, external-world problems that no amount of app scaffolding substitutes for.

This pass made the **AI layer honest and correct** (a single rate-limited, per-workflow-configurable gateway now carries *every* AI call), added a **live job source** with graceful offline fallback, added the **resume approval gate** the spec asked for, fixed CORS/Docker/env issues, and made the test suite hermetic.

---

## 2. Requirement-by-requirement mapping

Legend: ✅ implemented · 🟡 partial / simulated · ❌ missing

| # | Requirement (from spec) | Status | Implementation & evidence | Launch blocker |
|---|---|---|---|---|
| 1 | Master resume upload (PDF/DOCX) | ✅ | `POST /api/resume/upload`; `pdfminer`+`pypdf` text, `python-docx` for DOCX | — |
| 2 | AI extracts all profile details | ✅ | `ai_extract_profile()` → OpenAI-compatible JSON; heuristic fallback | needs a stronger heuristic name/title parser |
| 3 | Extract resume layout (margins/lines/caps/hyperlinks/bullets/borders/colors/fonts) | 🟡 | `extract_layout()` returns a structured layout JSON; margins/borders/colors are *estimated*, fonts are partially real | deep layout fidelity (color/border parsing) is approximate |
| 4 | Scrape jobs via internet using extracted keywords + freshness | 🟡 | **NEW** live keyless APIs (Arbeitnow, Remotive) merged with a curated+synthetic pool; freshness filter; adapter pattern for linkedin/naukri/indeed/lever/greenhouse/workday | ❌ LinkedIn/Naukri/Indeed/Workday have **no real adapter** (ToS/anti-bot). This is the single biggest blocker |
| 5 | Editable settings, similar categories clubbed | ✅ | `GET/PUT /api/settings` grouped into `ai/scraping/general/application/email/funding/workflows`; full UI | — |
| 6 | 3 pipelines (discovery / application / AI), FIFO, parallel | ✅ | `PipelineJob` table + in-memory priority queue + `asyncio.create_task` workers + shared rate limiter; `/api/pipelines/stats` | application "submit" is simulated (see #8) |
| 7 | Auto-create sign-in credentials → vault → Chrome/Apple import → delete forever | ✅ | Fernet-encrypted `VaultEntry`, per-domain reuse, Chrome+Apple CSV export, `DELETE /api/vault` | real account creation on Workday/Lever is not automated |
| 8 | Auto-fill profile via AI; unknown → User Input Needed queue → re-queue on submit | 🟡 | Form structure detection, missing-field detection, `UserInputRequest`, `POST /jobs/{id}/input` re-queues and completes. Autofill itself is a *deterministic simulation* | ❌ needs Playwright for real DOM filling |
| 9 | Big platforms (LinkedIn/Naukri/Indeed/Instahyre) auto-submit; external redirect → credential+profile workflow | 🟡 | Source detection + `is_external` branch models the redirect logic and logs the switch | ❌ no real browser automation/OAuth; legal review required |
| 10 | AI resume generator with memory + JD fact-guard; keep or replace skeleton; DOCX/PDF download; upload polished version; approve before upload | 🟡 | `jd_fact_guard_prompt` (never-invent rule), `strict_skeleton` toggle, `build_docx/build_pdf`, download. **NEW**: generated resumes now start `pending` and must be approved (`POST /resumes/{id}/approve`) before the pipeline uses them. "Upload a polished version" is not yet wired as a first-class resume | polish-upload UI + diff preview |
| 11 | Auto-tag generated resumes, editable later | ✅ | AI `tags` + heuristic fallback; `PUT /resumes/{id}/tags` | — |
| 12 | AI finds job-site structure/forms to fill accurately | 🟡 | `detect_form_structure()` returns portal type + fields; AI confidence is mocked | ❌ real HTML+LLM/vision parsing of live ATS forms |
| 13 | Scoring (with/without AI) for profile↔JD + decide reuse vs new resume | ✅ | Hybrid `heuristic_score` (TF-IDF cosine + coverage, tokenizer bug fixed) + `ai_score`; **NEW** `_choose_resume()` reuses an approved generated resume when JD-similarity ≥ 0.85 and score ≥ 65, else generates new or falls back to master | calibration tuning with real data |
| 14 | Separate AI request pipeline, prioritized, rate-limited, never hit RPM; green/red dot | ✅ | **NEW** unified `ai_client` gateway: *every* AI call now acquires a rate-limiter token; per-workflow overrides; `/api/settings/ai/status` pings the endpoint; UI polls the dot | burst behavior + provider 429 backoff |
| 15 | Classify company big/medium/small/startup with high accuracy | 🟡 | `heuristic_company_size` + `ai_company_size` | needs a firmographic data source for real accuracy |
| 16 | Email pipeline: find decision maker, email via user's SMTP with 2FA/OTP | 🟡 | `find_decision_maker` (AI + heuristic alias), `send_via_smtp` real `smtplib` + `needs_otp` branch, inline OTP retry UI | ❌ real contact discovery (Hunter/people-data API); Gmail OAuth/App-password UX |
| 17 | Funding radar (Seed→Series D), open roles → apply, else cold-email founder | 🟡 | AI-context-driven scan, freshness window, DB upsert+prune, `process` branches to apply-flow or founder draft. AI mode intentionally returns *synthetic* companies to avoid fabricating real funding claims | ❌ real funding data (Crunchbase/Tracxn) required for production value |
| 18 | Email approval bucket: edit/cut/copy/paste, AI-assisted, queued on approve | ✅ | `Email` status machine (draft→pending_approval→queued→sent/failed/needs_otp), editable subject/body/to | — |
| 19 | Per-workflow AI API (different API per workflow, single default) | ✅ | **NEW** functional per-workflow editor in Settings + `PUT /api/ai/config` + runtime registry sync | — |
| 20 | Clean dashboard; click any job → all details & states | ✅ | Dashboard cards + Jobs drawer (score, reason, forms, vault, resume choice, apply) | — |
| 21 | Error log maintained + accessible via dashboard | ✅ | `ErrorLog` table, `log_error/log_info`, `/api/logs`, Logs page | structured log levels/severity |
| 22 | Minimalist dashboard; dark/light/system theme | ✅ | `ThemeProvider`, light/dark/system, `prefers-color-scheme`, localStorage | — |

---

## 3. What this pass changed (v1.1.0 → v1.2.0)

1. **Unified, rate-limited AI gateway** (`services/ai_client.py`). Previously most AI calls hit `httpx` directly and *bypassed* the rate limiter — the "never hit RPM" guarantee was false. Now every AI call resolves its per-workflow config and acquires a limiter token first. All 7 AI-calling services refactored onto it.
2. **Per-workflow AI overrides are now real** — persisted in DB, mirrored into the runtime registry at startup and on save, and editable in the Settings UI (was a demo note only).
3. **Live job discovery** (Arbeitnow + Remotive, keyless, ToS-respecting) merged with the curated pool, with a user toggle (`scraping.live_enabled`) and graceful offline fallback.
4. **Resume approval gate** — generated resumes are `pending` until approved; the apply pipeline only auto-uses `approved` generated resumes or the master.
5. **Resume reuse decision** implemented for real: `_choose_resume()` uses JD↔JD cosine similarity + score to reuse an approved resume, generate new, or fall back to master.
6. **Bug fixes:** CORS `allow_credentials` + wildcard conflict; Docker frontend→backend proxy (`backend:8000`); `.env.example` placeholder key removed; `_guess_industry` "ai"⊂"chain" false positive; `process_funding_company` duplicate-Job creation; a TS implicit-any error.
7. **Test hardening:** hermetic per-run temp DB (`tests/conftest.py`), 6 new tests (AI client, live normalizer, reuse decision, approval flow) — 18 total, all green; `tsc --noEmit` clean.
8. **UX polish:** inline OTP input (replaces `prompt()`), approve buttons + status chips in Resume Studio, resume decision surfaced in Jobs, version string bumped.

---

## 4. What still blocks a real launch (ranked)

1. **Real job-board ingestion.** LinkedIn/Naukri/Indeed/Workday/Instahyre have no working adapter, and scraping them raises ToS + anti-bot + account-ban issues. Production path: official/partner APIs where available (LinkedIn's partner programs, Naukri's API), and a user-consented browser session (Playwright) otherwise.
2. **Real autofill & submit.** Current application is a deterministic simulation. Needs Playwright + per-ATS form schemas + CAPTCHA/2FA handling and careful rate/ethos controls.
3. **Identity/contact data.** `find_decision_maker` guesses aliases; funding radar uses synthetic companies. Needs Hunter/Clearbit-class and Crunchbase/Tracxn-class data — both paid, both with compliance requirements (GDPR/CAN-SPAM for cold email).
4. **Multi-user auth & tenancy.** The app is single-user, no authentication. A launch product needs accounts, per-user DB scoping, and encrypted-at-rest vault keys per user.
5. **Security hardening.** Vault Fernet key defaults to a hardcoded string; secrets are in env; no audit trail for "delete forever". Move vault key to a KMS/secret store, add request auth, TLS, and retention/export logging.
6. **Ops & CI.** No migrations (Alembic), no CI pipeline, no structured logging, no observability/metrics, no backup story for SQLite (move to Postgres).
7. **Legal/compliance review.** Automated applications, credential auto-creation, and cold outreach each carry ToS and anti-spam obligations that need explicit user consent + a compliance review before public launch.

---

## 5. Recommended roadmap to launch

- **M1 (correctness, now→2 weeks):** Alembic migrations + Postgres; CI (backend pytest + frontend tsc/vitest); secret management; audit logging.
- **M2 (real pipelines, 4–8 weeks):** Playwright autofill for Lever/Greenhouse/Workday; OAuth session handling for big boards; live funding + contact data integrations; real decision-maker discovery.
- **M3 (product trust, 2–4 weeks):** resume diff/preview + "upload polished version"; per-job approval console with granular state timeline; email open tracking + webhooks; error-resolution UX in the Jobs drawer.
- **M4 (launch):** multi-user auth + tenancy, billing/limits, compliance sign-off, load testing, docs, and a public beta.

---

## 6. Test & verification evidence

```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q   # 18 passed
cd frontend && npx tsc --noEmit                                     # clean
cd frontend && npm run build                                        # clean
```

Verified end-to-end over HTTP (v1.2.0): upload → profile+layout+keywords → discover (14 jobs) → generate (pending) → approve → apply (vault credential created, needs_input) → submit input → applied → Chrome/Apple vault export → funding radar (18 companies) → email draft/approve/send.

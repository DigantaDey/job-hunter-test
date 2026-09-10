# JobHunter AI — Autonomous Job Application System

> **"Remove the pain of applying to multiple jobs. Give every candidate an AI boost — with career-grade accuracy."**

A production-grade, full-stack **autonomous job-hunting platform** that ingests a master resume, extracts profile + layout with an OpenAI-compatible AI layer, **auto-extracts search keywords from your resume/profile/context**, discovers jobs (live keyless APIs + a curated pool), scores them, tailors resumes with a **JD fact-guard**, auto-creates vault credentials, auto-fills portals, handles the *User Input Needed* queue, cold-emails hiring managers/founders, runs an **AI-driven Funding Radar** (Seed→Series D), and runs **three parallel FIFO pipelines** (Discovery • Application • AI) under a single configurable rate limiter.

Built with product, engineering, AI, and design rigor — minimalist dashboard, dark/light/system theme, one-click Chrome/Apple Keychain vault export, error logs, and an offline-first heuristic fallback for every AI flow.

![Version](https://img.shields.io/badge/version-1.2.0-blue)
![Stack](https://img.shields.io/badge/stack-FastAPI%20%7C%20React%20%7C%20Vite%20%7C%20Tailwind-black)
![AI](https://img.shields.io/badge/AI-OpenAI%20compatible-emerald)
![Pipelines](https://img.shields.io/badge/pipelines-3%20parallel%20FIFO-violet)
![Tests](https://img.shields.io/badge/tests-18%20passing-green)

---

## ⚠️ Honest status (read this first)

This is a **fully-runnable v1.2 product-grade prototype**, not a finished SaaS. Every flow in the feature matrix has a working vertical slice, but the following are **simulated or delegated to public APIs** rather than fully automated:

| Area | Reality today |
|---|---|
| Job discovery | Live keyless APIs (**Arbeitnow**, **Remotive**) + a curated/synthetic pool. LinkedIn/Naukri/Indeed/Workday/Instahyre have *no real adapter* yet (ToS/anti-bot). |
| Application submit | Deterministic simulation (status machine + vault + needs-input queue are real; the browser autofill itself is not). |
| Decision-maker discovery | AI + conventional-alias heuristic (no people-data API). |
| Funding radar | AI mode intentionally proposes **synthetic** companies to avoid fabricating real funding claims; heuristic mode uses a clearly-labeled demo dataset. |
| Email sending | Real SMTP path implemented, plus a `needs_otp` flow; without SMTP config it returns a labeled *mock* success for the demo. |

See **[docs/LAUNCH_READINESS.md](docs/LAUNCH_READINESS.md)** for the full requirement-by-requirement assessment, blockers, and roadmap.

---

## ✨ Feature Matrix

| Requirement | Implementation |
|---|---|
| **Master resume upload** | PDF/DOCX, `pdfminer` + `pypdf` text, layout extractor (margins, lines, capitals, hyperlink style/color/underline, bullet style/count, borders, colors, fonts) |
| **AI extraction** | `ai_extract_profile()` → OpenAI-compatible `chat/completions` (JSON mode), fallback heuristic (regex + keyword) when no key |
| **Job scraping** | Adapter pattern (`linkedin/naukri/indeed/instahyre/lever/greenhouse/workday/custom`) + **live keyless APIs** (Arbeitnow, Remotive) + AI form-structure detection + freshness filter |
| **AI keyword extraction** | `keyword_extractor.py` — AI derives `keywords/roles/industries/tech_stack/locations/seniority/funding_focus` from profile + resume + free-form context (heuristic TF mining fallback). User input is **merged on top**. `GET /api/context/keywords` |
| **Editable settings (clubbed)** | `GET/PUT /api/settings` — categories `ai`, `scraping`, `general`, `application`, `email`, `funding`, `workflows`; RPM slider, keywords, freshness, live-source toggle, skeleton toggle, additional questions |
| **3 pipelines / FIFO / parallel** | DB `PipelineJob` + in-memory `AIPipeline` priority queue + `asyncio.create_task` + shared `rate_limiter`; `/api/pipelines/stats` |
| **Unified AI gateway** | `ai_client.py` — **every** AI call resolves per-workflow config and acquires a rate-limiter token *before* hitting the wire (the "never exceed RPM" guarantee now actually holds) |
| **Per-workflow AI API** | `GET/POST /api/ai/config` + a working editor in Settings — different `base_url/model/api_key` per workflow, default single key |
| **Auto-fill + User Input Needed queue** | missing `workAuthorization/linkedin` → `UserInputRequest(pending)` (deduped) → `POST /api/jobs/{id}/input` → re-queued and processed to completion |
| **Vault auto-credential** | `generate_credential()` + Fernet encrypt, per-portal-domain reuse; exports Chrome CSV + Apple CSV; `DELETE /api/vault` deletes forever |
| **Big platforms auto-submit** | source detection; `linkedin/naukri/indeed` → `is_external` → logs redirect, switches to credential+profile workflow |
| **AI resume generator** | `jd_fact_guard_prompt()` — memory + JD + “never hallucinate” rule; `strict_skeleton` toggle; DOCX/PDF build + download |
| **Resume approval gate** | generated resumes start `pending`; `POST /api/resumes/{id}/approve` unlocks them for the application pipeline (UI approve button + status chip) |
| **Resume reuse decision** | `_choose_resume()` — reuse an approved resume when score ≥ 65 **and** JD↔JD cosine ≥ 0.85, else generate new, else fall back to master |
| **Auto-tagging** | AI `{"tags": [...]}` + heuristic fallback; editable via `PUT /api/resumes/{id}/tags` |
| **AI form structure find** | `detect_form_structure(url)` — portal type + fields + confidence (mocked high-confidence; real HTML+LLM parsing is roadmap) |
| **Scoring (with/without AI)** | `heuristic_score()` TF-IDF cosine + coverage + bonus (tokenizer bug fixed) → `ai_score()` LLM JSON → fallback heuristic |
| **AI rate limiter** | `TokenBucketRateLimiter` 60/min default, rolling 60s window, `update_rpm()` from settings, precise sleeps, stats + throttled count |
| **AI online dot** | `GET /api/settings/ai/status` → pings `/models`, returns `online` + latency; frontend polls every 4s, green/red pulse |
| **Company classifier** | `heuristic_company_size()` + `ai_company_size()` → `big/medium/small/startup` |
| **Email pipeline (SMTP + 2FA)** | `find_decision_maker()` + `generate_cold_email()` (AI), `send_via_smtp()` real `smtplib` + `needs_otp` branch, inline OTP retry in the UI |
| **Funding radar (≤ Series D)** | AI scan matched to extracted `funding_focus`; dates always relative to *now*; DB upsert + auto-prune; stage filters; process → Job (open roles) or founder cold-email draft |
| **Email approval bucket** | `pending_approval → queued → sent/failed/needs_otp`; editable subject/body/to; `POST /approve` + `POST /send` |
| **Dashboard** | summary cards, recent jobs, pipeline mini, profile; job click → drawer with score, company size, forms, vault, resume choice, apply |
| **Error logs** | `ErrorLog` + `log_error/log_info` + `GET /api/logs` |
| **Theme** | light/dark/system via `prefers-color-scheme`, `localStorage`, Tailwind `dark:` variant |

---

## 🏗 Architecture

```
                ┌─────────────┐
     Resume PDF ━▶ Resume Parser ━┳━▶ Profile JSON
                └─────────────┘  ┃   Layout JSON (margins, bullets, colors…)
                                 ┃
                ┌────────────────▼──────────────────────┐
                │   Unified AI Gateway (ai_client)     │◀── per-workflow base_url/model/key
                │   rate-limiter token BEFORE every    │◀── Settings.rpm
                │   call · JSON mode · offline fallback│
                └──────────────┬───────────────────────┘
                               │  green/red health dot
                ┌──────────────▼──────────────┐
                │  Discovery Pipeline (FIFO)  │──▶ discover_jobs(keywords,freshness)
                │  live: arbeitnow/remotive   │    + curated pool + AI form detect
                │  curated: linkedin/naukri…  │    + scoring + classifier
                └──────────────┬──────────────┘
                               ▼
                          Jobs (DB)
                               │
                ┌──────────────▼──────────────┐
                │ Application Pipeline (FIFO) │──▶ vault credential (Fernet)
                │  autofill via AI + profile  │    + resume choose (reuse/generate/master)
                │  external redirect handled  │    + approval gate
                └──────────────┬──────────────┘
                               │
                ┌──────────────▼──────────────┐
                │  Email Pipeline             │──▶ find decision maker
                │  approval bucket → SMTP     │    + founder cold email (+2FA/OTP)
                └──────────────┬──────────────┘
                               │
                ┌──────────────▼──────────────┐
                │  Funding Pipeline           │──▶ AI keyword extraction
                │  Seed→D • fresh ≤45d •      │    (profile+resume+context)
                │  auto-refresh • pruned      │    → jobs or founder email
                └─────────────────────────────┘

Frontend: Vite + React 18 + TypeScript + Tailwind + React Router
         Dashboard / Jobs / Queues / Resume Studio / Email Bucket / Funding / Vault / Settings / Logs
Backend: FastAPI + SQLAlchemy (SQLite) + Pydantic + httpx + python-docx/reportlab + cryptography
```

**Resume decision:**
```
score ≥ 65 AND best JD-similarity ≥ 0.85  → reuse approved generated resume
score ≥ 65 (no close match)               → generate new tailored resume (pending approval)
otherwise                                 → use master resume
```

---

## 🚀 Quick Start

### Prereqs
- Python 3.11+, Node 20+, `pip`, `npm`
- OpenAI-compatible API key (optional — every flow has a heuristic fallback)

### 1. Clone & env
```bash
git clone <your-fork>
cd job-hunter-test
cp .env.example .env
# edit .env: AI_API_KEY, AI_BASE_URL, AI_MODEL (leave AI_API_KEY empty for offline mode)
```

### 2. Backend (FastAPI)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --app-dir backend
# → http://localhost:8000/api/docs
```

### 3. Frontend (Vite)
```bash
cd frontend
npm install
npm run dev          # → http://localhost:5173  (proxies /api → :8000)
npm run build        # → frontend/dist (served by FastAPI at /)
```

### One-shot dev (builds frontend, serves everything on :8000)
```bash
./run.sh
```

### Docker
```bash
docker-compose up --build   # backend :8000, frontend :5173
```

---

## 🔌 API Reference

Base: `http://localhost:8000` · interactive docs at `/api/docs`

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health |
| `GET` | `/api/meta` | Feature list |
| `POST` | `/api/resume/upload` | Upload PDF/DOCX → profile+layout+context |
| `GET` | `/api/profile/current` | Current profile |
| `GET` | `/api/resumes` | List resumes (incl. `status`) |
| `POST` | `/api/resumes/generate?job_id=&strict_skeleton=` | Tailored DOCX/PDF (starts `pending`) |
| `POST` | `/api/resumes/{id}/approve` | Approve a generated resume for use |
| `GET` | `/api/resumes/{id}/download?format=docx\|pdf` | Download |
| `PUT` | `/api/resumes/{id}/tags` | Edit tags |
| `GET` | `/api/jobs` | List (filter `status,source,company_size,q`) |
| `GET` | `/api/jobs/{id}` | Detail + score + forms |
| `POST` | `/api/jobs/discover` | Trigger discovery (live + curated, AI keywords) |
| `GET` | `/api/context/keywords` | AI-extracted search context |
| `POST` | `/api/jobs/{id}/apply?resume_choice=auto\|master\|generated&resume_id=` | Autofill + vault + queue (returns `resume_decision`) |
| `POST` | `/api/jobs/{id}/input` | Submit User Input Needed |
| `GET` | `/api/vault` · `/api/vault/export/chrome` · `/api/vault/export/apple` · `DELETE /api/vault` | Vault list / exports / delete-forever |
| `GET` | `/api/emails` · `POST /api/emails/generate` · `PUT /api/emails/{id}` · `POST /api/emails/{id}/approve` · `POST /api/emails/{id}/send?otp=` | Email bucket |
| `GET` | `/api/funding/companies?stage=&refresh=` · `POST /api/funding/refresh` · `GET /api/funding/context` · `POST /api/funding/{name}/process` | Funding radar |
| `GET/PUT` | `/api/settings` · `GET /api/settings/ai/status` | Grouped settings + AI health |
| `GET/POST` | `/api/ai/config` | Per-workflow AI override |
| `GET` | `/api/pipelines/stats` · `/api/pipelines/jobs` · `/api/user-input-queue` | Pipelines & queues |
| `GET` | `/api/logs` | Error logs |
| `POST` | `/api/classify/company?company=&jd=` | Company size |
| `GET` | `/api/dashboard/summary` | Dashboard cards |

---

## 🔒 Vault Export Format

**Chrome** (`chrome_passwords.csv`):
```
name,url,username,password
lever.co,https://lever.co,user_a1b2@jobhunter.local,Strong!Pass123
```

**Apple** (`apple_passwords.csv`):
```
Title,URL,Username,Password,Notes,OTPAuth
lever.co,https://lever.co,user_a1b2@jobhunter.local,Strong!Pass123,Generated by JobHunter,
```

Import via `chrome://settings/passwords` → Import or macOS Keychain → Import.

> ⚠️ In production, set a strong `VAULT_KEY` (or better, a KMS-backed secret) — the
> default is a dev-only placeholder.

---

## 🤖 AI Layer (OpenAI-Compatible)

All AI calls flow through `backend/app/services/ai_client.py`:

- One gateway → resolves `base_url/api_key/model` (per-workflow override wins, then env), acquires a **rate-limiter token**, then POSTs `/chat/completions`.
- JSON mode by default; every service keeps a deterministic heuristic fallback (works fully offline).
- Health probe (`/models`) powers the green/red dot.

**Workflows:** `parse`, `keyword_extract`, `scoring`, `resume_gen`, `classify`, `email_gen`, `form_detect`, `funding_scan`, `tagging`.

**Per-workflow override** (Settings UI, or API):
```json
POST /api/ai/config
{ "resume_gen": {"base_url":"https://api.openai.com/v1","model":"gpt-4o","api_key":"sk-..."} }
```

---

## 📁 Project Structure

```
backend/
  app/
    main.py               # FastAPI, CORS, startup (AI worker + override load), SPA fallback
    core/config.py        # Settings (env)
    core/security.py      # Fernet encrypt
    core/rate_limiter.py  # TokenBucket
    db.py                 # SQLAlchemy engine + init (+ lightweight SQLite migration)
    models/models.py      # Profile, Resume, Job, VaultEntry, Email, Settings, ErrorLog, PipelineJob, FundingCompany, UserInputRequest
    schemas/schemas.py    # Pydantic
    services/
      ai_client.py        # unified, rate-limited OpenAI-compatible gateway
      resume_parser.py    # layout + AI extract
      resume_generator.py # tailor + docx/pdf + tags
      scoring.py          # heuristic + AI + reuse decision
      classifier.py       # company size
      job_scraper.py      # live (arbeitnow/remotive) + curated pool + form detect
      vault.py            # generate + Chrome/Apple CSV
      email_pipeline.py   # finder + cold email + SMTP 2FA + funding shim
      ai_pipeline.py      # FIFO priority queue + worker + health
      keyword_extractor.py# AI/heuristic search context
      funding_radar.py    # Seed→D radar
    api/routes.py         # All endpoints
    utils/logger.py       # ErrorLog helper
  tests/                  # 18 tests (hermetic, offline)
  requirements.txt  Dockerfile
frontend/
  src/
    components/Layout.tsx # Sidebar, AI dot, theme
    pages/  Dashboard Jobs Queues Resumes Emails Funding Vault Settings Logs
    hooks/useTheme.tsx  api/client.ts  App.tsx  main.tsx  index.css
  vite.config.ts  tailwind.config.js  package.json  Dockerfile
docs/LAUNCH_READINESS.md # full launch assessment + roadmap
```

---

## 🧪 Testing

```bash
# Backend (offline, no AI key needed)
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q

# Frontend type-check + build
cd frontend && npx tsc --noEmit && npm run build
```

**Happy path (curl):**
```bash
curl -F "file=@/tmp/test_resume.pdf" http://localhost:8000/api/resume/upload
curl -X POST http://localhost:8000/api/jobs/discover
curl http://localhost:8000/api/jobs | jq
curl -X POST "http://localhost:8000/api/resumes/generate?job_id=1"
curl -X POST http://localhost:8000/api/resumes/2/approve
curl -X POST "http://localhost:8000/api/jobs/1/apply?resume_choice=auto"
curl http://localhost:8000/api/vault/export/chrome
curl -X POST "http://localhost:8000/api/emails/generate?company=Linear"
curl http://localhost:8000/api/funding/companies
```

---

## 🔮 Roadmap

- Playwright headless for real Workday/Lever/Greenhouse form autofill + CAPTCHA handling
- OAuth for LinkedIn/Indeed/Instahyre + Naukri session handling
- Real funding + people-data integrations (Crunchbase/Tracxn, Hunter/Clearbit) with compliance
- Resume diff/preview + "upload polished version" as a first-class flow
- Multi-user auth + tenancy, Alembic migrations, Postgres, CI (GitHub Actions), structured logs + metrics
- Vector DB for resume↔JD similarity (reuse vs new)

See **[docs/LAUNCH_READINESS.md](docs/LAUNCH_READINESS.md)** for the detailed blockers and milestones.

---

## 📄 License

MIT — do what you want, but keep the fact-guard.

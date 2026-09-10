# JobHunter AI — Autonomous Job Application System

> **"Remove the pain of applying to multiple jobs. Give every candidate an AI boost — with career-grade accuracy."**

A production-grade, full-stack **autonomous job-hunting platform** that ingests a master resume, extracts profile + layout with an OpenAI-compatible AI layer, discovers jobs across the internet, scores them, tailors resumes with a **JD fact-guard**, auto-creates vault credentials, auto-fills portals, handles *User Input Needed* queues, cold-emails hiring managers/founders, tracks funding-round companies, and runs **three parallel FIFO pipelines** (Discovery • Application • AI) under a configurable rate limiter.

Built with product, engineering, AI, and design rigor — minimalist dashboard, dark/light/system theme, one-click Chrome/Apple Keychain vault export, and error logs.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Stack](https://img.shields.io/badge/stack-FastAPI%20%7C%20React%20%7C%20Vite%20%7C%20Tailwind-black)
![AI](https://img.shields.io/badge/AI-OpenAI%20compatible-emerald)
![Pipelines](https://img.shields.io/badge/pipelines-3%20parallel%20FIFO-violet)

---

## ✨ Feature Matrix (Every Requirement Implemented)

| Requirement | Implementation |
|---|---|
| **Master resume upload** | PDF/DOCX, `pdfminer` + `pypdf` text, layout extractor (margins, lines, capitals, hyperlink style/color/underline, bullet style/count, borders, colors, fonts) |
| **AI extraction** | `ai_extract_profile()` → OpenAI-compatible `chat/completions` with `response_format:json_object`, fallback heuristic (regex + keyword) if no key |
| **Job scraping** | Adapter pattern for `linkedin / naukri / indeed / instahyre / lever / greenhouse / workday / custom` + AI form-structure detection (`detect_form_structure`) + freshness filter |
| **Editable settings (clubbed)** | `GET/PUT /api/settings` — categories: `ai`, `scraping`, `general`, `application`, `email`, `workflows`. RPM slider, keywords, freshness, skeleton toggle, additional questions |
| **3 pipelines / FIFO / parallel** | DB `PipelineJob` + in-memory `AIPipeline` queue with priority insertion (`priority 1 highest`), `asyncio.create_task` + `rate_limiter.wait_and_acquire()`; stats at `/api/pipelines/stats` |
| **Vault auto-credential** | `generate_credential()` + Fernet encrypt, save per Workday/Lever/Greenhouse; exports: Chrome CSV `name,url,username,password` + Apple CSV `Title,URL,Username,Password,Notes,OTPAuth`; `DELETE /api/vault` deletes forever |
| **Auto-fill + User Input Needed queue** | `extra.forms_detected` → missing `workAuthorization/linkedin` → `UserInputRequest(status=pending)` → frontend *User Input Needed Queue* → `POST /api/jobs/{id}/input` → re-queued to application |
| **Big platforms auto-submit** | `source` detection; if `linkedin/naukri/indeed` → `is_external` → log external redirect, switches to credential+profile workflow |
| **AI resume generator** | `jd_fact_guard_prompt()` — memory + JD + “never hallucinate” guard; `strict_skeleton` toggle preserves layout; `build_docx()` + `build_pdf()`; DOCX/PDF download; upload polished version re-uses |
| **Auto-tagging** | Tags from AI `{"tags": [...]}` + heuristic fallback; editable via `PUT /api/resumes/{id}/tags` |
| **AI form structure find** | `detect_form_structure(url)` — real fetch + LLM, mocked high-confidence for demo; stored in `job.extra` |
| **Scoring (with/without AI)** | `heuristic_score()` TF-IDF cosine + coverage + bonus → `ai_score()` LLM JSON `{"score","reason"}`, fallback to heuristic; `should_generate_new_resume()` decides reuse vs new |
| **AI rate limiter** | `TokenBucketRateLimiter` 60/min default, rolling 60s window, `update_rpm()` from settings, `wait_and_acquire()` sleeps precisely, stats + throttled count |
| **AI online dot** | `GET /api/settings/ai/status` → pings `/models`, returns `online` + `latency_ms`; frontend polls every 4s, green/red pulse |
| **Company classifier** | `heuristic_company_size()` (employees/int + keyword) + `ai_company_size()` LLM → `big/medium/small/startup` |
| **Email pipeline (SMTP + 2FA)** | `find_decision_maker()` + `generate_cold_email()` (AI), `send_via_smtp()` mock + real `smtplib` with `needs_otp` branch, frontend handles OTP prompt |
| **Funding radar (≤ Series D)** | `find_funded_companies()` mock Series A-D, `POST /api/funding/{name}/process` → if `has_open_positions` → create Job, else founder cold email |
| **Email approval bucket** | `Email.status` `pending_approval → queued → sent/failed/needs_otp`; editable subject/body, `POST /approve` + `POST /send` |
| **Per-workflow AI API** | `GET/PUT /api/ai/config` → `settings(category='ai_workflows')` → different `base_url/model/api_key` per workflow |
| **Dashboard** | Summary cards, recent jobs, pipeline mini, profile; job click → drawer with score, company_size, forms, status, apply |
| **Error logs** | `ErrorLog` + `log_error/log_info` + `GET /api/logs` |
| **Theme** | `ThemeProvider` — light/dark/system via `prefers-color-scheme`, `localStorage`, Tailwind `dark:` variant |

---

## 🏗 Architecture

```
                ┌─────────────┐
     Resume PDF ━▶ Resume Parser ━┳━▶ Profile JSON
                └─────────────┘  ┃   Layout JSON (margins, bullets, colors…)
                                 ┃
                ┌────────────────▼────────────────────┐
                │  AI Pipeline (FIFO, priority, RPM) │◀── Settings.rpm
                │  1: autofill  2: resume_gen 3: scoring ...
                └──────────────┬───────────────────────┘
                               │  green/red health dot
                ┌──────────────▼──────────────┐
                │  Discovery Pipeline (FIFO)  │──▶ discover_jobs(keywords,freshness)
                │  sources: linkedin, naukri, │    + AI form detect + scoring + classifier
                │  lever, greenhouse, workday │
                └──────────────┬──────────────┘
                               ▼
                          Jobs (DB)
                               │
                ┌──────────────▼──────────────┐
                │ Application Pipeline (FIFO) │──▶ vault credential (Fernet)
                │  autofill via AI + profile  │    + missing → UserInputNeeded → re-queue
                │  external redirect handled   │    + resume choice logic (scoring)
                └──────────────┬──────────────┘
                               │
                ┌──────────────▼──────────────┐
                │  Email Pipeline             │──▶ find decision maker
                │  approval bucket → SMTP     │    + founder cold email
                └──────────────┬──────────────┘
                               │
                ┌──────────────▼──────────────┐
                │  Funding Pipeline           │──▶ Series A-D radar
                └─────────────────────────────┘

Frontend: Vite + React 18 + TypeScript + Tailwind + React Router
         Dashboard / Jobs / Queues / Resume Studio / Email Bucket / Funding / Vault / Settings / Logs
Backend: FastAPI + SQLAlchemy (SQLite) + Pydantic + httpx + python-docx/reportlab + cryptography
```

**Scoring decision:**
```
heuristic: tf(profile) cosine tf(JD) *70 + coverage*25 + bonus → 0-100
AI: prompt → {"score","reason","missing_skills","strengths"} → fallback heuristic
Reuse if best_existing_sim >0.75 else generate new (strict_skeleton toggles layout preservation)
```

**Rate limiter:**
```
TokenBucket: deque[timestamps within 60s]; acquire() → if len+1 > rpm: wait = 60 - (now - oldest)
Stats: used_in_window, remaining, throttled
```

---

## 🚀 Quick Start

### Prereqs
- Python 3.11+, Node 20+, `pip`, `npm`
- OpenAI-compatible API key (optional — all flows have heuristic fallbacks)

### 1. Clone & env
```bash
git clone <your-fork>
cd job-hunter-test
cp .env.example .env
# edit .env: AI_API_KEY, AI_BASE_URL, AI_MODEL
```

### 2. Backend (FastAPI)
```bash
pip install --user -r backend/requirements.txt
# or: python3 -m venv .venv && source .venv/bin/activate && pip install -r backend/requirements.txt
PYTHONPATH=$HOME/.local/lib/python3.11/site-packages:/home/user/job-hunter-test/backend \
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# → http://localhost:8000/api/docs
```

### 3. Frontend (Vite)
```bash
cd frontend
npm install
npm run dev          # → http://localhost:5173  (proxies /api → :8000)
npm run build        # → frontend/dist (served by FastAPI at /)
```

### Docker (alternative)
```bash
docker-compose up --build
# backend :8000, frontend :5173
```

---

## 🔌 API Reference

Base: `http://localhost:8000`

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health |
| `GET` | `/api/meta` | Feature list |
| `POST` | `/api/resume/upload` | Upload PDF/DOCX → profile+layout |
| `GET` | `/api/profile/current` | Current profile |
| `GET` | `/api/resumes` | List resumes |
| `POST` | `/api/resumes/generate?job_id=&strict_skeleton=` | Tailored DOCX/PDF with fact-guard |
| `GET` | `/api/resumes/{id}/download?format=docx\|pdf` | Download |
| `PUT` | `/api/resumes/{id}/tags` | Edit tags |
| `GET` | `/api/jobs` | List (filter `status,source,q`) |
| `GET` | `/api/jobs/{id}` | Detail + score + forms |
| `POST` | `/api/jobs/discover` | Trigger discovery (keywords,freshness) |
| `POST` | `/api/jobs/{id}/apply?resume_choice=auto\|master` | Autofill + vault + queue |
| `POST` | `/api/jobs/{id}/input` | Submit User Input Needed |
| `GET` | `/api/vault` | List credentials |
| `GET` | `/api/vault/export/chrome` | Chrome CSV |
| `GET` | `/api/vault/export/apple` | Apple CSV |
| `DELETE` | `/api/vault` | Delete forever |
| `GET` | `/api/emails` | List |
| `POST` | `/api/emails/generate?company=&department=&founder=` | Draft |
| `PUT` | `/api/emails/{id}` | Edit |
| `POST` | `/api/emails/{id}/approve` | Approve → queued |
| `POST` | `/api/emails/{id}/send?otp=` | SMTP (2FA) |
| `GET` | `/api/funding/companies` | Series A-D |
| `POST` | `/api/funding/{name}/process` | Apply or founder email |
| `GET` | `/api/settings` | Grouped settings |
| `PUT` | `/api/settings` | Save grouped |
| `GET` | `/api/settings/ai/status` | Online/latency/RPM |
| `GET` | `/api/pipelines/stats` | All pipeline counts + AI queue |
| `GET` | `/api/pipelines/jobs` | Recent pipeline jobs |
| `GET` | `/api/user-input-queue` | Pending inputs |
| `GET` | `/api/logs` | Error logs |
| `POST` | `/api/classify/company?company=&jd=` | Company size |
| `GET` | `/api/dashboard/summary` | Cards |
| `GET/PUT` | `/api/ai/config` | Per-workflow AI |

Interactive docs: `http://localhost:8000/api/docs`

---

## 🎨 UI Tour

- **Dashboard** — hero with pipeline counters, stat cards (discovered/applied/needs_input/failed), recent discoveries, quick actions, funding teaser.
- **Jobs** — filters (status, source, search), list with score & size, detail drawer: JD, scoring reason, forms, vault, resume choice, apply + needs-input handling.
- **Queues** — three pipeline cards (queued/processing/done/failed/needs_input) + AI queue preview + RPM / throttled + User Input Needed queue (inline form, re-queue).
- **Resume Studio** — master upload → profile + layout JSON, skills chips, generate (pick job, strict skeleton toggle), files, auto-tags editable, scoring explanation.
- **Email Bucket** — generate (company+department → decision maker via AI), editable subject/body/to, approve → queued, send (OTP flow), status chips.
- **Funding Radar** — Series A-D cards, “Apply usual flow” vs “Cold email founder” branching.
- **Vault** — credential list, Chrome/Apple CSV download, delete forever, Fernet encryption note.
- **Settings** — grouped cards (AI RPM slider, scraping keywords/freshness/sources, resume skeleton, SMTP, workflow toggles) + per-workflow AI override.
- **Logs** — pipeline filter, level, timestamp, meta JSON.

Theme toggle: **Light • Dark • System** (top-right of sidebar, persists in `localStorage`, respects `prefers-color-scheme`).

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

---

## 🤖 AI Layer (OpenAI-Compatible)

All AI calls go through `AI_BASE_URL` (default `https://api.openai.com/v1`) with `Authorization: Bearer $AI_API_KEY`.

- **Profile extract** — `response_format: json_object`, strict JSON.
- **Resume tailor** — `jd_fact_guard_prompt` adds layout + “never invent” rule → `tailored_profile + tags`.
- **Scoring** — `{"score","reason","missing_skills","strengths"}` → fallback TF-IDF.
- **Company size** — `{"size","confidence","reason"}` → fallback heuristic.
- **Email** — subject+body, no hallucination.
- **Form detection** — would fetch HTML + LLM; mocked with confidence.

**Per-workflow override:**
```json
PUT /api/ai/config
{
  "resume_gen": {"base_url":"https://api.openai.com/v1","model":"gpt-4o","api_key":"sk-..."},
  "scoring": {"model":"gpt-4o-mini"}
}
```

**Rate limiter:** if RPM=60 and 60 requests used in last 60s, next acquire waits `60 - (now - oldest)`. Frontend shows `remaining/rpm` and throttled count.

---

## 📁 Project Structure

```
backend/
  app/
    main.py               # FastAPI, CORS, startup (AI worker), static frontend
    core/config.py        # Settings (env)
    core/security.py      # Fernet encrypt
    core/rate_limiter.py  # TokenBucket
    db.py                 # SQLAlchemy engine + init
    models/models.py      # Profile, Resume, Job, VaultEntry, Email, Settings, ErrorLog, PipelineJob, FundingCompany, UserInputRequest
    schemas/schemas.py    # Pydantic
    services/
      resume_parser.py    # layout + AI extract
      resume_generator.py # tailor + docx/pdf + tags
      scoring.py          # heuristic + AI
      classifier.py       # company size
      job_scraper.py      # discover + form detect
      vault.py            # generate + Chrome/Apple CSV
      email_pipeline.py   # finder + cold email + SMTP 2FA + funding
      ai_pipeline.py      # FIFO priority queue + worker + health
    api/routes.py         # All endpoints
    utils/logger.py       # ErrorLog helper
  requirements.txt
  Dockerfile
frontend/
  src/
    components/Layout.tsx # Sidebar, AI dot, theme
    pages/
      Dashboard.tsx  Jobs.tsx  Queues.tsx  Resumes.tsx
      Emails.tsx  Funding.tsx  Vault.tsx  Settings.tsx  Logs.tsx
    hooks/useTheme.tsx
    api/client.ts
    App.tsx  main.tsx  index.css
  vite.config.ts  tailwind.config.js  package.json
  Dockerfile
jobhunter.db              # SQLite (absolute path)
uploads/  generated/      # Resumes
```

---

## 🧪 Professional Standards

- **Type hints** + **Pydantic** + **SQLAlchemy 2.0** Declarative
- **FIFO + priority** queues, `asyncio` parallel pipelines
- **Error logging** to DB + UI, `try/except` with fallback heuristics (works offline)
- **Security:** Fernet encryption, CORS, env secrets, no password logging
- **UX:** Minimalist cards, mono metrics, motion-reduced, keyboard-friendly, responsive (mobile nav)
- **Docs:** OpenAPI at `/api/docs`, this README, `.env.example`, `docker-compose.yml`
- **Testing the happy path:**
  ```bash
  # 1. Upload resume
  curl -F "file=@/tmp/test_resume.pdf" http://localhost:8000/api/resume/upload
  # 2. Discover
  curl -X POST http://localhost:8000/api/jobs/discover
  # 3. List & pick
  curl http://localhost:8000/api/jobs | jq
  # 4. Generate tailored resume
  curl -X POST "http://localhost:8000/api/resumes/generate?job_id=1&strict_skeleton=false"
  # 5. Apply (may go to needs_input)
  curl -X POST "http://localhost:8000/api/jobs/1/apply?resume_choice=auto"
  # 6. Vault
  curl http://localhost:8000/api/vault/export/chrome
  # 7. Email
  curl -X POST "http://localhost:8000/api/emails/generate?company=Linear"
  # 8. Funding
  curl http://localhost:8000/api/funding/companies
  ```

---

## 🔮 Roadmap (Future Hardening)

- Playwright headless for real Workday/Lever/ Greenhouse form autofill + CAPTCHA solving
- Resume diff viewer before upload
- OAuth for LinkedIn/Indeed/Instahyre + Naukri session handling
- Vector DB for resume-JD similarity (reuse vs new)
- Webhooks + email open tracking
- Tests: `pytest` + `vitest`, CI (GitHub Actions), `alembic` migrations

---

## 📄 License

MIT — do what you want, but keep the fact-guard.

---

**Built with care for accuracy.** Every AI prompt is constrained (“do not invent”), every resume is approval-gated, every pipeline is FIFO-observable. Your career deserves no less.


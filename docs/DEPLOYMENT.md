# Deployment guide

Target topology: **one API container (serving the SPA + JSON API) + PostgreSQL + one worker
container**, behind a TLS-terminating reverse proxy. Everything below is what a reviewer would ask
for before calling the deployment production-grade.

---

## 1. Pre-flight checklist

| # | Item | How |
|---|---|---|
| 1 | `ENVIRONMENT=production` | refuses placeholder secrets, wildcard CORS/hosts, SQLite |
| 2 | `SECRET_KEY`, `ENCRYPTION_KEY` are 32+ byte random values | `python -c "import secrets;print(secrets.token_hex(32))"` |
| 3 | `DATABASE_URL` points at PostgreSQL | `postgresql+psycopg2://user:pass@host:5432/db` |
| 4 | `CORS_ORIGINS` and `ALLOWED_HOSTS` list your real domain(s) | e.g. `https://app.example.com` / `app.example.com` |
| 5 | TLS terminates in front of the app | reverse proxy or platform LB; HSTS is on in production |
| 6 | `METRICS_TOKEN` set (or `METRICS_ENABLED=false`) | **required**: production refuses to start with metrics enabled and no token. Scrape with `Authorization: Bearer <token>` |
| 7 | Email block configured **only if** you send real mail | `EMAIL_SENDING_ENABLED`, `EMAIL_DRY_RUN=false`, `EMAIL_POSTAL_ADDRESS`, `EMAIL_UNSUBSCRIBE_BASE_URL`, SPF/DKIM/DMARC |
| 8 | `EMAIL_WEBHOOK_TOKEN` set if your provider posts engagement events | `POST /api/track/events` |
| 9 | `ALLOW_SYNTHETIC_FUNDING_DATA=false` | demo data must never reach users |
| 10 | Backups scheduled and **restore-tested** | `backend/scripts/backup.py` (see §5) |
| 11 | `AUTOFILL_*` left at defaults unless you accept the portal-ToS risk | dry-run by default |
| 12 | Log shipper configured | `LOG_JSON=true`, ship stdout |

The application fails fast at startup with a readable list of the problems above (see
`Settings.validate_runtime()`); it never silently degrades into an insecure mode.

---

## 2. Compose deployment

```bash
git clone <repo> && cd job-hunter-test
cp .env.example .env
# edit .env: ENVIRONMENT=production, POSTGRES_PASSWORD, SECRET_KEY, ENCRYPTION_KEY,
#            CORS_ORIGINS, ALLOWED_HOSTS, AI_API_KEY (optional), EMAIL_* (optional)

docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api
```

The API container applies migrations on startup (`AUTO_MIGRATE=true`), waits for the database
healthcheck, and exposes `/api/health/ready`. The worker container processes the pipelines; set
`RUN_WORKER_IN_API=false` on the API (the compose file does).

Scale the worker (or a second API replica behind the LB) with:

```bash
docker compose -f docker-compose.prod.yml up -d --scale worker=3
```

Larger installs should run migrations as a separate step instead of on boot:

```bash
docker compose -f docker-compose.prod.yml run --rm api python -m alembic upgrade head
# then set AUTO_MIGRATE=false for the API/worker services
```

---

## 3. Reverse proxy (nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name app.example.com;

    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;

    client_max_body_size 20m;            # MAX_UPLOAD_MB + overhead

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;         # AI-backed requests can be slow
    }
}
```

Caddy/Traefik equivalents work unchanged; the app respects `X-Forwarded-*` (uvicorn is started with
`--proxy-headers`).

---

## 4. First run

1. Open `https://app.example.com` → **Create the owner account** (the first account is the owner).
2. Sign in → **Settings**: add your AI key if you want AI features (the app is fully functional
   without one), review the scraping sources, and set your keyword context.
3. **Resume Studio**: upload your master resume (PDF/DOCX). The parser extracts the profile and
   layout; a search context is generated.
4. **Account**: accept the consents you are comfortable with (terms, automation, outreach, data
   processing). Automation and outreach stay blocked until you do.
5. **Jobs**: run discovery; approve the tailored resumes you like; let the pipelines run.
6. **Vault**: credentials the app created are visible here; export to Chrome/Apple when needed.

---

## 5. Backups & restore

```bash
# manual
docker compose -f docker-compose.prod.yml run --rm backup

# cron (host)
0 3 * * * docker compose -f /srv/jobhunter/docker-compose.prod.yml run --rm backup >> /var/log/jobhunter-backup.log 2>&1
```

* **PostgreSQL** — `pg_dump | gzip`, retained `BACKUP_KEEP` (default 14) generations. Restore:
  `gunzip -c backup.sql.gz | psql "$DATABASE_URL"`.
* **SQLite** — online backup API + `PRAGMA integrity_check` before the file is accepted. Restore:
  stop the app, copy the file back, start.
* **User files** — `uploads/`, `generated/`, `artifacts/screenshots` are volumes; snapshot them
  alongside the database or run them through your object-storage backup.
* Restore drills matter: run one on a scratch host before you need it.

---

## 6. Monitoring

| Signal | Where |
|---|---|
| Liveness | `GET /api/health/live` → 200 `{"status":"alive"}` |
| Readiness | `GET /api/health/ready` → database + migration state + queue summary |
| Metrics | `GET /api/metrics` (Prometheus text; `Authorization: Bearer $METRICS_TOKEN`) |
| Queue depth / dead letters | `GET /api/ops/status`, `GET /api/pipelines/stats`, `POST /api/ops/queue/{recover,retry-dead}` |
| Errors | `GET /api/logs?level=error`, and stdout (JSON when `LOG_JSON=true`) |
| Audit | `GET /api/account/audit` per user; `audit_logs` table for the whole instance |

Suggested alerts: readiness failing for 2 minutes, `jobhunter_pipeline_jobs_total{status="dead"}`,
queue age > 10 minutes, `jobhunter_ai_requests_total{status="breaker_open"}` rising, 5xx rate, disk
usage on the uploads/generated volumes.

---

## 7. Upgrades

```bash
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm api python -m alembic upgrade head
docker compose -f docker-compose.prod.yml up -d
```

Migrations are additive; take a backup first (the `backup` service exists for exactly this). The
startup log prints the app version, environment, database driver and migration state.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Startup refuses with "SECRET_KEY looks like a placeholder" | you are in production with dev defaults — set real secrets |
| `alembic` says table already exists | a database from an older release: `python -m alembic stamp head` then upgrade (the app does this automatically for the v1.2 schema) |
| All AI features report offline | no `AI_API_KEY` (global or per-workflow) — the app degrades to heuristics by design |
| Emails stay `dry_run` | `EMAIL_DRY_RUN=true` — flip it deliberately, after configuring the postal address and unsubscribe URL |
| `/api/metrics` returns 401 | `METRICS_TOKEN` is set — send `Authorization: Bearer <token>` |
| Queue not draining | worker container not running (`RUN_WORKER_IN_API=false` on the API) |
| Upload rejected with 413 | file larger than `MAX_UPLOAD_MB`, or the proxy's `client_max_body_size` |

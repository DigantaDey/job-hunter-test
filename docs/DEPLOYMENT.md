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
| 10 | Backups scheduled **and restore-tested** | `backup-scheduler` runs daily out of the box (see §5); verify once with `logs backup-scheduler` |
| 11 | `AUTOFILL_*` left at defaults unless you accept the portal-ToS risk | dry-run by default |
| 12 | Log shipper configured | `LOG_JSON=true`, ship stdout |
| 13 | Billing provider configured | `BILLING_PROVIDER=stripe|razorpay|manual`, `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`; the webhook secret is mandatory — without it the billing webhook routes refuse every event (400; 503 in production) rather than skipping verification; webhooks idempotent via `billing_events` unique constraint |
| 14 | AI credit ceilings configured per plan | plan limits (`ai_credits_per_month`) live in `app/core/entitlements.py`; monitor `ai_credit_ledger` |
| 15 | Pricing page public | `/pricing` should be reachable without auth for SEO; `/billing` behind auth |
| 16 | Proxy trust boundary set **iff** a proxy is in front | `TRUSTED_PROXIES` + `FORWARDED_ALLOW_IPS` = the proxy's address/CIDR (§3.1). Never `*`/`0.0.0.0/0` — production refuses to start; without it any client can choose the IP that rate limits and audit rows are keyed on |
| 17 | `WEB_CONCURRENCY=1` unless you read §6 | every in-process budget (rate limits, AI token/daily budgets, per-user AI overrides, metrics) is per process; N workers multiply them by N |

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

Caddy/Traefik equivalents work unchanged — but **the app only believes `X-Forwarded-*` from a proxy
you name** (next section). With the shipped defaults it ignores the header entirely and uses the
address of the socket that connected.

### 3.1 Client IP trust boundary (read this before you put a proxy in front)

`X-Forwarded-For` is a claim made by whoever hands the app a request. Two settings decide which
claims are believed, and both are **empty/loopback by default**:

| Setting | Read by | Default | Effect |
|---|---|---|---|
| `FORWARDED_ALLOW_IPS` | uvicorn (`--proxy-headers`, in the image CMD) | `127.0.0.1` | rewrites `scope["client"]` from the header when the peer matches |
| `TRUSTED_PROXIES` | the app (`app.core.middleware.resolve_client_ip`) | *(empty)* | same question, answered in-process: rate-limit keys, login-failure attribution, `audit_logs.ip` |

Either one is sufficient; set both to the same value so the two layers cannot disagree. The
resolution rule is the standard one: walk the header **from the right** and take the first hop that
is not itself a trusted proxy — everything left of that was written by the client or by an upstream
it chose. A wildcard (`*`) or an all-covering CIDR (`0.0.0.0/0`) hands that choice back to any
client that can open a socket, which is why **production refuses to start with one**
(`Settings.validate_for_runtime`).

Pick the row that matches your topology:

| Topology | `FORWARDED_ALLOW_IPS` / `TRUSTED_PROXIES` |
|---|---|
| No proxy (`docker run -p 8000:8000`, bare uvicorn) | leave the defaults — the peer address *is* the client |
| nginx/Caddy on the docker host, port published | the docker gateway, usually `172.17.0.1` (`docker network inspect bridge`) |
| Proxy container in the compose network | that network's subnet, e.g. `172.18.0.0/16` |
| Cloud load balancer / ingress | the LB's subnet (e.g. `10.0.0.0/8`, or the provider's published ranges) |
| Proxy chain (LB → nginx → app) | list **every** hop; the rightmost non-listed address wins |

If you get this wrong the failure is *fail-closed*, not spoofable: every request is attributed to the
proxy's address, so all clients share one rate-limit bucket and audit rows all show the proxy. The
app says so rather than staying quiet — `jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}`
increments and one warning per minute is logged:

```
ignoring X-Forwarded-For (203.0.113.7) from untrusted peer 172.18.0.1 — if that peer is your reverse
proxy, list it in TRUSTED_PROXIES (and/or FORWARDED_ALLOW_IPS ...)
```

`GET /api/ops/status` reports the effective boundary under `edge.client_ip`.

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

**Automated backups ship enabled.** The `backup-scheduler` service in `docker-compose.prod.yml`
runs `backend/scripts/backup.py --out /backups --keep 14` once when the stack comes up and then
every 24 hours — nothing to install, no host crontab required (`docker compose up -d` starts it
with everything else). Writes land on the `backups` named volume, which lives in the same Docker
volume store as `db-data`; prune keeps the newest `BACKUP_KEEP` generations.

| Knob | Default | Effect |
|---|---|---|
| `BACKUP_KEEP` (`.env`) | `14` | generations retained (≈2 weeks of daily backups) |
| `BACKUP_INTERVAL_SECONDS` (add to `.env`) | `86400` | scheduler-only knob (not an app setting, hence not in `.env.example`); period between runs — keep ≤86400 so backups stay at-least-daily |

Check that backups are actually happening — `backup.py` exits non-zero on failure and the
scheduler logs and retries on the next tick:

```bash
docker compose -f docker-compose.prod.yml logs --tail=20 backup-scheduler
# backup-scheduler: starting backup at 2026-09-16T03:00:01Z
# backup ok: /backups/jobhunter-20260916-030001-000001.sql.gz (412.7 KiB), pruned 1 old backup(s)
```

Manual runs (e.g. before an upgrade) use the one-shot service:

```bash
docker compose -f docker-compose.prod.yml run --rm backup
```

Prefer a fixed wall-clock schedule over the loop, or run no scheduler container at all
(`docker compose up -d --scale backup-scheduler=0`)? Host cron is the alternative — this is the
equivalent of the shipped job:

```bash
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
| Metrics | `http://<host>:9464/metrics` — a dedicated internal port (see below) |
| Queue depth / dead letters | `GET /api/ops/status`, `GET /api/pipelines/stats`, `POST /api/ops/queue/{recover,retry-dead}` |
| Errors | `GET /api/logs?level=error`, and stdout (JSON when `LOG_JSON=true`) |
| Audit | `GET /api/account/audit` per user; `audit_logs` table for the whole instance |

### Metrics port

`METRICS_PORT` (default `9464`) makes the app process serve `/metrics` on a second port instead of
`GET /api/metrics`, which then answers `404 {"code":"metrics_moved"}`. Keep that port off the
internet: the listener runs *inside* the app process because the registry is in-process state, so a
sidecar cannot see it.

* `docker-compose.prod.yml`: the API and worker containers `expose:` `9464` (never `ports:`), so
  Prometheus scrapes `api:9464` and `worker:9464` over the compose network.
* Bare metal / systemd: leave `METRICS_HOST=127.0.0.1` and scrape through localhost (or an SSH
  tunnel); only set `0.0.0.0` on a network you control.
* `METRICS_TOKEN` is still checked on this port — set the same token in the scrape config.

Suggested alerts: readiness failing for 2 minutes, `jobhunter_pipeline_jobs_total{status="dead"}`,
queue age > 10 minutes, `jobhunter_ai_requests_total{status="breaker_open"}` rising, 5xx rate, disk
usage on the uploads/generated volumes, `jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}`
rising (proxy trust boundary not configured — §3.1), and `edge.registry.evicted` on
`GET /api/ops/status` rising (something is labelling metrics on unbounded data).

### Metric cardinality

The registry is in-process and a label value is a **permanent series**, so every label comes from a
bounded set — this is a memory property, not a tidiness one:

| Metric | Labels | Why it is bounded |
|---|---|---|
| `jobhunter_http_requests_total` | `method`, `path`, `status` | `path` is the **route template** (`/api/jobs/{job_id}`), resolved after routing. No `host` label: the `Host` header is caller-chosen. Unmatched URLs go through a 64-entry admission list and then count as `other` |
| `jobhunter_http_request_duration_seconds` | `method`, `path` | same |
| `jobhunter_http_*` (outbound client) | `host`, `status`/`kind`/`reason` | `host` is collapsed to the organisation-level domain of endpoints this deployment *intends* to call (`SOURCE_API_DOMAINS` + `OUTBOUND_ALLOWED_HOSTS` + the AI/funding endpoints); every host pulled out of a job feed counts as `other`. Per-source detail lives on `jobhunter_source_fetch_total{source=…}` |
| `jobhunter_rate_limited_total` | `path` | bounded as above |
| `jobhunter_edge_forwarded_for_total` | `reason` | four fixed reasons |

As a backstop, each metric name keeps at most `METRICS_MAX_SERIES_PER_METRIC` (512) series and evicts
the least recently updated one beyond that; `edge.registry` on `GET /api/ops/status` reports series
and evictions per metric. If you add a metric, label it from an enum, a route template or a config
value — never from a header, a raw path, a URL or an exception message.

### Workers, replicas and in-process state

`WEB_CONCURRENCY` (the image CMD passes it to `uvicorn --workers`) **defaults to 1** and
`docker-compose.prod.yml` pins it there. That is the least-surprising default for a self-hosted
product, because the app's budgets live in process memory:

| In-process state | What `WEB_CONCURRENCY=4` does to it |
|---|---|
| Rate limiter (`RateLimitMiddleware`) | each worker keeps its own hit log → the per-user/per-IP budget is effectively ×4 |
| AI token bucket (`app.core.rate_limiter`) and daily token budget | ×4 → the provider sees four times the intended RPM/spend |
| Per-user AI workflow overrides loaded at boot | an override saved through `PUT /api/settings` reaches the one worker that served the request; the other three keep the old value until they restart |
| Login-throttle windows (`/api/auth/login`) | ×4 attempts before a lockout |
| Metrics registry | only the worker that wins the `METRICS_PORT` bind serves it, so series cover one worker |
| Pipeline worker (`RUN_WORKER_IN_API=true`) | N copies of every pipeline claiming the same queue |

The app logs a warning naming these at startup when `WEB_CONCURRENCY > 1`
(`Settings.scaling_warnings()`), and `GET /api/ops/status` reports `edge.workers`.

Scale horizontally instead — it is the topology this product is built for:

```bash
# pipeline throughput: more worker containers (no API, no duplicated budgets)
docker compose -f docker-compose.prod.yml up -d --scale worker=3

# API throughput: more API replicas behind the load balancer, each with
# WEB_CONCURRENCY=1 and RUN_WORKER_IN_API=false
docker compose -f docker-compose.prod.yml up -d --scale api=3
```

Multiple *replicas* have the same in-process property as multiple workers (they are separate
processes). If you need limits that hold across replicas, put a shared limiter in front (the
load balancer's own rate limiting, or a Redis-backed middleware) — the per-user AI budgets then
belong in the database, which is a deliberate change rather than a configuration flag.

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
| `/api/metrics` returns 404 `metrics_moved` | `METRICS_PORT` is set — scrape `http://<host>:9464/metrics` instead |
| Nothing answers on the metrics port | `METRICS_ENABLED=true`, and (in a container) `METRICS_HOST=0.0.0.0`; a second worker that lost the bind logs a warning and stays quiet |
| Queue not draining | worker container not running (`RUN_WORKER_IN_API=false` on the API) |
| Upload rejected with 413 | file larger than `MAX_UPLOAD_MB`, or the proxy's `client_max_body_size`. The app answers 413 both on the declared `Content-Length` **and** while streaming (a chunked upload is counted as it arrives), so `{"code":"payload_too_large"}` is the app; an HTML 413 is the proxy |
| `audit_logs.ip` shows one address for every user | the proxy trust boundary is not configured (§3.1) — set `TRUSTED_PROXIES`/`FORWARDED_ALLOW_IPS` to the proxy's CIDR. Look for `jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}` |
| Everyone behind one office/NAT gets 429s together | anonymous traffic is limited per client IP by design. Signed-in users are limited per **user** (a verified token), so this only affects pre-auth endpoints — raise `API_RATE_LIMIT_PER_MINUTE` or terminate TLS closer to the client |
| 429s that do not match `API_RATE_LIMIT_PER_MINUTE` | `WEB_CONCURRENCY > 1` or several replicas: each process keeps its own limiter (§6). Also check `edge.rate_limiter.evictions` on `GET /api/ops/status` — if it climbs with real traffic, raise `RATE_LIMIT_MAX_KEYS` |
| Startup refuses with "… trusts every peer" | `TRUSTED_PROXIES`/`FORWARDED_ALLOW_IPS` contains `*` or `0.0.0.0/0`; list the proxy's actual address/CIDR instead (§3.1) |
| A metric you expect is missing from `/metrics` | series caps: each metric keeps `METRICS_MAX_SERIES_PER_METRIC` label combinations and evicts the least recently updated beyond that — check `edge.registry` on `GET /api/ops/status` |

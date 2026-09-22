# Production image: builds the SPA and serves it from the API process, so a
# deployment is a single container plus a database.
#
#   docker build -t jobhunter:2.0.0 .
#   docker run --env-file .env -p 8000:8000 jobhunter:2.0.0

# --------------------------------------------------------------------------- #
# Stage 1 — build the React SPA
# --------------------------------------------------------------------------- #
FROM node:20-alpine AS web

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------- #
# Stage 2 — python runtime
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend

# libpq5: the psycopg2 runtime. postgresql-client: pg_dump, used by the
# backup service in docker-compose.prod.yml (it exits non-zero without it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements-autofill.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Assisted Apply's browser: WITH_AUTOFILL=1 bakes Playwright plus a Chromium
# build (and its OS libraries) into the image — roughly 400 MB more. The compose
# files and plain `docker build` include it; pass WITH_AUTOFILL=0 to opt out.
ARG WITH_AUTOFILL=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$WITH_AUTOFILL" = "1" ]; then \
        pip install --no-cache-dir -r /app/backend/requirements-autofill.txt \
        && python -m playwright install --with-deps chromium \
        && chmod -R 755 /opt/ms-playwright; \
    fi

COPY backend/ /app/backend/
COPY --from=web /web/dist /app/frontend/dist

# Run as an unprivileged user; the writable data directories are chowned to it.
# Also ensure the playwright browser cache is readable by the unprivileged user
# when WITH_AUTOFILL=1 baked the browser in as root.
RUN useradd --create-home --uid 10001 jobhunter \
    && mkdir -p /app/backend/uploads /app/backend/generated /app/backend/artifacts/screenshots /opt/ms-playwright \
    && chown -R jobhunter:jobhunter /app /opt/ms-playwright \
    && chmod -R 755 /opt/ms-playwright
USER jobhunter

EXPOSE 8000
# Prometheus: metrics are served by the app process on its own port when
# METRICS_PORT is set. docker-compose.prod.yml only *exposes* it (never
# publishes it), so scrape it over the container network, e.g. api:9464.
EXPOSE 9464

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"

# WEB_CONCURRENCY controls uvicorn workers and defaults to 1 for two reasons:
# each API process also runs the in-process pipeline worker (RUN_WORKER_IN_API),
# and every budget the app enforces is per process — the rate limiter, AI token
# buckets and daily budgets, per-user AI overrides, the metrics registry — so N
# workers silently multiplies them by N. To scale, run the standalone worker
# container (docker-compose.prod.yml) with RUN_WORKER_IN_API=false and add API
# replicas behind a load balancer; see docs/DEPLOYMENT.md §6.
#
# FORWARDED_ALLOW_IPS is uvicorn's --proxy-headers trust boundary and defaults to
# loopback, i.e. X-Forwarded-For is ignored unless you name the proxy that sets
# it. It used to be '*', which let any direct client pick the address the app
# attributes the request to — per-IP rate limits, login-failure attribution and
# the `ip` column of audit rows. Set it to the reverse proxy's address or CIDR
# (and to the same value in TRUSTED_PROXIES, which the app's own resolver reads):
#
#   same host, nginx -> published port:  FORWARDED_ALLOW_IPS=<docker gateway, e.g. 172.17.0.1>
#   proxy in the compose network:        FORWARDED_ALLOW_IPS=172.18.0.0/16
#   cloud load balancer:                 FORWARDED_ALLOW_IPS=<LB subnet, e.g. 10.0.0.0/8>
#   no proxy at all:                     leave the default (peer address is the client)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --workers ${WEB_CONCURRENCY:-1}"]

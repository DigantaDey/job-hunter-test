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
    PYTHONPATH=/app/backend \
    WORKDIR=/app/backend

# libpq5: the psycopg2 runtime. postgresql-client: pg_dump, used by the
# backup service in docker-compose.prod.yml (it exits non-zero without it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY --from=web /web/dist /app/frontend/dist

# Run as an unprivileged user; the writable data directories are chowned to it.
RUN useradd --create-home --uid 10001 jobhunter \
    && mkdir -p /app/backend/uploads /app/backend/generated /app/backend/artifacts/screenshots \
    && chown -R jobhunter:jobhunter /app
USER jobhunter

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"

# WEB_CONCURRENCY controls uvicorn workers. It defaults to 1 because each API
# process also runs the in-process pipeline worker (RUN_WORKER_IN_API): N
# workers means N copies of every pipeline. To scale, run the standalone worker
# container (docker-compose.prod.yml) with RUN_WORKER_IN_API=false and only then
# raise this.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers ${WEB_CONCURRENCY:-1}"]

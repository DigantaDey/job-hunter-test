#!/usr/bin/env bash
# One-command development launcher: installs deps, builds the SPA, starts the API.
#
#   ./run.sh              # http://localhost:8000 (SPA + API, hot reload)
#   AUTO_INSTALL=0 ./run.sh
#
# For production use docker-compose.prod.yml (see docs/DEPLOYMENT.md).
set -euo pipefail

cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
AUTO_INSTALL="${AUTO_INSTALL:-1}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "→ Creating virtualenv…"
  python3 -m venv .venv
  PYTHON_BIN=".venv/bin/python"
fi

if [ "$AUTO_INSTALL" = "1" ]; then
  echo "→ Installing backend dependencies…"
  "$PYTHON_BIN" -m pip install -q -r backend/requirements-dev.txt
  if [ ! -d frontend/node_modules ]; then
    echo "→ Installing frontend dependencies…"
    (cd frontend && npm install --silent)
  fi
  echo "→ Building frontend…"
  (cd frontend && npm run build)
fi

if [ ! -f .env ]; then
  echo "→ No .env found — creating one with freshly generated secrets."
  "$PYTHON_BIN" - <<'PYEOF'
import secrets, pathlib
template = pathlib.Path(".env.example").read_text()
template = template.replace("SECRET_KEY=", f"SECRET_KEY={secrets.token_hex(32)}", 1)
template = template.replace("ENCRYPTION_KEY=", f"ENCRYPTION_KEY={secrets.token_hex(32)}", 1)
pathlib.Path(".env").write_text(template)
PYEOF
fi

echo "→ Starting backend on http://localhost:8000 (API docs at /api/docs)…"
cd backend
PYTHONPATH=. ../"$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

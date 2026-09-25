#!/usr/bin/env bash
# One-command development launcher: installs deps, builds the SPA, starts the API.
#
#   ./run.sh              # http://localhost:8000 (SPA + API, hot reload)
#   AUTO_INSTALL=0 ./run.sh     # skip all dependency installation
#   WITH_AUTOFILL=0 ./run.sh    # omit the optional browser stack
#   WITH_LAYA=0 ./run.sh        # omit the optional local decision engine (Laya)
#   LAYA_WARMUP=0 ./run.sh      # keep Laya but skip the one-off model download
#
# For production use docker-compose.prod.yml (see docs/DEPLOYMENT.md).
set -euo pipefail

cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
AUTO_INSTALL="${AUTO_INSTALL:-1}"
WITH_AUTOFILL="${WITH_AUTOFILL:-1}"
WITH_LAYA="${WITH_LAYA:-1}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "→ Creating virtualenv…"
  python3 -m venv .venv
  PYTHON_BIN=".venv/bin/python"
fi

if [ "$AUTO_INSTALL" = "1" ]; then
  echo "→ Installing backend dependencies…"
  "$PYTHON_BIN" -m pip install -q -r backend/requirements-dev.txt
  if [ "$WITH_AUTOFILL" = "1" ]; then
    echo "→ Installing backend Playwright and Chromium (may request sudo for OS libraries)…"
    "$PYTHON_BIN" -m pip install -q -r backend/requirements-autofill.txt
    "$PYTHON_BIN" -m playwright install --with-deps chromium
  fi
  if [ "$WITH_LAYA" = "1" ]; then
    echo "→ Installing the Laya local decision engine (fast typed ranking/classification; optional)…"
    # Optional by contract: a failed install (no torch wheel for this platform,
    # an offline mirror) must not stop the app from starting — every Laya route
    # degrades to the LLM path when the package is absent.
    if "$PYTHON_BIN" -m pip install -q -r backend/requirements-laya.txt; then
      # Warm the model cache once so ranking/classification work offline and the
      # first decision is instant. Non-fatal: a failed or skipped warm-up just
      # means the checkpoints download on first use (LAYA_WARMUP=0 skips it).
      if [ "${LAYA_WARMUP:-1}" = "1" ]; then
        echo "→ Warming the Laya model cache (one-off Hugging Face download of its checkpoints)…"
        "$PYTHON_BIN" backend/scripts/warm_laya.py --if-needed \
          || echo "  ! Laya warm-up failed — checkpoints will download on first use (auto mode falls back to the AI gateway meanwhile)."
      fi
    else
      echo "  ! Laya did not install — decision work will use the AI gateway instead (WITH_LAYA=0 silences this)."
    fi
  fi
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

cat <<'HINT'
→ First account (the owner): open http://localhost:8000 and fill in the
  bootstrap form — or, from another terminal:

    python backend/scripts/create_user.py --email you@example.com --password 'a-long-passphrase'

  Extra tester accounts need ALLOW_REGISTRATION=true in .env (then use the
  SPA's register link), or the same script with --role member.
HINT

echo "→ Starting backend on http://localhost:8000 (API docs at /api/docs)…"
cd backend
PYTHONPATH=. ../"$PYTHON_BIN" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

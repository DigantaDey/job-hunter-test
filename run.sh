#!/usr/bin/env bash
set -e
echo "→ Installing backend deps..."
pip install -q -r backend/requirements.txt
echo "→ Installing frontend deps..."
(cd frontend && npm install --silent)
echo "→ Building frontend..."
(cd frontend && npm run build)
echo "→ Starting backend on :8000..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --app-dir backend &
BACK_PID=$!
sleep 3
echo "✅ Backend at http://localhost:8000/api/docs"
echo "   Frontend built to frontend/dist served by FastAPI at http://localhost:8000"
wait $BACK_PID

"""Fake OpenAI-compatible provider for end-to-end verification.

Modes (set PROVIDER_MODE env var):
  normal      — /models 200 with valid key, chat works
  restricted  — /models 401 even with valid key (models listing disabled), chat works
  invalid     — everything 401
Logs every request's Authorization header to stdout so we can verify which key
the app actually sent.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.environ.get("PROVIDER_MODE", "normal")
VALID_KEY = "sk-e2e-valid-key-abcdef123456"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9101


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"PROVIDER {MODE}: {fmt % args}", flush=True)

    def _authed(self):
        ok = self.headers.get("Authorization", "") == f"Bearer {VALID_KEY}"
        print(f"PROVIDER auth check: {self.headers.get('Authorization')!r} -> {ok} [{self.command} {self.path}]", flush=True)
        return ok

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if MODE == "restricted":
                self._send(401, {"error": {"message": "models listing disabled for this key"}})
                return
            if self._authed():
                self._send(200, {"data": [{"id": "gpt-4o-mini"}]})
            else:
                self._send(401, {"error": {"message": "Incorrect API key provided"}})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._authed():
            self._send(401, {"error": {"message": "Incorrect API key provided"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._send(200, {
            "choices": [{"message": {"content": json.dumps({
                "score": 91, "reason": "AI: excellent match", "missing_skills": [],
                "strengths": ["python", "fastapi"], "breakdown": {},
                "recommendation": "HIGH PRIORITY", "recommendation_reason": "strong overlap"})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })


if __name__ == "__main__":
    print(f"fake provider listening on :{PORT} mode={MODE} valid_key={VALID_KEY}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

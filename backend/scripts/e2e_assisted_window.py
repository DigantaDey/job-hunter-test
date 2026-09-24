"""
Real-Chromium e2e for the Assisted Apply window + input recording.

Run it **on a desktop** (a machine where you can see windows), from anywhere:

    .venv/bin/python backend/scripts/e2e_assisted_window.py

It uses its own throwaway SQLite database under /tmp — your real data is never
touched — starts a local portal, and drives a genuine browser. What it proves,
against that live portal:

 1. a *headed* Chromium opens for the pass (mode=headed in the launch report),
    and a screenshot of the working window lands in /tmp/e2e_window.png;
 2. the pass fills the profile field, stops at the password wall — and the
    window STAYS OPEN (registered for the human step),
 3. the human types in that same window: a normal field is recorded for
    replay (checkpoint + learned answers), the password is refused everywhere,
 4. completing the handoff exports the cookie jar (persistence),
 5. the next pass ADOPTS the same window (same driver object), sees the
    portal's confirmation and closes the session — and only then the window.

On a server with no display the run stops at step 1 with the honest
``mode_reason`` — exactly what the product would report.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("OUTBOUND_ALLOW_PRIVATE", "true")
if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        or sys.platform in ("darwin", "win32")):
    # A machine that can show a window must show one for this test — even if
    # the deployment's .env would default to headless.
    os.environ.setdefault("AUTOFILL_BROWSER_MODE", "headed")
DB_PATH = "/tmp/e2e_assisted.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

# --------------------------------------------------------------------------- #
# A tiny local portal: sign-in wall + thank-you page, with a real cookie.
# --------------------------------------------------------------------------- #
APPLY_PAGE = """<!doctype html><html><body>
<h1>Apply — Acme</h1>
<form>
  <label>Email <input type="email" name="email" /></label>
  <label>How did you hear about us? <input type="text" name="how_did_you_hear" /></label>
  <label>Password <input type="password" name="password" /></label>
  <button type="submit">Sign in</button>
</form>
</body></html>"""

DONE_PAGE = """<!doctype html><html><body>
<h1>Thank you for applying</h1>
<p>We received your application.</p>
</body></html>"""


class Portal(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        page = {"/apply": APPLY_PAGE, "/step2": DONE_PAGE}.get(path)
        if page is None:
            self.send_response(404)
            self.end_headers()
            return
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "portal_sid=e2e-cookie-value; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
TARGET = f"http://127.0.0.1:{PORT}/apply"

# --------------------------------------------------------------------------- #
# App + fixtures
# --------------------------------------------------------------------------- #
from app.core.security import hash_password  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models.models import Job, Profile, User  # noqa: E402
from app.services import assisted_fill, live_browser  # noqa: E402
from app.services import browser_session as sessions
from app.services.autofill import browser_launch_plan  # noqa: E402
from app.services.user_settings import set_setting  # noqa: E402

init_db()
db = SessionLocal()
user = User(email="e2e@example.com", password_hash=hash_password("e2e-password-123"),
            role="owner", consents={})
db.add(user)
db.commit()
db.refresh(user)
db.add(Profile(user_id=user.id,
               data={"name": "Ada Lovelace", "firstName": "Ada", "lastName": "Lovelace",
                     "email": "ada@example.com"},
               layout={}))
job = Job(user_id=user.id, title="E2E Engineer", company="Acme", url=TARGET,
          external_id="e2e-1", dedupe_key="e2e-1", status="ready_to_apply",
          extra={"autofill_plan": {"fields": [
              {"name": "email", "profile_key": "email", "type": "email",
               "value": "ada@example.com", "value_source": "profile"}]}})
db.add(job)
db.commit()
db.refresh(job)

session, _created = sessions.start_session(db, user=user, job=job)
print(f"[1] session #{session.id} for {TARGET}")

plan = browser_launch_plan()
assert plan["available"], plan
assert plan["mode"] == "headed", f"expected a headed plan, got {plan}"
print(f"[2] launch plan: mode={plan['mode']} engine={plan['engine']} "
      f"reason={plan['mode_reason']!r}")

# --------------------------------------------------------------------------- #
# Pass 1: real browser, fills email, stops at the password wall — window stays
# --------------------------------------------------------------------------- #
policy = sessions.effective_policy(db, user.id)
driver = assisted_fill.build_driver(db, session, job, user=user, policy=policy)
assert driver is not None
result = asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver))
print(f"[3] pass 1: pause={result.get('pause')!r} filled={result.get('filled')} "
      f"browser={result.get('browser')}")
assert result.get("pause") == "login", result
assert result.get("filled") == ["email"], result
assert (result.get("browser") or {}).get("mode") == "headed", result.get("browser")
assert driver.closed is False, "the window must stay open for the human step"
assert live_browser.adopt(session.id) is driver, "the window must be registered"

# --------------------------------------------------------------------------- #
# The human works IN that window: a normal field is learned, the password is
# not; then signs in and the portal moves on (cookie set).
# --------------------------------------------------------------------------- #
async def human_step():
    win = live_browser.adopt(session.id)
    page = win._page
    await page.fill('input[name="how_did_you_hear"]', "Employee referral")
    await page.fill('input[name="password"]', "s3cret-hunter2")
    await page.screenshot(path="/tmp/e2e_window.png")
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("domcontentloaded")

asyncio.run(human_step())
db.refresh(session)
fields = (session.checkpoint or {}).get("fields") or {}
learned = sessions.learned_values(db, user_id=user.id, host=sessions.learning_host(session))
print(f"[4] recorded: how_did_you_hear -> {fields.get('how_did_you_hear')}")
print(f"    learned for host: {dict(learned)}")
assert fields.get("how_did_you_hear", {}).get("status") == "user_completed", fields
assert learned.get("how_did_you_hear") == "Employee referral", learned
assert "password" not in fields, "the password must never reach the checkpoint"
assert "password" not in learned, "the password must never be learned"
blob = json.dumps({"checkpoint": session.checkpoint, "fill": session.fill_values})
assert "s3cret-hunter2" not in blob, "the password value leaked into the session"
print("[5] password refused everywhere (checkpoint, working set, learned store) ✓")

# --------------------------------------------------------------------------- #
# Complete the handoff with persistence on: the cookie jar is exported.
# --------------------------------------------------------------------------- #
set_setting(db, user.id, "browser", "persist_session", True)
db.commit()
actions = sessions.pending_actions(db, user.id, session_id=session.id)
assert actions, "a login action must be pending"
async def finish():
    win = live_browser.adopt(session.id)
    state = await win.export_storage_state()
    assert state and state.get("cookies"), state
    result = sessions.persist_storage_state(db, session, state,
                                            policy=sessions.effective_policy(db, user.id))
    assert result["persisted"], result
    print(f"[6] cookie jar exported: {result['cookies']} cookie(s) persisted")
asyncio.run(finish())

sessions.complete_action(db, user=user, action=actions[0], note="signed in")
if session.state == "awaiting_user":
    sessions.transition(session, "paused", actor="user", reason="action_completed")
    db.commit()

# --------------------------------------------------------------------------- #
# Pass 2: the SAME window is adopted, sees the thank-you page, completes —
# and only then closes.
# --------------------------------------------------------------------------- #
driver2 = live_browser.adopt(session.id) or assisted_fill.build_driver(
    db, session, job, user=user, policy=sessions.effective_policy(db, user.id))
assert driver2 is driver, "pass 2 must reuse the window the human worked in"
result2 = asyncio.run(assisted_fill.run_pass(db, user=user, session=session, driver=driver2))
print(f"[7] pass 2: confirmed={result2.get('confirmed')!r} status={session.state} "
      f"closed={driver2.closed}")
assert result2.get("confirmed") == "submitted", result2
assert session.state == "completed"
assert driver2.closed is True, "a completed session's window must close"
assert live_browser.status(session.id) == {"open": False}

db.close()
server.shutdown()
print("\nALL E2E CHECKS PASSED")
print("screenshot: /tmp/e2e_window.png")

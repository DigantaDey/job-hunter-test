"""
Backend robustness: throttle state, the UTC daily cap, blocking DNS, log
injection, log field types and vault key rotation.

Six independent failure modes, each of which degraded quietly rather than
loudly:

1. the pre-auth login window wiped *everybody's* lockout when a spray of random
   addresses crossed a hardcoded threshold, and its threshold was not tunable;
2. the outreach daily cap measured "today" in the server's local timezone, so it
   drifted across DST and disagreed between workers;
3. the MX probe ran the blocking resolver *on* the event loop, freezing the
   worker for up to five seconds per contact;
4. the text log formatter interpolated the caller-supplied ``x-request-id``
   header raw, so a newline in it forged log records;
5. the JSON formatter emitted ``user_id`` as a number on the records that pass
   it explicitly (access log, audit) and as a string on the records that read it
   back off the ``LogContext`` — one field, two types, for the aggregator;
6. the vault's opportunistic re-key was never committed, and a ciphertext that
   no longer decrypted was swallowed into an empty password.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Iterator, List

import pytest

from app.core.config import settings
from tests.conftest import capture_json_logs


@pytest.fixture
def restore_tz() -> Iterator[None]:
    """Put the process timezone back the way we found it."""
    original = os.environ.get("TZ")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


# --------------------------------------------------------------------------- #
# 1. Login throttle: bounded per-key state, configurable threshold
# --------------------------------------------------------------------------- #
@pytest.fixture
def throttle_state() -> Iterator[object]:
    from app.api.routers import auth as auth_router

    auth_router._attempts.clear()
    try:
        yield auth_router._attempts
    finally:
        auth_router._attempts.clear()


def test_spraying_past_the_old_2000_threshold_leaves_other_lockouts_intact(throttle_state):
    """
    The old code was ``if len(_attempts) > 2000: _attempts.clear()`` — so an
    attacker who sprayed 2001 distinct random addresses reset the lockout of
    every account that was already throttled, and could then keep guessing.

    The window is a bounded LRU now, and a *full* window is pinned, so a spray
    costs only its own entries: the victim's throttle survives a flood smaller
    than the cap and one far larger than it.
    """
    from fastapi import HTTPException

    from app.api.routers import auth as auth_router

    def locked_out() -> bool:
        try:
            auth_router._throttle("victim@example.com")
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail["code"] == "too_many_attempts"
            return True
        return False

    # Lock the victim out properly first.
    for _ in range(auth_router._max_attempts()):
        auth_router._throttle("victim@example.com")
    assert locked_out()

    # Spray past the threshold the old implementation cleared at.
    for index in range(2_500):
        auth_router._throttle(f"spray{index}@attacker.example")
    assert locked_out(), "a flood under the cap must not disturb another user's lockout"

    # And past the map's own cap: partial windows are evicted, the lockout is not.
    for index in range(throttle_state.max_entries + 500):
        auth_router._throttle(f"flood{index}@attacker.example")
    assert locked_out(), "a flood larger than the cap must not evict an active lockout"

    assert len(throttle_state) <= throttle_state.max_entries + auth_router._max_attempts()
    assert throttle_state.stats()["evictions"] > 0
    assert "victim@example.com" in throttle_state.keys()


def test_login_threshold_and_window_come_from_settings(monkeypatch, throttle_state):
    """The 8-attempts/300s pair is configuration, not a constant in the source."""
    from fastapi import HTTPException

    from app.api.routers import auth as auth_router

    monkeypatch.setattr(settings, "max_login_attempts", 3)
    monkeypatch.setattr(settings, "login_throttle_window_seconds", 60)
    assert auth_router._max_attempts() == 3
    assert auth_router._window_seconds() == 60

    # Three attempts are now enough to trip it (eight would have been required).
    for _ in range(3):
        auth_router._throttle("tight@example.com")
    with pytest.raises(HTTPException):
        auth_router._throttle("tight@example.com")

    # And the entry's TTL follows the configured window, not the old hardcoded
    # 300 seconds: advance the clock past 60s and the throttle forgets it.
    real_clock = time.time
    monkeypatch.setattr(auth_router._attempts, "_clock", lambda: real_clock() + 61)
    auth_router._throttle("tight@example.com")  # must not raise

    # The ops snapshot reports the live configuration, so a tightened install is
    # visible on /api/ops/status rather than only in the source.
    state = auth_router.login_throttle_state()
    assert state["max_attempts"] == 3
    assert state["window_seconds"] == 60
    assert state["max_entries"] == settings.login_throttle_max_keys


def test_derived_key_cache_is_bounded_and_expires(monkeypatch):
    """
    ``security._key_cache`` is keyed on the *scope*, which embeds the user id —
    a plain dict there grows for the whole process lifetime, and a rotated
    VAULT_KEY would be shadowed by the old derivation forever.
    """
    from app.core import security
    from app.core.lru import BoundedTTLMap

    assert isinstance(security._key_cache, BoundedTTLMap)
    assert security._key_cache.default_ttl == settings.key_cache_ttl_seconds

    monkeypatch.setattr(security._key_cache, "max_entries", 4)
    for user_id in range(50):
        fernet = security.fernet_for(f"user:{user_id}:vault")
        assert fernet.decrypt(fernet.encrypt(b"roundtrip")) == b"roundtrip"
    assert len(security._key_cache) <= 4
    assert security._key_cache.stats()["evictions"] > 0
    # An evicted scope still works: it is simply re-derived on next use.
    assert security.fernet_for("user:0:vault").decrypt(
        security.encrypt_secret("still-readable", "user:0:vault").encode()) == b"still-readable"

    # The TTL is wired to the cache clock, so staleness is bounded even without
    # eviction pressure.
    now = {"t": 1_000.0}
    monkeypatch.setattr(security._key_cache, "_clock", lambda: now["t"])
    security.clear_key_cache()
    security.fernet_for("user:99:vault")
    assert "user:99:vault" in security._key_cache
    now["t"] += settings.key_cache_ttl_seconds + 1
    assert security._key_cache.get("user:99:vault") is None


def test_rotating_the_master_key_is_picked_up_after_the_cache_is_dropped(monkeypatch):
    """``clear_key_cache`` is what a live rotation calls; the TTL is the fallback."""
    from cryptography.fernet import InvalidToken

    from app.core import security

    token = security.encrypt_secret("under-the-old-key", "user:7:vault")
    monkeypatch.setattr(settings, "encryption_key", "a-completely-different-master-key-0123456789")
    security.clear_key_cache()
    with pytest.raises(InvalidToken):
        security.decrypt_secret(token, "user:7:vault", allow_legacy=False)


# --------------------------------------------------------------------------- #
# 2. Daily outreach cap measured in UTC
# --------------------------------------------------------------------------- #
def _email_row(db, user_id: int, sent_at: datetime):
    from app.models.models import Email

    row = Email(user_id=user_id, to_email="someone@example.com", subject="s", body="b",
                status="sent", sent_at=sent_at)
    db.add(row)
    db.commit()
    return row


def test_sent_today_counts_a_utc_day_not_a_local_one(db, owner, restore_tz):
    """
    ``sent_at`` is stored in UTC, so the window has to be too. The old
    ``date.today()`` midnight moved with ``TZ``: with the process in IST, a send
    made at 10:00 UTC was charged to the *previous* day for half the evening,
    handing out a second full budget.

    The day is derived from the current UTC clock rather than written out as a
    literal: the default-clock assertion below compares against *now*, so a
    hardcoded date turned this into a test that only passed until the midnight
    it was written around.
    """
    from app.models.models import User
    from app.services import outreach

    user = db.query(User).order_by(User.id).first()
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    morning = today + timedelta(hours=10)              # 10:00 UTC, today
    evening = today + timedelta(hours=23, minutes=30)  # 23:30 UTC, same UTC day
    previous = today - timedelta(seconds=1)            # just before the boundary
    _email_row(db, user.id, morning)
    _email_row(db, user.id, evening)
    _email_row(db, user.id, previous)

    now = today + timedelta(hours=23, minutes=45)
    for zone in ("UTC", "Asia/Kolkata", "America/Los_Angeles"):
        os.environ["TZ"] = zone
        time.tzset()
        assert outreach.sent_today(db, user.id, now=now) == 2, zone
        assert outreach.sent_today(db, user.id) == 2, f"{zone} (default clock)"
    time.tzset()


def test_daily_cap_rolls_over_at_utc_midnight(db, owner):
    """Mocked clock across the boundary: 23:59 UTC is one day, 00:01 is the next."""
    from app.models.models import User
    from app.services import outreach

    user = db.query(User).order_by(User.id).first()
    before = datetime(2026, 9, 15, 23, 59, 0)
    after = datetime(2026, 9, 16, 0, 1, 0)
    _email_row(db, user.id, before)
    _email_row(db, user.id, after)

    assert outreach.sent_today(db, user.id, now=before) == 1
    assert outreach.sent_today(db, user.id, now=after) == 1          # the new day starts empty...
    assert outreach.sent_today(db, user.id, now=after + timedelta(hours=1)) == 1

    # The window is a *range*: a row stamped outside today never counts towards
    # it, so a skewed clock cannot permanently consume the budget.
    _email_row(db, user.id, datetime(2026, 9, 20, 12, 0, 0))
    assert outreach.sent_today(db, user.id, now=after) == 1


def test_compliance_gate_uses_the_utc_count(db, owner, full_consent):
    """The cap the send gate reads is the same UTC number the API reports."""
    from app.models.models import User
    from app.services import outreach

    user = db.query(User).order_by(User.id).first()
    _email_row(db, user.id, datetime.utcnow() - timedelta(minutes=5))   # counts towards the cap

    pending = _email_row(db, user.id, datetime.utcnow() - timedelta(minutes=4))
    pending.status = "pending_approval"
    pending.sent_at = None
    db.commit()

    report = outreach.compliance_report(db, user, pending)
    assert report["checks"]["sent_today"] == 1
    assert report["checks"]["daily_limit"] == settings.email_daily_limit
    assert report["checks"]["under_daily_limit"] is True


# --------------------------------------------------------------------------- #
# 3. MX lookup must not run on the event loop
# --------------------------------------------------------------------------- #
def test_mx_lookup_runs_off_the_event_loop(monkeypatch):
    """A resolver that blocks for a second must not stop the loop ticking."""
    from app.services import contact_discovery as cd

    calls: List[str] = []

    def blocking_resolve(domain, rdtype=None, lifetime=5):
        calls.append(domain)
        time.sleep(1.0)
        return [object()]

    monkeypatch.setattr(settings, "contact_verify_mx", True)
    monkeypatch.setattr("dns.resolver.resolve", blocking_resolve)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        started = time.perf_counter()
        result = await cd.verify_email_async("jane.doe@slow.example")
        elapsed = time.perf_counter() - started
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return result, elapsed, ticks

    result, elapsed, ticks = asyncio.run(scenario())

    assert calls == ["slow.example"], "the resolver must actually have been used"
    assert result["mx_ok"] is True and result["score"] == pytest.approx(0.9)
    assert elapsed >= 1.0, "the lookup should still have waited for the answer"
    # The whole point: the loop kept running while the resolver blocked.
    assert ticks > 20, f"event loop stalled during the MX lookup (only {ticks} ticks)"


def test_mx_lookup_timeout_is_bounded(monkeypatch):
    """A blackholed resolver must not hold the caller for longer than the budget."""
    from app.services import contact_discovery as cd

    def never_returns(domain, rdtype=None, lifetime=5):
        time.sleep(30)
        return []

    monkeypatch.setattr(settings, "contact_verify_mx", True)
    monkeypatch.setattr(settings, "contact_mx_timeout_seconds", 0.5)
    monkeypatch.setattr("dns.resolver.resolve", never_returns)

    async def scenario():
        started = time.perf_counter()
        verdict = await cd.resolve_mx("blackhole.example")
        return verdict, time.perf_counter() - started

    verdict, elapsed = asyncio.run(scenario())
    assert verdict is None, "an unfinished lookup is unknown, never invalid"
    assert elapsed < 5.0, f"abandoning the thread took {elapsed:.1f}s"


def test_sync_verify_email_refuses_to_block_a_running_loop(monkeypatch):
    """
    The sync helper is still there for scripts and tests, but from inside a loop
    it declines and says so in the result instead of freezing every other task.
    """
    from app.services import contact_discovery as cd

    def slow(domain, rdtype=None, lifetime=5):
        time.sleep(2.0)
        return [object()]

    monkeypatch.setattr(settings, "contact_verify_mx", True)
    monkeypatch.setattr("dns.resolver.resolve", slow)

    async def scenario():
        started = time.perf_counter()
        return cd.verify_email("jane.doe@slow.example"), time.perf_counter() - started

    result, elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, "verify_email() blocked the loop it was called from"
    assert result["mx_ok"] is None
    assert result["mx"] == "skipped_event_loop"
    assert result["syntax_ok"] is True


def test_sync_verify_email_still_probes_mx_outside_a_loop(monkeypatch):
    """...and it has not lost the ability to verify when there is no loop."""
    from app.services import contact_discovery as cd

    monkeypatch.setattr(settings, "contact_verify_mx", True)
    monkeypatch.setattr("dns.resolver.resolve", lambda domain, rdtype=None, lifetime=5: [object()])
    result = cd.verify_email("jane.doe@acme.io")
    assert result["mx_ok"] is True and result["score"] == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# 4. Log injection through x-request-id
# --------------------------------------------------------------------------- #
FORGED = "x\n2026-01-01T00:00:00 INFO    app.audit                     audit [user_id=1]\tfake-logout"
#: Short enough to survive the 64-character cap, so the escaped payload itself
#: can be asserted on rather than just the truncation marker.
FORGED_SHORT = "x\nfake-logout"


def _text_line(record: logging.LogRecord) -> str:
    from app.core.logging import ContextFilter, TextFormatter

    ContextFilter().filter(record)
    return TextFormatter().format(record)


def test_forged_request_id_cannot_create_a_second_log_line():
    """
    A caller-supplied header with a newline used to be interpolated verbatim, so
    one request could write arbitrary extra records into the log — including a
    fake ``vault.viewed`` or a fake successful logout.
    """
    from app.core.logging import LogContext

    with LogContext(request_id=FORGED_SHORT, user_id=1):
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "did a thing", (), None)
        line = _text_line(record)

    lines = [entry for entry in line.splitlines() if entry.strip()]
    assert len(lines) == 1, f"the forged header produced {len(lines)} log lines:\n{line}"
    assert lines[0].endswith("[user_id=1]"), lines[0]
    # The attack is still *visible* — escaped, not silently dropped.
    assert "x\\nfake-logout" in lines[0], lines[0]


def test_a_long_forged_id_is_escaped_and_capped():
    from app.core.logging import LogContext

    with LogContext(request_id=FORGED, user_id=1):
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "did a thing", (), None)
        line = _text_line(record)

    assert line.count("\n") == 0, line
    assert "…" in line, "an over-long id should be visibly truncated"
    assert len(line) < 200


def test_control_characters_are_escaped_not_just_stripped():
    from app.core.logging import sanitize_log_value

    assert sanitize_log_value("a\nb") == "a\\nb"
    assert sanitize_log_value("a\tb") == "a\\tb"
    assert sanitize_log_value("a\x00b") == "a\\x00b"
    assert sanitize_log_value("a\u2028b") == "a\\x2028b"
    assert sanitize_log_value("plain") == "plain"
    assert sanitize_log_value(42) == 42          # non-strings pass through
    assert sanitize_log_value(None) is None
    assert len(sanitize_log_value("y" * 5000, limit=64)) == 64


def test_json_mode_stays_parseable_with_a_forged_id():
    from app.core.logging import ContextFilter, JsonFormatter, LogContext

    with LogContext(request_id=FORGED, user_id=7):
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "did a thing", (), None)
        ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))

    assert payload["user_id"] == 7        # a number, not "7" — one type per field
    assert isinstance(payload["user_id"], int)
    assert "\n" not in payload["request_id"]


def test_request_id_header_is_sanitized_at_the_edge(client, caplog):
    """
    End to end: the header is scrubbed before it is echoed, logged or stored on
    an audit row — a raw CR/LF there is also a response-splitting primitive.
    """
    from app.core.middleware import sanitize_request_id

    assert sanitize_request_id("trace-me-123") == "trace-me-123"
    assert sanitize_request_id(FORGED).count("\n") == 0
    assert sanitize_request_id("\n\r") == ""

    with caplog.at_level(logging.WARNING):
        response = client.get("/api/health", headers={"X-Request-ID": FORGED})
    assert response.status_code == 200

    echoed = response.headers.get("X-Request-ID") or ""
    assert "\n" not in echoed and "\r" not in echoed and "\t" not in echoed

    records = [r for r in caplog.records if getattr(r, "request_id", None)]
    assert records, "the request should have been logged with an id"
    for record in records:
        assert "\n" not in str(record.request_id)
        assert _text_line(record).count("\n") == 0


# --------------------------------------------------------------------------- #
# 4b. One field, one JSON type: user_id is a number on every path
# --------------------------------------------------------------------------- #
def _json_log(**extra) -> dict:
    """Log one record through the JSON pipeline and return the parsed payload."""
    with capture_json_logs("app.test.json") as sink:
        logging.getLogger("app.test.json").info("did a thing", extra=extra)
    assert len(sink.payloads) == 1, sink.payloads
    return sink.payloads[0]


def test_user_id_is_an_int_on_both_json_paths():
    """
    JSON mode emits ``user_id`` verbatim, so the two producers must agree: the
    explicit ``extra={"user_id": ...}`` (access log, audit) and a bare record
    that reads the value off the ``LogContext`` (auth, worker). A ``7`` on one
    path and a ``"7"`` on the other is one field with two types at the
    aggregator, where ``user_id: 7`` then fails to match the ``"7"`` records.
    """
    from app.core.logging import LogContext

    with LogContext(request_id="req-1", user_id=7):
        explicit = _json_log(user_id=7)
        ambient = _json_log()

    for payload in (explicit, ambient):
        assert isinstance(payload["user_id"], int), payload
        assert payload["user_id"] == 7, payload
    assert ambient["user_id"] == explicit["user_id"]
    # An opaque, caller-supplied token is not a numeric id — it stays a string.
    assert isinstance(ambient["request_id"], str)


def test_the_filter_coerces_a_stringified_user_id_without_raising():
    """
    The filter is the last line of defence: a caller that stringifies the id
    anyway (or a job row whose id arrives as ``"7"``) must still emit a number,
    and a value that is not an id at all is passed through rather than raising —
    a logging filter that throws takes the log line with it.
    """
    from app.core.logging import LogContext

    with LogContext(user_id=7):
        stringified = _json_log(user_id="7")
        not_an_id = _json_log(user_id="system")

    assert isinstance(stringified["user_id"], int) and stringified["user_id"] == 7
    assert not_an_id["user_id"] == "system"


def test_no_user_id_field_is_invented_when_there_is_no_user():
    """A record logged outside any request/job context stays anonymous."""
    from app.core.logging import user_id_var

    token = user_id_var.set(None)
    try:
        assert "user_id" not in _json_log()
    finally:
        user_id_var.reset(token)


# --------------------------------------------------------------------------- #
# 5. Vault: the re-key is persisted, a wrong key fails loudly
# --------------------------------------------------------------------------- #
def _owner_id(db) -> int:
    from app.models.models import User

    return int(db.query(User).order_by(User.id).first().id)


def _legacy_entry(db, user_id: int, domain: str = "legacy.example.com", password: str = "legacy-password"):
    from app.core.security import encrypt_secret
    from app.models.models import VaultEntry

    entry = VaultEntry(user_id=user_id, domain=domain, username="legacy@example.com",
                       password_enc=encrypt_secret(password, "global"))
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def test_rekey_is_committed_without_the_caller_doing_it(db, owner):
    """
    ``reveal_password`` upgraded a legacy ciphertext in memory and nothing
    committed it, so the entry stayed on the legacy key forever. A *fresh*
    session is the honest check: if the upgrade reached the database, the new
    session sees a per-user ciphertext and no caller had to commit anything.
    """
    from app.core.security import decrypt_secret, needs_reencrypt
    from app.db import SessionLocal
    from app.models.models import VaultEntry
    from app.services.vault import reveal_password

    user_id = _owner_id(db)
    entry = _legacy_entry(db, user_id)
    scope = f"user:{user_id}:vault"
    assert needs_reencrypt(entry.password_enc, scope) is True

    assert reveal_password(entry) == "legacy-password"

    fresh = SessionLocal()
    try:
        reloaded = fresh.query(VaultEntry).filter(VaultEntry.id == entry.id).one()
        assert needs_reencrypt(reloaded.password_enc, scope) is False
        assert decrypt_secret(reloaded.password_enc, scope) == "legacy-password"
    finally:
        fresh.close()


def test_the_csv_export_path_also_commits_the_rekey(db, owner):
    """
    The exports were named in the bug as a path that never committed: they read
    every entry and returned a string, so a legacy entry was re-keyed in memory
    and dropped. A download must upgrade what it reads, like any other read.
    """
    from app.core.security import needs_reencrypt
    from app.db import SessionLocal
    from app.models.models import VaultEntry
    from app.services.vault import export_chrome_csv, list_vault_entries

    user_id = _owner_id(db)
    entry = _legacy_entry(db, user_id, domain="exported.example.com")
    scope = f"user:{user_id}:vault"

    result = export_chrome_csv(list_vault_entries(db, user_id))
    assert "legacy-password" in result.text and result.undecryptable == ()

    fresh = SessionLocal()
    try:
        reloaded = fresh.query(VaultEntry).filter(VaultEntry.id == entry.id).one()
        assert needs_reencrypt(reloaded.password_enc, scope) is False
    finally:
        fresh.close()


def test_a_commit_failure_does_not_lose_the_read(db, owner, caplog):
    """Persisting the upgrade is best-effort; the password the user asked for is not."""
    from app.services.vault import reveal_password

    entry = _legacy_entry(db, _owner_id(db), domain="uncommittable.example.com")
    real_commit = db.commit

    def failing_commit():
        raise RuntimeError("database is read-only (simulated)")

    db.commit = failing_commit
    try:
        with caplog.at_level(logging.ERROR, logger="app.vault"):
            assert reveal_password(entry, db=db) == "legacy-password"
    finally:
        db.commit = real_commit
    assert any("could not persist the re-encryption" in r.getMessage() for r in caplog.records)


def test_wrong_vault_key_raises_and_is_logged(db, owner, monkeypatch, caplog):
    """
    An undecryptable entry used to come back as ``""`` — indistinguishable from a
    credential that was never set. It must raise, and say so at ERROR.
    """
    from app.core import security
    from app.services.vault import VaultDecryptionError, reveal_password, save_vault_entry

    user_id = _owner_id(db)
    entry = save_vault_entry(db, user_id, "rotated.example.com", "me@example.com", "correct-horse-battery")

    monkeypatch.setattr(settings, "encryption_key", "a-different-master-key-0123456789abcdef")
    security.clear_key_cache()

    with caplog.at_level(logging.ERROR, logger="app.vault"):
        with pytest.raises(VaultDecryptionError) as excinfo:
            reveal_password(entry, db=db)
    assert "cannot be decrypted" in str(excinfo.value)
    assert excinfo.value.entry_id == entry.id
    assert any(r.levelno >= logging.ERROR and "VAULT_KEY" in r.getMessage() for r in caplog.records)

    security.clear_key_cache()


def test_reveal_endpoint_reports_an_undecryptable_entry(client, auth, db, monkeypatch, caplog):
    """The API shows a named failure instead of an empty password field."""
    from app.core import security
    from app.models.models import AuditLog

    created = client.post("/api/vault", json={"domain": "rotated.example.com",
                                             "username": "me@example.com",
                                             "password": "correct-horse-battery"}, headers=auth)
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]

    monkeypatch.setattr(settings, "encryption_key", "a-different-master-key-0123456789abcdef")
    security.clear_key_cache()
    try:
        with caplog.at_level(logging.ERROR, logger="app.vault"):
            response = client.get(f"/api/vault/{entry_id}/reveal", headers=auth)
    finally:
        security.clear_key_cache()

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "vault_entry_undecryptable"
    assert response.json()["detail"]["entry_id"] == entry_id
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    actions = {row.action for row in db.query(AuditLog).all()}
    assert "vault.reveal_failed" in actions


def test_exports_omit_and_report_undecryptable_entries(client, auth, db, monkeypatch):
    """
    A CSV download cannot render an error, so the unreadable entry is *omitted*
    (never written as a blank password, which a browser importer would happily
    store) and the count is reported in a header and on the audit row.
    """
    from app.core import security
    from app.models.models import AuditLog, VaultEntry
    from app.services.vault import _encrypt

    good = client.post("/api/vault", json={"domain": "good.example.com", "username": "g@example.com",
                                          "password": "readable-password"}, headers=auth)
    assert good.status_code == 201, good.text
    bad = client.post("/api/vault", json={"domain": "bad.example.com", "username": "b@example.com",
                                          "password": "unreadable-password"}, headers=auth)
    assert bad.status_code == 201, bad.text

    # Re-encrypt one entry under a key this process no longer holds, then put
    # the real key back — so exactly one entry is unreadable.
    original_key = settings.encryption_key
    monkeypatch.setattr(settings, "encryption_key", "a-retired-master-key-0123456789abcdef")
    security.clear_key_cache()
    row = db.query(VaultEntry).filter(VaultEntry.domain == "bad.example.com").one()
    row.password_enc = _encrypt(_owner_id(db), "unreadable-password")
    db.commit()
    monkeypatch.setattr(settings, "encryption_key", original_key)
    security.clear_key_cache()
    try:
        chrome = client.get("/api/vault/export/chrome", headers=auth)
        apple = client.get("/api/vault/export/apple", headers=auth)
    finally:
        security.clear_key_cache()

    for response in (chrome, apple):
        assert response.status_code == 200
        assert response.headers["X-Vault-Undecryptable"] == "1"
        assert "good.example.com" in response.text
        assert "readable-password" in response.text
        # Not a blank password, not a placeholder — the row is simply absent.
        assert "bad.example.com" not in response.text
        assert "unreadable-password" not in response.text
    assert chrome.text.count("\r\n") == 2          # header + the one readable row

    exported = db.query(AuditLog).filter(AuditLog.action == "vault.exported").all()
    assert exported and any((row.detail or {}).get("undecryptable") for row in exported)


def test_gdpr_export_marks_undecryptable_entries(client, full_consent, db, monkeypatch):
    """A data-subject export must not flatten a failure into an empty password."""
    from app.core import security

    created = client.post("/api/vault", json={"domain": "rotated.example.com",
                                             "username": "me@example.com",
                                             "password": "correct-horse-battery"},
                          headers=full_consent)
    assert created.status_code == 201, created.text

    monkeypatch.setattr(settings, "encryption_key", "a-different-master-key-0123456789abcdef")
    security.clear_key_cache()
    try:
        body = client.get("/api/account/export", headers=full_consent).json()
    finally:
        security.clear_key_cache()

    entry = body["vault"][0]
    assert entry["password"] is None
    assert entry["password_error"] == "undecryptable"


def test_a_readable_vault_is_unchanged_by_all_this(client, auth):
    """The happy path still returns the real password everywhere."""
    created = client.post("/api/vault", json={"domain": "fine.example.com",
                                             "username": "me@example.com",
                                             "password": "a-strong-password"}, headers=auth)
    entry_id = created.json()["id"]
    assert client.get(f"/api/vault/{entry_id}/reveal", headers=auth).json()["password"] == "a-strong-password"
    chrome = client.get("/api/vault/export/chrome", headers=auth)
    assert "a-strong-password" in chrome.text
    assert chrome.headers["X-Vault-Undecryptable"] == "0"

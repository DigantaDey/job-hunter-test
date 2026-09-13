"""v2.2.3 — a queued discovery run gets the real AI verdict for its top slice.

The gap this closes: :func:`app.services.discovery.discover_for_user` has always
supported a guardrailed AI verdict for the top ``AI_RESCORE_TOP`` (12)
candidates — ``score_source='ai'`` with evidence, and a *hard* dependency on the
model (transient outage → the queued run pauses and re-executes when the
provider is back; needs-action → it dead-letters with the diagnosis). But its
only caller, ``handlers.handle_discovery``, never loaded the user's profile, so
``use_ai=bool(profile_data)`` was permanently ``False``: every discovered job was
a keyword-overlap estimate, and the AI top slice with its whole pause contract
was dead code. The AI verdict existed only per job
(``GET /api/jobs/{job_id}/intelligence``, Pro).

The contract under test:

* **Pro + profile** → the top 12 of the batch carry ``score_source='ai'`` (with
  evidence and the model's reason), the rest stay ``preliminary``, and the run's
  report says ``ai_rescore = {"enabled": true, "skipped": null, "scored": 12}``.
* **Free + profile** → the run completes, *zero* AI calls reach the provider
  (the profile is withheld wholesale, which is also what switches off the AI
  company-size verdict), every row is preliminary, and the report says
  ``skipped: "plan_free"``. A free board is not a back door around the plan gate
  ``/intelligence`` enforces.
* **No profile / empty profile** → zero AI calls, all preliminary,
  ``skipped: "no_profile"``.
* **Transient outage mid-slice** → the run *pauses* (not failed, not dead) and
  persists **no** job rows for that attempt; after the provider recovers and the
  watchdog drains, the run completes with the rows created exactly once and the
  top slice AI-scored.
* **v2.2.2 interaction** → a run that survived a pause/resume cycle with zero
  handler failures still has its whole *failure* budget: the first real failure
  retries, and N consecutive real failures dead-letter on the Nth.
* **Needs-action (401-style)** → the run dead-letters with the diagnosis in
  ``error``.
* **Auto-mode notification** → an all-AI top three drops the keyword-overlap
  caveat; a mixed run keeps it.

Every AI test drives the REAL gateway against the local scripted
OpenAI-compatible provider (the hermetic ``real_ai`` convention from
``test_ai_pause_resume.py``) and a faked source fan-out — no outbound network,
no stubs on the wire path.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
from conftest import stub_ai_response  # top-level conftest — see the note in test_ai_pause_resume

from app.services.sources import Posting

#: 16 candidates: the first 12 overlap the stub profile strongly (preliminary
#: ~83), the last 4 do not overlap it at all (~55). The deterministic pre-rank
#: therefore picks an unambiguous top slice, and the AI verdict (88 from the
#: scripted provider) outranks every estimate — so the three highest scorers an
#: auto-mode notification surfaces are AI rows.
STRONG_COUNT = 12
WEAK_JDS = (
    "Warehouse operations supervisor. Forklift certification, inventory control and shift scheduling.",
    "Field marketing manager. Trade shows, printed collateral and regional event logistics.",
    "Customer support specialist. Zendesk, telephone queue management and macro writing.",
    "Payroll administrator. ADP Workforce Now, garnishments and multi-state tax filings.",
)
CANDIDATES = STRONG_COUNT + len(WEAK_JDS)
#: ``AI_RESCORE_TOP`` — asserted against the constant so the tests cannot drift
#: from the product's own cost control.
RESCORE_TOP = 12


# --------------------------------------------------------------------------- #
# Isolation: the gateway's breaker/probe caches and the API's search-context
# cache are process-global. A leaked entry would let one test answer another's
# AI call — or mask an outage the test is asserting on.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_ai_state():
    import app.api.deps as deps
    import app.services.ai_client as ai_client

    def _clear() -> None:
        ai_client._breakers.clear()
        ai_client._PING_CACHE.clear()
        deps._context_cache.clear()

    _clear()
    yield
    _clear()


def _behavior() -> Dict[str, Any]:
    import conftest

    return conftest.ScriptedAIHandler.behavior


def _wire() -> List[Dict[str, Any]]:
    """Every POST the scripted provider received (the wire ground truth)."""
    return _behavior()["requests"]


def _prompt_of(request: Dict[str, Any]) -> str:
    return " ".join(str(m.get("content") or "") for m in request.get("messages") or []).lower()


def _calls(marker: str) -> List[Dict[str, Any]]:
    return [r for r in _wire() if marker in _prompt_of(r)]


def _scoring_calls() -> List[Dict[str, Any]]:
    """The scoring rubric prompt — one wire call per AI-scored candidate."""
    return _calls("score how well this candidate")


def _classify_calls() -> List[Dict[str, Any]]:
    return _calls("classify company size")


def _set_outage(status: int = 503, *, after: Optional[int] = None) -> None:
    """The scripted provider goes down (``after`` good responses, or at once)."""
    behavior = _behavior()
    behavior["status"] = status
    behavior["fail_after"] = behavior.get("good", 0) if after is None else after


def _set_recovery() -> None:
    _behavior()["fail_after"] = None


def _sync(db) -> None:
    """End any open read transaction so rows written by the worker are seen."""
    db.rollback()
    db.expire_all()


# --------------------------------------------------------------------------- #
# A deterministic source fan-out (discovery's only non-AI external boundary)
# --------------------------------------------------------------------------- #
def _install_sources(monkeypatch, count: int = CANDIDATES, *, no_descriptions: bool = False) -> None:
    """Replace the source registry's fan-out with ``count`` fixed postings.

    ``discover_for_user`` calls ``sources.fetch_all`` through the module object,
    so patching the module attribute is enough — and it keeps the run hermetic
    (``LIVE_SCRAPING_ENABLED=false`` already keeps the form detector off the
    network).

    ``no_descriptions`` models a source that returns titles and URLs only (every
    adapter defaults a missing description to ``""``) — postings with nothing to
    score against.
    """
    from app.services import sources as sources_registry

    async def fake_fetch_all(keywords, *, limit=40, since_hours=168, sources=None,
                             board_tokens=None, timeout_seconds=45.0):
        postings = []
        for index in range(count):
            if no_descriptions:
                description = ""
            elif index < STRONG_COUNT:
                description = (
                    f"Backend Engineer, team {index}. Python, FastAPI and PostgreSQL services on "
                    "AWS, packaged with Docker and orchestrated with Kubernetes. 5+ years of "
                    "experience building payments platforms at scale. You own the API end to end "
                    "and work closely with a React front end."
                )
            else:
                description = WEAK_JDS[index - STRONG_COUNT]
            postings.append(Posting(
                title=(f"Backend Engineer, Team {index}" if index < STRONG_COUNT
                       else WEAK_JDS[index - STRONG_COUNT].split(".")[0].title()),
                company=(f"Paystack {index}" if index < STRONG_COUNT else f"Northwind {index}"),
                url=f"https://jobs.example.com/posting-{index}",
                source="lever",
                external_id=f"ext-{index}",
                location="Remote",
                description=description,
                posted_at=datetime.utcnow() - timedelta(hours=index),
            ))
        report = {"requested": ["lever"], "ok": {"lever": len(postings)}, "errors": {},
                  "skipped": {}, "total": len(postings)}
        return postings, report

    monkeypatch.setattr(sources_registry, "fetch_all", fake_fetch_all)


def _strong_keys() -> set:
    """Dedupe keys of the candidates the pre-rank must put in the top slice."""
    return {f"ext-{index}" for index in range(STRONG_COUNT)}


# --------------------------------------------------------------------------- #
# Accounts, profiles and queued runs
# --------------------------------------------------------------------------- #
def _register(client, email: str) -> Dict[str, Any]:
    response = client.post("/api/auth/register",
                           json={"email": email, "password": "discovery-password-123",
                                 "name": email.split("@")[0]})
    assert response.status_code == 201, response.text
    return response.json()


def _user_id(db, email: str) -> int:
    from app.models.models import User

    _sync(db)
    user = db.query(User).filter(User.email == email).first()
    assert user is not None
    return int(user.id)


def _paid_user(client, db, email: str, plan: str = "pro") -> Dict[str, Any]:
    """A non-owner on a paid plan (the recipe ``test_ai_token_budgets`` uses)."""
    from app.models.models import Subscription

    tokens = _register(client, email)
    user_id = _user_id(db, email)
    db.add(Subscription(user_id=user_id, plan=plan, status="active", provider="manual",
                        current_period_start=datetime.utcnow()))
    db.commit()
    return {"id": user_id, "email": email,
            "auth": {"Authorization": f"Bearer {tokens['access_token']}"}}


def _free_user(client, db, email: str) -> Dict[str, Any]:
    """The same account with no subscription — the plan is the only variable."""
    tokens = _register(client, email)
    return {"id": _user_id(db, email), "email": email,
            "auth": {"Authorization": f"Bearer {tokens['access_token']}"}}


def _profile(db, user_id: int, data: Optional[Dict[str, Any]] = None) -> None:
    """The user's master profile row — the same shape the upload flow writes."""
    from app.models.models import Profile

    db.add(Profile(user_id=user_id, data=stub_ai_response("parse", "") if data is None else data,
                   layout={}, extraction_source="ai"))
    db.commit()


def _enqueue_discovery(db, user_id: int, dedupe_key: str, *, trigger: str = "manual",
                       max_attempts: int = 3, **payload: Any):
    from app.services.job_queue import enqueue

    item = enqueue(db, user_id=user_id, pipeline="discovery", max_attempts=max_attempts,
                   dedupe_key=dedupe_key,
                   payload={"keywords": ["python backend"], "freshness_hours": 168, "limit": 30,
                            "live_enabled": True, "sources": ["lever"], "trigger": trigger,
                            **payload})
    assert item is not None
    _sync(db)
    return item


async def _run(item_id: int) -> None:
    """Execute one queued item exactly like a worker slot does."""
    from app.worker import Worker

    await Worker(pipelines=["discovery"])._run_item(item_id, "discovery")


def _jobs(db, user_id: int) -> List[Any]:
    from app.models.models import Job

    _sync(db)
    return db.query(Job).filter(Job.user_id == user_id).order_by(Job.id).all()


def _provenance(rows) -> Counter:
    return Counter(str(row.score_source or "") for row in rows)


def _report(db, item) -> Dict[str, Any]:
    """The discovery report, as it rides in the queue item's result JSON."""
    _sync(db)
    db.refresh(item)
    payload = dict(item.payload or {})
    assert "result" in payload, payload
    return dict(payload["result"])


# --------------------------------------------------------------------------- #
# 1. Pro + profile + AI up → the top slice is the model's verdict
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_pro_run_ai_scores_the_top_slice_and_reports_provenance(
        client, auth, db, provider_owner, monkeypatch):
    """Triggered through the product's own endpoint, executed by the worker.

    16 candidates → exactly ``AI_RESCORE_TOP`` rows carry the AI verdict (with
    evidence and the model's reason), the remaining 4 stay preliminary, and the
    report says the slice ran and how many rows it produced.
    """
    from app.services.discovery import AI_CLASSIFY_TOP, AI_RESCORE_TOP

    assert AI_RESCORE_TOP == RESCORE_TOP, "the tests must not drift from the product's cost control"
    _install_sources(monkeypatch)
    pro = _paid_user(client, db, "pro-discovery@example.com")
    _profile(db, pro["id"])
    _sync(db)

    triggered = client.post("/api/jobs/discover",
                            json={"keywords": ["python backend"], "freshness_hours": 168,
                                  "limit": 30, "live_enabled": True, "sources": ["lever"]},
                            headers=pro["auth"])
    assert triggered.status_code == 200, triggered.text
    item_id = triggered.json()["pipeline_job_id"]
    assert item_id, triggered.json()

    await _run(item_id)

    from app.models.models import PipelineJob

    _sync(db)
    item = db.query(PipelineJob).filter(PipelineJob.id == item_id).first()
    assert item.status == "done", f"{item.status}: {item.error}"

    rows = _jobs(db, pro["id"])
    assert len(rows) == CANDIDATES, len(rows)
    assert _provenance(rows) == Counter({"ai": RESCORE_TOP,
                                         "preliminary": CANDIDATES - RESCORE_TOP}), _provenance(rows)
    # The slice is the *top* of the deterministic pre-rank, not an arbitrary 12:
    # the four postings with no overlap with the profile are the estimates.
    assert {row.dedupe_key for row in rows if row.score_source == "ai"} == _strong_keys()

    ai_rows = [row for row in rows if row.score_source == "ai"]
    for row in ai_rows:
        assert row.score_reason.startswith("AI: "), row.score_reason
        assert (row.score_detail or {}).get("evidence"), row.score_detail
        assert (row.score_detail or {}).get("breakdown"), row.score_detail
    for row in (r for r in rows if r.score_source == "preliminary"):
        assert "Preliminary keyword estimate" in row.score_reason, row.score_reason

    # The report block — the only place an operator can read what happened.
    assert _report(db, item)["ai_rescore"] == {"enabled": True, "skipped": None,
                                               "scored": RESCORE_TOP}

    # Cost control is explicit: one wire call per AI-scored candidate, plus the
    # AI company-size verdict for the top AI_CLASSIFY_TOP rows. Nothing else in
    # the batch touches the model.
    assert len(_scoring_calls()) == RESCORE_TOP, len(_scoring_calls())
    assert len(_classify_calls()) == AI_CLASSIFY_TOP, len(_classify_calls())

    # The persona context the handler already builds reaches the model, so a
    # persona-scoped run scores against the track (the endpoint creates the
    # default persona from the profile's current title).
    assert any("candidate track (persona)" in _prompt_of(request)
               for request in _scoring_calls()), "the batch slice lost the persona context"
    assert any("senior software engineer" in _prompt_of(request) for request in _scoring_calls())

    # The badge data the SPA renders from: the job detail carries the row's
    # provenance, and /intelligence (Pro) answers with the model's verdict.
    detail = client.get(f"/api/jobs/{ai_rows[0].id}", headers=pro["auth"]).json()
    assert detail["score_source"] == "ai", detail["score_source"]
    intelligence = client.get(f"/api/jobs/{ai_rows[0].id}/intelligence", headers=pro["auth"]).json()
    assert intelligence["score_source"] == "ai", intelligence
    assert "upgrade_hint" not in intelligence, intelligence


# --------------------------------------------------------------------------- #
# 2. Free + profile + AI up → the run completes, and the provider sees nothing
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_free_run_completes_preliminary_and_makes_zero_ai_calls(
        client, auth, db, provider_owner, monkeypatch):
    """The plan gate is load-bearing: **zero** AI calls, and the report says why.

    The profile is withheld wholesale rather than passed with ``use_ai=False``,
    because the same profile also switches on the AI company-size verdict for the
    top ``AI_CLASSIFY_TOP`` rows — so this asserts on the wire, not on a flag.
    """
    _install_sources(monkeypatch)
    free = _free_user(client, db, "free-discovery@example.com")
    _profile(db, free["id"])
    item = _enqueue_discovery(db, free["id"], "free:plan-gate")

    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"

    rows = _jobs(db, free["id"])
    assert len(rows) == CANDIDATES, len(rows)
    assert _provenance(rows) == Counter({"preliminary": CANDIDATES}), _provenance(rows)
    for row in rows:
        # With the profile withheld the estimate is the "insufficient data"
        # default — exactly what every batch run produced before v2.2.3.
        assert "Preliminary keyword estimate" in row.score_reason, row.score_reason

    assert _report(db, item)["ai_rescore"] == {"enabled": False, "skipped": "plan_free",
                                               "scored": 0}
    assert _scoring_calls() == [], "a free-tier run must not score with the model"
    assert _classify_calls() == [], "a free-tier run must not classify with the model"
    assert _wire() == [], f"a free-tier discovery run made {len(_wire())} AI call(s)"


# --------------------------------------------------------------------------- #
# 3. Pro without a usable profile → nothing to score against
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
@pytest.mark.parametrize("profile_data", [None, {}], ids=["no_profile_row", "empty_profile_data"])
async def test_pro_run_without_a_profile_skips_the_slice(
        client, auth, db, provider_owner, monkeypatch, profile_data):
    """No profile row, or a row with empty ``data``: zero AI calls, all
    preliminary, ``skipped: "no_profile"`` — the model is never asked to invent a
    match for a candidate it has nothing on."""
    _install_sources(monkeypatch)
    # Lowercase e-mail ids: the register endpoint normalises the address.
    case = "norow" if profile_data is None else "emptydata"
    pro = _paid_user(client, db, f"noprofile-{case}@example.com")
    if profile_data is not None:
        _profile(db, pro["id"], profile_data)
    item = _enqueue_discovery(db, pro["id"], f"pro:no-profile:{case}")

    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"
    rows = _jobs(db, pro["id"])
    assert len(rows) == CANDIDATES
    assert _provenance(rows) == Counter({"preliminary": CANDIDATES}), _provenance(rows)
    assert _report(db, item)["ai_rescore"] == {"enabled": False, "skipped": "no_profile",
                                               "scored": 0}
    assert _wire() == [], f"a profile-less run made {len(_wire())} AI call(s)"


# --------------------------------------------------------------------------- #
# 4. Transient outage mid-slice → paused, nothing persisted, then exactly once
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_outage_mid_slice_pauses_the_run_and_persists_nothing(
        client, auth, db, provider_owner, monkeypatch):
    """The batch path stays **hard**: an outage mid-slice pauses the run.

    Nothing is persisted before the slice, so the paused attempt leaves no job
    rows behind and the resumed run creates them exactly once — the same rows an
    uninterrupted run would have made.
    """
    import app.services.ai_watchdog as watchdog

    _install_sources(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    pro = _paid_user(client, db, "pro-outage@example.com")
    _profile(db, pro["id"])
    item = _enqueue_discovery(db, pro["id"], "pro:outage-mid-slice")

    # Three candidates score, then the provider dies — mid-slice.
    _set_outage(503, after=3)
    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "paused", f"an AI outage must pause, not fail: {item.status} ({item.error})"
    assert item.finished_at is None, "paused work is never 'completed'"
    assert item.attempts == 0, "neither the claim nor the pause may consume a failure attempt"
    assert (item.payload or {}).get("paused_count") == 1
    assert _jobs(db, pro["id"]) == [], "a paused attempt must persist no job rows"
    assert len(_scoring_calls()) == 4, "3 good verdicts + the attempt that hit the outage"

    # The provider recovers; the watchdog drains the paused run.
    _set_recovery()
    import app.services.ai_client as ai_client

    ai_client._PING_CACHE.clear()  # the 60s probe cache must not mask recovery
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)  # the pause backoff elapsed
    db.commit()
    assert await watchdog.watchdog_cycle() == 1
    _sync(db)
    db.refresh(item)
    assert item.status == "queued", item.status

    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"
    assert (item.payload or {}).get("paused_count") == 1, "no second pause may happen"
    assert item.attempts == 0, "the run never failed — it sat out an outage"

    rows = _jobs(db, pro["id"])
    assert len(rows) == CANDIDATES, f"the resumed run must create the rows exactly once: {len(rows)}"
    assert _provenance(rows) == Counter({"ai": RESCORE_TOP,
                                         "preliminary": CANDIDATES - RESCORE_TOP}), _provenance(rows)
    assert {row.dedupe_key for row in rows if row.score_source == "ai"} == _strong_keys()
    assert _report(db, item)["ai_rescore"] == {"enabled": True, "skipped": None,
                                               "scored": RESCORE_TOP}
    # Three verdicts were paid for and thrown away by the pause (plus the call
    # that hit the outage); the resumed run scored the whole slice again.
    # Duplicated *work* — and duplicated spend, which is what a hard dependency
    # costs — but never duplicated rows.
    assert len(_scoring_calls()) == 4 + RESCORE_TOP, len(_scoring_calls())


# --------------------------------------------------------------------------- #
# 5. v2.2.2 interaction: the failure budget survives the outage cycle
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_failure_budget_is_intact_after_a_pause_resume_cycle(
        client, auth, db, provider_owner, monkeypatch):
    """The semantics v2.2.2 established, through the worker rather than the
    queue functions: a run that paused and resumed with **zero** handler failures
    still retries its first real failure, and N consecutive real failures still
    dead-letter on the Nth."""
    from app.services import handlers

    _install_sources(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    pro = _paid_user(client, db, "pro-budget@example.com")
    _profile(db, pro["id"])
    item = _enqueue_discovery(db, pro["id"], "pro:failure-budget", max_attempts=3)

    # One outage cycle: paused → drained → queued, with no failure recorded.
    _set_outage(503)
    await _run(item.id)
    _sync(db)
    db.refresh(item)
    assert item.status == "paused" and item.attempts == 0
    _set_recovery()
    from app.services.job_queue import drain_paused

    assert drain_paused(db, user_id=pro["id"], force=True) == 1
    _sync(db)
    db.refresh(item)
    assert item.status == "queued" and item.attempts == 0

    # Now the work really breaks (a non-AI handler failure). Under the
    # pre-v2.2.2 accounting the claims above had already spent the budget and
    # this first failure dead-lettered the run.
    booms = 0

    async def _boom(*args, **kwargs):
        nonlocal booms
        booms += 1
        raise RuntimeError(f"boom {booms}")

    monkeypatch.setattr(handlers, "discover_for_user", _boom)

    def _runnable() -> None:
        item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

    for failure in range(1, 4):
        _runnable()
        await _run(item.id)
        _sync(db)
        db.refresh(item)
        assert booms == failure, booms
        assert item.attempts == failure, f"failure {failure}: attempts={item.attempts}"
        if failure < 3:
            assert item.status == "queued", f"failure {failure} must retry: {item.status}"
            assert item.finished_at is None
        else:
            assert item.status == "dead", "the 3rd consecutive failure dead-letters"
            assert item.finished_at is not None
            assert item.error == "RuntimeError: boom 3", item.error

    from app.services.job_queue import claim

    assert claim(db, pipelines=["discovery"]) is None, "a dead item is never claimable again"
    assert _jobs(db, pro["id"]) == [], "a run that never finished persists nothing"


# --------------------------------------------------------------------------- #
# 6. Needs-action → dead-lettered with the diagnosis
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_needs_action_failure_dead_letters_with_the_diagnosis(
        client, auth, db, provider_owner, monkeypatch):
    """A provider that rejects the key is not an outage: retrying is pointless,
    so the run dead-letters at once and ``error`` carries the diagnosis."""
    _install_sources(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    pro = _paid_user(client, db, "pro-blocked@example.com")
    _profile(db, pro["id"])
    item = _enqueue_discovery(db, pro["id"], "pro:needs-action", max_attempts=5)

    _set_outage(401, after=0)
    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "dead", f"a blocked key must dead-letter, not pause: {item.status}"
    assert item.finished_at is not None
    assert item.attempts == 1, "a non-retryable failure is still a failure"
    assert (item.payload or {}).get("paused_count") is None, "it never paused"
    assert "api key" in (item.error or "").lower(), item.error
    assert _jobs(db, pro["id"]) == [], "nothing is persisted before the AI call succeeds"
    assert len(_scoring_calls()) == 1, "a blocked key must not be retried"


# --------------------------------------------------------------------------- #
# 7. Auto-mode notifications: provenance decides the caveat
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_auto_notification_drops_the_caveat_when_the_top_three_are_ai(
        client, auth, db, provider_owner, monkeypatch):
    """A Pro AI-scored auto run whose three highest scorers all carry the
    model's verdict must not tell the user they were scored by keyword overlap."""
    from app.models.models import Notification

    _install_sources(monkeypatch)
    pro = _paid_user(client, db, "pro-auto@example.com")
    _profile(db, pro["id"])
    item = _enqueue_discovery(db, pro["id"], "pro:auto-notification", trigger="auto")

    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"
    notices = db.query(Notification).filter(Notification.user_id == pro["id"]).all()
    assert len(notices) == 1, [n.title for n in notices]
    assert notices[0].kind == "high_match" and notices[0].link == "/jobs"
    body = notices[0].body
    assert body.count("•") == 3, body
    assert "keyword" not in body.lower(), body
    assert (notices[0].meta or {}).get("score_source") == ["ai"], notices[0].meta
    # Still one notification kind, and the surfaced rows really are AI verdicts.
    from app.models.models import Job

    surfaced = db.query(Job).filter(Job.id.in_((notices[0].meta or {})["job_ids"])).all()
    assert len(surfaced) == 3 and all(row.score_source == "ai" for row in surfaced)


def test_auto_notification_keeps_the_caveat_for_mixed_provenance(client, auth, db):
    """A mixed run — some of the surfaced rows are estimates — keeps the caveat.

    The top slice is 12 rows while the notification surfaces the three highest
    scorers, so "the run AI-scored something" is not the same claim as "these
    three numbers came from the model".
    """
    from app.models.models import Job, Notification, PipelineJob, User

    free = _free_user(client, db, "mixed-provenance@example.com")
    item = PipelineJob(user_id=free["id"], pipeline="discovery", status="processing",
                       payload={"trigger": "auto"}, dedupe_key=f"mixed:{free['id']}")
    db.add(item)
    rows = []
    for index, source in enumerate(("ai", "ai", "preliminary", "ai")):
        row = Job(user_id=free["id"], title=f"Backend Engineer {index}", company=f"Paystack {index}",
                  location="Remote", description="Python, FastAPI, PostgreSQL.", source="lever",
                  dedupe_key=f"lever:mixed-{index}", status="discovered", score=90.0 - index,
                  score_reason="test fixture", score_source=source, company_size="small")
        db.add(row)
        rows.append(row)
    db.commit()

    from app.services import handlers

    result = {"scanned": 4, "fresh": 4, "inserted": 4, "sources": {}, "elapsed_seconds": 0.1,
              "ai_rescore": {"enabled": True, "skipped": None, "scored": 3},
              "jobs": [{"id": int(row.id), "title": row.title, "company": row.company,
                        "source": row.source, "score": float(row.score), "location": row.location,
                        "url": "https://x/1"} for row in rows]}
    handlers._notify_auto_discovery(db, item, db.get(User, free["id"]), result)
    _sync(db)

    notices = db.query(Notification).filter(Notification.user_id == free["id"]).all()
    assert len(notices) == 1, [n.title for n in notices]
    body = notices[0].body
    assert body.count("•") == 3, body
    assert "Scored by keyword overlap" in body, body
    assert (notices[0].meta or {}).get("score_source") == ["ai", "preliminary"], notices[0].meta


# --------------------------------------------------------------------------- #
# 8. A related bug found while wiring the slice up: an unscoreable candidate is
#    not an AI verdict
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_a_run_with_nothing_to_score_claims_no_ai_verdicts(
        client, auth, db, provider_owner, monkeypatch):
    """A source that returns titles and URLs only (every adapter defaults a
    missing description to ``""``) used to produce rows badged ``ai`` — with the
    reason "AI: Not enough profile or job text to score" — for calls the gateway
    never made, and a report claiming ``scored: 12``.

    The slice still runs (the profile and the plan say it may); it simply has
    nothing to score, and the report says exactly that.
    """
    _install_sources(monkeypatch, no_descriptions=True)
    pro = _paid_user(client, db, "pro-notext@example.com")
    _profile(db, pro["id"])
    item = _enqueue_discovery(db, pro["id"], "pro:no-description-source")

    await _run(item.id)

    _sync(db)
    db.refresh(item)
    assert item.status == "done", f"{item.status}: {item.error}"
    rows = _jobs(db, pro["id"])
    assert len(rows) == CANDIDATES
    # The top slice went to the scorer and came back "nothing to score"; the
    # bulk rows never left the deterministic pre-rank.
    assert _provenance(rows) == Counter({"insufficient_data": RESCORE_TOP,
                                         "preliminary": CANDIDATES - RESCORE_TOP}), _provenance(rows)
    for row in rows:
        assert not row.score_reason.startswith("AI: "), row.score_reason
    assert _report(db, item)["ai_rescore"] == {"enabled": True, "skipped": None, "scored": 0}
    assert _scoring_calls() == [], "there was nothing to score, so the model must not be asked"


@pytest.mark.asyncio
async def test_score_job_reports_insufficient_data_instead_of_a_fake_ai_verdict():
    """The scorer short-circuits when there is nothing to score — both doors.

    ``insufficient_data`` is a provenance ``score_job`` has always documented and
    the one the free tier's ``preliminary_score_detailed`` already returns; the
    AI path used to answer ``"ai"`` for the same case.
    """
    from conftest import stub_ai_response

    from app.services.scoring import score_job

    profile = stub_ai_response("parse", "")
    for label, args in (("empty job description", (profile, "   ")),
                        ("profile with no text", ({"name": "Someone"}, "Python, FastAPI, PostgreSQL."))):
        result = await score_job(*args)
        assert result["score_source"] == "insufficient_data", f"{label}: {result['score_source']}"
        assert not result["reason"].startswith("AI: "), f"{label}: {result['reason']}"
        assert result["error"] is None, result["error"]
        assert (result["detail"] or {}).get("source") == "insufficient_data"

    # …and a real pair is still the model's verdict (the deterministic stand-in
    # answers it), so the fix did not swallow the AI path.
    verdict = await score_job(profile, "Senior Backend Engineer. Python, FastAPI, PostgreSQL, "
                                       "payments platform at scale.")
    assert verdict["score_source"] == "ai", verdict["score_source"]
    assert verdict["reason"].startswith("AI: "), verdict["reason"]


# --------------------------------------------------------------------------- #
# 9. The SPA badge: provenance travels to the fields it renders from
# --------------------------------------------------------------------------- #
def test_jobs_ui_still_badges_both_provenances():
    """The badge is not new — this pins that it still covers both values.

    ``frontend/src/pages/Jobs.tsx`` renders ``SourceBadge`` from a
    ``score_source`` string; if either value lost its entry the badge would fall
    back to a bare "unknown" chip and an AI verdict would look like an estimate
    (or the other way round).
    """
    import os
    import re

    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                        "frontend", "src", "pages", "Jobs.tsx"))
    source = open(path, encoding="utf-8").read()
    badge = re.search(r"function SourceBadge.*?\n\}", source, re.DOTALL)
    assert badge, "the jobs page lost its provenance badge"
    for value, label in (("ai", "AI verified"), ("preliminary", "estimate")):
        entry = re.search(rf"\b{value}:\s*\{{[^}}]*label: '{re.escape(label)}'", badge.group(0))
        assert entry, f"SourceBadge no longer maps score_source='{value}' to '{label}'"

"""v2.2.2 — ``PipelineJob.attempts`` counts handler *failures*, not claims.

The contract (see the :mod:`app.services.job_queue` docstring):

* ``claim()`` / ``claim_item()`` take a lease. They do **not** touch
  ``attempts`` — being handed to a worker is not a failure.
* ``fail()`` increments ``attempts`` first and *then* applies the budget:
  ``attempts >= max_attempts`` → ``dead``, otherwise back off and re-queue.
* ``pause()`` (transient AI outage) consumes no attempt, exactly as before.
  Its own safety valve is the ``paused_count`` in the payload, capped at
  ``AI_PAUSE_MAX`` — a separate budget from ``attempts``.
* ``recover_stalled()`` dead-letters a stalled item whose *failure* count has
  reached ``max_attempts`` and re-queues it otherwise.

Before this fix ``attempts`` was incremented at claim time, so every
pause → resume cycle spent failure budget without the item ever failing: an
item with ``max_attempts=3`` and **zero** handler failures burned its whole
budget on outage cycles alone and then dead-lettered on its *first* real
failure. The live drill row read ``attempts=6, max_attempts=3`` — six claims,
not one failure.

Hermetic by design: no provider, no network, no worker loop — the queue
functions are called directly against the test database.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.models import PipelineJob, User
from app.services.job_queue import (
    AI_PAUSE_MAX,
    claim,
    claim_item,
    drain_paused,
    enqueue,
    fail,
    pause,
    recover_stalled,
)


def _user(db) -> User:
    return db.query(User).order_by(User.id).first()


def _runnable(db, item: PipelineJob) -> None:
    """Rewind a backoff delay so the item can be claimed again at once."""
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()


def _stall(db, item: PipelineJob) -> None:
    """Simulate the worker dying mid-run: the lease expires, the row stays
    ``processing`` and nobody ever called ``fail()``."""
    # v2.3: recover_stalled has a 300s safety margin (to avoid cloning a live
    # worker still inside a 300s AI call). Use 400s so the row is beyond the
    # margin and is considered truly stalled by default.
    item.lease_expires_at = datetime.utcnow() - timedelta(seconds=400)
    item.locked_by = "dead-worker-42"
    db.commit()


def _recover(db, **kwargs):
    """Helper that calls recover_stalled with safety_margin=0 for immediate recovery
    in unit tests that don't want to reason about the margin."""
    return recover_stalled(db, safety_margin_seconds=0, **kwargs)


# --------------------------------------------------------------------------- #
# 1. Claiming is not failing
# --------------------------------------------------------------------------- #
def test_claim_and_claim_item_do_not_increment_attempts(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="email", max_attempts=3,
                   dedupe_key="claim-is-not-a-failure")
    assert item.attempts == 0

    claimed = claim(db, pipelines=["email"])
    assert claimed is not None and claimed.id == item.id
    assert claimed.status == "processing"
    assert claimed.attempts == 0, "taking a lease must not spend the failure budget"
    assert claimed.lease_expires_at > datetime.utcnow()

    # Crash → re-queue → claim the *same* row again (claim_item this time):
    # still zero failures, so still zero attempts.
    _stall(db, claimed)
    assert recover_stalled(db, pipelines=["email"], safety_margin_seconds=0) == 1
    again = claim_item(db, item.id)
    assert again is not None
    assert again.attempts == 0
    db.refresh(item)
    assert item.attempts == 0


# --------------------------------------------------------------------------- #
# 2. The reported bug: pause/resume cycles must not spend the failure budget
# --------------------------------------------------------------------------- #
def test_pause_resume_cycles_cost_nothing_and_the_first_real_failure_retries(db, owner):
    """The reported incident: five outage cycles with zero handler failures,
    then one real failure — the item must *retry*, not dead-letter."""
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=3,
                   payload={"task": "tag_resume"}, dedupe_key="outage-drill")

    cycles = 0
    for cycle in range(1, 6):
        claimed = claim(db, pipelines=["ai"])
        assert claimed is not None, f"cycle {cycle}: the paused item must come back"
        cycles += 1
        assert claimed.attempts == 0, f"cycle {cycle}: an outage is not a failure"
        assert pause(db, claimed, "ai_transient_outage") == "paused"
        db.refresh(item)
        assert item.status == "paused" and item.finished_at is None
        assert (item.payload or {}).get("paused_count") == cycle
        assert item.attempts == 0, "pause() consumes no attempt"
        assert drain_paused(db, user_id=user.id, force=True) == 1
        db.refresh(item)
        assert item.status == "queued"

    assert cycles == 5
    assert item.attempts == 0, "five claims and five pauses are still zero failures"

    # The sixth claim, and the first REAL failure. Under the old code this row
    # read attempts=6 against max_attempts=3 here and dead-lettered — exactly
    # what the outage drill observed.
    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "boom") == "retrying"
    db.refresh(item)
    assert item.status == "queued"
    assert item.attempts == 1, "one failure, finally recorded"
    assert item.scheduled_at > datetime.utcnow(), "the retry is backed off"


def test_the_failure_budget_survives_an_outage_intact(db, owner):
    """The other half of the drill: after the pauses the item is not merely
    retryable once — it still has its *whole* failure budget, so it takes the
    full ``max_attempts`` real failures to dead-letter it."""
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=3,
                   payload={"task": "tag_resume"}, dedupe_key="budget-intact")

    for _ in range(4):
        claimed = claim(db, pipelines=["ai"])
        assert pause(db, claimed, "ai_transient_outage") == "paused"
        assert drain_paused(db, user_id=user.id, force=True) == 1

    # max_attempts=3 → failures 1 and 2 retry, the 3rd dead-letters. Four
    # outage cycles bought nothing and cost nothing: the budget is untouched.
    for failure in (1, 2):
        _runnable(db, item)
        claimed = claim(db, pipelines=["ai"])
        assert fail(db, claimed, f"boom {failure}") == "retrying"
        db.refresh(item)
        assert item.attempts == failure
        assert item.status == "queued"

    _runnable(db, item)
    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "boom 3") == "dead"
    db.refresh(item)
    assert item.status == "dead" and item.attempts == 3
    assert claim(db, pipelines=["ai"]) is None


# --------------------------------------------------------------------------- #
# 3. N consecutive real failures → dead on the Nth, never claimed again
# --------------------------------------------------------------------------- #
def test_consecutive_failures_dead_letter_on_the_nth(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="discovery", max_attempts=3,
                   dedupe_key="always-boom")

    for failure in (1, 2, 3):
        claimed = claim(db, pipelines=["discovery"])
        assert claimed is not None, f"failure {failure}: the item must still be claimable"
        outcome = fail(db, claimed, f"boom {failure}")
        db.refresh(item)
        assert item.attempts == failure, "fail() is the only thing that increments attempts"
        if failure < 3:
            assert outcome == "retrying"
            assert item.status == "queued" and item.finished_at is None
            _runnable(db, item)
        else:
            assert outcome == "dead"
            assert item.status == "dead" and item.finished_at is not None
            assert item.error == "boom 3"

    # Dead means dead: no claim path can pick it up again.
    assert claim(db, pipelines=["discovery"]) is None
    assert claim_item(db, item.id) is None
    db.refresh(item)
    assert item.attempts == 3 and item.status == "dead"


def test_non_retryable_failure_is_dead_on_the_first_failure(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=5,
                   dedupe_key="blocked-key")
    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "invalid_api_key", retryable=False) == "dead"
    db.refresh(item)
    assert item.status == "dead"
    assert item.attempts == 1, "a non-retryable failure is still a failure"


# --------------------------------------------------------------------------- #
# 4. Lease expiry: the failure count decides, not the claim count
# --------------------------------------------------------------------------- #
def test_stalled_item_with_budget_left_is_requeued(db, owner):
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=3,
                   payload={"task": "tag_resume"}, dedupe_key="stall-under-cap")

    # One recorded failure, then a crash mid-retry.
    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "boom") == "retrying"
    _runnable(db, item)

    claimed = claim(db, pipelines=["ai"])
    assert claimed.attempts == 1, "the second claim must not make it 2"
    _stall(db, claimed)

    assert recover_stalled(db, pipelines=["ai"], safety_margin_seconds=0) == 1
    db.refresh(item)
    assert item.status == "queued", "1 failure of 3 — the budget is not spent"
    assert item.attempts == 1
    assert item.locked_by == "" and item.lease_expires_at is None

    # …and a crash loop with no failures at all keeps the item alive too: it
    # never failed, so it never loses budget.
    for _ in range(4):
        claimed = claim(db, pipelines=["ai"])
        assert claimed is not None
        _stall(db, claimed)
        assert recover_stalled(db, pipelines=["ai"], safety_margin_seconds=0) == 1
        db.refresh(item)
        assert item.status == "queued" and item.attempts == 1


def test_stalled_item_at_the_failure_cap_is_dead_lettered(db, owner):
    """A stalled row whose failure counter has reached ``max_attempts`` is
    dead-lettered on lease expiry, not resurrected into an endless loop.

    Under the new semantics ``fail()`` dead-letters at the cap, so a
    ``processing`` row only reaches it when the counter was written before
    v2.2.2 (it counted claims — the drill row read ``attempts=6`` against
    ``max_attempts=3``) or an operator lowered the cap. Both must be honoured.
    """
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=3,
                   payload={"task": "tag_resume"}, dedupe_key="stall-at-cap")

    claimed = claim(db, pipelines=["ai"])
    claimed.attempts = 6  # the counter the live outage drill observed
    db.commit()
    _stall(db, claimed)

    assert recover_stalled(db, pipelines=["ai"], safety_margin_seconds=0) == 1
    db.refresh(item)
    assert item.status == "dead"
    assert item.finished_at is not None
    assert item.error == "lease expired"
    assert claim(db, pipelines=["ai"]) is None


# --------------------------------------------------------------------------- #
# 5. The pause budget is a separate safety valve
# --------------------------------------------------------------------------- #
def test_pause_budget_dead_letters_independently_of_attempts(db, owner):
    """``AI_PAUSE_MAX`` pauses dead-letter an item that has never failed once
    and still has its whole failure budget — the two counters stay separate."""
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=50,
                   payload={"task": "tag_resume"}, dedupe_key="pause-cap")

    for paused in range(1, AI_PAUSE_MAX + 1):
        claimed = claim(db, pipelines=["ai"])
        assert claimed is not None, f"pause {paused}: the item must still be claimable"
        assert pause(db, claimed, "ai_transient_outage") == "paused"
        db.refresh(item)
        assert item.attempts == 0, "no failure has happened yet"
        assert (item.payload or {}).get("paused_count") == paused
        assert drain_paused(db, user_id=user.id, force=True) == 1

    # The next pause is the 13th — over the cap, so the item is dead even
    # though attempts (0) is nowhere near max_attempts (50).
    claimed = claim(db, pipelines=["ai"])
    assert pause(db, claimed, "ai_transient_outage") == "dead"
    db.refresh(item)
    assert item.status == "dead"
    assert item.finished_at is not None
    assert item.attempts == 0, "the pause budget is not the failure budget"
    assert f"paused {AI_PAUSE_MAX} times" in item.error
    assert claim(db, pipelines=["ai"]) is None


def test_pause_budget_survives_real_failures_too(db, owner):
    """The reverse pairing: failures do not spend the pause budget, so an item
    can fail once, sit out a long outage, and still retry afterwards."""
    user = _user(db)
    item = enqueue(db, user_id=user.id, pipeline="ai", max_attempts=3,
                   payload={"task": "tag_resume"}, dedupe_key="mixed-budgets")

    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "boom") == "retrying"
    _runnable(db, item)

    for paused in range(1, 4):
        claimed = claim(db, pipelines=["ai"])
        assert pause(db, claimed, "ai_transient_outage") == "paused"
        db.refresh(item)
        assert item.attempts == 1, "pauses never add to the failure count"
        assert (item.payload or {}).get("paused_count") == paused
        assert drain_paused(db, user_id=user.id, force=True) == 1

    claimed = claim(db, pipelines=["ai"])
    assert fail(db, claimed, "boom again") == "retrying", "2 of 3 failures — still alive"
    db.refresh(item)
    assert item.attempts == 2 and item.status == "queued"

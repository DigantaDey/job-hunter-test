"""
Laya — the local typed-decision engine.

Laya (``pip install laya``; Apache 2.0; self-hosted) answers **typed
questions** over any text in a single forward pass: a ``choice`` among
options you declare, a ``score`` on an ordinal rubric, or a ``noul``
(yes/no probability). It never generates prose, so — unlike the LLM gateway —
it cannot return malformed JSON, hallucinate a schema or time out mid-parse.
That makes it the right engine for the *decision-shaped* work in this product:

* **ranking** (the rubric dimensions + 0-100 score of a job ↔ profile match),
* **classification** (company size, and other fixed option sets),
* **field mapping** (which canonical profile key a portal field asks for),

while anything that must *write* text (resume tailoring, outreach drafts,
evidence quotes) stays with the LLM. The LLM remains the fallback in ``auto``
mode, so removing Laya changes nothing.

Three deliberate design points:

* **One vocabulary of answers.** :func:`choice`, :func:`noul` and
  :func:`score` normalise Laya's payload into ``answer`` + ``confidence`` +
  ``probabilities`` regardless of upstream key drift — a caller never sees a
  raw model structure.
* **Owner-controlled routing.** :func:`should_attempt` resolves the env
  ceiling (``LAYA_ENABLED``) plus the owner's Settings → decision-engine
  mode (``auto`` / ``laya_only`` / ``llm_only``) and the per-task opt-ins.
  Nothing reaches Laya that the owner did not route to it.
* **Honest unavailability.** When Laya cannot answer (not installed, load
  failure, timeout) the helpers return ``None`` / raise :class:`LayaUnavailable`
  — callers either fall back to the LLM (``auto``) or surface the same
  honest "unavailable" outcome an AI outage gets (``laya_only``). There is no
  silent heuristic substitution anywhere in this module.

Model choice follows Laya's own guidance: short states ride the default
router (fast English checkpoint); long documents (a job description plus a
profile) are sent to the multilingual checkpoint with ``max_len`` raised,
because the English checkpoint's short window would silently truncate them.
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc

log = get_logger("app.laya")

#: The decision tasks this product routes to Laya. Each maps to the owner's
#: per-task opt-in setting key (Settings → decision engine).
TASKS: Dict[str, str] = {
    "ranking": "for_ranking",
    "classification": "for_classification",
    "field_mapping": "for_field_mapping",
}

#: Owner-configurable modes. ``auto`` answers with Laya and escalates
#: low-confidence answers to the LLM; ``laya_only`` never reaches the LLM;
#: ``llm_only`` never reaches Laya (the pre-Laya behaviour).
MODES: Sequence[str] = ("auto", "laya_only", "llm_only")
MODE_AUTO = "auto"
MODE_LAYA_ONLY = "laya_only"
MODE_LLM_ONLY = "llm_only"

#: Below this confidence an ``auto``-mode answer is escalated to the LLM
#: instead of being trusted. Laya's probabilities are calibrated (its whole
#: design point), so a fixed floor is meaningful here where it would not be
#: for a generative model's self-reported confidence.
DEFAULT_CONFIDENCE_FLOOR = 0.55

#: Long documents ride the multilingual checkpoint (see the module docstring).
LONG_STATE_CHARS = 1500

_lock = threading.Lock()
_router: Any = None
_load_error: Optional[str] = None

#: One process-wide lock around ``router.predict`` itself (``_lock`` only guards
#: construction). The engine is a *single* torch model: running two forward
#: passes on it at once is what produced the Metal
#: ``MTLCommandBufferStatusCommitted`` assertion on macOS (two command buffers on
#: one device) and thrashes memory anywhere else. Callers queue; the per-call
#: ``LAYA_TIMEOUT_SECONDS`` budget still bounds the wait, so a saturated engine
#: reports ``timeout`` rather than corrupting the process.
_predict_lock = threading.Lock()

#: Consecutive engine failures before it is parked, and for how long. A discovery
#: run scores up to ``AI_RESCORE_TOP`` candidates; without this, an engine that
#: is down (crash, missing GPU, OOM) costs the run one ``LAYA_TIMEOUT_SECONDS``
#: timeout *per candidate* before every one of them falls back to the LLM — the
#: 25-minute run in the v2.3 report. Parked means "do not retry yet": auto mode
#: uses the LLM path (the pre-Laya behaviour), ``laya_only`` reports the same
#: ``LayaUnavailable`` immediately instead of waiting out another timeout.
_FAILURE_THRESHOLD = 3
_FAILURE_COOLDOWN_SECONDS = 120.0
_failures = 0
_last_failure: Optional[float] = None


class LayaUnavailable(RuntimeError):
    """Laya cannot answer right now. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code

    def payload(self) -> Dict[str, Any]:
        return {"reason": "laya_unavailable", "code": self.code, "message": str(self)}


def installed() -> bool:
    """True when the ``laya`` package is importable. Cheap, cached by Python."""
    try:
        return importlib.util.find_spec("laya") is not None
    except Exception:  # pragma: no cover - exotic broken installs
        return False


def _get_router() -> Any:
    """Build (once) and return the Laya Router. Raises :class:`LayaUnavailable`."""
    global _router, _load_error
    with _lock:
        if _router is not None:
            return _router
        if _load_error:
            raise LayaUnavailable("load_failed", _load_error)
        if not installed():
            raise LayaUnavailable(
                "not_installed",
                "the 'laya' package is not installed (pip install laya, or "
                "WITH_LAYA=1 ./run.sh)",
            )
        try:
            from laya import Router  # noqa: PLC0415 - optional dependency

            kwargs: Dict[str, Any] = {}
            if settings.laya_device:
                kwargs["device"] = settings.laya_device
            _router = Router(**kwargs)
            return _router
        except Exception as exc:
            _load_error = f"{type(exc).__name__}: {exc}"
            log.warning("laya: checkpoint load failed: %s", _load_error)
            raise LayaUnavailable("load_failed", _load_error) from exc


def reset_for_tests() -> None:
    """Forget the cached router and failure state (fixtures swap fake modules)."""
    global _router, _load_error, _failures, _last_failure
    with _lock:
        _router = None
        _load_error = None
        _failures = 0
        _last_failure = None


def _record_failure() -> None:
    """Count a failed forward pass (timeout, crash, bad payload)."""
    global _failures, _last_failure
    with _lock:
        _failures += 1
        _last_failure = time.monotonic()


def _record_success() -> None:
    global _failures, _last_failure
    with _lock:
        _failures = 0
        _last_failure = None


def _failure_state() -> Tuple[int, Optional[float]]:
    with _lock:
        return _failures, _last_failure


def parked() -> bool:
    """True while the engine is benched after repeated failures.

    ``engine_parked`` is a distinct reason from ``timeout`` / ``predict_failed``:
    the UI and the run report can say "the local engine is parked for Ns" instead
    of pretending every candidate hit a fresh timeout.
    """
    failures, last = _failure_state()
    if failures < _FAILURE_THRESHOLD or last is None:
        return False
    return (time.monotonic() - last) < _FAILURE_COOLDOWN_SECONDS


def status() -> Dict[str, Any]:
    """What the settings UI needs to describe the engine honestly."""
    return {
        "installed": installed(),
        "platform_enabled": bool(settings.laya_enabled),
        "device": settings.laya_device or "auto",
        "max_len": settings.laya_max_len,
        "load_error": _load_error,
    }


# --------------------------------------------------------------------------- #
# Settings resolution (env ceiling → owner settings → task opt-in)
# --------------------------------------------------------------------------- #
def mode_for(db=None, user_id: Optional[int] = None) -> str:
    """The configured routing mode: ``auto`` / ``laya_only`` / ``llm_only``.

    The env ceiling wins outright: with ``LAYA_ENABLED=false`` the answer is
    always ``llm_only``, whatever the owner's row says. No settings context
    (tests, scripts) means the shipped default, ``auto``.
    """
    if not settings.laya_enabled:
        return MODE_LLM_ONLY
    if db is not None and user_id is not None:
        try:
            from app.services.user_settings import get_setting  # noqa: PLC0415

            value = str(get_setting(db, user_id, "laya", "mode", MODE_AUTO) or MODE_AUTO).strip()
            return value if value in MODES else MODE_AUTO
        except Exception as exc:
            log.warning("laya: mode resolution failed for user %s: %s", user_id, exc)
    return MODE_AUTO


def is_strict(db=None, user_id: Optional[int] = None) -> bool:
    """True in ``laya_only`` mode — the LLM must not be consulted as fallback."""
    return mode_for(db, user_id) == MODE_LAYA_ONLY


def should_attempt(db=None, user_id: Optional[int] = None, task: str = "") -> bool:
    """True when Laya answers may be used for *task* at all right now.

    All of: the env ceiling is on, the mode is not ``llm_only``, the owner left
    this task's opt-in on, and the package is installed. This is the one
    predicate behind every routing decision — call sites never re-derive it.
    """
    if not settings.laya_enabled or not installed():
        return False
    mode = mode_for(db, user_id)
    if mode == MODE_LLM_ONLY:
        return False
    key = TASKS.get(task)
    if key and db is not None and user_id is not None:
        try:
            from app.services.user_settings import get_setting  # noqa: PLC0415

            if not bool(get_setting(db, user_id, "laya", key, True)):
                return False
        except Exception as exc:
            log.warning("laya: task opt-in resolution failed for user %s: %s", user_id, exc)
    return True


def confidence_floor(db=None, user_id: Optional[int] = None) -> float:
    return float(getattr(settings, "laya_confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
                 or DEFAULT_CONFIDENCE_FLOOR)


# --------------------------------------------------------------------------- #
# Prediction + answer normalisation
# --------------------------------------------------------------------------- #
async def predict(questions: Mapping[str, Any], state: Any, *,
                  force_long: bool = False, timeout: Optional[float] = None) -> Dict[str, Any]:
    """One forward pass: answer *questions* over *state*. Raw ``system_one`` payload.

    Runs the (sync, CPU/GPU-bound) engine off the event loop, bounded by
    ``LAYA_TIMEOUT_SECONDS``. Raises :class:`LayaUnavailable` on any engine
    failure — the caller decides between fallback and honest unavailability.
    """
    router = _get_router()
    if parked():
        inc("jobhunter_laya_predict_total", result="parked")
        raise LayaUnavailable("engine_parked", "the engine is parked after repeated failures")
    max_len = int(settings.laya_max_len or 0)
    long_state = force_long
    if not long_state:
        try:
            long_state = len(str(state if not isinstance(state, Mapping)
                                 else " ".join(str(v) for v in state.values()))) > LONG_STATE_CHARS
        except Exception:
            long_state = False
    kwargs: Dict[str, Any] = {}
    if long_state and max_len > 0:
        # Laya's own guidance: long documents must name the multilingual
        # checkpoint and raise max_len, or the English checkpoint silently
        # truncates them at a few hundred tokens.
        kwargs["model"] = "multilingual"
        kwargs["max_len"] = max_len

    def _run() -> Dict[str, Any]:
        with _predict_lock:
            return router.predict(state, dict(questions), **kwargs)

    started = time.perf_counter()
    budget = float(timeout or settings.laya_timeout or 20.0)
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_run), timeout=budget)
    except asyncio.TimeoutError as exc:
        _record_failure()
        inc("jobhunter_laya_predict_total", result="timeout")
        raise LayaUnavailable("timeout", f"laya did not answer within {budget:.0f}s") from exc
    except LayaUnavailable:
        raise
    except Exception as exc:
        _record_failure()
        inc("jobhunter_laya_predict_total", result="error")
        raise LayaUnavailable("predict_failed", f"{type(exc).__name__}: {exc}") from exc
    duration_ms = (time.perf_counter() - started) * 1000
    if not isinstance(result, dict):
        _record_failure()
        inc("jobhunter_laya_predict_total", result="error")
        raise LayaUnavailable("bad_payload", "laya returned a non-dict payload")
    inc("jobhunter_laya_predict_total", result="ok")
    routing = result.get("routing")
    if not isinstance(routing, Mapping):
        routing = {}
    log.debug("laya: %d question(s) in %.0fms via %s", len(questions), duration_ms,
              routing.get("model") or "default")
    _record_success()
    result["_meta"] = {"duration_ms": duration_ms, "model": routing.get("model")}
    return result


def _answers_of(result: Mapping[str, Any]) -> Dict[str, Any]:
    answers = result.get("answers")
    return dict(answers) if isinstance(answers, Mapping) else {}


def _probabilities_of(answer: Mapping[str, Any]) -> Dict[str, float]:
    """Normalise the probability map (``probs`` / ``probabilities`` / ``distribution``)."""
    for key in ("probs", "probabilities", "distribution"):
        raw = answer.get(key)
        if isinstance(raw, Mapping):
            return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) \
                and all(isinstance(v, (int, float)) for v in raw):
            return {str(i): float(v) for i, v in enumerate(raw)}
    return {}


def _confidence_of(answer: Mapping[str, Any], probs: Mapping[str, float], *, chosen: str) -> float:
    raw = answer.get("confidence")
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    if chosen and chosen in probs:
        return max(0.0, min(1.0, float(probs[chosen])))
    if probs:
        return max(0.0, min(1.0, max(probs.values())))
    return 0.0


def _noul_probability(answer: Any) -> Optional[float]:
    """P(true) for a ``noul`` answer, whatever key it arrives under."""
    if isinstance(answer, (int, float)):
        return max(0.0, min(1.0, float(answer)))
    if isinstance(answer, Mapping):
        for key in ("noul", "probability", "prob", "p_true", "score", "answer"):
            raw = answer.get(key)
            if isinstance(raw, (int, float)):
                return max(0.0, min(1.0, float(raw)))
        probs = _probabilities_of(answer)
        for key in ("true", "yes", "True", "Yes", "1"):
            if key in probs:
                return max(0.0, min(1.0, float(probs[key])))
    return None


async def choice(state: Any, key: str, instructions: str,
                 criteria: Mapping[str, str], *,
                 db=None, user_id: Optional[int] = None,
                 min_confidence: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Answer one ``choice`` question. ``{answer, confidence, probabilities}`` or ``None``.

    ``None`` means "Laya did not answer with enough confidence" — the caller
    escalates (auto) or reports the pause/outage (laya_only). A key outside
    *criteria* is also ``None``: an answer the question did not offer is not
    an answer.
    """
    if not criteria:
        return None
    questions = {key: {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}}
    result = await predict(questions, state)
    answer = _answers_of(result).get(key)
    if not isinstance(answer, Mapping):
        return None
    chosen = str(answer.get("choice") or answer.get("answer") or "").strip()
    if chosen not in criteria:
        return None
    probs = _probabilities_of(answer)
    confidence = _confidence_of(answer, probs, chosen=chosen)
    floor = confidence_floor(db, user_id) if min_confidence is None else float(min_confidence)
    if confidence < floor:
        return None
    return {"answer": chosen, "confidence": confidence, "probabilities": probs}


async def noul(state: Any, key: str, instructions: str, *, timeout: Optional[float] = None) -> Optional[float]:
    """Answer one yes/no question: P(true), or ``None`` when Laya cannot."""
    questions = {key: {"type": "noul", "instructions": instructions}}
    result = await predict(questions, state, timeout=timeout)
    return _noul_probability(_answers_of(result).get(key))


async def score_ordinal(state: Any, key: str, instructions: str,
                        criteria: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Answer one ``score`` (ordinal rubric) question.

    Returns ``{level, expected, confidence, probabilities}`` where ``level`` is
    the argmax rubric index and ``expected`` the probability-weighted position
    across the rubric (smooth, in ``[0, len(criteria)-1]``) — the caller maps
    either onto its own scale. ``None`` when Laya cannot answer.
    """
    labels = [str(c) for c in criteria if str(c).strip()]
    if not labels:
        return None
    questions = {key: {"type": "score", "instructions": instructions, "criteria": labels}}
    result = await predict(questions, state)
    answer = _answers_of(result).get(key)
    if not isinstance(answer, Mapping):
        return None
    probs = _probabilities_of(answer)
    raw_score = answer.get("score", answer.get("answer", answer.get("level")))
    level: Optional[int] = None
    if isinstance(raw_score, (int, float)):
        level = int(round(float(raw_score)))
    elif isinstance(raw_score, str) and raw_score.strip() in labels:
        level = labels.index(raw_score.strip())
    expected: Optional[float] = None
    if probs:
        positions = [(int(k), float(v)) for k, v in probs.items() if str(k).isdigit()]
        total = sum(v for _, v in positions) or 0.0
        if positions and total > 0:
            expected = sum(i * v for i, v in positions) / total
            if level is None:
                level = max(positions, key=lambda iv: iv[1])[0]
    if level is None:
        return None
    level = max(0, min(len(labels) - 1, level))
    if expected is None:
        expected = float(level)
    # The rubric is the contract: a stray index outside it is clamped, never
    # projected onto a scale the question did not offer.
    expected = max(0.0, min(float(len(labels) - 1), expected))
    confidence = _confidence_of(answer, probs, chosen=str(level))
    return {"level": level, "expected": expected, "confidence": confidence,
            "probabilities": probs, "labels": labels}

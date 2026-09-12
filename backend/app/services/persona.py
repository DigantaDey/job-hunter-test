"""
User personas — the durable model of who the user is *for a given job track*.

A persona is not a settings bag. It is the thing that makes the product get
sharper the longer the user uses it:

* **Scope.** Each persona owns a target role plus its own search context,
  preferences and funnel, so "Data Analyst" and "Data Scientist" never blur
  into one generic search.
* **Learning.** Every scored job, application, reply, interview and manual edit
  records a signal in ``memory``. Signals are the only raw material the portrait
  is allowed to use.
* **Reflection.** ``build_portrait`` asks the model to describe the user *under
  this persona*, and the guardrail rejects any sentence that is not backed by a
  recorded signal. The portrait is what feeds discovery keywords, scoring
  emphasis and outreach tone — so a hallucinated portrait would corrupt the
  whole funnel, which is why it is grounded rather than free-form.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.models import Persona, Profile, User
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    GuardrailError,
    SchemaSpec,
    build_fact_ledger,
    check_grounded,
    run_guarded_task,
    strip_ai_artifacts,
)

log = get_logger("app.persona")


class PersonaExistsError(Exception):
    """The user already has a track with this name."""

    def __init__(self, name: str, persona_id: Optional[int] = None):
        self.name = name
        self.persona_id = persona_id
        super().__init__(f"A track named '{name}' already exists")

#: Signals that count as real evidence of the user's behaviour/intent.
SIGNAL_KINDS = {
    "job_scored", "job_saved", "job_skipped", "applied", "resume_generated",
    "email_drafted", "email_sent", "reply_received", "interview", "offer",
    "resume_edited", "preference_set", "keyword_added", "persona_created",
}

MAX_SIGNALS = 500
COUNTER_KEYS = ("applied", "resume_generated", "email_drafted", "email_sent",
                "reply_received", "interview", "offer", "job_saved", "job_skipped")

EMPTY_MEMORY: Dict[str, Any] = {
    "signals": [],
    "observed_titles": {},
    "observed_skills": {},
    "observed_companies": {},
    "engagement": dict.fromkeys(COUNTER_KEYS, 0),
    "preferences": {},
    "notes": [],
}


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def _blank_memory() -> Dict[str, Any]:
    import copy

    return copy.deepcopy(EMPTY_MEMORY)


def list_personas(db: Session, user_id: int) -> List[Persona]:
    return (
        db.query(Persona)
        .filter(Persona.user_id == user_id)
        .order_by(Persona.is_default.desc(), Persona.is_active.desc(), Persona.created_at.asc())
        .all()
    )


def get_persona(db: Session, user_id: int, persona_id: Optional[int] = None) -> Optional[Persona]:
    """Resolve a persona: explicit id → active → default → oldest."""
    query = db.query(Persona).filter(Persona.user_id == user_id)
    if persona_id:
        row = query.filter(Persona.id == persona_id).first()
        if row:
            return row
    # Active first: the user's explicit switch has to win over "default".
    return (
        query.order_by(Persona.is_active.desc(), Persona.is_default.desc(), Persona.id.asc()).first()
    )


def ensure_persona(
    db: Session,
    user_id: int,
    *,
    name: Optional[str] = None,
    target_role: str = "",
    profile: Optional[Dict[str, Any]] = None,
    search_context: Optional[Dict[str, Any]] = None,
    is_default: bool = False,
) -> Persona:
    """Create (or return) a persona. Every user gets one as soon as a profile exists."""
    label = (name or "").strip() or target_role.strip() or "Primary track"
    existing = (
        db.query(Persona)
        .filter(Persona.user_id == user_id, Persona.name == label)
        .first()
    )
    if existing:
        return existing
    row = Persona(
        user_id=user_id,
        name=label[:120],
        target_role=(target_role or label)[:200],
        is_active=True,
        is_default=is_default or not db.query(Persona).filter(Persona.user_id == user_id).first(),
        search_context=search_context or {},
        preferences={},
        memory=_blank_memory(),
        stats={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    make_active(db, row, commit=False)
    db.commit()
    db.refresh(row)
    record_signal(db, user_id, row.id, "persona_created", {"name": row.name, "target_role": row.target_role})
    return row


def ensure_default_persona(db: Session, user_id: int, profile: Optional[Dict[str, Any]] = None) -> Optional[Persona]:
    """Return the user's default persona, creating it from their profile if needed."""
    existing = get_persona(db, user_id)
    if existing:
        return existing
    data = profile
    if data is None:
        row = db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()
        data = (row.data if row else {}) or {}
    if not data:
        return None
    role = str(data.get("current_title") or "").strip()
    return ensure_persona(db, user_id, name=role or "Primary track", target_role=role, profile=data)


def create_persona(
    db: Session,
    user_id: int,
    *,
    name: str,
    target_role: str = "",
    search_context: Optional[Dict[str, Any]] = None,
    preferences: Optional[Dict[str, Any]] = None,
    source_resume_id: Optional[int] = None,
) -> Persona:
    label = (name or "New track").strip()[:120]
    # Track names are unique per user (uq_personas_user_name). A duplicate is a
    # normal user action — "create Data Scientist" twice — so it has to be a
    # clean refusal, not an IntegrityError surfacing as a 500.
    clash = (
        db.query(Persona)
        .filter(Persona.user_id == user_id, func.lower(Persona.name) == label.lower())
        .first()
    )
    if clash:
        raise PersonaExistsError(clash.name, clash.id)

    row = Persona(
        user_id=user_id,
        name=label,
        target_role=(target_role or name).strip()[:200],
        is_active=True,
        is_default=not db.query(Persona).filter(Persona.user_id == user_id).first(),
        search_context=search_context or {},
        preferences=preferences or {},
        memory=_blank_memory(),
        stats={},
        source_resume_id=source_resume_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent create with the same name
        db.rollback()
        raise PersonaExistsError(label, None) from exc
    db.refresh(row)
    make_active(db, row, commit=False)
    db.commit()
    db.refresh(row)
    record_signal(db, user_id, row.id, "persona_created", {"name": row.name, "target_role": row.target_role})
    return row


def update_persona(db: Session, user_id: int, persona_id: int, payload: Dict[str, Any]) -> Persona:
    row = get_persona(db, user_id, persona_id)
    if not row:
        raise LookupError("persona_not_found")
    if isinstance(payload.get("name"), str) and payload["name"].strip():
        row.name = payload["name"].strip()[:120]
    if isinstance(payload.get("target_role"), str):
        row.target_role = payload["target_role"].strip()[:200]
    if isinstance(payload.get("search_context"), dict):
        row.search_context = {**(row.search_context or {}), **_clean_context(payload["search_context"])}
    if isinstance(payload.get("preferences"), dict):
        row.preferences = {**(row.preferences or {}), **payload["preferences"]}
        record_signal(db, user_id, row.id, "preference_set", payload["preferences"])
    if "is_active" in payload:
        row.is_active = bool(payload["is_active"])
    db.commit()
    db.refresh(row)
    return row


def make_active(db: Session, row: Persona, *, commit: bool = True) -> Persona:
    """
    Make ``row`` the user's single active track.

    "Active" has to be a one-of-many state: every other endpoint resolves the
    current persona through :func:`get_persona`, so two active rows would mean a
    resume or email could be written against the wrong track.
    """
    db.query(Persona).filter(Persona.user_id == row.user_id, Persona.id != row.id).update(
        {Persona.is_active: False}, synchronize_session=False
    )
    row.is_active = True
    if commit:
        db.commit()
        db.refresh(row)
    return row


def set_active_persona(db: Session, user_id: int, persona_id: int) -> Persona:
    row = get_persona(db, user_id, persona_id)
    if not row:
        raise LookupError("persona_not_found")
    return make_active(db, row)


def delete_persona(db: Session, user_id: int, persona_id: int) -> bool:
    row = get_persona(db, user_id, persona_id)
    if not row:
        return False
    was_default = bool(row.is_default)
    db.delete(row)
    db.commit()
    if was_default:
        remaining = list_personas(db, user_id)
        if remaining:
            remaining[0].is_default = True
            db.commit()
    return True


# --------------------------------------------------------------------------- #
# Context resolution — persona overrides sit on top of the extracted profile
# --------------------------------------------------------------------------- #
_CONTEXT_KEYS = ("keywords", "roles", "industries", "tech_stack", "locations", "funding_focus")


def _clean_context(context: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key in _CONTEXT_KEYS:
        values = context.get(key)
        if isinstance(values, str):
            values = [part.strip() for part in values.split(",") if part.strip()]
        if isinstance(values, list):
            cleaned[key] = [str(v).strip()[:80] for v in values if str(v).strip()][:24]
    if isinstance(context.get("seniority"), str):
        cleaned["seniority"] = context["seniority"][:20]
    return cleaned


def context_for_persona(
    base_context: Dict[str, Any],
    persona: Optional[Persona],
    *,
    extra_context: str = "",
) -> Dict[str, Any]:
    """
    Merge the persona's track on top of the profile-derived context.

    Persona values come *first* (they are the user's stated intent for this
    track) and the profile-derived values are kept as a safety net, so a
    brand-new persona still searches like the candidate.
    """
    merged = dict(base_context or {})
    if persona is None:
        merged["persona_id"] = None
        merged["persona"] = None
        return merged

    stored = _clean_context(persona.search_context or {})
    for key in _CONTEXT_KEYS:
        persona_values = stored.get(key) or []
        base_values = merged.get(key) or []
        seen, out = set(), []
        for value in list(persona_values) + list(base_values):
            token = str(value).strip().lower()
            if token and token not in seen:
                seen.add(token)
                out.append(str(value).strip())
        if out:
            merged[key] = out
    if stored.get("seniority"):
        merged["seniority"] = stored["seniority"]

    if extra_context.strip():
        merged["extra_context"] = extra_context.strip()
    merged["persona_id"] = persona.id
    merged["persona"] = {"id": persona.id, "name": persona.name, "target_role": persona.target_role}
    merged["source"] = f"{merged.get('source', 'profile')}+persona"
    return merged


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #
def record_signal(
    db: Session,
    user_id: int,
    persona_id: Optional[int],
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Persona]:
    """
    Append one behavioural signal to the persona's memory.

    Called from every funnel step (scoring, resume generation, outreach,
    interviews). Signals are capped; the aggregates are what the portrait reads.
    """
    if not persona_id:
        return None
    row = db.query(Persona).filter(Persona.id == persona_id, Persona.user_id == user_id).first()
    if not row:
        return None
    memory = {**_blank_memory(), **(row.memory or {})}
    payload = payload or {}

    signals = list(memory.get("signals") or [])
    signals.append({"kind": kind, "at": datetime.utcnow().isoformat(),
                    "data": {k: v for k, v in payload.items() if k != "raw"}})
    memory["signals"] = signals[-MAX_SIGNALS:]

    if kind in COUNTER_KEYS:
        memory["engagement"][kind] = int(memory["engagement"].get(kind, 0)) + 1

    titles = dict(memory.get("observed_titles") or {})
    skills = dict(memory.get("observed_skills") or {})
    companies = dict(memory.get("observed_companies") or {})
    for title in _as_values(payload.get("title")):
        titles[title] = int(titles.get(title, 0)) + 1
    for skill in _as_values(payload.get("skills") or payload.get("keywords")):
        skills[skill] = int(skills.get(skill, 0)) + 1
    for company in _as_values(payload.get("company")):
        companies[company] = int(companies.get(company, 0)) + 1
    memory["observed_titles"] = _top(titles)
    memory["observed_skills"] = _top(skills)
    memory["observed_companies"] = _top(companies)

    note = str(payload.get("note") or "").strip()
    if note:
        notes = list(memory.get("notes") or [])
        notes.append({"at": datetime.utcnow().isoformat(), "kind": kind, "text": note[:400]})
        memory["notes"] = notes[-50:]

    row.memory = memory
    row.last_used_at = datetime.utcnow()
    row.stats = _stats(memory)
    db.commit()
    return row


def _as_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _top(counts: Dict[str, int], limit: int = 25) -> Dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit])


def _stats(memory: Dict[str, Any]) -> Dict[str, Any]:
    engagement = memory.get("engagement") or {}
    applied = int(engagement.get("applied", 0))
    replies = int(engagement.get("reply_received", 0))
    interviews = int(engagement.get("interview", 0))
    return {
        "signals": len(memory.get("signals") or []),
        **{key: int(engagement.get(key, 0)) for key in COUNTER_KEYS},
        "reply_rate": round(replies / applied, 3) if applied else 0.0,
        "interview_rate": round(interviews / applied, 3) if applied else 0.0,
        "first_signal_at": (memory.get("signals") or [{}])[0].get("at"),
        "last_signal_at": (memory.get("signals") or [{}])[-1].get("at"),
    }


def memory_summary(persona: Optional[Persona]) -> Dict[str, Any]:
    """Compact, UI-ready view of what the system currently believes."""
    if persona is None:
        return {"signals": 0, "engagement": dict.fromkeys(COUNTER_KEYS, 0), "observed_titles": {},
                "observed_skills": {}, "observed_companies": {}, "notes": [], "maturity": "new"}
    memory = {**_blank_memory(), **(persona.memory or {})}
    signals = len(memory.get("signals") or [])
    maturity = "new" if signals < 5 else ("learning" if signals < 40 else "established")
    return {
        "signals": signals,
        "maturity": maturity,
        "engagement": memory.get("engagement") or {},
        "observed_titles": memory.get("observed_titles") or {},
        "observed_skills": memory.get("observed_skills") or {},
        "observed_companies": memory.get("observed_companies") or {},
        "notes": (memory.get("notes") or [])[-10:],
        "stats": _stats(memory),
    }


# --------------------------------------------------------------------------- #
# Portrait — the reflection of the user, grounded in observed evidence
# --------------------------------------------------------------------------- #
PORTRAIT_SCHEMA = SchemaSpec([
    FieldSpec("headline", "str", min_length=10, max_length=160),
    FieldSpec("identity", "str", min_length=40, max_length=900),
    FieldSpec("strengths", "list", min_length=2),
    FieldSpec("gaps", "list"),
    FieldSpec("positioning", "str", min_length=40, max_length=700),
    FieldSpec("search_directives", "list"),
    FieldSpec("outreach_angle", "str", min_length=20, max_length=400),
    FieldSpec("evidence", "list", min_length=2),
])


def _evidence_pack(persona: Persona, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the portrait is allowed to be built from — nothing more."""
    memory = {**_blank_memory(), **(persona.memory or {})}
    recent = [
        {"kind": s.get("kind"), "at": s.get("at"), **{k: v for k, v in (s.get("data") or {}).items()
                                                     if k in ("title", "company", "score", "score_band",
                                                              "skills", "keywords", "note", "status")}}
        for s in (memory.get("signals") or [])[-60:]
    ]
    return {
        "persona_name": persona.name,
        "target_role": persona.target_role,
        "preferences": persona.preferences or {},
        "search_context": persona.search_context or {},
        "engagement": memory.get("engagement") or {},
        "observed_titles": memory.get("observed_titles") or {},
        "observed_skills": memory.get("observed_skills") or {},
        "observed_companies": memory.get("observed_companies") or {},
        "notes": memory.get("notes") or [],
        "recent_signals": recent,
        "resume_facts": {
            "name": profile.get("name"),
            "current_title": profile.get("current_title"),
            "skills": (profile.get("skills") or [])[:30],
            "companies": [e.get("company") for e in (profile.get("experience") or [])
                          if isinstance(e, dict) and e.get("company")],
            "titles": [e.get("title") for e in (profile.get("experience") or [])
                       if isinstance(e, dict) and e.get("title")],
            "education": [e.get("degree") for e in (profile.get("education") or [])
                          if isinstance(e, dict) and e.get("degree")],
        },
    }


def _portrait_checks(evidence: Dict[str, Any]):
    """
    Guardrails for the reflection, tuned per field.

    Three kinds of statement live in a portrait and only one of them is a
    factual claim about the past:

    * ``strengths`` — assertions about what the candidate has done. These must
      be traceable to the evidence pack, or the model is flattering them with
      something they cannot defend in an interview.
    * ``gaps`` — assertions about what is *absent*. They can never be "supported
      by something the user did"; the failure that actually hurts is naming a
      skill or employer the candidate demonstrably has, so that is what is
      checked.
    * ``search_directives`` — instructions to the discovery engine. They are
      grounded in the candidate's declared interests for this track, not in past
      activity, so the persona's own search context counts as evidence.
    """
    search_context = evidence.get("search_context") or {}
    declared = " ".join(
        " ".join(str(v) for v in (search_context.get(key) or []))
        for key in ("keywords", "industries", "roles", "locations")
    )
    activity = " ".join([
        str(evidence.get("target_role") or ""), str(evidence.get("persona_name") or ""),
        " ".join(str(v) for v in (evidence.get("observed_skills") or {}).keys()),
        " ".join(str(v) for v in (evidence.get("observed_titles") or {}).keys()),
        " ".join(str(v) for v in (evidence.get("observed_companies") or {}).keys()),
        " ".join(str(v) for v in ((evidence.get("resume_facts") or {}).get("skills") or [])),
        " ".join(str(v) for v in ((evidence.get("resume_facts") or {}).get("companies") or [])),
        " ".join(str(v) for v in ((evidence.get("resume_facts") or {}).get("titles") or [])),
        " ".join(str(n.get("text") or "") for n in (evidence.get("notes") or [])),
    ])
    haystack = f"{activity} {declared}".lower()
    demonstrated = {
        re.sub(r"[^a-z0-9+#.]+", "", str(v).lower())
        for group in ((evidence.get("observed_skills") or {}).keys(),
                      (evidence.get("resume_facts") or {}).get("skills") or [])
        for v in group
    } - {""}

    def portrait_evidence_checks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        for item in data.get("strengths") or []:
            text = strip_ai_artifacts(str(item))
            if not text:
                continue
            tokens = [t for t in re.split(r"[^a-z0-9+#.]+", text.lower()) if len(t) > 4]
            if tokens and not any(token in haystack for token in tokens):
                issues.append({"code": "unsupported_claim", "severity": "error", "field": "strengths",
                               "value": text[:120],
                               "message": f"'{text[:80]}' is not supported by anything the user has done."})
        for item in data.get("gaps") or []:
            text = strip_ai_artifacts(str(item))
            if not text:
                continue
            lowered = text.lower()
            # "No recent frontend ownership" is fine; "No Python experience"
            # when Python is on the resume is a hallucinated gap.
            hit = next((skill for skill in demonstrated if skill in lowered), None)
            negated = bool(re.search(r"\b(no|not|without|lacks?|missing|never)\b", lowered))
            if hit and negated:
                issues.append({"code": "contradicted_gap", "severity": "error", "field": "gaps",
                               "value": text[:120],
                               "message": f"'{text[:80]}' contradicts the evidence: {hit} is demonstrated."})
        for item in data.get("search_directives") or []:
            text = strip_ai_artifacts(str(item))
            if not text:
                continue
            tokens = [t for t in re.split(r"[^a-z0-9+#.]+", text.lower()) if len(t) > 4]
            if tokens and not any(token in haystack for token in tokens):
                issues.append({"code": "unsupported_directive", "severity": "error",
                               "field": "search_directives", "value": text[:120],
                               "message": f"'{text[:80]}' is not grounded in this track's declared focus "
                                          "or in the user's recorded activity."})
        claims = " ".join([str(data.get("identity") or ""), str(data.get("positioning") or ""),
                           str(data.get("outreach_angle") or "")])
        issues.extend(check_grounded(claims, build_fact_ledger(
            {"experience": [{"company": c} for c in ((evidence.get("resume_facts") or {}).get("companies") or [])],
             "skills": ((evidence.get("resume_facts") or {}).get("skills") or []),
             "raw_text": haystack},
        ), check_companies=True, check_dates=True, check_contacts=False))
        return issues

    return portrait_evidence_checks


async def build_portrait(db: Session, user: User, persona: Persona, *, force: bool = False) -> Dict[str, Any]:
    """
    Rebuild the persona's portrait from observed evidence.

    Fails loudly (``AIUnavailableError``) when the model is unreachable and
    (``GuardrailError``) when the reflection is not backed by evidence — the
    previous portrait is left untouched in both cases.
    """
    profile_row = (
        db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    )
    profile = (profile_row.data if profile_row else {}) or {}
    evidence = _evidence_pack(persona, profile)

    import json as _jsonlib

    from app.services.ai_client import fit_prompt_part, input_budget_chars

    budget = input_budget_chars(db=db, user_id=int(user.id))
    evidence_json, _truncated = fit_prompt_part(_jsonlib.dumps(evidence, default=str), budget,
                                                label="persona.evidence")
    prompt = (
        "You are the long-term memory of a job-search copilot. Using ONLY the evidence pack below, "
        "write an honest portrait of this candidate for this specific job track.\n\n"
        "Rules:\n"
        "- Never invent employers, skills, titles, dates, metrics or achievements.\n"
        "- 'identity' is 3-5 sentences: who they are on this track, what they have actually done, "
        "and what the data shows they want.\n"
        "- 'strengths' and 'gaps' must each be phrased as concrete, observable statements (max 12 words each).\n"
        "- 'search_directives' are 3-6 short search instructions for the discovery engine "
        "(e.g. 'prioritise product analytics roles over pure BI reporting').\n"
        "- 'outreach_angle' is the single most credible hook for cold outreach on this track.\n"
        "- 'evidence' lists the exact signals you relied on (quote them).\n"
        f"\nEvidence pack:\n{evidence_json}\n\n"
        "Return JSON with keys: headline, identity, strengths[], gaps[], positioning, "
        "search_directives[], outreach_angle, evidence[]."
    )

    data, report = await run_guarded_task(
        "persona",
        system=("You are a precise, evidence-bound career analyst. You never state anything that is "
                "not present in the supplied evidence."),
        prompt=prompt,
        schema=PORTRAIT_SCHEMA,
        checks=[_portrait_checks(evidence)],
        db=db,
        user_id=user.id,
        temperature=0.3,
    )

    portrait = {
        "headline": strip_ai_artifacts(str(data.get("headline") or ""))[:200],
        "identity": strip_ai_artifacts(str(data.get("identity") or "")),
        "strengths": [strip_ai_artifacts(str(v))[:160] for v in (data.get("strengths") or [])][:10],
        "gaps": [strip_ai_artifacts(str(v))[:160] for v in (data.get("gaps") or [])][:10],
        "positioning": strip_ai_artifacts(str(data.get("positioning") or "")),
        "search_directives": [strip_ai_artifacts(str(v))[:200] for v in (data.get("search_directives") or [])][:8],
        "outreach_angle": strip_ai_artifacts(str(data.get("outreach_angle") or "")),
    }
    persona.portrait = _json(portrait, 6000)
    persona.portrait_evidence = {
        "evidence": [str(v)[:300] for v in (data.get("evidence") or [])][:12],
        "signals_used": len((evidence.get("recent_signals") or [])),
        "engagement": evidence.get("engagement") or {},
        "guardrail": report.to_dict(),
    }
    persona.portrait_at = datetime.utcnow()
    # Fold the directives into the track's own search context so discovery and
    # scoring immediately act on them.
    directives = portrait["search_directives"]
    if directives:
        context = _clean_context(persona.search_context or {})
        keywords = list(dict.fromkeys(directives + (context.get("keywords") or [])))[:24]
        persona.search_context = {**context, "keywords": keywords}
    db.commit()
    db.refresh(persona)
    return {"portrait": portrait, "evidence": persona.portrait_evidence, "guardrail": report.to_dict()}


def portrait_dict(persona: Optional[Persona]) -> Dict[str, Any]:
    if persona is None or not persona.portrait:
        return {}
    try:
        import json as _json_mod

        parsed = _json_mod.loads(persona.portrait)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {"identity": persona.portrait}


def portrait_is_stale(persona: Optional[Persona], *, days: int = 7) -> bool:
    if persona is None or not persona.portrait_at:
        return True
    return datetime.utcnow() - persona.portrait_at > timedelta(days=max(1, days))


# --------------------------------------------------------------------------- #
# Track suggestions — "you could run these two searches in parallel"
# --------------------------------------------------------------------------- #
TRACK_SCHEMA = SchemaSpec([
    FieldSpec("tracks", "list", min_length=1),
])


async def suggest_tracks(db: Session, user: User, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """AI-suggested personas for a candidate whose resume supports several tracks."""
    import json as _jsonlib

    from app.services.ai_client import fit_prompt_part, input_budget_chars

    ledger = build_fact_ledger(profile)
    budget = input_budget_chars(db=db, user_id=int(user.id))
    facts_json, _truncated = fit_prompt_part(
        _jsonlib.dumps({'name': profile.get('name'), 'current_title': profile.get('current_title'),
                        'skills': (profile.get('skills') or [])[:40], 'experience': profile.get('experience') or [],
                        'education': profile.get('education') or [], 'summary': profile.get('summary')},
                       default=str),
        budget, label="persona.facts")
    prompt = (
        "This candidate's resume supports more than one job-search track. Suggest 2-4 distinct "
        "personas they could run in parallel (for example 'Data Analyst' and 'Data Scientist').\n\n"
        "Rules:\n"
        "- Every track must be supported by skills or roles that are actually in the resume.\n"
        "- Each track needs a distinct target role, keyword set and positioning — no duplicates.\n"
        "- 'why' explains, in one sentence, which part of the resume justifies the track.\n\n"
        f"Resume facts:\n{facts_json}\n\n"
        "Return JSON: {\"tracks\": [{\"name\", \"target_role\", \"keywords\"[], \"industries\"[], "
        "\"seniority\", \"why\"}]}"
    )
    data, _report = await run_guarded_task(
        "persona",
        system="You only propose career tracks that the candidate's own resume already supports.",
        prompt=prompt,
        schema=TRACK_SCHEMA,
        ledger=ledger,
        db=db,
        user_id=user.id,
        temperature=0.3,
    )
    tracks: List[Dict[str, Any]] = []
    for entry in (data.get("tracks") or [])[:4]:
        if not isinstance(entry, dict):
            continue
        tracks.append({
            "name": strip_ai_artifacts(str(entry.get("name") or ""))[:120],
            "target_role": strip_ai_artifacts(str(entry.get("target_role") or ""))[:200],
            "keywords": [strip_ai_artifacts(str(k))[:80] for k in (entry.get("keywords") or [])][:16],
            "industries": [strip_ai_artifacts(str(k))[:60] for k in (entry.get("industries") or [])][:8],
            "seniority": str(entry.get("seniority") or "")[:20],
            "why": strip_ai_artifacts(str(entry.get("why") or ""))[:400],
        })
    return [t for t in tracks if t["name"]]


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def to_dict(persona: Persona, *, include_memory: bool = True) -> Dict[str, Any]:
    data = {
        "id": persona.id,
        "name": persona.name,
        "target_role": persona.target_role,
        "is_active": bool(persona.is_active),
        "is_default": bool(persona.is_default),
        "search_context": persona.search_context or {},
        "preferences": persona.preferences or {},
        "portrait": portrait_dict(persona),
        "portrait_at": persona.portrait_at.isoformat() if persona.portrait_at else None,
        "stats": persona.stats or {},
        "last_used_at": persona.last_used_at.isoformat() if persona.last_used_at else None,
        "created_at": persona.created_at.isoformat() if persona.created_at else None,
    }
    if include_memory:
        data["memory"] = memory_summary(persona)
    return data


def _json(value: Any, limit: int) -> str:
    import json

    return json.dumps(value, default=str)[:limit]


__all__ = [
    "SIGNAL_KINDS", "list_personas", "get_persona", "ensure_persona", "ensure_default_persona",
    "create_persona", "update_persona", "set_active_persona", "delete_persona",
    "context_for_persona", "record_signal", "memory_summary", "build_portrait",
    "portrait_dict", "portrait_is_stale", "suggest_tracks", "to_dict",
    "AIUnavailableError", "GuardrailError",
]

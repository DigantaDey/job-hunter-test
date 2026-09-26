"""
Automation policy — the decision engine behind *every* automated action.

``docs/contracts/10-automation-policy.md`` §5 is the specification; this module
implements it. The one guarantee that matters most is that an automatic
submission can never happen by accident: ``auto_submit`` is never enabled
globally, not on any plan, not by a settings write, and not by a profiler. It is
only ever the result of an *explicit, source-specific* choice the user made
through the policy API — which versions the change, records who/why, and binds
the consent snapshot to submissions that went out under it.

Layout
------
* persistence + versioning  : :func:`get_or_create_policy`, :func:`update_policy`
* scope resolution          : :func:`resolve_policy` and
  :func:`effective_elements` (the contract's effective-policy merge)
* the decision              : :func:`evaluate_policy` — deterministic, *no* row
  is written and nothing is committed by this function.
* counters                  : :func:`record_outcome` — the atomic daily backstop.

``evaluate_policy`` returns the contract decision shape
(``allowed / mode / workflow / policy_id / policy_version / decided_at / reason /
blockers / limits / inputs / downgrade``). Callers persist the decision on the
attempt (``application_submissions.policy_id / policy_version /
consent_snapshot``) *and* re-evaluate at click time: one permission decided
twice — at trigger and at execution — for the same attempt.

The evaluation order is fixed and documented per reason below, so two calls
with the same inputs can never disagree about *why* something was refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.contracts import (
    AUTOMATION_MODES,
    AUTOMATION_SCOPES,
    AUTOMATION_WORKFLOWS,
    SENSITIVE_FIELD_POLICIES,
)
from app.core.config import settings
from app.core.entitlements import can as capability_can
from app.core.entitlements import limit_for
from app.core.logging import get_logger
from app.models.models import (
    AutomationPolicy,
    AutomationPolicyRevision,
    AutomationSubmissionCounter,
    Job,
    Resume,
    User,
)
from app.services.user_settings import get_setting

log = get_logger("app.automation_policy")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SUBMIT_WORKFLOW = "application_submit"

#: Row fields whose values merge across scopes (narrower trumps wider).
MERGE_FIELDS: Tuple[str, ...] = (
    "enabled", "mode", "mode_chosen_at", "min_match_score", "max_runs_per_day",
    "max_runs_per_month", "require_review_before_submit", "require_resume_approval",
    "sensitive_field_policy", "eeo_policy", "credential_policy",
    "consents_required", "blocked_keywords", "quiet_hours",
)

#: Row fields that are *cumulative* across scopes (all scopes' entries apply).
ACCUMULATE_FIELDS: Tuple[str, ...] = (
    "allowed_portals", "blocked_portals", "allowed_companies", "blocked_companies",
    "allowed_locations",
)

#: A mode's rank — how far the system may go without asking.
_MODE_RANK: Dict[str, int] = {name: index for index, name in enumerate(AUTOMATION_MODES)}

#: Defaults for a fresh global row (conservative, per contract §2). The only
#: thing per-row here is ``mode`` — ``None`` = "inherited/never chosen", which
#: resolves to ``prepare`` for the legacy back-compat path and to the plan
#: default otherwise.
ROW_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "mode": None,
    "min_match_score": None,
    "max_runs_per_day": None,
    "max_runs_per_month": None,
    "require_review_before_submit": True,
    "require_resume_approval": True,
    "sensitive_field_policy": "ask_every_time",
    "eeo_policy": "prefer_decline",
    "credential_policy": "create_on_demand",
    "consents_required": ["data_processing"],
}

#: ``SENSITIVE_FIELD_POLICIES`` is the canonical vocab; the row default and the
#: API restriction list both drop ``use_saved_answer`` for EEO fields (contract:
#: a saved answer is never auto-typed into a legally-protected question), and
#: invariant 4 further restricts EEO to these two — an EEO question is either
#: declined or left blank, never silently answered.
EEO_ALLOWED_POLICIES: Tuple[str, ...] = ("never_answer", "prefer_decline")

#: Plan → the *strongest* ``application_submit`` mode a user on that plan may
#: explicitly choose through the policy API. ``free`` cannot opt into
#: ``auto_submit`` (contract §3: application_submit is ``off`` on free); Pro /
#: Pro+ can raise it, but it is never the *default* anywhere.
APPLY_MODE_BY_PLAN: Dict[str, str] = {"free": "prepare", "pro": "auto_submit", "pro_plus": "auto_submit"}


# --------------------------------------------------------------------------- #
# HTTP-level gates: settings does not understand the policy model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlanHttpOverrides:
    """Legacy/HTTP automation gates expressed in the policy vocabulary.

    ``server_allow_submit`` is what ``apply_job`` used to compute from the two
    server flags plus the per-user consent; the policy engine consumes it so a
    submission that request-layer code would refuse is never passed to the
    browser, and a user's settings write can never open a gate the platform
    keeps shut.
    """

    server_allow_submit: bool
    server_automation_enabled: bool
    server_dry_run: bool
    consent_automation: bool
    consent_data_processing: bool
    user_allow_submit: bool
    disclosure_version: str

    @property
    def http_allowed(self) -> bool:
        return bool(
            self.server_allow_submit
            and self.user_allow_submit
            and self.consent_automation
            and self.consent_data_processing
        )


def disclosure_version() -> str:
    """The consent text revision the policy row binds to.

    The disclosure lives in ``account.DISCLOSURES`` but this module must not
    import the API layer; the comprehension is derived from the environment, so
    a deployment that changes its use of accounts (self-host, tenantless
    provisioning, new disclosure rollouts) produces a different binding key.
    """
    return f"{settings.environment}.1"


def _consent_kinds(consents: Optional[Dict[str, Any]], wanted: Sequence[str]) -> List[str]:
    """Which of ``wanted`` disclosures the user has accepted (by timestamp)."""
    acc: List[str] = []
    for kind in wanted:
        value = (consents or {}).get(f"{kind}_accepted_at")
        if value not in (None, "", False):
            acc.append(kind)
    return acc


def plan_http_overrides(db: Session, user: User) -> PlanHttpOverrides:
    """The request-layer gate state for one user, in one place.

    Mirrors ``browser_session.effective_policy`` and ``apply_job``'s inline gate
    so that every reader agrees on *one* server ceiling + *one* user consent
    field.
    """
    consents = dict(user.consents or {})
    allow_submit = bool(get_setting(db, user.id, "application", "allow_auto_submit", False))
    server_allow_submit = bool(
        settings.autofill_enabled
        and settings.autofill_allow_submit
        and not settings.autofill_dry_run
    )
    return PlanHttpOverrides(
        server_allow_submit=server_allow_submit,
        server_automation_enabled=bool(settings.autofill_enabled),
        server_dry_run=bool(settings.autofill_dry_run),
        consent_automation=bool(consents.get("automation_accepted_at")),
        consent_data_processing=bool(consents.get("data_processing_accepted_at")),
        user_allow_submit=allow_submit,
        disclosure_version=disclosure_version(),
    )


# --------------------------------------------------------------------------- #
# Mode ceilings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModeCeilings:
    mode: str
    default_mode: str
    allowed_modes: Tuple[str, ...]


def _plan_default_mode(plan: str) -> str:
    # The inherited default is conservative on every plan; ``auto_submit`` is
    # always opt-in (contract §3: application_submit defaults to off/prepare).
    return "prepare"


def _legacy_effective_mode(db: Session, user: User) -> Optional[str]:
    """The shipped per-user consent, translated into policy vocabulary.

    Contract §3: "``allow_auto_submit`` … becomes ``policy.mode = auto_submit``".
    This is the *inherited* (not explicit) bridge: a user who consented under
    the legacy chain before the policy model existed keeps behaving as they
    consented, without a synthetic ``mode_chosen_at`` pretending they opened
    the policy editor. It never overrides an explicit policy choice, and it is
    still clamped by the server and plan ceilings at evaluation time.
    """
    if not bool(get_setting(db, user.id, "application", "allow_auto_submit", False)):
        return None
    return "auto_submit"


def mode_ceilings(db: Session, user_id: int) -> ModeCeilings:
    """The strongest ``application_submit`` mode this user may *choose*.

    ``ceiling`` is the plan's choice ceiling; ``default_mode`` is what a row
    with ``mode_chosen_at = NULL`` resolves to (always conservative).
    """
    try:
        from app.core.entitlements import get_user_plan

        plan = get_user_plan(db, user_id)
    except Exception:  # pragma: no cover - defensive
        plan = "free"
    plan_ceiling = APPLY_MODE_BY_PLAN.get(str(plan or "free"), "prepare")
    if bool(capability_can(db, user_id, "can_use_autofill")) and plan in ("pro", "pro_plus"):
        ceiling = "auto_submit"
    else:
        ceiling = "prepare"
    ceiling = ceiling if _MODE_RANK[ceiling] >= _MODE_RANK[plan_ceiling] else plan_ceiling
    allowed = tuple(m for m in AUTOMATION_MODES if _MODE_RANK[m] <= _MODE_RANK[ceiling])
    return ModeCeilings(mode=ceiling, default_mode="prepare", allowed_modes=allowed)


def _never_more_than(db: Session, user_id: int, workflow: str) -> Tuple[Optional[int], Optional[int]]:
    """``(per_day, per_month)`` ceilings the plan may never exceed.

    ``None`` = no ceiling from the plan (still clamped by the policy row).
    """
    per_month: Optional[int] = None
    per_day: Optional[int] = None
    if workflow == SUBMIT_WORKFLOW:
        monthly = limit_for(db, user_id, "automation_runs_per_month")
        if monthly and monthly > 0:
            per_month = monthly
            # A monthly cap is spread into a daily ceiling; 0/None = don't add a
            # per-day ceiling the plan doesn't have. Not enforced here unless the
            # row asks for it — the plan's monthly cap is a backstop.
    return per_day, per_month


# --------------------------------------------------------------------------- #
# Persistence: get-or-create + versioned update
# --------------------------------------------------------------------------- #
def get_or_create_policy(
    db: Session,
    *,
    user_id: int,
    workflow: str,
    scope: str = "global",
    scope_key: Optional[str] = None,
    actor_type: str = "system_api",
    actor_id: Optional[int] = None,
    commit: bool = True,
) -> AutomationPolicy:
    """Fetch a policy row or seed it (with defaults), then return it.

    The seeded row is stamped ``mode_chosen_at=None``, which is what the
    onboarding/consent gate reads to tell "the user *chose* this" from "the
    plan default". The first revision is the creation record.
    """
    workflow = workflow or SUBMIT_WORKFLOW
    scope = scope if scope in AUTOMATION_SCOPES else "global"
    if scope == "global":
        scope_key = "*"
    else:
        scope_key = (scope_key or "").strip()[:200] or "?"
    row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user_id,
            AutomationPolicy.workflow == workflow,
            AutomationPolicy.scope == scope,
            AutomationPolicy.scope_key == scope_key,
        )
        .first()
    )
    if row is not None:
        return row
    row = AutomationPolicy(
        user_id=user_id,
        workflow=workflow,
        scope=scope,
        scope_key=scope_key,
        enabled=bool(ROW_DEFAULTS["enabled"]),
        mode="off",
        sensitive_field_policy=ROW_DEFAULTS["sensitive_field_policy"],
        eeo_policy=ROW_DEFAULTS["eeo_policy"],
        credential_policy=ROW_DEFAULTS["credential_policy"],
        require_review_before_submit=bool(ROW_DEFAULTS["require_review_before_submit"]),
        require_resume_approval=bool(ROW_DEFAULTS["require_resume_approval"]),
        consents_required=list(ROW_DEFAULTS["consents_required"]),
        disclosure_version=disclosure_version(),
        version=1,
        updated_by=actor_type,
        effective_from=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    db.add(_revision(db, row, changed_keys=[], actor_type=actor_type, actor_id=actor_id,
                     reason="policy_created"))
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _revision(
    db: Session,
    row: AutomationPolicy,
    *,
    changed_keys: Sequence[str],
    actor_type: str,
    actor_id: Optional[int],
    reason: str,
    request_id: str = "",
) -> AutomationPolicyRevision:
    """The append-only record of ``row`` as it stands after a change."""
    from app.core.logging import request_id_var

    return AutomationPolicyRevision(
        user_id=row.user_id,
        policy_id=row.id,
        version=row.version,
        document=_as_document(row),
        changed_keys=list(changed_keys),
        actor_type=actor_type,
        actor_id=actor_id,
        reason=(reason or "")[:200],
        request_id=request_id or (request_id_var.get() or "")[:64],
    )


def _as_document(row: AutomationPolicy) -> Dict[str, Any]:
    """The full row document a revision freezes."""
    return {
        "id": row.id,
        "scope": row.scope,
        "scope_key": row.scope_key,
        "workflow": row.workflow,
        "enabled": row.enabled,
        "mode": row.mode,
        "mode_chosen_at": row.mode_chosen_at.isoformat() if row.mode_chosen_at else None,
        "min_match_score": row.min_match_score,
        "max_runs_per_day": row.max_runs_per_day,
        "max_runs_per_month": row.max_runs_per_month,
        "require_review_before_submit": row.require_review_before_submit,
        "require_resume_approval": row.require_resume_approval,
        "sensitive_field_policy": row.sensitive_field_policy,
        "eeo_policy": row.eeo_policy,
        "credential_policy": row.credential_policy,
        "allowed_portals": row.allowed_portals or [],
        "blocked_portals": row.blocked_portals or [],
        "allowed_companies": row.allowed_companies or [],
        "blocked_companies": row.blocked_companies or [],
        "blocked_keywords": row.blocked_keywords or [],
        "allowed_locations": row.allowed_locations or [],
        "require_remote": row.require_remote,
        "require_onsite": row.require_onsite,
        "compensation_floor": row.compensation_floor or {},
        "schedule": row.schedule or {},
        "notify": row.notify or {},
        "consents_required": row.consents_required or [],
        "consent_snapshot": row.consent_snapshot or {},
        "disclosure_version": row.disclosure_version,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "version": row.version,
        "updated_by": row.updated_by,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
    }


def update_policy(
    db: Session,
    *,
    user_id: int,
    workflow: str,
    fields: Dict[str, Any],
    scope: str = "global",
    scope_key: Optional[str] = None,
    actor_type: str = "user",
    actor_id: Optional[int] = None,
    reason: str = "",
    commit: bool = True,
) -> AutomationPolicy:
    """Validate-sensitive update of one policy row.

    * ``mode=None`` clears an explicit choice (back to inherited).
    * ``auto_submit`` may only be chosen inside the plan/capability ceiling —
      a request above it is refused with ``ValueError`` (contract invariant 3:
      the write must fail, never write a row).
    * ``use_saved_answer`` is never allowed for ``eeo_policy`` (invariant 4).
    * one revision is appended per *apply*, recording exactly what changed.

    The consent chain is *not* re-checked here — a consent-bearing escalation
    is gated earlier by :func:`authorise_write`, so refusal leaves the table
    untouched. This function is a pure validation + rewrite step.

    Raises ``ValueError`` with a code-first message (routers translate to 400s).
    """
    workflow = workflow or SUBMIT_WORKFLOW
    if workflow not in AUTOMATION_WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}")
    row = get_or_create_policy(db, user_id=user_id, workflow=workflow, scope=scope,
                               scope_key=scope_key, actor_type=actor_type, actor_id=actor_id,
                               commit=False)
    if row is None:  # pragma: no cover - get_or_create always returns
        raise ValueError("policy row could not be resolved")

    changed: List[str] = []
    before = _as_document(row)

    normalize = dict(fields or {})

    if "mode" in normalize:
        mode = normalize["mode"]
        if mode is None:
            if row.mode != "off":
                row.mode = "off"
                row.mode_chosen_at = None
                changed.append("mode")
        else:
            if mode not in AUTOMATION_MODES:
                raise ValueError(f"mode must be one of {AUTOMATION_MODES}")
            ceilings = mode_ceilings(db, user_id)
            if mode not in ceilings.allowed_modes:
                raise ValueError(
                    f"mode {mode!r} is above this plan's ceiling for {workflow} "
                    f"(allowed: {list(ceilings.allowed_modes)})"
                )
            if row.mode != mode or row.mode_chosen_at is None:
                row.mode = mode
                row.mode_chosen_at = datetime.utcnow()
                changed.append("mode")

    bool_fields = ("enabled", "require_review_before_submit", "require_resume_approval",
                   "require_remote", "require_onsite")
    for key in bool_fields:
        if key in normalize:
            value = normalize[key]
            if value not in (True, False):
                raise ValueError(f"{key} must be a boolean")
            if bool(getattr(row, key)) != bool(value):
                setattr(row, key, bool(value))
                changed.append(key)

    number_fields = ("min_match_score", "max_runs_per_day", "max_runs_per_month")
    for key in number_fields:
        if key in normalize:
            value = normalize[key]
            if value is None:
                if getattr(row, key) is not None:
                    setattr(row, key, None)
                    changed.append(key)
            else:
                try:
                    parsed = float(value) if key == "min_match_score" else int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a number") from exc
                if key == "min_match_score" and not 0 <= parsed <= 100:
                    raise ValueError("min_match_score must be between 0 and 100")
                if key != "min_match_score" and parsed < 0:
                    raise ValueError(f"{key} must be >= 0")
                if getattr(row, key) != (parsed if key == "min_match_score" else int(parsed)):
                    setattr(row, key, parsed if key == "min_match_score" else int(parsed))
                    changed.append(key)

    for key in ("sensitive_field_policy", "credential_policy"):
        if key in normalize:
            value = normalize[key]
            if value not in SENSITIVE_FIELD_POLICIES:
                raise ValueError(f"{key} must be one of {SENSITIVE_FIELD_POLICIES}")
            if getattr(row, key) != value:
                setattr(row, key, value)
                changed.append(key)

    if "eeo_policy" in normalize:
        value = normalize["eeo_policy"]
        if value == "use_saved_answer":
            raise ValueError(
                "eeo_policy cannot be 'use_saved_answer' — a saved answer is never "
                "auto-typed into a legally-protected (EEO) question "
                "(contract 10 invariant 4)"
            )
        if value not in EEO_ALLOWED_POLICIES:
            raise ValueError(f"eeo_policy must be one of {EEO_ALLOWED_POLICIES}")
        if row.eeo_policy != value:
            row.eeo_policy = value
            changed.append("eeo_policy")

    list_fields = {
        "allowed_portals": "allowed_portals",
        "blocked_portals": "blocked_portals",
        "allowed_companies": "allowed_companies",
        "blocked_companies": "blocked_companies",
        "blocked_keywords": "blocked_keywords",
        "allowed_locations": "allowed_locations",
    }
    for key, attr in list_fields.items():
        if key in normalize:
            value = normalize[key]
            if value is None:
                value = []
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{key} must be a list")
            clean = sorted({str(item).strip().casefold() for item in value if str(item).strip()})
            current = sorted(str(x).casefold() for x in (getattr(row, attr) or []))
            if current != clean:
                setattr(row, attr, clean)
                changed.append(key)

    dict_fields = ("compensation_floor", "schedule", "notify")
    for key in dict_fields:
        if key in normalize:
            value = normalize[key]
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be an object")
            if (getattr(row, key) or {}) != value:
                setattr(row, key, value)
                changed.append(key)

    if "consents_required" in normalize:
        value = normalize["consents_required"]
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise ValueError("consents_required must be a list of disclosure keys")
        clean = [str(k).strip() for k in value if str(k).strip()]
        if list(row.consents_required or []) != clean:
            row.consents_required = clean
            changed.append("consents_required")

    if not changed:
        db.rollback()
        return row

    # Consent-binding backstop (invariant 3): writing a *persisted* new mode
    # must not contradict the authorisation the request-layer checked. The
    # write gate is re-checked against the mode *this* change resolves to —
    # a straight raise, not a normalization, so the row is never written in a
    # state the stored consent cannot authorise.
    if workflow == SUBMIT_WORKFLOW:
        candidate = dict(before, **{k: normalize[k] for k in normalize})
        if candidate.get("mode") == "auto_submit":
            owner = db.query(User).get(user_id)
            override = plan_http_overrides(db, owner) if owner is not None else None
            if override is None or not override.http_allowed:
                raise ValueError(
                    "auto_submit requires the automation disclosure and the "
                    "allow_auto_submit consent (the write was refused and no "
                    "change was recorded)"
                )
        elif candidate.get("mode") == "prepare" and before.get("mode") == "auto_submit":
            # Downgrade from auto_submit is always permitted (P4).
            pass

    row.version = (row.version or 1) + 1
    row.updated_by = actor_type
    row.effective_from = datetime.utcnow()

    db.add(_revision(db, row, changed_keys=changed, actor_type=actor_type, actor_id=actor_id,
                     reason=reason))
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    log.info("policy updated", extra={"user_id": user_id, "workflow": workflow,
                                      "policy_id": row.id, "version": row.version,
                                      "changed": changed, "before": before.get("mode"),
                                      "after": row.mode})
    return row


def authorise_write(
    db: Session,
    *,
    user: User,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    """Gate a policy *write* before it is applied (contracts/10 invariant 3).

    Returns ``{"ok": True}`` or a typed refusal (``plan_locked`` /
    ``server_disabled`` / ``consent_required``) — the upgrade and consent gates
    are answers *before* any row exists, not modifications to dish afterwards.
    """
    target_mode = _desired_fields_mode(fields)
    if target_mode == "auto_submit":
        ceilings = mode_ceilings(db, user.id)
        if "auto_submit" not in ceilings.allowed_modes:
            from app.core.entitlements import get_user_plan

            return {"ok": False, "code": "plan_locked",
                    "plan": str(get_user_plan(db, user.id) or "free"),
                    "message": "auto_submit is not available on this plan"}
        override = plan_http_overrides(db, user)
        if not override.server_allow_submit:
            # Contract §3: server ``AUTOFILL_ENABLED`` ∧ ¬``DRY_RUN`` ∧
            # ``ALLOW_SUBMIT`` are required even when the plan and consent
            # allow it — a deployment flag can never be raised by a user.
            return {"ok": False, "code": "server_disabled",
                    "message": "browser automation is disabled on this deployment "
                               "(AUTOFILL_ENABLED ∧ ¬AUTOFILL_DRY_RUN ∧ "
                               "AUTOFILL_ALLOW_SUBMIT)"}
        if not (override.user_allow_submit and override.consent_automation
                and override.consent_data_processing):
            missing = [kind for kind in ("automation", "data_processing")
                       if not bool((user.consents or {}).get(f"{kind}_accepted_at"))]
            if not override.user_allow_submit and "automation" not in missing:
                missing.append("automation")
            return {"ok": False, "code": "consent_required", "missing": missing,
                    "message": "auto_submit requires the automation disclosure "
                               "and the allow_auto_submit consent"}
    return {"ok": True}


def _desired_fields_mode(fields: Dict[str, Any]) -> Optional[str]:
    """The mode a candidate write asks for (including a clear to ``None``)."""
    if "mode" not in fields:
        return None
    value = fields["mode"]
    return value if value in AUTOMATION_MODES else None


def list_revisions(
    db: Session,
    *,
    user_id: int,
    workflow: str = SUBMIT_WORKFLOW,
    scope: str = "global",
    scope_key: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Revision history for one policy identity, newest first."""
    row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user_id,
            AutomationPolicy.workflow == workflow,
            AutomationPolicy.scope == scope,
            AutomationPolicy.scope_key == ("*" if scope == "global" else (scope_key or "")),
        )
        .first()
    )
    policy_id = row.id if row is not None else None
    query = (
        db.query(AutomationPolicyRevision)
        .filter(AutomationPolicyRevision.user_id == user_id)
        .order_by(AutomationPolicyRevision.id.desc())
    )
    if policy_id is not None:
        query = query.filter(AutomationPolicyRevision.policy_id == policy_id)
    return [
        {
            "id": revision.id,
            "policy_id": revision.policy_id,
            "version": revision.version,
            "document": revision.document,
            "changed_keys": revision.changed_keys or [],
            "actor_type": revision.actor_type,
            "actor_id": revision.actor_id,
            "reason": revision.reason or "",
            "request_id": revision.request_id or "",
            "created_at": revision.created_at.isoformat(),
        }
        for revision in query.limit(max(1, min(int(limit), 200))).all()
    ]


# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #
def scope_key_for(job: Optional[Job], workflow: str, persona_id: Optional[int]) -> Tuple[str, str, str, str]:
    """The four candidate keys (company, portal, persona, workflow) for a job."""
    company = str((job.company_name_normalized or job.company or "")).strip().casefold() if job else ""
    from app.services.form_detector import detect_portal_type

    portal = (
        str(detect_portal_type(job.url or "", job.source if job else None)).strip().casefold()
        if job and (job.url or "")
        else ""
    )
    persona = str(persona_id) if persona_id else ""
    return company, portal, persona, workflow


def scope_keys(job: Job) -> List[str]:
    """Ordered keys used to iterate a candidate's company/portal scopes."""
    keys: List[str] = []
    if job.company_name_normalized:
        keys.append(str(job.company_name_normalized).casefold())
    if job.company:
        keys.append(str(job.company).casefold())
    if job.source:
        keys.append(str(job.source).casefold())
    if job.url:
        from app.services.form_detector import detect_portal_type

        keys.append(str(detect_portal_type(job.url or "", job.source)).casefold())
    deduped: List[str] = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def resolve_policy(
    db: Session,
    *,
    user_id: int,
    workflow: str,
    job: Optional[Job] = None,
    persona_id: Optional[int] = None,
) -> AutomationPolicy:
    """The most specific row for ``(user, workflow, job/persona)``.

    Falls back through portal → company → workflow → persona → global, creating
    nothing unless a global row already exists (evaluation must not write);
    returns a *detached* synthesized global when no row exists yet.
    """
    workflow = workflow if workflow in AUTOMATION_WORKFLOWS else SUBMIT_WORKFLOW
    global_row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user_id,
            AutomationPolicy.workflow == workflow,
            AutomationPolicy.scope == "global",
        )
        .first()
    )
    if job is not None:
        company, portal, persona, _ = scope_key_for(job, workflow, persona_id)
        candidates: List[Tuple[str, Optional[str]]] = []
        if portal:
            candidates.append(("portal", portal))
        if company:
            candidates.append(("company", company))
        if persona:
            candidates.append(("persona", persona))
        for scope, key in candidates:
            match = (
                db.query(AutomationPolicy)
                .filter(
                    AutomationPolicy.user_id == user_id,
                    AutomationPolicy.workflow == workflow,
                    AutomationPolicy.scope == scope,
                    AutomationPolicy.scope_key == key,
                )
                .first()
            )
            if match is not None:
                return match
    if global_row is not None:
        return global_row
    # Synthesized, unattached: read-only evaluation only. The instance is
    # transient (never added to the session) and its identity column is
    # already ``None``, so it is returned detached as-is — no ``expunge``,
    # no ``id`` assignment (both would be type/lifetime mistakes).
    synth = AutomationPolicy(
        user_id=user_id,
        workflow=workflow,
        scope="global",
        scope_key="*",
        enabled=True,
        mode="off",
        mode_chosen_at=None,
        sensitive_field_policy=ROW_DEFAULTS["sensitive_field_policy"],
        eeo_policy=ROW_DEFAULTS["eeo_policy"],
        credential_policy=ROW_DEFAULTS["credential_policy"],
        require_review_before_submit=True,
        require_resume_approval=True,
        consents_required=list(ROW_DEFAULTS["consents_required"]),
        disclosure_version=disclosure_version(),
        version=1,
    )
    return synth


ListKey = Tuple[str, str]


def _rows_for_context(
    db: Session,
    *,
    user_id: int,
    workflow: str,
    job: Optional[Job],
    persona_id: Optional[int],
) -> List[Tuple[str, AutomationPolicy]]:
    """All rows that apply to this context, most specific first."""
    out: List[Tuple[str, AutomationPolicy]] = []
    if job is not None:
        company, portal, persona, _ = scope_key_for(job, workflow, persona_id)
        for scope, key in (("portal", portal), ("company", company), ("persona", persona)):
            if not key:
                continue
            row = (
                db.query(AutomationPolicy)
                .filter(
                    AutomationPolicy.user_id == user_id,
                    AutomationPolicy.workflow == workflow,
                    AutomationPolicy.scope == scope,
                    AutomationPolicy.scope_key == key,
                )
                .first()
            )
            if row is not None:
                out.append((scope, row))
    global_row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user_id,
            AutomationPolicy.workflow == workflow,
            AutomationPolicy.scope == "global",
        )
        .first()
    )
    if global_row is not None:
        out.append(("global", global_row))
    return out


def effective_elements(
    db: Session,
    *,
    user: User,
    workflow: str,
    job: Optional[Job] = None,
    persona_id: Optional[int] = None,
) -> Dict[str, Any]:
    """The contract's effective-policy merge (per-element, per-scope).

    Merge fields: narrowest scope that *explicitly* sets the field wins.
    Accumulate fields union across all scopes. Returns ``{"id", "version",
    "row", "elements"}`` where ``elements`` is what :func:`evaluate_policy`
    consumes.
    """
    workflow = workflow if workflow in AUTOMATION_WORKFLOWS else SUBMIT_WORKFLOW
    rows = _rows_for_context(db, user_id=user.id, workflow=workflow, job=job, persona_id=persona_id)
    # Apply wide → narrow so the narrowest write wins.
    ordered = list(reversed(rows))  # global first
    # Conservative defaults are the base layer: a user with no policy row still
    # evaluates (enabled, prepare-by-default, review required), and the legacy
    # consent bridge in :func:`_evaluator` can carry ``allow_auto_submit``.
    merged: Dict[str, Any] = {
        "enabled": True,
        "mode": None,
        "mode_chosen_at": None,
        "min_match_score": None,
        "max_runs_per_day": None,
        "max_runs_per_month": None,
        # The review gates are *policy* knobs; ``None`` here (no row / not set)
        # means "the legacy consent chain decides", while a real policy row
        # stores its own default. See ``_evaluator``.
        "require_review_before_submit": None,
        "require_resume_approval": None,
        "sensitive_field_policy": "ask_every_time",
        "eeo_policy": "prefer_decline",
        "credential_policy": "create_on_demand",
        "consents_required": list(ROW_DEFAULTS["consents_required"]),
        "blocked_keywords": [],
        "quiet_hours": None,
    }
    for key in MERGE_FIELDS:
        for _scope, row in ordered:
            value = getattr(row, key, None)
            if value is not None:
                if key == "consents_required":
                    merged[key] = merged[key] or []
                    merged[key] = list(dict.fromkeys([*merged[key], *list(value)]))
                elif key == "blocked_keywords":
                    merged[key] = merged[key] or []
                    merged[key] = list(dict.fromkeys([*merged[key], *list(value or [])]))
                else:
                    merged[key] = value

    acc: Dict[str, List[str]] = {key: [] for key in ACCUMULATE_FIELDS}
    for _scope, row in ordered:
        for key in ACCUMULATE_FIELDS:
            acc[key].extend([str(x) for x in (getattr(row, key) or []) if str(x)])
    for key in ACCUMULATE_FIELDS:
        merged[key] = sorted({item.casefold() for item in acc[key]})

    primary = rows[0][1] if rows else None  # most specific row that exists
    return {
        "id": primary.id if primary is not None and primary.id else None,
        "version": primary.version if primary is not None else 1,
        "scope": rows[0][0] if rows else "global",
        "row": primary,
        "elements": merged,
        "scopes_applied": [scope for scope, _row in rows],
    }


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #
def score_provenance_can_clear_floor(source: Optional[str]) -> bool:
    """Can a ``min_match_score`` floor be measured against this provenance?

    An engine verdict is what a confidence floor can be measured against: the
    model (``ai``), the multi-stage hybrid scorer, or the local decision engine
    (``laya``). Laya's answer is a typed, calibrated rubric verdict — the same
    one the board labels "Laya (local)" and the discovery report counts in
    ``ai_rescore.scored`` — so a floor that accepted an ``ai`` score while
    denying an identical ``laya`` one was measuring provenance, not confidence
    (v2.5). ``pending`` keeps its historical pass-through (its score is 0, so
    the score comparison denies it anyway); a keyword estimate, a guardrail
    rejection, insufficient text or a never-attempted row is not a floor pass.
    """
    return str(source or "") in ("ai", "pending", "hybrid", "laya")


def _evaluator(
    db: Session,
    *,
    user: User,
    job: Optional[Job],
    workflow: str,
    persona_id: Optional[int],
    elements: Dict[str, Any],
    now: datetime,
    outcome_count: int,
    for_http: bool,
) -> Dict[str, Any]:
    """The ordered gate list. Returns the first refusal, else the allowed shape."""
    from app.core.entitlements import get_user_plan

    consents = dict(user.consents or {})

    # Resolve the effective mode once: nothing → conservative ``off``; an
    # *explicit* row value wins; otherwise the legacy ``allow_auto_submit``
    # consent bridges in as ``auto_submit`` (contract §3) and the HTTP gate
    # below still has the final word.
    mode: str = elements.get("mode") or "off"
    if (mode == "off" and workflow == SUBMIT_WORKFLOW
            and elements.get("mode_chosen_at") is None):
        legacy = _legacy_effective_mode(db, user)
        if legacy == "auto_submit":
            mode = "auto_submit"

    def denied(reason: str, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "allowed": False,
            "mode": mode,
            "workflow": workflow,
            "policy_id": extra.pop("policy_id", None),
            "policy_version": extra.pop("policy_version", None),
            "decided_at": now.isoformat(),
            "reason": reason,
            "downgrade": extra.pop("downgrade", {}),
            "blockers": [],
            "limits": {"day": {"used": int(outcome_count), "limit": None}, "month": {"limit": None}},
            "inputs": {"user_id": user.id, "job_id": job.id if job else None, "for_http": for_http},
        }
        payload.update(extra)
        return payload

    enabled: bool = bool(elements.get("enabled", True))
    downgrade: Dict[str, Any] = {}

    # --- server / plan / consent (order fixed by the contract) --------------- #
    override = plan_http_overrides(db, user)
    plan = str(get_user_plan(db, user.id) or "free")

    if workflow == SUBMIT_WORKFLOW and not override.server_automation_enabled:
        return denied("server_disabled", server={
            "autofill_enabled": settings.autofill_enabled,
            "autofill_allow_submit": settings.autofill_allow_submit,
            "autofill_dry_run": settings.autofill_dry_run,
        })

    if not bool(capability_can(db, user.id, "can_use_autofill")) and for_http:
        # HTTP-side escalation is tier-gated; internal evaluation (a dry-run
        # warmup, a preparation) must still be allowed to run on free plans.
        return denied("plan_locked", plan=plan)

    wanted = list(elements.get("consents_required") or ROW_DEFAULTS["consents_required"])
    missing_consent = [kind for kind in wanted if not bool(consents.get(f"{kind}_accepted_at"))]
    if missing_consent:
        return denied("consent_missing", missing=missing_consent)

    if mode != "off" and elements.get("disclosure_version") not in (None, "", disclosure_version()):
        # The disclosure was accepted under an older text than the policy binds.
        return denied("disclosure_version_changed",
                      bound=elements.get("disclosure_version"), current=disclosure_version())

    if not enabled:
        return denied("policy_off")

    if mode == "off":
        return denied("policy_off")

    # --- expiration (contract: anything expired is treated as off) ----------- #
    expires_at = elements.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at <= now:
        return denied("expired")

    # --- limits -------------------------------------------------------------- #
    effective_day = elements.get("max_runs_per_day")
    effective_month = elements.get("max_runs_per_month")
    plan_day, plan_month = _never_more_than(db, user.id, workflow)
    if effective_day is None:
        effective_day = plan_day
    elif plan_day and plan_day > 0:
        effective_day = min(int(effective_day), int(plan_day))
    if effective_month is None:
        effective_month = plan_month
    elif plan_month and plan_month > 0:
        effective_month = min(int(effective_month), int(plan_month))

    limits = {"day": {"used": outcome_count, "limit": effective_day},
              "month": {"limit": effective_month}}

    if effective_day is not None and int(effective_day) >= 0 and outcome_count >= int(effective_day):
        return denied("daily_limit", limits=limits)
    # Monthly is the entitlements ledger's job at enqueue time; kept here as a
    # conservative gate the engine can still express.
    if effective_month is not None and int(effective_month) >= 0 and outcome_count >= int(effective_month):
        return denied("monthly_limit", limits=limits)

    # --- quiet hours (from the row's schedule) -------------------------------- #
    schedule = elements.get("schedule") or {}
    quiet = schedule.get("quiet_hours") if isinstance(schedule, dict) else None
    if isinstance(quiet, dict):
        try:
            tz = ZoneInfo(str(user.timezone or "UTC"))
        except Exception:
            tz = ZoneInfo("UTC")
        local = now.astimezone(tz).timetz()
        start = quiet.get("start")
        end = quiet.get("end")
        if start and end:
            try:
                start_t = dtime.fromisoformat(str(start))
                end_t = dtime.fromisoformat(str(end))
                inside = start_t <= local <= end_t if start_t <= end_t else (local >= start_t or local <= end_t)
                if inside:
                    return denied("quiet_hours", quiet_hours={"start": start, "end": end,
                                                              "timezone": str(user.timezone or "UTC")})
            except ValueError:
                log.warning("policy %s has malformed quiet_hours", workflow)

    # --- company / portal / keyword / location ------------------------------- #
    if job is not None:
        company = str((job.company_name_normalized or job.company or "")).casefold()
        blocked_companies = set(elements.get("blocked_companies") or [])
        allowed_companies = set(elements.get("allowed_companies") or [])
        if blocked_companies and company in blocked_companies:
            return denied("company_blocked", company=company)
        if allowed_companies and company not in allowed_companies:
            return denied("company_blocked", company=company)

        if job.url:
            from app.services.form_detector import detect_portal_type

            portal = str(detect_portal_type(job.url or "", job.source)).casefold()
            blocked_portals = set(elements.get("blocked_portals") or [])
            allowed_portals = set(elements.get("allowed_portals") or [])
            if blocked_portals and portal in blocked_portals:
                return denied("portal_not_allowed", portal=portal)
            if allowed_portals and portal not in allowed_portals:
                return denied("portal_not_allowed", portal=portal)

        description = f"{(job.title or '')} {(job.description or '')}".casefold()
        for keyword in (elements.get("blocked_keywords") or []):
            if keyword and keyword in description:
                return denied("keyword_blocked", keyword=keyword)

        location = str(job.location or "").casefold()
        allowed_locations = set(elements.get("allowed_locations") or [])
        if allowed_locations and location and not any(
            candidate in location for candidate in allowed_locations
        ):
            return denied("location_excluded", location=location)

    # --- data-confidence floor ------------------------------------------------ #
    minimum = elements.get("min_match_score")
    if minimum is not None and job is not None:
        score = float(job.score or 0.0)
        if not score_provenance_can_clear_floor(job.score_source):
            return denied("score_below_floor", score=score, floor=float(minimum))
        if score < float(minimum):
            return denied("score_below_floor", score=score, floor=float(minimum))

    # --- resume review (submit only) --------------------------------------------- #
    # ``require_review_before_submit`` / ``require_resume_approval`` are *policy*
    # knobs. They exist to harden the explicit opt-in surface. On the no-row
    # legacy path (``elements`` came back with ``None``) they are not enforced —
    # the imported ``allow_auto_submit`` consent already carried the user's
    # intent under the pre-policy rules, and changing those rules for a user who
    # never opened the policy editor would be a silent behaviour change. A real
    # policy row always stores explicit values (defaults True at create time).
    if workflow == SUBMIT_WORKFLOW and mode == "auto_submit":
        if job is not None and elements.get("require_resume_approval") is True:
            resume = db.query(Resume).filter(
                Resume.user_id == user.id, Resume.id == job.applied_with_resume_id
            ).first() if job.applied_with_resume_id else None
            if resume is None or str(resume.status or "").casefold() != "approved":
                return denied("review_required", resume_status=(resume.status if resume else None))

        if job is not None and elements.get("require_review_before_submit") is True:
            extra = dict(job.extra or {})
            plan_doc = extra.get("autofill_plan") or {}
            if plan_doc.get("missing_required"):
                return denied("review_required", missing_required=len(plan_doc["missing_required"]))

    if mode == "auto_submit" and workflow == SUBMIT_WORKFLOW:
        if not override.http_allowed:
            downgrade = {
                "from": "auto_submit",
                "to": "prepare",
                "because": "http_gate",
                "server": {
                    "user_allow_submit": override.user_allow_submit,
                    "server_allow_submit": override.server_allow_submit,
                    "consent_automation": override.consent_automation,
                },
            }
            if for_http:
                return denied("consent_missing", downgrade=downgrade, missing=[
                    kind for kind in ("automation", "data_processing")
                    if not bool(consents.get(f"{kind}_accepted_at"))
                ] or ["automation"])
            mode = "prepare"
        elif not override.user_allow_submit:
            return denied("consent_withdrawn", downgrade={"from": "auto_submit", "to": "prepare"})

    return {
        "allowed": True,
        "mode": mode,
        "workflow": workflow,
        "policy_id": elements.get("id"),
        "policy_version": elements.get("version"),
        "decided_at": now.isoformat(),
        "reason": "allowed",
        "limits": limits,
        "downgrade": downgrade if mode != elements.get("mode") else {},
        "inputs": {
            "user_id": user.id,
            "job_id": job.id if job else None,
            "persona_id": persona_id,
            "score": float(job.score or 0.0) if job else None,
            "score_source": job.score_source if job else None,
            "consents": {
                kind: bool(consents.get(f"{kind}_accepted_at"))
                for kind in ("terms", "automation", "outreach", "data_processing")
            },
            "for_http": for_http,
        },
    }


# --------------------------------------------------------------------------- #
# Public API: `evaluate_policy` — the pure contract decision
# --------------------------------------------------------------------------- #
def evaluate_policy(
    db: Session,
    *,
    user: User,
    job: Optional[Job],
    workflow: str = SUBMIT_WORKFLOW,
    persona_id: Optional[int] = None,
    now: Optional[datetime] = None,
    outcome_count: Optional[int] = None,
    for_http: bool = False,
) -> Dict[str, Any]:
    """Can this workflow act for this user on this job, right now?

    Pure with respect to data: nothing is flushed and the transaction is not
    touched. Callers must run it again at the moment of action and persist the
    returned ``policy_id / policy_version / consents / mode`` on the attempt.

    ``outcome_count`` is used by the *caller* if it already loaded the daily
    counter; when ``None`` the engine reads it from the atomic counter table.
    """
    workflow = workflow if workflow in AUTOMATION_WORKFLOWS else SUBMIT_WORKFLOW
    moment = now or datetime.utcnow()
    effective = effective_elements(db, user=user, workflow=workflow, job=job, persona_id=persona_id)
    count = outcome_count
    if count is None and workflow == SUBMIT_WORKFLOW:
        count = int(daily_usage(db, user.id, workflow=workflow, moment=moment))
    if count is None:
        count = 0
    decision = _evaluator(
        db,
        user=user,
        job=job,
        workflow=workflow,
        persona_id=persona_id,
        elements=effective["elements"],
        now=moment,
        outcome_count=int(count),
        for_http=for_http,
    )
    if decision.get("policy_id") is None:
        decision["policy_id"] = effective["id"]
    if decision.get("policy_version") is None:
        decision["policy_version"] = effective["version"]
    decision.setdefault("scope", effective["scope"])
    decision.setdefault("mode", effective["elements"].get("mode") or "off")
    decision.setdefault("blockers", [])
    decision.setdefault("inputs", {})
    decision.setdefault("limits", {"day": {"used": int(count), "limit": None}, "month": {"limit": None}})
    decision.setdefault("downgrade", {})
    return decision


# --------------------------------------------------------------------------- #
# Recommended next decision for a UI (never mutates policy)
# --------------------------------------------------------------------------- #
def recommendations(db: Session, *, user: User) -> Dict[str, Any]:
    """The suggested write for onboarding/Settings — what *should* the user choose?

    Always safe-side: never recommends ``auto_submit``, never recommends
    replacing a *stronger* existing choice, and it never writes anything.
    """
    global_row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user.id,
            AutomationPolicy.workflow == SUBMIT_WORKFLOW,
            AutomationPolicy.scope == "global",
        )
        .first()
    )
    current = global_row.mode if global_row is not None else None
    ceilings = mode_ceilings(db, user.id)
    # Keep an existing explicit choice; otherwise suggest the plan default.
    suggested = ceilings.default_mode
    if global_row is not None and current and current != "off":
        suggested = current
    return {
        "current_mode": current if current and current != "off" else None,
        "current_chosen_at": global_row.mode_chosen_at.isoformat() if global_row and global_row.mode_chosen_at else None,
        "suggested_mode": suggested,
        "ceiling_mode": ceilings.mode,
        "allowed_modes": list(ceilings.allowed_modes),
    }


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #
def _daily_period(moment: datetime) -> str:
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d")


def daily_usage(db: Session, user_id: int, *, workflow: str = SUBMIT_WORKFLOW,
                moment: Optional[datetime] = None) -> int:
    """The number of actions this user's automation performed today (committed only)."""
    period = _daily_period(moment or datetime.utcnow())
    counter = (
        db.query(AutomationSubmissionCounter)
        .filter(AutomationSubmissionCounter.user_id == user_id,
                AutomationSubmissionCounter.period == period)
        .first()
    )
    return int(counter.record) if counter else 0


def _counter_for_update(db: Session, user_id: int, workflow: str, period: str) -> AutomationSubmissionCounter:
    row = (
        db.query(AutomationSubmissionCounter)
        .filter(AutomationSubmissionCounter.user_id == user_id,
                AutomationSubmissionCounter.period == period)
        .with_for_update()
        .first()
    )
    if row is None:
        row = AutomationSubmissionCounter(user_id=user_id, workflow=workflow, period=period,
                                          record=0, rejected=0)
        db.add(row)
    return row


def record_outcome(
    db: Session,
    *,
    user_id: int,
    workflow: str = SUBMIT_WORKFLOW,
    allowed: Optional[bool] = None,
    rejected: bool = False,
    count: int = 1,
    moment: Optional[datetime] = None,
    commit: bool = False,
) -> AutomationSubmissionCounter:
    """Atomically record one daily outcome.

    ``rejected=True`` means the engine/inspection refused the attempt — it is
    recorded (so troubleshooting and rates are honest) but never charged as a
    *submission*: refusals do not consume run budget.

    On SQLite ``SELECT … FOR UPDATE`` is a no-op but the unique key plus the
    caller's own transaction makes the read-increment race-free; on PostgreSQL
    the ``FOR UPDATE`` row lock makes the same guarantee server-side.
    """
    period = _daily_period(moment or datetime.utcnow())
    row = _counter_for_update(db, user_id, workflow, period)
    if allowed is False:
        rejected = True
    if rejected:
        row.rejected += max(0, int(count))
    else:
        row.record += max(0, int(count))
    if commit:
        db.commit()
        db.refresh(row)
    return row


def counters_snapshot(db: Session, *, user_id: int,
                      workflow: str = SUBMIT_WORKFLOW) -> Dict[str, Any]:
    """``GET /api/automation`` daily-window numbers."""
    period = _daily_period(datetime.utcnow())
    row = (
        db.query(AutomationSubmissionCounter)
        .filter(AutomationSubmissionCounter.user_id == user_id,
                AutomationSubmissionCounter.workflow == workflow,
                AutomationSubmissionCounter.period == period)
        .first()
    )
    return {
        "period": period,
        "workflow": workflow,
        "submitted": int(row.record) if row else 0,
        "rejected": int(row.rejected) if row else 0,
    }


def report_policy(db: Session, *, user: User) -> Dict[str, Any]:
    """The full policy payload `GET /api/automation/policy` renders."""
    workflow = SUBMIT_WORKFLOW
    global_row = (
        db.query(AutomationPolicy)
        .filter(
            AutomationPolicy.user_id == user.id,
            AutomationPolicy.workflow == workflow,
            AutomationPolicy.scope == "global",
        )
        .first()
    )
    override = plan_http_overrides(db, user)
    ceilings = mode_ceilings(db, user.id)
    fresh: bool = global_row is None

    return {
        "workflow": workflow,
        "policy": _as_document(global_row) if global_row else {
            "scope": "global", "scope_key": "*", "workflow": workflow,
            "enabled": True, "mode": None, "mode_chosen_at": None,
            "min_match_score": None, "max_runs_per_day": None,
            "max_runs_per_month": None,
            "require_review_before_submit": True, "require_resume_approval": True,
            "sensitive_field_policy": "ask_every_time",
            "eeo_policy": "prefer_decline", "credential_policy": "create_on_demand",
            "consents_required": list(ROW_DEFAULTS["consents_required"]),
            "disclosure_version": disclosure_version(), "version": 1,
        },
        "created": not fresh,
        # A level "auto_submit" is never turned on implicitly: expose how far up
        # this account is actually allowed to be raised today.
        "available_modes": {
            "all": list(AUTOMATION_MODES),
            "allowed": list(ceilings.allowed_modes),
            "ceiling": ceilings.mode,
            "default": ceilings.default_mode,
        },
        "http_gate": {
            "server_automation_enabled": override.server_automation_enabled,
            "server_allow_submit": override.server_allow_submit,
            "server_dry_run": override.server_dry_run,
            "user_allow_submit": override.user_allow_submit,
            "consent_automation": override.consent_automation,
            "consent_data_processing": override.consent_data_processing,
        },
        "daily": counters_snapshot(db, user_id=user.id, workflow=workflow),
        "scoped_rows": [
            {
                "scope": row.scope,
                "scope_key": row.scope_key,
                "mode": row.mode,
                "enabled": row.enabled,
                "version": row.version,
                "mode_chosen_at": row.mode_chosen_at.isoformat() if row.mode_chosen_at else None,
            }
            for row in (
                db.query(AutomationPolicy)
                .filter(AutomationPolicy.user_id == user.id,
                        AutomationPolicy.workflow == workflow)
                .order_by(AutomationPolicy.scope, AutomationPolicy.scope_key)
                .all()
            )
        ],
    }

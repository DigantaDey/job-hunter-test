"""Auto-mode (scheduled work) observability + the automation policy API.

``GET /api/automation`` remains the one place the UI reads to answer "is auto
mode running for me, how often, when does each workflow run next, what happened
last, and how much budget is left" — assembled entirely from the scheduler's
own state (:mod:`app.services.auto_scheduler`) and the ``scheduled_runs``
history, plus the new policy block (:mod:`app.services.automation_policy`).

``automation_policy`` is a separate, *explicit* surface:

* ``GET    /api/automation/policy`` — the effective ``application_submit``
  policy, the allowed-mode ceiling for this plan, the HTTP consent gate and the
  daily counters.
* ``PUT    /api/automation/policy`` — the **only** place a user can enable
  automation. Everything writes one append-only revision (actor, reason, the
  full row). The profile/settings form cannot reach this model, so auto-apply
  can never be switched on through a normal profile form.
* ``GET    /api/automation/policy/revisions`` — the versioned history.

Authz is the house pattern: the payload is built from the authenticated user's
own row (``CurrentUser``), so there is no id to forge and nothing to leak.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.logging import get_logger
from app.services import automation_policy as domain
from app.services.auto_scheduler import overview

router = APIRouter(prefix="/automation", tags=["automation"])
log = get_logger("app.automation.api")


@router.get("")
def automation(user: CurrentUser, db: DbSession):
    """This user's auto-mode state: switch, tier gate, cadence, clocks, history, quota."""
    payload: Dict[str, Any] = overview(db, int(user.id))
    payload["policy"] = domain.report_policy(db, user=user)
    return payload


class PolicyUpdate(BaseModel):
    workflow: Optional[str] = None
    scope: str = "global"
    scope_key: Optional[str] = None
    reason: Optional[str] = None
    #: The policy fields themselves. Unknown keys are ignored (the contract
    #: lists the concrete fields); the service validates the values.
    enabled: Optional[bool] = None
    mode: Optional[str] = Field(default=None, examples=["auto_submit"])
    min_match_score: Optional[float] = None
    max_runs_per_day: Optional[int] = None
    max_runs_per_month: Optional[int] = None
    require_review_before_submit: Optional[bool] = None
    require_resume_approval: Optional[bool] = None
    sensitive_field_policy: Optional[str] = None
    eeo_policy: Optional[str] = None
    credential_policy: Optional[str] = None
    allowed_portals: Optional[list[str]] = None
    blocked_portals: Optional[list[str]] = None
    allowed_companies: Optional[list[str]] = None
    blocked_companies: Optional[list[str]] = None
    blocked_keywords: Optional[list[str]] = None
    allowed_locations: Optional[list[str]] = None
    require_remote: Optional[bool] = None
    require_onsite: Optional[bool] = None
    compensation_floor: Optional[dict] = None
    schedule: Optional[dict] = None
    notify: Optional[dict] = None
    consents_required: Optional[list[str]] = None
    expires_at: Optional[str] = None


@router.get("/policy")
def get_policy(user: CurrentUser, db: DbSession):
    """The effective ``application_submit`` policy + the consent gate around it."""
    return domain.report_policy(db, user=user)


@router.put("/policy")
def put_policy(payload: PolicyUpdate, request: Request, user: CurrentUser, db: DbSession):
    """Update one automation-policy row (the explicit, source-specific opt-in).

    The only path that can raise a mode to ``auto_submit``. Violations of the
    contract's invariants — a mode above the plan ceiling, ``use_saved_answer``
    on EEO, a boolean-as-string — are refused with a 400; consent-bearing
    changes are audited and recorded as a revision as well, so *who* enabled
    auto-apply and *why* is recoverable without prose archaeology.
    """
    from datetime import datetime

    data = payload.model_dump(exclude_none=True)
    reason = str(data.pop("reason", "") or "api_update")[:200]
    workflow = data.pop("workflow", None) or domain.SUBMIT_WORKFLOW
    scope = data.pop("scope", "global")
    scope_key = data.pop("scope_key", None)

    if scope == "global":
        scope_key = "*"
    elif not (scope_key or "").strip():
        raise HTTPException(400, {"code": "scope_key_required",
                                  "message": f"scope '{scope}' needs a scope_key"})

    if "expires_at" in data and data["expires_at"] not in (None, ""):
        try:
            parsed = datetime.fromisoformat(str(data["expires_at"]))
            data["expires_at"] = parsed
        except ValueError as exc:
            raise HTTPException(400, {"code": "invalid_expires_at",
                                      "message": "expires_at must be an ISO-8601 datetime"}) from exc
    else:
        data.pop("expires_at", None)

    # Gate before any row is created: an ``auto_submit`` escalation that the
    # plan can't grant or the consent chain can't authorise is refused here,
    # typed — it never touches ``automation_policies`` (contract invariant 3).
    gate = domain.authorise_write(db, user=user, fields=data)
    if not gate["ok"]:
        code = gate.get("code", "invalid_automation_policy")
        if code == "plan_locked":
            raise HTTPException(402, {"code": "upgrade_required",
                                      "capability": "auto_submit",
                                      "plan": gate.get("plan"),
                                      "message": gate.get("message")})
        if code == "server_disabled":
            raise HTTPException(409, {"code": "server_disabled",
                                      "message": gate.get("message")})
        raise HTTPException(403, {"code": "consent_required",
                                  "consent": gate.get("missing") or ["automation"],
                                  "message": gate.get("message")})

    current_mode = (domain.resolve_policy(db, user_id=user.id, workflow=workflow)).mode

    try:
        row = domain.update_policy(
            db,
            user_id=user.id,
            workflow=workflow,
            fields=data,
            scope=scope,
            scope_key=scope_key,
            actor_type="user",
            actor_id=user.id,
            reason=reason,
            commit=True,
        )
    except ValueError as exc:
        raise HTTPException(400, {"code": "invalid_automation_policy", "message": str(exc)}) from exc

    mode = row.mode
    audit.audit(
        db,
        "automation.policy_updated",
        user=user,
        target=f"{workflow}:{scope}:{scope_key}",
        detail={
            "policy_id": row.id,
            "version": row.version,
            "workflow": workflow,
            "scope": scope,
            "scope_key": scope_key,
            "mode": mode,
            "mode_was": current_mode,
            "reason": reason,
            "changed_keys": list(data.keys()),
        },
        request=request,
    )
    # Enabling real automatic submission is the one change worth its own
    # consent-adjacent audit row (contracts/10 §7 P3 — and the only policy
    # audit action in core.audit.SENSITIVE_ACTIONS, so it survives load
    # shedding).
    if mode == "auto_submit" and current_mode != "auto_submit":
        audit.audit(
            db,
            "automation.auto_submit_enabled",
            user=user,
            target=f"{workflow}:{scope}:{scope_key}",
            detail={"policy_id": row.id, "version": row.version, "reason": reason},
            request=request,
        )
    return {
        "policy": domain._as_document(row),
        "mode_chosen": mode,
        "version": row.version,
        "http_allowed": bool(domain.plan_http_overrides(db, user).http_allowed),
    }


@router.get("/policy/revisions")
def get_revisions(
    user: CurrentUser,
    db: DbSession,
    workflow: str = Query(default=domain.SUBMIT_WORKFLOW),
    scope: str = Query(default="global"),
    scope_key: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Append-only revision history for one policy identity, newest first."""
    return {
        "revisions": domain.list_revisions(
            db, user_id=user.id, workflow=workflow, scope=scope,
            scope_key=scope_key if scope != "global" else "*", limit=limit,
        )
    }

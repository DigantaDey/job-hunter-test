"""
Audit trail.

Every action that touches user data, credentials, money-adjacent data or
outbound communication is written to ``audit_logs``. The table is append-only
from the application's point of view: nothing updates or deletes audit rows
except the GDPR eraser, which keeps the entry but detaches the owner.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger, request_id_var
from app.core.middleware import resolve_client_ip
from app.models.models import AuditLog

logger = get_logger("audit")

# Actions that must never be dropped, even under load: they are the evidence
# trail a security reviewer or a regulator will ask for.
SENSITIVE_ACTIONS = (
    "auth.", "vault.", "account.", "consent.", "resume.delete", "email.send", "application.submitted",
)


def audit(
    db: Session,
    action: str,
    *,
    user: Any = None,
    user_id: Optional[int] = None,
    request: Any = None,
    target: str = "",
    detail: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    commit: bool = True,
) -> AuditLog:
    """
    Record an auditable action.

    ``user`` may be a ``User`` instance (the common case in routers); pass
    ``user_id`` explicitly for system actions such as queue processing or account
    deletion, where the row no longer exists.
    """
    resolved_id = getattr(user, "id", None) if user is not None else user_id
    actor = getattr(user, "email", "") if user is not None else "system"

    client_ip = ip
    resolved_detail: Dict[str, Any] = dict(detail or {})
    if request is not None:
        try:
            # Same trust boundary the rate limiter uses: a forwarded address is
            # believed only from a configured proxy, so an audit row cannot be
            # attributed to an IP the actor chose in a header.
            client_ip = client_ip or (resolve_client_ip(request.scope) or None)
            agent = request.headers.get("user-agent", "")
            if agent:
                resolved_detail.setdefault("user_agent", agent[:200])
        except Exception:  # pragma: no cover - defensive, request may be a stub
            pass

    entry = AuditLog(
        user_id=resolved_id,
        actor=actor or "",
        action=action[:80],
        target=(target or "")[:200],
        detail=resolved_detail,
        ip=(client_ip or "")[:64],
        request_id=(request_id_var.get() or "")[:64],
    )
    db.add(entry)
    if commit:
        db.commit()
        db.refresh(entry)
    logger.info("audit", extra={"action": action, "user_id": resolved_id, "target": target})
    return entry


# Backwards-compatible alias for internal callers that pass keyword-only fields.
record = audit

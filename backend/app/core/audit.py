"""
Audit trail.

Every action that touches user data, credentials, money-adjacent data or
outbound communication is written to ``audit_logs``. The table is append-only
from the application's point of view: nothing updates or deletes audit rows
except the GDPR eraser, which keeps the entry but detaches the owner.

Secret sanitisation
-------------------
``sanitize_audit_detail`` walks the detail dict and masks values that match
known secret shapes (API keys, bearer tokens, passwords, MFA codes) before
the row is persisted. This is a defence-in-depth boundary: no current caller
passes credentials, but a future one cannot silently leak them either.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger, request_id_var
from app.core.middleware import resolve_client_ip
from app.core.redaction import SECRET_PATTERNS, SENSITIVE_KEYS
from app.models.models import AuditLog

logger = get_logger("audit")

# --------------------------------------------------------------------------- #
# Secret masking for audit detail — defence in depth
# --------------------------------------------------------------------------- #
#: The canonical shapes live in :mod:`app.core.redaction` and are shared with
#: the logging formatters and the stored-error path, so a new secret shape is
#: masked on every channel at once instead of on two of the three. The audit
#: policy is the strictest of the three: a value that matches *any* shape is
#: masked in full, because an audit row is append-only and nobody gets a second
#: look at it.
_AUDIT_SECRET_VALUE_PATTERNS: tuple = tuple(pattern for pattern, _ in SECRET_PATTERNS)

#: Key names whose values are always masked, regardless of content.
_SENSITIVE_KEY_NAMES = SENSITIVE_KEYS

_MASK = "***"


def _mask_value(value: str) -> str:
    """Mask any known secret shape inside a string value."""
    for pattern in _AUDIT_SECRET_VALUE_PATTERNS:
        value = pattern.sub(_MASK, value)
    return value


def sanitize_audit_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return *detail* with secret-shaped values masked.

    Walks nested dicts and lists. Key names in :data:`_SENSITIVE_KEY_NAMES` have
    their values unconditionally replaced with ``***``. String values elsewhere
    are checked against known secret patterns. Non-string, non-container values
    pass through unchanged.
    """
    if not detail:
        return {}
    return _walk(detail)


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, val in value.items():
            key_lower = str(key).lower().replace("-", "_")
            if key_lower in _SENSITIVE_KEY_NAMES:
                result[key] = _MASK
            else:
                result[key] = _walk(val)
        return result
    if isinstance(value, (list, tuple)):
        return [_walk(item) for item in value]
    if isinstance(value, str):
        return _mask_value(value)
    return value

# Actions that must never be dropped, even under load: they are the evidence
# trail a security reviewer or a regulator will ask for.
SENSITIVE_ACTIONS = (
    "auth.", "vault.", "account.", "consent.", "resume.delete", "email.send", "application.submitted",
    # Auto-apply enablement is the highest-stakes consent-adjacent write the
    # policy engine performs and must survive load shedding exactly like a
    # consent or submission event (contracts/10 §7 P3).
    "automation.auto_submit_enabled",
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
    # Defence in depth: mask any secret-shaped values in the detail dict
    # before they reach the database. No current caller passes credentials,
    # but a future one cannot silently leak them either.
    resolved_detail: Dict[str, Any] = sanitize_audit_detail(detail)
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

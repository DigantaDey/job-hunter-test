"""
Security primitives: password hashing, JWT access tokens, opaque refresh/API
tokens, and envelope encryption for secrets at rest.

Design notes
------------
* Password hashing uses ``bcrypt`` directly (pre-hashed with SHA-256 so the
  72-byte limit can never truncate a long passphrase) with a stdlib PBKDF2
  fallback, so the app never becomes unusable because of a broken C extension.
* Vault/secret encryption uses Fernet with a **per-user key** derived from the
  master key via HKDF-SHA256. A leaked database row cannot be decrypted with the
  key of another user, and rotating the master key is a single documented step.
  Derived keys are memoised in a bounded, TTL'd LRU (``KEY_CACHE_MAX_ENTRIES``
  / ``KEY_CACHE_TTL_SECONDS``) — one scope per user vault, so a plain dict there
  would grow for the whole process lifetime, and the TTL is what stops a
  rotated master key from being shadowed by a stale derivation forever.
* Tokens (refresh/API keys) are only ever stored as SHA-256 hashes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any, Dict, Optional, Tuple

import jwt as pyjwt
from jwt import PyJWTError as JWTError

from app.core.config import settings
from app.core.lru import BoundedTTLMap

# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - import guard
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover
    _bcrypt = None


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding)


def _prehash(password: str) -> bytes:
    return _b64e(hashlib.sha256(password.encode("utf-8")).digest()).encode("ascii")


def hash_password(password: str) -> str:
    """Hash a password. Raises ValueError for empty/short input."""
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if _bcrypt is not None:
        return "bcrypt-sha256$" + _bcrypt.hashpw(_prehash(password), _bcrypt.gensalt(rounds=12)).decode()
    salt = secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2-sha256${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password verification across the supported schemes."""
    if not password or not stored:
        return False
    try:
        if stored.startswith("bcrypt-sha256$"):
            if _bcrypt is None:
                return False
            return _bcrypt.checkpw(_prehash(password), stored.split("$", 1)[1].encode())
        if stored.startswith("pbkdf2-sha256$"):
            _, iterations, salt, digest = stored.split("$", 3)
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _b64d(salt), int(iterations))
            return hmac.compare_digest(candidate, _b64d(digest))
    except Exception:
        return False
    return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password or "") < settings.password_min_length:
        problems.append(f"password must be at least {settings.password_min_length} characters")
    if password and password.lower() in {"password", "1234567890", "qwertyuiop", "letmein123"}:
        problems.append("password is too common")
    return problems


# --------------------------------------------------------------------------- #
# JSON Web Tokens
# --------------------------------------------------------------------------- #
def create_access_token(user_id: int, *, email: str = "", role: str = "member", expires_minutes: Optional[int] = None) -> Tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    minutes = expires_minutes or settings.access_token_minutes
    now = int(time.time())
    exp = now + minutes * 60
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": exp,
        "iss": settings.app_name,
        "typ": "access",
    }
    token = pyjwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    if isinstance(token, bytes):  # PyJWT < 2.0 compatibility
        token = token.decode()
    return token, minutes * 60


def decode_token(token: str) -> Dict[str, Any]:
    """Decode + verify a JWT; raises ``JWTError`` when invalid/expired."""
    return pyjwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm],
                        issuer=settings.app_name)


# --------------------------------------------------------------------------- #
# Opaque tokens (refresh tokens, API keys, tracking tokens)
# --------------------------------------------------------------------------- #
def new_refresh_token() -> str:
    return f"rt_{secrets.token_urlsafe(40)}"


def new_api_key() -> str:
    return f"jh_{secrets.token_urlsafe(32)}"


def new_tracking_token() -> str:
    return secrets.token_urlsafe(16)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def api_key_prefix(api_key: str) -> str:
    return api_key[:11]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# --------------------------------------------------------------------------- #
# Envelope encryption (per-user keys)
# --------------------------------------------------------------------------- #
from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

_key_cache: BoundedTTLMap = BoundedTTLMap(
    name="security.key_cache",
    max_entries=settings.key_cache_max_entries,
    default_ttl=settings.key_cache_ttl_seconds,
)


def key_cache_stats() -> Dict[str, Any]:
    """Counters for the derived-key cache (never the keys themselves)."""
    return _key_cache.stats()


def clear_key_cache() -> None:
    """
    Drop every derived key so the next use re-derives from the current master.

    Only needed after ``VAULT_KEY`` / ``ENCRYPTION_KEY`` is rotated on a *running*
    process; the TTL in :data:`_key_cache` bounds the staleness otherwise.
    """
    _key_cache.clear()


def derive_key(material: str, purpose: str, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869) implementation over stdlib hmac."""
    prk = hmac.new(b"jobhunter-hkdf-salt", material.encode("utf-8"), hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + purpose.encode("utf-8") + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def fernet_for(scope: str) -> Fernet:
    """
    Fernet instance for a scope (``user:12`` / ``global``). Cached per scope.

    The cache is a bounded LRU with a TTL, not a plain dict: the scope embeds
    the user id, so a multi-tenant deployment would otherwise mint one entry per
    user for the whole process lifetime. Bounding it also means a rotated master
    key is picked up within ``KEY_CACHE_TTL_SECONDS`` instead of never.
    """
    key = _key_cache.get(scope)
    if key is None:
        raw = derive_key(settings.encryption_key_effective, f"jobhunter:{scope}:v1")
        key = base64.urlsafe_b64encode(raw)
        _key_cache.put(scope, key)
    return Fernet(key)


def encrypt_secret(plaintext: str, scope: str) -> str:
    return fernet_for(scope).encrypt((plaintext or "").encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str, scope: str, *, allow_legacy: bool = True) -> str:
    """Decrypt with the scoped key, falling back to the legacy global key."""
    try:
        return fernet_for(scope).decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        if allow_legacy and scope != "global":
            try:
                return fernet_for("global").decrypt(token.encode("utf-8")).decode("utf-8")
            except Exception:
                pass
        raise


def needs_reencrypt(token: str, scope: str) -> bool:
    """True when a value was encrypted with the legacy/global key."""
    try:
        fernet_for(scope).decrypt(token.encode("utf-8"))
        return False
    except Exception:
        try:
            fernet_for("global").decrypt(token.encode("utf-8"))
            return True
        except Exception:
            return False


# Back-compat wrappers (pre-2.0 call sites used a single global key) --------- #
def encrypt_password(plain: str) -> str:  # pragma: no cover - legacy shim
    return encrypt_secret(plain, "global")


def decrypt_password(token: str) -> str:  # pragma: no cover - legacy shim
    return decrypt_secret(token, "global")


__all__ = [
    "hash_password",
    "verify_password",
    "password_problems",
    "create_access_token",
    "decode_token",
    "new_refresh_token",
    "new_api_key",
    "new_tracking_token",
    "sha256_hex",
    "api_key_prefix",
    "constant_time_equals",
    "derive_key",
    "fernet_for",
    "key_cache_stats",
    "clear_key_cache",
    "encrypt_secret",
    "decrypt_secret",
    "needs_reencrypt",
    "encrypt_password",
    "decrypt_password",
    "JWTError",
]

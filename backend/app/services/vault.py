"""
Credential vault.

Every entry is encrypted with a **per-user** key derived from the master key
(HKDF-SHA256) and can only be read by its owner. Values written by older
versions (encrypted with the single global key) are transparently re-encrypted on
first read, so upgrading does not lose credentials.

Two failure modes are handled explicitly rather than quietly:

* the opportunistic re-key is **committed** (:func:`_persist_rekey`). Mutating
  ``entry.password_enc`` in memory and letting the request end discarded it, so
  the entry stayed on the legacy key forever and became unreadable the moment
  ``VAULT_KEY`` was rotated. The commit is best-effort and logged: failing to
  persist the upgrade must not fail the read.
* a ciphertext that matches no key raises :class:`VaultDecryptionError`. It used
  to be swallowed into ``""``, which made a mis-rotated key look like a vault
  full of empty passwords in the UI, in both CSV exports and in autofill.
"""
from __future__ import annotations

import csv
import io
import secrets
import string
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy.orm import Session, object_session

from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, needs_reencrypt
from app.models.models import Profile, VaultEntry

log = get_logger("app.vault")

PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


class VaultDecryptionError(RuntimeError):
    """
    An entry's ciphertext cannot be decrypted with any key this process holds.

    Raised rather than turned into ``""``: an empty string is indistinguishable
    from a credential that was never set, so the reveal endpoint, the CSV
    exports, the GDPR export and autofill would all hand back an empty password
    as though it were the real one — and the operator would never learn that
    ``VAULT_KEY`` had been rotated without re-encrypting the stored entries.
    """

    def __init__(self, entry_id: Any, domain: str = "", reason: str = "") -> None:
        self.entry_id = entry_id
        self.domain = domain or ""
        self.reason = reason or "undecryptable"
        super().__init__(
            f"vault entry {entry_id} ({self.domain or 'no domain'}) cannot be decrypted "
            f"with the configured VAULT_KEY"
        )


class VaultExport(NamedTuple):
    """A rendered credential export plus the entries that could not be read."""

    text: str
    #: Ids omitted from ``text`` because they raised :class:`VaultDecryptionError`.
    #: They are *omitted*, never written as a blank or placeholder password:
    #: importing a placeholder would overwrite the user's real credential in
    #: Chrome or the macOS keychain, which is worse than a visibly short export.
    undecryptable: Tuple[int, ...] = ()


def vault_scope(user_id: int) -> str:
    return f"user:{user_id}:vault"


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def generate_credential(domain: str, username_hint: Optional[str] = None) -> Dict[str, str]:
    """Create a credential. Uses the applicant's own email when available."""
    username = username_hint or f"applicant_{secrets.token_hex(4)}@example.invalid"
    return {"username": username, "password": generate_password(), "domain": domain}


def _encrypt(user_id: int, password: str) -> str:
    return encrypt_secret(password, vault_scope(user_id))


def _persist_rekey(entry: VaultEntry, password: str, *, db: Optional[Session] = None) -> bool:
    """
    Best-effort commit of an opportunistic re-encryption.

    ``reveal_password`` upgrades a legacy/global-key ciphertext to the per-user
    key in memory; without a commit here that mutation lived only for the
    request and was thrown away, so the entry stayed on the legacy key forever —
    and the moment ``VAULT_KEY`` was rotated it became unreadable. The commit is
    best-effort on purpose: failing to *persist* the upgrade must not fail the
    read that the user actually asked for. Either outcome is logged, so a
    persistently failing re-key is visible instead of silent.
    """
    session = db if db is not None else object_session(entry)
    entry.password_enc = _encrypt(entry.user_id, password)
    if session is None:
        log.warning(
            "vault entry %s re-keyed in memory but has no session to commit with — "
            "the legacy ciphertext stays in the database", entry.id,
        )
        return False
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001 - a failed upgrade must not lose the read
        session.rollback()
        log.error("could not persist the re-encryption of vault entry %s: %s", entry.id, exc)
        return False
    log.info("vault entry %s re-encrypted with the per-user key", entry.id)
    return True


def reveal_password(entry: VaultEntry, *, db: Optional[Session] = None) -> str:
    """
    Decrypt an entry's password, re-keying it to the per-user key on first read.

    Raises :class:`VaultDecryptionError` — and logs at ERROR — when the
    ciphertext matches neither the per-user key nor the legacy global key. It
    never returns ``""``: callers that showed an empty string could not tell a
    missing credential from an undecryptable one, so a key-rotation mistake
    surfaced to users as silently blank passwords instead of an error.
    """
    scope = vault_scope(entry.user_id)
    try:
        password = decrypt_secret(entry.password_enc, scope)
    except Exception as exc:
        log.error(
            "vault entry %s (%s) cannot be decrypted — check VAULT_KEY; the stored "
            "ciphertext matches neither the per-user nor the legacy key: %s",
            entry.id, entry.domain, exc,
        )
        raise VaultDecryptionError(entry.id, entry.domain or "") from exc
    if needs_reencrypt(entry.password_enc, scope):
        _persist_rekey(entry, password, db=db)
    return password


def save_vault_entry(db: Session, user_id: int, domain: str, username: str, password: str,
                     origin: str = "auto") -> VaultEntry:
    entry = VaultEntry(user_id=user_id, domain=domain, username=username,
                       password_enc=_encrypt(user_id, password), origin=origin)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_vault_entry_for_domain(db: Session, user_id: int, domain: str) -> Optional[VaultEntry]:
    """Look up a vault entry for *domain*.

    An exact match wins first; otherwise, a sibling host in the same known ATS
    family (``jobs.lever.co`` ↔ ``auth.lever.co``, any Workday subdomain) is
    reused — portals commonly route sign-in/sign-up to a sibling host while
    using the same account, so creating a new credential per subdomain would
    lock the user out of their own account.
    """
    if not domain:
        return None
    exact = (
        db.query(VaultEntry)
        .filter(VaultEntry.user_id == user_id, VaultEntry.domain == domain)
        .first()
    )
    if exact is not None:
        return exact
    # Sibling match: any entry whose domain is the same organisation-level
    # domain (or the same known ATS family) as the requested host.
    from app.services import net_guard
    from app.services.form_detector import hosts_in_same_ats_family
    for entry in db.query(VaultEntry).filter(VaultEntry.user_id == user_id).all():
        stored = (entry.domain or "").strip().lower()
        if not stored:
            continue
        if net_guard.host_matches(domain, stored) or net_guard.host_matches(stored, domain):
            return entry
        if hosts_in_same_ats_family(domain, stored):
            return entry
    return None


def credential_for_application(db: Session, *, user_id: int, domain: str, company: str = "") -> Tuple[Optional[Dict[str, str]], bool]:
    """
    Reuse the existing credential for a portal domain, or create a new one.

    Returns ``(credential, created)``; the password is only decrypted in memory.

    Raises :class:`VaultDecryptionError` if the stored credential can no longer
    be read — autofill cannot proceed on an empty password, and silently
    submitting one is worse than failing the job.
    """
    existing = get_vault_entry_for_domain(db, user_id, domain)
    if existing:
        existing.last_used_at = datetime.utcnow()
        db.commit()
        return {"username": existing.username,
                "password": reveal_password(existing, db=db), "domain": domain}, False

    profile = db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()
    email = ((profile.data or {}).get("email") if profile else "") or None
    credential = generate_credential(domain, username_hint=email)
    save_vault_entry(db, user_id, domain, credential["username"], credential["password"], origin="auto")
    return credential, True


def list_vault_entries(db: Session, user_id: int) -> List[VaultEntry]:
    return db.query(VaultEntry).filter(VaultEntry.user_id == user_id).order_by(VaultEntry.created_at.desc()).all()


def _entry_url(entry: VaultEntry) -> str:
    return entry.domain if entry.domain.startswith("http") else f"https://{entry.domain}"


def _reveal_for_export(entry: VaultEntry, undecryptable: List[int]) -> Optional[str]:
    """The password, or ``None`` when this entry has to be left out of the file."""
    try:
        return reveal_password(entry)
    except VaultDecryptionError:
        undecryptable.append(int(entry.id))
        return None


def export_chrome_csv(entries: List[VaultEntry]) -> VaultExport:
    """Chrome import format. Undecryptable entries are omitted and reported."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "url", "username", "password"])
    undecryptable: List[int] = []
    for entry in entries:
        password = _reveal_for_export(entry, undecryptable)
        if password is None:
            continue
        writer.writerow([entry.domain, _entry_url(entry), entry.username, password])
    return VaultExport(output.getvalue(), tuple(undecryptable))


def export_apple_csv(entries: List[VaultEntry]) -> VaultExport:
    """macOS keychain import format. Undecryptable entries are omitted."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "URL", "Username", "Password", "Notes", "OTPAuth"])
    undecryptable: List[int] = []
    for entry in entries:
        password = _reveal_for_export(entry, undecryptable)
        if password is None:
            continue
        writer.writerow([entry.domain, _entry_url(entry), entry.username, password,
                         "Generated by JobHunter AI", ""])
    return VaultExport(output.getvalue(), tuple(undecryptable))


def delete_all_vault(db: Session, user_id: int) -> int:
    """Irreversibly delete every credential for a user. Returns the count."""
    entries = db.query(VaultEntry).filter(VaultEntry.user_id == user_id).all()
    count = len(entries)
    for entry in entries:
        db.delete(entry)
    db.commit()
    return count


def delete_entry(db: Session, user_id: int, entry_id: int) -> bool:
    entry = db.query(VaultEntry).filter(VaultEntry.id == entry_id, VaultEntry.user_id == user_id).first()
    if not entry:
        return False
    db.delete(entry)
    db.commit()
    return True

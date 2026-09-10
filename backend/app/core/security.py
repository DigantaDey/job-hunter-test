from cryptography.fernet import Fernet
import hashlib
import base64
from app.core.config import settings

def _derive_key() -> bytes:
    # Derive a 32-byte url-safe key from vault_key
    digest = hashlib.sha256(settings.vault_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)

_fernet = Fernet(_derive_key())

def encrypt_password(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()

def decrypt_password(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()

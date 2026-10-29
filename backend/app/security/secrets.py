"""Reversible encryption for secrets the engine must hand back to software —
NAS shared secrets (FreeRADIUS needs them in clients.conf, the CoA client
needs them to sign requests). Subscriber passwords never come here; those
are Argon2id one-way hashes (app/security/credentials.py).
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not settings.nas_secret_key:
        raise SecretKeyMissing("NAS_SECRET_KEY is not set; cannot store or read NAS secrets")
    return Fernet(settings.nas_secret_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretKeyMissing("NAS secret could not be decrypted with the configured NAS_SECRET_KEY") from exc

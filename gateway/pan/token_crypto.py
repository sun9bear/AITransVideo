"""Fernet symmetric encryption for Baidu Pan OAuth tokens.

Plan: design 2026-05-13 §13 / impl 2026-05-14 T2.3

Key comes from AVT_PAN_TOKEN_ENCRYPTION_KEY. Loss of key = total
unrecoverable token data — user must re-authorize via OAuth Web Flow.
Backup strategy (design §13): 1Password vault primary + physical paper
copy in fireproof safe.

Per-request: cipher object is built fresh from settings each call so
tests can monkeypatch the key without module reload. Production hot
path overhead: one Fernet() per encrypt/decrypt call ≈ 10μs, negligible.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from gateway.config import settings


def _cipher() -> Fernet:
    """Build a Fernet cipher from settings.pan_token_encryption_key.

    Raises:
        RuntimeError: if key is empty (i.e. flag enabled but env unset
            — startup_checks.validate_pan_backup_config should have
            blocked this; this is the defense-in-depth runtime check).
    """
    key = settings.pan_token_encryption_key
    if not key:
        raise RuntimeError(
            "AVT_PAN_TOKEN_ENCRYPTION_KEY not set. "
            "Set it (Fernet.generate_key().decode()) or AVT_ENABLE_PAN_BACKUP=false."
        )
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a string token to opaque bytes for PG BYTEA storage.

    Returns:
        Ciphertext bytes (urlsafe base64-encoded internally by Fernet).
        Always different across calls even for same plaintext (Fernet
        randomizes IV).
    """
    return _cipher().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt PG BYTEA bytes back to plaintext string.

    Raises:
        cryptography.fernet.InvalidToken: if ciphertext was encrypted
            with a different key, or has been tampered with (Fernet
            integrity check fails).
    """
    return _cipher().decrypt(ciphertext).decode()

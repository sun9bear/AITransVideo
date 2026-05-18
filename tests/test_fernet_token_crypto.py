"""Fernet encrypt/decrypt round-trip tests for pan token storage.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 2.3
"""
import pytest
from cryptography.fernet import Fernet


def test_encrypt_decrypt_round_trip(monkeypatch):
    """Plaintext → encrypt → decrypt → identical plaintext."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)

    from gateway.pan.token_crypto import encrypt_token, decrypt_token

    plain = 'baidu_access_token_xyz_123'
    ct = encrypt_token(plain)
    assert isinstance(ct, bytes), "encrypt_token must return bytes (for PG BYTEA)"
    assert ct != plain.encode(), "ciphertext must differ from plaintext"
    assert decrypt_token(ct) == plain


def test_decrypt_with_wrong_key_raises(monkeypatch):
    """Encrypting with key A, decrypting with key B must fail loudly."""
    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()

    from gateway.pan.token_crypto import encrypt_token, decrypt_token

    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', k1)
    ct = encrypt_token('secret_token')

    # Swap to key 2; decrypt should raise
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', k2)
    with pytest.raises(Exception):  # InvalidToken or similar
        decrypt_token(ct)


def test_empty_string_round_trips(monkeypatch):
    """Edge case: empty plaintext must still encrypt + decrypt cleanly."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)

    from gateway.pan.token_crypto import encrypt_token, decrypt_token

    assert decrypt_token(encrypt_token('')) == ''


def test_long_token_round_trips(monkeypatch):
    """Baidu refresh tokens can be ~100 chars; verify length tolerance."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)

    from gateway.pan.token_crypto import encrypt_token, decrypt_token

    long_token = 'abc123_' * 20  # 140 chars
    assert decrypt_token(encrypt_token(long_token)) == long_token


def test_missing_key_raises_clear_error(monkeypatch):
    """If key is empty (flag accidentally enabled without key) — error must
    tell operator what to do."""
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', '')

    from gateway.pan.token_crypto import encrypt_token

    with pytest.raises(RuntimeError) as exc:
        encrypt_token('anything')
    msg = str(exc.value)
    assert 'AVT_PAN_TOKEN_ENCRYPTION_KEY' in msg


def test_unicode_token_round_trips(monkeypatch):
    """Edge case: tokens with non-ASCII (in case Baidu ever returns one)."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('gateway.config.settings.pan_token_encryption_key', key)

    from gateway.pan.token_crypto import encrypt_token, decrypt_token

    token = '中文_token_测试_xyz'
    assert decrypt_token(encrypt_token(token)) == token

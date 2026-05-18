"""validate_pan_backup_config tests.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 2.2
"""
from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import pytest
from cryptography.fernet import Fernet


def _make_settings(**overrides):
    """Build GatewaySettings with all pan fields set to harmless defaults
    unless overridden. Avoids env var pollution."""
    from config import GatewaySettings
    defaults = {
        'enable_pan_backup': False,
        'baidu_pan_appkey': '',
        'baidu_pan_appsecret': '',
        'baidu_pan_redirect_uri': '',
        'pan_token_encryption_key': '',
    }
    defaults.update(overrides)
    # GatewaySettings reads from env by default; we override with kwargs
    return GatewaySettings(**defaults)


def test_disabled_flag_skips_validation():
    """flag OFF → validator returns silently regardless of other fields."""
    from startup_checks import validate_pan_backup_config
    s = _make_settings(enable_pan_backup=False)
    # Even with all other fields empty, should NOT raise
    validate_pan_backup_config(s)


def test_enabled_flag_missing_appkey_raises():
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='',          # missing
        baidu_pan_appsecret='x',
        baidu_pan_redirect_uri='https://aitrans.video/admin/pan/callback',
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(RuntimeError) as exc:
        validate_pan_backup_config(s)
    assert 'AVT_BAIDU_PAN_APPKEY' in str(exc.value)


def test_enabled_flag_missing_secret_raises():
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='x',
        baidu_pan_appsecret='',        # missing
        baidu_pan_redirect_uri='https://aitrans.video/admin/pan/callback',
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(RuntimeError) as exc:
        validate_pan_backup_config(s)
    assert 'AVT_BAIDU_PAN_APPSECRET' in str(exc.value)


def test_enabled_flag_missing_redirect_uri_raises():
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='x',
        baidu_pan_appsecret='x',
        baidu_pan_redirect_uri='',     # missing
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(RuntimeError) as exc:
        validate_pan_backup_config(s)
    assert 'AVT_BAIDU_PAN_REDIRECT_URI' in str(exc.value)


def test_enabled_flag_missing_fernet_key_raises():
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='x',
        baidu_pan_appsecret='x',
        baidu_pan_redirect_uri='https://aitrans.video/admin/pan/callback',
        pan_token_encryption_key='',   # missing
    )
    with pytest.raises(RuntimeError) as exc:
        validate_pan_backup_config(s)
    assert 'AVT_PAN_TOKEN_ENCRYPTION_KEY' in str(exc.value)


def test_enabled_flag_invalid_fernet_key_raises():
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='x',
        baidu_pan_appsecret='x',
        baidu_pan_redirect_uri='https://aitrans.video/admin/pan/callback',
        pan_token_encryption_key='not_a_real_fernet_key',  # wrong format
    )
    with pytest.raises(RuntimeError) as exc:
        validate_pan_backup_config(s)
    # Error message should clearly say Fernet key invalid, with regeneration hint
    msg = str(exc.value)
    assert 'Fernet' in msg or 'invalid' in msg.lower()


def test_enabled_flag_all_valid_passes():
    """Happy path — all 4 required env present + Fernet key valid → no raise."""
    from startup_checks import validate_pan_backup_config
    s = _make_settings(
        enable_pan_backup=True,
        baidu_pan_appkey='test_appkey',
        baidu_pan_appsecret='test_secret',
        baidu_pan_redirect_uri='https://aitrans.video/admin/pan/callback',
        pan_token_encryption_key=Fernet.generate_key().decode(),
    )
    validate_pan_backup_config(s)  # no raise

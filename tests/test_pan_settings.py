"""GatewaySettings pan-backup field tests.

Plan: docs/plans/2026-05-14-admin-pan-backup-implementation-plan.md Task 2.1
"""
import importlib

import pytest
from cryptography.fernet import Fernet


def _reload_settings_module():
    """Force re-import so env var changes take effect."""
    import config as cfg
    importlib.reload(cfg)
    return cfg


def test_pan_settings_defaults(monkeypatch):
    """All pan env vars absent → safe defaults; feature OFF by default."""
    # Strip any pan env vars from current process env
    for env in (
        'AVT_ENABLE_PAN_BACKUP', 'AVT_PAN_AUTO_ARCHIVE_ENABLED',
        'AVT_PAN_AUTO_ARCHIVE_DAYS', 'AVT_PAN_AUTO_ARCHIVE_HOUR_BJT',
        'AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN', 'AVT_PAN_AUTO_ARCHIVE_DRY_RUN',
        'AVT_PAN_ORPHAN_CLEANUP_WEEKDAY', 'AVT_PAN_UPLOAD_CHUNK_BYTES',
        'AVT_PAN_TASK_STALE_HOURS',
        'AVT_BAIDU_PAN_APPKEY', 'AVT_BAIDU_PAN_APPSECRET', 'AVT_BAIDU_PAN_REDIRECT_URI',
        'AVT_PAN_TOKEN_ENCRYPTION_KEY',
    ):
        monkeypatch.delenv(env, raising=False)

    cfg = _reload_settings_module()
    s = cfg.GatewaySettings()

    # Feature flags default OFF (safety: don't activate on undeployed env)
    assert s.enable_pan_backup is False
    assert s.pan_auto_archive_enabled is False

    # Auto-archive params
    assert s.pan_auto_archive_days == 30
    assert s.pan_auto_archive_hour_bjt == 3
    assert s.pan_auto_archive_max_per_run == 5
    assert s.pan_auto_archive_dry_run is True
    assert s.pan_orphan_cleanup_weekday == 5

    # Upload + reap params
    # 2026-06-01 production fix. Default bumped 4 MB → 16 MB. Baidu PCS
    # superfile2 partseq caps at 2048; 4 MB × 2048 = 8 GB single-upload
    # ceiling that Boris Cherny (8.61 GB) + c31b (11 GB) both blew past
    # (errno 31299 "Invalid param part_id"). 16 MB × 2048 = 32 GB now.
    assert s.pan_upload_chunk_bytes == 16 * 1024 * 1024  # 16 MB
    assert s.pan_task_stale_hours == 4

    # Baidu OAuth — empty (must be set before flag enabled)
    assert s.baidu_pan_appkey == ''
    assert s.baidu_pan_appsecret == ''
    assert s.baidu_pan_redirect_uri == ''

    # Fernet key — empty
    assert s.pan_token_encryption_key == ''


def test_pan_settings_env_override(monkeypatch):
    """Setting env vars override defaults."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('AVT_ENABLE_PAN_BACKUP', 'true')
    monkeypatch.setenv('AVT_PAN_AUTO_ARCHIVE_ENABLED', 'true')
    monkeypatch.setenv('AVT_PAN_AUTO_ARCHIVE_DAYS', '60')
    monkeypatch.setenv('AVT_PAN_AUTO_ARCHIVE_MAX_PER_RUN', '10')
    monkeypatch.setenv('AVT_PAN_AUTO_ARCHIVE_DRY_RUN', 'false')
    monkeypatch.setenv('AVT_BAIDU_PAN_APPKEY', 'test_appkey_value')
    monkeypatch.setenv('AVT_BAIDU_PAN_APPSECRET', 'test_secret')
    monkeypatch.setenv('AVT_BAIDU_PAN_REDIRECT_URI', 'https://aitrans.video/admin/pan/callback')
    monkeypatch.setenv('AVT_PAN_TOKEN_ENCRYPTION_KEY', key)

    cfg = _reload_settings_module()
    s = cfg.GatewaySettings()

    assert s.enable_pan_backup is True
    assert s.pan_auto_archive_enabled is True
    assert s.pan_auto_archive_days == 60
    assert s.pan_auto_archive_max_per_run == 10
    assert s.pan_auto_archive_dry_run is False
    assert s.baidu_pan_appkey == 'test_appkey_value'
    assert s.baidu_pan_appsecret == 'test_secret'
    assert s.baidu_pan_redirect_uri == 'https://aitrans.video/admin/pan/callback'
    assert s.pan_token_encryption_key == key

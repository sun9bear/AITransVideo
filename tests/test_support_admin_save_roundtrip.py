"""Regression test for save_support_settings (2026-05-08 prod 500).

The original bug: ``save_support_settings`` called
``file_lock(str(SETTINGS_FILE) + ".lock")`` — but ``file_lock`` expects
a ``Path`` and appends its own ``.lock`` suffix via
``path.with_suffix(...)``. The string argument failed at
``AttributeError: 'str' object has no attribute 'with_suffix'`` the
moment an admin clicked Save in production.

This test exercises the round-trip end-to-end against a temp config
dir, which would have caught the bug pre-deploy.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def support_module(tmp_path, monkeypatch):
    """Reload support_admin_settings against a tmp config dir.

    The module reads ``AIVIDEOTRANS_CONFIG_DIR`` at import time to build
    ``SETTINGS_FILE``; we have to set it before the import.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(config_dir))

    # Drop the cached version so the new env var is honored.
    import sys

    for mod in list(sys.modules):
        if mod.startswith("gateway.support_admin_settings") or mod.startswith(
            "support_admin_settings"
        ):
            sys.modules.pop(mod, None)

    module = importlib.import_module("gateway.support_admin_settings")
    importlib.reload(module)
    yield module


def test_save_then_load_roundtrip(support_module, tmp_path):
    """Save a SupportAdminSettings, then load — values must come back."""
    from gateway.support_models import SupportAdminSettings

    settings = SupportAdminSettings(
        support_enabled=True,
        support_anonymous_enabled=True,
        support_ai_enabled=True,
        support_ai_model="deepseek",
        support_ai_max_output_tokens=300,
        support_ai_monthly_budget_usd=25.0,
        support_ai_input_usd_per_1m_tokens=0.14,
        support_ai_output_usd_per_1m_tokens=0.28,
        support_budget_exhausted_message="custom busy message",
        support_sensitive_keywords=["test_kw"],
        support_ops_email="ops@example.com",
    )

    # Should not raise — file_lock + atomic_write_json signatures honored.
    support_module.save_support_settings(settings)

    # File should exist on disk.
    settings_file = Path(os.environ["AIVIDEOTRANS_CONFIG_DIR"]) / "admin_settings.json"
    assert settings_file.exists(), "save_support_settings did not write the JSON"

    # Load round-trips the saved values.
    merged = support_module.load_support_settings(force_reload=True)
    assert merged["support_enabled"] is True
    assert merged["support_anonymous_enabled"] is True
    assert merged["support_ai_enabled"] is True
    assert merged["support_ai_model"] == "deepseek"
    assert merged["support_ai_monthly_budget_usd"] == 25.0
    assert merged["support_ops_email"] == "ops@example.com"
    assert merged["support_sensitive_keywords"] == ["test_kw"]


def test_save_creates_lock_file_with_correct_suffix(support_module):
    """file_lock(path) appends .lock; passing a path that already ends in
    .lock would yield .lock.lock or fail. Confirm the lock surface is
    sane after save."""
    from gateway.support_models import SupportAdminSettings

    settings = SupportAdminSettings()
    support_module.save_support_settings(settings)

    config_dir = Path(os.environ["AIVIDEOTRANS_CONFIG_DIR"])
    # The lock file is short-lived (released when context exits) so we
    # don't assert it stayed; we assert no double-suffix oddity left
    # behind.
    bad = list(config_dir.glob("*.lock.lock"))
    assert bad == [], (
        f"Found double-suffixed lock files: {bad}. file_lock was passed "
        "a path that already ended in .lock — see save_support_settings "
        "regression."
    )


def test_save_preserves_other_admin_settings_keys(support_module, tmp_path):
    """save_support_settings only writes the 'support' sub-key — other
    top-level keys in admin_settings.json must survive. (Plan §7.2.)"""
    import json

    config_file = Path(os.environ["AIVIDEOTRANS_CONFIG_DIR"]) / "admin_settings.json"
    seed = {
        "tts_provider": "minimax",
        "prompt_models": {"studio": {"pass1": "gemini_pro"}},
        "provider_api_keys": {"deepseek": "sk-xxx"},
    }
    config_file.write_text(json.dumps(seed), encoding="utf-8")

    from gateway.support_models import SupportAdminSettings

    support_module.save_support_settings(SupportAdminSettings())

    # Re-read raw JSON to verify other keys survive.
    after = json.loads(config_file.read_text(encoding="utf-8"))
    assert after.get("tts_provider") == "minimax"
    assert after.get("prompt_models") == {"studio": {"pass1": "gemini_pro"}}
    assert after.get("provider_api_keys") == {"deepseek": "sk-xxx"}
    assert "support" in after
    assert after["support"]["support_ops_email"] == "sxz999@proton.me"

"""Tests for D-6 admin UI wiring: gateway AdminSettings Pydantic model
correctly round-trips the four whisper_alignment_* fields.

Phase D-6 of 2026-05-04-subtitle-audio-sync-plan.

Two contracts to lock down:
1. Gateway's ``AdminSettings`` Pydantic model accepts the same field
   shapes that the runtime reader (``services.admin_settings``) accepts.
   These two whitelists MUST stay synchronized — Gateway can't import
   from src/services (separate Python image), so we duplicate them.
2. Save → load round-trips preserve all four fields verbatim. The
   ``save_settings`` merge logic must not drop new fields when an
   older admin_settings.json is present.

Tests run against the Gateway side (``gateway/admin_settings.py``)
which is the API surface the frontend talks to.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Gateway package is one of the directories in sys.path (legacy_cleanup
# guards already enforce a clean import graph). Add it explicitly here
# so this test file can import without relying on conftest-level setup.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO_ROOT / "gateway"
_SRC = _REPO_ROOT / "src"
for _p in (_GATEWAY, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Pydantic model defaults match runtime defaults
# ---------------------------------------------------------------------------


def test_gateway_admin_settings_default_whisper_fields():
    """Default values match the runtime reader's defaults so empty
    admin_settings.json + factory-fresh AdminSettings() produce an
    identical state. Otherwise admin would see one set of defaults in
    the UI and the runtime would behave with different ones."""
    from admin_settings import AdminSettings  # gateway side
    s = AdminSettings()
    assert s.whisper_alignment_enabled is False
    assert s.whisper_alignment_trigger == "deliverable"
    assert s.whisper_alignment_skip_cache is False
    assert s.whisper_alignment_model == "small"


def test_gateway_admin_settings_accepts_valid_trigger_values():
    """All three trigger enum values pass validation."""
    from admin_settings import AdminSettings
    for t in ("publish", "deliverable", "manual"):
        s = AdminSettings(whisper_alignment_trigger=t)
        assert s.whisper_alignment_trigger == t


def test_gateway_admin_settings_rejects_invalid_trigger_value():
    """Unknown trigger → Pydantic validator raises ValueError."""
    from pydantic import ValidationError
    from admin_settings import AdminSettings
    with pytest.raises(ValidationError):
        AdminSettings(whisper_alignment_trigger="always")


def test_gateway_admin_settings_accepts_all_whitelisted_models():
    """All five whisper model sizes pass validation."""
    from admin_settings import AdminSettings
    for m in ("tiny", "base", "small", "medium", "large-v3"):
        s = AdminSettings(whisper_alignment_model=m)
        assert s.whisper_alignment_model == m


def test_gateway_admin_settings_rejects_invalid_model_value():
    """Unknown model name (e.g. typo) → Pydantic validator raises."""
    from pydantic import ValidationError
    from admin_settings import AdminSettings
    with pytest.raises(ValidationError):
        AdminSettings(whisper_alignment_model="gpt-9000")


def test_gateway_admin_settings_lowercases_trigger_and_model():
    """Validators normalize to lowercase so admins typing 'Publish' or
    'SMALL' don't get spurious validation errors."""
    from admin_settings import AdminSettings
    s = AdminSettings(
        whisper_alignment_trigger="DELIVERABLE",
        whisper_alignment_model="SMALL",
    )
    assert s.whisper_alignment_trigger == "deliverable"
    assert s.whisper_alignment_model == "small"


# ---------------------------------------------------------------------------
# Save → load round-trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip_preserves_whisper_fields(tmp_path, monkeypatch):
    """``save_settings(s)`` then ``load_settings()`` returns identical
    values for all four whisper fields. Catches schema drift between
    the file write path and the parsed read path."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    # Re-resolve the SETTINGS_FILE constant after env change. Gateway's
    # admin_settings.py computes this at import time; for tests we
    # re-evaluate by reloading the module.
    import importlib
    import admin_settings as gw_admin_settings
    importlib.reload(gw_admin_settings)
    AdminSettings = gw_admin_settings.AdminSettings
    save_settings = gw_admin_settings.save_settings
    load_settings = gw_admin_settings.load_settings

    s = AdminSettings(
        whisper_alignment_enabled=True,
        whisper_alignment_trigger="publish",
        whisper_alignment_skip_cache=True,
        whisper_alignment_model="medium",
    )
    save_settings(s)

    loaded = load_settings()
    assert loaded.whisper_alignment_enabled is True
    assert loaded.whisper_alignment_trigger == "publish"
    assert loaded.whisper_alignment_skip_cache is True
    assert loaded.whisper_alignment_model == "medium"


def test_save_settings_merges_with_unrelated_fields(tmp_path, monkeypatch):
    """``save_settings`` must NOT clobber unrelated keys (e.g.
    ``review_prompts``, ``prompt_models``, ``provider_api_keys``). The
    file is shared with other admin endpoints; whisper-only writes must
    leave them intact."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    import importlib
    import admin_settings as gw_admin_settings
    importlib.reload(gw_admin_settings)
    AdminSettings = gw_admin_settings.AdminSettings
    save_settings = gw_admin_settings.save_settings

    # Pre-populate with unrelated keys
    settings_file = tmp_path / "admin_settings.json"
    settings_file.write_text(json.dumps({
        "review_prompts": {"pass1": "custom prompt"},
        "prompt_models": {"studio": {"pass1": "gemini_pro"}},
        "provider_api_keys": {"deepseek": "sk-xxx"},
    }), encoding="utf-8")

    s = AdminSettings(whisper_alignment_enabled=True)
    save_settings(s)

    # Re-read raw to verify the unrelated keys are still there.
    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert raw["review_prompts"] == {"pass1": "custom prompt"}
    assert raw["prompt_models"] == {"studio": {"pass1": "gemini_pro"}}
    assert raw["provider_api_keys"] == {"deepseek": "sk-xxx"}
    # And the whisper field was written
    assert raw["whisper_alignment_enabled"] is True


# ---------------------------------------------------------------------------
# Cross-module whitelist consistency check
# ---------------------------------------------------------------------------


def test_gateway_and_runtime_share_the_same_whisper_trigger_whitelist():
    """The gateway-side ``_VALID_WHISPER_TRIGGERS`` and the runtime-side
    ``_VALID_TRIGGERS`` must contain the same values. They live in
    separate modules (gateway/admin_settings.py vs
    src/services/admin_settings.py) because gateway can't import from
    src/services — but they MUST agree, otherwise admin can save a
    value through the API that the runtime won't honor."""
    import admin_settings as gw  # gateway/admin_settings.py
    from services.admin_settings import _VALID_TRIGGERS

    assert gw._VALID_WHISPER_TRIGGERS == _VALID_TRIGGERS, (
        "Whitelist drift! Gateway accepts a trigger value that runtime "
        "rejects (or vice versa). Update both sides."
    )


def test_gateway_and_runtime_share_the_same_whisper_model_whitelist():
    """Same contract for the model field."""
    import admin_settings as gw
    from services.admin_settings import _VALID_MODELS

    assert gw._VALID_WHISPER_MODELS == _VALID_MODELS, (
        "Whitelist drift! Gateway accepts a model value that runtime "
        "rejects (or vice versa). Update both sides."
    )


# ---------------------------------------------------------------------------
# Frontend default values match backend defaults
# ---------------------------------------------------------------------------


def test_frontend_default_settings_match_backend_defaults():
    """The TypeScript ``DEFAULT_SETTINGS`` literal in
    ``frontend-next/src/app/[locale]/(app)/admin/settings/page.tsx`` for the
    four whisper fields must match the Pydantic defaults. Otherwise
    the UI and the API disagree on what "default" means and admin
    sees flickering values on save.

    AST-free string check: read the TS file, look for each default
    value pair literally. Cheap regression guard, no node setup.
    """
    page = (
        _REPO_ROOT
        / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin" / "settings"
        / "page.tsx"
    )
    text = page.read_text(encoding="utf-8")

    # The four field defaults — keyed assertions against literal substrings
    # the file should contain. Each pair is (field_name, default_literal).
    expected_pairs = [
        ("whisper_alignment_enabled", "false"),
        ("whisper_alignment_trigger", "'deliverable'"),
        ("whisper_alignment_skip_cache", "false"),
        ("whisper_alignment_model", "'small'"),
    ]
    for field, default_literal in expected_pairs:
        snippet = f"{field}: {default_literal}"
        assert snippet in text, (
            f"Frontend DEFAULT_SETTINGS missing expected default for "
            f"{field}: {default_literal}. Got page.tsx without "
            f"{snippet!r} on a single line."
        )

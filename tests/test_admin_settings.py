"""Tests for ``services.admin_settings`` shared reader.

The aligner already had its own private ``_is_force_dsp_alignment_enabled``
that read ``admin_settings.json``. With the 2026-05-05 Phase D rollout
adding 4 more fields for Whisper subtitle alignment, the read logic
gets factored out into a shared module so:
- both readers pick up admin changes without restart (fresh-read
  per call, like the aligner already does)
- defensive defaults are consistent (read failure → field's default
  value, NEVER raise)
- new admin fields can be added with one-line additions
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# read_admin_setting
# ---------------------------------------------------------------------------


def test_read_admin_setting_returns_default_when_file_missing(tmp_path, monkeypatch):
    """No admin_settings.json file → return default. No exception."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    from services.admin_settings import read_admin_setting

    assert read_admin_setting("anything", default="fallback") == "fallback"
    assert read_admin_setting("anything", default=False) is False
    assert read_admin_setting("anything", default=42) == 42


def test_read_admin_setting_reads_value_from_file(tmp_path, monkeypatch):
    """Value present in file → return it (typed correctly)."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_enabled": True, "whisper_alignment_model": "small"}),
        encoding="utf-8",
    )
    from services.admin_settings import read_admin_setting

    # Re-import path is fine because read_admin_setting reads file every call.
    assert read_admin_setting("whisper_alignment_enabled", default=False) is True
    assert read_admin_setting("whisper_alignment_model", default="x") == "small"


def test_read_admin_setting_returns_default_on_unreadable_json(tmp_path, monkeypatch):
    """Corrupt JSON → return default. Don't crash the caller."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text("{ this is not json", encoding="utf-8")
    from services.admin_settings import read_admin_setting

    assert read_admin_setting("whisper_alignment_enabled", default=False) is False


def test_read_admin_setting_returns_default_when_value_missing(tmp_path, monkeypatch):
    """File exists, key absent → return default."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"force_dsp_alignment": True}),  # different field
        encoding="utf-8",
    )
    from services.admin_settings import read_admin_setting

    assert read_admin_setting("whisper_alignment_enabled", default=False) is False
    assert read_admin_setting("force_dsp_alignment", default=False) is True


def test_read_admin_setting_picks_up_changes_without_restart(tmp_path, monkeypatch):
    """Admin can change the file at runtime; readers get the new value
    on next call. Critical for "toggle without restart" UX."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    settings_file = tmp_path / "admin_settings.json"
    settings_file.write_text(
        json.dumps({"whisper_alignment_enabled": False}), encoding="utf-8",
    )
    from services.admin_settings import read_admin_setting

    assert read_admin_setting("whisper_alignment_enabled", default=False) is False

    # Admin flips the toggle
    settings_file.write_text(
        json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
    )
    assert read_admin_setting("whisper_alignment_enabled", default=False) is True


def test_read_admin_setting_handles_non_dict_root(tmp_path, monkeypatch):
    """Defensive: file root must be a dict. List / string / null → default."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    settings_file = tmp_path / "admin_settings.json"
    from services.admin_settings import read_admin_setting

    for malformed in ("[]", "null", '"a string"', "42"):
        settings_file.write_text(malformed, encoding="utf-8")
        assert read_admin_setting("whisper_alignment_enabled", default=False) is False


# ---------------------------------------------------------------------------
# WhisperAlignmentSettings convenience reader
# ---------------------------------------------------------------------------


def test_whisper_alignment_settings_returns_all_defaults_when_file_missing(
    tmp_path, monkeypatch,
):
    """No settings file → all 4 fields at safe defaults."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    assert s.enabled is False                  # default OFF (CodeX guardrail)
    assert s.trigger == "deliverable"          # default to user's preferred trigger
    assert s.skip_cache is False               # default to cache-aware
    assert s.model == "small"                  # default model


def test_whisper_alignment_settings_reads_partial_overrides(tmp_path, monkeypatch):
    """Admin can set only some fields; the rest stay at defaults."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_trigger": "publish",
        }),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    assert s.enabled is True
    assert s.trigger == "publish"
    assert s.skip_cache is False  # default kept
    assert s.model == "small"     # default kept


def test_whisper_alignment_settings_validates_trigger_enum(tmp_path, monkeypatch):
    """Unknown trigger value → fall back to default. Don't let admin
    typo bring the system into an undefined state."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_trigger": "bogus_trigger_value"}),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    # Falls back to known-good default rather than propagating bogus.
    assert s.trigger == "deliverable"


def test_whisper_alignment_settings_validates_model_enum(tmp_path, monkeypatch):
    """Unknown model name → fall back to default."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({"whisper_alignment_model": "gpt-9000"}),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    assert s.model == "small"  # bogus model name → default


# ---------------------------------------------------------------------------
# Aligner stays compatible: existing _is_force_dsp_alignment_enabled keeps
# working through the shared reader.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CodeX P2 (2026-05-05): bool() on string field values is unsafe.
# bool("false") == True in Python — any non-empty string truthy. That
# means a hand-edited admin_settings.json containing
# {"whisper_alignment_enabled": "false"} would silently flip Whisper
# ON in production. Defensive cast: only real bool values are honored;
# anything else falls back to the field's default.
# ---------------------------------------------------------------------------


def test_whisper_alignment_string_false_is_NOT_treated_as_true(tmp_path, monkeypatch):
    """A hand-edited admin_settings.json where boolean fields were typed
    as strings ('false' / 'true' / '0' / '1') must NOT be coerced via
    Python's truthy semantics. Whisper is a high-cost path; a typo on
    the boolean side cannot quietly enable it."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": "false",   # string!
            "whisper_alignment_skip_cache": "false",
        }),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    # 'false' (string) must not be honored as truthy. The safe fallback
    # is the field's default — both default to False, so the wrong-typed
    # field reads as False either way: that's exactly the contract.
    assert s.enabled is False, (
        "Hand-edited 'false' string MUST NOT enable whisper. "
        "bool('false') == True — defensive parse must reject non-bool."
    )
    assert s.skip_cache is False


def test_whisper_alignment_string_true_falls_back_to_default(tmp_path, monkeypatch):
    """The mirror case: a string 'true' is also rejected. We only
    accept real Python bool. (Asymmetric handling — accepting 'true'
    but not 'false' — would be a footgun.)"""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": "true",
            "whisper_alignment_skip_cache": "true",
        }),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    # Both fields default to False. We chose: any non-bool → default.
    # So even string 'true' produces False. Admin UI / Pydantic model
    # always writes real bool; only hand-edits hit this branch.
    assert s.enabled is False
    assert s.skip_cache is False


def test_whisper_alignment_int_zero_one_falls_back_to_default(tmp_path, monkeypatch):
    """Integers (0 / 1) are common JSON "boolean-ish" values. They must
    NOT be honored — same defensive contract."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": 1,
            "whisper_alignment_skip_cache": 0,
        }),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    # int 1 != real bool True (in our defensive contract). Falls back to
    # default False.
    assert s.enabled is False
    assert s.skip_cache is False


def test_whisper_alignment_real_bool_still_honored(tmp_path, monkeypatch):
    """Sanity: real Python bool keeps working — the validation hardening
    must not break the supported (Pydantic-written) shape."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "admin_settings.json").write_text(
        json.dumps({
            "whisper_alignment_enabled": True,
            "whisper_alignment_skip_cache": True,
        }),
        encoding="utf-8",
    )
    from services.admin_settings import read_whisper_alignment_settings

    s = read_whisper_alignment_settings()
    assert s.enabled is True
    assert s.skip_cache is True


def test_aligner_force_dsp_still_reads_admin_settings(tmp_path, monkeypatch):
    """The aligner's force_dsp toggle was the original consumer of
    admin_settings.json. After D-1 refactor it should still work the
    same way (positive AND negative cases)."""
    monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
    settings_file = tmp_path / "admin_settings.json"

    from services.alignment.aligner import _is_force_dsp_alignment_enabled

    # Off by default (no file, no field)
    assert _is_force_dsp_alignment_enabled() is False

    # Field set true
    settings_file.write_text(
        json.dumps({"force_dsp_alignment": True}), encoding="utf-8",
    )
    assert _is_force_dsp_alignment_enabled() is True

    # Field set false (explicit)
    settings_file.write_text(
        json.dumps({"force_dsp_alignment": False}), encoding="utf-8",
    )
    assert _is_force_dsp_alignment_enabled() is False

"""Phase 2a LAUNCH GATE — validate_free_consent (HARD voice-rights consent).

A payload is returned ONLY when voice_rights_confirmed is exactly True; every
other shape returns (None, reason) so the caller 403s (consent_required). Strict
types (no 1 / "true" coercion), mirroring express/smart consent.
"""
from __future__ import annotations

import sys
from pathlib import Path

_GW = str(Path(__file__).resolve().parent.parent / "gateway")
if _GW not in sys.path:
    sys.path.insert(0, _GW)

from free_consent import validate_free_consent  # noqa: E402


def test_confirmed_true_returns_payload():
    payload, reason = validate_free_consent({"voice_rights_confirmed": True})
    assert reason is None
    assert payload == {"voice_rights_confirmed": True, "client_confirmed_at": None}


def test_confirmed_with_client_timestamp_stripped():
    payload, reason = validate_free_consent(
        {"voice_rights_confirmed": True, "client_confirmed_at": " 2026-05-30T00:00:00Z "}
    )
    assert reason is None
    assert payload["client_confirmed_at"] == "2026-05-30T00:00:00Z"


def test_missing_field_not_confirmed():
    payload, reason = validate_free_consent({})
    assert payload is None and reason == "voice_rights_not_confirmed"


def test_explicit_false_not_confirmed():
    payload, reason = validate_free_consent({"voice_rights_confirmed": False})
    assert payload is None and reason == "voice_rights_not_confirmed"


def test_non_bool_confirmation_rejected():
    for bad in (1, 0, "true", "True", "1"):
        payload, reason = validate_free_consent({"voice_rights_confirmed": bad})
        assert payload is None and reason == "voice_rights_confirmed_not_bool", bad


def test_non_dict_rejected():
    for bad in (None, "x", 1, [], True):
        payload, reason = validate_free_consent(bad)
        assert payload is None and reason == "free_consent_missing_or_invalid_type", bad


def test_client_confirmed_at_non_string_rejected():
    payload, reason = validate_free_consent(
        {"voice_rights_confirmed": True, "client_confirmed_at": 123}
    )
    assert payload is None and reason == "client_confirmed_at_not_string"

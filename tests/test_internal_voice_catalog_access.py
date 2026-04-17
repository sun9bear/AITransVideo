"""Tests for /api/internal/voice-catalog access control (T4).

Guards the T4 design contract:
  1. validate_internal_api_key is a pure function in startup_checks.py.
  2. Empty or short (<16 chars) keys raise at startup.
  3. Keys of 16+ chars are accepted.

Full-stack integration coverage for ``_require_internal_access`` (header check,
loopback check, 503 vs 403 flow) would require building a FastAPI TestClient
harness with settings monkeypatching. The pure function test below is the
load-bearing guarantee — the dependency wiring is verified by code review
against voice_catalog_api.py::internal_voice_catalog.
"""
from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import pytest

from startup_checks import validate_internal_api_key


def test_startup_refuses_empty_key():
    with pytest.raises(RuntimeError, match="AVT_INTERNAL_API_KEY"):
        validate_internal_api_key("")


def test_startup_refuses_short_key():
    with pytest.raises(RuntimeError, match="AVT_INTERNAL_API_KEY"):
        validate_internal_api_key("short")


def test_startup_accepts_16_char_key():
    # No raise.
    validate_internal_api_key("a" * 16)


def test_startup_accepts_long_key():
    # No raise.
    validate_internal_api_key("a" * 32)

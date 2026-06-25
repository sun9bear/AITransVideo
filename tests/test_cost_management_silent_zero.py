"""TU-01 / H2 (EH-003): credit derivation must never silently bill 0.

``_derive_credits_from_minutes`` keeps its fail-safe ``return 0`` on error,
but a swallowed exception used to be invisible — a pricing/import regression
could under-charge every job with zero trace. These tests lock the loud-logging
behaviour while asserting the 0-return semantics are unchanged.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import cost_management  # noqa: E402


def _job(**overrides):
    base = {"job_id": "job_zero", "service_mode": "express", "metering_snapshot": {}}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_derive_credits_logs_and_returns_zero_on_error(monkeypatch, caplog):
    import credits_service

    def _boom(*_a, **_k):
        raise RuntimeError("pricing blew up")

    monkeypatch.setattr(credits_service, "estimate_credits", _boom)
    with caplog.at_level(logging.ERROR, logger="cost_management"):
        result = cost_management._derive_credits_from_minutes(_job(), 5.0)

    assert result == 0  # fail-safe semantics unchanged
    assert any("derive_credits_failed" in r.getMessage() for r in caplog.records)


def test_zero_credits_suspect_warns_when_minutes_present(monkeypatch, caplog):
    import credits_service

    monkeypatch.setattr(credits_service, "estimate_credits", lambda *_a, **_k: 0)
    with caplog.at_level(logging.ERROR, logger="cost_management"):
        result = cost_management._derive_credits_from_minutes(_job(), 5.0)

    assert result == 0
    assert any("ZERO_CREDITS_SUSPECT" in r.getMessage() for r in caplog.records)


def test_no_estimate_and_no_warn_when_minutes_zero(monkeypatch, caplog):
    import credits_service

    called = {"n": 0}

    def _track(*_a, **_k):
        called["n"] += 1
        return 7

    monkeypatch.setattr(credits_service, "estimate_credits", _track)
    with caplog.at_level(logging.ERROR, logger="cost_management"):
        result = cost_management._derive_credits_from_minutes(_job(), 0)

    assert result == 0
    assert called["n"] == 0  # early-return, estimate never called
    assert not any("ZERO_CREDITS_SUSPECT" in r.getMessage() for r in caplog.records)

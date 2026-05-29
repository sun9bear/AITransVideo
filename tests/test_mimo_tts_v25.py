"""Phase 3 (plan 2026-05-27): MiMo TTS V2.5 proactive upgrade + promotional pricing.

- DEFAULT_MIMO_MODEL is mimo-v2.5-tts (smoke-verified 2026-05-29).
- MIMO_TTS_MODEL env overrides for runtime rollback to v2-tts.
- cost catalog marks MiMo TTS promotional (limited-free), not a permanent rate.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

from cost_management import (  # noqa: E402
    DEFAULT_PRICE_CATALOG,
    _aggregate_usage_events,
    apply_costs,
)


def test_default_mimo_tts_model_is_v25(monkeypatch):
    monkeypatch.delenv("MIMO_TTS_MODEL", raising=False)
    import services.tts.mimo_tts_provider as m
    importlib.reload(m)
    try:
        assert m.DEFAULT_MIMO_MODEL == "mimo-v2.5-tts"
    finally:
        importlib.reload(m)


def test_mimo_tts_model_env_override(monkeypatch):
    # Runtime rollback knob: set env -> revert to v2-tts without a code change.
    monkeypatch.setenv("MIMO_TTS_MODEL", "mimo-v2-tts")
    import services.tts.mimo_tts_provider as m
    importlib.reload(m)
    try:
        assert m.DEFAULT_MIMO_MODEL == "mimo-v2-tts"
    finally:
        monkeypatch.delenv("MIMO_TTS_MODEL", raising=False)
        importlib.reload(m)


def test_mimo_tts_promotional_rate_status():
    # MiMo TTS events resolve to the mimo-tts catalog key; limited-free must
    # surface as rate_status="promotional" (not missing_rate, not a fake cost).
    events = [
        {"kind": "tts", "bucket": "first_tts", "provider": "mimo",
         "model": "mimo-tts", "billed_chars": 0},
    ]
    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)
    row = breakdown.tts_rows[0]
    assert row.rate_status == "promotional"
    assert row.cost_rmb == 0.0
    assert "limited_free" in row.rate_source


def test_minimax_tts_still_configured_not_promotional():
    # Guard: the promotional branch must not leak to paid providers.
    events = [
        {"kind": "tts", "bucket": "first_tts", "provider": "minimax",
         "model": "speech-2.8-hd", "billed_chars": 10_000},
    ]
    breakdown = _aggregate_usage_events(events)
    apply_costs(breakdown, DEFAULT_PRICE_CATALOG)
    row = breakdown.tts_rows[0]
    assert row.rate_status == "configured"
    assert row.cost_rmb == 3.5

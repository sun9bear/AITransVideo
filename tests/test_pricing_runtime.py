from __future__ import annotations

import json

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))

import pricing_runtime
from pricing_schema import PricingPayload, build_default_pricing_payload


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point PRICING_RUNTIME_FILE to tmp_path and clear cache between tests."""
    runtime_file = tmp_path / "pricing_runtime.json"
    monkeypatch.setattr(pricing_runtime, "PRICING_RUNTIME_FILE", runtime_file)
    pricing_runtime.invalidate_runtime_pricing_cache()
    yield
    pricing_runtime.invalidate_runtime_pricing_cache()


def test_runtime_uses_snapshot_file_when_present(tmp_path):
    """Write a snapshot with modified value, reload, verify the modified value is read."""
    payload = build_default_pricing_payload()
    # Modify a value to distinguish from defaults
    payload.trial.days = 99

    pricing_runtime.write_runtime_snapshot(payload)

    # Force reload from file
    result = pricing_runtime.get_runtime_pricing(force_reload=True)
    assert result.trial.days == 99


def test_runtime_falls_back_to_defaults_when_snapshot_missing():
    """When the snapshot file does not exist, defaults are returned."""
    result = pricing_runtime.get_runtime_pricing(force_reload=True)
    defaults = build_default_pricing_payload()
    assert result.trial.days == defaults.trial.days
    assert result.version == defaults.version
    assert set(result.plans.keys()) == set(defaults.plans.keys())


def test_runtime_caches_and_invalidate_works(tmp_path):
    """Read twice (cached), invalidate, read again picks up new file."""
    # First read — no file, gets defaults
    r1 = pricing_runtime.get_runtime_pricing()
    defaults = build_default_pricing_payload()
    assert r1.trial.days == defaults.trial.days

    # Second read — same object (cached)
    r2 = pricing_runtime.get_runtime_pricing()
    assert r2 is r1

    # Now write a different snapshot
    payload = build_default_pricing_payload()
    payload.trial.days = 42
    pricing_runtime.write_runtime_snapshot(payload)

    # Without invalidation, cache still returns old value
    r3 = pricing_runtime.get_runtime_pricing()
    # write_runtime_snapshot updates the cache, so r3 should reflect the new value
    assert r3.trial.days == 42

    # Invalidate and verify re-read from file
    pricing_runtime.invalidate_runtime_pricing_cache()
    r4 = pricing_runtime.get_runtime_pricing()
    assert r4.trial.days == 42
    assert r4 is not r1


def test_runtime_falls_back_on_corrupt_file(tmp_path):
    """Write garbage to file, verify defaults are returned."""
    runtime_file = pricing_runtime.PRICING_RUNTIME_FILE
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text("NOT VALID JSON {{{", encoding="utf-8")

    result = pricing_runtime.get_runtime_pricing(force_reload=True)
    defaults = build_default_pricing_payload()
    assert result.trial.days == defaults.trial.days
    assert set(result.plans.keys()) == set(defaults.plans.keys())


def test_write_snapshot_creates_parent_dirs(tmp_path):
    """Point to a nested nonexistent dir, write succeeds."""
    import pricing_runtime as pr
    nested = tmp_path / "a" / "b" / "c" / "pricing_runtime.json"
    pr.PRICING_RUNTIME_FILE = nested

    payload = build_default_pricing_payload()
    pricing_runtime.write_runtime_snapshot(payload)

    assert nested.exists()
    data = json.loads(nested.read_text(encoding="utf-8"))
    assert data["version"] == 1

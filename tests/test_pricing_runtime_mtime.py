"""P1-11d (audit 2026-05-07) regression: pricing_runtime cache must
re-read on mtime change to support cross-process invalidation.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-HIGH-6 — pricing_runtime _cache was process-local; admin
                   publishing in process A left B/C serving stale
                   prices until restart.

Note: the actual gateway module exposes ``get_runtime_pricing()`` (not
``load_runtime_pricing()``) and ``PRICING_RUNTIME_FILE`` (no leading
underscore). This file targets the real symbols.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


@pytest.fixture
def isolated_pricing_runtime(tmp_path, monkeypatch):
    """Point pricing_runtime at a tmp file we control so we can mutate
    it freely. Clears the in-process cache before and after each test."""
    runtime_file = tmp_path / "pricing_runtime.json"

    import pricing_runtime
    from pricing_schema import build_default_pricing_payload

    # Seed with a valid initial payload so the first read hits the file
    # (not the fallback defaults code path).
    initial_payload = build_default_pricing_payload()
    initial_payload.trial.days = 7
    runtime_file.write_text(
        json.dumps(initial_payload.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    monkeypatch.setattr(pricing_runtime, "PRICING_RUNTIME_FILE", runtime_file)
    pricing_runtime.invalidate_runtime_pricing_cache()
    try:
        yield pricing_runtime, runtime_file
    finally:
        pricing_runtime.invalidate_runtime_pricing_cache()


def test_pricing_runtime_returns_same_object_on_repeat_when_mtime_unchanged(
    isolated_pricing_runtime,
):
    pr, _ = isolated_pricing_runtime
    a = pr.get_runtime_pricing()
    b = pr.get_runtime_pricing()
    # Should be the SAME cached object (identity), proves cache is
    # active when mtime is unchanged.
    assert a is b, (
        "P1-11d regression: cache is not active when mtime is unchanged "
        "— every read is doing JSON parse, which negates the cache"
    )


def test_pricing_runtime_reloads_after_mtime_change(isolated_pricing_runtime):
    pr, runtime_file = isolated_pricing_runtime
    from pricing_schema import build_default_pricing_payload

    first = pr.get_runtime_pricing()
    assert first.trial.days == 7

    # Sleep enough to ensure mtime granularity bumps. Most filesystems
    # have at least nanosecond mtime; sleeping 10ms is overkill but
    # guarantees a different value.
    time.sleep(0.01)

    # Simulate process B publishing a new pricing version by writing
    # the snapshot file directly (bypassing write_runtime_snapshot,
    # which would refresh THIS process's cache).
    new_payload = build_default_pricing_payload()
    new_payload.trial.days = 42
    runtime_file.write_text(
        json.dumps(new_payload.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Force mtime change explicitly in case the FS doesn't tick over
    # in 10ms (some Windows / FAT filesystems).
    new_mtime = os.stat(runtime_file).st_mtime_ns + 1_000_000
    os.utime(runtime_file, ns=(new_mtime, new_mtime))

    second = pr.get_runtime_pricing()
    assert second.trial.days == 42, (
        "P1-11d regression: pricing_runtime did not re-read after "
        "the file mtime changed; cross-process publish is invisible "
        "to readers until process restart"
    )
    # And it must be a freshly-parsed object, not the stale cache.
    assert second is not first


def test_pricing_runtime_invalidate_still_works(isolated_pricing_runtime):
    """The explicit invalidate function is still callable and forces
    a fresh read on the next get_runtime_pricing() call."""
    pr, _ = isolated_pricing_runtime
    a = pr.get_runtime_pricing()
    pr.invalidate_runtime_pricing_cache()
    b = pr.get_runtime_pricing()
    # After invalidate, the cache rebuilds — should be a freshly-parsed
    # object (different identity).
    assert a is not b, (
        "P1-11d regression: invalidate_runtime_pricing_cache no longer "
        "forces a re-read on next load"
    )

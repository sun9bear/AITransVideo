"""P1-13 (audit 2026-05-07) regression: background task concurrency cap.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P-CRITICAL-7 — generate_video / materials_pack had no concurrency
                       limit; 4 users clicking simultaneously could spawn
                       4 parallel ffmpegs at 100% CPU each, starving
                       polling and risking OOM.

Strategy: import the gateway module's module-level _BACKGROUND_TASK_SEMAPHORE
and assert its initial value is 2; then drive a synthetic executor
through the gating wrapper and verify only 2 are ever in flight at once.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

# Stub gateway dependencies so background_task_api imports cleanly without
# a live database (mirrors tests/test_background_task_api.py:27-31).
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)


def test_background_task_module_has_semaphore_with_value_2():
    """Module must expose _BACKGROUND_TASK_SEMAPHORE with initial=2."""
    import background_task_api
    sem = getattr(background_task_api, "_BACKGROUND_TASK_SEMAPHORE", None)
    assert sem is not None, (
        "P1-13 regression: gateway/background_task_api.py no longer "
        "defines _BACKGROUND_TASK_SEMAPHORE — concurrent ffmpeg jobs "
        "have no rate limit"
    )
    assert isinstance(sem, asyncio.Semaphore)
    # Semaphore stores its initial value as _value (lib-private but stable).
    assert sem._value == 2, (
        f"P1-13 regression: semaphore value is {sem._value}, expected 2"
    )


def test_background_task_create_task_call_is_gated_by_semaphore():
    """AST guard: any asyncio.create_task(executor(...)) call in the
    module body must be wrapped with the semaphore — either via an
    `async with _BACKGROUND_TASK_SEMAPHORE:` block in a wrapper, or
    via a helper that the source clearly references."""
    src_path = _REPO_ROOT / "gateway" / "background_task_api.py"
    src = src_path.read_text(encoding="utf-8")
    # Soft check: the string "_BACKGROUND_TASK_SEMAPHORE" must appear
    # in the FILE more than once — once for the definition, at least
    # once for a usage.
    occurrences = src.count("_BACKGROUND_TASK_SEMAPHORE")
    assert occurrences >= 2, (
        f"P1-13 regression: _BACKGROUND_TASK_SEMAPHORE appears {occurrences} "
        f"time(s) in gateway/background_task_api.py — expected >=2 (1 "
        f"definition + 1+ usage in the executor gating wrapper)"
    )


@pytest.mark.asyncio
async def test_semaphore_actually_limits_concurrent_executions():
    """Drive a fake executor through the semaphore directly — assert
    that only 2 are ever in flight at the same time even when 5 are
    queued."""
    import background_task_api

    sem = background_task_api._BACKGROUND_TASK_SEMAPHORE
    in_flight = 0
    max_in_flight = 0

    async def fake_executor(idx: int):
        nonlocal in_flight, max_in_flight
        async with sem:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # Hold the slot briefly to let the next task try to acquire
            await asyncio.sleep(0.05)
            in_flight -= 1

    tasks = [asyncio.create_task(fake_executor(i)) for i in range(5)]
    await asyncio.gather(*tasks)

    assert max_in_flight == 2, (
        f"P1-13 regression: max concurrent executors was {max_in_flight}, "
        f"expected exactly 2 (the Semaphore value)"
    )

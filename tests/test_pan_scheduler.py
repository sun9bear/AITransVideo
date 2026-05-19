"""Tests for gateway/pan/scheduler.py (Phase 8 §T8.4)."""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Bootstrap gateway/ on sys.path (mirrors other pan tests).
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


# Stub database before importing pan.scheduler — production code lazy-
# imports inside each loop, so module import itself works regardless,
# but tests that instantiate the loops need a stub.
_fake_db = types.ModuleType("database")
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
_fake_db.get_db = MagicMock()
sys.modules.setdefault("database", _fake_db)


# =========================================================================
# Time-helper unit tests
# =========================================================================


def test_seconds_until_next_daily_today_future():
    """When the daily target HH:MM is later today, returns seconds to it."""
    from pan.scheduler import _seconds_until_next_daily

    # Pick the hour 12h ahead — guaranteed future today regardless of
    # when this test runs (within reason).
    now = datetime.now(timezone.utc)
    target_hour = (now.hour + 12) % 24
    # If target hour is "tomorrow" relative to now (wraps), it's still
    # the next occurrence in <24h.
    seconds = _seconds_until_next_daily(target_hour, 0)
    assert 60.0 <= seconds <= 24 * 3600


def test_seconds_until_next_daily_already_passed_today():
    """When target HH:MM already passed today, returns next-day delta."""
    from pan.scheduler import _seconds_until_next_daily

    # Pick the hour 1h BEFORE now — definitely already passed.
    now = datetime.now(timezone.utc)
    target_hour = (now.hour - 1) % 24
    seconds = _seconds_until_next_daily(target_hour, 0)
    # Should be between ~23h and 24h — tomorrow.
    assert 22 * 3600 < seconds <= 24 * 3600 + 60


def test_seconds_until_next_daily_minimum_60s():
    """Even if target is "now exactly", floor is 60s to avoid hot loops."""
    from pan.scheduler import _seconds_until_next_daily

    # We can't easily hit "now exactly" in a deterministic test, but
    # we can verify the lower bound on a typical call.
    s = _seconds_until_next_daily(0, 0)
    assert s >= 60


def test_seconds_until_next_weekly_picks_correct_dow():
    """The weekly helper aligns to a specific weekday."""
    from pan.scheduler import _seconds_until_next_weekly

    # Compute when the next Friday 20:00 UTC is.
    seconds = _seconds_until_next_weekly(dow=4, hour=20, minute=0)
    fire_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    assert fire_at.weekday() == 4  # Friday
    assert fire_at.hour == 20
    assert fire_at.minute == 0


# =========================================================================
# register_pan_schedulers — task creation + app.state stashing
# =========================================================================


def test_register_pan_schedulers_creates_all_four_tasks():
    """All 4 scheduler loops register as asyncio tasks + are stashed on
    app.state under named attributes."""
    from pan.scheduler import register_pan_schedulers

    class FakeApp:
        state = types.SimpleNamespace()

    async def _go():
        app = FakeApp()
        register_pan_schedulers(app)
        try:
            # All 4 attrs present.
            assert hasattr(app.state, 'pan_archive_scanner_task')
            assert hasattr(app.state, 'pan_token_refresh_task')
            assert hasattr(app.state, 'pan_orphan_cleanup_task')
            assert hasattr(app.state, 'pan_stale_reaper_task')

            # All 4 are asyncio.Task instances.
            for attr in (
                'pan_archive_scanner_task', 'pan_token_refresh_task',
                'pan_orphan_cleanup_task', 'pan_stale_reaper_task',
            ):
                task = getattr(app.state, attr)
                assert isinstance(task, asyncio.Task)
                assert not task.done()  # still pending (sleeping)
        finally:
            # Cancel cleanly so the event loop can shut down.
            for attr in (
                'pan_archive_scanner_task', 'pan_token_refresh_task',
                'pan_orphan_cleanup_task', 'pan_stale_reaper_task',
            ):
                task = getattr(app.state, attr, None)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


def test_register_pan_schedulers_failure_does_not_raise():
    """If asyncio.create_task somehow fails for one loop, the others
    still register and register_pan_schedulers does NOT raise (consistent
    with existing r2_sweeper fail-safe pattern in main.py)."""
    from pan import scheduler as sched_mod

    class FakeApp:
        state = types.SimpleNamespace()

    # Patch create_task to raise on the FIRST loop, succeed on the rest.
    real_create_task = asyncio.create_task
    call_count = [0]

    def selective_create_task(coro, *, name=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # Close the unrun coroutine to suppress RuntimeWarning.
            coro.close()
            raise RuntimeError('synthetic task create failure')
        return real_create_task(coro, name=name)

    async def _go():
        app = FakeApp()
        with pytest.MonkeyPatch().context() as m:
            m.setattr(asyncio, 'create_task', selective_create_task)
            # MUST NOT raise.
            sched_mod.register_pan_schedulers(app)

        # 3 loops registered (the 2nd, 3rd, 4th); the 1st failed silently.
        registered = [
            attr for attr in (
                'pan_archive_scanner_task', 'pan_token_refresh_task',
                'pan_orphan_cleanup_task', 'pan_stale_reaper_task',
            ) if hasattr(app.state, attr)
        ]
        assert len(registered) == 3
        # Cancel survivors.
        for attr in registered:
            task = getattr(app.state, attr)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# =========================================================================
# Wire-up regression guard: main.py imports + calls register_pan_schedulers
# =========================================================================


def test_pan_scheduler_registered_in_main():
    """gateway/main.py MUST `from pan.scheduler import register_pan_schedulers`
    AND call `register_pan_schedulers(app)`. Without both, the 4 loops
    never start in production and pan auto-archive / refresh / reap /
    orphan-cleanup are silently off."""
    import ast

    main_py = (
        Path(__file__).resolve().parent.parent / 'gateway' / 'main.py'
    )
    text = main_py.read_text(encoding='utf-8')
    tree = ast.parse(text)

    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'pan.scheduler':
            for alias in node.names:
                if alias.name == 'register_pan_schedulers':
                    imported = True
                    break
    assert imported, (
        "gateway/main.py must `from pan.scheduler import register_pan_schedulers`"
    )

    called = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'register_pan_schedulers'
        ):
            called = True
            break
    assert called, (
        "gateway/main.py must call register_pan_schedulers(app) "
        "in the lifespan startup hook"
    )

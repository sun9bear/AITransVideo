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
# CodeX P2 (2026-05-19): _bjt_weekly_to_utc — BJT (wd, HH:MM) → UTC tuple
# =========================================================================


def test_bjt_weekly_to_utc_saturday_0400_bjt_is_friday_2000_utc():
    """Plan §10 specifies orphan cleanup at Sat 04:00 BJT. The UTC
    equivalent is Fri 20:00 UTC — weekday shifts AND hour shifts.
    A naive ``_bjt_hour_to_utc(4)=20`` combined with weekday=5 (Sat)
    would schedule Sat 20:00 UTC = Sun 04:00 BJT (one day late)."""
    from pan.scheduler import _bjt_weekly_to_utc

    wd, hr, mn = _bjt_weekly_to_utc(weekday_bjt=5, hour_bjt=4, minute_bjt=0)
    assert (wd, hr, mn) == (4, 20, 0)  # Friday 20:00 UTC


def test_bjt_weekly_to_utc_midday_bjt_same_day():
    """BJT noon Tuesday (wd=1, 12:00) → UTC Tuesday 04:00 — same day."""
    from pan.scheduler import _bjt_weekly_to_utc

    wd, hr, mn = _bjt_weekly_to_utc(weekday_bjt=1, hour_bjt=12, minute_bjt=0)
    assert (wd, hr, mn) == (1, 4, 0)


def test_bjt_weekly_to_utc_monday_0000_bjt_is_sunday_1600_utc():
    """BJT Monday midnight (wd=0, 00:00) → UTC Sunday 16:00."""
    from pan.scheduler import _bjt_weekly_to_utc

    wd, hr, mn = _bjt_weekly_to_utc(weekday_bjt=0, hour_bjt=0, minute_bjt=0)
    assert (wd, hr, mn) == (6, 16, 0)  # Sunday 16:00 UTC


def test_bjt_weekly_to_utc_minute_preserved():
    """Minute offset passes through (no DST in BJT, so no wrap)."""
    from pan.scheduler import _bjt_weekly_to_utc

    wd, hr, mn = _bjt_weekly_to_utc(weekday_bjt=3, hour_bjt=10, minute_bjt=30)
    assert (wd, hr, mn) == (3, 2, 30)


def test_orphan_cleanup_loop_initial_sleep_lands_on_saturday_0400_bjt(
    monkeypatch,
):
    """End-to-end: with pan_orphan_cleanup_weekday=5 (Sat BJT) the
    initial-sleep computation must target Fri 20:00 UTC (= Sat 04:00 BJT).
    Regression test for CodeX P2 2026-05-19."""
    from pan import scheduler as sched_mod

    captured_sleeps: list[float] = []

    async def capture_then_cancel(sec):
        captured_sleeps.append(sec)
        # Bail immediately so the test stays fast.
        raise asyncio.CancelledError

    monkeypatch.setattr(sched_mod.asyncio, 'sleep', capture_then_cancel)
    from config import settings
    monkeypatch.setattr(settings, 'enable_pan_backup', True, raising=False)
    monkeypatch.setattr(
        settings, 'pan_orphan_cleanup_weekday', 5, raising=False,
    )

    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await sched_mod._orphan_cleanup_loop()
        assert len(captured_sleeps) == 1
        # The sleep must land on Fri 20:00 UTC. Verify by adding to "now".
        fire_at = datetime.now(timezone.utc) + timedelta(
            seconds=captured_sleeps[0],
        )
        assert fire_at.weekday() == 4, (
            f"expected Friday (weekday=4) but got weekday={fire_at.weekday()}"
        )
        assert fire_at.hour == 20, (
            f"expected 20:00 UTC but got {fire_at.hour}:{fire_at.minute}"
        )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


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


# =========================================================================
# CodeX P1-4: scheduler honors enable_pan_backup / auto_archive / dry_run
# =========================================================================


def test_archive_scanner_loop_skips_when_enable_pan_backup_off(monkeypatch):
    """If settings.enable_pan_backup is False, scanner tick must NOT run.
    Production default IS False — scheduler shouldn't auto-archive."""
    from pan import scheduler as sched_mod

    # Track whether run_archive_scanner_tick is called.
    tick_calls: list = []

    async def fake_tick(db, **kwargs):
        tick_calls.append(kwargs)
        return {'candidates': [], 'enqueued': 0, 'enqueued_task_ids': [],
                'failed_enqueue': [], 'dry_run': True}

    # Make sleeps fast so the loop iterates once + we can cancel.
    sleep_count = [0]

    async def quick_sleep(s):
        sleep_count[0] += 1
        if sleep_count[0] > 2:  # let the loop body run once
            raise asyncio.CancelledError

    monkeypatch.setattr(sched_mod.asyncio, 'sleep', quick_sleep)
    monkeypatch.setattr(
        'pan.archive_scanner.run_archive_scanner_tick', fake_tick,
    )

    # Disable enable_pan_backup.
    from config import settings
    monkeypatch.setattr(settings, 'enable_pan_backup', False, raising=False)
    monkeypatch.setattr(
        settings, 'pan_auto_archive_enabled', True, raising=False,
    )

    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await sched_mod._archive_scanner_loop()
        # The tick was NEVER called because the flag was off.
        assert tick_calls == []

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


def test_archive_scanner_loop_passes_settings_to_tick(monkeypatch):
    """When both flags ON, the loop forwards settings as
    age_days / max_per_run / dry_run keyword args to the tick."""
    from pan import scheduler as sched_mod

    captured: list[dict] = []

    async def fake_tick(db, **kwargs):
        captured.append(kwargs)
        return {'candidates': [], 'enqueued': 0, 'enqueued_task_ids': [],
                'failed_enqueue': [], 'dry_run': kwargs.get('dry_run')}

    sleep_count = [0]

    async def quick_sleep(s):
        sleep_count[0] += 1
        if sleep_count[0] > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sched_mod.asyncio, 'sleep', quick_sleep)
    monkeypatch.setattr(
        'pan.archive_scanner.run_archive_scanner_tick', fake_tick,
    )

    # Stub the async_session contextmanager so the loop body doesn't
    # need a real DB.
    class FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(
        'database.async_session', lambda: FakeSession(),
    )

    from config import settings
    monkeypatch.setattr(settings, 'enable_pan_backup', True, raising=False)
    monkeypatch.setattr(
        settings, 'pan_auto_archive_enabled', True, raising=False,
    )
    monkeypatch.setattr(settings, 'pan_auto_archive_days', 45, raising=False)
    monkeypatch.setattr(
        settings, 'pan_auto_archive_max_per_run', 7, raising=False,
    )
    monkeypatch.setattr(settings, 'pan_auto_archive_dry_run', True, raising=False)

    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await sched_mod._archive_scanner_loop()
        # Settings forwarded.
        assert len(captured) >= 1
        kw = captured[0]
        assert kw['age_days'] == 45
        assert kw['max_per_run'] == 7
        assert kw['dry_run'] is True

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


def test_stale_reaper_loop_passes_stale_hours(monkeypatch):
    """Loop forwards settings.pan_task_stale_hours to the tick."""
    from pan import scheduler as sched_mod

    captured: list[dict] = []

    async def fake_tick(engine, **kwargs):
        captured.append(kwargs)
        return {
            'in_flight_reaped': 0, 'in_flight_skipped_locked': 0,
            'post_commit_forwarded': 0, 'post_commit_skipped_locked': 0,
            'residue_cleanup_enqueued': 0, 'dry_run': False,
        }

    sleep_count = [0]

    async def quick_sleep(s):
        sleep_count[0] += 1
        if sleep_count[0] > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(sched_mod.asyncio, 'sleep', quick_sleep)
    monkeypatch.setattr('pan.stale_reaper.run_stale_reaper_tick', fake_tick)
    monkeypatch.setattr('database.engine', object())

    from config import settings
    monkeypatch.setattr(settings, 'enable_pan_backup', True, raising=False)
    monkeypatch.setattr(settings, 'pan_task_stale_hours', 8, raising=False)

    async def _go():
        with pytest.raises(asyncio.CancelledError):
            await sched_mod._stale_reaper_loop()
        assert captured[0]['stale_hours'] == 8

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# =========================================================================
# CodeX P2-5: shutdown cancellation in main.py
# =========================================================================


def test_main_shutdown_cancels_all_four_pan_scheduler_tasks():
    """main.py lifespan shutdown MUST cancel + await the 4 pan task
    attrs (pan_archive_scanner_task, pan_token_refresh_task,
    pan_orphan_cleanup_task, pan_stale_reaper_task). Without this,
    sleeping tasks race with engine.dispose() on shutdown."""
    import ast
    from pathlib import Path

    main_py = (
        Path(__file__).resolve().parent.parent / 'gateway' / 'main.py'
    )
    text = main_py.read_text(encoding='utf-8')

    required_attrs = {
        'pan_archive_scanner_task',
        'pan_token_refresh_task',
        'pan_orphan_cleanup_task',
        'pan_stale_reaper_task',
    }
    # Find the shutdown cancellation list — look for the for-loop that
    # iterates task attrs and calls handle.cancel().
    tree = ast.parse(text)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            # The iter is a tuple/list of string constants.
            iter_node = node.iter
            if isinstance(iter_node, (ast.Tuple, ast.List)):
                for elt in iter_node.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value in required_attrs:
                            found.add(elt.value)
    missing = required_attrs - found
    assert not missing, (
        f"main.py shutdown loop must include all 4 pan scheduler task "
        f"attrs; missing: {missing}"
    )


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

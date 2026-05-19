"""Pan background scheduler loops (Phase 8 §T8.4).

Plan 2026-05-13 §10. Four asyncio loops registered at gateway startup:

  archive_scanner_loop   — daily 03:30 BJT (= 19:30 UTC)
  token_refresh_loop     — every 6h
  orphan_cleanup_loop    — Saturday 04:00 BJT (= 20:00 UTC Friday)
  stale_reaper_loop      — every 30 min

Each loop:
  - Catches Exception around the tick body (transient DB hiccup must
    NOT kill the loop for the rest of gateway's life).
  - Sleeps an initial offset to spread startup load (matches the
    existing project_cleanup convention).
  - Logs at INFO when work happens, WARNING on failure.

Loops are launched from gateway/main.py's lifespan startup hook via
`register_pan_schedulers(app)`. Each task is stashed on
`app.state.pan_*_task` so the lifespan shutdown can cancel cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


# Tunables (override via env in production).
ARCHIVE_SCANNER_HOUR_UTC = 19
ARCHIVE_SCANNER_MINUTE_UTC = 30
TOKEN_REFRESH_INTERVAL_S = 6 * 3600
ORPHAN_CLEANUP_DOW_UTC = 4  # Friday in UTC = Saturday morning BJT
ORPHAN_CLEANUP_HOUR_UTC = 20
ORPHAN_CLEANUP_MINUTE_UTC = 0
STALE_REAPER_INTERVAL_S = 30 * 60


def _seconds_until_next_daily(hour: int, minute: int) -> float:
    """Seconds from now until the next occurrence of HH:MM UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


def _seconds_until_next_weekly(dow: int, hour: int, minute: int) -> float:
    """Seconds until the next dow-th day of the week at HH:MM UTC.
    dow: 0=Monday ... 6=Sunday (datetime.weekday convention)."""
    now = datetime.now(timezone.utc)
    days_ahead = (dow - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if target <= now:
        target = target + timedelta(days=7)
    return max(60.0, (target - now).total_seconds())


# BJT → UTC offset. Beijing is UTC+8, so HH BJT = (HH-8) UTC mod 24.
_BJT_OFFSET_HOURS = 8


def _bjt_hour_to_utc(hour_bjt: int) -> int:
    return (hour_bjt - _BJT_OFFSET_HOURS) % 24


def _bjt_weekly_to_utc(
    weekday_bjt: int, hour_bjt: int, minute_bjt: int,
) -> tuple[int, int, int]:
    """Convert (weekday, HH:MM) in Beijing to (weekday, HH:MM) in UTC.

    CodeX P2 (2026-05-19): the orphan cleanup cron is specified as
    Saturday 04:00 BJT (plan §10). Naively passing
    ``(pan_orphan_cleanup_weekday=5, hour_utc=20)`` to a UTC weekly
    helper schedules UTC Sat 20:00, which is BJT Sun 04:00 — one day
    late.

    The hour shift (-8h) often crosses midnight in UTC, which means
    the weekday also shifts. We compute the conversion holistically:

        bjt_minute_of_week  = wd_bjt * 1440 + hr_bjt * 60 + min_bjt
        utc_minute_of_week  = (bjt_minute_of_week - 8 * 60) mod 10080
        wd_utc, hr_utc, min_utc = unpack utc_minute_of_week

    Example: BJT Sat(5) 04:00 → minute_of_week = 5*1440 + 240 = 7440.
    minus 480 = 6960. 6960 // 1440 = 4 (Friday) ✓.  rem 1200 → 20:00.
    So BJT Sat 04:00 → UTC Fri 20:00. ✓

    weekday convention: 0=Monday ... 6=Sunday (datetime.weekday).
    """
    bjt_minute = weekday_bjt * 24 * 60 + hour_bjt * 60 + minute_bjt
    utc_minute = (bjt_minute - _BJT_OFFSET_HOURS * 60) % (7 * 24 * 60)
    weekday_utc = utc_minute // (24 * 60)
    rem = utc_minute % (24 * 60)
    hour_utc = rem // 60
    minute_utc = rem % 60
    return weekday_utc, hour_utc, minute_utc


def _settings():
    """Lazy access — tests can monkeypatch attributes on the singleton."""
    from config import settings
    return settings


async def _archive_scanner_loop() -> None:
    """Daily archive candidate scan. Honors:
      - settings.enable_pan_backup            (off → skip every tick)
      - settings.pan_auto_archive_enabled     (off → skip)
      - settings.pan_auto_archive_dry_run     (passed into tick)
      - settings.pan_auto_archive_days        (age threshold)
      - settings.pan_auto_archive_max_per_run (limit)
      - settings.pan_auto_archive_hour_bjt    (cron hour in BJT)
    """
    # Initial offset 240s to spread load with other startup sweepers.
    await asyncio.sleep(240)
    while True:
        s = _settings()
        if not s.enable_pan_backup or not s.pan_auto_archive_enabled:
            # Flag off → tick is no-op. Still sleep until next slot so
            # turning the flag on mid-day picks up at the next cron.
            logger.debug(
                "pan_archive_scanner: skipped tick "
                "(enable_pan_backup=%s, auto_archive_enabled=%s)",
                s.enable_pan_backup, s.pan_auto_archive_enabled,
            )
        else:
            try:
                from database import async_session as _session
                from pan.archive_scanner import run_archive_scanner_tick

                async with _session() as db:
                    result = await run_archive_scanner_tick(
                        db,
                        age_days=s.pan_auto_archive_days,
                        max_per_run=s.pan_auto_archive_max_per_run,
                        dry_run=s.pan_auto_archive_dry_run,
                    )
                    if result.get('enqueued') or result.get('candidates'):
                        logger.info(
                            "pan_archive_scanner tick: enqueued=%d "
                            "candidates=%d dry_run=%s",
                            result.get('enqueued', 0),
                            len(result.get('candidates', [])),
                            result.get('dry_run'),
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pan_archive_scanner tick failed: %s", exc)

        sleep_s = _seconds_until_next_daily(
            _bjt_hour_to_utc(s.pan_auto_archive_hour_bjt), 30,
        )
        await asyncio.sleep(sleep_s)


async def _token_refresh_loop() -> None:
    """Every 6h pan token refresh. Gated by settings.enable_pan_backup."""
    await asyncio.sleep(300)  # 5 min initial offset
    while True:
        s = _settings()
        if not s.enable_pan_backup:
            logger.debug("pan_token_refresh: skipped (enable_pan_backup off)")
        else:
            try:
                from database import async_session as _session
                from pan.auth import pan_token_refresh_tick

                async with _session() as db:
                    stats = await pan_token_refresh_tick(db)
                    if stats['refreshed'] or stats['revoked']:
                        logger.info(
                            "pan_token_refresh: checked=%d refreshed=%d "
                            "revoked=%d",
                            stats['checked'], stats['refreshed'],
                            stats['revoked'],
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pan_token_refresh tick failed: %s", exc)

        await asyncio.sleep(TOKEN_REFRESH_INTERVAL_S)


async def _orphan_cleanup_loop() -> None:
    """Weekly orphan cleanup. Gated by settings.enable_pan_backup. Weekday
    + BJT hour from settings."""
    s = _settings()
    # CodeX P2 (2026-05-19): weekday must convert BJT→UTC alongside the
    # hour. BJT Sat 04:00 = UTC Fri 20:00 (one day earlier in UTC).
    # Just translating hour without weekday would land on UTC Sat 20:00,
    # which equals BJT Sun 04:00.
    wd_utc, hr_utc, min_utc = _bjt_weekly_to_utc(
        s.pan_orphan_cleanup_weekday, 4, 0,
    )
    sleep_s = _seconds_until_next_weekly(wd_utc, hr_utc, min_utc)
    await asyncio.sleep(sleep_s)
    while True:
        s = _settings()
        if not s.enable_pan_backup:
            logger.debug("pan_orphan_cleanup: skipped (enable_pan_backup off)")
        else:
            try:
                from database import engine as _engine
                from pan.orphan_cleanup import run_orphan_cleanup_tick

                stats = await run_orphan_cleanup_tick(_engine)
                logger.info(
                    "pan_orphan_cleanup tick: A_deleted=%d B_keys=%d C_states=%d",
                    stats['pass_a']['deleted'],
                    stats['pass_b']['keys_deleted'],
                    stats['pass_c']['states_deleted'],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pan_orphan_cleanup tick failed: %s", exc)

        s = _settings()
        # CodeX P2 (2026-05-19): same BJT-weekday correction as the
        # initial-sleep helper above.
        wd_utc, hr_utc, min_utc = _bjt_weekly_to_utc(
            s.pan_orphan_cleanup_weekday, 4, 0,
        )
        sleep_s = _seconds_until_next_weekly(wd_utc, hr_utc, min_utc)
        await asyncio.sleep(sleep_s)


async def _stale_reaper_loop() -> None:
    """Every 30 min — reap stuck pan operations. Gated by enable_pan_backup."""
    await asyncio.sleep(120)  # 2 min initial offset
    while True:
        s = _settings()
        if not s.enable_pan_backup:
            logger.debug("pan_stale_reaper: skipped (enable_pan_backup off)")
        else:
            try:
                from database import engine as _engine
                from pan.stale_reaper import run_stale_reaper_tick

                stats = await run_stale_reaper_tick(
                    _engine, stale_hours=s.pan_task_stale_hours,
                )
                if (stats['in_flight_reaped'] or stats['post_commit_forwarded']
                        or stats['in_flight_skipped_locked']
                        or stats['post_commit_skipped_locked']):
                    logger.info(
                        "pan_stale_reaper tick: reaped=%d forwarded=%d "
                        "skipped_locked=%d",
                        stats['in_flight_reaped'],
                        stats['post_commit_forwarded'],
                        stats['in_flight_skipped_locked']
                        + stats['post_commit_skipped_locked'],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pan_stale_reaper tick failed: %s", exc)

        await asyncio.sleep(STALE_REAPER_INTERVAL_S)


def register_pan_schedulers(app) -> None:
    """Register all 4 pan scheduler loops on the FastAPI app.

    Tasks are stashed on app.state.pan_*_task so the lifespan shutdown
    can cancel them cleanly. Failure to start a loop is logged but
    does NOT block gateway startup (consistent with existing scheduler
    pattern in main.py).
    """
    spec = [
        ('pan_archive_scanner', _archive_scanner_loop,
         'pan_archive_scanner_task'),
        ('pan_token_refresh', _token_refresh_loop,
         'pan_token_refresh_task'),
        ('pan_orphan_cleanup', _orphan_cleanup_loop,
         'pan_orphan_cleanup_task'),
        ('pan_stale_reaper', _stale_reaper_loop,
         'pan_stale_reaper_task'),
    ]
    for name, fn, attr in spec:
        try:
            task = asyncio.create_task(fn(), name=name)
            setattr(app.state, attr, task)
            logger.info("pan_scheduler: registered %s", name)
        except Exception:
            logger.exception(
                "pan_scheduler: failed to start %s; continuing without it",
                name,
            )

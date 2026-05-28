"""Phase 4.3b-C — temporary voice cleanup sweeper (gateway lifespan task).

周期跑 ``express_voice_cleanup_service.cleanup_expired_temporary_voices``：到期
临时 cosyvoice 音色 → 删 DashScope voice（付费）→ 软删 DB。参照 PR2
``express_reservation_sweeper`` 的 lifespan/fail-safe 模式。

**边界（Codex 4.3b-C）**：

- **worker 不可用在 claim 之前 fail-fast**：实跑模式每轮先用
  ``is_mainland_voice_worker_config_ready`` 纯读探针确认 worker 配好；没配好 →
  整轮 skip（**不认领**，避免认领行 lease 空占到过期）。
- **默认 dry-run**（``AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN`` 默认 ``true``）：观察期
  只 log/audit "would delete N"，不真删；显式设 ``false`` 才实删（灰度确认后）。
- **audit 覆盖 success / fail / give-up / dry-run**：注入 ``emit_voice_cleanup_audit``
  给 core，每音色一行 JSONL。
- **fail-safe**：单轮异常只 log，loop 续命，绝不崩 gateway。
- worker 同步 client 由 core 用 ``asyncio.to_thread`` 丢线程（不阻塞事件循环）。

保守默认：1h 周期、单批 50、启动延迟 300s。
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = int(os.environ.get("AVT_EXPRESS_VOICE_CLEANUP_INTERVAL_S", "3600"))
SWEEP_BATCH_SIZE = int(os.environ.get("AVT_EXPRESS_VOICE_CLEANUP_BATCH_SIZE", "50"))
INITIAL_DELAY_S = int(os.environ.get("AVT_EXPRESS_VOICE_CLEANUP_INITIAL_DELAY_S", "300"))


def _dry_run_default() -> bool:
    """默认 True（观察期）。显式 ``AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN=false`` 才实删。"""
    return os.environ.get("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", "true").strip().lower() != "false"


def _dry_run_worker_delete(voice_id, *, user_id, job_id, reason):  # pragma: no cover - 占位
    """dry-run 占位 worker_delete —— core 在 dry-run 分支不会调它。被调即 bug。"""
    raise RuntimeError("worker_delete must not be called in dry-run")


async def sweep_once(*, session_factory=None, dry_run=None):
    """跑一轮清理。``dry_run=None`` → 读 env 默认（观察期 True）。

    实跑模式：**worker 不可用在 claim 前 fail-fast**（整轮 skip，return None）。
    返回 ``CleanupReport`` 或 None（worker 未配置而 skip）。
    """
    import express_voice_cleanup_service as svc
    from express_voice_cleanup_audit import emit_voice_cleanup_audit

    dry = _dry_run_default() if dry_run is None else bool(dry_run)
    if session_factory is None:
        from database import async_session
        session_factory = async_session

    worker_delete = _dry_run_worker_delete
    client = None
    if not dry:
        # claim 前 fail-fast：worker 配置 ready 才继续；否则整轮 skip（不认领）
        from config import settings as gw_settings
        from mainland_voice_worker import (
            build_mainland_voice_worker_client,
            is_mainland_voice_worker_config_ready,
        )

        if not is_mainland_voice_worker_config_ready(gw_settings):
            logger.info(
                "express_voice_cleanup_sweeper: mainland worker not configured "
                "— skip tick before claim (no rows claimed)"
            )
            return None
        client = build_mainland_voice_worker_client(gw_settings)
        if client is None:
            logger.info(
                "express_voice_cleanup_sweeper: worker client build returned None — skip tick"
            )
            return None
        from services.mainland_worker.types import WorkerDeleteVoiceRequest

        def worker_delete(voice_id, *, user_id, job_id, reason):
            client.delete_voice(
                str(voice_id),
                WorkerDeleteVoiceRequest(
                    job_id=str(job_id), user_id=str(user_id), reason=str(reason)
                ),
            )

    try:
        return await svc.cleanup_expired_temporary_voices(
            session_factory,
            worker_delete=worker_delete,
            dry_run=dry,
            limit=SWEEP_BATCH_SIZE,
            audit_emit=emit_voice_cleanup_audit,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — close 失败不致命
                logger.warning("express_voice_cleanup_sweeper: worker client close failed")


async def _interruptible_sleep(delay_s: float, stop_event: asyncio.Event | None) -> bool:
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except asyncio.TimeoutError:
        return False


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """周期跑 sweep_once。单轮崩溃只 log + 续命，绝不让清理故障终止 loop / 影响
    gateway。生产 ``stop_event=None``，shutdown 靠 ``task.cancel()``。"""
    logger.info(
        "express_voice_cleanup_sweeper started (interval=%ss, batch=%d, dry_run_default=%s)",
        SWEEP_INTERVAL_S, SWEEP_BATCH_SIZE, _dry_run_default(),
    )
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            report = await sweep_once()
            if report is not None and (report.deleted or report.failed or report.gave_up):
                logger.info(
                    "express_voice_cleanup_sweeper: deleted=%d failed=%d gave_up=%d",
                    report.deleted, report.failed, report.gave_up,
                )
        except Exception:
            logger.exception(
                "express_voice_cleanup_sweeper tick failed; retrying next interval"
            )
        if await _interruptible_sleep(SWEEP_INTERVAL_S, stop_event):
            return


__all__ = [
    "sweep_once",
    "sweeper_loop",
    "SWEEP_INTERVAL_S",
    "SWEEP_BATCH_SIZE",
    "INITIAL_DELAY_S",
]

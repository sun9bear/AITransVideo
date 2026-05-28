"""Phase 4.3b-B — temporary voice cleanup core (claim-lease + two-step delete).

承载 spec §4.1 的清理核心。临时音色到期 → **先删 DashScope voice（付费，注入式
worker_delete）→ 才软删 DB**（写 ``expired_at``）。失败永不写 ``expired_at``，
靠 backoff / give-up 重试，并发靠 claim-lease 防重复付费删除。

核心不变量（spec §2.1 / §2.2 / §2.7）：

- **两步且按序**：claim → 事务外 worker_delete 成功 → ``complete_soft_delete``。
  颠倒会留 DashScope 孤儿。
- **claim-lease 并发**：``claim_batch`` 短事务用 ``FOR UPDATE SKIP LOCKED``（PG）
  原子认领一批，写 ``cleanup_claim_until`` + ``cleanup_run_id``，COMMIT 释放行锁；
  worker 调用在事务外。sqlite 无 SKIP LOCKED（单 runner 测），并发原子性留 PG（B-pg）。
- **完成 / 失败更新都用 ``cleanup_run_id`` 守卫**：若本 runner 的 lease 已过期被
  别的 runner 重认领（run_id 变了），本次更新 no-op，不 clobber。
- **失败立刻释放 lease**（清 ``cleanup_claim_until`` / ``cleanup_run_id``）——
  不留 lease 悬挂到过期才释放（Codex 4.3b-B 重点）。成功软删也清 claim 字段。
- **未知 delete 失败全部 backoff/give-up**，**不**做 already-gone 成功映射
  （spec §2.4：真实 provider 把任意异常统一成 ``delete_voice_failed``，无稳定
  already-gone code；该优化前置 A0，默认关闭）。

**边界**：本模块**不 import** worker client —— ``worker_delete`` 是注入式
callable（C/sweeper 层装配真 ``MainlandWorkerClient.delete_voice``；worker
不可用的 fail-fast 也在 C 层，不在这里）。测试注入 mock，0 真实 DashScope。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserVoice

logger = logging.getLogger(__name__)


def _safe_audit(audit_emit, /, **fields) -> None:
    """调注入式 audit 回调；audit 永不破坏清理（buggy emitter / 写盘失败都吞）。

    ``audit_emit=None`` → no-op（B core 不强制 audit；C/D 装配真 emitter）。
    """
    if audit_emit is None:
        return
    try:
        audit_emit(**fields)
    except Exception:  # noqa: BLE001 — audit 非致命
        logger.exception("voice cleanup audit emit failed (non-fatal)")

# 只清 Express 自动 clone 出的临时 cosyvoice 音色（有 DashScope worker voice 可删）
CLEANUP_PROVIDER = "cosyvoice_voice_clone"
# give-up 阈值：失败达此次数停止自动重试，转 manual（spec §2.2）
MAX_CLEANUP_ATTEMPTS = 5
# claim 租约（spec §2.7）：必须 > delete_voice 最坏重试窗口（见守卫常量）
CLEANUP_CLAIM_LEASE_SECONDS = 600
# delete_voice 最坏重试窗口安全下界（守卫断言 LEASE >= 此值；spec §2.7）。
# 与真实 client 常量绑定（src/services/mainland_worker/client.py，2026-05-28）：
#   MAX_NETWORK_RETRIES = 3
#   DEFAULT_TIMEOUT = Timeout(pool=5, connect=5, read=60, write=10)
#   RETRY_BACKOFF_SECONDS = (1, 5, 15)  → 3 次尝试间 2 次退避 = 1+5 = 6s
# 最坏 ≈ 3 × (pool 5 + connect 5 + read 60 + write 10) + 6 = 246s。取 300s 留 margin。
# **守卫**（test_lease_exceeds_real_delete_worst_case）从真实 client 常量重算
# （含 pool/connect/read/write 全 4 段），client retry/timeout 变大而 LEASE/floor
# 没跟上 → 测试 red（Codex 4.3b-B-fix P2 + 二轮 pool 补全）。
DELETE_VOICE_WORST_CASE_FLOOR_SECONDS = 300
# 失败 backoff 基数（指数退避，封顶 1h）
_BACKOFF_BASE_SECONDS = 300
_BACKOFF_CAP_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int) -> timedelta:
    secs = _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return timedelta(seconds=min(secs, _BACKOFF_CAP_SECONDS))


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)[:200]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


@dataclass(frozen=True)
class ClaimedVoice:
    """认领到的行的纯值快照（脱离 ORM session，跨事务安全传递）。

    携带 ``temporary_expires_at`` / ``cleanup_attempts``（认领时的值）供 audit
    结构化（spec §6 对账字段）。"""

    pk: object               # UserVoice.id
    voice_id: str
    user_id: object
    source_job_id: str | None
    temporary_expires_at: datetime | None = None
    cleanup_attempts: int = 0


@dataclass
class CleanupReport:
    dry_run: bool
    selected: list[str] = field(default_factory=list)  # dry-run: 候选；实跑: 认领的 voice_id
    deleted: int = 0
    failed: int = 0
    gave_up: int = 0
    run_id_conflict: int = 0  # 完成/失败时发现 lease 已被重认领（不 double-count）


def _eligible_clauses(now: datetime, *, include_give_up: bool):
    """spec §3 / DoD #1 选行条件（含 retry backoff + claim lease gate）。"""
    clauses = [
        UserVoice.provider == CLEANUP_PROVIDER,
        UserVoice.requires_worker.is_(True),
        UserVoice.is_temporary.is_(True),
        UserVoice.temporary_expires_at.isnot(None),
        UserVoice.temporary_expires_at < now,
        UserVoice.expired_at.is_(None),
        # backoff gate
        (UserVoice.cleanup_retry_after.is_(None)) | (UserVoice.cleanup_retry_after < now),
        # claim lease gate（§2.7）：未被他人 lease（空 / 已过期）
        (UserVoice.cleanup_claim_until.is_(None)) | (UserVoice.cleanup_claim_until < now),
    ]
    if not include_give_up:
        clauses.append(UserVoice.cleanup_attempts < MAX_CLEANUP_ATTEMPTS)
    return clauses


async def select_eligible(
    db: AsyncSession, *, limit: int, now: datetime | None = None, include_give_up: bool = False
) -> list[UserVoice]:
    """选到期可清理行（不认领、不锁）。dry-run + 单测用。"""
    now = now or _now()
    rows = (
        await db.execute(
            select(UserVoice)
            .where(*_eligible_clauses(now, include_give_up=include_give_up))
            .order_by(UserVoice.temporary_expires_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def claim_batch(
    db: AsyncSession,
    *,
    run_id: str,
    limit: int,
    now: datetime | None = None,
    lease_seconds: int = CLEANUP_CLAIM_LEASE_SECONDS,
    include_give_up: bool = False,
) -> list[ClaimedVoice]:
    """phase 1：原子认领一批（短事务，PG ``FOR UPDATE SKIP LOCKED``）。

    写 ``cleanup_claim_until = now + lease`` + ``cleanup_run_id``，COMMIT 释放
    行锁。返回纯值快照（脱离 session）。sqlite 忽略 SKIP LOCKED（单 runner）。
    """
    now = now or _now()
    rows = (
        await db.execute(
            select(UserVoice)
            .where(*_eligible_clauses(now, include_give_up=include_give_up))
            .order_by(UserVoice.temporary_expires_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if not rows:
        await db.commit()
        return []
    claim_until = now + timedelta(seconds=int(lease_seconds))
    claimed: list[ClaimedVoice] = []
    for row in rows:
        row.cleanup_claim_until = claim_until
        row.cleanup_run_id = run_id
        row.updated_at = now
        claimed.append(
            ClaimedVoice(
                pk=row.id,
                voice_id=row.voice_id,
                user_id=row.user_id,
                source_job_id=getattr(row, "source_job_id", None),
                temporary_expires_at=row.temporary_expires_at,
                cleanup_attempts=int(row.cleanup_attempts or 0),
            )
        )
    await db.commit()
    return claimed


async def complete_soft_delete(
    db: AsyncSession, voice_pk: object, *, run_id: str, now: datetime | None = None
) -> bool:
    """成功路径：软删（``expired_at=now``）+ 清 claim 字段。

    **run_id 守卫 + ``expired_at IS NULL``**：若 lease 已被重认领（run_id 变）
    或已被别处软删，更新 0 行 → 返 False（不 clobber、不 double-complete）。
    """
    now = now or _now()
    result = await db.execute(
        update(UserVoice)
        .where(
            UserVoice.id == voice_pk,
            UserVoice.cleanup_run_id == run_id,
            UserVoice.expired_at.is_(None),
        )
        .values(
            expired_at=now,
            cleanup_claim_until=None,
            cleanup_run_id=None,
            updated_at=now,
        )
    )
    await db.commit()
    return int(result.rowcount or 0) > 0


async def release_with_backoff(
    db: AsyncSession, voice_pk: object, *, run_id: str, error: str, now: datetime | None = None
) -> str:
    """失败路径：``attempts+1`` + ``last_error`` + **立刻清 claim**（不留 lease 悬挂）
    + 设 ``retry_after``（或 give-up）。**run_id 守卫**。

    返回 ``"failed"`` / ``"gave_up"`` / ``"noop"``（run_id 冲突，已被重认领）。
    """
    now = now or _now()
    row = (
        await db.execute(
            select(UserVoice).where(
                UserVoice.id == voice_pk,
                UserVoice.cleanup_run_id == run_id,  # 守卫：lease 仍属本 runner
                UserVoice.expired_at.is_(None),  # 守卫：行已软删（manual/竞态）→ 不再 bump attempts/error
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return "noop"
    row.cleanup_attempts = int(row.cleanup_attempts or 0) + 1
    row.cleanup_last_error = (error or "")[:200]
    # Codex 4.3b-B 重点：失败立刻释放 lease，不等过期
    row.cleanup_claim_until = None
    row.cleanup_run_id = None
    row.updated_at = now
    if row.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
        # give-up：不再设 retry_after；select 用 attempts < MAX 自动排除
        row.cleanup_retry_after = None
        outcome = "gave_up"
    else:
        row.cleanup_retry_after = now + _backoff(row.cleanup_attempts)
        outcome = "failed"
    await db.commit()
    return outcome


async def reset_cleanup_state(
    db: AsyncSession, *, now: datetime | None = None, limit: int = 200
) -> int:
    """**Manual ops only**（CLI ``--reset-attempts``）：把 eligible-base 的到期临时
    cosyvoice 音色里 ``cleanup_attempts > 0`` 的行重置——清 attempts / retry_after /
    last_error / claim_until / run_id，让 give-up / backoff 行重新可被清理。

    **自动 sweeper 绝不调本函数**（give-up 是有意停手，自动重试会无限刷付费）。

    **并发安全（Codex 4.3b-D-fix P1）**：**绝不**重置正被其它 runner 认领
    （``cleanup_claim_until > now``，in-flight）的行——否则清掉它的 ``run_id``
    会让那个 runner 删完 worker 后 ``complete_soft_delete`` 更新 0 行、DB 仍
    ``expired_at=NULL`` → 下轮重复 ``delete_voice``。select 用
    ``FOR UPDATE SKIP LOCKED`` + claim-lease predicate 原子排除 in-flight，
    UPDATE 再重复 predicate（belt-and-suspenders）。返回重置的行数。
    """
    now = now or _now()
    not_in_flight = (UserVoice.cleanup_claim_until.is_(None)) | (
        UserVoice.cleanup_claim_until < now
    )
    stuck_ids = (
        await db.execute(
            select(UserVoice.id)
            .where(
                UserVoice.provider == CLEANUP_PROVIDER,
                UserVoice.requires_worker.is_(True),
                UserVoice.is_temporary.is_(True),
                UserVoice.temporary_expires_at.isnot(None),
                UserVoice.temporary_expires_at < now,
                UserVoice.expired_at.is_(None),
                UserVoice.cleanup_attempts > 0,
                not_in_flight,  # 不碰 in-flight 行（防与 auto sweeper 竞态 double-delete）
            )
            .limit(limit)
            .with_for_update(skip_locked=True)  # PG：原子跳过他人持锁行；sqlite no-op
        )
    ).scalars().all()
    if not stuck_ids:
        return 0
    result = await db.execute(
        update(UserVoice)
        .where(UserVoice.id.in_(stuck_ids), not_in_flight)  # 重复 predicate 防 TOCTOU
        .values(
            cleanup_attempts=0,
            cleanup_retry_after=None,
            cleanup_last_error=None,
            cleanup_claim_until=None,
            cleanup_run_id=None,
            updated_at=now,
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


async def cleanup_expired_temporary_voices(
    db_factory,
    *,
    worker_delete,
    dry_run: bool,
    limit: int,
    now: datetime | None = None,
    include_give_up: bool = False,
    lease_seconds: int = CLEANUP_CLAIM_LEASE_SECONDS,
    audit_emit=None,
) -> CleanupReport:
    """清理核心入口（spec §4.1）。

    ``db_factory``：``() -> async context manager``（每个 phase 用独立 session；
    **绝不**跨 worker 调用持事务）。``worker_delete``：注入式
    ``(voice_id, *, user_id, job_id, reason) -> None``，失败 raise。

    流程：dry-run → 只 SELECT 报告；否则 phase1 claim → phase2 事务外逐行
    worker_delete → 成功 ``complete_soft_delete`` / 失败 ``release_with_backoff``。
    """
    run_id = uuid.uuid4().hex
    now = now or _now()

    # dry-run：只选行报告，绝不认领 / 不调 worker / 不改 DB（spec §2.3）
    if dry_run:
        async with db_factory() as db:
            rows = await select_eligible(
                db, limit=limit, now=now, include_give_up=include_give_up
            )
            for r in rows:
                _safe_audit(
                    audit_emit, decision="dry_run", voice_id=r.voice_id,
                    user_id=str(r.user_id), dry_run=True,
                    cleanup_attempts=int(r.cleanup_attempts or 0),
                    temporary_expires_at=_iso(r.temporary_expires_at),
                )
            return CleanupReport(dry_run=True, selected=[r.voice_id for r in rows])

    # phase 1：认领（短事务）
    async with db_factory() as db:
        claimed = await claim_batch(
            db, run_id=run_id, limit=limit, now=now,
            lease_seconds=lease_seconds, include_give_up=include_give_up,
        )
    if not claimed:
        return CleanupReport(dry_run=False, selected=[])

    report = CleanupReport(dry_run=False, selected=[c.voice_id for c in claimed])

    # phase 2：处理（事务外，逐行；每行独立短事务更新）
    for c in claimed:
        try:
            # 付费/外部。worker client 是**同步阻塞** HTTP（read 超时可达 60s）；
            # 丢线程跑，**绝不阻塞 gateway 事件循环**（参照 r2_artifact_sweeper
            # 的 asyncio.to_thread(publish_artifacts)）。失败 raise。
            # 返回值是 worker_request_id（对账锚点；mock / 旧 adapter 可能返 None）
            worker_request_id = await asyncio.to_thread(
                worker_delete,
                c.voice_id,
                user_id=c.user_id,
                job_id=c.source_job_id or "cleanup",
                reason="temporary_voice_ttl_cleanup",
            )
        except Exception as exc:  # noqa: BLE001 — 任意失败均 backoff/give-up（无 already-gone 捷径）
            async with db_factory() as db:
                outcome = await release_with_backoff(
                    db, c.pk, run_id=run_id, error=_error_code(exc), now=_now()
                )
            code = _error_code(exc)
            # 失败后 attempts = 认领时的值 + 1（release_with_backoff 已 +1）
            attempts_after = c.cleanup_attempts + 1
            if outcome == "gave_up":
                report.gave_up += 1
                _safe_audit(audit_emit, decision="cleanup_give_up",
                            voice_id=c.voice_id, user_id=str(c.user_id), error=code,
                            cleanup_attempts=attempts_after,
                            temporary_expires_at=_iso(c.temporary_expires_at))
            elif outcome == "failed":
                report.failed += 1
                _safe_audit(audit_emit, decision="cleanup_failed",
                            voice_id=c.voice_id, user_id=str(c.user_id), error=code,
                            cleanup_attempts=attempts_after,
                            temporary_expires_at=_iso(c.temporary_expires_at))
            else:  # noop：lease 已被重认领
                report.run_id_conflict += 1
            continue
        # 成功 → 软删（run_id 守卫）
        async with db_factory() as db:
            ok = await complete_soft_delete(db, c.pk, run_id=run_id, now=_now())
        if ok:
            report.deleted += 1
            _safe_audit(audit_emit, decision="cleaned",
                        voice_id=c.voice_id, user_id=str(c.user_id),
                        worker_request_id=worker_request_id,
                        cleanup_attempts=c.cleanup_attempts,
                        temporary_expires_at=_iso(c.temporary_expires_at))
        else:
            report.run_id_conflict += 1
    return report


__all__ = [
    "CLEANUP_PROVIDER",
    "MAX_CLEANUP_ATTEMPTS",
    "CLEANUP_CLAIM_LEASE_SECONDS",
    "DELETE_VOICE_WORST_CASE_FLOOR_SECONDS",
    "ClaimedVoice",
    "CleanupReport",
    "select_eligible",
    "claim_batch",
    "complete_soft_delete",
    "release_with_backoff",
    "reset_cleanup_state",
    "cleanup_expired_temporary_voices",
]

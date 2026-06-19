"""P3e-3c-2: preview→full smart-clone reuse contract (server-side).

plan 2026-06-14-p3e2-preview-lane-design.md §7. Validates that a finished
smart *preview* job's 600-credit clone was actually CAPTURED for the
**same** user, then retrieves the server-authoritative ``voice_id`` + original
source reference so a full paid job can reuse them WITHOUT re-cloning or
re-charging the 600. The frontend only sends ``reuse_preview_job_id``; this
module is the 越权 (overreach) defense — ``voice_id`` is NEVER taken from
the client.

Money / security invariants:
- ``voice_id`` is derived ONLY from a CAPTURED ``SmartCloneReservation``
  (status ``captured`` + ``settled_at`` set + ``captured_voice_id``) cross-
  checked against a chargeable ``CloneBillingEvent`` — reader B's 唯一权威
  计费信号. A reserve-then-released / never-cloned / denied preview yields
  no captured row → reject (never silently treated as "paid").
- ownership: the preview Job must belong to ``user_id`` (cross-user → reject).
- voice liveness: the cloned voice must still be a live ``UserVoice`` row
  (``expired_at IS NULL``) for that user.
- pure DB reads — no paid API, no writes. The caller (create path) does the
  ``request_data`` override and lets the normal full create flow charge
  minutes; this module decides nothing about money beyond "is the prior
  clone genuinely captured and reusable".
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from models import (
    AnonymousPreviewRecord,
    CloneBillingEvent,
    Job,
    SmartCloneReservation,
    UserVoice,
)
from smart_clone_reservation_service import CAPTURED as _CAPTURED_STATUS

# Rejection reason codes (stable; surfaced to the create-path 4xx response).
REASON_NOT_FOUND = "preview_not_found"
REASON_FORBIDDEN = "preview_forbidden"
REASON_NOT_PREVIEW = "preview_not_a_preview_job"
REASON_NOT_CAPTURED = "preview_clone_not_captured"
REASON_VOICE_UNAVAILABLE = "preview_voice_unavailable"
REASON_SOURCE_UNAVAILABLE = "preview_source_unavailable"

# D7 匿名预览转完整 reason codes（独立命名空间，与 smart preview reuse 区分）。
REASON_ANON_NOT_FOUND = "anon_preview_not_found"
REASON_ANON_FORBIDDEN = "anon_preview_forbidden"  # 未认领 / 非本人 / 非 ready 契约状态
REASON_ANON_SOURCE_UNAVAILABLE = "anon_preview_source_unavailable"  # 无源/丢失/越界/teaser/hash 不符

# 匿名预览只允许这个 ready 契约状态转完整（= anonymous_preview_api._READY_STATUS /
# PreviewStatus.READY_FOR_MODE.value）。内联字面量避免跨模块 import 私有常量。
_ANON_READY_STATUS = "ready_for_mode"


@dataclass(frozen=True)
class PreviewReuseResolution:
    """Server-derived inputs for the full reuse job (never client-supplied)."""

    preview_job_id: str
    voice_id: str
    source_type: str
    source_ref: str
    # P3e D-C（plan 2026-06-15 §4.6）：预览 600 结转 marker 的**唯一权威来源**。
    # full 任务终态结算时凭 ``preview_reservation_id`` single-use 消费该预览的
    # ``preview_credit_amount``（取自 reservation.amount_credits，不写死 600）。
    preview_reservation_id: str
    preview_credit_amount: int
    # A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX；对齐 D7
    # AnonymousPreviewReuseResolution.source_duration_seconds，消除两路径不对称）：
    # **本地源**完整全长（秒），由 resolver 对 stored final_path 重探（见
    # ``_probe_source_duration_seconds``）。**YouTube 源恒 None**——不在此重探，由创建期
    # 既有 yt-dlp 探测路径处理（不重复）。探测失败/非本地源 → None（转完整 wiring 的
    # pre-flight 闸据此跳过，管线时长 gate 兜底，绝不因探测失败误拒转完整）。纯只读派生。
    source_duration_seconds: float | None = None


async def resolve_preview_reuse(
    db, *, user_id, preview_job_id: str
) -> tuple[PreviewReuseResolution | None, str | None]:
    """Validate + resolve a preview→full reuse request.

    Returns ``(PreviewReuseResolution, None)`` on success, or
    ``(None, reason_code)`` on any rejection. Does NOT raise for validation
    failures — typed rejection so the caller maps to a 4xx response. No
    writes, no paid API.
    """
    pjid = str(preview_job_id or "").strip()
    if not pjid:
        return None, REASON_NOT_FOUND

    # 1. Preview Job must exist.
    job = (
        await db.execute(select(Job).where(Job.job_id == pjid))
    ).scalar_one_or_none()
    if job is None:
        return None, REASON_NOT_FOUND

    # 2. Ownership — 防越权：the preview must belong to the requesting user.
    #    Without this a user could pass another user's preview_job_id and
    #    reuse THEIR paid clone voice.
    if str(getattr(job, "user_id", "") or "") != str(user_id or ""):
        return None, REASON_FORBIDDEN

    # 2.5. Contract: the reused job MUST be an actual smart *preview* job
    #      (``smart_state.smart_preview_mode is True``). CodeX P3e-4c merge
    #      review: because ``smart_preview_clone_enabled`` couples *full* smart
    #      tasks into the same 600 reservation gate, a non-preview full-smart
    #      job ALSO has a captured reservation + chargeable billing event +
    #      cloned voice — so without this guard it could be "converted" via the
    #      preview→full path. Same-user + already-paid-clone constraints make
    #      that money-safe (the voice is already in the user's library and
    #      reusable by explicit selection anyway), but it violates the
    #      preview→full contract. Placed AFTER ownership so a non-owner can't
    #      probe whether a job is a preview. Strict ``is True`` (fail-safe;
    #      mirrors ``preview_policy.extract_smart_preview_flag``, inlined to keep
    #      this module's import surface minimal — see memory
    #      feedback_test_database_stub_convention).
    _smart_state = getattr(job, "smart_state", None)
    if not (
        isinstance(_smart_state, dict)
        and _smart_state.get("smart_preview_mode") is True
    ):
        return None, REASON_NOT_PREVIEW

    # 3. Authoritative capture proof: a CAPTURED reservation for (task, user)
    #    with settled_at set + captured_voice_id present (reader B). A
    #    reserve-then-released or never-captured preview has no such row.
    reservation = (
        await db.execute(
            select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == pjid,
                SmartCloneReservation.user_id == user_id,
                SmartCloneReservation.status == _CAPTURED_STATUS,
            )
        )
    ).scalar_one_or_none()
    if (
        reservation is None
        or getattr(reservation, "settled_at", None) is None
        or not str(getattr(reservation, "captured_voice_id", "") or "").strip()
    ):
        return None, REASON_NOT_CAPTURED
    voice_id = str(reservation.captured_voice_id).strip()

    # 4. Cross-check the 唯一权威计费信号: a chargeable CloneBillingEvent for
    #    this reservation. Defense in depth — capture without a chargeable
    #    event would be an inconsistent ledger state; refuse to reuse on it.
    billing = (
        await db.execute(
            select(CloneBillingEvent).where(
                CloneBillingEvent.reservation_id == reservation.id,
                CloneBillingEvent.chargeable.is_(True),
            )
        )
    ).scalar_one_or_none()
    if billing is None:
        return None, REASON_NOT_CAPTURED

    # 5. Voice liveness: must still be a live UserVoice row for this user
    #    (not expired / soft-deleted).
    voice = (
        await db.execute(
            select(UserVoice).where(
                UserVoice.user_id == user_id,
                UserVoice.voice_id == voice_id,
                UserVoice.expired_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if voice is None:
        return None, REASON_VOICE_UNAVAILABLE

    # 6. Original source reference for reuse. P3e-3b only trimmed the derived
    #    ``preview_teaser.wav`` — the Job's source_ref still points at the
    #    original full-length source (YouTube URL / upload final_path).
    source_type = str(getattr(job, "source_type", "") or "").strip()
    source_ref = str(getattr(job, "source_ref", "") or "").strip()
    if not source_type or not source_ref:
        return None, REASON_SOURCE_UNAVAILABLE

    # 7. 完整源全长（A 方案 pre-flight 时长闸用，plan 2026-06-16）。**仅对本地源**重探：
    #    本地 source_ref = 原始完整上传 final_path（server-authoritative，来自 Job 行非
    #    客户端），ffprobe 只读容器元数据（毫秒级）。YouTube 源**不**在此重探——其时长由
    #    创建期既有 yt-dlp 探测路径处理（job_intercept 创建期 duration gate），不重复探测，
    #    故 source_duration_seconds 留 None（pre-flight 闸据此跳过 → 走既有 yt-dlp 闸）。
    #    探测失败 → None（闸跳过、管线兜底，绝不误拒可转完整的源）。
    #    **故意用本地 allow-list（`== "local_video"`）而非 `!= "youtube_url"`**（CodeX 复审）：
    #    probe 必须只对**已知本地文件**源跑——绝不能把一个远端 URL 递给 ffprobe（ffprobe
    #    会真去打开 http(s) 输入 → SSRF 风险）。今天只有 local_video / youtube_url 两种持久
    #    source_type；未来若新增**本地**源类型，须在此显式加入本 allow-list（有意识决策，非
    #    静默缺口）；新增**远端**源类型则保持跳过（与 youtube 一样走创建期探测）。
    source_duration_seconds: float | None = None
    if source_type == "local_video":
        source_duration_seconds = await _probe_source_duration_seconds(Path(source_ref))

    return (
        PreviewReuseResolution(
            preview_job_id=pjid,
            voice_id=voice_id,
            source_type=source_type,
            source_ref=source_ref,
            preview_reservation_id=str(reservation.id),
            preview_credit_amount=int(getattr(reservation, "amount_credits", 0) or 0),
            source_duration_seconds=source_duration_seconds,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# D7 匿名预览 → 转完整（plan 2026-06-15-anonymous-preview-claim-binding-plan.md §6.5）
# ---------------------------------------------------------------------------
#
# 认领后的下一步：登录用户用**完整原始上传** audit.stored_upload_path（**非** 3min
# teaser）生成正式计费 job。与 smart preview reuse 的关键区别：① 无 600 克隆点结转
# （匿名 lane 是免费/快捷 CosyVoice，无 reservation）；② 不强制 service_mode（用户
# 自选 express/studio/smart，正常 gating + 计费）；③ 不复用 voice（用户自选）。
#
# 红线：本 resolver 纯 DB 读 + 只读文件 hash（无写、无付费 API、无 clone）。source
# 由服务端从认领 record 派生，**绝不**信客户端路径（防越权/遍历）。


@dataclass(frozen=True)
class AnonymousPreviewReuseResolution:
    """D7 server-derived 转完整输入（绝不取自客户端）。"""

    preview_id: str
    source_type: str
    source_ref: str  # 完整原始上传绝对路径（stored_upload_path），非 teaser
    # A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX）：**完整源全长**（秒）。
    # 匿名 record/audit 不持久化源全长（只存 teaser ~180s 时长），故由 resolver 对
    # stored_upload_path 重探一次（见 _probe_source_duration_seconds）。探测失败/
    # 不可信 → None（转完整 wiring 的 pre-flight 闸据此跳过，管线时长 gate 兜底，
    # 绝不因探测失败误拒转完整）。纯只读派生，绝不取自客户端。
    source_duration_seconds: float | None = None


def _normalize_hash(value: str | None) -> str:
    """归一化 sha256 hex（剥可选 ``algo:`` 前缀 + lower），防 intake/比对格式漂移。

    匿名 intake 存裸 hex（anonymous_preview_upload.py:316 ``digest.hexdigest()``），
    但归一化两侧使比对对 ``sha256:<hex>`` / 裸 hex 都稳健。"""
    h = str(value or "").strip().lower()
    return h.split(":")[-1] if ":" in h else h


def _sha256_file(path: Path, buf_size: int = 4 * 1024 * 1024) -> str:
    """流式 sha256（与 intake 同算法：对整个文件字节）。阻塞 I/O，须经 to_thread 调。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(buf_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _anon_upload_root() -> Path:
    """匿名上传根目录（唯一真源 = anonymous_preview_upload._resolve_project_root），
    用于 is_relative_to 越权校验锚点。本地 import 避模块加载期耦合。"""
    from anonymous_preview_upload import _resolve_project_root

    return (_resolve_project_root() / "uploads" / "anonymous").resolve()


async def _probe_source_duration_seconds(path: Path) -> float | None:
    """ffprobe **完整源全长**（秒），可信正值才返回，否则 None。

    A 方案 pre-flight 时长闸（plan 2026-06-16 转化漏斗 UX）。匿名 record/audit **不**
    持久化源全长——只存 teaser(~180s)时长——故此处对已过校验的完整源重探。``probe_source``
    是 gateway 内纯 stdlib ffprobe 封装（匿名上传 teaser 切割就用它，故 gateway 容器必装
    ffprobe）；它只读容器元数据（毫秒级，远小于上面整文件 sha256），且自身已校验时长
    可信（``ok`` 为 True 时 ``duration_seconds`` 必为有限正数）。阻塞 subprocess 经
    ``to_thread``；**任何异常/不可信吞为 None**——pre-flight 闸纯增强，绝不因探测失败
    阻断转完整（管线 _check_duration_limit 仍兜底）。惰性 import 保持本模块导入面最小。
    """
    try:
        from anonymous_preview_probe import probe_source  # noqa: PLC0415

        result = await asyncio.to_thread(probe_source, path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    try:
        dur = float(result.get("duration_seconds"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # probe_source 的 ok 契约已保证有限正数；此处 ``> 0`` 兜底（NaN > 0 == False → None）。
    return dur if dur > 0 else None


async def resolve_anonymous_preview_reuse(
    db, *, user_id, preview_id: str
) -> tuple[AnonymousPreviewReuseResolution | None, str | None]:
    """校验 + 解析「匿名预览转完整」请求。

    成功 → ``(AnonymousPreviewReuseResolution, None)``；拒绝 → ``(None, reason_code)``
    （不 raise，调用方映射 4xx）。纯 DB 读 + 只读文件 hash，无写、无付费 API。

    校验链（plan §6.5 / v3 #5）：record 存在 → claim_user_id==本人（认领过）→ status
    == ready_for_mode → audit.stored_upload_path 非空 → 规范化后**位于匿名上传根内**
    （防遍历）→ **非 teaser** → 文件存在 → sha256 **匹配 record.source_hash**（防陈旧/替换）。
    """
    pid = str(preview_id or "").strip()
    if not pid:
        return None, REASON_ANON_NOT_FOUND

    # 1. record 存在。
    rec = (
        await db.execute(
            select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.preview_id == pid
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        return None, REASON_ANON_NOT_FOUND

    # 2. 所有权：必须已被本用户认领（claim_user_id==user）。未认领（NULL）/他人 → 拒。
    #    str().strip() 两侧比较，对 UUID 列稳健（不依赖 asyncpg 严格类型）+ 容忍尾随空白。
    if str(getattr(rec, "claim_user_id", "") or "").strip() != str(user_id or "").strip():
        return None, REASON_ANON_FORBIDDEN

    # 3. 状态：仅 ready 契约态可转（与 create gate / 认领过滤同契约）。strip 容忍空白。
    if str(getattr(rec, "status", "") or "").strip() != _ANON_READY_STATUS:
        return None, REASON_ANON_FORBIDDEN

    # 3.5. expires_at 防御（认领已延长 7d）：过期 → 源可能已被 sweeper 清，提前拒。
    exp = getattr(rec, "expires_at", None)
    if exp is not None:
        try:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                return None, REASON_ANON_SOURCE_UNAVAILABLE
        except Exception:  # noqa: BLE001 — 时间比较异常不阻断（后续文件存在/hash 兜底）
            pass

    # 4. 完整源路径来自 audit（**非** job.source_ref——那是 teaser）。audit 可能 NULL。
    audit = getattr(rec, "audit", None) or {}
    stored_str = audit.get("stored_upload_path")
    if not stored_str:
        return None, REASON_ANON_SOURCE_UNAVAILABLE
    try:
        stored = Path(str(stored_str)).resolve()
    except Exception:  # noqa: BLE001
        return None, REASON_ANON_SOURCE_UNAVAILABLE

    # 5. 越权/遍历：规范化后必须在匿名上传根内（用 .resolve()+is_relative_to，非
    #    str.startswith——后者可被 ``../`` 绕过）。
    try:
        if not stored.is_relative_to(_anon_upload_root()):
            return None, REASON_ANON_SOURCE_UNAVAILABLE
    except Exception:  # noqa: BLE001
        return None, REASON_ANON_SOURCE_UNAVAILABLE

    # 6. 非 teaser（防误用 3min 裁剪片）。权威判定 = 精确比对 audit['teaser_path']
    #    （CodeX P3：stem 前缀启发式可能误伤名为 teaser_* 的原始上传）；teaser_path
    #    缺失/异常时退到 stem 兜底（uploads 带 upload_id 前缀，实际不会误伤）。
    teaser_str = audit.get("teaser_path")
    if teaser_str:
        try:
            if stored == Path(str(teaser_str)).resolve():
                return None, REASON_ANON_SOURCE_UNAVAILABLE
        except Exception:  # noqa: BLE001 — teaser_path 异常 → 退 stem 兜底
            pass
    if stored.stem.startswith("teaser_"):
        return None, REASON_ANON_SOURCE_UNAVAILABLE

    # 7. 文件存在。
    if not stored.is_file():
        return None, REASON_ANON_SOURCE_UNAVAILABLE

    # 8. hash 匹配 record.source_hash（防陈旧/被替换文件）。流式 sha256 走 to_thread。
    expected = _normalize_hash(getattr(rec, "source_hash", ""))
    if not expected:
        return None, REASON_ANON_SOURCE_UNAVAILABLE
    try:
        actual = await asyncio.to_thread(_sha256_file, stored)
    except Exception:  # noqa: BLE001
        return None, REASON_ANON_SOURCE_UNAVAILABLE
    if _normalize_hash(actual) != expected:
        return None, REASON_ANON_SOURCE_UNAVAILABLE

    # 9. 完整源全长（A 方案 pre-flight 时长闸用）。record/audit 不持久化源全长 → 对
    #    已过校验的完整源重探一次（纯只读，失败 → None，闸跳过、管线兜底）。
    source_duration_seconds = await _probe_source_duration_seconds(stored)

    # source_type 固定 "local_video"（**不**透传 rec.source_type）。CodeX P1：匿名
    # record 的 source_type 是 intake 内部值 "local_upload"（SourceType.LOCAL_UPLOAD），
    # 而正式 create 流程只归一化 local_file→local_video、**不认 local_upload**
    # （job_intercept.py:1827），pipeline 本地分支也只认 local_video（process.py:2870）。
    # 匿名 create 自身发给 Job API 的也是硬编码 "local_video"（anonymous_preview_api.py:1433）。
    # stored_upload_path 恒为本地文件 → 正确规范类型就是 local_video。
    return (
        AnonymousPreviewReuseResolution(
            preview_id=pid,
            source_type="local_video",
            source_ref=str(stored),
            source_duration_seconds=source_duration_seconds,
        ),
        None,
    )


__all__ = [
    "PreviewReuseResolution",
    "resolve_preview_reuse",
    "REASON_NOT_FOUND",
    "REASON_FORBIDDEN",
    "REASON_NOT_PREVIEW",
    "REASON_NOT_CAPTURED",
    "REASON_VOICE_UNAVAILABLE",
    "REASON_SOURCE_UNAVAILABLE",
    "AnonymousPreviewReuseResolution",
    "resolve_anonymous_preview_reuse",
    "REASON_ANON_NOT_FOUND",
    "REASON_ANON_FORBIDDEN",
    "REASON_ANON_SOURCE_UNAVAILABLE",
]

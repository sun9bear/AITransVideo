"""Express auto-clone 编排入口（spec §6.2，锁死调用顺序）。

**调用顺序（不可乱）**：

    consent gate → main speaker → sample(extract+validate)
      → ★ ATOMIC RESERVE ★  （最终外部调用闸；之后才有 provider 副作用）
      → upload sample → worker clone(provider API) → register-smart → consume

**失败路径（Codex PR2-E）**：

| 失败点 | reservation | provider 副作用清理 | routing |
|---|---|---|---|
| consent/main-speaker/sample | （未 reserve）无 | 无 | 预设 |
| reserve denied/error | 无 | 无（未 upload/worker） | 预设 |
| upload 失败 | **release** | 无 | 预设 |
| worker clone 失败 | **release** | 无（worker 未成功） | 预设 |
| register 失败（worker 已成功）| **release** | best-effort **delete_voice** 孤儿清理 | 预设 |
| consume 失败（已 clone+register）| 留给 TTL sweeper | 无（voice 可用） | **注入**（不浪费已创建 voice）+ audit |
| release 自身失败 | 记 audit + TTL 兜底 | — | — |

**绝不调 MiniMax**：任何失败 ``speaker_voices`` 保持不变 → 下游 voice
matcher 用 CosyVoice 预设音色。

**DI（PR2-E）**：所有外部动作（reserve/upload/clone/register/delete/consume/
release/prepare_sample）通过注入式 ``ExpressAutoCloneClients`` 调用，便于守卫
测试锁死顺序 + 失败分支；具体 HTTP / worker 装配在 PR2-F。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from services.express.audit import emit_express_clone_audit
from services.express.main_speaker import (
    identify_express_main_speaker,
    main_speaker_stats,
    speaker_of,
)

logger = logging.getLogger(__name__)

TARGET_MODEL = "cosyvoice-v3.5-flash"
_RELEASE_FAILED_REASON_CODE = "express_auto_clone_reservation_release_failed"


# --------------------------------------------------------------------------
# 注入式 client 必须返回的结果形状（duck-typed dataclass）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplePrep:
    """``prepare_sample`` 成功返回（extract + validate 通过）。None 表示
    抽样失败 / 时长不足（不占 reservation 名额）。"""

    sample_path: str
    duration_s: float
    segment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    reservation_id: str | None = None
    deny_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    presigned_get_url: str | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CloneResult:
    ok: bool
    voice_id: str | None = None
    worker_request_id: str | None = None
    provider_request_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RegisterResult:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class ExpressAutoCloneClients:
    """注入式依赖集合。PR2-F 用真实现装配；测试用 mock 断言顺序/失败分支。"""

    prepare_sample: Callable[..., "SamplePrep | None"]
    reserve: Callable[..., ReserveResult]
    upload: Callable[..., UploadResult]
    clone: Callable[..., CloneResult]
    register: Callable[..., RegisterResult]
    delete_voice: Callable[..., bool]
    consume: Callable[..., bool]
    release: Callable[..., bool]


@dataclass(frozen=True)
class ExpressAutoCloneOutcome:
    """编排结果。``cloned=True`` 表示已注入 clone 音色 routing；否则回预设。"""

    cloned: bool
    decision: str
    reason_code: str
    voice_id: str | None = None
    reservation_id: str | None = None
    main_speaker_id: str | None = None


def run_express_auto_clone(
    *,
    user_id,
    job_id,
    project_dir,
    transcript_lines,
    speaker_voices: dict,
    speaker_routing: dict,
    express_consent: dict | None,
    clients: ExpressAutoCloneClients,
    target_model: str = TARGET_MODEL,
    min_ratio: float = 0.30,
    min_line_count: int = 5,
    admin_settings_snapshot: dict | None = None,
) -> ExpressAutoCloneOutcome:
    """编排 Express auto-clone（见模块 docstring 的顺序 / 失败表）。

    成功时**原地修改** ``speaker_voices[main_speaker_id]`` = clone voice_id +
    ``speaker_routing[main_speaker_id]`` = worker routing；任何失败保持不变
    （下游回 CosyVoice 预设音色，绝不 MiniMax）。每次调用必写一行决策 audit。
    """
    consent = express_consent or {}
    audit: dict[str, Any] = {
        "job_id": str(job_id),
        "user_id": str(user_id),
        "service_mode": "express",
        "express_consent_server_at": consent.get("server_confirmed_at"),
        "express_consent_client_at": consent.get("client_confirmed_at"),
        "express_consent_parse_error": consent.get("parse_error"),
        "admin_settings_snapshot": admin_settings_snapshot,
    }

    def _finish(decision: str, reason_code: str, *, cloned: bool = False, **extra) -> ExpressAutoCloneOutcome:
        audit["decision"] = decision
        audit["reason_code"] = reason_code
        audit.update(extra)
        emit_express_clone_audit(project_dir, audit)
        return ExpressAutoCloneOutcome(
            cloned=cloned,
            decision=decision,
            reason_code=reason_code,
            voice_id=audit.get("voice_id"),
            reservation_id=audit.get("reservation_id"),
            main_speaker_id=audit.get("main_speaker_id"),
        )

    # === L4 consent gate（防御性；主 gating 在上游 F）===
    if not (consent.get("auto_voice_clone") is True and consent.get("server_confirmed_at")):
        return _finish("skipped", "consent_not_given")

    # === L6 main speaker 识别（纯函数，0 成本）===
    main_speaker_id = identify_express_main_speaker(
        transcript_lines, min_ratio=min_ratio, min_line_count=min_line_count
    )
    if main_speaker_id:
        audit["main_speaker_id"] = main_speaker_id
        stats = main_speaker_stats(transcript_lines, main_speaker_id)
        if stats is not None:
            audit["main_speaker_line_count"] = stats.line_count
            audit["main_speaker_ratio"] = round(stats.ratio, 4)
    else:
        return _finish("skipped", "no_main_speaker")

    # === L7 sample extract + validate（在 reserve 之前；本地 CPU 不占名额）===
    speaker_lines = [ln for ln in (transcript_lines or []) if speaker_of(ln) == main_speaker_id]
    try:
        sample = clients.prepare_sample(speaker_id=main_speaker_id, speaker_lines=speaker_lines)
    except Exception as exc:  # noqa: BLE001 — 抽样异常不该炸 pipeline
        logger.exception("express prepare_sample failed job=%s", job_id)
        return _finish("skipped", "sample_extract_failed", sample_error=type(exc).__name__)
    if sample is None:
        return _finish("skipped", "sample_too_short")
    audit["sample_seconds"] = round(float(sample.duration_s), 2)
    audit["sample_segment_ids"] = list(sample.segment_ids)

    # === ★ ATOMIC RESERVE ★（最终成本闸；失败则停，绝不 upload/worker）===
    reserve_res = clients.reserve(
        user_id=user_id, job_id=job_id, speaker_id=main_speaker_id, target_model=target_model
    )
    if not reserve_res.ok:
        reason = reserve_res.deny_reason or reserve_res.error or "reserve_failed"
        return _finish("skipped", f"reserve_{reason}")
    reservation_id = reserve_res.reservation_id
    if not reservation_id:
        # 200 ok 但无 reservation_id —— 没有可 consume/release 的句柄，看似成功实则
        # 没占到名额。绝不进 upload/worker（Codex E-fix item 2，防越过成本闸）。
        return _finish("skipped", "reserve_malformed_response")
    audit["reservation_id"] = reservation_id

    # 此后持有 reservation —— 任何失败必 release（_safe_release 不静默）。
    def _safe_release(reason: str) -> None:
        try:
            ok = bool(clients.release(reservation_id, reason=reason))
        except Exception as exc:  # noqa: BLE001
            logger.exception("express release raised job=%s reservation=%s", job_id, reservation_id)
            _emit_release_failed(error=type(exc).__name__)
            return
        if not ok:
            _emit_release_failed(error="release_not_ok")

    def _emit_release_failed(*, error: str) -> None:
        # 独立 forensic 行（不替代主决策行）：release 名额泄漏的留痕，TTL sweeper 兜底回收。
        emit_express_clone_audit(
            project_dir,
            {
                "job_id": str(job_id),
                "user_id": str(user_id),
                "service_mode": "express",
                "decision": "reservation_release_failed",
                "reason_code": _RELEASE_FAILED_REASON_CODE,
                "reservation_id": reservation_id,
                "main_speaker_id": main_speaker_id,
                "release_error": error,
            },
        )

    def _orphan_cleanup(voice_id: str | None) -> bool:
        # best-effort delete_voice：清 worker 已创建的孤儿 voice（rollback 自建资源）。
        try:
            ok = bool(clients.delete_voice(voice_id, reason="express_register_failed"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("express delete_voice raised job=%s voice=%s", job_id, voice_id)
            audit["delete_voice_error"] = type(exc).__name__
            return False
        audit["delete_voice_error"] = None if ok else "delete_not_ok"
        return ok

    # === upload sample（PR1-E1 endpoint，F 装配）===
    try:
        upload_res = clients.upload(
            sample_path=sample.sample_path,
            user_id=user_id,
            job_id=job_id,
            speaker_id=main_speaker_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("express upload raised job=%s", job_id)
        _safe_release("upload_failed")
        return _finish("skipped", "upload_failed", upload_error=type(exc).__name__)
    if not upload_res.ok:
        _safe_release("upload_failed")
        return _finish("skipped", "upload_failed", upload_error=upload_res.error)
    if not (upload_res.presigned_get_url and upload_res.sha256):
        # ok 但缺 presigned URL / sha256 —— worker 没有可引用的 sample，必 release
        # 不进 worker（Codex E-fix item 2）。
        _safe_release("upload_malformed_response")
        return _finish("skipped", "upload_malformed_response")

    # === worker clone（付费；CLAUDE.md：不重试，client max_attempts=1）===
    try:
        clone_res = clients.clone(
            sample_url=upload_res.presigned_get_url,
            sample_sha256=upload_res.sha256,
            speaker_id=main_speaker_id,
            job_id=job_id,
            user_id=user_id,
            consent_at=consent.get("server_confirmed_at"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("express worker clone raised job=%s", job_id)
        _safe_release("worker_failed")
        return _finish("skipped", "worker_failed", worker_error=type(exc).__name__)
    if not clone_res.ok:
        _safe_release("worker_failed")
        return _finish("skipped", "worker_failed", worker_error=clone_res.error)
    if not clone_res.voice_id:
        # worker 报成功但没回 voice_id —— 无法 register/consume/routing；也无 id 可
        # delete（无法孤儿清理），只能 release + skip（Codex E-fix item 2）。
        _safe_release("worker_malformed_response")
        return _finish("skipped", "worker_malformed_response")
    audit["voice_id"] = clone_res.voice_id
    audit["worker_request_id"] = clone_res.worker_request_id
    audit["provider_request_id"] = clone_res.provider_request_id

    # === register-smart（落库 user_voices，临时音色）===
    try:
        register_res = clients.register(
            voice_id=clone_res.voice_id,
            speaker_id=main_speaker_id,
            job_id=job_id,
            user_id=user_id,
            sample_sha256=upload_res.sha256,
            target_model=target_model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("express register raised job=%s", job_id)
        register_res = RegisterResult(ok=False, detail=f"exception:{type(exc).__name__}")
    if not register_res.ok:
        # worker 已付费成功但 register 失败 → 孤儿清理 + release（§7.3）
        audit["register_failure_detail"] = (register_res.detail or "")[:200]
        cleanup_ok = _orphan_cleanup(clone_res.voice_id)
        _safe_release("register_failed")
        decision = (
            "register_failed_orphan_cleanup_ok"
            if cleanup_ok
            else "register_failed_orphan_cleanup_failed"
        )
        return _finish(decision, "register_failed", cloned=False)

    try:
        from services.usage_meter import UsageMeter

        UsageMeter(project_dir, job_id=job_id).record_voice_clone(
            provider="cosyvoice_voice_clone",
            model=target_model,
            voice_id=clone_res.voice_id,
            speaker_id=main_speaker_id,
            source_audio_seconds=float(sample.duration_s or 0.0),
            source_audio_bytes=0,
            selected_segment_count=len(sample.segment_ids),
            clone_count=1,
            billable=False,
            success=True,
            extra={
                "billing_policy": "cosyvoice_voice_enrollment_free",
                "service_mode": "express",
                "worker_request_id": clone_res.worker_request_id or "",
                "provider_request_id": clone_res.provider_request_id or "",
                "reservation_id": reservation_id,
            },
        )
    except Exception:
        logger.warning("express clone usage metering skipped job=%s", job_id, exc_info=True)

    # === consume reservation（成功路径）===
    reservation_status_final = "consumed"
    try:
        consumed_ok = bool(clients.consume(reservation_id, voice_id=clone_res.voice_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("express consume raised job=%s reservation=%s", job_id, reservation_id)
        consumed_ok = False
        audit["consume_error"] = type(exc).__name__
    if not consumed_ok:
        # consume 失败：clone 已成功 + voice 已注册可用 → 注入 routing 不浪费付费；
        # 但**不静默成功**——audit 标记 + TTL sweeper 回收 reservation 名额。
        reservation_status_final = "reserved"
        audit["consume_status"] = "failed"
        logger.warning(
            "express consume not ok job=%s reservation=%s (voice usable; TTL will reclaim slot)",
            job_id,
            reservation_id,
        )

    # === routing 注入（成功；原地改 caller 的 dict）===
    speaker_voices[main_speaker_id] = clone_res.voice_id
    speaker_routing[main_speaker_id] = {
        "requires_worker": True,
        "worker_target_model": target_model,
    }
    audit["is_temporary"] = True
    decision = "cloned" if reservation_status_final == "consumed" else "cloned_consume_failed"
    reason_code = "cloned" if reservation_status_final == "consumed" else "cloned_consume_failed"
    return _finish(
        decision,
        reason_code,
        cloned=True,
        reservation_status_final=reservation_status_final,
    )


__all__ = [
    "SamplePrep",
    "ReserveResult",
    "UploadResult",
    "CloneResult",
    "RegisterResult",
    "ExpressAutoCloneClients",
    "ExpressAutoCloneOutcome",
    "run_express_auto_clone",
    "TARGET_MODEL",
]

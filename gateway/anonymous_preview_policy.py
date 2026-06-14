"""Gateway 匿名预览薄 adapter — 契约调用翻译层。

本模块是 ``src.services.anonymous_preview_admission`` 契约的**薄 adapter**，
禁止新增任何决策规则（方案 C5）。模块只做：

1. ``admit_for_free_preview``：从 settings 构造 ``AnonymousPreviewAdmissionConfig``，
   调用契约入口 ``evaluate_anonymous_preview_admission``，把返回值逐字段透传为
   gateway 侧消费的轻量 dict。
2. ``stream_gate_from_artifact_policy``：把 ``AnonymousPreviewArtifactPolicy``
   逐字段翻译成 stream 端点 gate 需要的约束元组，只透传契约值，不添加任何默认。

所有决策值（decision / duration / voice_strategy / artifact_policy 字段）均来自
契约返回值，**不得在本模块写死或覆盖任何决策常量**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, FrozenSet, NamedTuple

from services.anonymous_preview_admission import (
    AdmissionDecision,
    AnonymousPreviewAdmissionConfig,
    AnonymousPreviewArtifactPolicy,
    VoiceStrategy,
    evaluate_anonymous_preview_admission,
)

if TYPE_CHECKING:
    from gateway.config import Settings


# ---------------------------------------------------------------------------
# Output types — plain containers, no decision logic.
# ---------------------------------------------------------------------------


class FreePreviewAdmissionResult(NamedTuple):
    """Logically flat view of an ``AnonymousPreviewAdmission`` for gateway consumers.

    Fields are **exactly** the admission contract fields, renamed only where
    required for the gateway domain (none here — all names match). Any field
    added here that is NOT present in ``AnonymousPreviewAdmission`` is a C5
    violation and must be caught by the parity guard in the test suite.
    """

    decision: AdmissionDecision
    preview_duration_seconds: float
    voice_strategy: VoiceStrategy
    artifact_policy: AnonymousPreviewArtifactPolicy
    reason: str


class StreamGate(NamedTuple):
    """Gate constraints derived from ``AnonymousPreviewArtifactPolicy`` for the
    stream endpoint.  All values are **copied verbatim** from the policy — no
    defaults, no overrides.
    """

    stream_only_required: bool
    watermark_required: bool
    artifact_ttl_required: bool
    low_priority_required: bool
    # Frozenset of artifact key names that must NOT be served as downloads.
    # Derived from the four ``allow_*`` download flags on the policy; each
    # ``False`` flag contributes its corresponding logical key name.
    download_forbidden_keys: FrozenSet[str]


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def admit_for_free_preview(
    teaser_duration_seconds: object,
    settings: "Settings",
    *,
    mode: str = "free",
    express_clone_enabled: bool = False,
) -> FreePreviewAdmissionResult:
    """Translate a gateway upload request into an admission contract call.

    Constructs ``AnonymousPreviewAdmissionConfig`` entirely from *settings*
    fields — no numeric literals appear in this module. Returns a
    ``FreePreviewAdmissionResult`` whose fields are the contract result fields
    copied verbatim; no field is defaulted, overridden, or omitted here.

    plan 2026-06-14 §3.3（CosyVoice 克隆放行）：``mode`` / ``express_clone_enabled``
    让 adapter 把真实档位 + admin 主开关 ``anonymous_express_cosyvoice_clone_enabled``
    传给契约，使 admission 的 ``voice_strategy`` **诚实反映** express 克隆 gate
    （flag 开 → ``EXPRESS_TEMPORARY_CLONE_GATE``）。**注意**：admission 的
    voice_strategy 是契约信号，**当前不被 create 消费**（create 的 payload
    voice_strategy 恒 preset_mapping，防线② 不动）；真正的匿名/快捷 CosyVoice 克隆
    触发由 **pipeline** ``services.express.maybe_run_express_auto_clone`` 独立
    gating（admin 主开关 + consent + 全局 cap + worker + reservation）。本参数仅
    让契约诚实，不改运行时克隆行为。默认 ``mode="free"`` / ``express_clone_enabled
    =False`` 保持旧调用方字节级不变。

    Args:
        teaser_duration_seconds: Probed duration of the teaser clip. Passed
            directly to the contract validator (accepts int/float, rejects
            bool/NaN/inf/negative).
        settings: Gateway ``Settings`` instance（读 ``anonymous_preview_max_seconds``）。
        mode: 档位（"free" / "express"）。非 express 时 clone flag 强制 False。
        express_clone_enabled: admin ``anonymous_express_cosyvoice_clone_enabled``
            的真实值（caller 从 admin_settings 读后传入）。

    Returns:
        ``FreePreviewAdmissionResult`` with fields taken verbatim from
        ``AnonymousPreviewAdmission``.
    """
    config = AnonymousPreviewAdmissionConfig(
        max_preview_duration_seconds=settings.anonymous_preview_max_seconds,
        # 只有 express 档才把 admin 主开关传进契约；其它档（free 等）强制 False，
        # 契约据此 emit EXPRESS_TEMPORARY_CLONE_GATE 或 PRESET_ONLY（仅契约信号）。
        anonymous_express_cosyvoice_clone_enabled=(
            bool(express_clone_enabled) if str(mode) == "express" else False
        ),
    )

    admission = evaluate_anonymous_preview_admission(
        config=config,
        mode=mode,
        source_duration_seconds=teaser_duration_seconds,
    )

    # Verbatim field copy — no logic, no defaults.
    return FreePreviewAdmissionResult(
        decision=admission.decision,
        preview_duration_seconds=admission.preview_duration_seconds,
        voice_strategy=admission.voice_strategy,
        artifact_policy=admission.artifact_policy,
        reason=admission.reason,
    )


def stream_gate_from_artifact_policy(
    policy: AnonymousPreviewArtifactPolicy,
) -> StreamGate:
    """Translate ``AnonymousPreviewArtifactPolicy`` into stream endpoint gate inputs.

    Every field in the returned ``StreamGate`` is copied verbatim from *policy*.
    The ``download_forbidden_keys`` frozenset is the logical inverse of the four
    ``allow_*`` download-permission flags; each ``False`` flag contributes its
    corresponding artifact key name so the stream endpoint can reject download
    attempts without duplicating policy logic.

    No magic numbers, no overrides, no defaults — the contract owns all values.
    """
    forbidden: list[str] = []
    if not policy.allow_download_url:
        forbidden.append("download_url")
    if not policy.allow_subtitle_export:
        forbidden.append("subtitle_export")
    if not policy.allow_jianying_draft_export:
        forbidden.append("jianying_draft_export")
    if not policy.allow_provider_voice_id:
        forbidden.append("provider_voice_id")
    if not policy.allow_clone_artifact:
        forbidden.append("clone_artifact")
    if not policy.allow_payment_fields:
        forbidden.append("payment_fields")
    if not policy.allow_editable_assets:
        forbidden.append("editable_assets")

    return StreamGate(
        stream_only_required=policy.stream_only_required,
        watermark_required=policy.watermark_required,
        artifact_ttl_required=policy.artifact_ttl_required,
        low_priority_required=policy.low_priority_required,
        download_forbidden_keys=frozenset(forbidden),
    )


__all__ = [
    "FreePreviewAdmissionResult",
    "StreamGate",
    "admit_for_free_preview",
    "stream_gate_from_artifact_policy",
]

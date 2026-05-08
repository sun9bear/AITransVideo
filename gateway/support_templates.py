"""Deterministic template router for the customer support flow.

Plan §5.0 — "user message → router → FAQ/template/job-error → LLM → handoff".
This module is the deterministic part of that chain; anything matched here
short-circuits the LLM call entirely.

Three classes of templates:

1. **Sensitive keyword routing** — refunds, complaints, regulator threats,
   privacy / copyright disputes. Always escalate to human, never let the
   LLM speak first. Keywords are admin-configurable; the defaults below
   reflect the plan §5.3 list.
2. **High-frequency Q&A templates** — short, deterministic answers for the
   five most common questions. Plan §5.0 mandates this.
3. **Job error code templates** — when a logged-in user asks "why did my
   task fail?", the resolved ``error_category`` from
   ``sanitize_job_context_for_ai`` decides the canned response.

A non-match returns ``None``; ``support_service`` falls through to the
LLM path (if budget permits) or, ultimately, handoff.

Templates are Python dicts (not DB rows). Plan §16.5 / §7.2 explicitly
keeps this in code for P1 — admin editing is P2+.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateMatch:
    """Result of a deterministic template hit.

    ``handoff_recommended`` is the structured equivalent of "this template
    wants the conversation escalated, not just answered." Sensitive keyword
    matches always set this True.
    """

    template_id: str
    reply: str
    category: str
    confidence: float
    handoff_recommended: bool = False
    handoff_required: bool = False
    handoff_reason: str | None = None


# ---------------------------------------------------------------------------
# Sensitive keyword router (plan §5.3)
# ---------------------------------------------------------------------------


# Default Chinese keyword list. The exact set is admin-overridable via
# AdminSettings.support_sensitive_keywords; this constant only seeds the
# initial list and is what unit tests assert against.
DEFAULT_SENSITIVE_KEYWORDS: list[str] = [
    "人工",
    "真人",
    "转客服",
    "找人",
    "退款",
    "重复扣费",
    "套餐未到账",
    "发票",
    "投诉",
    "差评",
    "工信部",
    "315",
    "赔偿",
    "举报",
    "律师",
    "消协",
    "侵权",
    "版权",
    "隐私删除",
    "账号被盗",
]


HUMAN_REQUEST_KEYWORDS: list[str] = [
    "人工",
    "真人",
    "转客服",
    "找人",
    "别让 AI 回答",
    "不要 AI",
]


# English fallback for non-Chinese visitors. Plan §5.4 says do NOT let the
# LLM auto-switch language — return a fixed English banner instead.
ENGLISH_FALLBACK_REPLY = (
    "Our AI support is currently optimized for Chinese conversations. "
    "For pre-sales or technical questions in English, please email "
    "{ops_email}. We typically reply within 1 business day."
)


def looks_english_only(text: str) -> bool:
    """Quick heuristic: does the input contain almost no CJK?

    Used to gate the English fallback. We do NOT want to flip a Chinese
    speaker who happened to write a single English word. The threshold:
    if there are ASCII letters and < 2 CJK characters, treat as English.
    """
    if not text:
        return False
    has_ascii_letter = bool(re.search(r"[A-Za-z]", text))
    cjk_count = len(re.findall(r"[一-鿿]", text))
    return has_ascii_letter and cjk_count < 2


def detect_sensitive_keyword(
    message: str,
    *,
    keywords: Iterable[str] | None = None,
) -> str | None:
    """Return the first matched keyword (or None).

    Match is plain substring on the lowercased message. Keywords are short
    Chinese phrases so a substring match is more correct than a token match
    here (token boundaries are ambiguous in CN).
    """
    if not message:
        return None
    lowered = message.lower()
    candidates = list(keywords) if keywords is not None else DEFAULT_SENSITIVE_KEYWORDS
    for kw in candidates:
        if not kw:
            continue
        if kw.lower() in lowered:
            return kw
    return None


def classify_handoff_reason(matched_keyword: str) -> str:
    """Map a matched keyword to a structured handoff reason.

    Drives both ``support_messages.metadata`` and the
    ``support_handoff_requests.reason`` column for later analytics.
    """
    if matched_keyword in HUMAN_REQUEST_KEYWORDS:
        return "user_requested_human"
    if matched_keyword in {"投诉", "差评", "工信部", "315", "举报", "律师", "消协"}:
        return "abuse_review"
    if matched_keyword in {
        "退款",
        "重复扣费",
        "套餐未到账",
        "发票",
        "侵权",
        "版权",
        "隐私删除",
        "账号被盗",
    }:
        return "policy_required"
    return "sensitive_category"


# ---------------------------------------------------------------------------
# High-frequency Q&A templates (plan §5.0 — at least 5)
# ---------------------------------------------------------------------------


_HIGH_FREQUENCY_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "tpl_trial_auto_charge",
        "trigger_tokens": ["试用", "扣费", "续费", "自动收费"],
        "category": "trial",
        "reply": (
            "试用结束后不会自动扣费。\n\n"
            "1. 试用结束后会自动回到 Free 套餐，按免费额度使用。\n"
            "2. 想继续使用更多额度，可以在「定价」页面主动升级到 Plus 或 Pro。\n"
            "3. 没有任何隐藏续费或自动签约。"
        ),
    },
    {
        "id": "tpl_jianying_draft_export",
        "trigger_tokens": ["剪映", "草稿", "导出", "继续编辑", "精剪"],
        "category": "jianying_draft",
        "reply": (
            "剪映草稿可以在任务结果页下载。\n\n"
            "1. 任务完成后进入工作台 → 任务详情 → 结果页。\n"
            "2. 找到「剪映草稿」下载项，下载并解压。\n"
            "3. 用剪映 App 打开后即可继续编辑字幕、配音和素材轨道。\n\n"
            "提示：如果你刚刚修改过字幕或配音，建议重新生成一次草稿，"
            "避免下载到旧版本。"
        ),
    },
    {
        "id": "tpl_express_vs_studio",
        "trigger_tokens": ["express", "studio", "区别", "模式", "工作室", "快捷版"],
        "category": "service_mode",
        "reply": (
            "Express 和 Studio 的区别：\n\n"
            "- Express：自动跑完整条流程，速度快、成本低，适合先验证效果或批量出片。\n"
            "- Studio：可逐句复核译文、选择音色，质量更稳，适合发布或交付。\n"
            "- 在合法授权前提下，Studio 还支持声音克隆。\n\n"
            "如果你是第一次试用，建议先用 Express 跑一条样片确认效果。"
        ),
    },
    {
        "id": "tpl_job_failure_basics",
        "trigger_tokens": ["任务失败", "处理失败", "出错", "报错", "为什么失败"],
        "category": "job_failure",
        "reply": (
            "任务失败的常见原因和排查步骤：\n\n"
            "1. 视频源链接是否仍然可访问，YouTube 链接是否需要登录。\n"
            "2. 上传的本地视频是否完整未损坏，文件大小是否超过套餐限制。\n"
            "3. 在任务详情页查看「失败原因」摘要，是否提示需要重试。\n"
            "4. 如果失败发生在配音阶段，可以在 Studio 中重新生成对应片段。\n\n"
            "失败的任务不会扣除额度。如果重试仍失败，请把任务 ID 发给客服。"
        ),
    },
    {
        "id": "tpl_modify_one_segment",
        "trigger_tokens": ["改一句", "重新生成", "增量", "单段", "修改"],
        "category": "incremental_regen",
        "reply": (
            "修改一句不需要重新生成整条视频。\n\n"
            "1. 进入工作台 → 任务详情 → 编辑模式。\n"
            "2. 找到要改的句子，修改译文或重新选择配音音色。\n"
            "3. 点击该段的「重新生成」，系统只会处理这一段。\n"
            "4. 全部满意后再点「保存修改」，会自动重新拼接视频。\n\n"
            "增量重生成不会重复合成全片，节省时间也节省成本。"
        ),
    },
]


def match_high_frequency_template(message: str) -> TemplateMatch | None:
    """Return the first high-frequency template that matches.

    Match is "any trigger token appears in the lowercased message". A
    weighted scorer is intentionally not used — plan §6.3 favors
    traceability over recall, and these five templates already cover
    distinct topics so collisions are rare.
    """
    if not message:
        return None
    lowered = message.lower()
    for tpl in _HIGH_FREQUENCY_TEMPLATES:
        for token in tpl["trigger_tokens"]:
            if token.lower() in lowered:
                return TemplateMatch(
                    template_id=tpl["id"],
                    reply=tpl["reply"],
                    category=tpl["category"],
                    confidence=0.9,
                )
    return None


# ---------------------------------------------------------------------------
# Job error code templates
# ---------------------------------------------------------------------------


_JOB_ERROR_TEMPLATES: dict[str, str] = {
    "source_unavailable": (
        "任务在拉取源视频阶段失败。可能原因：\n\n"
        "1. YouTube 链接被原作者下架或区域限制。\n"
        "2. 上传的本地视频文件损坏。\n"
        "3. 网络拉取超时。\n\n"
        "建议：换一个可访问的链接重新提交，失败任务不会扣除额度。"
    ),
    "transcription_failed": (
        "音频转写阶段失败。可能原因：\n\n"
        "1. 视频中没有清晰的人声或语速极快。\n"
        "2. 多人同时讲话且没有明显说话人区分。\n\n"
        "建议：可以尝试在 Studio 模式手动指定说话人数后重试。"
    ),
    "translation_failed": (
        "翻译阶段失败。可能原因：\n\n"
        "1. 上游 LLM 临时不可用。\n"
        "2. 文本中含有触发内容审核的片段。\n\n"
        "建议：稍后重试。如果反复失败，请把任务 ID 发给客服排查。"
    ),
    "tts_failed": (
        "配音合成失败。可能原因：\n\n"
        "1. 选择的音色配额已用完。\n"
        "2. 文本过长触发单次合成上限。\n\n"
        "建议：在 Studio 中切换其他音色，或对超长片段拆分后重新生成。"
    ),
    "alignment_failed": (
        "音视频对齐阶段失败。这通常是临时性问题。\n\n"
        "建议：在任务详情页点击「重新对齐」即可，不会重复扣除额度。"
    ),
    "publish_failed": (
        "成片发布阶段失败。可能原因：\n\n"
        "1. 临时存储问题，重试通常能恢复。\n"
        "2. 上传到 R2 时网络异常。\n\n"
        "建议：稍后重试；如反复失败请联系客服。"
    ),
}


def match_job_error_template(error_category: str | None) -> TemplateMatch | None:
    """Return a canned response for a known error category."""
    if not error_category:
        return None
    reply = _JOB_ERROR_TEMPLATES.get(error_category)
    if reply is None:
        return None
    return TemplateMatch(
        template_id=f"tpl_err_{error_category}",
        reply=reply,
        category=f"job_error:{error_category}",
        confidence=0.85,
    )


# ---------------------------------------------------------------------------
# Composite entrypoint
# ---------------------------------------------------------------------------


def route_message(
    message: str,
    *,
    error_category: str | None = None,
    sensitive_keywords: Iterable[str] | None = None,
) -> TemplateMatch | None:
    """Top-level deterministic router.

    Order matters:
    1. Sensitive keyword (always escalate first).
    2. Job error code (if a current job has a known failure category).
    3. High-frequency Q&A.
    """
    matched_kw = detect_sensitive_keyword(message, keywords=sensitive_keywords)
    if matched_kw is not None:
        reason = classify_handoff_reason(matched_kw)
        # We still return a brief, user-facing reply. The caller
        # (support_service) will create the handoff and append a
        # "we'll route you to a human" suffix.
        if reason == "user_requested_human":
            reply = (
                "好的，正在为你转接人工客服。\n"
                "请稍等，运营会通过站内消息或邮件回复你。"
            )
        elif reason == "abuse_review":
            reply = (
                "我已记录你的反馈。这类问题会由人工客服直接跟进，"
                "请留意运营的回复邮件。"
            )
        elif reason == "policy_required":
            reply = (
                "这个问题涉及账单 / 退款 / 版权 / 隐私等政策性事项，"
                "AI 客服不直接处理。我已为你创建人工工单，"
                "运营会通过邮件回复你。"
            )
        else:
            reply = (
                "你的反馈我已记录，会由人工客服跟进，请留意邮件回复。"
            )
        return TemplateMatch(
            template_id=f"tpl_sensitive_{reason}",
            reply=reply,
            category=f"sensitive:{reason}",
            confidence=1.0,
            handoff_recommended=True,
            handoff_required=(reason != "user_requested_human"),
            handoff_reason=reason,
        )

    job_err = match_job_error_template(error_category)
    if job_err is not None:
        return job_err

    return match_high_frequency_template(message)

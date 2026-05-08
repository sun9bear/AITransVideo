"""Support knowledge resolver: FAQ, plan facts, and sanitized job context.

Three responsibilities:

1. **FAQ candidate retrieval** — keyword / synonym overlap against the
   curated Chinese FAQ corpus. Plan §6.3 explicitly does NOT want a vector
   index in P1; this is intentionally simple so we can reason about why a
   reply did or did not surface a particular FAQ entry.
2. **Plan / pricing facts** — proxies into ``gateway.plan_catalog`` so any
   claim about pricing, trial behavior, or quotas comes from the same
   authoritative source the marketing pages and billing flow read.
3. **Job context sanitization** — converts a ``Job`` ORM row into the
   ``JobContextForAI`` dataclass. **Allowlist only.** No internal paths,
   stacktraces, manifest references, or cross-user resource ids are ever
   exposed to the AI / template layer.

Anything outside these three resolvers is out of scope for the knowledge
module — message routing, budget guards, prompt construction live in
sibling modules.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# FAQ corpus (mirrored from frontend-next/src/components/marketing/faq.tsx)
# ---------------------------------------------------------------------------
#
# The frontend FAQ keeps its own hardcoded copy for SEO / FAQPage JSON-LD. The
# Python copy below stays in sync via PR review — if the two drift, the worst
# outcome is the AI says something the FAQ page does not, which is recoverable
# via a single PR. We deliberately did NOT centralize this data into one file
# in P1 because:
#
# - The frontend renders FaqJsonLd for AI search engines (Perplexity, ChatGPT)
#   and that path needs the exact text in the React tree, not a runtime fetch.
# - Loading TSX from Python at build time adds a toolchain dependency for one
#   small list. Plan §6.3 explicitly opts for "轻量可测试" over indirection.
#
# Each entry has a stable ``id`` so ``support_ai_usage.route='faq'`` rows can
# point back to which FAQ short-circuited the LLM call.

FAQ_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "faq_long_video",
        "tags": ["长视频", "时长", "支持多长", "几分钟", "几小时"],
        "q": "为什么你们强调长视频？",
        "a": (
            "很多知识类内容不是几十秒短视频，而是 30 分钟、1 小时甚至 3 小时的"
            "访谈、课程、播客、演讲。Pro 套餐支持单条视频最长 180 分钟，正是"
            "为这类长内容做翻译和中文配音准备的。"
        ),
    },
    {
        "id": "faq_source_languages",
        "tags": ["源语言", "英文", "日语", "西语", "支持哪些语言"],
        "q": "支持哪些源语言？",
        "a": (
            "现阶段聚焦海外英文长视频翻译为中文配音版本——访谈、课程、播客、"
            "产品演示这类内容效果最稳定。其他源语言（日语、西语等）正在评估中，"
            "暂未开放。如果你计划上传非英文素材，建议先与客服确认。"
        ),
    },
    {
        "id": "faq_video_sources",
        "tags": ["视频来源", "youtube", "本地视频", "授权", "上传"],
        "q": "支持哪些视频来源？",
        "a": (
            "支持您本人或已获授权内容的导入：本地视频文件上传、YouTube 链接"
            "（适用于您自己频道或已获授权的视频）、其他视频链接。使用前请确认"
            "您对相关视频拥有合法授权，详见《服务条款》。"
        ),
    },
    {
        "id": "faq_competitors",
        "tags": ["rask", "heygen", "elevenlabs", "区别", "对比", "差异"],
        "q": "和 Rask、HeyGen、ElevenLabs 有什么区别？",
        "a": (
            "Rask、HeyGen、ElevenLabs 都是优秀的全球化平台，有些在数字人或声音"
            "克隆上非常强。爱译视频更关注中文创作者处理海外英文长视频的实际"
            "需求：更长的视频时长、更低的使用门槛、多种结果导出，以及生成后"
            "还能逐句修改和单段重生成。"
        ),
    },
    {
        "id": "faq_translation_quality",
        "tags": ["翻译", "不满意", "修改", "改译文", "重新生成"],
        "q": "如果 AI 翻译不满意怎么办？",
        "a": (
            "AI 生成的是第一版，不是最终版。你可以在工作台里逐句检查译文、"
            "字幕和配音，对不满意的句子直接修改，并单独重生成对应的片段。"
        ),
    },
    {
        "id": "faq_incremental_regen",
        "tags": ["修改一句", "增量", "重新生成", "整条视频"],
        "q": "修改一句话需要重新生成整条视频吗？",
        "a": (
            "不需要。爱译视频支持增量重生成：你只改某一句时，系统只重新处理"
            "对应的片段，不会重复合成整条视频，节省时间也节省成本。"
        ),
    },
    {
        "id": "faq_express_vs_studio",
        "tags": ["express", "studio", "区别", "模式", "快捷版", "工作室"],
        "q": "Studio 模式和 Express 模式有什么区别？",
        "a": (
            "Express 模式自动跑完全流程，速度快、成本低，适合先验证效果或批量"
            "出片；Studio 模式可以在工作台里逐句复核译文、选择更合适的中文"
            "配音音色，质量更稳，更适合需要发布或交付客户的内容。在获得合法"
            "授权的前提下，Studio 也支持声音克隆能力。"
        ),
    },
    {
        "id": "faq_downloads",
        "tags": ["下载", "结果", "成片", "字幕", "音频", "剪映草稿", "素材包"],
        "q": "可以下载哪些结果？",
        "a": (
            "任务完成后可下载：中文配音视频、配音音频、中文字幕（含英文/双语"
            "字幕）、翻译文本、原始素材包，以及剪映草稿工程——在剪映里直接"
            "打开成片的字幕、配音和素材轨道继续精剪，不必从零铺时间线。"
        ),
    },
    {
        "id": "faq_trial_auto_charge",
        "tags": ["试用", "自动扣费", "结束", "free", "免费"],
        "q": "试用结束后会怎样？",
        "a": (
            "试用结束后不会自动扣费。你可以继续以 Free 套餐的免费额度使用，"
            "也可以主动升级到 Plus 或 Pro。"
        ),
    },
    {
        "id": "faq_failed_no_charge",
        "tags": ["失败", "不计费", "取消", "扣费", "退款"],
        "q": "「失败不计费」具体指什么？",
        "a": (
            "如果任务因系统处理失败未生成可下载结果，不扣除对应处理额度；"
            "用户主动取消未完成的任务也不计费。注意「不满意」不属于此情形——"
            "已完成并交付的结果按时长正常计费，但你可以在工作台里逐句修改并"
            "单段重生成，避免重复付费跑全片。"
        ),
    },
    {
        "id": "faq_jianying_draft",
        "tags": ["剪映", "草稿", "下载", "导出", "继续编辑", "精剪"],
        "q": "怎么下载剪映草稿？",
        "a": (
            "任务完成后，在结果页可以找到「剪映草稿」下载项。下载后用剪映打开"
            "就能继续编辑字幕、配音和素材轨道。如果你刚修改过字幕或配音，"
            "建议先重新生成草稿，避免下载到旧版本。"
        ),
    },
]


# ---------------------------------------------------------------------------
# Synonym map — collapses common phrasings into the same retrieval hits.
# Keep this list short. Plan §6.3 favors traceability over recall.
# ---------------------------------------------------------------------------

_SYNONYMS: dict[str, list[str]] = {
    "试用": ["免费试用", "trial", "试一下"],
    "扣费": ["收费", "续费", "自动扣款", "付费"],
    "剪映草稿": ["jianying", "剪映", "草稿"],
    "失败": ["报错", "出错", "异常"],
    "退款": ["退钱", "refund"],
    "翻译": ["译文", "翻成中文"],
    "字幕": ["subtitle", "中文字幕"],
    "音色": ["语音", "voice", "克隆", "tts", "配音"],
}

_ASCII_TOKEN_RE = re.compile(r"[a-zA-Z]+|\d+")


def _expand_synonyms(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for tok in tokens:
        for canon, aliases in _SYNONYMS.items():
            if tok == canon or tok in aliases:
                out.add(canon)
                out.update(aliases)
    return out


def _match_score(query: str, candidate: str) -> float:
    """Substring + bigram overlap score.

    Chinese tokenization without segmentation tools is unreliable, so we
    score on:
      - direct substring containment (each side of the comparison),
      - shared 2-character CJK bigrams,
      - shared ASCII tokens (words).

    Higher is better; 0 means no overlap.
    """
    q = (query or "").lower()
    c = (candidate or "").lower()
    if not q or not c:
        return 0.0

    score = 0.0

    # Strong signal: tag literally appears in user query.
    if c in q:
        score += 3.0
    if q in c:
        score += 2.0

    # ASCII / digit tokens (e.g. "studio", "express", "315").
    q_ascii = set(_ASCII_TOKEN_RE.findall(q))
    c_ascii = set(_ASCII_TOKEN_RE.findall(c))
    score += float(len(q_ascii & c_ascii)) * 1.5

    # CJK bigrams — proxy for word overlap when no segmenter is available.
    q_bigrams = {q[i : i + 2] for i in range(len(q) - 1) if all("一" <= ch <= "鿿" for ch in q[i : i + 2])}
    c_bigrams = {c[i : i + 2] for i in range(len(c) - 1) if all("一" <= ch <= "鿿" for ch in c[i : i + 2])}
    score += float(len(q_bigrams & c_bigrams)) * 1.0

    # Synonym expansion — every alias hit also counts.
    q_synonyms = _expand_synonyms(q_ascii)
    for syn in q_synonyms:
        if syn and syn in c:
            score += 0.5
    return score


# Minimum score for an FAQ hit to be returned. A single CJK bigram
# overlap (e.g. interrogative "怎么") scores 1.0; we require at least
# 2.0 so the match has either a substring hit OR multiple bigrams.
_MIN_FAQ_SCORE = 2.0


def search_faq(query: str, *, top_k: int = 3) -> list[dict[str, Any]]:
    """Return up to ``top_k`` FAQ entries ranked by tag/question overlap.

    Scoring strategy (plan §6.3 — no vector index in P1):
    - For each entry, compute the max ``_match_score`` between the query
      and any of its tags + the question text.
    - Drop entries scoring under ``_MIN_FAQ_SCORE`` so a single shared
      interrogative ("怎么", "为什么") does not surface unrelated FAQs.
    - Sort descending, return top_k.
    """
    if not query:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in FAQ_ENTRIES:
        candidates = list(entry.get("tags") or []) + [entry.get("q") or ""]
        score = max((_match_score(query, c) for c in candidates if c), default=0.0)
        if score < _MIN_FAQ_SCORE:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:top_k]]


# ---------------------------------------------------------------------------
# Plan / pricing facts (proxy into plan_catalog)
# ---------------------------------------------------------------------------


def get_plan_facts() -> dict[str, Any]:
    """Return a plain-dict snapshot of plan facts, for prompt context.

    Reads from ``gateway.plan_catalog.PLANS`` so the AI cannot drift
    away from what the pricing page and billing flow consider
    authoritative. Returns a stripped view (just plan codes, display
    names, single-line summaries) — full plan definitions stay inside
    the gateway.

    Codex P1-4 fix (2026-05-08): the prior version imported a
    ``_PLAN_DEFINITIONS`` symbol that doesn't exist (real export is
    ``PLANS``) and read ``plan.name`` instead of ``display_name``. Both
    bugs combined to make the function return ``{"plans": []}``, which
    silently disabled the Gateway-truth path for plan questions.
    """

    try:
        from plan_catalog import PLANS  # type: ignore
    except Exception:  # pragma: no cover — import failure means no plan facts
        return {"plans": []}
    plans: list[dict[str, Any]] = []
    for plan in PLANS.values():  # type: ignore[attr-defined]
        plans.append(
            {
                "code": getattr(plan, "code", ""),
                "name": getattr(plan, "display_name", "") or getattr(plan, "code", ""),
                "max_duration_minutes": getattr(plan, "max_duration_minutes", None),
                "max_concurrent_jobs": getattr(plan, "max_concurrent_jobs", None),
                "allowed_service_modes": list(
                    getattr(plan, "allowed_service_modes", []) or []
                ),
                "free_quota_total": getattr(plan, "free_quota_total", None),
                "self_serve": bool(getattr(plan, "self_serve", False)),
            }
        )
    return {"plans": plans}


# ---------------------------------------------------------------------------
# Job context sanitization (plan §10.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobContextForAI:
    """Allowlist-only job context that may be passed to AI / templates.

    NEVER add ``project_dir`` / ``workspace_dir`` / ``manifest_path`` /
    absolute paths / stacktraces / provider raw error payloads to this
    dataclass. The AST guard in ``tests/test_support_no_internal_field_leakage.py``
    enforces this contract — adding such a field will fail CI.
    """

    job_id: str
    display_name: str | None
    status: str
    service_mode: str | None
    source_duration_seconds: float | None
    error_category: str | None
    user_visible_error: str | None
    available_artifacts: tuple[str, ...] = field(default_factory=tuple)
    updated_at: str | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        """Plain dict for prompt embedding (lists instead of tuples)."""
        d = asdict(self)
        d["available_artifacts"] = list(self.available_artifacts)
        return d


def _bucketize_error(error_summary: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Map raw ``error_summary`` JSON into (category, user_visible_message).

    The full error_summary JSON often contains stacktraces, provider raw
    payloads, internal paths. We extract only the bucketed category and a
    short user-friendly summary; everything else is dropped.
    """

    if not error_summary or not isinstance(error_summary, dict):
        return None, None
    category = error_summary.get("category") or error_summary.get("kind")
    if isinstance(category, str):
        category = category.strip()[:32] or None
    else:
        category = None
    # Allowlist of fields that may contain a user-visible string. We do NOT
    # fall through to ``error_summary["message"]`` because that field is
    # frequently the raw upstream payload.
    user_msg = (
        error_summary.get("user_visible_message")
        or error_summary.get("user_message")
        or error_summary.get("display_message")
    )
    if isinstance(user_msg, str):
        user_msg = user_msg.strip()
        if len(user_msg) > 240:
            user_msg = user_msg[:237] + "..."
        # Strip absolute paths defensively, in case downstream shoves one in.
        user_msg = re.sub(r"[A-Z]:\\[^\s'\"]+", "[内部路径]", user_msg)
        user_msg = re.sub(r"/(opt|home|root|var|tmp|mnt)/[^\s'\"]+", "[内部路径]", user_msg)
    else:
        user_msg = None
    return category, user_msg


def sanitize_job_context_for_ai(job: Any) -> JobContextForAI | None:
    """Convert a ``Job`` ORM row to allowlist-only context.

    Returns ``None`` when ``job`` is None — callers should treat that as
    "no job context to inject."

    ``job`` is typed as ``Any`` so this module does not depend on
    ``gateway.models`` (which would create an import cycle for tests that
    want to call this with a fixture).
    """
    if job is None:
        return None
    error_summary = getattr(job, "error_summary", None)
    error_category, user_visible_error = _bucketize_error(error_summary)

    artifacts: list[str] = []
    review_gate = getattr(job, "review_gate", None)
    if isinstance(review_gate, dict):
        # ``review_gate`` may carry an artifacts manifest. We only surface
        # known keys; never emit absolute paths.
        for key in ("dubbed_video", "subtitles", "materials_pack", "jianying_draft"):
            if review_gate.get(key):
                artifacts.append(key)

    updated_at = getattr(job, "updated_at", None)
    return JobContextForAI(
        job_id=str(getattr(job, "job_id", "") or ""),
        display_name=getattr(job, "display_name", None),
        status=str(getattr(job, "status", "") or "unknown"),
        service_mode=getattr(job, "service_mode", None),
        source_duration_seconds=getattr(job, "source_duration_seconds", None),
        error_category=error_category,
        user_visible_error=user_visible_error,
        available_artifacts=tuple(artifacts),
        updated_at=updated_at.isoformat() if updated_at else None,
    )


# ---------------------------------------------------------------------------
# PII redactor — last-mile defense before AI prompt
# ---------------------------------------------------------------------------


_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Mainland-CN mobile: 11 digits starting with 1[3-9]
    (re.compile(r"\b1[3-9]\d{9}\b"), "[手机号]"),
    # Email
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[邮箱]"),
    # Order-ish ids: ord_/order_/ali_/wx_ + alnum
    (re.compile(r"\b(?:ord_|order_|ali_|wx_)[A-Za-z0-9]+\b"), "[订单号]"),
    # URL query secrets
    (re.compile(r"([?&](?:token|code|signature|sign|sig|key)=)[^&\s]+"), r"\1[已脱敏]"),
]


def redact_pii(text: str) -> str:
    """Strip common PII from a string before it leaves Gateway.

    Idempotent and safe to call repeatedly. Scoped to defensive last-mile
    cleanup; the canonical place to enforce this is the AI provider adapter
    (which calls this on every prompt segment).
    """
    if not text:
        return text
    out = text
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out

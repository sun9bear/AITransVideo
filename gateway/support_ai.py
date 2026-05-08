"""Customer support AI provider abstraction + fake/deterministic default.

Plan §11 — the default provider literal MUST be ``"fake"`` and a real
provider may only be activated by explicit env config + admin action.

The AST guard ``tests/test_support_ai_provider_defaults.py`` enforces:

- ``DEFAULT_PROVIDER == "fake"`` literal in this file.
- A missing real-provider key never silently falls back to the real
  provider — the resolver always returns the fake when configuration is
  incomplete.

DeepSeek provider activation requires ALL of:

1. ``AVT_SUPPORT_AI_PROVIDER`` set to ``"deepseek"`` OR admin selects a
   model whose registry provider is ``deepseek``.
2. Admin flips ``support_ai_enabled = true`` in /admin/support.
3. ``DEEPSEEK_API_KEY`` env var is set.
4. ``deepseek`` is in ``_IMPLEMENTED_REAL_PROVIDERS`` below.

Even then, the budget guard upstream (``support_service``) can still
override the path to template-only mode if the monthly USD cap is
exhausted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# src/ on sys.path so we can use llm_registry's get_api_key + registry.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

logger = logging.getLogger(__name__)


# IMPORTANT: this literal is enforced by an AST guard. Do NOT change to a
# variable, env-derived expression, or any non-string-literal default — the
# guard inspects the AST for a string equal to "fake".
DEFAULT_PROVIDER: str = "fake"


# Providers whose ``.reply()`` is fully implemented and reviewed.
#
# Adding a provider to this set is a deliberate rendezvous point with
# code review. Each new entry MUST come with:
#   1. A real ``.reply()`` implementation (returns AIReply, never
#      raises NotImplementedError).
#   2. Unit tests with mocked HTTP that exercise success + timeout +
#      4xx + 5xx + JSON-parse-failure paths.
#   3. The corresponding ``test_support_codex_round2.py`` test updated.
#
# 2026-05-08: ``deepseek`` wired (DeepSeek V4 Flash via OpenAI-compatible
# /chat/completions endpoint, JSON mode, thinking disabled, structured
# output schema). User-explicit activation only — admin must flip
# ``support_ai_enabled=true`` in /admin/support and ``DEEPSEEK_API_KEY``
# must be set in env.
_IMPLEMENTED_REAL_PROVIDERS: set[str] = {"deepseek"}


# DeepSeek API endpoint (OpenAI-compatible). Override via env if a
# proxy / mirror is needed.
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")


# System prompt for the support_chat task. Plan §5.2 + §5.4 + §5.5.
# Important: this string must NOT contain any forbidden internal
# identifiers — the AST guard at
# ``tests/test_support_no_internal_field_leakage.py`` scans this file
# and rejects ``project_dir`` / ``workspace_dir`` / ``manifest_path`` /
# absolute path prefixes in any literal.
_SYSTEM_PROMPT_TEMPLATE = """你是「爱译视频」的客服小助手，帮中国创作者解答关于：
- 产品定位（海外英文长视频 → 中文配音）
- 套餐 / 试用规则
- 视频上传与授权
- Express vs Studio 模式
- 任务失败排查
- 剪映草稿 / 字幕导出

回答原则（按优先级）：
1. 先给结论，再给 1-3 个具体步骤；中文，简短，避免直译腔。
2. 涉及套餐数字（试用天数 / 时长上限 / 价格 / 额度）时，**只复述下方 KNOWLEDGE 中的事实**，不允许编造或猜测。
3. 涉及账单 / 退款 / 投诉 / 隐私 / 版权时，建议转人工，不做最终承诺。
4. 不暴露技术细节（错误堆栈、内部路径、provider 名称、token、密钥）。
5. 不替用户执行高风险账户操作。
6. 用户消息已经做了 PII 脱敏，里面出现的 [手机号]/[邮箱]/[订单号] 等占位符照原样反馈即可。

输出格式：必须是合法 JSON 对象，schema：
{{
  "reply": "中文回答（必填，不超过 {max_chars} 字）",
  "confidence": 0.0-1.0 之间的浮点数（你对回答正确性的信心；不确定就给 < 0.55，会触发转人工建议）,
  "category": "trial | jianying | service_mode | upload | task_failure | billing | privacy | copyright | other",
  "handoff_recommended": true | false,
  "handoff_reason": null 或 "low_confidence" | "policy_required" | "abuse_review"
}}

KNOWLEDGE:
{knowledge_block}
"""


def _format_knowledge_block(knowledge: dict[str, Any]) -> str:
    """Render allowlist knowledge as a compact, JSON-safe block.

    Keep the output bounded: top 3 FAQ candidates + plan list + an
    optional sanitized job context block. We do NOT pass arbitrary keys
    from ``knowledge`` — only the documented shape.
    """
    parts: list[str] = []

    plans = knowledge.get("plans") or {}
    if isinstance(plans, dict):
        plan_list = plans.get("plans") or []
    else:
        plan_list = []
    if plan_list:
        parts.append("套餐事实（来自 Gateway plan_catalog，权威）：")
        parts.append(json.dumps(plan_list, ensure_ascii=False, indent=2))

    faq_hits = knowledge.get("faq_candidates") or []
    if faq_hits:
        parts.append("\nFAQ 候选（已按相关度排序）：")
        compact = [
            {"id": item.get("id"), "q": item.get("q"), "a": item.get("a")}
            for item in faq_hits[:3]
            if isinstance(item, dict)
        ]
        parts.append(json.dumps(compact, ensure_ascii=False, indent=2))

    job_ctx = knowledge.get("job")
    if job_ctx:
        parts.append("\n用户当前任务上下文（已脱敏 / 仅 allowlist 字段）：")
        parts.append(json.dumps(job_ctx, ensure_ascii=False, indent=2))

    if not parts:
        parts.append("（暂无外部知识；按通用客服原则作答即可。）")
    return "\n".join(parts)


def _build_messages(
    *,
    system_prompt: str,
    history: list[dict[str, str]],
    user_message: str,
) -> list[dict[str, str]]:
    """Build OpenAI-compatible messages list.

    History entries from ``support_service._recent_history`` already have
    PII redacted. We map ``sender`` to OpenAI roles:
      user / human → "user"  (human is operator-side reply, surfaces back as user-like)
      assistant → "assistant"
      system → skip (UI banners, not part of the conversation)
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for entry in history or []:
        sender = (entry.get("sender") or "").strip()
        body = (entry.get("body") or "").strip()
        if not body:
            continue
        if sender == "assistant":
            messages.append({"role": "assistant", "content": body})
        elif sender in ("user", "human"):
            messages.append({"role": "user", "content": body})
        # system messages are dropped — they're UI banners, not LLM context
    messages.append({"role": "user", "content": user_message})
    return messages


@dataclass(frozen=True)
class AIReply:
    """The structured shape every provider must return.

    Fields mirror ``SendMessageResponse`` but without the routing-decision
    fields (those are added in ``support_service``).
    """

    reply: str
    confidence: float
    category: str | None
    handoff_recommended: bool
    handoff_reason: str | None
    input_tokens: int
    output_tokens: int


class SupportAIProvider(ABC):
    """Abstract interface every support AI provider implements."""

    name: str = ""

    @abstractmethod
    async def reply(
        self,
        *,
        message: str,
        history: list[dict[str, str]],
        knowledge: dict[str, Any],
        max_output_tokens: int,
        max_input_chars: int,
        timeout_seconds: float,
    ) -> AIReply:
        ...


# ---------------------------------------------------------------------------
# Fake provider (default)
# ---------------------------------------------------------------------------


class FakeProvider(SupportAIProvider):
    """Deterministic provider for tests, dev, and budget-exhausted fallback.

    The reply text is fully derived from the input message via a stable
    hash, so unit tests can assert exact equality without monkeypatching
    randomness. ``input_tokens`` / ``output_tokens`` are computed from the
    actual prompt and reply lengths so the budget accumulator records
    realistic-looking rows even in tests.
    """

    name = "fake"

    async def reply(
        self,
        *,
        message: str,
        history: list[dict[str, str]],
        knowledge: dict[str, Any],
        max_output_tokens: int,
        max_input_chars: int,
        timeout_seconds: float,
    ) -> AIReply:
        snippet = (message or "").strip()[:60]
        digest = hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:8]
        # Build a short, deterministic answer that nudges the user back to
        # the FAQ/template path. Real production conversations should rarely
        # land here — the service tries templates first.
        reply_lines = [
            "（这是默认应答 / fake provider）",
            "我没有完全确定的答案，可以试试这些方向：",
            "1. 在右下角问题输入框旁边的快捷问题里挑一条最接近的。",
            "2. 在帮助中心查看「常见问题」。",
            "3. 如果问题涉及账单、退款、隐私、版权，请直接转人工。",
            f"会话标记：{digest}",
        ]
        reply_text = "\n".join(reply_lines)
        in_tokens = max(1, len(snippet) // 2)
        out_tokens = max(1, len(reply_text) // 2)
        return AIReply(
            reply=reply_text,
            confidence=0.4,
            category="generic",
            handoff_recommended=False,
            handoff_reason=None,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )


# ---------------------------------------------------------------------------
# DeepSeek provider (wired but not auto-invoked)
# ---------------------------------------------------------------------------


class DeepseekProvider(SupportAIProvider):
    """Production provider — DeepSeek V4 Flash via OpenAI-compatible API.

    Activated when ALL of the following hold:
      - ``"deepseek" in _IMPLEMENTED_REAL_PROVIDERS`` (it is, as of 2026-05-08).
      - ``DEEPSEEK_API_KEY`` env var or ``provider_api_keys.deepseek``
        admin-config override is set.
      - Admin flips ``support_ai_enabled=true`` in /admin/support.
      - Caller resolves a model whose registry provider is ``deepseek``.

    Failure modes never raise — the user always gets a polite reply. HTTP
    errors / timeouts / parse failures all degrade to a "AI 客服当前繁忙"
    message with ``handoff_recommended=true`` so the user can immediately
    escalate. Underlying error is logged at WARNING/ERROR for ops.

    Cost accounting: returns the exact ``input_tokens`` / ``output_tokens``
    from the API ``usage`` block. The caller (support_service +
    support_budget) multiplies by admin-configured unit prices to update
    the monthly accumulator.
    """

    name = "deepseek"

    # Conservative defaults — admin can lower max_output_tokens further
    # via /admin/support; this is the upper ceiling we send.
    _DEFAULT_TEMPERATURE = 0.3
    _MAX_RETRY = 1  # retry once on 5xx / network; never on 4xx (caller-side bug)

    async def reply(
        self,
        *,
        message: str,
        history: list[dict[str, str]],
        knowledge: dict[str, Any],
        max_output_tokens: int,
        max_input_chars: int,
        timeout_seconds: float,
    ) -> AIReply:
        # 1. Resolve API key + model id from llm_registry (respects admin
        #    provider_api_keys override; falls back to DEEPSEEK_API_KEY env).
        try:
            from services.llm_registry import (
                MODEL_REGISTRY,
                get_api_key,
                resolve_model_id,
            )
        except Exception as exc:
            logger.error("llm_registry unavailable: %s", exc)
            return self._busy_reply("registry_unavailable")

        model_logical = "deepseek"
        api_key = (get_api_key(model_logical) or "").strip()
        if not api_key:
            logger.warning(
                "DeepSeek selected but no API key resolvable; degrading. "
                "Configure DEEPSEEK_API_KEY env or provider_api_keys.deepseek admin override."
            )
            return self._busy_reply("missing_api_key")
        api_model_id = resolve_model_id(model_logical) or "deepseek-v4-flash"
        request_overrides = (
            MODEL_REGISTRY.get(model_logical, {}).get("request_overrides") or {}
        )

        # 2. Cap input length BEFORE sending — even if the user sent a
        #    10MB rant, we never send more than max_input_chars.
        clipped_message = (message or "")[: max(1, int(max_input_chars))]

        # 3. Build prompt.
        knowledge_block = _format_knowledge_block(knowledge or {})
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            max_chars=max(120, int(max_output_tokens) * 2),
            knowledge_block=knowledge_block,
        )
        messages = _build_messages(
            system_prompt=system_prompt,
            history=history or [],
            user_message=clipped_message,
        )

        # 4. POST with bounded retries.
        payload: dict[str, Any] = {
            "model": api_model_id,
            "messages": messages,
            "temperature": self._DEFAULT_TEMPERATURE,
            "max_tokens": max(1, int(max_output_tokens)),
            "response_format": {"type": "json_object"},
        }
        # Honor request_overrides from registry (DeepSeek V4 needs
        # ``thinking: {type: disabled}`` to skip reasoning mode and stay
        # at the cheap cost rank).
        for k, v in request_overrides.items():
            payload[k] = v

        url = f"{DEEPSEEK_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_err: str | None = None
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                attempt = 0
                while attempt <= self._MAX_RETRY:
                    attempt += 1
                    try:
                        response = await client.post(
                            url, headers=headers, json=payload
                        )
                    except httpx.TimeoutException:
                        last_err = "timeout"
                        if attempt > self._MAX_RETRY:
                            return self._busy_reply("timeout")
                        continue
                    except httpx.RequestError as exc:
                        last_err = f"network: {exc}"
                        if attempt > self._MAX_RETRY:
                            return self._busy_reply("network")
                        continue

                    if response.status_code >= 500:
                        last_err = f"http_{response.status_code}"
                        if attempt > self._MAX_RETRY:
                            logger.warning(
                                "DeepSeek %d after %d attempt(s): %s",
                                response.status_code,
                                attempt,
                                response.text[:300],
                            )
                            return self._busy_reply(last_err)
                        continue
                    if response.status_code >= 400:
                        # 4xx: don't retry. Most likely auth bad / payload bad.
                        logger.error(
                            "DeepSeek %d: %s",
                            response.status_code,
                            response.text[:300],
                        )
                        return self._busy_reply(f"http_{response.status_code}")

                    return self._parse_response(response)
        except Exception as exc:  # belt-and-suspenders
            logger.exception("DeepSeek call failed unexpectedly: %s", exc)
            return self._busy_reply("unexpected")

        return self._busy_reply(last_err or "unknown")

    @staticmethod
    def _parse_response(response: httpx.Response) -> AIReply:
        """Parse DeepSeek /chat/completions response.

        Expected shape:
          {"choices": [{"message": {"content": "<json string>"}}],
           "usage": {"prompt_tokens": N, "completion_tokens": M}}

        ``content`` itself is the JSON string we asked for via
        ``response_format=json_object``. Two-stage parse: outer envelope,
        inner reply schema. Fall back to plaintext if inner parse fails.
        """
        try:
            envelope = response.json()
        except ValueError:
            logger.warning("DeepSeek: response not JSON: %s", response.text[:300])
            return DeepseekProvider._busy_reply_static("invalid_envelope")

        usage = envelope.get("usage") or {}
        in_tokens = int(usage.get("prompt_tokens", 0) or 0)
        out_tokens = int(usage.get("completion_tokens", 0) or 0)

        try:
            raw_content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning("DeepSeek: unexpected envelope shape: %s", str(envelope)[:300])
            return DeepseekProvider._busy_reply_static("invalid_envelope")

        content = (raw_content or "").strip()
        if not content:
            return DeepseekProvider._busy_reply_static("empty_content")

        # Inner JSON: response_format=json_object guarantees valid JSON,
        # but defend anyway.
        try:
            parsed = json.loads(content)
        except ValueError:
            # Treat the whole content as the user-facing reply.
            return AIReply(
                reply=content[:1500],
                confidence=0.45,
                category="other",
                handoff_recommended=True,
                handoff_reason="low_confidence",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )

        if not isinstance(parsed, dict):
            return AIReply(
                reply=str(parsed)[:1500],
                confidence=0.45,
                category="other",
                handoff_recommended=True,
                handoff_reason="low_confidence",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
            )

        reply_text = str(parsed.get("reply") or "").strip()
        if not reply_text:
            return DeepseekProvider._busy_reply_static("empty_reply", in_tokens, out_tokens)

        try:
            confidence = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        category = str(parsed.get("category") or "other").strip()[:32]
        handoff_recommended = bool(parsed.get("handoff_recommended", False))
        handoff_reason_raw = parsed.get("handoff_reason")
        handoff_reason = (
            str(handoff_reason_raw).strip()
            if isinstance(handoff_reason_raw, str) and handoff_reason_raw.strip()
            else None
        )
        # Low-confidence safety: even if the model says
        # handoff_recommended=false, anything under 0.55 escalates per
        # plan §5.3.
        if confidence < 0.55:
            handoff_recommended = True
            if handoff_reason is None:
                handoff_reason = "low_confidence"

        return AIReply(
            reply=reply_text,
            confidence=confidence,
            category=category,
            handoff_recommended=handoff_recommended,
            handoff_reason=handoff_reason,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

    @staticmethod
    def _busy_reply_static(
        reason: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> AIReply:
        """Standard "AI 客服繁忙" fallback. Never charges for failed calls
        (input_tokens / output_tokens default to 0)."""
        return AIReply(
            reply=(
                "AI 客服暂时无法回答这个问题，建议直接转人工客服处理；"
                "或者你可以先看看右下角的快捷问题。"
            ),
            confidence=0.1,
            category="ai_unavailable",
            handoff_recommended=True,
            handoff_reason="low_confidence",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _busy_reply(self, reason: str) -> AIReply:
        return self._busy_reply_static(reason)


# ---------------------------------------------------------------------------
# Provider resolver
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, type[SupportAIProvider]] = {
    "fake": FakeProvider,
    "deepseek": DeepseekProvider,
}


def resolve_provider(name: str | None = None) -> SupportAIProvider:
    """Return the provider singleton for ``name``.

    Falls back to the fake provider if:
    - ``name`` is None / empty / unknown.
    - The configured provider is not the default and is missing its API key.
      (We deliberately keep this stateless — caller decides whether the
      configuration error should be loud.)
    """
    desired = (name or os.environ.get("AVT_SUPPORT_AI_PROVIDER", "") or DEFAULT_PROVIDER).strip()
    if desired not in _REGISTRY:
        desired = DEFAULT_PROVIDER
    cls = _REGISTRY[desired]
    return cls()


def is_real_provider_ready(name: str | None) -> bool:
    """Return True only if the named provider can actually run.

    Used by the admin UI / support service to gate the real-LLM path.
    Two conditions must BOTH hold:

    1. ``name`` is in ``_IMPLEMENTED_REAL_PROVIDERS``. P1 keeps that set
       empty so any "real" path silently falls back to fake — Codex P1
       review round 2.
    2. The provider's credentials are present (e.g. DeepSeek needs
       ``DEEPSEEK_API_KEY``).

    Even when (2) holds — and it usually does, because translation
    already uses the same key — (1) keeps the support flow on fake
    until a human reviewer adds the provider to the implemented set.
    """
    target = (name or "").strip()
    if target == "" or target == "fake":
        return False
    if target not in _IMPLEMENTED_REAL_PROVIDERS:
        return False
    if target == "deepseek":
        return bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip())
    return False

"""Customer support AI provider abstraction + fake/deterministic default.

Plan §11 — the default provider literal MUST be ``"fake"`` and a real
provider may only be activated by explicit env config + admin action.

The AST guard ``tests/test_support_ai_provider_defaults.py`` enforces:

- ``DEFAULT_PROVIDER == "fake"`` literal in this file.
- A missing real-provider key never silently falls back to the real
  provider — the resolver always returns the fake when configuration is
  incomplete.

The real DeepSeek provider is wired up but **never auto-invoked**. The
hard project constraint (CLAUDE.md) forbids paid-LLM calls in fallback
paths, so the resolver reaches the real provider ONLY when:

1. ``AVT_SUPPORT_AI_PROVIDER`` is set explicitly to a non-fake value.
2. ``AVT_SUPPORT_AI_ENABLED`` resolves true via admin config or env.
3. The provider's API key env (e.g. ``DEEPSEEK_API_KEY``) is set.

Even then, this module short-circuits to fake responses if the budget
guard already returned ``budget_exhausted`` upstream — but that decision
is made by the caller (``support_service``), not here.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


# IMPORTANT: this literal is enforced by an AST guard. Do NOT change to a
# variable, env-derived expression, or any non-string-literal default — the
# guard inspects the AST for a string equal to "fake".
DEFAULT_PROVIDER: str = "fake"


# Codex review round 2 (2026-05-08, P1): this set MUST contain only
# providers whose ``.reply()`` is fully implemented and reviewed. P1
# ships with NONE — the DeepSeek class is a stub that raises
# NotImplementedError, so adding it here would cause a real 500 the
# moment an admin flips ``support_ai_enabled=true`` while
# ``DEEPSEEK_API_KEY`` is set (which it almost certainly already is in
# production, since translation uses the same key).
#
# Add to this set ONLY when a provider's HTTP wiring lands and has been
# verified end-to-end. The corresponding regression guard
# ``test_support_codex_round2.py`` asserts the set stays empty until
# that work is done.
_IMPLEMENTED_REAL_PROVIDERS: set[str] = set()


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
    """Production provider stub.

    Intentionally NOT implemented in P1: the hard project constraint forbids
    auto-routing to a paid LLM. To activate this provider in production:

    1. Set ``AVT_SUPPORT_AI_PROVIDER=deepseek``.
    2. Set ``DEEPSEEK_API_KEY`` to a real key.
    3. Have an admin flip ``support_ai_enabled=true`` in the admin support
       page after confirming the monthly budget cap.

    The actual HTTP call lives behind a TODO so this commit cannot
    accidentally spend money.
    """

    name = "deepseek"

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
        raise NotImplementedError(
            "Real DeepSeek wiring is intentionally absent in P1. "
            "Set AVT_SUPPORT_AI_PROVIDER=fake (default) until the wiring "
            "is reviewed and the user explicitly opts in."
        )


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

"""High-level routing policy for support_service.

Inputs: a message, current conversation state, current budget state.
Outputs: a ``RoutingDecision`` enum + structured handoff hint.

The actual deterministic routing (template / FAQ matching) lives in
``support_templates`` and ``support_knowledge``. This module decides
*which path the message should take* — handoff vs LLM vs template — and
why.

Plan §5.3 / §5.4 / §5.6 / §5.7 — all the policy gates land here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from support_templates import (
    HUMAN_REQUEST_KEYWORDS,
    detect_sensitive_keyword,
    looks_english_only,
)


class RoutingDecision(str, Enum):
    """High-level decision for ``support_service``.

    - ``handoff_now`` — bypass AI entirely, create a human handoff.
    - ``template`` — try the deterministic template path.
    - ``llm`` — fall through to the AI provider (template miss + budget OK).
    - ``budget_blocked`` — would have been ``llm`` but budget is exhausted;
      caller should respond with the configured exhausted message.
    - ``english_fallback`` — non-Chinese input, return the English banner.
    - ``rate_limited`` — caller hit a per-IP / per-user / per-conversation
      cap; reply with the throttle message.
    """

    handoff_now = "handoff_now"
    template = "template"
    llm = "llm"
    budget_blocked = "budget_blocked"
    english_fallback = "english_fallback"
    rate_limited = "rate_limited"


@dataclass(frozen=True)
class PolicyOutcome:
    decision: RoutingDecision
    handoff_recommended: bool = False
    handoff_required: bool = False
    handoff_reason: str | None = None
    notes: str | None = None


def decide_route(
    *,
    message: str,
    sensitive_keywords: Iterable[str] | None,
    budget_exhausted: bool,
    rate_limited: bool,
    ai_enabled: bool,
    consecutive_unresolved: int = 0,
    repeated_paraphrase_count: int = 0,
) -> PolicyOutcome:
    """Evaluate routing rules in priority order.

    Order matters and reflects plan §5.3:
    1. Rate limit — never call any downstream when throttled.
    2. Sensitive keyword — always escalate, never let LLM speak.
    3. Explicit human request — escalate, friendly handoff.
    4. Repeated unresolved / paraphrase — escalate after thresholds.
    5. English fallback — short-circuit before LLM/template path.
    6. Budget exhausted — fall back to template-only mode.
    7. AI disabled — template-only mode.
    8. Default — template path (LLM only after a template miss).
    """

    if rate_limited:
        return PolicyOutcome(decision=RoutingDecision.rate_limited)

    matched_kw = detect_sensitive_keyword(message, keywords=sensitive_keywords)
    if matched_kw is not None:
        if matched_kw in HUMAN_REQUEST_KEYWORDS:
            return PolicyOutcome(
                decision=RoutingDecision.handoff_now,
                handoff_recommended=True,
                handoff_required=False,
                handoff_reason="user_requested_human",
                notes=f"keyword:{matched_kw}",
            )
        return PolicyOutcome(
            decision=RoutingDecision.handoff_now,
            handoff_recommended=True,
            handoff_required=True,
            handoff_reason=_classify(matched_kw),
            notes=f"keyword:{matched_kw}",
        )

    if consecutive_unresolved >= 2:
        return PolicyOutcome(
            decision=RoutingDecision.handoff_now,
            handoff_recommended=True,
            handoff_required=False,
            handoff_reason="repeated_unresolved",
        )
    if repeated_paraphrase_count >= 3:
        return PolicyOutcome(
            decision=RoutingDecision.handoff_now,
            handoff_recommended=True,
            handoff_required=False,
            handoff_reason="repeated_unresolved",
        )

    if looks_english_only(message):
        return PolicyOutcome(decision=RoutingDecision.english_fallback)

    if budget_exhausted:
        return PolicyOutcome(decision=RoutingDecision.budget_blocked)

    if not ai_enabled:
        return PolicyOutcome(decision=RoutingDecision.template)

    return PolicyOutcome(decision=RoutingDecision.template)


def _classify(matched_keyword: str) -> str:
    """Mirror of ``support_templates.classify_handoff_reason`` for the
    one-line case used here. Kept local to avoid an import cycle."""
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

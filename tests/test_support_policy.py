"""Routing policy tests.

The policy module is pure (no DB / no IO) so we can exercise the entire
decision matrix directly. This file mirrors plan §5.3 / §5.4 / §5.6 /
§5.7 — every priority rule is covered.
"""
from __future__ import annotations


def _decide(**kwargs):
    from gateway.support_policy import decide_route

    defaults = dict(
        message="",
        sensitive_keywords=None,
        budget_exhausted=False,
        rate_limited=False,
        ai_enabled=False,
    )
    defaults.update(kwargs)
    return decide_route(**defaults)


def test_rate_limit_wins_over_everything():
    out = _decide(message="人工", rate_limited=True, budget_exhausted=True)
    assert out.decision.value == "rate_limited"


def test_sensitive_user_request_routes_to_handoff():
    out = _decide(message="我要找人工")
    assert out.decision.value == "handoff_now"
    assert out.handoff_recommended is True
    assert out.handoff_required is False  # voluntary, not forced
    assert out.handoff_reason == "user_requested_human"


def test_sensitive_complaint_words_force_handoff():
    out = _decide(message="我要去工信部投诉")
    assert out.decision.value == "handoff_now"
    assert out.handoff_required is True
    assert out.handoff_reason == "abuse_review"


def test_policy_required_keywords():
    out = _decide(message="申请退款")
    assert out.decision.value == "handoff_now"
    assert out.handoff_required is True
    assert out.handoff_reason == "policy_required"


def test_repeated_unresolved_escalates():
    out = _decide(message="再问一次", consecutive_unresolved=2)
    assert out.decision.value == "handoff_now"
    assert out.handoff_reason == "repeated_unresolved"


def test_repeated_paraphrase_escalates():
    out = _decide(message="问问看", repeated_paraphrase_count=3)
    assert out.decision.value == "handoff_now"
    assert out.handoff_reason == "repeated_unresolved"


def test_english_input_falls_back_to_english_template():
    out = _decide(message="Can I upload a long video?")
    assert out.decision.value == "english_fallback"


def test_chinese_with_some_english_still_chinese_path():
    out = _decide(message="我想问 Studio 模式怎么开？")
    # Has 6+ CJK chars — does not trigger english_fallback.
    assert out.decision.value != "english_fallback"


def test_budget_exhausted_blocks_llm_path():
    out = _decide(message="试用怎么用", budget_exhausted=True, ai_enabled=True)
    assert out.decision.value == "budget_blocked"


def test_ai_disabled_uses_template_path():
    out = _decide(message="试用怎么用", ai_enabled=False)
    assert out.decision.value == "template"


def test_ai_enabled_default_path_is_template_first():
    """Plan §5.0 — even with AI on, the default routing prefers
    deterministic templates; the LLM is the post-template fallback. The
    policy returns ``template`` because the actual fall-through to the
    LLM happens inside support_service after the template lookup misses."""
    out = _decide(message="试用怎么用", ai_enabled=True)
    assert out.decision.value == "template"

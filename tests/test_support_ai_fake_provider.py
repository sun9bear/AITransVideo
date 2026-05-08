"""Determinism + bound checks on the fake support AI provider."""
from __future__ import annotations

import asyncio


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_fake_provider_reply_is_deterministic_for_same_input():
    from gateway.support_ai import FakeProvider

    provider = FakeProvider()
    msg = "试用结束后会自动扣费吗？"
    r1 = _run(
        provider.reply(
            message=msg,
            history=[],
            knowledge={"plans": []},
            max_output_tokens=100,
            max_input_chars=200,
            timeout_seconds=5.0,
        )
    )
    r2 = _run(
        provider.reply(
            message=msg,
            history=[],
            knowledge={"plans": []},
            max_output_tokens=100,
            max_input_chars=200,
            timeout_seconds=5.0,
        )
    )
    assert r1.reply == r2.reply
    assert r1.confidence == r2.confidence
    assert r1.input_tokens > 0
    assert r1.output_tokens > 0


def test_fake_provider_replies_different_for_different_inputs():
    from gateway.support_ai import FakeProvider

    provider = FakeProvider()
    a = _run(
        provider.reply(
            message="A",
            history=[],
            knowledge={},
            max_output_tokens=100,
            max_input_chars=200,
            timeout_seconds=5.0,
        )
    )
    b = _run(
        provider.reply(
            message="B",
            history=[],
            knowledge={},
            max_output_tokens=100,
            max_input_chars=200,
            timeout_seconds=5.0,
        )
    )
    # Both replies share the boilerplate, but the deterministic suffix
    # (会话标记) differs between inputs.
    assert a.reply != b.reply


def test_deepseek_provider_returns_busy_reply_when_api_key_missing():
    """As of 2026-05-08 DeepSeek is wired (HTTP via httpx). When env has
    no DEEPSEEK_API_KEY, the provider must NOT raise — it returns a
    polite "AI 客服繁忙" fallback so the user gets something usable.

    Separately, ``is_real_provider_ready("deepseek")`` returns False in
    that state, so support_service should never even pick deepseek. This
    test exercises the defense-in-depth path in case the readiness check
    is ever bypassed.
    """
    import os

    from gateway.support_ai import DeepseekProvider

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        # Also clear admin-config provider key by pointing config dir
        # somewhere that has no admin_settings.json.
        original_config_dir = os.environ.get("AIVIDEOTRANS_CONFIG_DIR")
        os.environ["AIVIDEOTRANS_CONFIG_DIR"] = "/nonexistent/path/for/test"
        # Force llm_registry cache invalidation so it doesn't return a
        # cached admin override from another test.
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass

        p = DeepseekProvider()
        reply = _run(
            p.reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=5.0,
            )
        )
        assert reply.handoff_recommended is True
        assert reply.handoff_reason in {"low_confidence", "policy_required"}
        assert reply.input_tokens == 0
        assert reply.output_tokens == 0
        assert "AI 客服" in reply.reply or "繁忙" in reply.reply
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved
        else:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        if original_config_dir is not None:
            os.environ["AIVIDEOTRANS_CONFIG_DIR"] = original_config_dir
        else:
            os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass

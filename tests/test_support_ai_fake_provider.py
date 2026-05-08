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


def test_deepseek_provider_raises_in_p1():
    """The DeepSeek provider is wired but intentionally not auto-invoked."""
    from gateway.support_ai import DeepseekProvider

    p = DeepseekProvider()
    try:
        _run(
            p.reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=5.0,
            )
        )
    except NotImplementedError as exc:
        assert "fake" in str(exc).lower() or "P1" in str(exc) or "p1" in str(exc).lower()
        return
    raise AssertionError("DeepseekProvider.reply should raise NotImplementedError in P1")

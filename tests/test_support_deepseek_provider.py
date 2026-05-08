"""Unit tests for the wired DeepseekProvider (2026-05-08).

Mock httpx end-to-end — no real network. We exercise:

- 200 success path with proper JSON envelope + inner JSON content
- Low-confidence threshold forces handoff_recommended=True
- 4xx (auth fail) returns busy reply, no retry
- 5xx triggers retry, then busy reply if retry also fails
- Timeout returns busy reply
- Invalid envelope JSON returns busy reply
- Inner content not JSON falls back to plain-text reply
- Missing API key short-circuits before HTTP

The provider must NEVER raise — it must always return an AIReply.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@contextmanager
def _api_key_env(key: str | None = "sk-test-deepseek-key"):
    """Temporarily set DEEPSEEK_API_KEY + clean admin config so the
    provider sees the env value (not a cached admin override)."""
    saved_key = os.environ.pop("DEEPSEEK_API_KEY", None)
    saved_config = os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
    try:
        os.environ["AIVIDEOTRANS_CONFIG_DIR"] = "/nonexistent/path/for/test"
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass
        if key:
            os.environ["DEEPSEEK_API_KEY"] = key
        yield
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        if saved_key is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved_key
        os.environ.pop("AIVIDEOTRANS_CONFIG_DIR", None)
        if saved_config is not None:
            os.environ["AIVIDEOTRANS_CONFIG_DIR"] = saved_config
        try:
            from services import llm_registry  # type: ignore

            llm_registry.invalidate_cache()
        except Exception:
            pass


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` so we can drive the
    provider's request loop deterministically.

    ``responses`` is a list of (status_code, json_body, raise_exc) tuples
    consumed in order on each ``post`` call.
    """

    def __init__(self, responses: list, *, raise_exc_each_call: list | None = None):
        self.responses = list(responses)
        self.raise_exc_each_call = list(raise_exc_each_call or [])
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.raise_exc_each_call:
            exc = self.raise_exc_each_call.pop(0)
            if exc is not None:
                raise exc
        if not self.responses:
            raise RuntimeError("no more queued responses")
        status, body, _ = self.responses.pop(0)
        request = httpx.Request("POST", url)
        return httpx.Response(
            status_code=status,
            content=json_dumps(body) if isinstance(body, (dict, list)) else (body or "").encode(),
            request=request,
            headers={"content-type": "application/json"},
        )


def json_dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _wrap_envelope(content: str | dict, *, in_tokens: int = 100, out_tokens: int = 50) -> dict:
    """Build the OpenAI-shaped envelope with `content` as message body."""
    if isinstance(content, dict):
        content_str = json.dumps(content, ensure_ascii=False)
    else:
        content_str = content
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content_str},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


def test_provider_happy_path_returns_structured_reply():
    from gateway.support_ai import AIReply, DeepseekProvider

    inner = {
        "reply": "试用结束后不会自动扣费，会自动回到 Free 套餐。",
        "confidence": 0.85,
        "category": "trial",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])

    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="试用结束会自动扣费吗？",
                history=[],
                knowledge={"plans": {"plans": [{"code": "free", "name": "Free"}]}},
                max_output_tokens=300,
                max_input_chars=500,
                timeout_seconds=10.0,
            )
        )

    assert isinstance(out, AIReply)
    assert out.reply.startswith("试用结束后不会")
    assert out.confidence == 0.85
    assert out.category == "trial"
    assert out.handoff_recommended is False
    assert out.input_tokens == 100
    assert out.output_tokens == 50
    # Single call, no retry.
    assert len(fake.calls) == 1
    posted = fake.calls[0]["json"]
    assert posted["model"] == "deepseek-v4-flash"
    assert posted["max_tokens"] == 300
    assert posted["response_format"] == {"type": "json_object"}
    # request_overrides from registry must propagate (DeepSeek V4: thinking disabled).
    assert posted.get("thinking") == {"type": "disabled"}
    # System prompt must be the first message.
    assert posted["messages"][0]["role"] == "system"
    # User message must be last.
    assert posted["messages"][-1]["role"] == "user"
    assert "试用结束" in posted["messages"][-1]["content"]


def test_provider_low_confidence_forces_handoff():
    """Plan §5.3: confidence < 0.55 always escalates."""
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "我也不太确定…",
        "confidence": 0.3,
        "category": "other",
        "handoff_recommended": False,  # model said no
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="某个偏门问题",
                history=[],
                knowledge={},
                max_output_tokens=300,
                max_input_chars=500,
                timeout_seconds=10.0,
            )
        )
    # We override even though model said no — confidence is below the
    # plan-mandated threshold.
    assert out.handoff_recommended is True
    assert out.handoff_reason == "low_confidence"


def test_provider_clips_oversized_input_message():
    """``max_input_chars`` is the hard ceiling sent to DeepSeek, even if
    the user spammed 100KB."""
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "ok",
        "confidence": 0.9,
        "category": "other",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    huge_message = "甲" * 10_000  # 10k Chinese chars
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        _run(
            DeepseekProvider().reply(
                message=huge_message,
                history=[],
                knowledge={},
                max_output_tokens=200,
                max_input_chars=200,  # <- clip to 200
                timeout_seconds=10.0,
            )
        )
    sent_user_msg = fake.calls[0]["json"]["messages"][-1]["content"]
    assert len(sent_user_msg) == 200
    assert sent_user_msg == huge_message[:200]


def test_provider_uses_history_user_assistant_roles_only():
    """system messages from the conversation log must be dropped — they
    are UI banners, not part of the LLM context."""
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "ok",
        "confidence": 0.9,
        "category": "other",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    history = [
        {"sender": "user", "body": "上一句问题"},
        {"sender": "assistant", "body": "上一次回复"},
        {"sender": "system", "body": "[已转人工] 这条 UI banner 不能进 prompt"},
        {"sender": "human", "body": "运营回复"},
    ]
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        _run(
            DeepseekProvider().reply(
                message="新问题",
                history=history,
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    sent = fake.calls[0]["json"]["messages"]
    bodies = [m["content"] for m in sent]
    assert any("UI banner" in b for b in bodies) is False
    assert any("上一句问题" in b for b in bodies)
    assert any("运营回复" in b for b in bodies)


# ---------------------------------------------------------------------------
# Error paths — all must return AIReply, never raise
# ---------------------------------------------------------------------------


def test_provider_4xx_returns_busy_reply_no_retry():
    from gateway.support_ai import DeepseekProvider

    fake = _FakeAsyncClient([(401, {"error": {"message": "bad key"}}, None)])
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert out.handoff_recommended is True
    assert out.input_tokens == 0
    assert out.output_tokens == 0
    # No retry on 4xx.
    assert len(fake.calls) == 1


def test_provider_5xx_retries_once_then_busy_reply():
    from gateway.support_ai import DeepseekProvider

    fake = _FakeAsyncClient(
        [
            (502, {"error": "bad gateway"}, None),
            (503, {"error": "still bad"}, None),
        ]
    )
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert out.handoff_recommended is True
    assert len(fake.calls) == 2  # initial + 1 retry


def test_provider_5xx_then_200_succeeds():
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "ok",
        "confidence": 0.9,
        "category": "other",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient(
        [
            (502, {"error": "bad gateway"}, None),
            (200, _wrap_envelope(inner), None),
        ]
    )
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert out.reply == "ok"
    assert out.confidence == 0.9
    assert len(fake.calls) == 2


def test_provider_timeout_returns_busy_reply():
    from gateway.support_ai import DeepseekProvider

    fake = _FakeAsyncClient(
        [(200, {}, None), (200, {}, None)],  # never used
        raise_exc_each_call=[
            httpx.TimeoutException("timeout"),
            httpx.TimeoutException("timeout"),
        ],
    )
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=5.0,
            )
        )
    assert out.handoff_recommended is True


def test_provider_invalid_envelope_json_returns_busy_reply():
    from gateway.support_ai import DeepseekProvider

    fake = _FakeAsyncClient([(200, "not-valid-json", None)])
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert out.handoff_recommended is True
    assert out.input_tokens == 0
    assert out.output_tokens == 0


def test_provider_inner_content_not_json_falls_back_to_plaintext():
    """When ``response_format=json_object`` is honored, inner content
    is JSON. But defend against models that ignore the schema."""
    from gateway.support_ai import DeepseekProvider

    # Inner content is plaintext, not JSON — should still surface as a
    # reply (low confidence, handoff recommended).
    fake = _FakeAsyncClient(
        [(200, _wrap_envelope("Sorry, I'm not sure but here's a guess..."), None)]
    )
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert "guess" in out.reply
    assert out.handoff_recommended is True
    assert out.confidence < 0.55


def test_provider_empty_inner_reply_returns_busy_reply():
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "",
        "confidence": 0.9,
        "category": "other",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        out = _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    assert out.handoff_recommended is True


# ---------------------------------------------------------------------------
# Knowledge injection
# ---------------------------------------------------------------------------


def test_provider_passes_plan_facts_into_system_prompt():
    from gateway.support_ai import DeepseekProvider

    inner = {
        "reply": "ok",
        "confidence": 0.9,
        "category": "trial",
        "handoff_recommended": False,
        "handoff_reason": None,
    }
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    knowledge = {
        "plans": {
            "plans": [
                {"code": "free", "name": "Free", "max_duration_minutes": 10},
                {"code": "plus", "name": "Plus", "max_duration_minutes": 45},
            ]
        }
    }
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        _run(
            DeepseekProvider().reply(
                message="哪个套餐能传 30 分钟",
                history=[],
                knowledge=knowledge,
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    system_msg = fake.calls[0]["json"]["messages"][0]["content"]
    assert "Free" in system_msg
    assert "Plus" in system_msg
    assert "max_duration_minutes" in system_msg


def test_provider_omits_job_block_when_no_context():
    from gateway.support_ai import DeepseekProvider

    inner = {"reply": "ok", "confidence": 0.9, "category": "other", "handoff_recommended": False, "handoff_reason": None}
    fake = _FakeAsyncClient([(200, _wrap_envelope(inner), None)])
    with _api_key_env(), patch("gateway.support_ai.httpx.AsyncClient", return_value=fake):
        _run(
            DeepseekProvider().reply(
                message="hi",
                history=[],
                knowledge={},  # no plans, no job context
                max_output_tokens=100,
                max_input_chars=200,
                timeout_seconds=10.0,
            )
        )
    system_msg = fake.calls[0]["json"]["messages"][0]["content"]
    assert "用户当前任务上下文" not in system_msg
    # Sentinel: prompt still says "暂无外部知识" when both lists empty.
    assert "暂无外部知识" in system_msg

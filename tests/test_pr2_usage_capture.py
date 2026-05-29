"""PR 2 (plan 2026-05-27): OpenAI-compatible real usage capture.

Covers the usage normalizer (`GeminiTranslator._normalize_openai_usage`) and
the `UsageMeter.record_llm` provider-usage fields. Uses the Phase 0a spike
fixtures as the authoritative field shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.gemini.translator import GeminiTranslator
from services.usage_meter import UsageMeter

_FIX = Path(__file__).parent / "fixtures" / "provider_responses"


def _body(fixture_name: str) -> dict:
    data = json.loads((_FIX / fixture_name).read_text(encoding="utf-8"))
    data.pop("_note", None)
    return {"choices": [{"message": {"content": "x"}}], "usage": data}


# --- normalizer: split prompt_tokens to avoid double-counting cached/audio ---

def test_normalize_mimo_text():
    u = GeminiTranslator._normalize_openai_usage(_body("mimo_v25_text_usage.json"))
    # prompt 249, cached 192, audio 0 -> text input 57
    assert u == {
        "input_tokens": 57,
        "output_tokens": 8,
        "cached_input_tokens": 192,
        "input_audio_tokens": 0,
    }


def test_normalize_mimo_audio():
    u = GeminiTranslator._normalize_openai_usage(_body("mimo_v25_audio_usage.json"))
    # prompt 253, cached 192, audio 2 -> text input 59
    assert u["input_tokens"] == 59
    assert u["cached_input_tokens"] == 192
    assert u["input_audio_tokens"] == 2
    assert u["output_tokens"] == 8


def test_normalize_deepseek():
    u = GeminiTranslator._normalize_openai_usage(_body("deepseek_chat_usage.json"))
    assert u["input_tokens"] == 6
    assert u["cached_input_tokens"] == 0
    assert u["input_audio_tokens"] == 0


def test_normalize_missing_or_malformed_usage_returns_empty():
    assert GeminiTranslator._normalize_openai_usage({"choices": []}) == {}
    assert GeminiTranslator._normalize_openai_usage({"usage": "not-a-dict"}) == {}
    assert GeminiTranslator._normalize_openai_usage({}) == {}


def test_normalize_never_negative_input():
    # Defensive: if cached+audio somehow exceed prompt, input clamps to 0.
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": 8, "audio_tokens": 5}}}
    u = GeminiTranslator._normalize_openai_usage(body)
    assert u["input_tokens"] == 0


# --- record_llm: provider usage vs estimate fallback ---

def test_record_llm_provider_usage_fields(tmp_path):
    meter = UsageMeter(tmp_path, job_id="pr2-usage")
    meter.record_llm(
        task="s3_translate", provider="mimo", model="mimo_v25",
        model_id="mimo-v2.5", input_text="x" * 100, output_text="y" * 20,
        input_tokens=57, output_tokens=8,
        cached_input_tokens=192, input_audio_tokens=0,
    )
    ev = meter.events[0]
    assert ev["input_tokens"] == 57          # real, not estimated from text
    assert ev["output_tokens"] == 8
    assert ev["cached_input_tokens"] == 192
    assert ev["input_audio_tokens"] == 0
    assert ev["token_count_source"] == "provider_usage"


def test_record_llm_estimate_path_unchanged(tmp_path):
    # Backward compat: callers that pass no token usage keep the old shape.
    meter = UsageMeter(tmp_path, job_id="pr2-estimate")
    meter.record_llm(
        task="s3_translate", provider="gemini", model="gemini_pro",
        input_text="hello world", output_text="resp",
    )
    ev = meter.events[0]
    assert ev["token_count_source"] == "estimated_text_length"
    assert "cached_input_tokens" not in ev
    assert "input_audio_tokens" not in ev


# --- PR 2 pt2: transcript_reviewer MiMo audio path ---

def test_reviewer_record_llm_usage_threads_tokens():
    import services.transcript_reviewer as tr

    captured: dict = {}

    class FakeMeter:
        def record_llm(self, **kwargs):
            captured.update(kwargs)

    tr._record_llm_usage(
        FakeMeter(),
        task="s2_pass1",
        review_model="mimo_v25",
        prompt="p",
        response_text="r",
        usage={"input_tokens": 59, "output_tokens": 8,
               "cached_input_tokens": 192, "input_audio_tokens": 2},
    )
    assert captured["input_tokens"] == 59
    assert captured["output_tokens"] == 8
    assert captured["cached_input_tokens"] == 192
    assert captured["input_audio_tokens"] == 2


def test_reviewer_call_mimo_omni_raw_fills_usage_sink(monkeypatch):
    import urllib.request
    import services.transcript_reviewer as tr

    body = {
        "choices": [{"message": {"content": json.dumps({"speakers": {}})}}],
        "usage": {"prompt_tokens": 253, "completion_tokens": 8,
                  "prompt_tokens_details": {"cached_tokens": 192, "audio_tokens": 2}},
    }

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(body).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())

    usage: dict = {}
    text = tr._call_mimo_omni_raw(
        api_key="k", prompt="p", model_id="mimo-v2.5", usage_sink=usage
    )
    assert json.loads(text) == {"speakers": {}}
    assert usage == {
        "input_tokens": 59,
        "output_tokens": 8,
        "cached_input_tokens": 192,
        "input_audio_tokens": 2,
    }

"""Tests for volcengine_tts_provider.py — 豆包 TTS (V3 HTTP Chunked API, 1.0 + 2.0)."""

from __future__ import annotations

import base64
import json
import struct

import pytest

import services.tts.volcengine_tts_provider as vc_module
from services.tts.volcengine_tts_provider import (
    DEFAULT_SPEAKER,
    DEFAULT_SPEAKER_1_0,
    DEFAULT_SPEAKER_2_0,
    DEFAULT_RESOURCE_ID,
    MODEL_1_0,
    RESOURCE_ID_1_0,
    RESOURCE_ID_2_0,
    VolcEngineTTSError,
    default_speaker_for_resource,
    synthesize,
)


# --- Helpers ---

def _make_pcm_bytes(n_samples: int = 100) -> bytes:
    """Generate minimal valid PCM16 mono samples."""
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _make_chunk_line(code: int, pcm_bytes: bytes = b"", message: str = "Success") -> bytes:
    """Build a single V3 chunked response JSON line."""
    payload: dict = {"code": code, "message": message}
    if pcm_bytes:
        payload["data"] = base64.b64encode(pcm_bytes).decode()
    return json.dumps(payload).encode("utf-8")


def _make_streaming_response(lines: list[bytes]):
    """Build a fake requests.Response with iter_lines support."""

    class FakeResponse:
        status_code = 200
        headers = {"X-Tt-Logid": "test-log-id"}

        def iter_lines(self, decode_unicode=False):
            for line in lines:
                yield line

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    return FakeResponse()


# --- Empty text ---

def test_empty_text_raises() -> None:
    with pytest.raises(VolcEngineTTSError, match="empty"):
        synthesize("", "zh_female_shuangkuaisisi_moon_bigtts")


def test_whitespace_only_raises() -> None:
    with pytest.raises(VolcEngineTTSError, match="empty"):
        synthesize("   ", "zh_female_shuangkuaisisi_moon_bigtts")


# --- Credentials ---

def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOLCENGINE_TTS_APP_ID", raising=False)
    monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_KEY", raising=False)
    monkeypatch.delenv("VOLCENGINE_TTS_APPID", raising=False)
    monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_TOKEN", raising=False)
    with pytest.raises(VolcEngineTTSError, match="VOLCENGINE_TTS_APP_ID"):
        synthesize("测试", "zh_female_shuangkuaisisi_moon_bigtts")


# --- V3 request headers ---

def test_synthesize_uses_v3_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "my-app-id")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "my-access-key")
    monkeypatch.setenv("VOLCENGINE_TTS_RESOURCE_ID", "seed-tts-2.0")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试", "zh_female_shuangkuaisisi_moon_bigtts")

    h = captured["headers"]
    assert h["X-Api-App-Id"] == "my-app-id"
    assert h["X-Api-Access-Key"] == "my-access-key"
    assert h["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert "X-Api-Request-Id" in h
    assert h["Content-Type"] == "application/json"
    # Must NOT contain V1-style Authorization header
    assert "Authorization" not in h
    # Request body must include sample_rate matching PCM_SAMPLE_RATE
    assert captured["json"]["req_params"]["audio_params"]["sample_rate"] == 24000


# --- Chunked audio accumulation ---

def test_synthesize_accumulates_pcm_chunks_and_returns_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    chunk1 = _make_pcm_bytes(100)
    chunk2 = _make_pcm_bytes(200)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        return _make_streaming_response([
            _make_chunk_line(0, chunk1),
            _make_chunk_line(0, chunk2),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    result = synthesize("测试文本")

    # Must be valid WAV
    assert result[:4] == b"RIFF"
    assert result[8:12] == b"WAVE"
    # WAV should contain both chunks' PCM data
    assert len(result) > len(chunk1) + len(chunk2)


# --- Finish event missing ---

def test_synthesize_raises_when_finish_event_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            # No finish event (code 20000000)
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    with pytest.raises(VolcEngineTTSError, match="finish"):
        synthesize("测试文本")


# --- Business error codes ---

def test_unknown_positive_error_code_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any positive code other than 0/20000000 must raise, not fall through to missing-finish."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        return _make_streaming_response([
            _make_chunk_line(99999, message="unknown error type"),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    with pytest.raises(VolcEngineTTSError, match="99999.*unknown error type"):
        synthesize("测试")


def test_synthesize_raises_on_business_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        return _make_streaming_response([
            _make_chunk_line(45000000, message="invalid speaker"),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    with pytest.raises(VolcEngineTTSError, match="45000000.*invalid speaker"):
        synthesize("测试")


# --- V1 guard removed ---

def test_long_text_not_rejected_by_v1_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """V3 has no local 1024-byte guard. Long text should reach the API, not be locally rejected."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    long_text = "测" * 500  # 1500 bytes — would fail V1 guard

    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    result = synthesize(long_text)
    assert result[:4] == b"RIFF"  # Success — not rejected locally


# --- Legacy env compat ---

def test_legacy_env_vars_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Old VOLCENGINE_TTS_APPID / VOLCENGINE_TTS_ACCESS_TOKEN should work as fallback."""
    monkeypatch.delenv("VOLCENGINE_TTS_APP_ID", raising=False)
    monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_KEY", raising=False)
    monkeypatch.setenv("VOLCENGINE_TTS_APPID", "legacy-app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_TOKEN", "legacy-key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")
    assert captured["headers"]["X-Api-App-Id"] == "legacy-app"
    assert captured["headers"]["X-Api-Access-Key"] == "legacy-key"


# ===================================================================
# B1: resource_id + model dual-mode support
# ===================================================================

# --- Constants ---

def test_constants_consistency() -> None:
    """Verify constant relationships."""
    assert DEFAULT_RESOURCE_ID == RESOURCE_ID_1_0
    assert DEFAULT_SPEAKER == DEFAULT_SPEAKER_1_0
    assert "_moon_bigtts" in DEFAULT_SPEAKER_1_0
    assert "_uranus_bigtts" in DEFAULT_SPEAKER_2_0


# --- default_speaker_for_resource ---

def test_default_speaker_for_1_0() -> None:
    assert default_speaker_for_resource(RESOURCE_ID_1_0) == DEFAULT_SPEAKER_1_0
    assert default_speaker_for_resource("seed-tts-1.0") == DEFAULT_SPEAKER_1_0


def test_default_speaker_for_2_0() -> None:
    assert default_speaker_for_resource(RESOURCE_ID_2_0) == DEFAULT_SPEAKER_2_0
    assert default_speaker_for_resource("seed-tts-2.0") == DEFAULT_SPEAKER_2_0


def test_default_speaker_for_none_returns_1_0() -> None:
    assert default_speaker_for_resource(None) == DEFAULT_SPEAKER_1_0


def test_default_speaker_for_unknown_returns_1_0() -> None:
    assert default_speaker_for_resource("some-unknown-resource") == DEFAULT_SPEAKER_1_0


# --- Explicit resource_id parameter ---

def test_explicit_resource_id_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing resource_id= to synthesize() overrides the env var."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")
    monkeypatch.setenv("VOLCENGINE_TTS_RESOURCE_ID", "env-resource-from-env")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试", resource_id="seed-tts-2.0")

    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"


def test_no_resource_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit resource_id, env var is used."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")
    monkeypatch.setenv("VOLCENGINE_TTS_RESOURCE_ID", "env-resource-id")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")  # no resource_id kwarg

    assert captured["headers"]["X-Api-Resource-Id"] == "env-resource-id"


def test_no_resource_id_no_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit resource_id and no env var, DEFAULT_RESOURCE_ID is used."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")
    monkeypatch.delenv("VOLCENGINE_TTS_RESOURCE_ID", raising=False)

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")

    assert captured["headers"]["X-Api-Resource-Id"] == DEFAULT_RESOURCE_ID


# --- model parameter ---

def test_model_written_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """When model= is passed, it appears in req_params.model."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试", model=MODEL_1_0)

    assert captured["json"]["req_params"]["model"] == MODEL_1_0


def test_model_omitted_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When model= is None (default), req_params must NOT contain 'model' key."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")  # no model kwarg

    assert "model" not in captured["json"]["req_params"]


def test_model_omitted_when_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """model='' should also omit the key from req_params."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试", model="")

    assert "model" not in captured["json"]["req_params"]


# --- Combined resource_id + model ---

def test_express_style_call_with_resource_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates the express path: resource=1.0, model=1.1, 1.0 speaker."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize(
        "测试",
        voice_id=DEFAULT_SPEAKER_1_0,
        resource_id=RESOURCE_ID_1_0,
        model=MODEL_1_0,
    )

    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-1.0"
    assert captured["json"]["req_params"]["model"] == "seed-tts-1.1"
    assert captured["json"]["req_params"]["speaker"] == DEFAULT_SPEAKER_1_0


def test_studio_style_call_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates the studio path: resource=2.0, no model, 2.0 speaker."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize(
        "测试",
        voice_id=DEFAULT_SPEAKER_2_0,
        resource_id=RESOURCE_ID_2_0,
        model=None,
    )

    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert "model" not in captured["json"]["req_params"]
    assert captured["json"]["req_params"]["speaker"] == DEFAULT_SPEAKER_2_0


# --- speech_rate (Phase 2 Task 1 VolcEngine branch, 2026-04-15) -------------

def test_speech_rate_default_zero_omits_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """speech_rate not passed (default 0) must NOT appear in audio_params —
    keeps the wire payload byte-identical to pre-Phase 2 callers."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)
    synthesize("测试")
    assert "speech_rate" not in captured["json"]["req_params"]["audio_params"]


def test_speech_rate_explicit_zero_omits_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing speech_rate=0 explicitly must also omit the key (semantically
    identical to omission; baseline rate = 0)."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)
    synthesize("测试", speech_rate=0)
    assert "speech_rate" not in captured["json"]["req_params"]["audio_params"]


def test_speech_rate_positive_injected_as_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive speech_rate (speed up) appears in audio_params as int."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)
    synthesize("测试", speech_rate=15)
    audio_params = captured["json"]["req_params"]["audio_params"]
    assert audio_params["speech_rate"] == 15
    assert isinstance(audio_params["speech_rate"], int)


def test_speech_rate_negative_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative speech_rate (slow down) appears in audio_params."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)
    synthesize("测试", speech_rate=-30)
    assert captured["json"]["req_params"]["audio_params"]["speech_rate"] == -30


def test_speech_rate_orthogonal_to_model_and_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """speech_rate coexists with model + resource_id without interference."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)
    synthesize(
        "测试",
        voice_id=DEFAULT_SPEAKER_1_0,
        resource_id=RESOURCE_ID_1_0,
        model=MODEL_1_0,
        speech_rate=20,
    )
    rp = captured["json"]["req_params"]
    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-1.0"
    assert rp["model"] == "seed-tts-1.1"
    assert rp["speaker"] == DEFAULT_SPEAKER_1_0
    assert rp["audio_params"]["speech_rate"] == 20
    # Other audio_params fields must still be present and untouched.
    assert rp["audio_params"]["format"] == "pcm"
    assert rp["audio_params"]["sample_rate"] == 24000


# --- B1.1: voice_id auto-resolves from effective resource_id ---

def test_no_voice_id_with_resource_2_0_uses_2_0_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """resource_id=seed-tts-2.0 and no voice_id → speaker must be DEFAULT_SPEAKER_2_0."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试", resource_id=RESOURCE_ID_2_0)  # no voice_id

    assert captured["json"]["req_params"]["speaker"] == DEFAULT_SPEAKER_2_0


def test_no_voice_id_with_env_resource_2_0_uses_2_0_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """VOLCENGINE_TTS_RESOURCE_ID=seed-tts-2.0 and no voice_id → speaker must be DEFAULT_SPEAKER_2_0."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")
    monkeypatch.setenv("VOLCENGINE_TTS_RESOURCE_ID", "seed-tts-2.0")

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")  # no voice_id, no resource_id → env picks seed-tts-2.0

    assert captured["json"]["req_params"]["speaker"] == DEFAULT_SPEAKER_2_0


def test_no_voice_id_no_resource_uses_1_0_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No voice_id, no resource_id, no env → 1.0 defaults for both."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "app")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "key")
    monkeypatch.delenv("VOLCENGINE_TTS_RESOURCE_ID", raising=False)

    captured: dict = {}
    pcm = _make_pcm_bytes(50)

    def fake_post(url, *, headers=None, json=None, stream=False, timeout=None):
        captured["headers"] = dict(headers or {})
        captured["json"] = json
        return _make_streaming_response([
            _make_chunk_line(0, pcm),
            _make_chunk_line(20000000),
        ])

    monkeypatch.setattr(vc_module, "_do_post", fake_post)

    synthesize("测试")  # all defaults

    assert captured["headers"]["X-Api-Resource-Id"] == RESOURCE_ID_1_0
    assert captured["json"]["req_params"]["speaker"] == DEFAULT_SPEAKER_1_0

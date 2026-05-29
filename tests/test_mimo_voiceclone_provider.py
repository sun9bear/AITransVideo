"""Phase 1 (plan 2026-05-29 free-tier): MiMo voiceclone provider function."""

import base64

import pytest

import services.tts.mimo_tts_provider as mp


def _ok_response(audio_bytes=b"RIFF....fakewav-payload-over-44-bytes-xxxxxxxxxxxxxxxxxxxx"):
    return {"choices": [{"message": {"audio": {"data": base64.b64encode(audio_bytes).decode()}}}]}


def test_synthesize_voiceclone_returns_audio_bytes(monkeypatch):
    captured = {}

    def fake_post(*, endpoint, api_key, payload, **kw):
        captured["payload"] = payload
        return _ok_response()

    monkeypatch.setattr(mp, "_post_json", fake_post)
    out = mp.synthesize_voiceclone("你好世界这是测试", reference_audio=b"\x00" * 2000, api_key="k")
    assert isinstance(out, bytes) and len(out) >= 44
    p = captured["payload"]
    assert p["model"] == "mimo-v2.5-tts-voiceclone"
    assert p["audio"]["voice"].startswith("data:audio/wav;base64,")
    assert p["messages"][0]["role"] == "assistant"
    assert p["messages"][0]["content"] == "你好世界这是测试"
    assert p["modalities"] == ["audio"]


def test_synthesize_voiceclone_rejects_oversized_reference(monkeypatch):
    monkeypatch.setattr(mp, "_post_json", lambda **kw: _ok_response())
    with pytest.raises(mp.MiMoTTSError, match="10MB|exceeds"):
        mp.synthesize_voiceclone("x", reference_audio=b"\x00" * (8 * 1024 * 1024), api_key="k")


def test_synthesize_voiceclone_requires_key(monkeypatch):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    with pytest.raises(mp.MiMoTTSError):
        mp.synthesize_voiceclone("x", reference_audio=b"\x00" * 100, api_key=None)


def test_synthesize_voiceclone_accepts_path_reference(monkeypatch, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"\x11" * 500)
    captured = {}

    def fake_post(*, endpoint, api_key, payload, **kw):
        captured["payload"] = payload
        return _ok_response()

    monkeypatch.setattr(mp, "_post_json", fake_post)
    out = mp.synthesize_voiceclone("文本", reference_audio=ref, api_key="k")
    assert len(out) >= 44
    # base64 of the file bytes must be embedded
    assert base64.b64encode(b"\x11" * 500).decode() in captured["payload"]["audio"]["voice"]

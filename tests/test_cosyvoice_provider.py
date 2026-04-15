"""Tests for cosyvoice_provider.py — subprocess/helper architecture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import services.tts.cosyvoice_provider as cosyvoice_provider_module


def test_cosyvoice_empty_text_raises() -> None:
    with pytest.raises(cosyvoice_provider_module.CosyVoiceTTSError, match="empty"):
        cosyvoice_provider_module.synthesize("", "longanyang")
    with pytest.raises(cosyvoice_provider_module.CosyVoiceTTSError, match="empty"):
        cosyvoice_provider_module.synthesize("   ", "longanyang")


def test_resolve_deployment_mode_returns_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", raising=False)
    result = cosyvoice_provider_module._resolve_deployment_mode()
    assert result in ("international", "mainland")


def test_resolve_ws_url_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", raising=False)
    result = cosyvoice_provider_module._resolve_ws_url()
    assert result.startswith("wss://")


def test_synthesize_once_request_contains_endpoint_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _synthesize_once writes endpoint_mode into request.json."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")

    captured_request: dict = {}

    original_popen = cosyvoice_provider_module.subprocess.Popen

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            # Read the request JSON that the provider wrote
            request_path = cmd[-1]  # last arg is request.json path
            captured_request.update(json.loads(Path(request_path).read_text(encoding="utf-8")))
            # Write a fake output WAV
            output_path = captured_request.get("output_path", "")
            if output_path:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"RIFF" + b"\x00" * 100)

        def communicate(self, timeout=None):
            stdout = json.dumps({"ok": True, "output_path": captured_request.get("output_path", ""), "bytes": 104})
            return stdout, ""

        @property
        def returncode(self):
            return 0

    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", FakePopen)

    audio = cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash")

    assert "endpoint_mode" in captured_request
    assert captured_request["endpoint_mode"] == "international"
    assert captured_request["voice"] == "longanyang"
    assert captured_request["model"] == "cosyvoice-v3-flash"


def test_synthesize_once_passes_mainland_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify runtime mainland mode is passed through."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "mainland")

    captured_request: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            request_path = cmd[-1]
            captured_request.update(json.loads(Path(request_path).read_text(encoding="utf-8")))
            output_path = captured_request.get("output_path", "")
            if output_path:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"RIFF" + b"\x00" * 100)

        def communicate(self, timeout=None):
            return json.dumps({"ok": True, "output_path": captured_request.get("output_path", ""), "bytes": 104}), ""

        @property
        def returncode(self):
            return 0

    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", FakePopen)

    cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash")

    assert captured_request["endpoint_mode"] == "mainland"


def test_shutdown_runtime_callable() -> None:
    cosyvoice_provider_module.shutdown_runtime()


# --- speech_rate (Phase 2 Task 1 CosyVoice branch, 2026-04-15) --------------

def _build_fake_popen(captured: dict):
    """Shared FakePopen that reads the helper request JSON and writes a
    fake 100-byte WAV so _synthesize_once can complete without a real
    DashScope call."""
    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            request_path = cmd[-1]
            captured.update(json.loads(Path(request_path).read_text(encoding="utf-8")))
            out = captured.get("output_path", "")
            if out:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_bytes(b"RIFF" + b"\x00" * 100)

        def communicate(self, timeout=None):
            return json.dumps({"ok": True, "output_path": captured.get("output_path", ""), "bytes": 104}), ""

        @property
        def returncode(self):
            return 0

    return _FakePopen


def test_speech_rate_default_omitted_from_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """speech_rate=1.0 (default) must NOT be written into request.json so
    pre-Phase 2 callers emit byte-identical payloads."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    captured: dict = {}
    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", _build_fake_popen(captured))

    cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash")

    assert "speech_rate" not in captured


def test_speech_rate_explicit_one_omitted_from_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit speech_rate=1.0 (same as SDK default) also omits the key."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    captured: dict = {}
    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", _build_fake_popen(captured))

    cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash", speech_rate=1.0)

    assert "speech_rate" not in captured


def test_speech_rate_positive_injected_in_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-default speech_rate gets written into request.json as float."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    captured: dict = {}
    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", _build_fake_popen(captured))

    cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash", speech_rate=1.15)

    assert captured["speech_rate"] == pytest.approx(1.15)


def test_speech_rate_slow_value_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-1.0 speech_rate (slow down) flows through."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    captured: dict = {}
    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", _build_fake_popen(captured))

    cosyvoice_provider_module._synthesize_once("test", "longanyang", "cosyvoice-v3-flash", speech_rate=0.85)

    assert captured["speech_rate"] == pytest.approx(0.85)


def test_synthesize_public_api_forwards_speech_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public ``synthesize()`` exposes speech_rate as a kwarg and
    forwards it to the subprocess helper."""
    monkeypatch.setenv("COSYVOICE_RUNTIME_ENDPOINT_MODE", "international")
    captured: dict = {}
    monkeypatch.setattr(cosyvoice_provider_module.subprocess, "Popen", _build_fake_popen(captured))

    audio = cosyvoice_provider_module.synthesize("测试", "longanyang", speech_rate=1.30)

    assert audio.startswith(b"RIFF")
    assert captured["speech_rate"] == pytest.approx(1.30)

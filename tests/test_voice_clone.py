from urllib import request

import pytest

import services.voice_clone as voice_clone_module
from services.voice_clone import MiniMaxVoiceCloneClient, VoiceCloneUploadError, VoiceCloneConfig


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return self._body


def test_minimax_voice_clone_client_retries_timeout_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = VoiceCloneConfig(
        enabled=True,
        base_url="https://clone.example",
        api_key="secret",
        timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    client = MiniMaxVoiceCloneClient(config)
    attempts = {"count": 0}

    def fake_urlopen(request_object, timeout: float):
        del request_object
        assert timeout == 5.0
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TimeoutError("timed out")
        return _FakeHTTPResponse(b'{"ok": true}')

    monkeypatch.setattr(voice_clone_module.request, "urlopen", fake_urlopen)

    payload = client._execute_http_request(
        request_object=request.Request("https://clone.example/v1/files/upload", method="POST"),
        failure_error_type="upload",
    )

    assert payload == b'{"ok": true}'
    assert attempts["count"] == 3
    captured = capsys.readouterr()
    assert "voice clone upload failed (timeout); retrying 2/3 in 0.0s..." in captured.out
    assert "voice clone upload failed (timeout); retrying 3/3 in 0.0s..." in captured.out


def test_minimax_voice_clone_client_reports_retry_exhaustion_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceCloneConfig(
        enabled=True,
        base_url="https://clone.example",
        api_key="secret",
        timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    client = MiniMaxVoiceCloneClient(config)
    attempts = {"count": 0}

    def fake_urlopen(request_object, timeout: float):
        del request_object, timeout
        attempts["count"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(voice_clone_module.request, "urlopen", fake_urlopen)

    with pytest.raises(VoiceCloneUploadError, match="after 3 attempts"):
        client._execute_http_request(
            request_object=request.Request("https://clone.example/v1/files/upload", method="POST"),
            failure_error_type="upload",
        )

    assert attempts["count"] == 3


def test_build_requested_voice_id_strips_non_ascii_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_clone_module.time, "time", lambda: 1234.567)

    requested_voice_id = voice_clone_module._build_requested_voice_id("speaker_沃伦·巴菲特")

    assert requested_voice_id == "vt_speaker_1234567"
    assert requested_voice_id.isascii()

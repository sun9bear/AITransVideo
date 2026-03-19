from io import BytesIO
import json
from pathlib import Path

from pydub import AudioSegment
import pytest

from services.gemini.translator import DubbingSegment
from services.tts.tts_generator import (
    TTSConfig,
    TTSGenerationError,
    TTSGenerator,
    load_tts_config,
)
import services.tts.tts_generator as tts_generator_module


def _build_segment(
    *,
    segment_id: int,
    start_ms: int = 0,
    end_ms: int = 1_000,
    cn_text: str = "测试中文配音",
    tts_cn_text: str | None = None,
    voice_id: str = "voice_demo_001",
) -> DubbingSegment:
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id=voice_id,
        start_ms=start_ms,
        end_ms=end_ms,
        target_duration_ms=end_ms - start_ms,
        source_text="Demo English text.",
        cn_text=cn_text,
        tts_cn_text=cn_text if tts_cn_text is None else tts_cn_text,
    )


def _build_wav_bytes(*, duration_ms: int = 1_000) -> bytes:
    buffer = BytesIO()
    AudioSegment.silent(duration=duration_ms).export(buffer, format="wav")
    return buffer.getvalue()


def _install_fake_requests(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, object] | Exception],
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeRequests:
        @staticmethod
        def post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int) -> FakeResponse:
            calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            response = responses[len(calls) - 1]
            if isinstance(response, Exception):
                raise response
            return FakeResponse(
                status_code=int(response["status_code"]),
                payload=dict(response.get("payload", {})),
            )

    monkeypatch.setattr(tts_generator_module, "requests", FakeRequests())
    return calls


def test_tts_generator_generates_single_segment_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_000)
    audio_hex = wav_bytes.hex()
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            }
        ],
    )
    segment = _build_segment(segment_id=1)

    results = TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        [segment],
        str(tmp_path / "tts"),
    )

    assert len(results) == 1
    result = results[0]
    assert Path(result.audio_path).exists()
    assert Path(result.audio_path).name == "segment_001_speaker_a.wav"
    assert result.duration_ms > 0
    assert segment.tts_audio_path == result.audio_path
    assert calls[0]["url"] == "https://api.minimaxi.com/v1/t2a_v2"


def test_tts_generator_generates_multiple_segments_and_updates_alignment_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_hex = _build_wav_bytes(duration_ms=800).hex()
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
        ],
    )
    segments = [
        _build_segment(segment_id=1, start_ms=0, end_ms=1_000),
        _build_segment(segment_id=2, start_ms=1_000, end_ms=2_000),
        _build_segment(segment_id=3, start_ms=2_000, end_ms=3_000),
    ]

    results = TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        segments,
        str(tmp_path / "tts"),
    )

    assert len(results) == 3
    for index, segment in enumerate(segments, start=1):
        assert Path(results[index - 1].audio_path).exists()
        assert Path(results[index - 1].audio_path).name == f"segment_{index:03d}_speaker_a.wav"
        assert segment.actual_duration_ms > 0
        assert segment.alignment_ratio > 0
        assert segment.tts_audio_path is not None


def test_tts_generator_raises_on_non_200_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_requests(
        monkeypatch,
        [{"status_code": 500, "payload": {}}],
    )

    with pytest.raises(TTSGenerationError, match="status_code=500"):
        TTSGenerator(TTSConfig(api_key="secret", max_retries=0)).generate_all(
            [_build_segment(segment_id=1)],
            str(tmp_path / "tts"),
        )


def test_tts_generator_raises_on_business_error_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 1039, "status_msg": "invalid voice_id"},
                    "data": {"audio": ""},
                },
            }
        ],
    )

    with pytest.raises(TTSGenerationError, match="invalid voice_id"):
        TTSGenerator(TTSConfig(api_key="secret")).generate_all(
            [_build_segment(segment_id=1)],
            str(tmp_path / "tts"),
        )
    assert len(calls) == 1


def test_tts_generator_decodes_hex_audio_and_writes_expected_wav_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_200)
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            }
        ],
    )
    segment = _build_segment(segment_id=1)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
    )

    written_bytes = Path(result.audio_path).read_bytes()
    assert written_bytes == wav_bytes
    assert len(AudioSegment.from_wav(result.audio_path)) > 0


def test_tts_generator_prefers_tts_cn_text_over_cn_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=900)
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            }
        ],
    )
    segment = _build_segment(
        segment_id=1,
        cn_text="字幕文本",
        tts_cn_text="配音文本",
    )

    TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        [segment],
        str(tmp_path / "tts"),
    )

    assert calls[0]["json"]["text"] == "配音文本"


def test_tts_generator_prints_progress_every_five_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audio_hex = _build_wav_bytes(duration_ms=800).hex()
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            }
            for _ in range(6)
        ],
    )
    segments = [
        _build_segment(segment_id=index, start_ms=(index - 1) * 1_000, end_ms=index * 1_000)
        for index in range(1, 7)
    ]

    TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        segments,
        str(tmp_path / "tts"),
    )

    captured = capsys.readouterr().out
    assert "[S4] TTS进度：5/6 段" in captured
    assert "[S4] TTS进度：6/6 段" in captured


def test_load_tts_config_reads_existing_tts_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "api_key": "config-secret",
                    "api_key_env_var": "AUTODUB_TTS_API_KEY",
                    "base_url": "https://api.minimaxi.com",
                    "model_name": "speech-2.8-turbo",
                    "audio_format": "wav",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tts_generator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_tts_config()

    assert config.api_key == "config-secret"
    assert config.base_url == "https://api.minimaxi.com"
    assert config.model == "speech-2.8-turbo"
    assert config.audio_format == "wav"


def test_tts_generator_retries_transient_request_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_000)
    calls = _install_fake_requests(
        monkeypatch,
        [
            RuntimeError("tls eof"),
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            },
        ],
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(tts_generator_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    results = TTSGenerator(
        TTSConfig(
            api_key="secret",
            timeout_seconds=12,
            max_retries=2,
            retry_backoff_seconds=0.5,
        )
    ).generate_all(
        [_build_segment(segment_id=1)],
        str(tmp_path / "tts"),
    )

    assert len(results) == 1
    assert len(calls) == 2
    assert sleep_calls == [0.5]
    captured = capsys.readouterr().out
    assert "MiniMax请求失败，0.5秒后重试（1/2）" in captured


def test_load_tts_config_reads_retry_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "api_key": "config-secret",
                    "timeout_seconds": 45,
                    "max_retries": 5,
                    "retry_backoff_seconds": 2.5,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tts_generator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_tts_config()

    assert config.timeout_seconds == 45
    assert config.max_retries == 5
    assert config.retry_backoff_seconds == 2.5

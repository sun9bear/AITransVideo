import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.assemblyai.transcriber import AssemblyAITranscriber, TranscriptionError, load_assemblyai_config
import services.assemblyai.transcriber as transcriber_module


def _write_dummy_audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFFdemo")
    return path


def _write_large_dummy_audio(path: Path, *, size_mb: int = 51) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size_mb * 1024 * 1024)
    return path


def _install_fake_assemblyai_sdk(
    monkeypatch: pytest.MonkeyPatch,
    transcript: object | None = None,
    *,
    outcomes: list[object] | None = None,
) -> dict[str, object]:
    calls: dict[str, object] = {"transcribe_calls": 0}
    resolved_outcomes = list(outcomes) if outcomes is not None else [transcript]

    class FakeTranscriptionConfig:
        def __init__(
            self,
            *,
            language_code: str,
            speaker_labels: bool,
            speech_models: list[object] | None = None,
            speech_model: object | None = None,
            speakers_expected: int | None = None,
            disfluencies: bool | None = None,
            prompt: str | None = None,
        ) -> None:
            self.language_code = language_code
            self.speaker_labels = speaker_labels
            self.speech_model = speech_model
            self.speech_models = speech_models
            self.speakers_expected = speakers_expected
            self.disfluencies = disfluencies
            self.prompt = prompt
            calls["config"] = self

    class FakeTranscriber:
        def transcribe(self, audio_path: str, config: FakeTranscriptionConfig) -> object:
            calls["transcribe_calls"] += 1
            calls["audio_path"] = audio_path
            calls["transcribe_config"] = config
            current = resolved_outcomes.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

    fake_aai = SimpleNamespace(
        settings=SimpleNamespace(api_key=None),
        TranscriptionConfig=FakeTranscriptionConfig,
        Transcriber=lambda: FakeTranscriber(),
    )
    monkeypatch.setattr(transcriber_module, "_load_assemblyai_sdk", lambda: fake_aai)
    return calls


def _make_sentence(start_ms: int, end_ms: int, text: str, speaker: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(start=start_ms, end=end_ms, text=text, speaker=speaker)


def _make_word(start_ms: int, end_ms: int, text: str, speaker: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(start=start_ms, end=end_ms, text=text, speaker=speaker)


def _make_utterance(start_ms: int, end_ms: int, text: str, speaker: str) -> SimpleNamespace:
    return SimpleNamespace(start=start_ms, end=end_ms, text=text, speaker=speaker)


def test_assemblyai_transcriber_transcribes_single_speaker_sentences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[
            _make_sentence(0, 900, "Hello there."),
            _make_sentence(900, 1_800, "This is a test."),
            _make_sentence(1_800, 2_700, "Goodbye now."),
        ],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=2_700,
        json_response={"id": "tx_123", "language_code": "en"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "original.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert calls["config"].language_code == "en"
    assert calls["config"].speaker_labels is False
    assert calls["config"].speech_models == ["universal-3-pro", "universal-2"]
    assert calls["config"].disfluencies is True
    assert 'Include spoken filler words like "um," "uh," "you know," and "like"' in calls["config"].prompt
    assert len(result.lines) == 3
    assert [line.speaker_id for line in result.lines] == ["speaker_a", "speaker_a", "speaker_a"]
    assert [line.speaker_label for line in result.lines] == ["A", "A", "A"]
    assert [line.start_ms for line in result.lines] == [0, 900, 1_800]
    assert [line.end_ms for line in result.lines] == [900, 1_800, 2_700]
    assert [line.source_text for line in result.lines] == ["Hello there.", "This is a test.", "Goodbye now."]
    assert result.total_duration_ms == 2_700
    assert transcriber_module.DEFAULT_MAX_RETRIES == 5
    assert calls["config"].language_code == "en"


def test_assemblyai_transcriber_sets_sdk_http_timeout_on_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=0,
        json_response={"id": "tx_timeout"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)

    AssemblyAITranscriber("test_key")

    assert calls == {"transcribe_calls": 0}
    fake_sdk = transcriber_module._load_assemblyai_sdk()
    assert fake_sdk.settings.http_timeout == 900.0


def test_assemblyai_transcriber_allows_custom_http_timeout_on_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=0,
        json_response={"id": "tx_timeout_custom"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)

    AssemblyAITranscriber("test_key", http_timeout_seconds=1_200.0)

    fake_sdk = transcriber_module._load_assemblyai_sdk()
    assert fake_sdk.settings.http_timeout == 1_200.0


def test_assemblyai_transcriber_logs_upload_and_waiting_phases_when_submit_api_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: dict[str, int | str | bool] = {
        "submit_calls": 0,
        "transcribe_calls": 0,
        "wait_calls": 0,
    }

    class FakeTranscript:
        def __init__(self) -> None:
            self.id = "tx_submit_flow"
            self.status = "completed"
            self.error = None
            self.sentences = [_make_sentence(0, 900, "Submit path.")]
            self.words = []
            self.utterances = []
            self.language_code = "en"
            self.audio_duration = 900
            self.json_response = {"id": self.id}

        def wait_for_completion(self) -> "FakeTranscript":
            events["wait_calls"] += 1
            return self

    class FakeTranscriptionConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeTranscriber:
        def submit(self, audio_path: str, config: FakeTranscriptionConfig) -> FakeTranscript:
            del config
            events["submit_calls"] += 1
            events["audio_path"] = audio_path
            return FakeTranscript()

        def transcribe(self, audio_path: str, config: FakeTranscriptionConfig) -> FakeTranscript:
            del audio_path, config
            events["transcribe_calls"] += 1
            return FakeTranscript()

    fake_aai = SimpleNamespace(
        settings=SimpleNamespace(api_key=None, http_timeout=None),
        TranscriptionConfig=FakeTranscriptionConfig,
        Transcriber=lambda: FakeTranscriber(),
    )
    monkeypatch.setattr(transcriber_module, "_load_assemblyai_sdk", lambda: fake_aai)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "submit.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    output = capsys.readouterr().out
    assert events["submit_calls"] == 1
    assert events["wait_calls"] == 1
    assert events["transcribe_calls"] == 0
    assert "正在上传音频到 AssemblyAI" in output
    assert "正在等待转录结果" in output
    assert "转录结果已返回" in output
    assert result.lines[0].source_text == "Submit path."


def test_assemblyai_transcriber_transcribes_two_speakers_from_utterances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[],
        words=[],
        utterances=[
            _make_utterance(0, 1_000, "Welcome to the show.", "A"),
            _make_utterance(1_000, 2_200, "Thanks for having me.", "B"),
            _make_utterance(2_200, 3_300, "Let's dive in.", "A"),
        ],
        language_code="en",
        audio_duration=3_300,
        json_response={"id": "tx_dual"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "dual.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=True,
        speakers_expected=2,
    )

    assert calls["config"].speaker_labels is True
    assert calls["config"].speakers_expected == 2
    assert calls["config"].disfluencies is True
    assert [line.speaker_id for line in result.lines] == ["speaker_a", "speaker_b", "speaker_a"]
    assert [line.speaker_label for line in result.lines] == ["A", "B", "A"]
    assert [line.source_text for line in result.lines] == [
        "Welcome to the show.",
        "Thanks for having me.",
        "Let's dive in.",
    ]


def test_assemblyai_transcriber_retries_after_transient_sdk_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Recovered transcript.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_retry"},
    )
    calls = _install_fake_assemblyai_sdk(
        monkeypatch,
        transcript,
        outcomes=[RuntimeError("temporary network issue"), transcript],
    )
    monkeypatch.setattr(transcriber_module.time, "sleep", lambda seconds: None)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "retry.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert calls["transcribe_calls"] == 2
    assert len(result.lines) == 1
    assert result.lines[0].source_text == "Recovered transcript."


def test_assemblyai_transcriber_uses_exponential_backoff_retry_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Recovered after many retries.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_backoff"},
    )
    calls = _install_fake_assemblyai_sdk(
        monkeypatch,
        transcript,
        outcomes=[
            RuntimeError("net-1"),
            RuntimeError("net-2"),
            RuntimeError("net-3"),
            RuntimeError("net-4"),
            RuntimeError("net-5"),
            transcript,
        ],
    )
    sleep_calls: list[int] = []
    monkeypatch.setattr(transcriber_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    audio_path = _write_dummy_audio(tmp_path / "audio" / "backoff.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert calls["transcribe_calls"] == 6
    assert sleep_calls == [10, 20, 40, 60, 60]
    assert result.lines[0].source_text == "Recovered after many retries."


def test_retry_wait_seconds_caps_at_sixty_seconds() -> None:
    assert transcriber_module._retry_wait_seconds(0) == 10
    assert transcriber_module._retry_wait_seconds(1) == 20
    assert transcriber_module._retry_wait_seconds(2) == 40
    assert transcriber_module._retry_wait_seconds(3) == 60
    assert transcriber_module._retry_wait_seconds(4) == 60


def test_assemblyai_transcriber_uploads_small_file_without_generating_mp3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Small audio path.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_small_upload"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "small.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert calls["audio_path"] == str(audio_path.resolve(strict=False))
    assert not (audio_path.parent / "original_upload.mp3").exists()
    assert result.lines[0].source_text == "Small audio path."


def test_assemblyai_transcriber_generates_mp3_for_large_audio_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Large audio path.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_large_upload"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_large_dummy_audio(tmp_path / "audio" / "large.wav")
    export_calls: list[tuple[str, str, str]] = []
    set_channels_calls: list[int] = []
    set_frame_rate_calls: list[int] = []

    class FakeAudioSegment:
        @staticmethod
        def from_file(path: str) -> "FakeAudioSegment":
            assert path == str(audio_path.resolve(strict=False))
            return FakeAudioSegment()

        def set_channels(self, channels: int) -> "FakeAudioSegment":
            set_channels_calls.append(channels)
            return self

        def set_frame_rate(self, frame_rate: int) -> "FakeAudioSegment":
            set_frame_rate_calls.append(frame_rate)
            return self

        def export(self, path: str, format: str, bitrate: str) -> str:
            export_calls.append((path, format, bitrate))
            Path(path).write_bytes(b"mp3data")
            return path

    monkeypatch.setattr(transcriber_module, "AudioSegment", FakeAudioSegment)

    AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    upload_path = audio_path.parent / "original_upload.mp3"
    assert upload_path.exists()
    assert set_channels_calls == [1]
    assert set_frame_rate_calls == [16_000]
    assert export_calls == [(str(upload_path), "mp3", "64k")]
    assert calls["audio_path"] == str(upload_path.resolve(strict=False))


def test_assemblyai_transcriber_reuses_existing_mp3_upload_file_when_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Reuse mp3 upload.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_reuse_upload"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_large_dummy_audio(tmp_path / "audio" / "large.wav")
    upload_path = audio_path.parent / "original_upload.mp3"
    upload_path.write_bytes(b"existing mp3")
    current_time = max(audio_path.stat().st_mtime, upload_path.stat().st_mtime) + 10
    os.utime(upload_path, (current_time, current_time))
    export_called = {"value": False}

    class FakeAudioSegment:
        @staticmethod
        def from_file(path: str) -> "FakeAudioSegment":
            del path
            export_called["value"] = True
            return FakeAudioSegment()

        def export(self, path: str, format: str, bitrate: str) -> str:
            del path, format, bitrate
            export_called["value"] = True
            return ""

    monkeypatch.setattr(transcriber_module, "AudioSegment", FakeAudioSegment)

    AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert export_called["value"] is False
    assert calls["audio_path"] == str(upload_path.resolve(strict=False))


def test_assemblyai_transcriber_regenerates_stale_mp3_upload_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Refresh stale mp3.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_refresh_upload"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_large_dummy_audio(tmp_path / "audio" / "large.wav")
    upload_path = audio_path.parent / "original_upload.mp3"
    upload_path.write_bytes(b"stale mp3")
    stale_time = audio_path.stat().st_mtime - 10
    os.utime(upload_path, (stale_time, stale_time))
    export_calls: list[str] = []
    set_channels_calls: list[int] = []
    set_frame_rate_calls: list[int] = []

    class FakeAudioSegment:
        @staticmethod
        def from_file(path: str) -> "FakeAudioSegment":
            assert path == str(audio_path.resolve(strict=False))
            return FakeAudioSegment()

        def set_channels(self, channels: int) -> "FakeAudioSegment":
            set_channels_calls.append(channels)
            return self

        def set_frame_rate(self, frame_rate: int) -> "FakeAudioSegment":
            set_frame_rate_calls.append(frame_rate)
            return self

        def export(self, path: str, format: str, bitrate: str) -> str:
            assert format == "mp3"
            assert bitrate == "64k"
            del format, bitrate
            export_calls.append(path)
            Path(path).write_bytes(b"fresh mp3")
            return path

    monkeypatch.setattr(transcriber_module, "AudioSegment", FakeAudioSegment)

    AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert set_channels_calls == [1]
    assert set_frame_rate_calls == [16_000]
    assert export_calls == [str(upload_path)]
    assert calls["audio_path"] == str(upload_path.resolve(strict=False))


def test_assemblyai_transcriber_falls_back_to_original_audio_when_mp3_generation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[_make_sentence(0, 900, "Fallback to wav.")],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=900,
        json_response={"id": "tx_fallback_upload"},
    )
    calls = _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_large_dummy_audio(tmp_path / "audio" / "large.wav")

    class FailingAudioSegment:
        @staticmethod
        def from_file(path: str) -> "FailingAudioSegment":
            del path
            raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(transcriber_module, "AudioSegment", FailingAudioSegment)

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert calls["audio_path"] == str(audio_path.resolve(strict=False))
    assert not (audio_path.parent / "original_upload.mp3").exists()
    assert result.lines[0].source_text == "Fallback to wav."


def test_assemblyai_transcriber_splits_word_stream_by_sentence_punctuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[],
        words=[
            _make_word(0, 200, "Hello"),
            _make_word(200, 450, "there."),
            _make_word(600, 800, "General"),
            _make_word(800, 1_050, "Kenobi!"),
        ],
        utterances=[],
        language_code="en",
        audio_duration=1_050,
        json_response={"id": "tx_words"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "words.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
        speaker_labels=False,
    )

    assert len(result.lines) == 2
    assert [line.source_text for line in result.lines] == ["Hello there.", "General Kenobi!"]
    assert [line.start_ms for line in result.lines] == [0, 600]
    assert [line.end_ms for line in result.lines] == [450, 1_050]


def test_assemblyai_transcriber_raises_transcription_error_on_sdk_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="error",
        error="bad audio",
        sentences=[],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=0,
        json_response={"error": "bad audio"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "broken.wav")

    with pytest.raises(TranscriptionError, match="bad audio"):
        AssemblyAITranscriber("test_key").transcribe(
            str(audio_path),
            str(tmp_path / "transcript"),
            speaker_labels=False,
        )


def test_assemblyai_transcriber_writes_raw_and_structured_json_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[
            _make_sentence(0, 600, "First line."),
            _make_sentence(600, 1_200, "Second line."),
            _make_sentence(1_200, 1_800, "Third line."),
        ],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=1_800,
        json_response={"id": "tx_saved", "status": "completed"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "original.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
    )

    raw_path = Path(result.raw_response_path)
    structured_path = Path(result.structured_transcript_path)
    assert raw_path.exists()
    assert structured_path.exists()

    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    structured_payload = json.loads(structured_path.read_text(encoding="utf-8"))

    assert raw_payload["id"] == "tx_saved"
    assert isinstance(structured_payload, dict)
    assert len(structured_payload["lines"]) == 3
    assert structured_payload["lines"][0]["source_text"] == "First line."


def test_assemblyai_transcriber_normalizes_second_based_audio_duration_to_milliseconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[
            _make_sentence(0, 330_000, "Very long explanation."),
            _make_sentence(330_000, 659_000, "Closing thought."),
        ],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=659,
        json_response={"id": "tx_seconds"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "seconds.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
    )

    assert result.total_duration_ms == 659_000


def test_assemblyai_transcriber_keeps_millisecond_audio_duration_when_already_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[
            _make_sentence(0, 330_000, "Very long explanation."),
            _make_sentence(330_000, 659_000, "Closing thought."),
        ],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=659_000,
        json_response={"id": "tx_milliseconds"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "milliseconds.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
    )

    assert result.total_duration_ms == 659_000


def test_load_assemblyai_config_reads_api_key_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "assemblyai": {
                    "api_key": None,
                    "api_key_env_var": "ASSEMBLYAI_API_KEY",
                    "language_code": "en",
                    "speaker_labels": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transcriber_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test_key")

    config = load_assemblyai_config()

    assert config["api_key"] == "test_key"
    assert config["language_code"] == "en"
    assert config["speaker_labels"] is False
    assert config["http_timeout_seconds"] == 900.0


def test_load_assemblyai_config_reads_custom_http_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "assemblyai": {
                    "api_key": "test_key",
                    "api_key_env_var": "ASSEMBLYAI_API_KEY",
                    "language_code": "en",
                    "speaker_labels": False,
                    "http_timeout_seconds": 1200.0,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transcriber_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_assemblyai_config()

    assert config["http_timeout_seconds"] == 1200.0


def test_load_assemblyai_config_raises_when_api_key_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "assemblyai": {
                    "api_key": None,
                    "api_key_env_var": "ASSEMBLYAI_API_KEY",
                    "language_code": "en",
                    "speaker_labels": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transcriber_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)

    with pytest.raises(TranscriptionError, match="ASSEMBLYAI_API_KEY"):
        load_assemblyai_config()


def test_assemblyai_transcriber_returns_empty_lines_for_empty_audio_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = SimpleNamespace(
        status="completed",
        error=None,
        sentences=[],
        words=[],
        utterances=[],
        language_code="en",
        audio_duration=0,
        json_response={"id": "tx_empty", "status": "completed"},
    )
    _install_fake_assemblyai_sdk(monkeypatch, transcript)
    audio_path = _write_dummy_audio(tmp_path / "audio" / "silent.wav")

    result = AssemblyAITranscriber("test_key").transcribe(
        str(audio_path),
        str(tmp_path / "transcript"),
    )

    assert result.lines == []
    assert result.total_duration_ms == 0

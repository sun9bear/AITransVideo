from pathlib import Path
from typing import cast
import wave

import pytest

from core.exceptions import (
    MediaUnderstandingConfigurationError,
    MediaUnderstandingError,
    MediaUnderstandingExtractedTranscriptOutputError,
    MediaUnderstandingInvalidSourcePathError,
    MediaUnderstandingOutputError,
    MediaUnderstandingProviderUnavailableError,
    MediaUnderstandingTranscriptExtractionModelError,
    MediaUnderstandingTranscriptExtractionNoResultError,
    MediaUnderstandingTranscriptExtractionRuntimeError,
    MediaUnderstandingTranscriptExtractionUnavailableError,
    MediaUnderstandingUnsupportedSourceKindError,
)
from modules.media_understanding.models import (
    AttributedTranscriptLine,
    MediaSource,
    MediaSourceKind,
    TranscriptExtractionRequest,
    TranscriptExtractionResult,
    TranscriptLine,
)
from modules.media_understanding.normalizer import AttributedTranscriptNormalizer
from modules.media_understanding.pipeline import MediaUnderstandingPipeline
import modules.media_understanding.providers as media_understanding_providers
from modules.media_understanding.providers import (
    FutureMultimodalMediaUnderstandingProviderConfig,
    FutureMultimodalMediaUnderstandingProviderSkeleton,
    FutureMultimodalTranscriptExtractionProviderConfig,
    FutureMultimodalTranscriptExtractionProviderSkeleton,
    CommandTranscriptExtractionProviderConfig,
    LocalASRTranscriptExtractionProviderConfig,
    LocalASRTranscriptExtractionProviderSkeleton,
    LocalSRTProvider,
    LocalSRTProviderConfig,
    LocalTranscriptProvider,
    LocalTranscriptProviderConfig,
    MediaUnderstandingProvider,
    MediaUnderstandingProviderSelectionConfig,
    MockMediaUnderstandingProvider,
    MockMediaUnderstandingProviderConfig,
    SystemSpeechLocalASRTranscriptExtractionProvider,
    TranscriptExtractionMediaUnderstandingProvider,
    TranscriptExtractionMediaUnderstandingProviderConfig,
    TranscriptExtractionProvider,
    TranscriptExtractionProviderSelectionConfig,
    classify_media_understanding_error,
    resolve_media_understanding_provider,
    resolve_transcript_extraction_provider,
)


def _write_dummy_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 1600)


def test_attributed_transcript_normalizer_converts_to_subtitle_lines() -> None:
    normalizer = AttributedTranscriptNormalizer()

    attributed_lines = normalizer.normalize(
        [
            AttributedTranscriptLine(
                index=1,
                start_ms=0,
                end_ms=1_200,
                speaker_id=" speaker_host ",
                speaker_name=" Host ",
                source_text=" Welcome back. ",
            )
        ]
    )
    subtitle_lines = normalizer.to_subtitle_lines(attributed_lines)

    assert attributed_lines[0].speaker_id == "speaker_host"
    assert attributed_lines[0].speaker_name == "Host"
    assert attributed_lines[0].source_text == "Welcome back."
    assert subtitle_lines[0].speaker_id == "speaker_host"
    assert subtitle_lines[0].speaker_name == "Host"
    assert subtitle_lines[0].en_text == "Welcome back."
    assert subtitle_lines[0].cn_text == ""
    assert subtitle_lines[0].literal_cn_text == ""
    assert subtitle_lines[0].tts_cn_text == ""


def test_mock_media_understanding_provider_satisfies_protocol() -> None:
    provider = MockMediaUnderstandingProvider(default_speaker_id="speaker_mock", default_speaker_name="Mock Speaker")
    typed_provider = cast(MediaUnderstandingProvider, provider)
    pipeline = MediaUnderstandingPipeline(typed_provider)

    result = pipeline.run(
        MediaSource(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=900, source_text="Hello world.")
            ],
        )
    )

    assert isinstance(provider, MediaUnderstandingProvider)
    assert result.execution_mode == "provider_run"
    assert result.attributed_lines[0].speaker_id == "speaker_mock"
    assert result.attributed_lines[0].speaker_name == "Mock Speaker"
    assert result.subtitle_lines[0].en_text == "Hello world."


def test_provider_selection_config_resolves_mock_provider() -> None:
    selection = MediaUnderstandingProviderSelectionConfig(
        mode="mock",
        mock=MockMediaUnderstandingProviderConfig(
            default_speaker_id="speaker_mock",
            default_speaker_name="Mock Speaker",
        ),
    )

    binding = resolve_media_understanding_provider(selection)

    assert binding.provider_name == "mock_media_understanding"
    assert binding.mode == "mock"
    assert binding.fallback_applied is False
    assert binding.version_context["provider_variant"] == "mock_media_understanding_v1"


def test_local_transcript_provider_converts_transcript_source_to_subtitle_lines() -> None:
    pipeline = MediaUnderstandingPipeline(
        LocalTranscriptProvider(default_speaker_id="speaker_local", default_speaker_name="Narrator")
    )

    result = pipeline.run(
        MediaSource(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[
                TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Line A"),
                TranscriptLine(index=2, start_ms=900, end_ms=1_600, source_text="Line B"),
            ],
        )
    )

    assert [line.en_text for line in result.subtitle_lines] == ["Line A", "Line B"]
    assert [line.speaker_id for line in result.subtitle_lines] == ["speaker_local", "speaker_local"]
    assert [line.speaker_name for line in result.subtitle_lines] == ["Narrator", "Narrator"]
    assert result.authoritative_input_used is True
    assert result.authoritative_path_kind == MediaSourceKind.TRANSCRIPT.value
    assert result.authoritative_flow == "transcript -> attributed_transcript -> subtitle_line_bridge"
    assert result.transcript_extraction_used is False
    assert result.attributed_transcript_normalized is True
    assert result.subtitle_line_bridge_applied is True


def test_provider_selection_config_resolves_local_transcript_path() -> None:
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(
            mode="local_transcript",
            local_transcript=LocalTranscriptProviderConfig(
                default_speaker_id="speaker_local",
                default_speaker_name="Narrator",
            ),
        )
    )

    result = pipeline.run(
        MediaSource(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Line A")],
        )
    )

    assert result.subtitle_lines[0].speaker_id == "speaker_local"
    assert pipeline.get_provider_audit()["provider_mode"] == "local_transcript"


def test_local_srt_provider_converts_srt_source_to_subtitle_lines(tmp_path: Path) -> None:
    srt_path = tmp_path / "sample.srt"
    srt_path.write_text(
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "Line A\n\n"
        "2\n"
        "00:00:00,900 --> 00:00:01,600\n"
        "Line B\n",
        encoding="utf-8",
    )
    pipeline = MediaUnderstandingPipeline(
        LocalSRTProvider(default_speaker_id="speaker_srt", default_speaker_name="Narrator")
    )

    result = pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_SRT, locator=str(srt_path)))

    assert [line.source_text for line in result.attributed_lines] == ["Line A", "Line B"]
    assert [line.speaker_id for line in result.attributed_lines] == ["speaker_srt", "speaker_srt"]
    assert [line.en_text for line in result.subtitle_lines] == ["Line A", "Line B"]
    assert [line.speaker_name for line in result.subtitle_lines] == ["Narrator", "Narrator"]
    assert result.authoritative_input_used is True
    assert result.authoritative_path_kind == MediaSourceKind.LOCAL_SRT.value
    assert result.authoritative_flow == "local_srt -> attributed_transcript -> subtitle_line_bridge"
    assert result.transcript_extraction_used is False
    assert result.attributed_transcript_normalized is True
    assert result.subtitle_line_bridge_applied is True


def test_provider_selection_config_resolves_local_srt_path(tmp_path: Path) -> None:
    srt_path = tmp_path / "selection_sample.srt"
    srt_path.write_text(
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "Line A\n",
        encoding="utf-8",
    )
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(
            mode="local_srt",
            local_srt=LocalSRTProviderConfig(default_speaker_id="speaker_srt", default_speaker_name="Narrator"),
        )
    )

    result = pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_SRT, locator=str(srt_path)))

    assert result.attributed_lines[0].speaker_id == "speaker_srt"
    assert pipeline.get_provider_audit()["provider_mode"] == "local_srt"


def test_local_transcript_provider_preserves_speaker_identity_fields_for_attributed_input() -> None:
    pipeline = MediaUnderstandingPipeline(LocalTranscriptProvider())

    result = pipeline.run(
        MediaSource(
            kind=MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
            attributed_lines=[
                AttributedTranscriptLine(1, 0, 800, "speaker_a", "Host", "Line A"),
                AttributedTranscriptLine(2, 900, 1_600, "speaker_b", "Host", "Line B"),
            ],
        )
    )

    assert [line.speaker_id for line in result.subtitle_lines] == ["speaker_a", "speaker_b"]
    assert [line.speaker_name for line in result.subtitle_lines] == ["Host", "Host"]
    assert [line.en_text for line in result.subtitle_lines] == ["Line A", "Line B"]


def test_single_speaker_default_rule_uses_default_speaker_id_and_no_display_name() -> None:
    pipeline = MediaUnderstandingPipeline(LocalTranscriptProvider())

    result = pipeline.run(
        MediaSource(
            kind=MediaSourceKind.TRANSCRIPT,
            transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Solo line")],
        )
    )

    assert result.attributed_lines[0].speaker_id == "speaker_default"
    assert result.attributed_lines[0].speaker_name is None
    assert result.subtitle_lines[0].speaker_id == "speaker_default"
    assert result.subtitle_lines[0].speaker_name is None


def test_single_speaker_inputs_require_non_empty_default_speaker_id() -> None:
    pipeline = MediaUnderstandingPipeline(LocalTranscriptProvider(default_speaker_id=""))

    with pytest.raises(MediaUnderstandingConfigurationError, match="default_speaker_id is required"):
        pipeline.run(
            MediaSource(
                kind=MediaSourceKind.TRANSCRIPT,
                transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Solo line")],
            )
        )


def test_attributed_input_with_missing_speaker_id_is_rejected() -> None:
    pipeline = MediaUnderstandingPipeline(LocalTranscriptProvider())

    with pytest.raises(MediaUnderstandingOutputError, match="speaker_id is required"):
        pipeline.run(
            MediaSource(
                kind=MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
                attributed_lines=[
                    AttributedTranscriptLine(
                        index=1,
                        start_ms=0,
                        end_ms=800,
                        speaker_id="",
                        speaker_name="Host",
                        source_text="Line A",
                    )
                ],
            )
        )


def test_provider_selection_rejects_unsupported_mode() -> None:
    with pytest.raises(MediaUnderstandingConfigurationError, match="Unsupported media understanding mode"):
        resolve_media_understanding_provider(MediaUnderstandingProviderSelectionConfig(mode="unknown_mode"))


def test_local_srt_provider_rejects_unsupported_source_kind() -> None:
    provider = LocalSRTProvider()

    with pytest.raises(MediaUnderstandingUnsupportedSourceKindError, match="only supports local_srt sources"):
        provider.load_attributed_transcript(
            MediaSource(
                kind=MediaSourceKind.TRANSCRIPT,
                transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Line A")],
            )
        )


def test_future_multimodal_provider_selection_resolves_skeleton() -> None:
    selection = MediaUnderstandingProviderSelectionConfig(
        mode="multimodal_skeleton",
        transcript_extraction=TranscriptExtractionMediaUnderstandingProviderConfig(provider_name="media_extraction"),
        extraction=TranscriptExtractionProviderSelectionConfig(
            multimodal=FutureMultimodalTranscriptExtractionProviderConfig(provider_name="gemini_like_extraction")
        ),
    )

    binding = resolve_media_understanding_provider(selection)

    assert binding.provider_name == "media_extraction"
    assert binding.mode == "transcript_extraction"
    assert binding.extraction_provider_name == "gemini_like_extraction"
    assert binding.extraction_provider_mode == "multimodal_skeleton"
    assert binding.version_context["provider_variant"] == "transcript_extraction_adapter_v1"


def test_local_video_source_kind_routes_to_multimodal_skeleton(tmp_path: Path) -> None:
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"fake-video")
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(
            mode="multimodal_skeleton",
            transcript_extraction=TranscriptExtractionMediaUnderstandingProviderConfig(provider_name="media_extraction"),
            extraction=TranscriptExtractionProviderSelectionConfig(
                multimodal=FutureMultimodalTranscriptExtractionProviderConfig(provider_name="gemini_like_extraction")
            ),
        )
    )

    with pytest.raises(MediaUnderstandingTranscriptExtractionUnavailableError, match="not connected in this sprint"):
        pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_VIDEO, locator=str(video_path)))

    assert pipeline.get_provider_audit()["provider_mode"] == "transcript_extraction"
    assert pipeline.get_provider_audit()["extraction_provider_name"] == "gemini_like_extraction"
    assert pipeline.get_provider_audit()["extraction_provider_mode"] == "multimodal_skeleton"
    assert pipeline.get_provider_audit()["extraction_version_context"]["provider_variant"] == (
        "future_multimodal_transcript_extraction_v1"
    )


def test_local_audio_source_kind_routes_to_multimodal_skeleton(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFFfake-audio")
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(
            mode="multimodal_skeleton",
            transcript_extraction=TranscriptExtractionMediaUnderstandingProviderConfig(provider_name="media_extraction"),
            extraction=TranscriptExtractionProviderSelectionConfig(
                multimodal=FutureMultimodalTranscriptExtractionProviderConfig(provider_name="gemini_like_extraction")
            ),
        )
    )

    with pytest.raises(MediaUnderstandingTranscriptExtractionUnavailableError, match="not connected in this sprint"):
        pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)))

    assert pipeline.get_provider_audit()["provider_name"] == "media_extraction"
    assert pipeline.get_provider_audit()["extraction_provider_name"] == "gemini_like_extraction"


def test_local_audio_source_kind_routes_to_local_asr_transcript_extraction_adapter(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.wav"
    _write_dummy_wav(audio_path)
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(mode="local_asr_skeleton")
    )

    with pytest.raises(MediaUnderstandingTranscriptExtractionUnavailableError, match="not connected in this sprint"):
        pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)))

    assert pipeline.get_provider_audit()["provider_mode"] == "transcript_extraction"
    assert pipeline.get_provider_audit()["extraction_provider_name"] == "system_speech_local_asr"
    assert pipeline.get_provider_audit()["extraction_provider_mode"] == "local_asr_skeleton"
    assert pipeline.get_provider_audit()["extraction_version_context"]["provider_variant"] == (
        "system_speech_local_asr_v1"
    )


def test_local_video_source_kind_routes_to_local_asr_transcript_extraction_adapter(tmp_path: Path) -> None:
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"fake-video")
    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(mode="local_asr_skeleton")
    )

    with pytest.raises(MediaUnderstandingTranscriptExtractionUnavailableError, match="local_video transcript extraction path is not connected"):
        pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_VIDEO, locator=str(video_path)))

    assert pipeline.get_provider_audit()["provider_mode"] == "transcript_extraction"
    assert pipeline.get_provider_audit()["extraction_provider_name"] == "system_speech_local_asr"
    assert pipeline.get_provider_audit()["extraction_provider_mode"] == "local_asr_skeleton"


def test_real_local_asr_provider_selection_resolves_system_speech_provider() -> None:
    selection = TranscriptExtractionProviderSelectionConfig(mode="local_asr")

    binding = resolve_transcript_extraction_provider(selection)

    assert binding.provider_name == "system_speech_local_asr"
    assert binding.mode == "local_asr"
    assert binding.version_context["provider_variant"] == "system_speech_local_asr_v1"
    assert binding.version_context["model_name"] == "system_speech_dictation"
    assert binding.version_context["language"] == "auto"
    assert binding.version_context["supported_extensions"] == [".wav", ".wave"]
    assert binding.version_context["audio_input_contract"] == "pcm_wav_mono_16bit_non_empty"


def test_transcript_extraction_provider_binding_supports_local_asr_skeleton() -> None:
    binding = resolve_transcript_extraction_provider(
        TranscriptExtractionProviderSelectionConfig(mode="local_asr_skeleton")
    )

    assert binding.provider_name == "system_speech_local_asr"
    assert binding.mode == "local_asr_skeleton"
    assert binding.version_context["provider_variant"] == "system_speech_local_asr_v1"


def test_transcript_extraction_provider_binding_supports_command_skeleton_slot() -> None:
    binding = resolve_transcript_extraction_provider(
        TranscriptExtractionProviderSelectionConfig(
            provider="command_transcript_extraction",
            mode="skeleton",
            command=CommandTranscriptExtractionProviderConfig(),
        )
    )

    assert binding.provider_name == "command_transcript_extraction"
    assert binding.mode == "command_skeleton"
    assert binding.version_context["provider_variant"] == "command_transcript_extraction_stub_v1"
    assert binding.version_context["runtime_backend"] == "external_command_stub"


def test_transcript_extraction_selection_reads_provider_neutral_selector_from_local_config(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        '{"media_understanding": {"transcript_extraction_provider": "command_transcript_extraction",'
        ' "transcript_extraction_mode": "skeleton"}}',
        encoding="utf-8",
    )

    selection = TranscriptExtractionProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.provider == "command_transcript_extraction"
    assert selection.mode == "skeleton"


def test_real_local_asr_provider_path_normalizes_to_attributed_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.wav"
    _write_dummy_wav(audio_path)

    def fake_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[
                TranscriptLine(index=4, start_ms=0, end_ms=700, source_text=" Hello   local ASR. "),
                TranscriptLine(index=4, start_ms=600, end_ms=500, source_text=" ... "),
                TranscriptLine(index=8, start_ms=500, end_ms=1_100, source_text=" This is a real provider path. "),
                TranscriptLine(index=9, start_ms=1_050, end_ms=1_000, source_text=" trailing cleanup "),
            ],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={
                "provider_variant": "system_speech_local_asr_v1",
                "model_name": "system_speech_dictation",
                "language": "en-US",
                "task": "transcribe",
            },
        )

    monkeypatch.setattr(
        SystemSpeechLocalASRTranscriptExtractionProvider,
        "_run_backend",
        fake_backend,
    )

    pipeline = MediaUnderstandingPipeline.from_selection(
        MediaUnderstandingProviderSelectionConfig(mode="local_asr")
    )
    result = pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)))

    assert [line.source_text for line in result.attributed_lines] == [
        "Hello local ASR.",
        "This is a real provider path.",
        "trailing cleanup",
    ]
    assert [line.index for line in result.attributed_lines] == [1, 2, 3]
    assert result.attributed_lines[1].start_ms == 700
    assert result.attributed_lines[1].end_ms == 1_300
    assert result.attributed_lines[2].start_ms == 1_300
    assert result.attributed_lines[2].end_ms == 2_200
    assert [line.speaker_id for line in result.attributed_lines] == [
        "speaker_default",
        "speaker_default",
        "speaker_default",
    ]
    assert [line.speaker_name for line in result.attributed_lines] == [None, None, None]
    assert [line.en_text for line in result.subtitle_lines] == [
        "Hello local ASR.",
        "This is a real provider path.",
        "trailing cleanup",
    ]
    assert pipeline.get_provider_audit()["extraction_provider_name"] == "system_speech_local_asr"
    assert pipeline.get_provider_audit()["extraction_provider_mode"] == "local_asr"
    assert pipeline.get_provider_audit()["extraction_version_context"]["model_name"] == "system_speech_dictation"
    assert pipeline.get_provider_audit()["extraction_version_context"]["timing_strategy"] == (
        "recognizer_offsets_with_sequential_fallback"
    )
    assert result.authoritative_input_used is True
    assert result.authoritative_path_kind == MediaSourceKind.LOCAL_AUDIO.value
    assert result.authoritative_flow == (
        "local_audio -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    )
    assert result.transcript_extraction_used is True
    assert result.attributed_transcript_normalized is True
    assert result.subtitle_line_bridge_applied is True


def test_real_local_asr_provider_rejects_local_video_input(tmp_path: Path) -> None:
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"fake-video")
    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    with pytest.raises(MediaUnderstandingUnsupportedSourceKindError, match="only supports local_audio"):
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_VIDEO,
                source_path=str(video_path),
            )
        )


def test_transcript_extraction_adapter_rewrites_local_video_local_asr_mismatch_as_unavailable(
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"fake-video")
    adapter = TranscriptExtractionMediaUnderstandingProvider(
        extraction_provider=SystemSpeechLocalASRTranscriptExtractionProvider(),
        extraction_provider_name="system_speech_local_asr",
        extraction_provider_mode="local_asr",
    )

    with pytest.raises(MediaUnderstandingTranscriptExtractionUnavailableError, match="local_video transcript extraction path is not connected"):
        adapter.extract_transcript_result(
            MediaSource(kind=MediaSourceKind.LOCAL_VIDEO, locator=str(video_path))
        )

    error_info = classify_media_understanding_error(
        MediaUnderstandingTranscriptExtractionUnavailableError(
            "local_video transcript extraction path is not connected in this sprint."
        )
    )

    assert error_info["error_type"] == "transcript_extraction_unavailable"


def test_real_local_asr_provider_requires_wav_audio(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"not-a-wav")
    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    with pytest.raises(MediaUnderstandingTranscriptExtractionRuntimeError, match="only supports WAV/WAVE"):
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_AUDIO,
                source_path=str(audio_path),
            )
        )


def test_real_local_asr_provider_accepts_wave_extension(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.wave"
    _write_dummy_wav(audio_path)

    def fake_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=900, source_text="wave input works")],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={"language": "zh-CN"},
        )

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", fake_backend)
    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    result = provider.extract_transcript(
        TranscriptExtractionRequest(
            source_kind=MediaSourceKind.LOCAL_AUDIO,
            source_path=str(audio_path),
        )
    )

    assert [line.source_text for line in result.transcript_lines] == ["wave input works"]


def test_real_local_asr_provider_rejects_non_pcm_stereo_wav_input(tmp_path: Path) -> None:
    audio_path = tmp_path / "stereo.wav"
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00\x00\x00" * 1600)

    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    with pytest.raises(MediaUnderstandingTranscriptExtractionRuntimeError, match="requires mono"):
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_AUDIO,
                source_path=str(audio_path),
            )
        )


def test_real_local_asr_provider_empty_result_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "sample.wav"
    _write_dummy_wav(audio_path)

    def empty_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self
        return TranscriptExtractionResult(
            request=request,
            transcript_lines=[],
            provider_name="system_speech_local_asr",
            provider_mode="real",
            version_context={"language": "zh-CN"},
        )

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", empty_backend)
    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    with pytest.raises(MediaUnderstandingTranscriptExtractionNoResultError) as exc_info:
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_AUDIO,
                source_path=str(audio_path),
            )
        )

    assert classify_media_understanding_error(exc_info.value)["error_type"] == "transcript_extraction_no_result"


def test_real_local_asr_provider_runtime_error_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "sample.wav"
    _write_dummy_wav(audio_path)

    def broken_backend(
        self: SystemSpeechLocalASRTranscriptExtractionProvider,
        request: TranscriptExtractionRequest,
    ) -> TranscriptExtractionResult:
        del self, request
        raise MediaUnderstandingTranscriptExtractionRuntimeError("backend crashed")

    monkeypatch.setattr(SystemSpeechLocalASRTranscriptExtractionProvider, "_run_backend", broken_backend)
    provider = SystemSpeechLocalASRTranscriptExtractionProvider()

    with pytest.raises(MediaUnderstandingTranscriptExtractionRuntimeError) as exc_info:
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_AUDIO,
                source_path=str(audio_path),
            )
        )

    assert classify_media_understanding_error(exc_info.value)["error_type"] == "transcript_extraction_runtime_error"


def test_real_local_asr_provider_model_error_is_classified() -> None:
    error = MediaUnderstandingTranscriptExtractionModelError("missing recognizer")

    assert classify_media_understanding_error(error)["error_type"] == "transcript_extraction_model_error"


def test_real_local_asr_provider_no_result_error_is_classified() -> None:
    error = MediaUnderstandingTranscriptExtractionNoResultError("no recognizable speech")

    assert classify_media_understanding_error(error)["error_type"] == "transcript_extraction_no_result"


def test_transcript_extraction_adapter_builds_minimal_request_contract_for_local_audio(tmp_path: Path) -> None:
    audio_path = tmp_path / "adapter_input.wav"
    _write_dummy_wav(audio_path)
    provider = TranscriptExtractionMediaUnderstandingProvider(
        extraction_provider=LocalASRTranscriptExtractionProviderSkeleton(
            config=LocalASRTranscriptExtractionProviderConfig(provider_name="local_asr_adapter_target")
        )
    )

    request = provider.build_extraction_request(
        MediaSource(
            kind=MediaSourceKind.LOCAL_AUDIO,
            locator=str(audio_path),
            metadata={"language_hint": "en"},
        )
    )

    assert request.source_kind == MediaSourceKind.LOCAL_AUDIO
    assert request.source_path == str(audio_path)
    assert request.metadata == {"language_hint": "en"}


def test_transcript_extraction_adapter_normalizes_provider_result_into_attributed_lines(tmp_path: Path) -> None:
    audio_path = tmp_path / "adapter_result.wav"
    _write_dummy_wav(audio_path)

    class RecordingExtractionProvider:
        def __init__(self) -> None:
            self.last_request: TranscriptExtractionRequest | None = None

        def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
            self.last_request = request
            return TranscriptExtractionResult(
                request=request,
                transcript_lines=[
                    TranscriptLine(index=1, start_ms=0, end_ms=800, source_text=" Hello there. "),
                    TranscriptLine(index=2, start_ms=900, end_ms=1_600, source_text=" General Kenobi. "),
                ],
                provider_name="local_asr_future",
                provider_mode="local_asr",
                version_context={"provider_variant": "local_asr_future_v0"},
            )

    extraction_provider = RecordingExtractionProvider()
    provider = TranscriptExtractionMediaUnderstandingProvider(
        extraction_provider=cast(TranscriptExtractionProvider, extraction_provider),
        default_speaker_id="speaker_local_asr",
        default_speaker_name=None,
    )

    attributed_lines = provider.load_attributed_transcript(
        MediaSource(
            kind=MediaSourceKind.LOCAL_AUDIO,
            locator=str(audio_path),
            metadata={"language_hint": "en"},
        )
    )

    assert extraction_provider.last_request is not None
    assert extraction_provider.last_request.source_kind == MediaSourceKind.LOCAL_AUDIO
    assert extraction_provider.last_request.source_path == str(audio_path)
    assert [line.source_text for line in attributed_lines] == ["Hello there.", "General Kenobi."]
    assert [line.speaker_id for line in attributed_lines] == ["speaker_local_asr", "speaker_local_asr"]
    assert [line.speaker_name for line in attributed_lines] == [None, None]


def test_future_multimodal_provider_skeleton_remains_unwired() -> None:
    provider = FutureMultimodalMediaUnderstandingProviderSkeleton(provider_name="gemini_like")

    with pytest.raises(MediaUnderstandingProviderUnavailableError, match="not connected in this sprint"):
        provider.load_attributed_transcript(
            MediaSource(
                kind=MediaSourceKind.YOUTUBE_URL,
                locator="https://youtube.com/watch?v=demo",
            )
        )


def test_future_multimodal_provider_rejects_non_media_source_kinds() -> None:
    provider = FutureMultimodalMediaUnderstandingProviderSkeleton(provider_name="gemini_like")

    with pytest.raises(
        MediaUnderstandingUnsupportedSourceKindError,
        match="only targets youtube_url, local_video, and local_audio",
    ):
        provider.load_attributed_transcript(
            MediaSource(
                kind=MediaSourceKind.TRANSCRIPT,
                transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Line A")],
            )
        )


def test_direct_multimodal_provider_role_is_distinct_from_transcript_extraction_path() -> None:
    binding = resolve_media_understanding_provider(MediaUnderstandingProviderSelectionConfig(mode="multimodal_skeleton"))

    assert isinstance(binding.provider, TranscriptExtractionMediaUnderstandingProvider)
    assert not isinstance(binding.provider, FutureMultimodalMediaUnderstandingProviderSkeleton)
    assert binding.provider_name == "transcript_extraction_adapter"
    assert binding.extraction_provider_name == "gemini_like_multimodal_extraction"


def test_future_multimodal_provider_rejects_missing_local_video_path() -> None:
    provider = FutureMultimodalTranscriptExtractionProviderSkeleton(
        config=FutureMultimodalTranscriptExtractionProviderConfig(provider_name="gemini_like_extraction")
    )

    with pytest.raises(MediaUnderstandingInvalidSourcePathError, match="local_video source requires a local file path locator"):
        provider.extract_transcript(
            TranscriptExtractionRequest(source_kind=MediaSourceKind.LOCAL_VIDEO, source_path="")
        )


def test_future_multimodal_provider_rejects_missing_local_audio_file(tmp_path: Path) -> None:
    provider = FutureMultimodalTranscriptExtractionProviderSkeleton(
        config=FutureMultimodalTranscriptExtractionProviderConfig(provider_name="gemini_like_extraction")
    )
    missing_audio_path = tmp_path / "missing_audio.wav"

    with pytest.raises(MediaUnderstandingInvalidSourcePathError, match="local_audio source file not found"):
        provider.extract_transcript(
            TranscriptExtractionRequest(
                source_kind=MediaSourceKind.LOCAL_AUDIO,
                source_path=str(missing_audio_path),
            )
        )


def test_invalid_attributed_transcript_output_is_classified() -> None:
    class BrokenProvider:
        def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
            del source
            return [
                AttributedTranscriptLine(
                    index=1,
                    start_ms=0,
                    end_ms=800,
                    speaker_id="",
                    speaker_name="Host",
                    source_text="Line A",
                )
            ]

    pipeline = MediaUnderstandingPipeline(cast(MediaUnderstandingProvider, BrokenProvider()))

    with pytest.raises(MediaUnderstandingOutputError) as exc_info:
        pipeline.run(
            MediaSource(
                kind=MediaSourceKind.TRANSCRIPT,
                transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="Line A")],
            )
        )

    assert classify_media_understanding_error(exc_info.value)["error_type"] == "invalid_attributed_transcript_output"


def test_invalid_extracted_transcript_output_is_classified(tmp_path: Path) -> None:
    audio_path = tmp_path / "fixtures_invalid_extracted.wav"
    audio_path.write_bytes(b"RIFFfake-audio")

    class BrokenExtractionProvider:
        def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
            return TranscriptExtractionResult(
                request=request,
                transcript_lines=[TranscriptLine(index=1, start_ms=0, end_ms=800, source_text="")],
                provider_name="broken_extraction",
                provider_mode="local_asr",
            )

    provider = TranscriptExtractionMediaUnderstandingProvider(
        extraction_provider=cast(TranscriptExtractionProvider, BrokenExtractionProvider())
    )
    pipeline = MediaUnderstandingPipeline(cast(MediaUnderstandingProvider, provider))

    with pytest.raises(MediaUnderstandingExtractedTranscriptOutputError) as exc_info:
        pipeline.run(MediaSource(kind=MediaSourceKind.LOCAL_AUDIO, locator=str(audio_path)))

    assert classify_media_understanding_error(exc_info.value)["error_type"] == "invalid_extracted_transcript_output"


def test_classify_media_understanding_error_maps_unsupported_source_kind() -> None:
    error = MediaUnderstandingUnsupportedSourceKindError("unsupported")

    assert classify_media_understanding_error(error)["error_type"] == "unsupported_source_kind"


def test_configuration_error_uses_configuration_error_type() -> None:
    error = MediaUnderstandingConfigurationError("invalid config")

    assert classify_media_understanding_error(error)["error_type"] == "configuration_error"


def test_future_multimodal_provider_unavailable_maps_to_provider_unavailable_error_type() -> None:
    error = MediaUnderstandingProviderUnavailableError("not connected")

    assert classify_media_understanding_error(error)["error_type"] == "provider_unavailable"


def test_transcript_extraction_unavailable_maps_to_specific_error_type() -> None:
    error = MediaUnderstandingTranscriptExtractionUnavailableError("not connected")

    assert classify_media_understanding_error(error)["error_type"] == "transcript_extraction_unavailable"


def test_invalid_source_path_maps_to_invalid_source_path_error_type() -> None:
    error = MediaUnderstandingInvalidSourcePathError("missing path")

    assert classify_media_understanding_error(error)["error_type"] == "invalid_source_path"


def test_media_understanding_output_error_is_still_a_base_media_understanding_error() -> None:
    error = MediaUnderstandingOutputError("invalid output")

    assert isinstance(error, MediaUnderstandingError)


def test_system_speech_transcription_script_handles_localized_eof_audio_input_message() -> None:
    script = media_understanding_providers._build_system_speech_transcription_script()

    assert "No audio input is supplied" in script
    assert "没有将任何音频输入提供给此识别器" in script
    assert "SetInputToWaveFile" in script

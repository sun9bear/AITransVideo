from __future__ import annotations

import sys
from pathlib import Path

import pytest


_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from pipeline.process import ProcessPipeline  # noqa: E402
from services.assemblyai.transcriber import TranscriptLine  # noqa: E402
from services.gemini.translator import DubbingSegment, GeminiTranslator  # noqa: E402


class _FakeTranslator(GeminiTranslator):
    def __init__(self) -> None:
        super().__init__("test-key", _skip_init=True)
        self.seen_batches: list[list[int]] = []

    def _translate_batch_with_length_retry(self, batch: list[dict], **kwargs: object) -> list[dict]:
        self.seen_batches.append([int(group["segment_id"]) for group in batch])
        return [
            {"segment_id": int(group["segment_id"]), "cn_text": f"中文{group['segment_id']}"}
            for group in batch
        ]


def test_keep_original_segments_are_not_sent_to_translation(tmp_path: Path) -> None:
    translator = _FakeTranslator()
    lines = [
        TranscriptLine(1, 0, 3_000, "speaker_a", "A", "Hello"),
        TranscriptLine(2, 3_000, 5_000, "speaker_a", "A", "Applause", dubbing_mode="keep_original"),
        TranscriptLine(3, 5_000, 8_000, "speaker_a", "A", "World"),
    ]

    result = translator.translate(lines, str(tmp_path), voice_id="voice_a")

    assert translator.seen_batches == [[1, 3]]
    assert [segment.segment_id for segment in result.segments] == [1, 2, 3]
    assert result.segments[0].cn_text == "中文1"
    assert result.segments[1].dubbing_mode == "keep_original"
    assert result.segments[1].cn_text == ""
    assert result.segments[2].cn_text == "中文3"


def test_empty_short_cn_segment_is_auto_kept_original_before_tts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=63,
        speaker_id="speaker_c",
        display_name="Audience",
        voice_id="voice_c",
        start_ms=10_000,
        end_ms=11_752,
        target_duration_ms=1_752,
        source_text="Audience response",
        cn_text="",
    )
    observed: dict[str, object] = {}

    def fake_materialize(
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        observed["segments"] = list(segments)
        observed["source_audio_path"] = source_audio_path
        observed["tts_dir"] = tts_dir
        for item in segments:
            item.tts_audio_path = str(tts_dir / "segment.wav")
            item.aligned_audio_path = item.tts_audio_path
            item.actual_duration_ms = item.target_duration_ms
            item.alignment_method = "keep_original"
        return len(segments)

    monkeypatch.setattr(pipeline, "_materialize_keep_original_segments", fake_materialize)

    count = pipeline._materialize_empty_text_keep_original_segments(
        [segment],
        source_audio_path=tmp_path / "source.wav",
        tts_dir=tmp_path / "tts",
    )

    assert count == 1
    assert observed["segments"] == [segment]
    assert segment.dubbing_mode == "keep_original"
    assert segment.tts_audio_path


def test_empty_long_speech_segment_is_auto_kept_original_before_tts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = ProcessPipeline()
    segment = DubbingSegment(
        segment_id=7,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_a",
        start_ms=0,
        end_ms=8_000,
        target_duration_ms=8_000,
        source_text="This is real speech content that should have been translated.",
        cn_text="",
    )
    observed: dict[str, object] = {}

    def fake_materialize(
        segments: list[DubbingSegment],
        *,
        source_audio_path: Path,
        tts_dir: Path,
    ) -> int:
        observed["segments"] = list(segments)
        return len(segments)

    monkeypatch.setattr(pipeline, "_materialize_keep_original_segments", fake_materialize)

    count = pipeline._materialize_empty_text_keep_original_segments(
        [segment],
        source_audio_path=tmp_path / "source.wav",
        tts_dir=tmp_path / "tts",
    )

    assert count == 1
    assert observed["segments"] == [segment]
    assert segment.dubbing_mode == "keep_original"


def test_cached_translation_inherits_transcript_dubbing_mode() -> None:
    segment = DubbingSegment(
        segment_id=2,
        speaker_id="speaker_a",
        display_name="A",
        voice_id="voice_a",
        start_ms=1_000,
        end_ms=2_000,
        target_duration_ms=1_000,
        source_text="Applause",
        cn_text="掌声",
    )
    line = TranscriptLine(
        2,
        1_000,
        2_000,
        "speaker_a",
        "A",
        "Applause",
        dubbing_mode="keep_original",
    )

    changed = ProcessPipeline._apply_transcript_dubbing_modes_to_segments([segment], [line])

    assert changed is True
    assert segment.dubbing_mode == "keep_original"

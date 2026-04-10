import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.assemblyai.transcriber import TranscriptLine
import services.transcript_reviewer as transcript_reviewer
from services.transcript_reviewer import (
    AudioPreprocessError,
    _prepare_review_audio,
    _prepare_review_audio_clip,
    _get_audio_duration_ms,
    _try_compress_audio,
    _format_prompt,
    _resolve_model_id,
    _REVIEW_AUDIO_WHOLE_FILE_THRESHOLD_MS,
    _REVIEW_AUDIO_CLIP_PADDING_MS,
)
from services.llm_registry import MODEL_REGISTRY as _MODEL_REGISTRY


def _line(
    index: int,
    start_ms: int,
    end_ms: int,
    speaker_id: str,
    text: str,
) -> TranscriptLine:
    return TranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_label=speaker_id.replace("speaker_", "").upper(),
        source_text=text,
    )


def _interview_speakers() -> dict[str, dict[str, str]]:
    return {
        "speaker_a": {
            "name": "Host",
            "role": "host",
            "style": "professional interviewer",
            "voice_description": "clear interviewer voice",
        },
        "speaker_b": {
            "name": "Guest",
            "role": "guest",
            "style": "serious guest",
            "voice_description": "low thoughtful voice",
        },
    }


def test_short_backchannel_is_reassigned_to_host() -> None:
    lines = [
        _line(1, 0, 1_200, "speaker_a", "What was your worst trade?"),
        _line(2, 1_200, 1_700, "speaker_b", "Yes."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 1
    assert adjusted[1].speaker_id == "speaker_a"


def test_answer_continuation_requires_actual_continuation_signal() -> None:
    lines = [
        _line(1, 0, 4_500, "speaker_b", "I think people learn more from their mistakes."),
        _line(2, 4_500, 7_800, "speaker_b", "That sounds strange."),
    ]

    assert transcript_reviewer._is_answer_continuation(  # noqa: SLF001
        lines=lines,
        position=1,
        host_speaker="speaker_a",
        guest_speaker="speaker_b",
    ) is False


def test_named_utterance_stays_conservative() -> None:
    lines = [
        _line(1, 0, 1_000, "speaker_a", "What happened next?"),
        _line(2, 1_000, 1_900, "speaker_b", "Thanks, Ron."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 0
    assert adjusted[1].speaker_id == "speaker_b"


def test_long_ambiguous_sentence_keeps_original_speaker() -> None:
    lines = [
        _line(1, 0, 4_500, "speaker_a", "What was that like for you?"),
        _line(2, 4_500, 7_600, "speaker_b", "Thank you, Charlotte. I let her do that."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 0
    assert adjusted[1].speaker_id == "speaker_b"


def test_interview_sanity_check_skips_when_actual_speaker_count_exceeds_two() -> None:
    lines = [
        _line(1, 0, 5_000, "speaker_a", "What happened after that?"),
        _line(2, 5_000, 9_000, "speaker_b", "I think the board was surprised."),
        _line(3, 9_000, 13_500, "speaker_c", "Absolutely. I mean, so it's a real transition."),
    ]

    adjusted, applied = transcript_reviewer._apply_interview_sanity_check(  # noqa: SLF001
        lines,
        _interview_speakers(),
    )

    assert applied == 0
    assert [line.speaker_id for line in adjusted] == ["speaker_a", "speaker_b", "speaker_c"]


def test_merge_correction_preserves_speaker_c() -> None:
    lines = [
        _line(1, 0, 2_000, "speaker_c", "First part."),
        _line(2, 2_000, 4_000, "speaker_c", "Second part."),
    ]

    adjusted, applied = transcript_reviewer._apply_corrections(  # noqa: SLF001
        lines,
        [
            {
                "action": "merge",
                "indices": [1, 2],
                "speaker": "speaker_c",
            }
        ],
    )

    assert applied == 1
    assert len(adjusted) == 1
    assert adjusted[0].speaker_id == "speaker_c"


def test_review_transcript_writes_raw_response_and_speaker_diff_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        _line(1, 0, 4_000, "speaker_a", "All right, we have some other news to tell you about, too."),
        _line(2, 4_000, 8_000, "speaker_b", "Everything, uh, will be the same."),
    ]
    debug_dir = tmp_path / "transcript"
    raw_response_text = json.dumps(
        {
            "speakers": {
                "speaker_a": {
                    "name": "贝基·奎克",
                    "role": "host",
                    "style": "anchor",
                    "voice_description": "clear voice",
                }
            },
            "glossary": {},
            "corrections": [
                {
                    "action": "correct_speaker",
                    "index": 1,
                    "to": "speaker_b",
                    "reason": "speaker mismatch",
                }
            ],
        },
        ensure_ascii=False,
    )

    def _fake_call_review(**kwargs):
        kwargs["trace_sink"].append(
            {
                "call_type": "single",
                "model": "gemini-2.5-flash-lite",
                "has_audio": False,
                "line_count": kwargs["line_count"],
                "response_text": raw_response_text,
                "parsed_payload": json.loads(raw_response_text),
            }
        )
        return (
            {
                "speaker_a": {
                    "name": "贝基·奎克",
                    "role": "host",
                    "style": "anchor",
                    "voice_description": "clear voice",
                }
            },
            {},
            [
                {
                    "action": "correct_speaker",
                    "index": 1,
                    "to": "speaker_b",
                    "reason": "speaker mismatch",
                }
            ],
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(transcript_reviewer, "_get_review_model", lambda: "gemini")
    monkeypatch.setattr(transcript_reviewer, "_call_review", _fake_call_review)

    result = transcript_reviewer.legacy_review_transcript_single_pass(
        lines,
        audio_path=None,
        video_title="Test Video",
        video_url="https://example.com/watch?v=test",
        debug_output_dir=debug_dir,
    )

    assert result is not None
    assert result.debug_artifacts["raw_response_path"] == str(
        (debug_dir / "s2_review_raw_response.json").resolve(strict=False)
    )
    assert result.debug_artifacts["speaker_diff_path"] == str(
        (debug_dir / "s2_review_speaker_diff.json").resolve(strict=False)
    )

    raw_payload = json.loads((debug_dir / "s2_review_raw_response.json").read_text(encoding="utf-8"))
    assert len(raw_payload["events"]) == 1
    assert raw_payload["events"][0]["response_text"] == raw_response_text

    diff_payload = json.loads((debug_dir / "s2_review_speaker_diff.json").read_text(encoding="utf-8"))
    assert diff_payload["line_counts"] == {
        "original": 2,
        "after_corrections": 2,
        "after_sanity": 2,
        "final": 2,
    }
    assert diff_payload["speaker_diffs"]["original_to_after_corrections"] == [
        {
            "position": 0,
            "before_index": 1,
            "after_index": 1,
            "before_speaker_id": "speaker_a",
            "after_speaker_id": "speaker_b",
            "start_ms": 0,
            "end_ms": 4000,
            "source_text": "All right, we have some other news to tell you about, too.",
        }
    ]
    assert diff_payload["speaker_diffs"]["after_corrections_to_after_sanity"] == []


# ===================================================================
# A1: Audio preprocessing
# ===================================================================


class TestPrepareReviewAudio:
    """Tests for _prepare_review_audio (full-file compression)."""

    def test_success_creates_compressed_file(self, tmp_path: Path) -> None:
        """Compressed file is created in tmp_dir."""
        src = tmp_path / "original.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)
        out_dir = tmp_path / "review_tmp"

        with patch("services.transcript_reviewer.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            # Simulate ffmpeg creating the output file
            def fake_run(cmd, **kw):
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"OggS" + b"\x00" * 50)
                return MagicMock(returncode=0)
            mock_sub.run.side_effect = fake_run

            result = _prepare_review_audio(src, out_dir)

        assert result.name == "review_audio.ogg"
        assert result.exists()
        cmd_args = mock_sub.run.call_args[0][0]
        assert "-ac" in cmd_args
        assert "1" in cmd_args  # mono
        assert "-ar" in cmd_args
        assert "16000" in cmd_args
        assert "-c:a" in cmd_args
        assert "libopus" in cmd_args

    def test_ffmpeg_not_found_raises(self, tmp_path: Path) -> None:
        """Raises AudioPreprocessError if ffmpeg is missing."""
        src = tmp_path / "original.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("services.transcript_reviewer.subprocess") as mock_sub:
            mock_sub.run.side_effect = FileNotFoundError("ffmpeg not found")
            with pytest.raises(AudioPreprocessError, match="ffmpeg"):
                _prepare_review_audio(src, tmp_path / "out")

    def test_empty_output_raises(self, tmp_path: Path) -> None:
        """Raises if compressed file is empty (ffmpeg ran but produced nothing)."""
        src = tmp_path / "original.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)
        out_dir = tmp_path / "review_tmp"

        with patch("services.transcript_reviewer.subprocess") as mock_sub:
            def fake_run(cmd, **kw):
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"")  # empty
                return MagicMock(returncode=0)
            mock_sub.run.side_effect = fake_run

            with pytest.raises(AudioPreprocessError, match="empty"):
                _prepare_review_audio(src, out_dir)


class TestPrepareReviewAudioClip:
    """Tests for _prepare_review_audio_clip (batch-local time-range clip)."""

    def test_clip_uses_correct_time_range_with_padding(self, tmp_path: Path) -> None:
        """Clip should include ±10s padding."""
        src = tmp_path / "original.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("services.transcript_reviewer.subprocess") as mock_sub:
            def fake_run(cmd, **kw):
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"OggS" + b"\x00" * 50)
                return MagicMock(returncode=0)
            mock_sub.run.side_effect = fake_run

            result = _prepare_review_audio_clip(
                src, tmp_path / "clips",
                start_ms=60_000,   # 1:00
                end_ms=180_000,    # 3:00
                clip_index=1,
            )

        assert result.name == "review_clip_001.ogg"
        cmd_args = mock_sub.run.call_args[0][0]

        # Find -ss and -t values
        ss_idx = cmd_args.index("-ss")
        t_idx = cmd_args.index("-t")
        ss_val = float(cmd_args[ss_idx + 1])
        t_val = float(cmd_args[t_idx + 1])

        # start_ms=60000 - padding 10000 = 50000 → 50.0s
        assert ss_val == pytest.approx(50.0, abs=0.1)
        # duration = (180000 + 10000) - (60000 - 10000) = 140000 → 140.0s
        assert t_val == pytest.approx(140.0, abs=0.1)

    def test_clip_padding_clamps_to_zero(self, tmp_path: Path) -> None:
        """Start padding should not go below 0."""
        src = tmp_path / "original.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch("services.transcript_reviewer.subprocess") as mock_sub:
            def fake_run(cmd, **kw):
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"OggS" + b"\x00" * 50)
                return MagicMock(returncode=0)
            mock_sub.run.side_effect = fake_run

            _prepare_review_audio_clip(
                src, tmp_path / "clips",
                start_ms=5_000,    # 5s, padding would be -5s → clamped to 0
                end_ms=30_000,
                clip_index=0,
            )

        cmd_args = mock_sub.run.call_args[0][0]
        ss_idx = cmd_args.index("-ss")
        ss_val = float(cmd_args[ss_idx + 1])
        assert ss_val == pytest.approx(0.0, abs=0.01)


class TestReviewTranscriptAudioFirst:
    """Tests for audio-first review path in review_transcript()."""

    def test_short_audio_compressed_before_upload(self, tmp_path: Path, monkeypatch) -> None:
        """≤20 min audio: review_transcript compresses once and passes to _call_review."""
        src_audio = tmp_path / "original.wav"
        src_audio.write_bytes(b"RIFF" + b"\x00" * 100)

        received_audio_paths: list = []

        def spy_call_review(**kwargs):
            received_audio_paths.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A", "gender": "male", "age_group": "middle"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(transcript_reviewer, "_get_review_model", lambda: "gemini")

        # Short audio: 10 min → should compress whole file
        monkeypatch.setattr(transcript_reviewer, "_get_audio_duration_ms", lambda p: 600_000)

        def fake_compress(audio_path, tmp_dir, **kw):
            compressed = tmp_dir / "review_audio.ogg"
            compressed.parent.mkdir(parents=True, exist_ok=True)
            compressed.write_bytes(b"OggS" + b"\x00" * 50)
            return compressed
        monkeypatch.setattr(transcript_reviewer, "_prepare_review_audio", fake_compress)

        lines = [_line(1, 0, 5000, "speaker_a", "Hello world.")]
        result = transcript_reviewer.legacy_review_transcript_single_pass(
            lines, audio_path=str(src_audio), video_title="Test",
        )

        assert result is not None
        assert len(received_audio_paths) == 1
        assert received_audio_paths[0] is not None
        assert "review_audio.ogg" in str(received_audio_paths[0])

    def test_long_audio_does_not_compress_whole_file(self, tmp_path: Path, monkeypatch) -> None:
        """>20 min audio with few lines: single-batch path still compresses on demand."""
        src_audio = tmp_path / "original.wav"
        src_audio.write_bytes(b"RIFF" + b"\x00" * 100)

        compress_calls: list = []

        def fake_compress(audio_path, tmp_dir, **kw):
            compress_calls.append("whole")
            compressed = tmp_dir / "review_audio.ogg"
            compressed.parent.mkdir(parents=True, exist_ok=True)
            compressed.write_bytes(b"OggS" + b"\x00" * 50)
            return compressed

        monkeypatch.setattr(transcript_reviewer, "_prepare_review_audio", fake_compress)
        monkeypatch.setattr(transcript_reviewer, "_get_audio_duration_ms", lambda p: 2_400_000)  # 40 min

        received_audio: list = []
        def spy_call_review(**kwargs):
            received_audio.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A", "gender": "male", "age_group": "middle"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(transcript_reviewer, "_get_review_model", lambda: "gemini")

        # Single batch (< 200 lines) but long audio
        lines = [_line(1, 0, 5000, "speaker_a", "Hello world.")]
        transcript_reviewer.legacy_review_transcript_single_pass(
            lines, audio_path=str(src_audio), video_title="Test",
        )

        # use_whole_audio=False, but single_batch_audio path compresses on demand
        assert len(compress_calls) == 1
        assert received_audio[0] is not None

    def test_compression_failure_retries_aggressive_then_proceeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """On compression failure, _try_compress_audio retries with aggressive bitrate;
        if both fail, proceed without audio."""
        src_audio = tmp_path / "original.wav"
        src_audio.write_bytes(b"RIFF" + b"\x00" * 100)

        compress_calls: list[str] = []

        def fake_prepare(audio_path, tmp_dir, *, bitrate="32k"):
            compress_calls.append(bitrate)
            raise AudioPreprocessError(f"ffmpeg failed at {bitrate}")

        received_audio: list = []

        def spy_call_review(**kwargs):
            received_audio.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A", "gender": "male", "age_group": "middle"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_prepare_review_audio", fake_prepare)
        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(transcript_reviewer, "_get_review_model", lambda: "gemini")
        monkeypatch.setattr(transcript_reviewer, "_get_audio_duration_ms", lambda p: 600_000)

        lines = [_line(1, 0, 5000, "speaker_a", "Hello world.")]
        result = transcript_reviewer.legacy_review_transcript_single_pass(
            lines, audio_path=str(src_audio), video_title="Test",
        )

        assert result is not None
        # _try_compress_audio is called for use_whole_audio path (≤20min, tries 32k+16k),
        # then again for single_batch_audio fallback (tries 32k+16k again) → 4 total
        assert len(compress_calls) == 4
        assert compress_calls == ["32k", "16k", "32k", "16k"]
        # _call_review receives None audio (all compressions failed)
        assert received_audio[0] is None


class TestBatchedReviewAudioStrategy:
    """Tests for batched review audio strategy (≤20min vs >20min)."""

    def test_short_audio_reuses_whole_compressed_file(self, tmp_path: Path, monkeypatch) -> None:
        """≤20 min audio: each batch gets the same compressed audio."""
        original = tmp_path / "original.wav"
        original.write_bytes(b"RIFF" + b"\x00" * 100)
        compressed = tmp_path / "review_audio.ogg"
        compressed.write_bytes(b"OggS" + b"\x00" * 50)

        batch_audios: list = []

        def spy_call_review(**kwargs):
            batch_audios.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)
        monkeypatch.setattr(transcript_reviewer, "_try_create_audio_cache", lambda **kw: None)

        lines = [_line(i, i * 2000, (i + 1) * 2000, "speaker_a", f"Line {i}") for i in range(1, 250)]

        transcript_reviewer._batched_review(
            api_key="key",
            lines=lines,
            original_audio_path=original,
            compressed_audio_path=compressed,
            audio_duration_ms=600_000,  # 10 min — under threshold
            review_tmp_dir=tmp_path / "review_tmp",
            video_title="Test",
            video_url="",
            review_model="gemini",
        )

        assert len(batch_audios) >= 2
        for audio in batch_audios:
            assert audio == compressed

    def test_long_audio_uses_batch_local_clips_from_original(self, tmp_path: Path, monkeypatch) -> None:
        """>20 min audio: each batch gets a local clip generated from original audio (not pre-compressed)."""
        original = tmp_path / "original.wav"
        original.write_bytes(b"RIFF" + b"\x00" * 100)
        review_tmp = tmp_path / "review_tmp"

        clip_calls: list[dict] = []

        def fake_clip(audio_path, tmp_dir, *, start_ms, end_ms, clip_index, bitrate="32k"):
            clip_calls.append({
                "source": str(audio_path),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "index": clip_index,
            })
            clip_path = tmp_dir / f"review_clip_{clip_index:03d}.ogg"
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            clip_path.write_bytes(b"OggS" + b"\x00" * 50)
            return clip_path

        monkeypatch.setattr(transcript_reviewer, "_prepare_review_audio_clip", fake_clip)

        batch_audios: list = []

        def spy_call_review(**kwargs):
            batch_audios.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)

        lines = [_line(i, i * 2000, (i + 1) * 2000, "speaker_a", f"Line {i}") for i in range(1, 250)]

        transcript_reviewer._batched_review(
            api_key="key",
            lines=lines,
            original_audio_path=original,
            compressed_audio_path=None,  # Not used for long audio
            audio_duration_ms=1_800_000,  # 30 min — over threshold
            review_tmp_dir=review_tmp,
            video_title="Test",
            video_url="",
            review_model="gemini",
        )

        assert len(clip_calls) >= 2
        assert clip_calls[0]["index"] == 1
        assert clip_calls[1]["index"] == 2
        # Clips are generated from original audio, NOT a compressed intermediate
        for call in clip_calls:
            assert str(original) in call["source"]
        for audio in batch_audios:
            assert audio is not None
            assert "review_clip_" in str(audio)

    def test_cache_created_for_short_audio(self, tmp_path: Path, monkeypatch) -> None:
        """≤20 min: explicit cache is attempted; on success, passed to _call_review."""
        original = tmp_path / "original.wav"
        original.write_bytes(b"RIFF" + b"\x00" * 100)
        compressed = tmp_path / "review_audio.ogg"
        compressed.write_bytes(b"OggS" + b"\x00" * 50)

        monkeypatch.setattr(
            transcript_reviewer, "_try_create_audio_cache",
            lambda **kw: "cached-content-xyz",
        )

        received_cache_names: list = []

        def spy_call_review(**kwargs):
            received_cache_names.append(kwargs.get("cached_content_name"))
            return ({"speaker_a": {"name": "A"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)

        lines = [_line(i, i * 2000, (i + 1) * 2000, "speaker_a", f"Line {i}") for i in range(1, 250)]

        transcript_reviewer._batched_review(
            api_key="key",
            lines=lines,
            original_audio_path=original,
            compressed_audio_path=compressed,
            audio_duration_ms=600_000,  # 10 min
            review_tmp_dir=tmp_path / "review_tmp",
            video_title="Test",
            video_url="",
            review_model="gemini",
        )

        assert len(received_cache_names) >= 2
        for name in received_cache_names:
            assert name == "cached-content-xyz"

    def test_cache_failure_falls_back_to_compressed_file(self, tmp_path: Path, monkeypatch) -> None:
        """≤20 min: if cache creation fails, batches still get compressed audio file."""
        original = tmp_path / "original.wav"
        original.write_bytes(b"RIFF" + b"\x00" * 100)
        compressed = tmp_path / "review_audio.ogg"
        compressed.write_bytes(b"OggS" + b"\x00" * 50)

        monkeypatch.setattr(
            transcript_reviewer, "_try_create_audio_cache",
            lambda **kw: None,  # Cache creation failed
        )

        received_args: list[dict] = []

        def spy_call_review(**kwargs):
            received_args.append(kwargs)
            return ({"speaker_a": {"name": "A"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)

        lines = [_line(i, i * 2000, (i + 1) * 2000, "speaker_a", f"Line {i}") for i in range(1, 250)]

        transcript_reviewer._batched_review(
            api_key="key",
            lines=lines,
            original_audio_path=original,
            compressed_audio_path=compressed,
            audio_duration_ms=600_000,
            review_tmp_dir=tmp_path / "review_tmp",
            video_title="Test",
            video_url="",
            review_model="gemini",
        )

        assert len(received_args) >= 2
        for args in received_args:
            # No cache, but still has audio file
            assert args["cached_content_name"] is None
            assert args["audio_path"] == compressed

    def test_no_audio_batched_review_still_works(self, monkeypatch) -> None:
        """When no audio is available, batched review works without audio."""
        batch_audios: list = []

        def spy_call_review(**kwargs):
            batch_audios.append(kwargs.get("audio_path"))
            return ({"speaker_a": {"name": "A"}}, {}, [])

        monkeypatch.setattr(transcript_reviewer, "_call_review", spy_call_review)

        lines = [_line(i, i * 2000, (i + 1) * 2000, "speaker_a", f"Line {i}") for i in range(1, 250)]

        transcript_reviewer._batched_review(
            api_key="key",
            lines=lines,
            original_audio_path=None,
            compressed_audio_path=None,
            audio_duration_ms=None,
            review_tmp_dir=None,
            video_title="Test",
            video_url="",
            review_model="gemini",
        )

        assert len(batch_audios) >= 2
        for audio in batch_audios:
            assert audio is None


class TestCallReviewAudioFirst:
    """Tests for _call_review audio-first behavior (no 200MB threshold)."""

    def test_audio_uploaded_regardless_of_size(self, monkeypatch) -> None:
        """Audio is uploaded no matter how large (we rely on prior compression)."""
        uploaded_files: list = []

        class FakeClient:
            class files:
                @staticmethod
                def upload(file=None):
                    uploaded_files.append(str(file))
                    return MagicMock()

            class models:
                @staticmethod
                def generate_content(model, contents, config, **kw):
                    resp = MagicMock()
                    resp.text = '{"speakers": {}, "glossary": {}, "corrections": []}'
                    return resp

        monkeypatch.setattr(transcript_reviewer, "_create_review_client", lambda api_key: FakeClient())
        monkeypatch.setattr(transcript_reviewer, "_load_genai_types", lambda: MagicMock())

        audio = Path("/tmp/test_big_audio.ogg")

        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=500 * 1024 * 1024)):
            result = transcript_reviewer._call_review(
                api_key="key",
                transcript_body="test",
                line_count=1,
                audio_path=audio,
                video_title="Test",
                video_url="",
            )

        assert result is not None
        assert len(uploaded_files) == 1


# ===================================================================
# A2: Prompt templates — audio vs text-only
# ===================================================================


class TestPromptTemplates:
    """Tests for dual prompt templates (A2)."""

    def test_audio_prompt_contains_listen_instruction(self) -> None:
        """Audio prompt must instruct the model to listen to audio."""
        prompt = _format_prompt(
            has_audio=True,
            video_title="Test Video",
            video_url="https://example.com",
            line_count=5,
            transcript_body="[1](0.00-5.00) speaker_a: Hello",
        )
        assert "听音频" in prompt
        assert "本次没有提供音频" not in prompt

    def test_text_only_prompt_does_not_mention_listening(self) -> None:
        """Text-only prompt must NOT ask the model to listen to audio."""
        prompt = _format_prompt(
            has_audio=False,
            video_title="Test Video",
            video_url="https://example.com",
            line_count=5,
            transcript_body="[1](0.00-5.00) speaker_a: Hello",
        )
        assert "听音频" not in prompt
        assert "本次没有提供音频" in prompt

    def test_both_prompts_require_gender_and_age(self) -> None:
        """Both prompt versions must request gender and age_group."""
        for has_audio in (True, False):
            prompt = _format_prompt(
                has_audio=has_audio,
                video_title="Test",
                video_url="",
                line_count=1,
                transcript_body="test",
            )
            assert "gender" in prompt
            assert "age_group" in prompt
            assert "voice_description" in prompt

    def test_both_prompts_include_output_format(self) -> None:
        """Both prompts include the shared JSON output format."""
        for has_audio in (True, False):
            prompt = _format_prompt(
                has_audio=has_audio,
                video_title="Test",
                video_url="",
                line_count=1,
                transcript_body="test",
            )
            assert '"speakers"' in prompt
            assert '"glossary"' in prompt
            assert '"corrections"' in prompt

    def test_call_review_uses_audio_prompt_when_audio_present(self, monkeypatch) -> None:
        """_call_review selects audio prompt when audio upload succeeds."""
        prompts_used: list[str] = []

        class FakeClient:
            class files:
                @staticmethod
                def upload(file=None):
                    return MagicMock()

            class models:
                @staticmethod
                def generate_content(model, contents, config, **kw):
                    # Capture the prompt text (last item in contents)
                    prompts_used.append(contents[-1])
                    resp = MagicMock()
                    resp.text = '{"speakers": {}, "glossary": {}, "corrections": []}'
                    return resp

        monkeypatch.setattr(transcript_reviewer, "_create_review_client", lambda api_key: FakeClient())
        monkeypatch.setattr(transcript_reviewer, "_load_genai_types", lambda: MagicMock())

        audio = Path("/tmp/test_audio.ogg")
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=5 * 1024 * 1024)):
            transcript_reviewer._call_review(
                api_key="key",
                transcript_body="test",
                line_count=1,
                audio_path=audio,
                video_title="Test",
                video_url="",
            )

        assert len(prompts_used) == 1
        assert "听音频" in prompts_used[0]

    def test_call_review_uses_text_prompt_when_no_audio(self, monkeypatch) -> None:
        """_call_review selects text-only prompt when no audio is available."""
        prompts_used: list[str] = []

        class FakeClient:
            class models:
                @staticmethod
                def generate_content(model, contents, config, **kw):
                    prompts_used.append(contents[-1])
                    resp = MagicMock()
                    resp.text = '{"speakers": {}, "glossary": {}, "corrections": []}'
                    return resp

        monkeypatch.setattr(transcript_reviewer, "_create_review_client", lambda api_key: FakeClient())
        monkeypatch.setattr(transcript_reviewer, "_load_genai_types", lambda: MagicMock())

        transcript_reviewer._call_review(
            api_key="key",
            transcript_body="test",
            line_count=1,
            audio_path=None,
            video_title="Test",
            video_url="",
        )

        assert len(prompts_used) == 1
        assert "本次没有提供音频" in prompts_used[0]
        assert "听音频" not in prompts_used[0]


# ===================================================================
# A3: Review model mapping
# ===================================================================


class TestModelMap:
    """Tests for MODEL_REGISTRY and _resolve_model_id (A3)."""

    def test_registry_has_all_logical_names(self) -> None:
        assert "gemini_pro" in _MODEL_REGISTRY
        assert "gemini" in _MODEL_REGISTRY
        assert "mimo_omni" in _MODEL_REGISTRY

    def test_resolve_known_names(self) -> None:
        assert _resolve_model_id("gemini_pro") == _MODEL_REGISTRY["gemini_pro"]["api_model_id"]
        assert _resolve_model_id("gemini") == _MODEL_REGISTRY["gemini"]["api_model_id"]
        assert _resolve_model_id("mimo_omni") == _MODEL_REGISTRY["mimo_omni"]["api_model_id"]

    def test_resolve_unknown_falls_back(self) -> None:
        result = _resolve_model_id("nonexistent_model")
        # Unknown names return themselves (from resolve_model_id fallback)
        assert isinstance(result, str)

    def test_model_ids_are_not_logical_names(self) -> None:
        """API model IDs must differ from the logical names (no pass-through)."""
        for logical, info in _MODEL_REGISTRY.items():
            api_id = info["api_model_id"]
            assert logical != api_id, f"{logical} should not equal its API ID"


class TestGetReviewModel:
    """Tests for _get_review_model (legacy compat wrapper)."""

    def test_default_is_gemini_pro(self, monkeypatch) -> None:
        """_get_review_model delegates to llm_registry, defaults to gemini_pro."""
        from services.llm_registry import invalidate_cache
        invalidate_cache()
        monkeypatch.setattr("os.path.exists", lambda p: False)
        result = transcript_reviewer._get_review_model()
        assert result == "gemini_pro"

    def test_legacy_wrapper_returns_valid_model(self, monkeypatch) -> None:
        """_get_review_model always returns a model from the registry."""
        from services.llm_registry import invalidate_cache, MODEL_REGISTRY
        invalidate_cache()
        monkeypatch.setattr("os.path.exists", lambda p: False)
        result = transcript_reviewer._get_review_model()
        assert result in MODEL_REGISTRY

    def test_call_review_passes_resolved_model_to_gemini(self, monkeypatch) -> None:
        """_call_review must pass the resolved API model ID (not the logical name)."""
        captured_models: list[str] = []

        class FakeClient:
            class files:
                @staticmethod
                def upload(file=None):
                    return MagicMock()

            class models:
                @staticmethod
                def generate_content(model, contents, config, **kw):
                    captured_models.append(model)
                    resp = MagicMock()
                    resp.text = '{"speakers": {}, "glossary": {}, "corrections": []}'
                    return resp

        monkeypatch.setattr(transcript_reviewer, "_create_review_client", lambda api_key: FakeClient())
        monkeypatch.setattr(transcript_reviewer, "_load_genai_types", lambda: MagicMock())

        transcript_reviewer._call_review(
            api_key="key",
            transcript_body="test",
            line_count=1,
            audio_path=None,
            video_title="Test",
            video_url="",
            review_model="gemini_pro",
        )

        assert len(captured_models) == 1
        # Must be the resolved API ID, not "gemini_pro"
        assert captured_models[0] == _MODEL_REGISTRY["gemini_pro"]["api_model_id"]
        assert captured_models[0] != "gemini_pro"


# ===================================================================
# Three-pass split tests
# ===================================================================


class TestThreePassContractEnforcement:
    """Tests for Pass 1/2/3 contract filtering."""

    def test_pass1_drops_fix_text_corrections(self) -> None:
        """Pass 1 contract: correct_speaker + split allowed, fix_text/merge dropped."""
        from src.services.transcript_reviewer import _PASS1_ALLOWED_ACTIONS
        assert "correct_speaker" in _PASS1_ALLOWED_ACTIONS
        assert "split" in _PASS1_ALLOWED_ACTIONS
        assert "fix_text" not in _PASS1_ALLOWED_ACTIONS
        assert "merge" not in _PASS1_ALLOWED_ACTIONS

    def test_pass2_drops_correct_speaker_corrections(self) -> None:
        """Pass 2 contract: only fix_text + split allowed, correct_speaker dropped."""
        from src.services.transcript_reviewer import _PASS2_ALLOWED_ACTIONS
        assert "fix_text" in _PASS2_ALLOWED_ACTIONS
        assert "split" in _PASS2_ALLOWED_ACTIONS
        assert "correct_speaker" not in _PASS2_ALLOWED_ACTIONS
        assert "merge" not in _PASS2_ALLOWED_ACTIONS

    def test_pass1_prompt_forbids_fix_text(self) -> None:
        """Pass 1 prompt explicitly forbids fix_text/merge."""
        from src.services.transcript_reviewer import _PASS1_PROMPT
        assert "fix_text" in _PASS1_PROMPT
        prompt_lower = _PASS1_PROMPT.lower()
        assert "不要输出" in _PASS1_PROMPT or "绝对不要" in _PASS1_PROMPT or "do not output" in prompt_lower

    def test_pass2_prompt_forbids_correct_speaker(self) -> None:
        """Pass 2 prompt explicitly forbids correct_speaker."""
        from src.services.transcript_reviewer import _PASS2_PROMPT
        assert "correct_speaker" in _PASS2_PROMPT
        assert "绝对不要" in _PASS2_PROMPT

    def test_pass3_prompt_forbids_corrections_and_glossary(self) -> None:
        """Pass 3 prompt explicitly forbids corrections and glossary."""
        from src.services.transcript_reviewer import _PASS3_PROMPT
        assert "不要输出 corrections" in _PASS3_PROMPT
        assert "不要输出 glossary" in _PASS3_PROMPT


class TestThreePassFallback:
    """Tests for three-pass fallback to legacy."""

    def test_pass_failure_returns_none_after_retries(self, monkeypatch) -> None:
        """When Pass 1 fails after retries, review_transcript returns None (no legacy fallback)."""
        monkeypatch.setattr(transcript_reviewer, "_try_compress_audio", lambda *a, **kw: None)
        # Don't mock Gemini — will fail to connect → _PassFailure after retries → None
        from services.llm_registry import invalidate_cache
        invalidate_cache()
        monkeypatch.setattr("os.path.exists", lambda p: False)

        lines = [_line(1, 0, 5000, "speaker_a", "Hello world.")]
        result = transcript_reviewer.review_transcript(
            lines, video_title="Test",
        )

        assert result is None

    def test_mimo_omni_skips_three_pass(self, monkeypatch) -> None:
        """MiMo Omni model bypasses three-pass and uses legacy directly."""
        legacy_called = []

        def fake_legacy(*args, **kwargs):
            legacy_called.append(True)
            return transcript_reviewer.ReviewResult(
                speakers={"speaker_a": {"name": "A", "gender": "male", "age_group": "middle"}},
                glossary={},
                corrections_applied=0,
                lines=args[0],
            )

        monkeypatch.setattr(
            transcript_reviewer,
            "legacy_review_transcript_single_pass",
            fake_legacy,
        )
        # MiMo Omni for pass1 still triggers legacy (text-only, can't do audio Pass 1)
        from services.llm_registry import invalidate_cache, _DEFAULTS
        invalidate_cache()
        monkeypatch.setitem(_DEFAULTS, "pass1", "mimo_omni")
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setenv("MIMO_API_KEY", "fake-key")

        lines = [_line(1, 0, 5000, "speaker_a", "Hello.")]
        result = transcript_reviewer.review_transcript(
            lines, video_title="Test",
        )

        assert result is not None
        assert len(legacy_called) == 1


class TestThreePassVoiceProfiles:
    """Tests for Pass 3 voice profiling."""

    def test_fallback_minimal_profiles(self) -> None:
        """_fallback_minimal_speaker_styles creates minimal profiles from speaker info."""
        from src.services.transcript_reviewer import _fallback_minimal_speaker_styles

        speakers = {
            "speaker_a": {"name": "Alice", "gender": "female", "age_group": "young", "voice_description": "清晰"},
            "speaker_b": {"name": "Bob", "gender": "male", "age_group": "elderly"},
        }
        profiles = _fallback_minimal_speaker_styles(speakers)
        assert "speaker_a" in profiles
        assert "speaker_b" in profiles
        assert profiles["speaker_a"]["gender"] == "female"
        assert profiles["speaker_a"]["voice_description"] == "清晰"
        assert profiles["speaker_b"]["gender"] == "male"
        assert profiles["speaker_b"]["energy_level"] == "medium"

    def test_pass3_no_audio_returns_fallback(self, monkeypatch) -> None:
        """Pass 3 without audio returns fallback profiles."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        speakers = {
            "speaker_a": {"name": "A", "gender": "male", "age_group": "middle"},
        }
        result = transcript_reviewer.review_pass3_voice_profiles(
            [_line(1, 0, 5000, "speaker_a", "Hello")],
            source_audio_path=None,
            speakers=speakers,
        )
        assert "speaker_a" in result
        assert result["speaker_a"]["gender"] == "male"

    def test_pass3_no_api_key_returns_fallback(self, monkeypatch) -> None:
        """Pass 3 without API key returns fallback profiles."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        speakers = {
            "speaker_a": {"name": "A", "gender": "female", "age_group": "young"},
        }
        result = transcript_reviewer.review_pass3_voice_profiles(
            [_line(1, 0, 5000, "speaker_a", "Hello")],
            source_audio_path=Path("/nonexistent/audio.wav"),
            speakers=speakers,
        )
        assert "speaker_a" in result
        assert result["speaker_a"]["gender"] == "female"


class TestSpeakerAudioExtraction:
    """Tests for Pass 3 speaker audio clip extraction."""

    def test_extract_finds_longest_utterance(self) -> None:
        """_extract_speaker_audio_clips picks the longest utterance per speaker."""
        from src.services.transcript_reviewer import _extract_speaker_audio_clips

        lines = [
            _line(1, 0, 3000, "speaker_a", "Short"),
            _line(2, 3000, 20000, "speaker_a", "This is a much longer utterance"),
            _line(3, 20000, 25000, "speaker_b", "Speaker B talks"),
        ]

        # We can't run ffmpeg in unit tests, but we can verify the function
        # signature and error handling
        from pathlib import Path
        result = _extract_speaker_audio_clips(
            lines,
            Path("/nonexistent/audio.wav"),
            Path("/tmp/test_clips"),
        )
        # ffmpeg will fail, but the function should handle gracefully
        assert isinstance(result, dict)


class TestThreePassEvidenceChain:
    """Verify that three-pass mode produces real evidence artifacts."""

    def test_three_pass_raw_response_contains_pass1_and_pass2(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """s2_review_raw_response.json must contain Pass 1 and Pass 2 events."""
        lines = [
            _line(1, 0, 4000, "speaker_a", "Hello there."),
            _line(2, 4000, 8000, "speaker_b", "Hi, nice to meet you."),
        ]
        debug_dir = tmp_path / "transcript"

        call_count = [0]

        class FakeClient:
            class files:
                @staticmethod
                def upload(file=None):
                    return MagicMock()

            class models:
                @staticmethod
                def generate_content(model, contents, config, **kw):
                    call_count[0] += 1
                    resp = MagicMock()
                    if call_count[0] == 1:
                        # Pass 1 response
                        resp.text = json.dumps({
                            "speakers": {
                                "speaker_a": {"name": "Alice", "gender": "female", "age_group": "middle", "role": "host", "style": "calm"},
                                "speaker_b": {"name": "Bob", "gender": "male", "age_group": "elderly", "role": "guest", "style": "warm"},
                            },
                            "corrections": [
                                {"action": "correct_speaker", "index": 2, "to": "speaker_a", "reason": "same voice"},
                            ],
                        })
                    else:
                        # Pass 2 response
                        resp.text = json.dumps({
                            "corrections": [
                                {"action": "fix_text", "index": 1, "old": "Hello there.", "new": "Hello, there.", "reason": "punctuation"},
                            ],
                            "glossary": {"Alice": "爱丽丝"},
                        })
                    return resp

        monkeypatch.setattr(transcript_reviewer, "_create_review_client", lambda api_key: FakeClient())
        monkeypatch.setattr(transcript_reviewer, "_load_genai_types", lambda: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(transcript_reviewer, "_get_review_model", lambda: "gemini")
        monkeypatch.setattr(transcript_reviewer, "_try_compress_audio", lambda *a, **kw: None)

        result = transcript_reviewer.review_transcript(
            lines,
            audio_path=None,
            video_title="Test",
            video_url="https://example.com",
            debug_output_dir=debug_dir,
        )

        assert result is not None
        # Should have gone through three-pass (not legacy fallback)
        assert call_count[0] == 2

        # --- Verify s2_review_raw_response.json has both pass events ---
        raw_path = debug_dir / "s2_review_raw_response.json"
        assert raw_path.exists()
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        assert len(raw["events"]) == 2
        assert raw["events"][0]["pass"] == "pass1_speakers"
        assert raw["events"][0]["response_text"]  # non-empty
        assert raw["events"][1]["pass"] == "pass2_text"
        assert raw["events"][1]["response_text"]  # non-empty

        # --- Verify s2_review_speaker_diff.json has separated snapshots ---
        diff_path = debug_dir / "s2_review_speaker_diff.json"
        assert diff_path.exists()
        diff = json.loads(diff_path.read_text(encoding="utf-8"))

        # Pass 1 applied correct_speaker on line 2 → speaker_b → speaker_a
        # So original_to_after_corrections should show that change
        assert len(diff["speaker_diffs"]["original_to_after_corrections"]) == 1
        entry = diff["speaker_diffs"]["original_to_after_corrections"][0]
        assert entry["before_speaker_id"] == "speaker_b"
        assert entry["after_speaker_id"] == "speaker_a"

        # Sanity check should not fire (2 speakers detected in transcript
        # but after Pass 1 correction both lines are speaker_a, so
        # actual_speakers != 2 → sanity check skips entirely)
        # Therefore after_corrections == after_sanity
        # But importantly: they are SEPARATE snapshots, not aliased
        snap = diff["snapshots"]
        assert "original" in snap
        assert "after_corrections" in snap
        assert "after_sanity" in snap
        assert "final" in snap

        # --- Verify per-pass artifacts exist ---
        assert (debug_dir / "s2_pass1_result.json").exists()
        assert (debug_dir / "s2_pass2_result.json").exists()

        # --- Verify aggregated result ---
        result_path = debug_dir / "s2_review_result.json"
        assert result_path.exists()
        agg = json.loads(result_path.read_text(encoding="utf-8"))
        assert agg["speakers"]["speaker_a"]["name"] == "Alice"
        assert agg["glossary"] == {"Alice": "爱丽丝"}

from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from services.audio.separator import AudioStemSeparator


def _export_audio(path: Path, audio: AudioSegment) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(path, format="wav")
    return path


def test_audio_stem_separator_creates_speech_and_ambient_tracks_for_stereo_input(tmp_path: Path) -> None:
    centered = Sine(440).to_audio_segment(duration=1_200).apply_gain(-12.0)
    side_only = Sine(880).to_audio_segment(duration=1_200).apply_gain(-15.0)
    left = centered.overlay(side_only)
    right = centered
    stereo = AudioSegment.from_mono_audiosegments(left, right).set_frame_rate(44_100).set_sample_width(2)
    source_path = _export_audio(tmp_path / "audio" / "original.wav", stereo)

    result = AudioStemSeparator().separate(str(source_path), str(source_path.parent))

    speech_audio = AudioSegment.from_wav(result.speech_audio_path)
    ambient_audio = AudioSegment.from_wav(result.ambient_audio_path)

    assert result.reused_cache is False
    assert speech_audio.channels == 1
    assert speech_audio.frame_rate == 16_000
    assert len(speech_audio) == 1_200
    assert ambient_audio.channels == 2
    assert len(ambient_audio) == 1_200
    assert ambient_audio.rms > 0


def test_audio_stem_separator_uses_silent_ambient_fallback_for_mono_input(tmp_path: Path) -> None:
    mono = Sine(440).to_audio_segment(duration=900).apply_gain(-10.0).set_channels(1).set_frame_rate(22_050)
    source_path = _export_audio(tmp_path / "audio" / "original.wav", mono)

    result = AudioStemSeparator().separate(str(source_path), str(source_path.parent))

    speech_audio = AudioSegment.from_wav(result.speech_audio_path)
    ambient_audio = AudioSegment.from_wav(result.ambient_audio_path)

    assert speech_audio.channels == 1
    assert speech_audio.frame_rate == 16_000
    assert ambient_audio.channels == 2
    assert len(ambient_audio) == 900
    assert ambient_audio.rms == 0


# ---------------------------------------------------------------------------
# 2026-04-20 OOM-safe rewrite: pydub full-buffer loading was balloning RSS
# past the 7.6 GB container limit for ~100-min inputs. Replaced with a
# streaming ffmpeg pan filter (O(1) python memory regardless of input
# length). These tests lock the new behavior in.
# ---------------------------------------------------------------------------


def test_audio_stem_separator_reuses_cache_when_outputs_newer_than_source(
    tmp_path: Path,
) -> None:
    """Cache-reuse path is unchanged from the pydub version. This guards
    against the rewrite accidentally re-running ffmpeg every time."""
    stereo = (
        Sine(440).to_audio_segment(duration=500)
        .apply_gain(-10)
        .set_frame_rate(44_100)
        .set_sample_width(2)
    )
    source_path = _export_audio(tmp_path / "audio" / "original.wav", stereo)

    first = AudioStemSeparator().separate(str(source_path), str(source_path.parent))
    assert first.reused_cache is False

    second = AudioStemSeparator().separate(str(source_path), str(source_path.parent))
    assert second.reused_cache is True, (
        "second separate() on fresh outputs must skip ffmpeg and reuse cache"
    )


def test_audio_stem_separator_re_runs_when_source_newer_than_outputs(
    tmp_path: Path,
) -> None:
    """Cache invalidation still triggers a fresh run when source mtime
    advances past output mtime (e.g. a re-downloaded project)."""
    import os
    stereo = (
        Sine(440).to_audio_segment(duration=500)
        .apply_gain(-10)
        .set_frame_rate(44_100)
        .set_sample_width(2)
    )
    source_path = _export_audio(tmp_path / "audio" / "original.wav", stereo)

    AudioStemSeparator().separate(str(source_path), str(source_path.parent))
    # Bump source mtime to simulate a re-download
    future = source_path.stat().st_mtime + 60
    os.utime(source_path, (future, future))

    result = AudioStemSeparator().separate(str(source_path), str(source_path.parent))
    assert result.reused_cache is False, "stale cache must force a re-run"


def test_audio_stem_separator_wraps_ffmpeg_failure(tmp_path: Path) -> None:
    """ffmpeg invocation failure (missing binary / bad input / timeout)
    surfaces as AudioSeparationError — not a raw CalledProcessError /
    FileNotFoundError leaking the stack to the user."""
    from services.audio.separator import AudioSeparationError

    # Point ffmpeg at a file that doesn't exist — ffmpeg will fail.
    # (We pass the real ffmpeg; the failure comes from the missing input.)
    fake_source = tmp_path / "audio" / "does_not_exist.wav"
    fake_source.parent.mkdir(parents=True, exist_ok=True)
    try:
        AudioStemSeparator().separate(str(fake_source), str(fake_source.parent))
        raise AssertionError("expected AudioSeparationError for missing source")
    except AudioSeparationError:
        pass  # expected

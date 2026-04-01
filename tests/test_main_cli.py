import pytest
import builtins

import main
from core.enums import OutputTarget


def test_parse_local_audio_demo_args_defaults_to_editor_output() -> None:
    parsed = main.parse_local_audio_demo_args(
        ["main.py", "local-audio-demo", "sample.wav"]
    )

    assert parsed.translation_mode == "mock"
    assert parsed.tts_mode == "mock"
    assert parsed.output_target == OutputTarget.EDITOR


def test_parse_local_video_demo_args_accepts_publish_output() -> None:
    parsed = main.parse_local_video_demo_args(
        ["main.py", "local-video-demo", "sample.mp4", "real", "mock", "--output", "publish"]
    )

    assert parsed.translation_mode == "real"
    assert parsed.tts_mode == "mock"
    assert parsed.output_target == OutputTarget.PUBLISH


def test_parse_local_audio_demo_args_accepts_output_before_modes() -> None:
    parsed = main.parse_local_audio_demo_args(
        ["main.py", "local-audio-demo", "sample.wav", "--output", "editor", "real", "real"]
    )

    assert parsed.translation_mode == "real"
    assert parsed.tts_mode == "real"
    assert parsed.output_target == OutputTarget.EDITOR


def test_parse_local_video_demo_args_rejects_invalid_output_target() -> None:
    with pytest.raises(SystemExit, match="Unsupported output target"):
        main.parse_local_video_demo_args(
            ["main.py", "local-video-demo", "sample.mp4", "--output", "draft"]
        )


def test_parse_job_api_args_defaults_to_a1_port() -> None:
    parsed = main.parse_job_api_args(["main.py", "job-api"])

    assert parsed.port == main.JOB_API_DEFAULT_PORT


def test_parse_job_api_args_rejects_non_integer_port() -> None:
    with pytest.raises(SystemExit, match="job-api <port> must be an integer"):
        main.parse_job_api_args(["main.py", "job-api", "not-a-port"])


def test_run_process_command_completes_successfully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """run_process_command calls ProcessPipeline.run() and prints completion summary."""
    run_calls: list[object] = []

    class _FakePipeline:
        def run(self, config):
            run_calls.append(config)
            return type(
                "Result",
                (),
                {
                    "status": "completed",
                    "project_dir": str(tmp_path),
                    "dubbed_audio_path": str(tmp_path / "output" / "dubbed_audio_complete.wav"),
                    "segments_dir": str(tmp_path / "output" / "segments"),
                    "subtitles_path": str(tmp_path / "output" / "subtitles.srt"),
                    "alignment_report_path": str(tmp_path / "output" / "alignment_report.txt"),
                    "background_sounds_path": str(tmp_path / "output" / "background_sounds.txt"),
                    "total_segments": 3,
                    "needs_review_count": 0,
                },
            )()

    printed_lines: list[str] = []

    monkeypatch.setattr(main, "parse_process_args", lambda argv: object())
    monkeypatch.setattr(main, "ProcessPipeline", _FakePipeline)
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: printed_lines.append(" ".join(str(a) for a in args)))

    main.run_process_command(["main.py", "process", "https://example.test/video"])

    assert len(run_calls) == 1, "ProcessPipeline.run() should be called exactly once"
    assert any("处理完成" in line for line in printed_lines), "completion summary should be printed"


def test_run_process_command_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_process_command wraps pipeline exceptions in SystemExit."""

    class _FakePipeline:
        def run(self, config):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "parse_process_args", lambda argv: object())
    monkeypatch.setattr(main, "ProcessPipeline", _FakePipeline)

    with pytest.raises(SystemExit, match="process failed: boom"):
        main.run_process_command(["main.py", "process", "https://example.test/video"])


# ===================================================================
# parse_process_args — CLI source parameter tests
# ===================================================================


class TestParseProcessArgsSourceParams:
    """Verify legacy positional, new explicit, and mixed CLI source args."""

    def test_legacy_positional_youtube_url(self):
        config = main.parse_process_args(
            ["main.py", "process", "https://youtube.com/watch?v=abc"]
        )
        assert config.youtube_url == "https://youtube.com/watch?v=abc"
        assert config.source_type == "youtube_url"
        assert config.source_ref == "https://youtube.com/watch?v=abc"

    def test_explicit_youtube_url(self):
        config = main.parse_process_args(
            ["main.py", "process",
             "--source-type", "youtube_url",
             "--source-ref", "https://youtube.com/watch?v=xyz"]
        )
        assert config.source_type == "youtube_url"
        assert config.source_ref == "https://youtube.com/watch?v=xyz"
        # Back-filled for legacy pipeline compat
        assert config.youtube_url == "https://youtube.com/watch?v=xyz"

    def test_explicit_local_video(self):
        config = main.parse_process_args(
            ["main.py", "process",
             "--source-type", "local_video",
             "--source-ref", "/uploads/42/abc_video.mp4"]
        )
        assert config.source_type == "local_video"
        assert config.source_ref == "/uploads/42/abc_video.mp4"
        # youtube_url should be empty — not a YouTube source
        assert config.youtube_url == ""

    def test_explicit_local_audio(self):
        config = main.parse_process_args(
            ["main.py", "process",
             "--source-type", "local_audio",
             "--source-ref", "D:/input.wav"]
        )
        assert config.source_type == "local_audio"
        assert config.source_ref == "D:/input.wav"

    def test_explicit_takes_precedence_over_positional(self):
        """When both positional and explicit are given, explicit wins."""
        config = main.parse_process_args(
            ["main.py", "process", "https://youtube.com/old",
             "--source-type", "local_video",
             "--source-ref", "/uploads/new.mp4"]
        )
        assert config.source_type == "local_video"
        assert config.source_ref == "/uploads/new.mp4"
        assert config.youtube_url == ""  # non-YouTube: youtube_url cleared

    def test_explicit_youtube_overrides_positional_youtube_url(self):
        """Explicit --source-ref must override the positional youtube_url compat field."""
        config = main.parse_process_args(
            ["main.py", "process", "https://youtube.com/old",
             "--source-type", "youtube_url",
             "--source-ref", "https://youtube.com/new"]
        )
        assert config.source_type == "youtube_url"
        assert config.source_ref == "https://youtube.com/new"
        assert config.youtube_url == "https://youtube.com/new"

    def test_only_source_type_without_source_ref_is_rejected(self):
        with pytest.raises(SystemExit):
            main.parse_process_args(
                ["main.py", "process", "--source-type", "local_video"]
            )

    def test_only_source_ref_without_source_type_is_rejected(self):
        with pytest.raises(SystemExit):
            main.parse_process_args(
                ["main.py", "process", "--source-ref", "/tmp/x.mp4"]
            )

    def test_legacy_with_other_flags(self):
        config = main.parse_process_args(
            ["main.py", "process", "https://youtube.com/watch?v=test",
             "--speakers", "2", "--voice-a", "voice-001",
             "--job-id", "job_abc"]
        )
        assert config.source_type == "youtube_url"
        assert config.source_ref == "https://youtube.com/watch?v=test"
        assert config.speakers == "2"
        assert config.voice_a == "voice-001"
        assert config.job_id == "job_abc"

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


def test_run_process_command_shuts_down_tts_runtime_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    cleanup_calls: list[str] = []

    class _FakePipeline:
        def run(self, config):
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

    monkeypatch.setattr(main, "parse_process_args", lambda argv: object())
    monkeypatch.setattr(main, "ProcessPipeline", _FakePipeline)
    monkeypatch.setattr(main, "_shutdown_cli_tts_runtimes", lambda: cleanup_calls.append("done"))
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: None)

    main.run_process_command(["main.py", "process", "https://example.test/video"])

    assert cleanup_calls == ["done"]


def test_run_process_command_shuts_down_tts_runtime_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[str] = []

    class _FakePipeline:
        def run(self, config):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "parse_process_args", lambda argv: object())
    monkeypatch.setattr(main, "ProcessPipeline", _FakePipeline)
    monkeypatch.setattr(main, "_shutdown_cli_tts_runtimes", lambda: cleanup_calls.append("done"))

    with pytest.raises(SystemExit, match="process failed: boom"):
        main.run_process_command(["main.py", "process", "https://example.test/video"])

    assert cleanup_calls == ["done"]

import pytest

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

import shutil
import subprocess
from pathlib import Path

import pytest

from core.exceptions import PublishError
from modules.output.publish import PublishBackend, PublishRequest, VideoRenderer


def test_publish_backend_generates_minimal_dubbed_video_with_renderer(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"
    original_video_path.write_bytes(b"fake-video")
    dubbed_audio_path.write_bytes(b"fake-audio")

    def fake_runner(command: list[str]) -> None:
        Path(command[-1]).write_bytes(b"rendered-video")

    backend = PublishBackend(renderer=VideoRenderer(command_runner=fake_runner))
    result = backend.publish(
        PublishRequest(
            project_id="publish-demo",
            original_video_path=str(original_video_path),
            dubbed_audio_path=str(dubbed_audio_path),
            output_dir=str(tmp_path / "publish"),
        )
    )

    assert Path(result.dubbed_video_path).exists()
    assert Path(result.dubbed_video_path).name == "dubbed_video.mp4"
    assert result.project_id == "publish-demo"
    assert result.original_video_path == str(original_video_path)
    assert result.dubbed_audio_path == str(dubbed_audio_path)


def test_video_renderer_mixes_ambient_audio_when_available(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"
    ambient_audio_path = tmp_path / "ambient.wav"
    original_video_path.write_bytes(b"fake-video")
    dubbed_audio_path.write_bytes(b"fake-audio")
    ambient_audio_path.write_bytes(b"fake-ambient")
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"rendered")

    result = VideoRenderer(command_runner=fake_runner).render(
        PublishRequest(
            project_id="publish-with-ambient",
            original_video_path=str(original_video_path),
            dubbed_audio_path=str(dubbed_audio_path),
            ambient_audio_path=str(ambient_audio_path),
            output_dir=str(tmp_path / "publish"),
        )
    )

    mux_command = commands[0]
    assert Path(result.dubbed_video_path).exists()
    assert str(ambient_audio_path.resolve(strict=False)) in mux_command
    assert mux_command.count("-i") == 3
    assert "-filter_complex" in mux_command
    filter_graph = mux_command[mux_command.index("-filter_complex") + 1]
    assert "volume=-12.0dB" in filter_graph
    assert "amix=inputs=2" in filter_graph


def test_video_renderer_rejects_missing_publish_input(tmp_path: Path) -> None:
    dubbed_audio_path = tmp_path / "dubbed.wav"
    dubbed_audio_path.write_bytes(b"fake-audio")

    with pytest.raises(PublishError, match="original_video_path"):
        VideoRenderer(command_runner=lambda command: None).render(
            PublishRequest(
                project_id="publish-missing-input",
                original_video_path=str(tmp_path / "missing.mp4"),
                dubbed_audio_path=str(dubbed_audio_path),
                output_dir=str(tmp_path / "publish"),
            )
        )


def test_video_renderer_surfaces_missing_ffmpeg_runtime(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"
    original_video_path.write_bytes(b"fake-video")
    dubbed_audio_path.write_bytes(b"fake-audio")

    def missing_ffmpeg_runner(command: list[str]) -> None:
        del command
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(PublishError, match="ffmpeg executable not found"):
        VideoRenderer(command_runner=missing_ffmpeg_runner).render(
            PublishRequest(
                project_id="publish-missing-ffmpeg",
                original_video_path=str(original_video_path),
                dubbed_audio_path=str(dubbed_audio_path),
                output_dir=str(tmp_path / "publish"),
            )
        )


def test_video_renderer_surfaces_ffmpeg_stderr_on_failure(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"
    original_video_path.write_bytes(b"fake-video")
    dubbed_audio_path.write_bytes(b"fake-audio")

    def failed_runner(command: list[str]) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            stderr="Invalid data found when processing input",
        )

    with pytest.raises(PublishError, match="Invalid data found when processing input"):
        VideoRenderer(command_runner=failed_runner).render(
            PublishRequest(
                project_id="publish-invalid-media",
                original_video_path=str(original_video_path),
                dubbed_audio_path=str(dubbed_audio_path),
                output_dir=str(tmp_path / "publish"),
            )
        )


def test_video_renderer_rejects_directory_as_publish_input(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    original_video_path.write_bytes(b"fake-video")
    audio_dir = tmp_path / "dubbed_dir"
    audio_dir.mkdir()

    with pytest.raises(PublishError, match="must be a file"):
        VideoRenderer(command_runner=lambda command: None).render(
            PublishRequest(
                project_id="publish-invalid-input-type",
                original_video_path=str(original_video_path),
                dubbed_audio_path=str(audio_dir),
                output_dir=str(tmp_path / "publish"),
            )
        )


def test_video_renderer_rejects_empty_publish_output(tmp_path: Path) -> None:
    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"
    original_video_path.write_bytes(b"fake-video")
    dubbed_audio_path.write_bytes(b"fake-audio")

    def empty_output_runner(command: list[str]) -> None:
        Path(command[-1]).write_bytes(b"")

    with pytest.raises(PublishError, match="empty output file"):
        VideoRenderer(command_runner=empty_output_runner).render(
            PublishRequest(
                project_id="publish-empty-output",
                original_video_path=str(original_video_path),
                dubbed_audio_path=str(dubbed_audio_path),
                output_dir=str(tmp_path / "publish"),
            )
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_video_renderer_renders_real_minimal_video_with_ffmpeg(tmp_path: Path) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    assert ffmpeg_path is not None

    original_video_path = tmp_path / "source.mp4"
    dubbed_audio_path = tmp_path / "dubbed.wav"

    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(original_video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.8",
            str(dubbed_audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = VideoRenderer(ffmpeg_executable=ffmpeg_path).render(
        PublishRequest(
            project_id="publish-real-minimal-video",
            original_video_path=str(original_video_path),
            dubbed_audio_path=str(dubbed_audio_path),
            output_dir=str(tmp_path / "publish"),
        )
    )

    rendered_path = Path(result.dubbed_video_path)
    assert rendered_path.exists()
    assert rendered_path.suffix == ".mp4"
    assert rendered_path.stat().st_size > 0

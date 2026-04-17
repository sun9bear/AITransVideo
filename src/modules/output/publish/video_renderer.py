from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
import subprocess

from core.exceptions import PublishError
from modules.output.publish.publish_models import PublishRequest, PublishResult


CommandRunner = Callable[[list[str]], object]
ProgressCallback = Callable[[dict[str, Any]], None]


class VideoRenderer:
    """Render the minimal publish artifact by muxing source video and dubbed audio."""

    def __init__(
        self,
        *,
        ffmpeg_executable: str = "ffmpeg",
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.ffmpeg_executable = ffmpeg_executable
        self.command_runner = command_runner or self._run_command

    def render(
        self,
        request: PublishRequest,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PublishResult:
        def _notify(stage: str, percent: int) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback({"stage": stage, "percent": percent})
            except Exception:  # noqa: BLE001 — progress is advisory, never fail render
                pass

        _notify("starting", 5)
        original_video_path = self._require_existing_file_path(
            request.original_video_path,
            field_name="original_video_path",
        )
        dubbed_audio_path = self._require_existing_file_path(
            request.dubbed_audio_path,
            field_name="dubbed_audio_path",
        )
        output_dir = Path(request.output_dir).resolve(strict=False)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PublishError(f"Unable to create publish output directory: {output_dir}") from exc
        output_filename = request.output_filename.strip() or "dubbed_video.mp4"
        output_path = output_dir / output_filename

        # Check for ambient audio (background sounds separated during S0)
        ambient_audio_path: Path | None = None
        raw_ambient = getattr(request, "ambient_audio_path", None)
        if raw_ambient and isinstance(raw_ambient, str) and raw_ambient.strip():
            candidate = Path(raw_ambient.strip()).resolve(strict=False)
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                ambient_audio_path = candidate

        if ambient_audio_path is not None:
            # Three-track mix: video + dubbed audio + ambient audio (lowered volume)
            vol_db = getattr(request, "ambient_volume_db", -12.0)
            command = [
                self.ffmpeg_executable,
                "-y",
                "-i", str(original_video_path),
                "-i", str(dubbed_audio_path),
                "-i", str(ambient_audio_path),
                "-filter_complex",
                f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo[dub];"
                f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"volume={vol_db}dB[amb];"
                f"[dub][amb]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v:0",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            # Two-track: video + dubbed audio only (no ambient available)
            command = [
                self.ffmpeg_executable,
                "-y",
                "-i", str(original_video_path),
                "-i", str(dubbed_audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ]
        _notify("muxing", 20)
        try:
            self.command_runner(command)
        except FileNotFoundError as exc:
            raise PublishError(
                f"ffmpeg executable not found for publish rendering: {self.ffmpeg_executable}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise PublishError(
                f"Publish rendering failed: {self._summarize_process_output(exc.stderr, exc.stdout, fallback=str(exc))}"
            ) from exc
        except OSError as exc:
            raise PublishError(f"Publish rendering could not start: {exc}") from exc

        _notify("finalizing", 90)
        self._validate_rendered_output(output_path)

        _notify("done", 100)
        return PublishResult(
            project_id=request.project_id,
            dubbed_video_path=str(output_path),
            original_video_path=str(original_video_path),
            dubbed_audio_path=str(dubbed_audio_path),
        )

    @staticmethod
    def _require_existing_file_path(path: str, *, field_name: str) -> Path:
        normalized_input = path.strip() if isinstance(path, str) else ""
        if not normalized_input:
            raise PublishError(f"Missing required publish input {field_name}.")
        normalized_path = Path(normalized_input).resolve(strict=False)
        if not normalized_path.exists():
            raise PublishError(f"Missing required publish input {field_name}: {path}")
        if not normalized_path.is_file():
            raise PublishError(f"Publish input {field_name} must be a file: {path}")
        if normalized_path.stat().st_size <= 0:
            raise PublishError(f"Publish input {field_name} is empty: {path}")
        return normalized_path

    @staticmethod
    def _validate_rendered_output(output_path: Path) -> None:
        if not output_path.exists():
            raise PublishError(f"Publish rendering did not produce output: {output_path}")
        if not output_path.is_file():
            raise PublishError(f"Publish rendering produced an invalid output path: {output_path}")
        if output_path.stat().st_size <= 0:
            raise PublishError(f"Publish rendering produced an empty output file: {output_path}")

    @staticmethod
    def _summarize_process_output(stderr: object, stdout: object, *, fallback: str) -> str:
        for candidate in (stderr, stdout):
            if isinstance(candidate, str):
                normalized = " ".join(candidate.strip().split())
                if normalized:
                    return normalized[:400]
        return fallback[:400]

    @staticmethod
    def _run_command(command: list[str]) -> None:
        subprocess.run(command, check=True, capture_output=True, text=True)

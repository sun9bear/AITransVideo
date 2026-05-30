from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PublishRequest:
    project_id: str
    original_video_path: str
    dubbed_audio_path: str
    output_dir: str
    output_filename: str = "dubbed_video.mp4"
    ambient_audio_path: str | None = None
    ambient_volume_db: float = -12.0
    # Phase 2a Task 8 (gate #8): when set (free service mode), the renderer burns
    # this text into the video via ffmpeg drawtext (forces a re-encode). None /
    # empty -> clean -c:v copy mux (paid modes).
    watermark_text: str | None = None


@dataclass(slots=True)
class PublishResult:
    project_id: str
    dubbed_video_path: str
    original_video_path: str
    dubbed_audio_path: str
    poster_path: str | None = None

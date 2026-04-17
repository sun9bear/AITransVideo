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


@dataclass(slots=True)
class PublishResult:
    project_id: str
    dubbed_video_path: str
    original_video_path: str
    dubbed_audio_path: str

from __future__ import annotations

from dataclasses import dataclass

from .constants import WEB_UI_DEFAULT_PORT


@dataclass(frozen=True, slots=True)
class WebUICommandArgs:
    port: int = WEB_UI_DEFAULT_PORT


@dataclass(slots=True)
class ProcessJobSnapshot:
    job_id: str | None
    status: str
    youtube_url: str
    speakers: str
    voice_a: str | None
    voice_b: str | None
    translation_model_alias: str
    project_dir: str | None = None
    current_stage: str | None = None
    current_message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    returncode: int | None = None
    logs: list[str] | None = None
    review_gate: dict[str, object] | None = None
    control_mode: str = "legacy_process"

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "youtube_url": self.youtube_url,
            "speakers": self.speakers,
            "voice_a": self.voice_a,
            "voice_b": self.voice_b,
            "translation_model_alias": self.translation_model_alias,
            "project_dir": self.project_dir,
            "current_stage": self.current_stage,
            "current_message": self.current_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "returncode": self.returncode,
            "logs": list(self.logs or []),
            "review_gate": dict(self.review_gate or {}),
            "control_mode": self.control_mode,
        }

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JOB_TYPE_LOCALIZE_VIDEO = "localize_video"

SOURCE_TYPE_YOUTUBE_URL = "youtube_url"
SOURCE_TYPE_LOCAL_AUDIO = "local_audio"
SOURCE_TYPE_LOCAL_VIDEO = "local_video"
# The model keeps reserved source slots, while the A1 public service contract
# intentionally accepts only youtube_url.
SUPPORTED_SOURCE_TYPES = {
    SOURCE_TYPE_YOUTUBE_URL,
    SOURCE_TYPE_LOCAL_AUDIO,
    SOURCE_TYPE_LOCAL_VIDEO,
}

OUTPUT_TARGET_EDITOR = "editor"
OUTPUT_TARGET_PUBLISH = "publish"
OUTPUT_TARGET_BOTH = "both"
SUPPORTED_OUTPUT_TARGETS = {
    OUTPUT_TARGET_EDITOR,
    OUTPUT_TARGET_PUBLISH,
    OUTPUT_TARGET_BOTH,
}

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_WAITING_FOR_REVIEW = "waiting_for_review"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
SUPPORTED_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
}
ACTIVE_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
}

STAGE_INGESTION = "ingestion"
STAGE_MEDIA_UNDERSTANDING = "media_understanding"
STAGE_SPEAKER_REVIEW = "speaker_review"
STAGE_TRANSLATION_CONFIG_REVIEW = "translation_config_review"
STAGE_TRANSLATION_REVIEW = "translation_review"
STAGE_VOICE_REVIEW = "voice_review"
STAGE_VOICE_SELECTION_REVIEW = "voice_selection_review"
STAGE_DRAFT = "draft"
STAGE_LEGACY_PROCESS_OUTPUT = "legacy_process_output"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
SUPPORTED_PUBLIC_STAGES = {
    STAGE_INGESTION,
    STAGE_MEDIA_UNDERSTANDING,
    STAGE_SPEAKER_REVIEW,
    STAGE_TRANSLATION_CONFIG_REVIEW,
    STAGE_TRANSLATION_REVIEW,
    STAGE_VOICE_REVIEW,
    STAGE_VOICE_SELECTION_REVIEW,
    STAGE_DRAFT,
    STAGE_LEGACY_PROCESS_OUTPUT,
    STAGE_COMPLETED,
    STAGE_FAILED,
}


@dataclass(slots=True)
class JobRecord:
    job_id: str
    job_type: str
    source_type: str
    source_ref: str
    output_target: str
    speakers: str
    voice_a: str | None
    voice_b: str | None
    status: str
    current_stage: str | None
    progress_message: str | None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    project_dir: str | None = None
    manifest_path: str | None = None
    review_gate: dict[str, object] | None = None
    error_summary: dict[str, object] | None = None
    fallback_summary: dict[str, object] | None = None
    transcription_method: str = "assemblyai"
    service_mode: str | None = None
    tts_provider: str | None = None
    tts_model: str | None = None
    requires_review: bool | None = None
    voice_clone_enabled: bool | None = None
    voice_strategy: str | None = None
    plan_code_snapshot: str | None = None
    role_snapshot: str | None = None
    source_duration_seconds: float | None = None
    estimated_duration_seconds: float | None = None
    quota_cost: int | None = None
    quota_state: str = "none"
    create_idempotency_key: str | None = None
    user_id: str | None = None
    workspace_dir: str | None = None
    source_content_hash: str | None = None

    def __post_init__(self) -> None:
        self.job_id = str(self.job_id).strip()
        self.job_type = str(self.job_type).strip()
        self.source_type = str(self.source_type).strip()
        self.source_ref = str(self.source_ref).strip()
        self.output_target = str(self.output_target).strip().lower()
        self.speakers = str(self.speakers).strip().lower()
        self.voice_a = _normalize_optional_text(self.voice_a)
        self.voice_b = _normalize_optional_text(self.voice_b)
        self.status = str(self.status).strip().lower()
        self.current_stage = _normalize_optional_text(self.current_stage)
        self.progress_message = _normalize_optional_text(self.progress_message)
        self.created_at = str(self.created_at).strip()
        self.updated_at = str(self.updated_at).strip()
        self.started_at = _normalize_optional_text(self.started_at)
        self.completed_at = _normalize_optional_text(self.completed_at)
        self.project_dir = _normalize_optional_text(self.project_dir)
        self.manifest_path = _normalize_optional_text(self.manifest_path)
        self.review_gate = _copy_optional_dict(self.review_gate)
        self.error_summary = _copy_optional_dict(self.error_summary)
        self.fallback_summary = _copy_optional_dict(self.fallback_summary)
        self.user_id = _normalize_optional_text(self.user_id)
        self.workspace_dir = _normalize_optional_text(self.workspace_dir)
        self.source_content_hash = _normalize_optional_text(self.source_content_hash)

        if not self.job_id:
            raise ValueError("job_id is required")
        if not self.job_type:
            raise ValueError("job_type is required")
        if self.source_type not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(f"Unsupported source_type: {self.source_type}")
        if not self.source_ref:
            raise ValueError("source_ref is required")
        if self.output_target not in SUPPORTED_OUTPUT_TARGETS:
            raise ValueError(f"Unsupported output_target: {self.output_target}")
        if self.speakers not in {"auto", "1", "2"}:
            raise ValueError(f"Unsupported speakers value: {self.speakers}")
        if self.status not in SUPPORTED_JOB_STATUSES:
            raise ValueError(f"Unsupported job status: {self.status}")
        if self.current_stage is not None and self.current_stage not in SUPPORTED_PUBLIC_STAGES:
            raise ValueError(f"Unsupported public stage: {self.current_stage}")
        if not self.created_at:
            raise ValueError("created_at is required")
        if not self.updated_at:
            raise ValueError("updated_at is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "output_target": self.output_target,
            "speakers": self.speakers,
            "voice_a": self.voice_a,
            "voice_b": self.voice_b,
            "status": self.status,
            "current_stage": self.current_stage,
            "progress_message": self.progress_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "project_dir": self.project_dir,
            "manifest_path": self.manifest_path,
            "review_gate": _serialize_optional_dict(self.review_gate),
            "error_summary": _serialize_optional_dict(self.error_summary),
            "fallback_summary": _serialize_optional_dict(self.fallback_summary),
            "transcription_method": self.transcription_method,
            "service_mode": self.service_mode,
            "tts_provider": self.tts_provider,
            "tts_model": self.tts_model,
            "requires_review": self.requires_review,
            "voice_clone_enabled": self.voice_clone_enabled,
            "voice_strategy": self.voice_strategy,
            "plan_code_snapshot": self.plan_code_snapshot,
            "role_snapshot": self.role_snapshot,
            "source_duration_seconds": self.source_duration_seconds,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "quota_cost": self.quota_cost,
            "quota_state": self.quota_state,
            "create_idempotency_key": self.create_idempotency_key,
            "user_id": self.user_id,
            "workspace_dir": self.workspace_dir,
            "source_content_hash": self.source_content_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobRecord:
        return cls(
            job_id=payload["job_id"],
            job_type=payload["job_type"],
            source_type=payload["source_type"],
            source_ref=payload["source_ref"],
            output_target=payload["output_target"],
            speakers=payload.get("speakers", "auto"),
            voice_a=payload.get("voice_a"),
            voice_b=payload.get("voice_b"),
            status=payload["status"],
            current_stage=payload.get("current_stage"),
            progress_message=payload.get("progress_message"),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            started_at=payload.get("started_at"),
            completed_at=payload.get("completed_at"),
            project_dir=payload.get("project_dir"),
            manifest_path=payload.get("manifest_path"),
            review_gate=payload.get("review_gate"),
            error_summary=payload.get("error_summary"),
            fallback_summary=payload.get("fallback_summary"),
            transcription_method=payload.get("transcription_method", "assemblyai"),
            service_mode=payload.get("service_mode"),
            tts_provider=payload.get("tts_provider"),
            tts_model=payload.get("tts_model"),
            requires_review=payload.get("requires_review"),
            voice_clone_enabled=payload.get("voice_clone_enabled"),
            voice_strategy=payload.get("voice_strategy"),
            plan_code_snapshot=payload.get("plan_code_snapshot"),
            role_snapshot=payload.get("role_snapshot"),
            source_duration_seconds=payload.get("source_duration_seconds"),
            estimated_duration_seconds=payload.get("estimated_duration_seconds"),
            quota_cost=payload.get("quota_cost"),
            quota_state=payload.get("quota_state", "none"),
            create_idempotency_key=payload.get("create_idempotency_key"),
            user_id=payload.get("user_id"),
            workspace_dir=payload.get("workspace_dir"),
            source_content_hash=payload.get("source_content_hash"),
        )


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized_value = str(value).strip()
    return normalized_value or None


def _copy_optional_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return dict(value)


def _serialize_optional_dict(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return dict(value)

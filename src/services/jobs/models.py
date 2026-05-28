from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JOB_TYPE_LOCALIZE_VIDEO = "localize_video"

# Jianying draft on-demand generation (plan §11.5, K2 task)
VALID_JIANYING_DRAFT_STATUSES = frozenset({"idle", "running", "succeeded", "failed"})

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
JOB_STATUS_EDITING = "editing"  # Post-edit workflow (plan 2026-04-18, D21)
# 2026-04-21: soft-delete marker written by Gateway's project_cleanup
# once the 7d retention window elapses. Kept in DB so the user retains a
# history entry ("过期已清理任务 X"); project_dir on disk is gone.
JOB_STATUS_PURGED = "purged"
# Pan backup statuses (plan 2026-05-14 Task 1.3 / design 2026-05-13 §4.1).
# archiving = transient: backup_executor uploading to pan
# archived  = terminal: tar.gz on pan, local + R2 deleted
# restoring = transient: restore_executor downloading from pan + extracting
JOB_STATUS_ARCHIVING = "archiving"
JOB_STATUS_ARCHIVED = "archived"
JOB_STATUS_RESTORING = "restoring"
SUPPORTED_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_PURGED,
    JOB_STATUS_ARCHIVING,
    JOB_STATUS_ARCHIVED,
    JOB_STATUS_RESTORING,
}
ACTIVE_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_STATUS_EDITING,  # editing is active — cleanup / list-page polling must see it
    JOB_STATUS_ARCHIVING,   # pan transient
    JOB_STATUS_RESTORING,   # pan transient
}
# Subset of ACTIVE_JOB_STATUSES that require a live worker process. Reap-stale
# logic uses this (NOT ACTIVE_JOB_STATUSES) so that editing/waiting_for_review
# jobs — which legitimately have no worker — are not mis-flagged as failed.
# See docs/internal/status-touchpoints-2026-04-18.md §0.
WORKER_ACTIVE_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
}

STAGE_INGESTION = "ingestion"
STAGE_MEDIA_UNDERSTANDING = "media_understanding"
STAGE_SPEAKER_REVIEW = "speaker_review"
STAGE_TRANSLATION_CONFIG_REVIEW = "translation_config_review"
STAGE_TRANSLATION_REVIEW = "translation_review"
STAGE_VOICE_REVIEW = "voice_review"
STAGE_VOICE_SELECTION_REVIEW = "voice_selection_review"
STAGE_DRAFT = "draft"
# Post-edit commit entry point (T1-8). Commits re-run pipeline starting at
# alignment, skipping ingestion / transcription / translation — those have
# already been paid for by the original run and their outputs are what
# editing edits. NEVER reached in normal forward-flow jobs; always
# explicitly set by runner_extensions.submit_job_from_existing_project_dir.
STAGE_ALIGNMENT = "alignment"
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
    STAGE_ALIGNMENT,
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
    source_video_title: str | None = None
    source_published_at: str | None = None
    source_content_summary: str | None = None
    source_content_era: str | None = None
    source_content_tags: object | None = None
    # --- Post-edit infra (plan 2026-04-18 §3.1) ---
    display_name: str | None = None
    expires_at: str | None = None           # ISO-8601
    editing_touched_at: str | None = None   # ISO-8601; §5.4.1 refresh points
    copy_of_job_id: str | None = None       # direct parent of a copy
    root_job_id: str | None = None          # lineage root; originals: == job_id
    edit_generation: int = 0                # editing→running→succeeded cycles

    # --- Jianying draft on-demand generation (plan §11, K2/K3 task) ---
    jianying_draft_status: str = "idle"     # idle / running / succeeded / failed
    jianying_draft_started_at: str | None = None   # ISO 8601 UTC
    jianying_draft_completed_at: str | None = None
    jianying_draft_error: str | None = None
    jianying_draft_zip_path: str | None = None  # zip path for download (set when status=succeeded)
    jianying_draft_user_root: str | None = None  # user's local drafts root used for absolute paths (K11)
    # --- Runner hardening (plan 2026-05-03 §A3) ---
    jianying_draft_fingerprint: str | None = None  # SHA256 of artifact-content + version inputs (§A4)
    jianying_draft_attempt_id: str | None = None   # UUID for current attempt; correlates lock / events
    jianying_draft_substep: str | None = None      # internal sub-step (validating_inputs / building_draft / ...)

    # --- Smart MVP P2 skeleton (plan 2026-05-13 §4.2 / §4.3) ---
    # State machine snapshot for Smart pipeline. Written by pipeline via
    # `[SMART_STATE] {...}` stdout marker → process_runner parses → JobStore
    # update → Gateway mirror via metering callback whitelist. Read by
    # settle_job_credit_ledger (smart dispatcher), editing/jianying gates,
    # admin diagnostics. Always None for express/studio jobs.
    # Shape: {"status": "...", "reason": "...", "handoff_stage": "...",
    #         "credits_policy": "...", "reserved_credits_per_minute": int, ...}
    smart_state: dict[str, object] | None = None

    # --- Smart MVP P2 entry-input (PR#3C-b3g, 2026-05-15) ---
    # User's smart-mode consent payload (plan §4.2). Written by Gateway at
    # job creation from the request body, persisted on JobRecord, read by
    # pipeline via ``_snap("smart_consent")`` to gate auto-clone /
    # auto-translation-review / retry-budget paths. Always None for
    # express/studio jobs.
    # Shape: {"auto_voice_clone": bool, ...future plan-§4.2 keys}.
    # NB: ``auto_voice_clone is True`` (strict identity) is the gate
    # — truthy values that aren't True (1, "true", {}, …) all fall to
    # PRESET. See ``services.smart.auto_voice_review`` for the contract
    # and tests/test_smart_auto_voice_review.py:206 for enforcement.
    smart_consent: dict[str, object] | None = None

    # --- Phase 4.3a Express auto-clone (canary, 2026-05-28) ---
    # User's express-mode consent payload (spec v0.3 §3). Written by
    # Gateway at job creation from the request body. Persisted on
    # JobRecord, read by pipeline (Phase 4.3a F) to gate the Express
    # CosyVoice auto-clone canary path. Always None for studio/smart jobs.
    # Shape: {
    #   "auto_voice_clone": bool,          # explicit opt-in (strict True)
    #   "client_confirmed_at": str | None, # frontend timestamp, untrusted
    #   "server_confirmed_at": str | None, # gateway-generated UTC ISO8601;
    #                                      # set ONLY when auto_voice_clone
    #                                      # is True; the authoritative
    #                                      # timestamp for worker request +
    #                                      # audit (spec §3.1.a).
    # }
    # ``auto_voice_clone is True`` is the gate (strict identity, matches
    # smart_consent style). Pipeline must read ``server_confirmed_at``
    # not ``client_confirmed_at`` for worker / audit-level consent time.
    express_consent: dict[str, object] | None = None
    # Parse error reason from ``validate_express_consent`` when the
    # raw payload was malformed (e.g. ``auto_voice_clone_not_bool``).
    # Soft-skip semantics: presence of a non-None value means the
    # client sent a malformed payload but the job still continues
    # without auto-clone — pipeline writes the reason to audit JSONL
    # so排障 can distinguish "user did not opt in" from "client bug".
    # spec §3.1.a + §9.1 audit schema.
    express_consent_parse_error: str | None = None

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
        self.smart_state = _copy_optional_dict(self.smart_state)
        self.smart_consent = _copy_optional_dict(self.smart_consent)
        # Phase 4.3a: deep-copy and normalize express_consent + parse_error
        self.express_consent = _copy_optional_dict(self.express_consent)
        self.express_consent_parse_error = _normalize_optional_text(
            self.express_consent_parse_error
        )
        self.user_id = _normalize_optional_text(self.user_id)
        self.workspace_dir = _normalize_optional_text(self.workspace_dir)
        self.source_content_hash = _normalize_optional_text(self.source_content_hash)
        self.source_video_title = _normalize_optional_text(self.source_video_title)
        self.source_published_at = _normalize_optional_text(self.source_published_at)
        self.source_content_summary = _normalize_optional_text(self.source_content_summary)
        self.source_content_era = _normalize_optional_text(self.source_content_era)
        self.source_content_tags = _copy_optional_json(self.source_content_tags)
        # --- Post-edit fields normalize ---
        self.display_name = _normalize_optional_text(self.display_name)
        self.expires_at = _normalize_optional_text(self.expires_at)
        self.editing_touched_at = _normalize_optional_text(self.editing_touched_at)
        self.copy_of_job_id = _normalize_optional_text(self.copy_of_job_id)
        self.root_job_id = _normalize_optional_text(self.root_job_id)
        # Originals: root_job_id == job_id (ensures TTL lookup works for pre-
        # post-edit data that was migrated in without an explicit root set).
        if self.root_job_id is None:
            self.root_job_id = self.job_id

        # --- Jianying draft fields normalize ---
        self.jianying_draft_status = str(self.jianying_draft_status).strip().lower()
        self.jianying_draft_started_at = _normalize_optional_text(self.jianying_draft_started_at)
        self.jianying_draft_completed_at = _normalize_optional_text(self.jianying_draft_completed_at)
        self.jianying_draft_error = _normalize_optional_text(self.jianying_draft_error)
        self.jianying_draft_zip_path = _normalize_optional_text(self.jianying_draft_zip_path)
        self.jianying_draft_user_root = _normalize_optional_text(self.jianying_draft_user_root)
        self.jianying_draft_fingerprint = _normalize_optional_text(self.jianying_draft_fingerprint)
        self.jianying_draft_attempt_id = _normalize_optional_text(self.jianying_draft_attempt_id)
        self.jianying_draft_substep = _normalize_optional_text(self.jianying_draft_substep)

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
            "source_video_title": self.source_video_title,
            "source_published_at": self.source_published_at,
            "source_content_summary": self.source_content_summary,
            "source_content_era": self.source_content_era,
            "source_content_tags": _copy_optional_json(self.source_content_tags),
            # --- Post-edit infra ---
            "display_name": self.display_name,
            "expires_at": self.expires_at,
            "editing_touched_at": self.editing_touched_at,
            "copy_of_job_id": self.copy_of_job_id,
            "root_job_id": self.root_job_id,
            "edit_generation": self.edit_generation,
            # --- Jianying draft on-demand generation ---
            "jianying_draft_status": self.jianying_draft_status,
            "jianying_draft_started_at": self.jianying_draft_started_at,
            "jianying_draft_completed_at": self.jianying_draft_completed_at,
            "jianying_draft_error": self.jianying_draft_error,
            "jianying_draft_zip_path": self.jianying_draft_zip_path,
            "jianying_draft_user_root": self.jianying_draft_user_root,
            "jianying_draft_fingerprint": self.jianying_draft_fingerprint,
            "jianying_draft_attempt_id": self.jianying_draft_attempt_id,
            "jianying_draft_substep": self.jianying_draft_substep,
            # --- Smart MVP P2 skeleton ---
            "smart_state": _serialize_optional_dict(self.smart_state),
            # --- Smart MVP P2 entry-input (PR#3C-b3g) ---
            "smart_consent": _serialize_optional_dict(self.smart_consent),
            # --- Phase 4.3a Express auto-clone entry-input ---
            "express_consent": _serialize_optional_dict(self.express_consent),
            "express_consent_parse_error": self.express_consent_parse_error,
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
            source_video_title=payload.get("source_video_title"),
            source_published_at=payload.get("source_published_at"),
            source_content_summary=payload.get("source_content_summary"),
            source_content_era=payload.get("source_content_era"),
            source_content_tags=payload.get("source_content_tags"),
            # --- Post-edit infra ---
            display_name=payload.get("display_name"),
            expires_at=payload.get("expires_at"),
            editing_touched_at=payload.get("editing_touched_at"),
            copy_of_job_id=payload.get("copy_of_job_id"),
            root_job_id=payload.get("root_job_id"),
            edit_generation=int(payload.get("edit_generation") or 0),
            # --- Jianying draft on-demand generation (backward compat: defaults to "idle"/None) ---
            jianying_draft_status=payload.get("jianying_draft_status", "idle"),
            jianying_draft_started_at=payload.get("jianying_draft_started_at"),
            jianying_draft_completed_at=payload.get("jianying_draft_completed_at"),
            jianying_draft_error=payload.get("jianying_draft_error"),
            jianying_draft_zip_path=payload.get("jianying_draft_zip_path"),
            jianying_draft_user_root=payload.get("jianying_draft_user_root"),
            jianying_draft_fingerprint=payload.get("jianying_draft_fingerprint"),
            jianying_draft_attempt_id=payload.get("jianying_draft_attempt_id"),
            jianying_draft_substep=payload.get("jianying_draft_substep"),
            # --- Smart MVP P2 skeleton ---
            smart_state=payload.get("smart_state"),
            # --- Smart MVP P2 entry-input (PR#3C-b3g) ---
            smart_consent=payload.get("smart_consent"),
            # --- Phase 4.3a Express auto-clone entry-input ---
            express_consent=payload.get("express_consent"),
            express_consent_parse_error=payload.get("express_consent_parse_error"),
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


def _copy_optional_json(value: object) -> object | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return None


def _serialize_optional_dict(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return dict(value)

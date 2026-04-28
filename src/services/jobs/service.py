from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from services.job_paths import build_workspace_dir
from services.jobs.events import EVENT_LEVEL_ERROR, EVENT_TYPE_STATUS, JobEvent
from services.jobs.models import (
    ACTIVE_JOB_STATUSES,
    WORKER_ACTIVE_STATUSES,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_TYPE_LOCALIZE_VIDEO,
    OUTPUT_TARGET_EDITOR,
    SOURCE_TYPE_YOUTUBE_URL,
    SUPPORTED_SOURCE_TYPES,
    STAGE_FAILED,
    JobRecord,
)
from services.jobs.process_runner import ProcessJobRunner, is_review_stage_approved
from services.jobs.read_surface import build_job_artifacts_payload, build_job_result_summary
from services.jobs.store import JobStore
from services.state_manager import utc_now_iso


class JobServiceError(Exception):
    """Base error for the A1 job service."""


class JobNotFoundError(JobServiceError):
    """Raised when a job_id does not exist."""


class JobConflictError(JobServiceError):
    """Raised when a lifecycle action conflicts with the current job state."""


class UnsupportedJobRequestError(JobServiceError):
    """Raised when a request is outside the A1 public contract."""


class JobService:
    def __init__(
        self,
        *,
        store: JobStore,
        runner: ProcessJobRunner,
    ) -> None:
        self.store = store
        self.runner = runner

    def submit_job(
        self,
        *,
        source_type: str,
        source_ref: str,
        output_target: str = OUTPUT_TARGET_EDITOR,
        job_type: str = JOB_TYPE_LOCALIZE_VIDEO,
        speakers: str = "auto",
        voice_a: str | None = None,
        voice_b: str | None = None,
        transcription_method: str | None = None,
        service_mode: str | None = None,
        tts_provider: str | None = None,
        tts_model: str | None = None,
        requires_review: bool | None = None,
        voice_clone_enabled: bool | None = None,
        voice_strategy: str | None = None,
        plan_code_snapshot: str | None = None,
        role_snapshot: str | None = None,
        source_duration_seconds: float | None = None,
        estimated_duration_seconds: float | None = None,
        quota_cost: int | None = None,
        quota_state: str = "none",
        create_idempotency_key: str | None = None,
        user_id: str | None = None,
        source_content_hash: str | None = None,
        display_name: str | None = None,
        expires_at: str | None = None,
    ) -> JobRecord:
        normalized_source_type = str(source_type).strip()
        normalized_source_ref = str(source_ref).strip()
        # display_name is caller-provided (gateway orchestrator). We only
        # normalise whitespace + cap length — never invent one here. NULL
        # is a valid state (legacy CLI submit, anonymous path); the
        # frontend has its own fallback chain.
        normalized_display_name: str | None = None
        if display_name is not None:
            stripped = str(display_name).strip()
            if stripped:
                # Matches the DB VARCHAR(60) limit from migration 015.
                normalized_display_name = stripped[:60]
        normalized_expires_at: str | None = None
        if expires_at is not None:
            stripped_expires_at = str(expires_at).strip()
            if stripped_expires_at:
                normalized_expires_at = stripped_expires_at
        normalized_output_target = str(output_target).strip().lower()
        normalized_job_type = str(job_type).strip()
        normalized_speakers = str(speakers).strip().lower() or "auto"
        normalized_voice_a = str(voice_a or "").strip() or None
        normalized_voice_b = str(voice_b or "").strip() or None
        normalized_transcription_method = str(transcription_method or "assemblyai").strip().lower()

        if normalized_job_type != JOB_TYPE_LOCALIZE_VIDEO:
            raise UnsupportedJobRequestError(f"unsupported job_type: {normalized_job_type}")
        if normalized_source_type not in SUPPORTED_SOURCE_TYPES:
            raise UnsupportedJobRequestError(f"unsupported source_type: {normalized_source_type}")
        if not normalized_source_ref:
            raise UnsupportedJobRequestError("source.value is required")
        if normalized_output_target != OUTPUT_TARGET_EDITOR:
            raise UnsupportedJobRequestError(f"unsupported output_target: {normalized_output_target}")
        if normalized_speakers not in {"auto", "1", "2"}:
            raise UnsupportedJobRequestError(f"unsupported speakers: {normalized_speakers}")

        # Concurrency control is enforced at gateway layer (per-user plan limits).
        # Job API no longer rejects new submissions due to globally active jobs,
        # but still reaps stale jobs (no live worker process) on submit.
        self._reap_stale_jobs()

        timestamp = utc_now_iso()
        job_id = f"job_{uuid4().hex}"
        workspace_dir = build_workspace_dir(user_id, job_id) if user_id else None
        # 2026-04-20: pre-fill project_dir as the absolute resolution of
        # workspace_dir. This closes the long-standing "stdout regex
        # bootstrap" attack surface (yt-dlp's `55.88KiB/s` progress line
        # matched as `/s`, poisoning JobRecord.project_dir permanently
        # via the write-once identity guard). With project_dir filled
        # from day one, `_parse_project_dir_from_line` short-circuits
        # entirely for modern gateway-originated traffic.
        #
        # user_id=None → legacy CLI direct submit; keep project_dir None
        # and fall through to the existing stdout capture path (pipeline
        # derives its own slug from video_title).
        project_dir_absolute: str | None = None
        if workspace_dir:
            project_dir_absolute = str(
                (self.runner.project_root / workspace_dir).resolve(strict=False)
            )
        record = JobRecord(
            job_id=job_id,
            job_type=normalized_job_type,
            source_type=normalized_source_type,
            source_ref=normalized_source_ref,
            output_target=normalized_output_target,
            speakers=normalized_speakers,
            voice_a=normalized_voice_a,
            voice_b=normalized_voice_b,
            status=JOB_STATUS_QUEUED,
            current_stage=None,
            progress_message="Job queued.",
            created_at=timestamp,
            updated_at=timestamp,
            transcription_method=normalized_transcription_method,
            service_mode=service_mode,
            tts_provider=tts_provider,
            tts_model=tts_model,
            requires_review=requires_review,
            voice_clone_enabled=voice_clone_enabled,
            voice_strategy=voice_strategy,
            plan_code_snapshot=plan_code_snapshot,
            role_snapshot=role_snapshot,
            source_duration_seconds=source_duration_seconds,
            estimated_duration_seconds=estimated_duration_seconds,
            quota_cost=quota_cost,
            quota_state=quota_state or "none",
            create_idempotency_key=create_idempotency_key,
            user_id=user_id,
            workspace_dir=workspace_dir,
            project_dir=project_dir_absolute,
            source_content_hash=source_content_hash,
            display_name=normalized_display_name,
            expires_at=normalized_expires_at,
        )
        self.store.save_job(record)
        self.store.append_event(
            record.job_id,
            JobEvent(
                job_id=record.job_id,
                event_type=EVENT_TYPE_STATUS,
                created_at=timestamp,
                status=record.status,
                message=record.progress_message,
            ),
        )
        self.runner.start(record)
        return self.require_job(record.job_id)

    def update_display_name(
        self, job_id: str, display_name: str | None
    ) -> JobRecord:
        """Persist a rename on an existing job. Plan §6.5 (D16).

        - ``None`` or whitespace-only input clears the field (display_name
          becomes ``None``); the frontend then falls back through
          ``getJobDisplayTitle`` → slug → videoId → "未命名视频".
        - Non-empty input is stripped + truncated to 60 chars (DB column
          limit, migration 015).
        - Collision resolution with the user's existing names is the
          gateway's responsibility (it owns the SQL); this method is a
          pure write of a pre-validated value.

        Raises :class:`KeyError` (via ``require_job``) if the job id is
        unknown.
        """
        record = self.store.require_job(job_id)
        normalized: str | None = None
        if display_name is not None:
            stripped = str(display_name).strip()
            if stripped:
                normalized = stripped[:60]
        next_record = replace(
            record,
            display_name=normalized,
            updated_at=utc_now_iso(),
        )
        self.store.save_job(next_record)
        return self.store.require_job(job_id)

    def continue_job(self, job_id: str) -> JobRecord:
        record = self.require_job(job_id)
        if record.status != JOB_STATUS_WAITING_FOR_REVIEW:
            raise JobConflictError(f"job {job_id} is not waiting_for_review")
        if record.project_dir is None:
            raise JobConflictError(f"job {job_id} has no project_dir to continue")
        review_gate = dict(record.review_gate or {})
        review_stage = str(review_gate.get("stage") or "").strip()
        if not review_stage:
            raise JobConflictError(f"job {job_id} has no review gate summary")
        if not is_review_stage_approved(record.project_dir, review_stage):
            raise JobConflictError(
                f"review stage {review_stage} is not approved for job {job_id}"
            )

        # Concurrency control is enforced at gateway layer.

        self.runner.start(record, continue_existing=True)
        return self.require_job(job_id)

    def cancel_and_delete_job(self, job_id: str) -> bool:
        """Stop any running process and delete the job record. Returns True if the job existed."""
        self.runner.stop_process(job_id)
        return self.store.delete_job(job_id)

    # ------------------------------------------------------------------
    # Post-edit transitions (plan 2026-04-18, T1-1 skeleton).
    # Thin delegates to services.jobs.editing — all state machine checks,
    # filesystem side effects, and event emission live in that module.
    # ------------------------------------------------------------------

    def enter_editing(self, job_id: str) -> JobRecord:
        """``succeeded → editing``. Creates editor/editing/ + baseline snapshot."""
        from services.jobs.editing import enter_editing as _enter_editing

        record = self.require_job(job_id)
        return _enter_editing(record, self.store)

    def cancel_editing(
        self,
        job_id: str,
        *,
        reason: str = "user_cancel",
    ) -> JobRecord:
        """``editing → succeeded``. Drops editor/editing/ and clears touched_at."""
        from services.jobs.editing import cancel_editing as _cancel_editing

        record = self.require_job(job_id)
        return _cancel_editing(record, self.store, reason=reason)

    def commit_editing(
        self,
        job_id: str,
        *,
        strategy: str,
        copy_display_name: str | None = None,
    ) -> dict:
        """Run the commit pipeline (T1-9). Returns response dict (not a
        JobRecord because copy_as_new produces a sibling + the source is
        also changed; the dict is the HTTP response shape).

        Pre-T1-9 versions of this method raised NotImplementedError as a
        skeleton; now delegates to ``editing_commit.commit_editing_pipeline``.
        """
        from services.jobs.editing_commit import commit_editing_pipeline

        record = self.require_job(job_id)
        return commit_editing_pipeline(
            record, self.store, self.runner,
            strategy=strategy,
            copy_display_name=copy_display_name,
        )

    # ------------------------------------------------------------------
    # Editing segments CRUD (T1-2). All mutations refresh editing_touched_at
    # so the idle scanner sees activity.
    # ------------------------------------------------------------------

    def _require_editing(self, job_id: str) -> JobRecord:
        from services.jobs.editing import EditingConflictError
        from services.jobs.models import JOB_STATUS_EDITING

        record = self.require_job(job_id)
        if record.status != JOB_STATUS_EDITING:
            raise EditingConflictError(
                f"job {job_id} is not in editing state (current: {record.status})"
            )
        if not record.project_dir:
            raise EditingConflictError(f"job {job_id} has no project_dir")
        return record

    def get_editing_segments(self, job_id: str) -> dict:
        """Read-only fetch of the editing buffer. Does NOT refresh
        editing_touched_at — the scanner-facing rule is "mutations count as
        activity, reads do not". See plan §5.4.1."""
        from services.jobs.editing_segments import editing_payload

        record = self._require_editing(job_id)
        payload = editing_payload(record.project_dir)
        return {
            **payload,
            "editing_touched_at": record.editing_touched_at,
            "edit_generation": record.edit_generation,
        }

    def patch_editing_segment(
        self,
        job_id: str,
        segment_id: str,
        patch: dict,
    ) -> dict:
        """Mutate one segment in editing/segments.json and refresh touched_at.
        Returns a payload with the updated segment + full status map."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import (
            load_segment_status,
            patch_editing_segment as _patch_editing_segment,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        updated_segment = _patch_editing_segment(
            record.project_dir, segment_id, patch
        )
        _touch_editing(record, self.store)
        return {
            "segment": updated_segment,
            "segment_status": load_segment_status(record.project_dir),
        }

    def split_editing_segment(
        self,
        job_id: str,
        segment_id: str,
        *,
        split_source_index: int,
        split_cn_index: int,
        speaker_a: str,
        speaker_b: str,
    ) -> dict:
        """Split one segment into two at user-chosen character positions.

        Mirrors the translation-review split UX from the main flow but
        operates on the editing buffer; also refreshes ``editing_touched_at``
        so the idle scanner sees activity."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import (
            load_segment_status,
            split_editing_segment as _split_editing_segment,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = _split_editing_segment(
            record.project_dir,
            segment_id=segment_id,
            split_source_index=int(split_source_index),
            split_cn_index=int(split_cn_index),
            speaker_a=speaker_a,
            speaker_b=speaker_b,
        )
        _touch_editing(record, self.store)
        # Enrich with the post-split status map so the frontend can patch
        # its cached state in one shot rather than re-fetching.
        return {
            **result,
            "segment_status": load_segment_status(record.project_dir),
        }

    def preview_editing_segment_source_audio(
        self,
        job_id: str,
        segment_id: str,
    ) -> dict:
        """Return a base64 WAV slice of the source audio for one editing
        segment. Read-only; does NOT refresh ``editing_touched_at`` —
        previewing isn't a mutation and we don't want to extend the
        24-hour idle clock just because the user scrubbed around.

        Kept for legacy callers / unit tests. Production HTTP path uses
        :meth:`prepare_preview_source_cache` + the GET stream endpoint
        because 1 MB+ JSON bodies trigger ``RemoteProtocolError`` on
        the gateway's Uvicorn + httpx proxy under concurrency."""
        from services.jobs.editing_segments import (
            slice_source_audio_for_editing_segment,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        return slice_source_audio_for_editing_segment(
            record.project_dir, segment_id
        )

    def prepare_preview_source_cache(
        self,
        job_id: str,
        segment_id: str,
    ) -> dict:
        """Slice + cache the source audio WAV and return the meta only
        (not the bytes). Frontend then fetches the WAV via the GET
        stream endpoint, which serves ``<audio>``-friendly Range-aware
        responses without the 1 MB JSON-body pathology.

        Returns ``{"duration_ms", "start_ms", "end_ms", "mime_type",
        "size_bytes", "segment_id"}`` — all small primitives so the
        POST response body stays under ~150 bytes."""
        from services.jobs.editing_segments import cache_preview_source_wav
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        path, meta = cache_preview_source_wav(record.project_dir, segment_id)
        return {
            **meta,
            "segment_id": segment_id,
            "size_bytes": path.stat().st_size,
        }

    def mark_editing_segment_status(
        self,
        job_id: str,
        segment_id: str,
        status: str,
    ) -> dict:
        """Explicit segment_status update (e.g. frontend calls this after
        the user "accepts" or "discards" a draft TTS). Also refreshes
        editing_touched_at."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import mark_segment_status
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        status_map = mark_segment_status(record.project_dir, segment_id, status)
        _touch_editing(record, self.store)
        return {"segment_status": status_map}

    def regenerate_segment_tts(
        self,
        job_id: str,
        segment_id: str,
        *,
        tts_caller=None,
    ) -> dict:
        """Kick off single-segment TTS re-synthesis (T1-5).

        ``tts_caller`` explicit arg wins; otherwise fall back to the caller
        injected at service construction (``self._segment_tts_caller``),
        then to ``None`` — which makes editing_tts resolve to the "not
        wired" 501 placeholder. main.run_job_api_command installs the
        production caller via ``services.tts.segment_regenerate``.
        Also refreshes editing_touched_at on success.
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import regenerate_segment_tts as _regenerate
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        caller = tts_caller or getattr(self, "_segment_tts_caller", None)
        result = _regenerate(
            record.project_dir, segment_id, tts_caller=caller
        )
        _touch_editing(record, self.store)
        return result

    def accept_segment_draft_tts(self, job_id: str, segment_id: str) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import accept_draft_tts
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = accept_draft_tts(record.project_dir, segment_id)
        _touch_editing(record, self.store)
        return result

    def discard_segment_draft_tts(self, job_id: str, segment_id: str) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import discard_draft_tts
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = discard_draft_tts(record.project_dir, segment_id)
        _touch_editing(record, self.store)
        return result

    # ------------------------------------------------------------------
    # Batch re-TTS + voice_map (T1-6)
    # ------------------------------------------------------------------

    def regenerate_all_dirty_segments(
        self,
        job_id: str,
        *,
        tts_caller=None,
    ) -> dict:
        """Synchronous batch regenerate. Per D38 response shape.

        Same DI story as regenerate_segment_tts — explicit arg wins, then
        the injected per-service caller, then None (501 placeholder).

        Retained for tests and admin tooling. The HTTP POST endpoint
        uses ``regenerate_all_dirty_segments_async`` instead — gateway
        times out on 100+ segment batches (D39).
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_batch import regenerate_all_dirty_segments as _batch

        record = self._require_editing(job_id)
        caller = tts_caller or getattr(self, "_segment_tts_caller", None)
        result = _batch(record.project_dir, tts_caller=caller)
        _touch_editing(record, self.store)
        return result

    def regenerate_all_dirty_segments_async(
        self,
        job_id: str,
        *,
        tts_caller=None,
    ) -> dict:
        """Async batch regenerate (D39). Spawns a daemon thread, returns
        immediately with a ``task_id``. The thread writes progress to
        ``{project_dir}/editor/editing/regen_status.json``; poll via
        ``get_regenerate_all_status(job_id, task_id)``.

        Still refreshes ``editing_touched_at`` synchronously before
        returning so the idle scanner sees activity even if the thread
        hasn't emitted any per-segment events yet.
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.regenerate_all_async import start_regen_all_async

        record = self._require_editing(job_id)
        caller = tts_caller or getattr(self, "_segment_tts_caller", None)
        task_id = start_regen_all_async(
            project_dir=record.project_dir, tts_caller=caller,
        )
        _touch_editing(record, self.store)
        return {"task_id": task_id, "status": "running"}

    def get_regenerate_all_status(
        self,
        job_id: str,
        task_id: str,
    ) -> dict | None:
        """Read the async batch re-TTS status file for ``task_id``.

        Returns the status snapshot dict, or ``None`` if no batch has
        ever started for this project. Unlike the mutation endpoints,
        this does NOT require editing state (the user may poll after
        commit finishes for terminal state recovery) and does NOT
        refresh editing_touched_at."""
        from services.jobs.regenerate_all_async import read_regen_all_status

        record = self.require_job(job_id)
        if not record.project_dir:
            return None
        return read_regen_all_status(record.project_dir, task_id)

    def request_regenerate_all_cancel(
        self,
        job_id: str,
        task_id: str,
    ) -> dict:
        """Signal the running batch re-TTS thread to stop between segments
        (plan §7.10 / D39). Returns ``{"cancelled": bool}`` — True means
        the flag was written and the worker will land on
        ``stage='cancelled'`` on its next per-segment tick; False means
        no matching live batch was found (wrong task_id, already done,
        or cleaned up).

        Safe to call repeatedly — the helper is idempotent. Does NOT
        refresh editing_touched_at (cancelling isn't a mutation the
        idle scanner should care about)."""
        from services.jobs.regenerate_all_async import request_regen_all_cancel

        record = self._require_editing(job_id)
        wrote = request_regen_all_cancel(record.project_dir, task_id)
        return {"cancelled": wrote}

    def get_editing_voice_map(self, job_id: str) -> dict:
        from services.jobs.editing_voice_map import load_voice_map

        record = self._require_editing(job_id)
        return {"voice_map": load_voice_map(record.project_dir)}

    def set_editing_voice_override(
        self,
        job_id: str,
        segment_id: str,
        *,
        provider: str,
        voice_id: str,
    ) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_voice_map import set_voice_override
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = set_voice_override(
            record.project_dir, segment_id,
            provider=provider, voice_id=voice_id,
        )
        _touch_editing(record, self.store)
        return result

    def clear_editing_voice_override(self, job_id: str, segment_id: str) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_voice_map import clear_voice_override
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = clear_voice_override(record.project_dir, segment_id)
        _touch_editing(record, self.store)
        return result

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.store.load_job(job_id)

    def require_job(self, job_id: str) -> JobRecord:
        try:
            return self.store.require_job(job_id)
        except KeyError as exc:
            raise JobNotFoundError(str(exc)) from exc

    def list_jobs(self, *, limit: int | None = 20) -> list[JobRecord]:
        return self.store.list_jobs(limit=limit)

    def read_logs(self, job_id: str) -> list[JobEvent]:
        self.require_job(job_id)
        return self.store.load_events(job_id)

    def get_result_summary(self, job_id: str) -> dict[str, object]:
        return build_job_result_summary(self.require_job(job_id))

    def get_artifacts(self, job_id: str) -> dict[str, object]:
        return build_job_artifacts_payload(self.require_job(job_id))

    def _reap_stale_jobs(self) -> None:
        """Mark stale queued/running jobs (no live worker) as failed.

        Uses WORKER_ACTIVE_STATUSES (not ACTIVE_JOB_STATUSES) so that
        waiting_for_review / editing jobs — which legitimately have no worker —
        are not mis-flagged as failed. See docs/internal/status-touchpoints-2026-04-18.md.
        """
        for record in self.store.list_jobs(limit=None):
            if record.status in WORKER_ACTIVE_STATUSES:
                if self._is_stale_process_backed_active_job(record):
                    self._mark_stale_active_job_failed(record)

    def _find_active_job(self, *, exclude_job_id: str | None = None) -> JobRecord | None:
        for record in self.store.list_jobs(limit=None):
            if exclude_job_id is not None and record.job_id == exclude_job_id:
                continue
            if record.status in ACTIVE_JOB_STATUSES:
                if self._is_stale_process_backed_active_job(record):
                    self._mark_stale_active_job_failed(record)
                    continue
                return record
        return None

    def _is_stale_process_backed_active_job(self, record: JobRecord) -> bool:
        # Only QUEUED/RUNNING (= WORKER_ACTIVE_STATUSES) require a live worker
        # process. waiting_for_review / editing legitimately have no worker and
        # must never be treated as stale by this helper. See
        # docs/internal/status-touchpoints-2026-04-18.md §0.
        if record.status not in WORKER_ACTIVE_STATUSES:
            return False
        return not self.runner.is_process_active(record.job_id)

    def _mark_stale_active_job_failed(self, record: JobRecord) -> JobRecord:
        timestamp = utc_now_iso()
        error_message = "Recovered stale active job without a live worker process."
        next_record = replace(
            record,
            status=JOB_STATUS_FAILED,
            current_stage=STAGE_FAILED,
            progress_message=error_message,
            updated_at=timestamp,
            completed_at=timestamp,
            error_summary={
                "stage": STAGE_FAILED,
                "error_type": "stale_active_job",
                "message": error_message,
            },
            review_gate=None,
        )
        self.store.save_job(next_record)
        self.store.append_event(
            record.job_id,
            JobEvent(
                job_id=record.job_id,
                event_type=EVENT_TYPE_STATUS,
                created_at=timestamp,
                stage=next_record.current_stage,
                status=next_record.status,
                level=EVENT_LEVEL_ERROR,
                message=next_record.progress_message,
            ),
        )
        return next_record


def build_default_job_service(
    *,
    project_root: Path,
    jobs_root: Path | None = None,
    python_executable: str | None = None,
) -> JobService:
    resolved_jobs_root = (jobs_root or (project_root / "jobs")).resolve(strict=False)
    store = JobStore(resolved_jobs_root)
    runner = ProcessJobRunner(
        store=store,
        project_root=project_root,
        python_executable=python_executable,
    )
    return JobService(store=store, runner=runner)

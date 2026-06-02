from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from services.job_paths import build_workspace_dir
from services.jobs.events import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_WARN,
    EVENT_TYPE_LOG,
    EVENT_TYPE_STATUS,
    JobEvent,
)
from services.jobs.models import (
    ACTIVE_JOB_STATUSES,
    WORKER_ACTIVE_STATUSES,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_WAITING_FOR_REVIEW,
    JOB_TYPE_LOCALIZE_VIDEO,
    OUTPUT_TARGET_EDITOR,
    SOURCE_TYPE_YOUTUBE_URL,
    STAGE_ALIGNMENT,
    SUPPORTED_SOURCE_TYPES,
    STAGE_FAILED,
    JobRecord,
)
from services.jobs.process_runner import ProcessJobRunner, is_review_stage_approved
from services.jobs.read_surface import build_job_artifacts_payload, build_job_result_summary
from services.jobs.store import JobStore
from services.state_manager import utc_now_iso


SUPPORTED_MINIMAX_TTS_MODELS = {"speech-2.8-turbo", "speech-2.8-hd"}


class JobServiceError(Exception):
    """Base error for the A1 job service."""


class JobNotFoundError(JobServiceError):
    """Raised when a job_id does not exist."""


class JobConflictError(JobServiceError):
    """Raised when a lifecycle action conflicts with the current job state."""


class UnsupportedJobRequestError(JobServiceError):
    """Raised when a request is outside the A1 public contract."""


_DEFAULT_AUDIT_OBSERVER = object()


def _accepted_overwrite_commit_response(record: JobRecord) -> dict | None:
    """Treat a duplicate overwrite submit as success after commit left editing."""
    if record.status not in {JOB_STATUS_RUNNING, JOB_STATUS_SUCCEEDED}:
        return None
    if int(record.edit_generation or 0) <= 0:
        return None
    if record.editing_touched_at is not None:
        return None
    if not record.project_dir:
        return None
    editing_dir = Path(record.project_dir) / "editor" / "editing"
    if editing_dir.exists():
        return None
    return {
        "strategy": "overwrite",
        "job_id": record.job_id,
        "edit_generation": record.edit_generation,
        "current_stage": record.current_stage or STAGE_ALIGNMENT,
        "already_started": True,
        "already_completed": record.status == JOB_STATUS_SUCCEEDED,
    }


class JobService:
    def __init__(
        self,
        *,
        store: JobStore,
        runner: ProcessJobRunner,
        audit_observer: object | None = _DEFAULT_AUDIT_OBSERVER,
    ) -> None:
        self.store = store
        self.runner = runner
        # User-edit audit observer (plan 2026-05-04 §12 P0). Default to a
        # JsonlAuditObserver so production gets audit out of the box;
        # tests can inject fakes, or explicitly pass None to disable. Not typed as
        # AuditObserver here to keep the import lazy — JobService is
        # imported very early in the boot path and we don't want to drag
        # in user_edit_audit's transitive imports until first use.
        if audit_observer is _DEFAULT_AUDIT_OBSERVER:
            from services.jobs.user_edit_audit import JsonlAuditObserver
            audit_observer = JsonlAuditObserver()
        self._audit_observer = audit_observer

    # ------------------------------------------------------------------
    # User-edit audit helpers (plan 2026-05-04 §12 P0)
    #
    # Service layer wraps every observer call in safe_observe so observer
    # implementations stay simple and any audit failure stays out of the
    # user-facing main path. Callers in this module + review_actions
    # should always go through these helpers, never call
    # self._audit_observer.observe directly.
    # ------------------------------------------------------------------

    def _emit_user_edit_event(self, project_dir: object, event: dict) -> None:
        """Service-layer chokepoint for safe audit emission."""
        from services.jobs.user_edit_audit import safe_observe

        if project_dir is None:
            return
        # Build a tiny job-event emitter that lets safe_observe surface
        # audit-write failures into the JobEvent stream (deduplicated
        # by safe_observe internally).
        job_id = str(event.get("job_id") or "").strip()

        def _emit_audit_failure_jobevent(message: str, payload: dict) -> None:
            if not job_id:
                return
            try:
                self.store.append_event(
                    job_id,
                    JobEvent(
                        job_id=job_id,
                        event_type=EVENT_TYPE_LOG,
                        created_at=utc_now_iso(),
                        stage="user_edit_audit",
                        level=EVENT_LEVEL_WARN,
                        message=message,
                        payload=payload,
                    ),
                )
            except Exception:
                # Final layer of best-effort — do not raise.
                pass

        safe_observe(
            self._audit_observer,
            project_dir=project_dir,
            event=event,
            job_event_emitter=_emit_audit_failure_jobevent,
        )

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
        source_video_title: str | None = None,
        source_published_at: str | None = None,
        source_content_summary: str | None = None,
        source_content_era: str | None = None,
        source_content_tags: object | None = None,
        display_name: str | None = None,
        expires_at: str | None = None,
        smart_consent: dict | None = None,
        express_consent: dict | None = None,
        express_consent_parse_error: str | None = None,
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
            source_video_title=source_video_title,
            source_published_at=source_published_at,
            source_content_summary=source_content_summary,
            source_content_era=source_content_era,
            source_content_tags=source_content_tags,
            display_name=normalized_display_name,
            expires_at=normalized_expires_at,
            # PR#3C-b3g: smart_consent passthrough — JobRecord persists,
            # pipeline reads via _snap("smart_consent") to gate
            # auto-clone / auto-translation-review paths. None for
            # express/studio jobs (Gateway only forwards when service_mode==smart).
            smart_consent=smart_consent,
            # Phase 4.3a Express auto-clone canary (spec §3.2). JobRecord
            # persists; pipeline (Phase 4.3a F stage) reads to gate the
            # CosyVoice auto-clone. None for studio/smart jobs (Gateway
            # only forwards when service_mode==express).
            express_consent=express_consent,
            express_consent_parse_error=express_consent_parse_error,
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

        Raises :class:`KeyError` (via ``update_job``) if the job id is
        unknown.

        P1-15b caller migration (audit 2026-05-07): routed through
        ``update_job`` so the rename atomically merges with concurrent
        ProcessJobRunner stage transitions instead of overwriting them
        from a stale snapshot.
        """
        normalized: str | None = None
        if display_name is not None:
            stripped = str(display_name).strip()
            if stripped:
                normalized = stripped[:60]
        return self.store.update_job(
            job_id,
            lambda current: replace(
                current,
                display_name=normalized,
                updated_at=utc_now_iso(),
            ),
        )

    def update_tts_model_from_voice_selection(
        self, job_id: str, tts_model: str | None
    ) -> JobRecord:
        """Persist the MiniMax model selected in voice_selection_review.

        P1-15b caller migration (audit 2026-05-07): routed through
        ``update_job`` to serialize with concurrent ProcessJobRunner
        writes. The "no-op when same model" optimization moves into the
        mutator so the freshness check happens against the locked
        snapshot, not a pre-lock require_job that may have been stale.
        """
        normalized_model = str(tts_model or "").strip()
        if not normalized_model:
            return self.store.require_job(job_id)
        if normalized_model not in SUPPORTED_MINIMAX_TTS_MODELS:
            raise ValueError(f"Unsupported MiniMax TTS model: {normalized_model}")

        def mutator(current: JobRecord) -> JobRecord:
            if current.tts_model == normalized_model:
                # No-op: same model already set. Return current to skip
                # an unnecessary write while still holding the lock.
                return current
            return replace(
                current,
                tts_model=normalized_model,
                updated_at=utc_now_iso(),
            )

        return self.store.update_job(job_id, mutator)

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
        """``succeeded → editing``. Creates editor/editing/ + baseline snapshot.

        After the FS baseline is ready, emit a ``editing_session_started``
        user-edit audit marker (plan 2026-05-04 §7.3) so subsequent
        before/after diff events have an anchor. The marker is NOT a user
        correction — analysis tools must not count it as one.
        """
        from services.jobs.editing import enter_editing as _enter_editing

        record = self.require_job(job_id)
        was_legacy_lazy_backfill = self._editor_tts_segments_was_empty(record)
        result = _enter_editing(record, self.store)

        # Compute baseline metrics (best-effort; audit failure must not
        # propagate). All values default to None — offline analysis treats
        # missing as "could not compute" rather than "zero".
        try:
            self._emit_editing_session_started(result, legacy_lazy_backfill=was_legacy_lazy_backfill)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "editing_session_started audit emit failed for %s", job_id
            )
        return result

    def _editor_tts_segments_was_empty(self, record: JobRecord) -> bool:
        """Heuristic for ``legacy_lazy_backfill`` flag on editing_session_started.

        Returns True if ``editor/tts_segments`` is missing or empty BEFORE
        enter_editing runs — that's the signature of a legacy task that
        will trigger ensure_editor_tts_segments_baseline's lazy copy. We
        check before so the audit marker can faithfully report whether
        the user's session started with a fresh-built editor or with
        baseline files that materialized during enter_editing.
        """
        if not record.project_dir:
            return False
        from pathlib import Path as _Path
        d = _Path(record.project_dir) / "editor" / "tts_segments"
        if not d.exists():
            return True
        try:
            for entry in d.iterdir():
                if entry.is_file() and entry.suffix.lower() == ".wav":
                    return False
        except OSError:
            return False
        return True

    def _emit_editing_session_started(
        self,
        record: JobRecord,
        *,
        legacy_lazy_backfill: bool,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_editing_session_started_event,
            manifest_audio_fingerprint,
        )

        if not record.project_dir:
            return
        project_dir = _Path(record.project_dir)
        ctx = AuditContext.from_job_record(record)

        segment_count, speaker_count, speaker_distribution = self._summarize_editing_baseline(project_dir)
        tts_dir = project_dir / "editor" / "tts_segments"
        baseline_audio_fp = manifest_audio_fingerprint(tts_dir)
        baseline_audio_present = tts_dir.exists() and baseline_audio_fp is not None

        event = build_editing_session_started_event(
            ctx,
            segment_count=segment_count,
            speaker_count=speaker_count,
            speaker_distribution=speaker_distribution,
            baseline_audio_fingerprint=baseline_audio_fp,
            baseline_audio_present=baseline_audio_present,
            legacy_lazy_backfill=legacy_lazy_backfill,
            edit_generation=record.edit_generation,
        )
        self._emit_user_edit_event(project_dir, event)

    @staticmethod
    def _summarize_editing_baseline(project_dir):
        """Read editor/segments.json and return (segment_count, speaker_count,
        per-speaker distribution dict). All-None on read failure."""
        from pathlib import Path as _Path
        import json as _json

        seg_path = _Path(project_dir) / "editor" / "segments.json"
        if not seg_path.exists():
            return None, None, {}
        try:
            payload = _json.loads(seg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None, {}
        segments = payload if isinstance(payload, list) else payload.get("segments", [])
        if not isinstance(segments, list):
            return None, None, {}

        per_speaker: dict[str, dict[str, int]] = {}
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            sid = str(seg.get("speaker_id") or "").strip() or "unknown"
            entry = per_speaker.setdefault(sid, {"segments": 0, "duration_ms": 0})
            entry["segments"] += 1
            try:
                start_ms = int(seg.get("start_ms") or 0)
                end_ms = int(seg.get("end_ms") or 0)
                if end_ms > start_ms:
                    entry["duration_ms"] += end_ms - start_ms
            except (TypeError, ValueError):
                pass

        return len(segments), len(per_speaker), per_speaker

    def cancel_editing(
        self,
        job_id: str,
        *,
        reason: str = "user_cancel",
    ) -> JobRecord:
        """``editing → succeeded``. Drops editor/editing/ and clears touched_at.

        Emits a ``post_edit_cancelled`` user-edit audit event after the FS
        teardown so analysis can flag UX friction (e.g. user did a lot of
        edits then bailed out). Plan 2026-05-04 §7.3.
        """
        from services.jobs.editing import cancel_editing_atomic as _cancel_editing_atomic

        record = self.require_job(job_id)
        # P1-15b batch 2 follow-up² (Codex review of c170cff): use the
        # atomic variant that exposes the lock-internal transition_happened
        # flag instead of inferring it from a record/result diff. The
        # diff-based predicate failed in the stale-snapshot race —
        # caller's record could legitimately have editing/touched while
        # result legitimately had succeeded/None even when the mutator
        # no-op'd because a concurrent winner already wrote that state.
        # Exposing the flag is the only way to distinguish "this call
        # transitioned" from "this call observed someone else's transition".
        result, transition_happened = _cancel_editing_atomic(
            record, self.store, reason=reason,
        )

        if transition_happened:
            try:
                self._emit_post_edit_cancelled(record, result, reason=reason)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception(
                    "post_edit_cancelled audit emit failed for %s", job_id
                )
        return result

    def _emit_post_edit_cancelled(
        self,
        record_before: JobRecord,
        record_after: JobRecord,
        *,
        reason: str,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_cancelled_event,
        )

        if not record_after.project_dir:
            return
        ctx = AuditContext.from_job_record(record_after)

        # There is no durable editing_session_started timestamp in JobRecord.
        # editing_touched_at is refreshed on every mutation, so using it here
        # would record "time since last edit" while calling it session duration.
        # P1 can compute true session duration from the append-only audit stream.
        event = build_post_edit_cancelled_event(
            ctx,
            cancel_reason=reason,
            session_duration_seconds=None,
            edit_counts={},  # P0: empty; P1 dataset builder reconstructs from prior events
        )
        self._emit_user_edit_event(_Path(record_after.project_dir), event)

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

        After commit succeeds, emit a ``post_edit_committed`` audit event
        (plan §7.3) and an effective_marker so offline analysis can flip
        the prior session's edit events from ``effective=False`` to true
        (without rewriting them — append-only is the contract).
        """
        from services.jobs.editing_commit import commit_editing_pipeline

        record = self.require_job(job_id)
        if strategy == "overwrite":
            accepted = _accepted_overwrite_commit_response(record)
            if accepted is not None:
                return accepted

        result = commit_editing_pipeline(
            record, self.store, self.runner,
            strategy=strategy,
            copy_display_name=copy_display_name,
        )

        try:
            self._emit_post_edit_committed(record, result, strategy=strategy)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_committed audit emit failed for %s", job_id
            )
        return result

    def revert_unsynced_text_segments(
        self,
        job_id: str,
        *,
        segment_ids: list[str],
    ) -> dict:
        from services.jobs.editing import EditingConflictError, touch_editing
        from services.jobs.editing_segments import revert_text_changes_to_audio_baseline

        record = self.require_job(job_id)
        if record.status != "editing":
            raise EditingConflictError(
                f"job {job_id} is not in editing state (current status: {record.status})"
            )
        if not record.project_dir:
            raise EditingConflictError(f"job {job_id} has no project_dir")
        result = revert_text_changes_to_audio_baseline(
            record.project_dir,
            segment_ids,
        )
        updated = touch_editing(record, self.store)
        result["editing_touched_at"] = updated.editing_touched_at
        return result

    def _emit_post_edit_committed(
        self,
        record_pre_commit: JobRecord,
        result: dict,
        *,
        strategy: str,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AUDIT_DIR_NAME,
            AUDIT_EVENTS_FILENAME,
            AuditContext,
            EFFECTIVE_REASON_COMMITTED,
            STAGE_POST_EDIT,
            build_effective_marker_event,
            build_post_edit_committed_event,
            compute_post_edit_marked_event_ids,
        )

        # commit_as_new can return a copy_target_job_id; overwrite stays on
        # the same project_dir. Audit attaches to the SOURCE project_dir
        # because the user's editing session lives there.
        if not record_pre_commit.project_dir:
            return
        ctx = AuditContext.from_job_record(record_pre_commit)
        result = result if isinstance(result, dict) else {}
        target_job_id = (
            result.get("copy_target_job_id")
            or result.get("target_job_id")
            or result.get("new_job_id")
        )
        new_project_dir = result.get("new_project_dir")

        committed = build_post_edit_committed_event(
            ctx,
            strategy=str(strategy),
            edit_counts={},  # P0: empty; P1 reconstructs from prior session events
            target_job_id=str(target_job_id) if target_job_id else None,
        )
        project_dir = _Path(record_pre_commit.project_dir)
        self._emit_user_edit_event(project_dir, committed)

        # Effective marker — turn the prior session's intent events from
        # ``effective=False`` into "effective" via append-only join.
        # ``marked_event_ids`` lists the intent events whose changes
        # survived to the post-commit final segments.json. The marker is
        # a separate event; the original intent lines are never rewritten.
        #
        # final segments live at ``editor/segments.json``:
        # - overwrite: same project_dir as the source (editing/ promoted in place).
        # - copy_as_new: result["new_project_dir"] (the target — source
        #   editor/segments.json stays on the previous baseline).
        # Audit JSONL stays on the SOURCE project_dir for both strategies
        # (per copy_as_new policy: target starts with a fresh audit slate).
        if strategy == "copy_as_new" and new_project_dir:
            final_segments_path = _Path(new_project_dir) / "editor" / "segments.json"
        else:
            final_segments_path = project_dir / "editor" / "segments.json"
        audit_path = project_dir / AUDIT_DIR_NAME / AUDIT_EVENTS_FILENAME

        marked_event_ids: list[str] = []
        try:
            marked_event_ids = compute_post_edit_marked_event_ids(
                audit_path=audit_path,
                final_segments_path=final_segments_path,
                edit_generation=record_pre_commit.edit_generation,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "compute_post_edit_marked_event_ids failed for %s; "
                "marker will be emitted with empty marked_event_ids",
                record_pre_commit.job_id,
            )

        marker = build_effective_marker_event(
            ctx,
            stage=STAGE_POST_EDIT,
            effective_reason=EFFECTIVE_REASON_COMMITTED,
            marked_event_ids=marked_event_ids,
            extra_context={
                "edit_generation": record_pre_commit.edit_generation,
                "strategy": str(strategy),
                "target_job_id": str(target_job_id) if target_job_id else None,
            },
        )
        self._emit_user_edit_event(project_dir, marker)

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
        Returns a payload with the updated segment + full status map.

        Emits ``post_edit_text_changed`` and/or ``post_edit_segment_speaker_changed``
        depending on which fields the patch actually mutated. Plan 2026-05-04 §7.3.
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import (
            load_segment_status,
            load_editing_segments_for_audit,
            patch_editing_segment as _patch_editing_segment,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        # Snapshot the pre-patch segment so audit before/after is faithful.
        before_segment = load_editing_segments_for_audit(
            record.project_dir, segment_id
        )
        updated_segment = _patch_editing_segment(
            record.project_dir, segment_id, patch
        )
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_segment_patch_audit(
                record, segment_id, before_segment, updated_segment, patch
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit segment-patch audit emit failed for %s", job_id
            )

        return {
            "segment": updated_segment,
            "segment_status": load_segment_status(record.project_dir),
        }

    def preview_bulk_replace_terms(
        self,
        job_id: str,
        *,
        find: str,
        replace: str,
        field: str = "cn_text",
    ) -> dict:
        """Preview a literal terminology replacement in editing/segments.json.

        Read-only, but still requires editing state so the preview represents
        the same buffer that apply/regenerate will mutate.
        """
        from services.jobs.editing_bulk_replace import preview_bulk_replace_terms

        record = self._require_editing(job_id)
        return preview_bulk_replace_terms(
            record.project_dir,
            find=find,
            replace=replace,
            field=field,
        )

    def apply_bulk_replace_terms(
        self,
        job_id: str,
        *,
        find: str,
        replace: str,
        field: str = "cn_text",
        expected_segment_ids: object = None,
        expected_total_matches: int | None = None,
    ) -> dict:
        """Apply a confirmed terminology replacement and mark affected TTS stale."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_bulk_replace import apply_bulk_replace_terms

        record = self._require_editing(job_id)
        result = apply_bulk_replace_terms(
            record.project_dir,
            find=find,
            replace=replace,
            field=field,
            expected_segment_ids=expected_segment_ids,
            expected_total_matches=expected_total_matches,
        )
        _touch_editing(record, self.store)

        try:
            before_by_id = {
                str(item.get("segment_id")): item
                for item in result.get("matches", [])
                if isinstance(item, dict)
            }
            after_by_id = {
                str(item.get("segment_id")): item
                for item in result.get("segments", [])
                if isinstance(item, dict)
            }
            for sid in result.get("replaced_segment_ids", []):
                sid = str(sid)
                before = before_by_id.get(sid) or {}
                after = after_by_id.get(sid) or {}
                self._emit_post_edit_segment_patch_audit(
                    record,
                    sid,
                    {"cn_text": before.get("before_text")},
                    after,
                    {"cn_text": after.get("cn_text", "")},
                )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "post_edit bulk-replace audit emit failed for %s", job_id
            )

        return result

    def _emit_post_edit_segment_patch_audit(
        self,
        record: JobRecord,
        segment_id: str,
        before_segment: dict | None,
        after_segment: dict,
        patch: dict,
    ) -> None:
        """Decompose a patch into per-event audit emissions.

        - cn_text / source_text → post_edit_text_changed
        - speaker_id → post_edit_segment_speaker_changed

        We rely on the patch dict to know what the user *intended* to change;
        before/after segment values verify the change actually landed.
        """
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_segment_speaker_changed_event,
            build_post_edit_text_changed_event,
            text_hash,
        )

        if not record.project_dir or not isinstance(patch, dict):
            return
        ctx = AuditContext.from_job_record(record)
        project_dir = _Path(record.project_dir)
        before_segment = before_segment or {}

        # Text changes (cn_text or source_text). Emit one event per field
        # actually touched so analysis can correlate independently.
        for field in ("cn_text", "source_text"):
            if field not in patch:
                continue
            before_text = before_segment.get(field)
            after_text = after_segment.get(field)
            if before_text == after_text:
                continue
            duration_ms = None
            try:
                start_ms = int(after_segment.get("start_ms") or 0)
                end_ms = int(after_segment.get("end_ms") or 0)
                if end_ms > start_ms:
                    duration_ms = end_ms - start_ms
            except (TypeError, ValueError):
                duration_ms = None
            event = build_post_edit_text_changed_event(
                ctx,
                segment_id=segment_id,
                before_chars=len(before_text) if isinstance(before_text, str) else None,
                after_chars=len(after_text) if isinstance(after_text, str) else None,
                before_text_hash=text_hash(before_text) if isinstance(before_text, str) else None,
                after_text_hash=text_hash(after_text) if isinstance(after_text, str) else None,
                field=field,
                duration_ms=duration_ms,
            )
            self._emit_user_edit_event(project_dir, event)

        # Speaker change (post_edit_segment_speaker_changed)
        if "speaker_id" in patch:
            before_sp = before_segment.get("speaker_id")
            after_sp = after_segment.get("speaker_id")
            if before_sp != after_sp:
                duration_ms = None
                try:
                    start_ms = int(after_segment.get("start_ms") or 0)
                    end_ms = int(after_segment.get("end_ms") or 0)
                    if end_ms > start_ms:
                        duration_ms = end_ms - start_ms
                except (TypeError, ValueError):
                    duration_ms = None
                event = build_post_edit_segment_speaker_changed_event(
                    ctx,
                    segment_id=segment_id,
                    before_speaker_id=str(before_sp or ""),
                    after_speaker_id=str(after_sp or ""),
                    duration_ms=duration_ms,
                )
                self._emit_user_edit_event(project_dir, event)

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
        so the idle scanner sees activity. Emits
        ``post_edit_segment_split_confirmed`` audit event (plan §7.3)."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import (
            load_editing_segments_for_audit,
            load_segment_status,
            split_editing_segment as _split_editing_segment,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        before_segment = load_editing_segments_for_audit(record.project_dir, segment_id)
        result = _split_editing_segment(
            record.project_dir,
            segment_id=segment_id,
            split_source_index=int(split_source_index),
            split_cn_index=int(split_cn_index),
            speaker_a=speaker_a,
            speaker_b=speaker_b,
        )
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_split_audit(
                record,
                original_segment_id=segment_id,
                result=result,
                before_segment=before_segment,
                split_source_index=split_source_index,
                split_cn_index=split_cn_index,
                speaker_a=speaker_a,
                speaker_b=speaker_b,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_segment_split_confirmed audit emit failed for %s", job_id
            )

        # Enrich with the post-split status map so the frontend can patch
        # its cached state in one shot rather than re-fetching.
        return {
            **result,
            "segment_status": load_segment_status(record.project_dir),
        }

    def suggest_split_for_segment(
        self,
        job_id: str,
        segment_id: str,
        *,
        speaker_name_map: dict[str, str],
        available_speaker_ids: list[str],
        video_title: str = "",
    ) -> dict:
        """Phase 2b v2: LLM-backed split suggestion (plan §5.4 v2).

        User-explicit-trigger only (frontend button click). Per-segment
        cap = 1; per-job cap = MAX(MIN(0.2 × N, anomaly_count), 5).
        Reuses admin-configured Pass1 model + audio fallback chain.

        Returns dict with needs_split / reason / cuts / usage. Raises
        EditingConflictError on bad segment_id; SplitSuggestSegmentUsedError
        / SplitSuggestCapExhaustedError on rate limit; SplitSuggestNoAudioError
        on missing audio; SplitSuggestError on LLM failure.
        """
        from services.jobs.editing_split_suggest import suggest_split_for_segment as _suggest
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        return _suggest(
            record.project_dir,
            segment_id,
            speaker_name_map=speaker_name_map,
            available_speaker_ids=available_speaker_ids,
            video_title=video_title,
        )

    def get_suggest_split_quota(self, job_id: str) -> dict:
        """Phase 2b v2: read-only quota for split-suggest endpoint.
        Frontend hits this on modal open to render the counter +
        disable button when cap reached."""
        from services.jobs.editing_split_suggest import get_suggest_split_quota as _quota
        record = self._require_editing(job_id)
        return _quota(record.project_dir)

    def split_editing_segment_many(
        self,
        job_id: str,
        segment_id: str,
        *,
        cuts: list[dict],
        speaker_ids: list[str],
    ) -> dict:
        """Atomic multi-cut split — replace one segment with N+1 pieces.

        Phase 2a (plan 2026-05-17 §5.6). Backed by write-ahead journal
        for atomicity across segments.json / segment_status.json /
        voice_map.json.

        Audit: emits a single ``post_edit_segment_split_many_confirmed``
        event (TODO: define the event type in user_edit_audit.py before
        production rollout — falls back to per-pair single-split events
        currently)."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_segments import (
            load_segment_status,
            split_editing_segment_many as _split_editing_segment_many,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = _split_editing_segment_many(
            record.project_dir,
            segment_id=segment_id,
            cuts=cuts,
            speaker_ids=speaker_ids,
        )
        _touch_editing(record, self.store)

        # Enrich with the post-split status map so the frontend can patch
        # its cached state in one shot.
        return {
            **result,
            "segment_status": load_segment_status(record.project_dir),
        }

    def _emit_post_edit_split_audit(
        self,
        record: JobRecord,
        *,
        original_segment_id: str,
        result: dict,
        before_segment: dict | None,
        split_source_index: int,
        split_cn_index: int,
        speaker_a: str,
        speaker_b: str,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_segment_split_confirmed_event,
        )

        if not record.project_dir:
            return
        ctx = AuditContext.from_job_record(record)
        # split_editing_segment returns
        # ``{"replaced_segment_id": ..., "new_segments": [seg_a, seg_b], ...}``
        # where each entry is a full segment dict carrying ``segment_id``.
        # The earlier ``new_segment_ids`` / ``children`` shape was speculative
        # and never produced by the helper, so every audit event shipped with
        # an empty ``after.child_segment_ids``. Read the actual key.
        new_ids: list[str] = []
        new_segments = result.get("new_segments") if isinstance(result, dict) else None
        if isinstance(new_segments, list):
            for seg in new_segments:
                sid = seg.get("segment_id") if isinstance(seg, dict) else None
                if sid:
                    new_ids.append(str(sid))

        original_speaker = (before_segment or {}).get("speaker_id") if before_segment else None
        event = build_post_edit_segment_split_confirmed_event(
            ctx,
            original_segment_id=str(original_segment_id),
            new_segment_ids=new_ids,
            split_source_index=int(split_source_index),
            split_cn_index=int(split_cn_index),
            speaker_a=speaker_a,
            speaker_b=speaker_b,
            original_speaker=str(original_speaker) if original_speaker else None,
        )
        self._emit_user_edit_event(_Path(record.project_dir), event)

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
        Also refreshes editing_touched_at on success. Emits
        ``post_edit_tts_regenerated`` audit event (plan §7.3); the
        ``usage_event_ids`` correlation field is left empty for P0 — P1
        dataset builder backfills via UsageMeter (job_id, segment_id,
        timestamp window).
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import regenerate_segment_tts as _regenerate
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        caller = tts_caller or getattr(self, "_segment_tts_caller", None)
        result = _regenerate(
            record.project_dir,
            segment_id,
            tts_caller=caller,
            default_tts_model=record.tts_model,
        )
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_tts_regenerated(record, segment_id, result)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_tts_regenerated audit emit failed for %s", job_id
            )
        return result

    def _emit_post_edit_tts_regenerated(
        self,
        record: JobRecord,
        segment_id: str,
        result: dict,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_tts_regenerated_event,
        )

        if not record.project_dir:
            return
        ctx = AuditContext.from_job_record(record)
        # Pull whatever the TTS helper bothered to return; keep tolerant
        # because the result shape varies by provider.
        result = result if isinstance(result, dict) else {}
        provider = result.get("provider")
        voice_id = result.get("voice_id")
        model = result.get("model")
        target_duration_ms = result.get("target_duration_ms")
        draft_audio_duration_ms = result.get("draft_audio_duration_ms") or result.get(
            "audio_duration_ms"
        )
        success = bool(result.get("success", True))
        trigger_reason = result.get("trigger_reason") or "manual_retry"

        event = build_post_edit_tts_regenerated_event(
            ctx,
            segment_id=segment_id,
            trigger_reason=str(trigger_reason),
            provider=provider,
            voice_id=voice_id,
            model=model,
            target_duration_ms=target_duration_ms,
            draft_audio_duration_ms=draft_audio_duration_ms,
            success=success,
        )
        self._emit_user_edit_event(_Path(record.project_dir), event)

    def accept_segment_draft_tts(self, job_id: str, segment_id: str) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import accept_draft_tts
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = accept_draft_tts(record.project_dir, segment_id)
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_draft_tts_event(
                record, segment_id, result, accepted=True
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_draft_tts_accepted audit emit failed for %s", job_id
            )
        return result

    def discard_segment_draft_tts(self, job_id: str, segment_id: str) -> dict:
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_tts import discard_draft_tts
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        result = discard_draft_tts(record.project_dir, segment_id)
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_draft_tts_event(
                record, segment_id, result, accepted=False
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_draft_tts_discarded audit emit failed for %s", job_id
            )
        return result

    def _emit_post_edit_draft_tts_event(
        self,
        record: JobRecord,
        segment_id: str,
        result: dict,
        *,
        accepted: bool,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_draft_tts_accepted_event,
            build_post_edit_draft_tts_discarded_event,
        )

        if not record.project_dir:
            return
        ctx = AuditContext.from_job_record(record)
        result = result if isinstance(result, dict) else {}
        if accepted:
            event = build_post_edit_draft_tts_accepted_event(
                ctx,
                segment_id=segment_id,
                draft_audio_duration_ms=result.get("draft_audio_duration_ms"),
                target_duration_ms=result.get("target_duration_ms"),
                voice_id=result.get("voice_id"),
                provider=result.get("provider"),
            )
        else:
            event = build_post_edit_draft_tts_discarded_event(
                ctx,
                segment_id=segment_id,
                voice_id=result.get("voice_id"),
                provider=result.get("provider"),
                draft_audio_duration_ms=result.get("draft_audio_duration_ms"),
                target_duration_ms=result.get("target_duration_ms"),
            )
        self._emit_user_edit_event(_Path(record.project_dir), event)

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
        result = _batch(
            record.project_dir,
            tts_caller=caller,
            default_tts_model=record.tts_model,
        )
        _touch_editing(record, self.store)
        return result

    def regenerate_selected_dirty_segments_async(
        self,
        job_id: str,
        *,
        segment_ids: list[str],
        tts_caller=None,
    ) -> dict:
        """Async re-TTS for an explicit subset of dirty editing segments."""
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.input_validators import validate_segment_id
        from services.jobs.regenerate_all_async import start_regen_all_async

        normalised: list[str] = []
        seen: set[str] = set()
        for raw_sid in segment_ids:
            sid = str(raw_sid).strip()
            if not sid or sid in seen:
                continue
            validate_segment_id(sid)
            normalised.append(sid)
            seen.add(sid)
        if not normalised:
            raise ValueError("segment_ids must contain at least one segment id")

        record = self._require_editing(job_id)
        caller = tts_caller or getattr(self, "_segment_tts_caller", None)
        task_id = start_regen_all_async(
            project_dir=record.project_dir,
            tts_caller=caller,
            default_tts_model=record.tts_model,
            segment_ids=normalised,
        )
        _touch_editing(record, self.store)
        return {"task_id": task_id, "status": "running"}

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
            project_dir=record.project_dir,
            tts_caller=caller,
            default_tts_model=record.tts_model,
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
        tts_model_key: str | None = None,
        voice_reuse: bool = False,
        requires_worker: bool | None = None,
        worker_target_model: str | None = None,
    ) -> dict:
        """Set per-segment voice override + emit
        ``post_edit_voice_override_changed`` audit event (plan 2026-05-04
        §10.4 — feeds the auto voice-recommendation analysis loop).

        Phase 4.2 E.1 PR #15 P1 二轮 fix (Codex 2026-05-27): caller may
        pass ``requires_worker`` + ``worker_target_model`` so CosyVoice
        clone voice overrides persist their worker routing. The gateway
        editing/voice-map endpoint enriches these from a
        ``user_voices`` lookup (same DB / ownership check as the approve
        flow uses); the pipeline subprocess cannot fabricate them and
        does NOT do its own DB lookup here.
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_voice_map import (
            load_voice_map,
            set_voice_override,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        # Snapshot the pre-set voice_map entry so audit before/after is
        # faithful (segment may already have an override or be unset).
        before_entry: dict | None = None
        try:
            before_entry = load_voice_map(record.project_dir).get(segment_id)
        except Exception:  # noqa: BLE001
            before_entry = None
        result = set_voice_override(
            record.project_dir, segment_id,
            provider=provider,
            voice_id=voice_id,
            tts_model_key=tts_model_key,
            requires_worker=requires_worker,
            worker_target_model=worker_target_model,
        )
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_voice_override_audit(
                record,
                segment_id=segment_id,
                operation="set",
                before_entry=before_entry,
                after_provider=provider,
                after_voice_id=voice_id,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_voice_override_changed audit emit failed for %s", job_id
            )
        if voice_reuse:
            try:
                from services.usage_meter import UsageMeter

                audit_provider = (
                    "minimax_voice_clone"
                    if str(provider or "").strip().lower() in {"minimax", "minimax_tts"}
                    else provider
                )
                UsageMeter(record.project_dir, job_id=job_id).record_voice_reuse(
                    provider=audit_provider,
                    voice_id=voice_id,
                    speaker_id=segment_id,
                    source_voice_id=voice_id,
                    match_confidence="user_confirmed",
                    match_reason="post_edit_reuse_confirmed",
                    extra={
                        "event_id": f"voice_reuse_postedit:{job_id}:{segment_id}:{voice_id}",
                        "source": "post_edit_voice_map",
                        "segment_id": segment_id,
                    },
                )
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception(
                    "post_edit voice reuse audit emit failed for %s", job_id
                )
        return result

    def clear_editing_voice_override(self, job_id: str, segment_id: str) -> dict:
        """Clear per-segment voice override + emit
        ``post_edit_voice_override_changed`` (operation=clear). Idempotent;
        we still emit the event so analysis can see "user reverted to
        auto-match" — that's a useful signal even when the prior override
        was already missing.
        """
        from services.jobs.editing import touch_editing as _touch_editing
        from services.jobs.editing_voice_map import (
            clear_voice_override,
            load_voice_map,
        )
        from services.jobs.input_validators import validate_segment_id

        validate_segment_id(segment_id)
        record = self._require_editing(job_id)
        before_entry: dict | None = None
        try:
            before_entry = load_voice_map(record.project_dir).get(segment_id)
        except Exception:  # noqa: BLE001
            before_entry = None
        result = clear_voice_override(record.project_dir, segment_id)
        _touch_editing(record, self.store)

        try:
            self._emit_post_edit_voice_override_audit(
                record,
                segment_id=segment_id,
                operation="clear",
                before_entry=before_entry,
                after_provider=None,
                after_voice_id=None,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "post_edit_voice_override_changed audit emit failed for %s", job_id
            )
        return result

    def _emit_post_edit_voice_override_audit(
        self,
        record: JobRecord,
        *,
        segment_id: str,
        operation: str,
        before_entry: dict | None,
        after_provider: str | None,
        after_voice_id: str | None,
    ) -> None:
        from pathlib import Path as _Path
        from services.jobs.user_edit_audit import (
            AuditContext,
            build_post_edit_voice_override_changed_event,
        )

        if not record.project_dir:
            return
        before_provider = (before_entry or {}).get("provider") if before_entry else None
        before_voice_id = (before_entry or {}).get("voice_id") if before_entry else None
        ctx = AuditContext.from_job_record(record)
        event = build_post_edit_voice_override_changed_event(
            ctx,
            segment_id=segment_id,
            operation=operation,
            before_voice_id=str(before_voice_id) if before_voice_id else None,
            after_voice_id=str(after_voice_id) if after_voice_id else None,
            before_provider=str(before_provider) if before_provider else None,
            after_provider=str(after_provider) if after_provider else None,
        )
        self._emit_user_edit_event(_Path(record.project_dir), event)

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.store.load_job(job_id)

    def require_job(self, job_id: str) -> JobRecord:
        try:
            return self.store.require_job(job_id)
        except KeyError as exc:
            raise JobNotFoundError(str(exc)) from exc

    def list_jobs(self, *, limit: int | None = None, offset: int = 0) -> list[JobRecord]:
        return self.store.list_jobs(limit=limit, offset=offset)

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
        # P1-15b caller migration (audit 2026-05-07): the stale-job
        # reaper runs from a background scanner thread alongside
        # ProcessJobRunner. The previous require_job → replace →
        # save_job pattern could clobber a fresh runner-side stage
        # update with a snapshot that the scanner had read seconds
        # earlier. Route through update_job so the reaper either wins
        # cleanly or loses cleanly to the runner's progress save —
        # never silently overwrites it.
        timestamp = utc_now_iso()
        error_message = "Recovered stale active job without a live worker process."

        def mutator(current: JobRecord) -> JobRecord:
            # Re-check liveness under the lock. The runner may have
            # produced a stage update between the scanner's last
            # observation and our acquisition of the lock; if the
            # current state is no longer worker-active, do nothing.
            if current.status not in WORKER_ACTIVE_STATUSES:
                return current
            if self.runner.is_process_active(current.job_id):
                # The runner came back to life between our scan and
                # this critical section — leave the record alone.
                return current
            return replace(
                current,
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

        next_record = self.store.update_job(record.job_id, mutator)
        # P1-15b follow-up (Codex review of a687ae6): the mutator above
        # may intentionally return `current` (no-op) when the job is
        # no longer worker-active or the runner came back to life
        # between scan and lock acquisition. In that case we must NOT
        # emit a stale_active_job error event, otherwise the event log
        # gets false "Recovered stale active job" noise for jobs that
        # explicitly weren't marked stale.
        # Detect the no-op via status: if status is still worker-active
        # the mutator decided to leave the record alone; only on a
        # transition to FAILED do we record the recovery event.
        if next_record.status == JOB_STATUS_FAILED and \
                next_record.current_stage == STAGE_FAILED and \
                (next_record.error_summary or {}).get("error_type") == "stale_active_job":
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

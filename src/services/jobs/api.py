from __future__ import annotations

import io
import os
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import quote, urlparse
import zipfile

from services.jobs.service import (
    JobConflictError,
    JobNotFoundError,
    JobService,
    UnsupportedJobRequestError,
)


JOB_API_DEFAULT_HOST = "127.0.0.1"
JOB_API_DEFAULT_PORT = 8877


def build_job_api_server(
    *,
    service: JobService,
    host: str = JOB_API_DEFAULT_HOST,
    port: int = JOB_API_DEFAULT_PORT,
) -> ThreadingHTTPServer:
    handler_class = _build_job_api_handler(service=service)
    return ThreadingHTTPServer((host, port), handler_class)


def _build_job_api_handler(*, service: JobService) -> type[BaseHTTPRequestHandler]:
    class JobAPIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if path_parts == ["jobs"]:
                    payload = {
                        "jobs": [record.to_dict() for record in service.list_jobs()],
                    }
                    self._write_json(HTTPStatus.OK, payload)
                    return
                if len(path_parts) == 2 and path_parts[0] == "jobs":
                    job = service.require_job(path_parts[1])
                    self._write_json(HTTPStatus.OK, job.to_dict())
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "logs":
                    events = service.read_logs(path_parts[1])
                    self._write_json(
                        HTTPStatus.OK,
                        {
                            "job_id": path_parts[1],
                            "events": [event.to_dict() for event in events],
                            "lines": [event.message for event in events if event.message],
                        },
                    )
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "result-summary":
                    self._write_json(
                        HTTPStatus.OK,
                        service.get_result_summary(path_parts[1]),
                    )
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "artifacts":
                    self._write_json(
                        HTTPStatus.OK,
                        service.get_artifacts(path_parts[1]),
                    )
                    return
                # --- Phase 1: review-state (job-scoped, strict) ---
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "review-state":
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    self._write_json(
                        HTTPStatus.OK,
                        _build_review_state_for_job(record, project_dir=project_dir, project_root=service.runner.project_root),
                    )
                    return
                # --- Phase 1: key-based download ---
                if len(path_parts) == 4 and path_parts[0] == "jobs" and path_parts[2] == "download":
                    job_id = path_parts[1]
                    download_key = path_parts[3]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    download_path = _resolve_download_path(
                        project_root=service.runner.project_root,
                        project_dir=project_dir,
                        download_key=download_key,
                    )
                    if download_path is None:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "Requested download was not found."})
                        return
                    content_type = mimetypes.guess_type(str(download_path))[0] or "application/octet-stream"
                    self._write_binary(
                        HTTPStatus.OK,
                        download_path.read_bytes(),
                        content_type=content_type,
                        download_name=download_path.name,
                    )
                    return
                # --- Phase 1: tts-segments-zip ---
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "tts-segments-zip":
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    tts_dir = project_dir / "tts"
                    if not tts_dir.is_dir():
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "TTS segments not found"})
                        return
                    aligned_files = sorted(tts_dir.glob("*_aligned.wav"))
                    if not aligned_files:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "No aligned TTS segments"})
                        return
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in aligned_files:
                            zf.write(f, f.name)
                    self._write_binary(
                        HTTPStatus.OK,
                        buf.getvalue(),
                        content_type="application/zip",
                        download_name=f"tts_segments_{job_id[:12]}.zip",
                    )
                    return
                # --- Phase 1: voice-library (global) ---
                if path_parts == ["voice-library"]:
                    self._write_json(
                        HTTPStatus.OK,
                        _build_global_voice_library(project_root=service.runner.project_root),
                    )
                    return
                # --- speaker-audio: list segments ---
                if (len(path_parts) == 4
                        and path_parts[0] == "jobs"
                        and path_parts[2] == "speaker-audio"):
                    job_id = path_parts[1]
                    speaker_id = path_parts[3]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    from services.jobs.review_actions import get_speaker_audio_segments
                    result = get_speaker_audio_segments(
                        project_dir=project_dir,
                        speaker_id=speaker_id,
                    )
                    # Fix audio_url with actual job_id
                    for seg in result.get("segments", []):
                        seg["audio_url"] = f"/job-api/jobs/{job_id}/speaker-audio/{speaker_id}/{seg['segment_id']}.wav"
                    self._write_json(HTTPStatus.OK, result)
                    return
                # --- speaker-audio: serve WAV segment ---
                if (len(path_parts) == 5
                        and path_parts[0] == "jobs"
                        and path_parts[2] == "speaker-audio"):
                    import re
                    job_id = path_parts[1]
                    speaker_id = path_parts[3]
                    seg_filename = path_parts[4]
                    seg_match = re.match(r"^(\d+)\.wav$", seg_filename)
                    if not seg_match:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": "无效的片段文件名"})
                        return
                    segment_id = int(seg_match.group(1))
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    from services.jobs.review_actions import extract_speaker_audio_segment
                    wav_bytes = extract_speaker_audio_segment(
                        project_dir=project_dir,
                        speaker_id=speaker_id,
                        segment_id=segment_id,
                    )
                    self._write_binary(
                        HTTPStatus.OK,
                        wav_bytes,
                        content_type="audio/wav",
                    )
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except JobNotFoundError as exc:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except UnsupportedJobRequestError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflictError as exc:
                self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if path_parts == ["jobs"]:
                    payload = self._read_json_payload()
                    source_payload = payload.get("source", {})
                    if not isinstance(source_payload, dict):
                        raise ValueError("source must be an object")
                    # Parse optional snapshot fields
                    raw_requires_review = payload.get("requires_review")
                    parsed_requires_review = bool(raw_requires_review) if raw_requires_review is not None else None
                    raw_voice_clone = payload.get("voice_clone_enabled")
                    parsed_voice_clone = bool(raw_voice_clone) if raw_voice_clone is not None else None
                    raw_duration = payload.get("source_duration_seconds")
                    parsed_duration = float(raw_duration) if raw_duration is not None else None
                    raw_quota = payload.get("quota_cost")
                    parsed_quota = int(raw_quota) if raw_quota is not None else None
                    raw_est_duration = payload.get("estimated_duration_seconds")
                    parsed_est_duration = float(raw_est_duration) if raw_est_duration is not None else None

                    job = service.submit_job(
                        job_type=str(payload.get("job_type") or "localize_video"),
                        source_type=str(source_payload.get("type") or ""),
                        source_ref=str(source_payload.get("value") or ""),
                        output_target=str(payload.get("output_target") or "editor"),
                        speakers=str(payload.get("speakers") or "auto"),
                        voice_a=str(payload.get("voice_a") or "").strip() or None,
                        voice_b=str(payload.get("voice_b") or "").strip() or None,
                        transcription_method=str(payload.get("transcription_method") or "assemblyai").strip() or None,
                        service_mode=str(payload["service_mode"]).strip() if payload.get("service_mode") else None,
                        tts_provider=str(payload["tts_provider"]).strip() if payload.get("tts_provider") else None,
                        tts_model=str(payload["tts_model"]).strip() if payload.get("tts_model") else None,
                        requires_review=parsed_requires_review,
                        voice_clone_enabled=parsed_voice_clone,
                        voice_strategy=str(payload["voice_strategy"]).strip() if payload.get("voice_strategy") else None,
                        plan_code_snapshot=str(payload["plan_code_snapshot"]).strip() if payload.get("plan_code_snapshot") else None,
                        role_snapshot=str(payload["role_snapshot"]).strip() if payload.get("role_snapshot") else None,
                        source_duration_seconds=parsed_duration,
                        estimated_duration_seconds=parsed_est_duration,
                        quota_cost=parsed_quota,
                        quota_state=str(payload.get("quota_state") or "none").strip(),
                        create_idempotency_key=str(payload["create_idempotency_key"]).strip() if payload.get("create_idempotency_key") else None,
                        user_id=str(payload["user_id"]).strip() if payload.get("user_id") else None,
                        source_content_hash=str(payload["source_content_hash"]).strip() if payload.get("source_content_hash") else None,
                    )
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "continue":
                    job = service.continue_job(path_parts[1])
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                # --- Phase 2: review write endpoints ---
                if (len(path_parts) >= 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "review"):
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    review_subpath = "/".join(path_parts[3:])

                    if review_subpath == "translation/approve":
                        _require_review_gate(record, expected_stage="translation_review")
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import approve_translation
                        approve_translation(
                            project_dir=project_dir,
                            segments_payload=payload.get("segments"),
                            segment_speakers=payload.get("segment_speakers") if isinstance(payload.get("segment_speakers"), dict) else None,
                        )
                        # Continue the job after approval
                        continued = service.continue_job(job_id)
                        self._write_json(HTTPStatus.OK, {"success": True, "job": continued.to_dict()})
                        return

                    if review_subpath == "translation-config/approve":
                        _require_review_gate(record, expected_stage="translation_config_review")
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import approve_translation_config
                        approve_translation_config(
                            project_dir=project_dir,
                            selected_model=str(payload.get("selected_model", "")).strip() or None,
                            prompt_template=payload.get("prompt_template"),
                        )
                        # Continue the job after approval
                        continued = service.continue_job(job_id)
                        self._write_json(HTTPStatus.OK, {"success": True, "job": continued.to_dict()})
                        return

                    if review_subpath == "split-segment":
                        _require_waiting_for_review(record)
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import split_segment
                        result = split_segment(
                            project_dir=project_dir,
                            stage=str(payload.get("stage", "translation_review")),
                            segment_id=payload.get("segment_id"),
                            split_source_index=int(payload["split_source_index"]) if payload.get("split_source_index") is not None else None,
                            split_cn_index=int(payload["split_cn_index"]) if payload.get("split_cn_index") is not None else None,
                            speaker_a=str(payload["speaker_a"]).strip() if payload.get("speaker_a") else None,
                            speaker_b=str(payload["speaker_b"]).strip() if payload.get("speaker_b") else None,
                            pending_speaker_changes=payload.get("pending_speaker_changes") if isinstance(payload.get("pending_speaker_changes"), dict) else None,
                        )
                        self._write_json(HTTPStatus.OK, {"success": True, "split_result": result})
                        return

                    if review_subpath == "preview-segment":
                        _require_waiting_for_review(record)
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import preview_segment
                        from services import config_loader
                        result = preview_segment(
                            project_dir=project_dir,
                            segment_id=payload.get("segment_id"),
                            source_start_ms=float(payload["source_start_ms"]) if payload.get("source_start_ms") is not None else None,
                            source_end_ms=float(payload["source_end_ms"]) if payload.get("source_end_ms") is not None else None,
                            cn_text=str(payload.get("cn_text", "")),
                            voice_id=str(payload.get("voice_id", "")),
                            config_path=config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH,
                        )
                        self._write_json(HTTPStatus.OK, result)
                        return

                    if review_subpath == "voice/preview":
                        _require_waiting_for_review(record)
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import preview_voice
                        from services import config_loader
                        result = preview_voice(
                            voice_id=str(payload.get("voice_id", "")).strip(),
                            config_path=config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH,
                            tts_provider=str(payload.get("tts_provider", "")).strip() or None,
                            sample_text=str(payload.get("sample_text", "")).strip() or None,
                        )
                        self._write_json(HTTPStatus.OK, result)
                        return

                    if review_subpath == "voice/clone":
                        _require_waiting_for_review(record)
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import clone_voice
                        from services import config_loader
                        result = clone_voice(
                            project_dir=project_dir,
                            speaker_id=str(payload.get("speaker_id", "")).strip(),
                            speaker_name=str(payload.get("speaker_name", "")).strip() or None,
                            sample_path=str(payload.get("sample_path", "")).strip() or None,
                            config_path=config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH,
                            project_root=service.runner.project_root,
                        )
                        self._write_json(HTTPStatus.OK, result)
                        return

                    if review_subpath == "voice-selection/approve":
                        _require_review_gate(record, expected_stage="voice_selection_review")
                        payload = self._read_json_payload()
                        from services.jobs.review_actions import approve_voice_selection
                        approve_voice_selection(
                            project_dir=project_dir,
                            speakers=payload.get("speakers", []),
                        )
                        continued = service.continue_job(job_id)
                        self._write_json(HTTPStatus.OK, {"success": True, "job": continued.to_dict()})
                        return

                    self._write_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown review action: {review_subpath}"})
                    return
                # --- Internal endpoints: require API key if configured ---
                if path_parts and path_parts[0] == "internal":
                    _internal_key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
                    if _internal_key:
                        req_key = self.headers.get("X-Internal-Key", "").strip()
                        if req_key != _internal_key:
                            self._write_json(HTTPStatus.FORBIDDEN, {"error": "Invalid or missing X-Internal-Key"})
                            return

                # --- Internal: CosyVoice verify (TTS synthesis check) ---
                if path_parts == ["internal", "voice-verify", "cosyvoice"]:
                    payload = self._read_json_payload()
                    voice_id = str(payload.get("voice_id", "")).strip()
                    if not voice_id:
                        raise ValueError("voice_id is required")
                    test_text = str(payload.get("test_text", "这是一段验证音色可用性的测试。"))
                    try:
                        from services.tts.cosyvoice_provider import synthesize as cosy_synth
                        wav_bytes = cosy_synth(text=test_text, voice=voice_id)
                        ok = len(wav_bytes) > 1000
                        self._write_json(HTTPStatus.OK, {
                            "ok": ok,
                            "bytes": len(wav_bytes),
                            "error": None if ok else f"音频太短 ({len(wav_bytes)} bytes)",
                        })
                    except Exception as exc:
                        self._write_json(HTTPStatus.OK, {
                            "ok": False,
                            "bytes": 0,
                            "error": str(exc)[:500],
                        })
                    return

                # --- Internal: voice label tasks ---
                if path_parts == ["internal", "voice-label", "text"]:
                    payload = self._read_json_payload()
                    voices = payload.get("voices", [])
                    if not isinstance(voices, list) or not voices:
                        raise ValueError("voices must be a non-empty list of metadata dicts")
                    from services.jobs.voice_label_tasks import run_text_labeling
                    labels = run_text_labeling(voices)
                    self._write_json(HTTPStatus.OK, {"ok": True, "labels": labels})
                    return

                if (len(path_parts) == 4
                        and path_parts[:3] == ["internal", "voice-label", "audio"]):
                    round_name = path_parts[3]
                    payload = self._read_json_payload()
                    voices = payload.get("voices", [])
                    if not isinstance(voices, list) or not voices:
                        raise ValueError("voices must be a non-empty list of metadata dicts")
                    from services.jobs.voice_label_tasks import run_audio_profiling
                    labels = run_audio_profiling(voices, round_name)
                    self._write_json(HTTPStatus.OK, {"ok": True, "labels": labels})
                    return

                # --- Phase 1: job-scoped cancel ---
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "cancel":
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    if record.status not in ("queued", "running", "waiting_for_review"):
                        raise JobConflictError(f"job {job_id} is not in a cancellable state (current: {record.status})")
                    service.runner.stop_process(job_id)
                    from dataclasses import replace as _replace
                    from services.state_manager import utc_now_iso
                    timestamp = utc_now_iso()
                    cancelled_record = _replace(
                        record,
                        status="cancelled",
                        current_stage="failed",
                        progress_message="Job cancelled by user.",
                        updated_at=timestamp,
                        completed_at=timestamp,
                    )
                    service.store.save_job(cancelled_record)
                    self._write_json(HTTPStatus.OK, {"success": True, "job": cancelled_record.to_dict()})
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except JobNotFoundError as exc:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except UnsupportedJobRequestError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except JobConflictError as exc:
                self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if len(path_parts) == 2 and path_parts[0] == "jobs":
                    deleted = service.cancel_and_delete_job(path_parts[1])
                    if deleted:
                        self._write_json(HTTPStatus.OK, {"deleted": True, "job_id": path_parts[1]})
                    else:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": f"Job not found: {path_parts[1]}"})
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except Exception as exc:  # pragma: no cover
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

        def _read_json_payload(self) -> dict[str, object]:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                return {}
            body = self.rfile.read(content_length)
            if not body:
                return {}
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            return payload

        def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(serialized_payload)))
            self.end_headers()
            self.wfile.write(serialized_payload)

        def _write_binary(
            self,
            status: HTTPStatus,
            payload: bytes,
            *,
            content_type: str,
            download_name: str | None = None,
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            if download_name:
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(download_name)}",
                )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return JobAPIHandler


# ---------------------------------------------------------------------------
# Phase 1 helper functions (handler-level, not in JobService)
# ---------------------------------------------------------------------------

def _require_waiting_for_review(record: object) -> None:
    """Verify that the job is in waiting_for_review status. Raises JobConflictError if not."""
    status = str(getattr(record, "status", "")).strip()
    if status != "waiting_for_review":
        raise JobConflictError(
            f"Job {getattr(record, 'job_id', '?')} is not waiting_for_review (current: {status})"
        )


def _require_review_gate(record: object, *, expected_stage: str) -> None:
    """Verify that the job is in waiting_for_review AND its review_gate matches the expected stage.

    Raises JobConflictError if the job is not in the correct review state.
    Must be called BEFORE any disk writes to prevent writing to a job in the wrong state.
    """
    _require_waiting_for_review(record)
    review_gate = getattr(record, "review_gate", None)
    if not isinstance(review_gate, dict):
        raise JobConflictError(
            f"Job {getattr(record, 'job_id', '?')} has no review_gate"
        )
    gate_stage = str(review_gate.get("stage", "")).strip()
    if gate_stage != expected_stage:
        raise JobConflictError(
            f"Job {getattr(record, 'job_id', '?')} review gate is '{gate_stage}', expected '{expected_stage}'"
        )


def _require_project_dir(record: object) -> Path:
    """Extract and validate project_dir from a JobRecord."""
    project_dir_text = getattr(record, "project_dir", None)
    if not project_dir_text or not str(project_dir_text).strip():
        raise JobNotFoundError(f"Job {getattr(record, 'job_id', '?')} has no project_dir")
    project_dir = Path(str(project_dir_text)).resolve(strict=False)
    if not project_dir.exists():
        raise JobNotFoundError(f"Project directory does not exist: {project_dir}")
    return project_dir


def _build_review_state_for_job(
    record: object,
    *,
    project_dir: Path,
    project_root: Path,
) -> dict[str, object]:
    """Build review state for a specific job using its verified project_dir.

    Strict job-scoped: does NOT fall back to youtube_url matching.
    The caller must have already validated project_dir via _require_project_dir().
    """
    from services.web_ui.project_resolver import _build_results_snapshot
    from services.web_ui.voice_library import _build_voice_library_snapshot
    from services import config_loader

    job_id = getattr(record, "job_id", "")
    config_path = config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH

    # Build job_snapshot with explicit project_dir only.
    # Deliberately omit youtube_url to prevent _resolve_project_dir_for_results
    # from falling back to URL-based matching of other projects.
    job_snapshot: dict[str, object] = {
        "job_id": job_id,
        "status": getattr(record, "status", ""),
        "project_dir": str(project_dir),
        "review_gate": getattr(record, "review_gate", None),
    }

    results_snapshot = _build_results_snapshot(
        project_root=project_root,
        job_snapshot=job_snapshot,
    )

    transcript_items = []
    if isinstance(results_snapshot.get("transcript_review"), dict):
        transcript_items = list(results_snapshot["transcript_review"].get("items", []))

    voice_library_snapshot = _build_voice_library_snapshot(
        project_root=project_root,
        config_path=config_path,
        project_dir=project_dir,
        transcript_items=transcript_items,
    )
    results_snapshot["voice_library"] = voice_library_snapshot

    return {
        "job_id": job_id,
        "status": getattr(record, "status", ""),
        "review_gate": getattr(record, "review_gate", None),
        "results": results_snapshot,
    }


def _resolve_download_path(
    *,
    project_root: Path,
    project_dir: Path,
    download_key: str,
) -> Path | None:
    """Resolve a whitelisted download key to a file path."""
    from services.web_ui.project_resolver import _resolve_public_result_download_path
    return _resolve_public_result_download_path(
        project_root=project_root,
        project_dir=project_dir,
        download_key=download_key,
    )


def _build_global_voice_library(*, project_root: Path) -> dict[str, object]:
    """Build the global voice library snapshot (not job-scoped)."""
    from services.web_ui.voice_library import _build_voice_library_snapshot
    from services import config_loader

    config_path = config_loader.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH
    return _build_voice_library_snapshot(
        project_root=project_root,
        config_path=config_path,
        project_dir=None,
        transcript_items=[],
    )

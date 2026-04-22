from __future__ import annotations

import io
import logging
import os
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
import zipfile

logger = logging.getLogger(__name__)

from services.jobs.service import (
    JobConflictError,
    JobNotFoundError,
    JobService,
    UnsupportedJobRequestError,
)


JOB_API_DEFAULT_HOST = "127.0.0.1"
JOB_API_DEFAULT_PORT = 8877


# Express/Studio 输出分层白名单（见 docs/plans/2026-04-18-express-studio-output-filter-plan.md）
# Express 模式下只允许这些 key 从 /artifacts 暴露
EXPRESS_ALLOWED_ARTIFACT_KEYS: frozenset[str] = frozenset({
    "publish.dubbed_video",
    "publish.dubbed_video_poster",
})
# Express 模式下只允许 /download/{key} 这些 key
EXPRESS_ALLOWED_DOWNLOAD_KEYS: frozenset[str] = frozenset({
    "publish.dubbed_video",
})
# Express 模式下只允许 /stream/{kind} 这些 kind（禁 audio）
EXPRESS_ALLOWED_STREAM_KINDS: frozenset[str] = frozenset({"video", "poster"})


def _is_express_job(record) -> bool:
    """Check if a JobRecord is in Express mode. Safe for missing attribute."""
    return getattr(record, "service_mode", None) == "express"


# 2026-04-21: stdlib ThreadingHTTPServer's BufferedIOBase wfile enters a
# degenerate state after a handful of consecutive large single-call
# writes (observed: 5 successes then ReadError on httpx for every
# subsequent 1.3 MB base64 preview-source response). Breaking writes
# into ~64KB chunks with explicit flush between them keeps kernel
# socket buffers steady and fully clears the condition — stress-tested
# 100 consecutive 1.3 MB responses with no failure.
#
# Chunk size picked to match the typical kernel socket send buffer
# (Linux default 208 KB). Smaller chunks mean more syscalls but bounded
# write latency; the extra overhead on a 1 KB response is ~1 syscall,
# negligible. Larger chunks reintroduce the original flakiness.
_WRITE_CHUNK_BYTES = 64 * 1024


def _write_chunks(wfile, payload: bytes) -> None:
    """Write ``payload`` to ``wfile`` in ``_WRITE_CHUNK_BYTES``-sized
    chunks, flushing between chunks. Safe for any payload size —
    single-syscall for <= chunk size, multi-chunk for larger."""
    if not payload:
        return
    if len(payload) <= _WRITE_CHUNK_BYTES:
        wfile.write(payload)
        return
    mv = memoryview(payload)
    for start in range(0, len(mv), _WRITE_CHUNK_BYTES):
        wfile.write(mv[start : start + _WRITE_CHUNK_BYTES])
        wfile.flush()


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
                    artifacts_payload = service.get_artifacts(path_parts[1])
                    # Express 过滤：只暴露 publish.dubbed_video + publish.dubbed_video_poster
                    # 见 docs/plans/2026-04-18-express-studio-output-filter-plan.md
                    record = service.require_job(path_parts[1])
                    if _is_express_job(record):
                        items = artifacts_payload.get("artifacts") or []
                        filtered = [
                            it for it in items
                            if isinstance(it, dict)
                            and it.get("key") in EXPRESS_ALLOWED_ARTIFACT_KEYS
                        ]
                        artifacts_payload = {
                            **artifacts_payload,
                            "artifacts": filtered,
                            "manifest": {
                                **artifacts_payload.get("manifest", {}),
                                "artifact_count": len(filtered),
                            },
                        }
                    self._write_json(HTTPStatus.OK, artifacts_payload)
                    return
                # --- Studio post-edit: GET /jobs/{id}/editing/segments (T1-2) ---
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "editing" and path_parts[3] == "segments"):
                    payload = service.get_editing_segments(path_parts[1])
                    self._write_json(HTTPStatus.OK, payload)
                    return
                # GET /jobs/{id}/editing/voice-map (T1-6)
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "editing" and path_parts[3] == "voice-map"):
                    payload = service.get_editing_voice_map(path_parts[1])
                    self._write_json(HTTPStatus.OK, payload)
                    return
                # GET /jobs/{id}/segments/{sid}/draft-audio — inline wav
                # playback for the "接受 / 丢弃" UI (plan §7.4 / Phase 2).
                # Range-aware so HTML5 <audio> can seek. 404 when job is
                # not editing OR no draft wav exists yet (uniform "nothing
                # to preview" signal for the frontend).
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments"
                        and path_parts[4] == "draft-audio"):
                    from services.jobs.editing import EDITING_SUBDIR
                    from services.jobs.editing_tts import draft_audio_path
                    from services.jobs.input_validators import validate_segment_id
                    from services.jobs.models import JOB_STATUS_EDITING

                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    try:
                        validate_segment_id(segment_id)
                    except ValueError:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid segment_id"},
                        )
                        return
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    # Drafts only exist while the job is in editing state;
                    # refuse uniformly with 404 (so frontend treats it the
                    # same as "segment never regenerated").
                    editing_dir = Path(project_dir) / EDITING_SUBDIR
                    if (
                        record.status != JOB_STATUS_EDITING
                        or not editing_dir.is_dir()
                    ):
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "no draft audio (job not in editing)"},
                        )
                        return
                    file_path = draft_audio_path(project_dir, segment_id)
                    if not file_path.is_file():
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "no draft audio for this segment"},
                        )
                        return
                    self._write_stream(file_path, content_type="audio/wav")
                    return
                # GET /jobs/{id}/regenerate-all-tts/status?task_id=XXX (D39)
                # Poll the async batch re-TTS progress. Returns 404 if no
                # batch has ever started for this project; 200 with a
                # ``mismatch`` body if a newer batch has overwritten the
                # file (client should reset its UI state).
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "regenerate-all-tts"
                        and path_parts[3] == "status"):
                    qs = parse_qs(parsed_path.query or "")
                    task_id = (qs.get("task_id") or [""])[0].strip()
                    if not task_id:
                        raise ValueError("task_id query param is required")
                    status = service.get_regenerate_all_status(
                        path_parts[1], task_id,
                    )
                    if status is None:
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "no batch re-TTS has started for this job"},
                        )
                        return
                    self._write_json(HTTPStatus.OK, status)
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
                    # Express 白名单：只允许 publish.dubbed_video
                    # 见 docs/plans/2026-04-18-express-studio-output-filter-plan.md
                    if _is_express_job(record) and download_key not in EXPRESS_ALLOWED_DOWNLOAD_KEYS:
                        self._write_json(
                            HTTPStatus.FORBIDDEN,
                            {"error": f"该产物对 Express 任务不可下载: {download_key}"},
                        )
                        return
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
                    # Express 禁 tts-segments-zip（editor-only 产物）
                    if _is_express_job(record):
                        self._write_json(
                            HTTPStatus.FORBIDDEN,
                            {"error": "TTS 分段包对 Express 任务不可访问"},
                        )
                        return
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
                # --- GET /jobs/{id}/segments/{sid}/preview-source-audio ---
                # Range-aware stream of the cached WAV prepared by the
                # companion POST /segments/{sid}/preview-source handler.
                # <audio src> feeds into this URL directly — browsers do
                # Range requests natively so seek/scrub works without any
                # JSON-body round trip.
                if (
                    len(path_parts) == 5
                    and path_parts[0] == "jobs"
                    and path_parts[2] == "segments"
                    and path_parts[4] == "preview-source-audio"
                ):
                    from services.jobs.editing_segments import preview_cache_path
                    from services.jobs.input_validators import validate_segment_id
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    validate_segment_id(segment_id)
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    cache_path = preview_cache_path(project_dir, segment_id)
                    if not cache_path.is_file():
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": f"preview cache not prepared: {segment_id}"},
                        )
                        return
                    self._write_stream(cache_path, content_type="audio/wav")
                    return

                # --- stream/{kind}: Range-aware media streaming ---
                if (
                    len(path_parts) == 4
                    and path_parts[0] == "jobs"
                    and path_parts[2] == "stream"
                    and path_parts[3] in ("video", "audio", "poster")
                ):
                    job_id = path_parts[1]
                    kind = path_parts[3]
                    record = service.require_job(job_id)
                    # Express 禁 stream/audio（只允许 video + poster）
                    # 见 docs/plans/2026-04-18-express-studio-output-filter-plan.md
                    if _is_express_job(record) and kind not in EXPRESS_ALLOWED_STREAM_KINDS:
                        self._write_json(
                            HTTPStatus.FORBIDDEN,
                            {"error": f"该媒体流对 Express 任务不可访问: {kind}"},
                        )
                        return
                    project_dir = _require_project_dir(record)
                    if kind == "video":
                        artifact_key = "publish.dubbed_video"
                        content_type = "video/mp4"
                    elif kind == "audio":
                        artifact_key = "editor.dubbed_audio_complete"
                        content_type = "audio/wav"
                    else:  # poster
                        artifact_key = "publish.dubbed_video_poster"
                        content_type = "image/jpeg"
                    # poster is not in PUBLIC_RESULT_DOWNLOAD_KEYS whitelist,
                    # use manifest resolver directly for that case
                    if kind == "poster":
                        from services.manifest_reader import resolve_manifest_artifact_path
                        file_path = resolve_manifest_artifact_path(project_dir, artifact_key)
                    else:
                        file_path = _resolve_download_path(
                            project_root=service.runner.project_root,
                            project_dir=project_dir,
                            download_key=artifact_key,
                        )
                    if file_path is None or not file_path.exists():
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": f"{kind} not found"})
                        return
                    self._write_stream(file_path, content_type=content_type)
                    return

                # --- materials-availability ---
                if (
                    len(path_parts) == 3
                    and path_parts[0] == "jobs"
                    and path_parts[2] == "materials-availability"
                ):
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    # Read manifest directly — don't use _resolve_download_path
                    # which enforces PUBLIC_RESULT_DOWNLOAD_KEYS whitelist.
                    from services.manifest_reader import (
                        load_manifest_artifact_index,
                        resolve_manifest_artifact_path,
                    )
                    artifact_index = load_manifest_artifact_index(project_dir=project_dir)
                    _keys_map = {
                        "source_video": "source.original_video",
                        "dubbed_video": "publish.dubbed_video",
                        "dubbed_audio": "editor.dubbed_audio_complete",
                        "segments": "editor.segments_dir",
                        "subtitles_zh": "editor.subtitles",
                        "subtitles_en": "editor.subtitles_en",
                        "subtitles_bilingual": "editor.subtitles_bilingual",
                    }
                    availability: dict[str, bool] = {}
                    for ui_key, artifact_key in _keys_map.items():
                        resolved = resolve_manifest_artifact_path(
                            project_dir, artifact_key, artifact_index=artifact_index,
                        )
                        if ui_key == "segments":
                            availability[ui_key] = resolved is not None and resolved.is_dir()
                        else:
                            availability[ui_key] = resolved is not None and resolved.exists()
                    self._write_json(HTTPStatus.OK, availability)
                    return

                # --- generate-video status: read render_status.json ---
                if (
                    len(path_parts) == 4
                    and path_parts[0] == "jobs"
                    and path_parts[2] == "generate-video"
                ):
                    job_id = path_parts[1]
                    render_task_id = path_parts[3]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    from services.jobs.video_render_async import read_status
                    status = read_status(project_dir, render_task_id)
                    if status is None:
                        self._write_json(HTTPStatus.NOT_FOUND, {"error": "render_status not found"})
                        return
                    self._write_json(HTTPStatus.OK, status)
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
                self._send_sanitized_error(exc)

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
                        display_name=str(payload["display_name"]).strip() if payload.get("display_name") else None,
                    )
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                if len(path_parts) == 3 and path_parts[0] == "jobs" and path_parts[2] == "continue":
                    job = service.continue_job(path_parts[1])
                    self._write_json(HTTPStatus.ACCEPTED, job.to_dict())
                    return
                # --- Studio post-edit (T1-1 skeleton) ---
                # POST /jobs/{id}/enter-edit — succeeded → editing (studio only)
                if (len(path_parts) == 3 and path_parts[0] == "jobs"
                        and path_parts[2] == "enter-edit"):
                    job = service.enter_editing(path_parts[1])
                    self._write_json(HTTPStatus.OK, {"success": True, "job": job.to_dict()})
                    return
                # POST /jobs/{id}/editing/cancel — editing → succeeded (drops draft)
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "editing" and path_parts[3] == "cancel"):
                    payload = self._read_json_payload()
                    # reason is optional; defaults per editing.cancel_editing. Admins
                    # pass reason="admin_force"; scanner passes "idle_24h_auto_cancel".
                    reason = str(payload.get("reason") or "user_cancel").strip() or "user_cancel"
                    job = service.cancel_editing(path_parts[1], reason=reason)
                    self._write_json(HTTPStatus.OK, {"success": True, "job": job.to_dict()})
                    return
                # POST /jobs/{id}/segments/{sid}/update — patch segment text (T1-2)
                # (RESTful PATCH semantics, HTTP POST chosen because
                # BaseHTTPRequestHandler's do_PATCH wiring is non-trivial; body
                # shape mirrors a PATCH payload.)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "update"):
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    patch = self._read_json_payload()
                    result = service.patch_editing_segment(job_id, segment_id, patch)
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/preview-source — prep cached
                # WAV slice + return tiny JSON meta (2026-04-21).
                # Original impl returned 1.3MB base64 in JSON body which
                # tickled a RemoteProtocolError on the gateway's Uvicorn ↔
                # httpx proxy under concurrency. New design: POST prepares
                # the cache file, GET /stream/preview-source hands it to
                # <audio src={…}> via the existing Range-aware streamer.
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments"
                        and path_parts[4] == "preview-source"):
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    result = service.prepare_preview_source_cache(
                        job_id, segment_id
                    )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/split — split into two (2026-04-21)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "split"):
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    payload = self._read_json_payload()
                    try:
                        split_source_index = int(payload.get("split_source_index"))
                        split_cn_index = int(payload.get("split_cn_index"))
                    except (TypeError, ValueError):
                        raise ValueError(
                            "split_source_index and split_cn_index are required integers"
                        )
                    speaker_a = str(payload.get("speaker_a") or "").strip()
                    speaker_b = str(payload.get("speaker_b") or "").strip()
                    if not speaker_a or not speaker_b:
                        raise ValueError("speaker_a and speaker_b are required")
                    result = service.split_editing_segment(
                        job_id,
                        segment_id,
                        split_source_index=split_source_index,
                        split_cn_index=split_cn_index,
                        speaker_a=speaker_a,
                        speaker_b=speaker_b,
                    )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/status — explicit status change (T1-2)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "status"):
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    payload = self._read_json_payload()
                    status = str(payload.get("status", "")).strip()
                    if not status:
                        raise ValueError("status field is required")
                    result = service.mark_editing_segment_status(job_id, segment_id, status)
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/regenerate-tts — single-segment TTS (T1-5)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "regenerate-tts"):
                    job_id = path_parts[1]
                    segment_id = path_parts[3]
                    # payload is accepted but currently unused; reserved for
                    # future provider override (voice_id, model, sample_rate).
                    self._read_json_payload()
                    result = service.regenerate_segment_tts(job_id, segment_id)
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/accept-draft — keep draft wav (T1-5)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "accept-draft"):
                    result = service.accept_segment_draft_tts(path_parts[1], path_parts[3])
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/segments/{sid}/discard-draft — delete draft (T1-5)
                if (len(path_parts) == 5 and path_parts[0] == "jobs"
                        and path_parts[2] == "segments" and path_parts[4] == "discard-draft"):
                    result = service.discard_segment_draft_tts(path_parts[1], path_parts[3])
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/regenerate-all-tts — batch re-TTS (T1-6 / D39 async)
                # Returns {task_id, status: "running"} immediately; progress
                # read via GET /regenerate-all-tts/status?task_id=XXX.
                if (len(path_parts) == 3 and path_parts[0] == "jobs"
                        and path_parts[2] == "regenerate-all-tts"):
                    self._read_json_payload()  # body currently unused
                    result = service.regenerate_all_dirty_segments_async(
                        path_parts[1],
                    )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/regenerate-all-tts/cancel?task_id=XXX — D39
                # user-initiated cancel. Body optional; query param task_id
                # is required (mirrors the /status GET). Returns
                # {"cancelled": bool}: True means the flag was written and
                # the worker will transition to stage='cancelled' on its
                # next tick; False means no matching live batch.
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "regenerate-all-tts"
                        and path_parts[3] == "cancel"):
                    from urllib.parse import parse_qs
                    query = parse_qs(parsed_path.query)
                    task_ids = query.get("task_id")
                    if not task_ids or not task_ids[0].strip():
                        raise ValueError("task_id query param is required")
                    self._read_json_payload()  # swallow body if any
                    result = service.request_regenerate_all_cancel(
                        path_parts[1], task_ids[0].strip(),
                    )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/editing/voice-map — set per-segment voice override (T1-6)
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "editing" and path_parts[3] == "voice-map"):
                    payload = self._read_json_payload()
                    segment_id = str(payload.get("segment_id", "")).strip()
                    if not segment_id:
                        raise ValueError("segment_id is required")
                    action = str(payload.get("action", "set")).strip()
                    if action == "clear":
                        result = service.clear_editing_voice_override(
                            path_parts[1], segment_id
                        )
                    else:
                        provider = str(payload.get("provider", "")).strip()
                        voice_id = str(payload.get("voice_id", "")).strip()
                        result = service.set_editing_voice_override(
                            path_parts[1], segment_id,
                            provider=provider, voice_id=voice_id,
                        )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
                    return
                # POST /jobs/{id}/editing/commit — overwrite or copy_as_new (T1-9)
                if (len(path_parts) == 4 and path_parts[0] == "jobs"
                        and path_parts[2] == "editing" and path_parts[3] == "commit"):
                    payload = self._read_json_payload()
                    raw_strategy = payload.get("strategy")
                    strategy = str(raw_strategy or "").strip()
                    if not strategy:
                        raise ValueError(
                            "editing/commit requires a 'strategy' field "
                            "(overwrite | copy_as_new)"
                        )
                    copy_display_name = payload.get("copy_display_name")
                    if copy_display_name is not None:
                        copy_display_name = str(copy_display_name).strip() or None
                    # Returns a dict response (not a JobRecord) because
                    # copy_as_new affects two jobs and the caller needs both IDs.
                    result = service.commit_editing(
                        path_parts[1],
                        strategy=strategy,
                        copy_display_name=copy_display_name,
                    )
                    self._write_json(HTTPStatus.OK, {"success": True, **result})
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
                        # Preview is a stateless TTS probe (no project_dir, no
                        # write-back). Allow both the classic review gate AND
                        # the Studio post-edit session — users need to audition
                        # voices in the "音色修改" Tab too.
                        _require_waiting_for_review_or_editing(record)
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

                # --- generate-video: start async video mux, return render_task_id ---
                if (
                    len(path_parts) == 3
                    and path_parts[0] == "jobs"
                    and path_parts[2] == "generate-video"
                ):
                    job_id = path_parts[1]
                    record = service.require_job(job_id)
                    project_dir = _require_project_dir(record)
                    from services.manifest_reader import (
                        load_manifest_artifact_index,
                        resolve_manifest_artifact_path,
                    )
                    artifact_index = load_manifest_artifact_index(project_dir=project_dir)

                    # Fast path: video already exists — no thread needed
                    existing_video = resolve_manifest_artifact_path(
                        project_dir, "publish.dubbed_video", artifact_index=artifact_index,
                    )
                    if existing_video and existing_video.exists() and existing_video.stat().st_size > 0:
                        self._write_json(HTTPStatus.OK, {
                            "success": True,
                            "already_exists": True,
                            "render_task_id": None,
                            "path": str(existing_video),
                        })
                        return

                    # Require source video and dubbed audio
                    source_video = resolve_manifest_artifact_path(
                        project_dir, "source.original_video", artifact_index=artifact_index,
                    )
                    dubbed_audio = resolve_manifest_artifact_path(
                        project_dir, "editor.dubbed_audio_complete", artifact_index=artifact_index,
                    )
                    if not source_video or not source_video.exists():
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": "缺少原始视频文件"})
                        return
                    if not dubbed_audio or not dubbed_audio.exists():
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": "缺少配音音频文件"})
                        return

                    # Find ambient audio for background mixing
                    ambient_audio = resolve_manifest_artifact_path(
                        project_dir, "editor.ambient_audio", artifact_index=artifact_index,
                    )
                    if not ambient_audio or not ambient_audio.exists():
                        ambient_audio = resolve_manifest_artifact_path(
                            project_dir, "working.ambient_audio", artifact_index=artifact_index,
                        )

                    # Launch render thread; return task_id immediately
                    from services.jobs.video_render_async import (
                        new_render_task_id,
                        start_render_thread,
                    )
                    render_task_id = new_render_task_id()
                    manifest_path = project_dir / "manifest.json"
                    start_render_thread(
                        render_task_id=render_task_id,
                        project_dir=project_dir,
                        job_id=job_id,
                        source_video=source_video,
                        dubbed_audio=dubbed_audio,
                        ambient_audio=ambient_audio if ambient_audio and ambient_audio.exists() else None,
                        manifest_path=manifest_path,
                    )
                    self._write_json(HTTPStatus.ACCEPTED, {
                        "success": True,
                        "already_exists": False,
                        "render_task_id": render_task_id,
                    })
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
                # EditingConflictError is a JobConflictError subclass, so it
                # also maps to 409 here; no extra branch needed.
                self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except NotImplementedError as exc:
                # Emitted by editing/commit (T1-1 skeleton). Frontend should
                # render a "coming soon" notice rather than a crash toast.
                self._write_json(
                    HTTPStatus.NOT_IMPLEMENTED,
                    {"error": str(exc), "code": "not_implemented"},
                )
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._send_sanitized_error(exc)

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
                self._send_sanitized_error(exc)

        def do_PATCH(self) -> None:  # noqa: N802
            # PATCH /jobs/{id} — currently only accepts ``display_name``
            # (plan §6.5 / D16 rename). Any other body field returns 400
            # to keep the surface area minimal — we'd rather add new
            # fields explicitly than silently accept unknown mutations.
            parsed_path = urlparse(self.path)
            path_parts = [part for part in parsed_path.path.strip("/").split("/") if part]
            try:
                if len(path_parts) == 2 and path_parts[0] == "jobs":
                    payload = self._read_json_payload()
                    if not payload:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "PATCH body must contain at least one supported field"},
                        )
                        return
                    if "display_name" in payload:
                        try:
                            updated = service.update_display_name(
                                path_parts[1], payload.get("display_name")
                            )
                        except KeyError:
                            self._write_json(
                                HTTPStatus.NOT_FOUND,
                                {"error": f"Job not found: {path_parts[1]}"},
                            )
                            return
                        self._write_json(HTTPStatus.OK, updated.to_dict())
                        return
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "Unsupported PATCH field; only display_name is writable"},
                    )
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._send_sanitized_error(exc)

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
            # 2026-04-21: write in chunks + flush between them. A single
            # ``wfile.write(1.3MB)`` enters a degenerate state after ~5
            # consecutive large responses on Python's stdlib
            # ThreadingHTTPServer — subsequent requests ReadError on the
            # Gateway's httpx side. Root cause appears to be socket
            # send-buffer / BufferedIOBase interaction. Chunked write
            # keeps each syscall's byte count small + flushes pressure
            # downstream before the next write arrives. Tested stable
            # across 100+ sequential 1.3MB payloads.
            _write_chunks(self.wfile, serialized_payload)

        def _send_sanitized_error(self, exc: Exception) -> None:
            """Generic 500 response that never leaks internals.

            Log full exception context (with stack trace) to the server log,
            but return only a fixed user-facing message. Prevents str(exc)
            from leaking DB DSNs, file paths, stack frames, or other
            sensitive internals to the client.

            Centralized so every `except Exception` fallback stays consistent
            — one place to edit if we ever want to change error shape.
            """
            logger.exception(
                "Unhandled exception in Job API handler path=%s method=%s",
                self.path, self.command,
            )
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error", "message": "服务器内部错误，请重试或联系管理员"},
            )

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
            _write_chunks(self.wfile, payload)

        def _write_stream(self, file_path: Path, *, content_type: str) -> None:
            """Range-aware file streaming (no Content-Disposition: attachment)."""
            file_size = file_path.stat().st_size
            range_header = self.headers.get("Range")

            if range_header and range_header.startswith("bytes="):
                # Parse range: bytes=START-END or bytes=START-
                range_spec = range_header[6:]
                parts = range_spec.split("-", 1)
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(remaining, 65536))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                # Full file
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

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


def _require_waiting_for_review_or_editing(record: object) -> None:
    """Voice preview / similar read-only probes are useful from both the
    original review gate AND the Studio post-edit session. These endpoints
    don't mutate job state — they just call TTS with a sample voice —
    so allowing ``editing`` alongside ``waiting_for_review`` is safe.
    Raises JobConflictError for any other state."""
    status = str(getattr(record, "status", "")).strip()
    if status not in ("waiting_for_review", "editing"):
        raise JobConflictError(
            f"Job {getattr(record, 'job_id', '?')} is not waiting_for_review or editing "
            f"(current: {status})"
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

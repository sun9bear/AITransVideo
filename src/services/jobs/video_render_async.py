"""Threaded video render with status-file progress reporting.

Part of Export Tasks v1. Gateway's ``execute_generate_video`` coordinator
calls this via HTTP: POST starts a thread and returns ``render_task_id``;
GET reads the status file. Renders run in a plain ``threading.Thread``
(Job API is stdlib http.server; no asyncio here).

Status file location: ``{project_dir}/publish/render_status.json``.
One render per project at a time — a new POST while the previous is still
running will start a new thread and overwrite the status file. The
Gateway's fingerprint-based dedup prevents this in practice; this module
does not enforce singleton.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def new_render_task_id() -> str:
    return uuid.uuid4().hex[:12]


def status_file_path(project_dir: Path) -> Path:
    return project_dir / "publish" / "render_status.json"


def read_status(project_dir: Path, render_task_id: str) -> dict[str, Any] | None:
    """Return the status dict if the file exists and matches the task id."""
    path = status_file_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("render_task_id") != render_task_id:
        # A newer render is in progress — return the file anyway so the
        # caller sees "task_id_mismatch" and can decide.
        return {"mismatch": True, "actual_task_id": data.get("render_task_id")}
    return data


def _write_status_atomic(project_dir: Path, payload: dict[str, Any]) -> None:
    """Write status JSON via tmpfile + rename to avoid torn reads."""
    path = status_file_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Failed to write render status: %s", exc)


def _initial_status(render_task_id: str) -> dict[str, Any]:
    return {
        "render_task_id": render_task_id,
        "stage": "starting",
        "percent": 0,
        "result": None,
        "error": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def start_render_thread(
    *,
    render_task_id: str,
    project_dir: Path,
    job_id: str,
    source_video: Path,
    dubbed_audio: Path,
    ambient_audio: Path | None,
    manifest_path: Path,
) -> None:
    """Spawn a daemon thread that renders the dubbed video and updates status.json."""
    # Seed initial status synchronously so immediate GET sees something sane.
    _write_status_atomic(project_dir, _initial_status(render_task_id))

    thread = threading.Thread(
        target=_run_render,
        name=f"video-render-{render_task_id}",
        kwargs={
            "render_task_id": render_task_id,
            "project_dir": project_dir,
            "job_id": job_id,
            "source_video": source_video,
            "dubbed_audio": dubbed_audio,
            "ambient_audio": ambient_audio,
            "manifest_path": manifest_path,
        },
        daemon=True,
    )
    thread.start()


def _run_render(
    *,
    render_task_id: str,
    project_dir: Path,
    job_id: str,
    source_video: Path,
    dubbed_audio: Path,
    ambient_audio: Path | None,
    manifest_path: Path,
) -> None:
    from modules.output.publish.video_renderer import VideoRenderer
    from modules.output.publish.publish_models import PublishRequest

    def _progress(payload: dict[str, Any]) -> None:
        _write_status_atomic(
            project_dir,
            {
                "render_task_id": render_task_id,
                "stage": payload.get("stage", "muxing"),
                "percent": int(payload.get("percent", 0)),
                "result": None,
                "error": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    publish_dir = project_dir / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)

    try:
        req = PublishRequest(
            project_id=job_id,
            original_video_path=str(source_video),
            dubbed_audio_path=str(dubbed_audio),
            output_dir=str(publish_dir),
            ambient_audio_path=(
                str(ambient_audio) if ambient_audio and ambient_audio.exists() else None
            ),
        )
        result = VideoRenderer().render(req, progress_callback=_progress)
    except Exception as exc:  # noqa: BLE001 — thread top-level
        logger.exception("Video render failed for job %s", job_id)
        _write_status_atomic(
            project_dir,
            {
                "render_task_id": render_task_id,
                "stage": "failed",
                "percent": 0,
                "result": None,
                "error": f"视频生成失败: {exc}"[:500],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    # Update manifest artifact_index (non-fatal)
    try:
        if manifest_path.exists():
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "artifact_index" not in manifest_data:
                manifest_data["artifact_index"] = {}
            manifest_data["artifact_index"]["publish.dubbed_video"] = result.dubbed_video_path
            manifest_path.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:  # noqa: BLE001 — manifest update is advisory
        logger.warning("Manifest update failed for job %s (video still exists)", job_id)

    _write_status_atomic(
        project_dir,
        {
            "render_task_id": render_task_id,
            "stage": "done",
            "percent": 100,
            "result": {"path": result.dubbed_video_path},
            "error": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

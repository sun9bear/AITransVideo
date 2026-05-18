"""Background task executors.

Each executor is an async function that receives the task id and params,
performs the work, and updates the task row via ``background_task_queue``.
Executors are launched via ``asyncio.create_task`` from the API layer;
they own their own DB session (independent of the request session).

Two executors for Export Tasks v1:
- ``execute_materials_pack``: Gateway-native zip packaging.
- ``execute_generate_video``: HTTP coordinates with Job API's threaded render.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx

import background_task_queue as queue
from config import settings
from database import async_session
from materials_pack_common import (
    MAX_ZIP_SIZE_BYTES,
    collect_files_for_items,
    load_artifact_index,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# materials_pack
# -----------------------------------------------------------------------------

async def execute_materials_pack(
    *,
    task_id: str,
    job_id: str,
    project_dir: Path,
    params: dict[str, Any],
) -> None:
    """Pack selected materials into a zip stored under {project_dir}/exports.

    Deletes any prior ``materials_*.zip`` in the project's exports directory
    before writing the new one — per-project retention = latest only.

    D-4 (2026-05-05): when the user picked the ``subtitles`` item, we
    delegate to Job API's ``/internal/jobs/{job_id}/ensure-whisper-aligned-subtitles``
    BEFORE packing so the SRT in the zip uses whisper-aligned timing
    (admin-gated). Gateway has no direct access to the cue pipeline /
    whisper code (separate container, separate Python image), so the
    HTTP delegation is the cleanest seam. Failure of the ensure call
    is logged + tolerated — packing continues with the on-disk SRT.
    """
    async with async_session() as db:
        try:
            await queue.mark_running(db, task_id)

            item_list = [
                str(s).strip() for s in params.get("items", []) if str(s).strip()
            ]
            if not item_list:
                await queue.mark_failed(db, task_id, "未选择任何素材")
                return

            if not project_dir.is_dir():
                await queue.mark_failed(db, task_id, "项目目录不存在")
                return

            # D-4 ensure-whisper-aligned-subtitles delegation (only when
            # subtitles are part of the selected items). Idempotent + admin-
            # gated; helper returns "skipped_admin_disabled" / fast path
            # / regenerated. We don't block the pack on errors here.
            if "subtitles" in item_list:
                await queue.update_progress(
                    db, task_id,
                    {"stage": "aligning_subtitles", "percent": 5, "files": 0},
                )
                await _ensure_whisper_aligned_subtitles(job_id)

            artifact_index = load_artifact_index(project_dir)
            files_to_pack, total_size = collect_files_for_items(
                project_dir=project_dir,
                artifact_index=artifact_index,
                item_list=item_list,
            )

            if not files_to_pack:
                await queue.mark_failed(db, task_id, "没有可打包的文件")
                return
            if total_size > MAX_ZIP_SIZE_BYTES:
                await queue.mark_failed(db, task_id, "素材包过大，请减少选择项")
                return

            # Report progress (coarse, no per-file update — zip is fast)
            await queue.update_progress(
                db, task_id, {"stage": "packing", "percent": 10, "files": len(files_to_pack)},
            )

            exports_dir = project_dir / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)

            # Retention: delete prior materials_*.zip for this project
            for old_zip in exports_dir.glob("materials_*.zip"):
                try:
                    old_zip.unlink()
                except OSError as exc:
                    logger.warning("Failed to delete old materials zip %s: %s", old_zip, exc)

            zip_path = exports_dir / f"materials_{task_id}.zip"
            # Zip on a thread to avoid blocking the event loop
            await asyncio.to_thread(_write_zip, zip_path, files_to_pack)

            if not zip_path.exists() or zip_path.stat().st_size == 0:
                await queue.mark_failed(db, task_id, "打包后的文件为空")
                return

            # Canonical download filename (what the user sees in browser)
            download_filename = f"materials_{job_id[:12]}.zip"

            result_payload = {
                "zip_path": str(zip_path),
                "size_bytes": zip_path.stat().st_size,
                "filename": download_filename,
            }
            await queue.mark_completed(db, task_id, result_payload)

        except Exception as exc:  # noqa: BLE001 — task top-level
            logger.exception("materials_pack failed for task %s", task_id)
            try:
                await queue.mark_failed(db, task_id, f"打包失败: {exc}"[:500])
            except Exception:
                pass


def _write_zip(zip_path: Path, files: list[tuple[str, Path]]) -> None:
    """Write zip synchronously. Called via asyncio.to_thread."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, file_path in files:
            zf.write(file_path, arcname)


# Internal endpoint timeout — deliberately generous because cold cache
# whisper run can take ~10+ min on a 38-min audio. Job API end will
# stream subprocess work but we wait for the HTTP response. If it hits,
# we fall through to packing the on-disk SRT.
_ENSURE_WHISPER_TIMEOUT_SEC = 60 * 30  # 30 min hard cap


async def _ensure_whisper_aligned_subtitles(job_id: str) -> None:
    """D-4: HTTP-delegate to Job API's internal endpoint.

    Gateway and Job API run in separate containers with separate Python
    images (Gateway image is built from ``./gateway/`` only). The
    whisper helper lives in ``src/services/subtitles/...`` which is
    NOT in the gateway image — the cleanest cross-container call is
    HTTP, mirroring the existing ``generate-video`` pattern.

    Failure semantics: any HTTP error / timeout is logged and
    SWALLOWED. The materials_pack flow continues, packs whatever SRT
    is on disk (likely proportional). The whisper helper itself is
    idempotent and gates on admin policy, so a missed call here is
    not a correctness issue — just a "next click might benefit from
    re-trying" UX hiccup.
    """
    from internal_auth import internal_headers
    upstream = settings.job_api_upstream.rstrip("/")
    url = f"{upstream}/internal/jobs/{job_id}/ensure-whisper-aligned-subtitles"
    try:
        async with httpx.AsyncClient(timeout=_ENSURE_WHISPER_TIMEOUT_SEC) as client:
            r = await client.post(url, headers=internal_headers())
        if r.status_code == 200:
            try:
                payload = r.json()
            except ValueError:
                payload = {"action": "unknown"}
            logger.info(
                "ensure-whisper-aligned-subtitles for job %s: %s",
                job_id, payload,
            )
        else:
            logger.warning(
                "ensure-whisper-aligned-subtitles for job %s: HTTP %d (proceeding "
                "with on-disk SRT)",
                job_id, r.status_code,
            )
    except (httpx.HTTPError, OSError) as exc:
        logger.warning(
            "ensure-whisper-aligned-subtitles for job %s: %s (proceeding with "
            "on-disk SRT)", job_id, exc,
        )


# -----------------------------------------------------------------------------
# generate_video
# -----------------------------------------------------------------------------

# Short HTTP timeout — each request to Job API is short-lived. The render
# itself runs in Job API's own thread; Gateway only polls status.
_JOB_API_HTTP_TIMEOUT_SEC = 30.0
# Poll cadence for render status
_POLL_INTERVAL_SEC = 4.0
# Max wall-clock this executor will wait for render completion. Protects
# against runaway renders hanging a Gateway coroutine forever. If hit, the
# task is marked failed but the Job API thread may still complete.
_MAX_WAIT_SEC = 60 * 60  # 1 hour


async def execute_generate_video(
    *,
    task_id: str,
    job_id: str,
    project_dir: Path,
    params: dict[str, Any],  # noqa: ARG001 — reserved for future params
) -> None:
    """Tell Job API to render; poll its status; mirror to background_tasks row."""
    async with async_session() as db:
        try:
            await queue.mark_running(db, task_id)

            upstream = settings.job_api_upstream.rstrip("/")
            start_url = f"{upstream}/jobs/{job_id}/generate-video"

            async with httpx.AsyncClient(timeout=_JOB_API_HTTP_TIMEOUT_SEC) as client:
                # Kick off render (or fast-path completion)
                try:
                    r = await client.post(start_url)
                except httpx.HTTPError as exc:
                    await queue.mark_failed(db, task_id, f"无法联系 Job API: {exc}"[:500])
                    return

                if r.status_code == 400:
                    # Missing inputs — propagate the error message verbatim
                    err = _extract_error(r) or "缺少渲染所需输入"
                    await queue.mark_failed(db, task_id, err)
                    return
                if r.status_code >= 500:
                    await queue.mark_failed(db, task_id, f"Job API 错误: HTTP {r.status_code}")
                    return

                try:
                    body = r.json()
                except ValueError:
                    await queue.mark_failed(db, task_id, "Job API 返回非 JSON")
                    return

                if body.get("already_exists"):
                    await queue.mark_completed(
                        db,
                        task_id,
                        {"video_ready": True, "path": body.get("path")},
                    )
                    return

                render_task_id = body.get("render_task_id")
                if not render_task_id:
                    await queue.mark_failed(db, task_id, "Job API 未返回 render_task_id")
                    return

                status_url = f"{upstream}/jobs/{job_id}/generate-video/{render_task_id}"
                elapsed = 0.0
                while elapsed < _MAX_WAIT_SEC:
                    await asyncio.sleep(_POLL_INTERVAL_SEC)
                    elapsed += _POLL_INTERVAL_SEC

                    try:
                        sr = await client.get(status_url)
                    except httpx.HTTPError as exc:
                        logger.warning("Status poll failed for %s: %s", task_id, exc)
                        continue  # tolerate transient network blips

                    if sr.status_code == 404:
                        # Status file vanished — treat as failed
                        await queue.mark_failed(db, task_id, "渲染状态丢失")
                        return
                    if sr.status_code >= 500:
                        logger.warning("Status poll 5xx for %s", task_id)
                        continue

                    try:
                        status = sr.json()
                    except ValueError:
                        continue

                    if status.get("mismatch"):
                        # A newer render overwrote status.json. Bail.
                        await queue.mark_failed(
                            db, task_id, "渲染任务被另一个新任务覆盖",
                        )
                        return

                    stage = status.get("stage", "")
                    percent = int(status.get("percent", 0) or 0)
                    error = status.get("error")

                    if error:
                        await queue.mark_failed(db, task_id, str(error)[:500])
                        return

                    if stage == "done":
                        result_info = status.get("result") or {}
                        await queue.mark_completed(
                            db,
                            task_id,
                            {"video_ready": True, "path": result_info.get("path")},
                        )
                        return

                    # Mirror progress to background_tasks row
                    await queue.update_progress(
                        db,
                        task_id,
                        {"stage": stage or "muxing", "percent": percent},
                    )

                # Timeout
                await queue.mark_failed(db, task_id, "渲染超时（超过 1 小时未完成）")

        except Exception as exc:  # noqa: BLE001 — task top-level
            logger.exception("generate_video failed for task %s", task_id)
            try:
                await queue.mark_failed(db, task_id, f"渲染失败: {exc}"[:500])
            except Exception:
                pass


def _extract_error(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        err = data.get("error") or data.get("detail")
        if isinstance(err, str):
            return err
    return None


# -----------------------------------------------------------------------------
# Dispatch table
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Pan backup / restore / residue cleanup — dispatcher adapters (CodeX P2)
# -----------------------------------------------------------------------------
#
# The pan executors in gateway/pan/{backup,restore,residue_cleanup}.py use
# a payload-dict signature for clean injection-seam testing. The
# background_task_queue dispatcher (background_task_api.py:98) invokes
# executors as `executor(task_id=..., job_id=..., project_dir=..., params=...)`,
# so these adapters do the signature translation + drive BackgroundTask
# lifecycle (mark_running / mark_completed / mark_failed) for UI consistency.
#
# Production payloads (params):
#   pan_backup / pan_restore / pan_residue_cleanup:
#     {'user_id': str(UUID), 'provider': str?}
#
# Per the T5.11.6 source-of-truth split, `BackupRecord.status` is
# authoritative for backup/restore lifecycle. BackgroundTask.status is
# scheduling-only — UI reads from `backup_records` for definitive state.


async def execute_pan_backup_dispatched(
    *,
    task_id: str,
    job_id: str,
    project_dir: Path,  # noqa: ARG001 — re-read from Job row inside executor
    params: dict[str, Any],
) -> None:
    """Dispatcher adapter for `pan_backup` task type."""
    from pan.backup_executor import execute_pan_backup

    payload = {
        'job_id': job_id,
        'user_id': str(params['user_id']),
        'provider': params.get('provider', 'baidu_pan'),
    }

    async with async_session() as db:
        await queue.mark_running(db, task_id)
    try:
        await execute_pan_backup(payload)
    except Exception as exc:  # noqa: BLE001
        async with async_session() as db:
            await queue.mark_failed(db, task_id, str(exc)[:500])
        return
    async with async_session() as db:
        await queue.mark_completed(db, task_id, {'pan_backup': 'ok'})


async def execute_pan_restore_dispatched(
    *,
    task_id: str,
    job_id: str,
    project_dir: Path,  # noqa: ARG001
    params: dict[str, Any],
) -> None:
    """Dispatcher adapter for `pan_restore` task type."""
    from pan.restore_executor import execute_pan_restore

    payload = {
        'job_id': job_id,
        'user_id': str(params['user_id']),
        'provider': params.get('provider', 'baidu_pan'),
    }

    async with async_session() as db:
        await queue.mark_running(db, task_id)
    try:
        await execute_pan_restore(payload)
    except Exception as exc:  # noqa: BLE001
        async with async_session() as db:
            await queue.mark_failed(db, task_id, str(exc)[:500])
        return
    async with async_session() as db:
        await queue.mark_completed(db, task_id, {'pan_restore': 'ok'})


async def execute_pan_residue_cleanup_dispatched(
    *,
    task_id: str,
    job_id: str,
    project_dir: Path,  # noqa: ARG001
    params: dict[str, Any],
) -> None:
    """Dispatcher adapter for `pan_residue_cleanup` task type.

    params must include 'backup_id' (CodeX P2) — picking "latest uploaded"
    by (user_id, job_id) is ambiguous when prior failed attempts left
    multiple BackupRecord rows.
    """
    from pan.residue_cleanup import execute_pan_residue_cleanup

    if 'backup_id' not in params:
        raise ValueError(
            "pan_residue_cleanup task params missing required 'backup_id'."
        )
    payload = {
        'job_id': job_id,
        'user_id': str(params['user_id']),
        'provider': params.get('provider', 'baidu_pan'),
        'backup_id': str(params['backup_id']),
    }

    async with async_session() as db:
        await queue.mark_running(db, task_id)
    try:
        await execute_pan_residue_cleanup(payload)
    except Exception as exc:  # noqa: BLE001
        async with async_session() as db:
            await queue.mark_failed(db, task_id, str(exc)[:500])
        return
    async with async_session() as db:
        await queue.mark_completed(db, task_id, {'pan_residue_cleanup': 'ok'})


TASK_EXECUTORS = {
    "materials_pack": execute_materials_pack,
    "generate_video": execute_generate_video,
    "pan_backup": execute_pan_backup_dispatched,
    "pan_restore": execute_pan_restore_dispatched,
    "pan_residue_cleanup": execute_pan_residue_cleanup_dispatched,
}

"""Job API intercept layer — inject user_id, filter by ownership.

Gateway intercepts job-related requests to:
1. Inject user_id when creating a job
2. Filter job listings by user_id
3. Verify job ownership for single-job operations
4. Sync job metadata to PostgreSQL (dual-write)

The upstream Job API (8877) is the sole backend service.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from auth import require_auth
from config import settings
from database import get_db
from models import Job, User
from proxy import proxy_request
from quota import check_quota, reserve_quota, settle_job_quota, TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Structured error codes — spec §7
# ---------------------------------------------------------------------------

def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    detail: dict | None = None,
) -> Response:
    """Return a JSON error with structured error_code for frontend consumption."""
    body: dict = {"error": error_code, "message": message}
    if detail:
        body["detail"] = detail
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )


# --- Plan catalog ---
# The authoritative plan gate facts now live in ``plan_catalog.py``. The module-level
# ``PLAN_CATALOG`` name is preserved as a backward-compatible view so existing imports
# (including ``tests/test_gateway_job_policy.py``) keep working without change.
from plan_catalog import get_legacy_plan_gate_dict  # noqa: E402

PLAN_CATALOG = get_legacy_plan_gate_dict()


# Gateway-local allowed TTS providers (no cross-layer import from tts_strategy)
_VALID_EXPRESS_PROVIDERS = {"cosyvoice", "mimo", "volcengine"}
_VALID_STUDIO_PROVIDERS = {"minimax", "mimo", "volcengine"}
_DEFAULT_EXPRESS_PROVIDER = "cosyvoice"
_DEFAULT_STUDIO_PROVIDER = "minimax"


def compute_job_policy(user, service_mode: str) -> dict:
    """Compute job execution policy based on user role, plan, and service mode.

    TTS provider is read from admin settings (express_tts_provider / studio_tts_provider).
    Invalid values fall back to defaults (cosyvoice / minimax).

    Note on ``tts_model`` semantics — this field means different things per provider:

    * **minimax**: MiniMax model name (``speech-2.8-hd`` / ``speech-2.8-turbo``)
    * **cosyvoice**: CosyVoice model name (``cosyvoice-v3-flash``)
    * **volcengine**: value for ``req_params.model`` in the V3 API body
      (``seed-tts-1.1`` for express / *None* for studio 2.0 public voices).
      The ``resource_id`` (``seed-tts-1.0`` vs ``seed-tts-2.0``) is **not** stored
      in the snapshot — it is derived at runtime by the Generator layer from
      ``tts_provider + service_mode``.
    """
    from admin_settings import load_settings

    role = getattr(user, "role", "user") or "user"
    plan = getattr(user, "plan_code", "free") or "free"

    # Admin bypasses all limits
    is_admin = role == "admin"

    admin = load_settings()

    if service_mode == "studio":
        configured_provider = (admin.studio_tts_provider or "").strip().lower()
        tts_provider = configured_provider if configured_provider in _VALID_STUDIO_PROVIDERS else _DEFAULT_STUDIO_PROVIDER

        if tts_provider == "volcengine":
            # 豆包 2.0 — public voices do not need req_params.model;
            # voice cloning not supported on 2.0 (reserved for future seed-icl-2.0).
            tts_model = None
            voice_clone_enabled = False
        else:
            tts_model = "speech-2.8-hd" if (plan == "pro" or is_admin) else "speech-2.8-turbo"
            voice_clone_enabled = True

        return {
            "service_mode": "studio",
            "tts_provider": tts_provider,
            "tts_model": tts_model,
            "requires_review": True,
            "voice_clone_enabled": voice_clone_enabled,
            "voice_strategy": "user_selected",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
        }
    else:
        # Default: express
        configured_provider = (admin.express_tts_provider or "").strip().lower()
        tts_provider = configured_provider if configured_provider in _VALID_EXPRESS_PROVIDERS else _DEFAULT_EXPRESS_PROVIDER

        if tts_provider == "volcengine":
            # 豆包 1.0 — use seed-tts-1.1 for improved quality / latency.
            tts_model = "seed-tts-1.1"
        else:
            tts_model = "cosyvoice-v3-flash"

        return {
            "service_mode": "express",
            "tts_provider": tts_provider,
            "tts_model": tts_model,
            "requires_review": False,
            "voice_clone_enabled": False,
            "voice_strategy": "preset_mapping",
            "plan_code_snapshot": plan,
            "role_snapshot": role,
        }


def _probe_youtube_duration(url: str, timeout: float = 5.0) -> float | None:
    """Lightweight yt-dlp metadata probe. Returns duration in seconds or None on failure."""
    import subprocess
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", "--no-warnings", url],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            meta = json.loads(result.stdout)
            dur = meta.get("duration")
            if dur is not None:
                return float(dur)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, Exception) as e:
        logger.warning("yt-dlp probe failed for %s: %s", url, e)
    return None


async def _compensate_upstream_job(job_id: str) -> None:
    """Best-effort cancel/delete of an upstream job after local quota failure."""
    import httpx as _httpx
    upstream_url = f"{settings.job_api_upstream.rstrip('/')}/jobs/{job_id}"
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            resp = await client.delete(upstream_url)
            logger.info("Compensated upstream job %s: status=%s", job_id, resp.status_code)
    except Exception as exc:
        logger.error("Failed to compensate upstream job %s: %s", job_id, exc)


async def intercept_list_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs — forward to upstream, then filter by user_id."""
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If auth not required or no user, return as-is
    if not settings.auth_required or user is None:
        return upstream_response

    # Filter jobs by user_id, with auto-reconciliation for orphan jobs
    try:
        data = json.loads(upstream_response.body)
        all_jobs = data.get("jobs", [])

        # Get all job_ids in DB (any user)
        result_all = await db.execute(select(Job.job_id))
        all_db_job_ids = {row[0] for row in result_all.all()}

        # Get this user's job_ids
        result_user = await db.execute(select(Job.job_id).where(Job.user_id == user.id))
        user_job_ids = {row[0] for row in result_user.all()}

        # Log orphan jobs but do NOT auto-claim
        orphan_ids = [j.get("job_id") for j in all_jobs if j.get("job_id") and j.get("job_id") not in all_db_job_ids]
        if orphan_ids:
            print(f"[GATEWAY] ⚠ {len(orphan_ids)} orphan job(s) not in DB: {orphan_ids[:5]}", flush=True)

        # Sync status from upstream to DB + settle quota on terminal transitions
        upstream_by_id = {j.get("job_id"): j for j in all_jobs if j.get("job_id")}
        for jid in user_job_ids:
            upstream_job = upstream_by_id.get(jid)
            if upstream_job:
                upstream_status = upstream_job.get("status", "")
                upstream_stage = upstream_job.get("current_stage")
                try:
                    result_job = await db.execute(
                        select(Job).where(Job.job_id == jid)
                    )
                    db_job = result_job.scalar_one_or_none()
                    if db_job is not None:
                        old_status = db_job.status
                        db_job.status = upstream_status
                        db_job.current_stage = upstream_stage
                        # Settle quota when transitioning to terminal status
                        if upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES:
                            await settle_job_quota(db, db_job, upstream_status)
                except Exception:
                    pass
        try:
            await db.commit()
        except Exception:
            await db.rollback()

        # Only return jobs that belong to this user in DB
        filtered_jobs = [j for j in all_jobs if j.get("job_id") in user_job_ids]
        print(f"[GATEWAY] list_jobs: upstream={len(all_jobs)}, db_user={len(user_job_ids)}, returning={len(filtered_jobs)}", flush=True)
        data["jobs"] = filtered_jobs

        return Response(
            content=json.dumps(data, ensure_ascii=False),
            status_code=200,
            headers={"content-type": "application/json"},
        )
    except Exception as exc:
        import traceback
        print(f"[GATEWAY] ❌ Failed to filter jobs: {exc}", flush=True)
        print(f"[GATEWAY] ❌ Traceback: {traceback.format_exc()}", flush=True)
        return upstream_response


async def intercept_create_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs — full spec §8.1 flow:

    1. Parse request
    2. Validate service_mode against plan
    3. Check concurrency limit
    4. Accept estimated_duration from frontend, validate against plan max
    5. Compute execution snapshot
    6. Generate create_idempotency_key
    7. Write PostgreSQL job record (quota_state='none')
    8. Forward to upstream Job API
    9. On upstream failure, rollback DB record
    """
    raw_request_body = await request.body()
    try:
        request_data = json.loads(raw_request_body) if raw_request_body else {}
    except Exception:
        request_data = {}

    service_mode = request_data.get("service_mode", "express")
    if service_mode not in ("express", "studio"):
        service_mode = "express"

    # --- User context ---
    user_role = getattr(user, "role", "user") or "user" if user else "user"
    user_plan = getattr(user, "plan_code", "free") or "free" if user else "free"
    is_admin = user_role == "admin"
    # Trial-aware plan gate (P3): if user is in active trial window, elevate
    # capabilities to Plus-tier (Studio, higher duration/concurrency) without
    # changing plan_code. Falls back to PLAN_CATALOG for non-trial users.
    from plan_catalog import get_effective_plan_gate
    plan_info = get_effective_plan_gate(user) if user else PLAN_CATALOG.get("free", PLAN_CATALOG["free"])

    # --- 1. Validate service_mode ---
    if user and not is_admin:
        if service_mode not in plan_info["allowed_service_modes"]:
            return _error_response(
                403, "service_mode_not_allowed",
                f"当前套餐（{user_plan}）不支持{service_mode}模式，请升级套餐。",
                {"plan_code": user_plan, "requested_mode": service_mode,
                 "allowed_modes": plan_info["allowed_service_modes"]},
            )

    # --- 2. Concurrency limit ---
    if user and not is_admin:
        active_count_result = await db.execute(
            select(func.count()).where(
                Job.user_id == user.id,
                Job.status.in_(["queued", "running", "waiting_for_review"]),
            )
        )
        active_count = active_count_result.scalar() or 0
        max_concurrent = plan_info["max_concurrent_jobs"]
        if active_count >= max_concurrent:
            return _error_response(
                409, "concurrent_limit",
                f"已有{active_count}个未完成任务，上限{max_concurrent}个。请先完成或取消。",
                {"active_count": active_count, "max_concurrent": max_concurrent},
            )

    # --- 2b. Free quota check ---
    if user and not is_admin and user_plan == "free":
        has_quota, quota_used, quota_total = await check_quota(db, user)
        if not has_quota:
            return _error_response(
                403, "quota_exhausted",
                f"免费额度已用完（{quota_used}/{quota_total}），请升级套餐。",
                {"free_jobs_quota_used": quota_used, "free_jobs_quota_total": quota_total},
            )

    # --- 3. Validate source ---
    source = request_data.get("source", {})
    source_type = str(source.get("type", "")).strip() if isinstance(source, dict) else ""
    source_value = str(source.get("value", "")).strip() if isinstance(source, dict) else ""
    if not source_type or not source_value:
        return _error_response(
            400, "invalid_source",
            "缺少视频来源信息。",
            {"source_type": source_type or None, "source_value": source_value or None},
        )

    # Normalize frontend "local_file" to the canonical "local_video"
    if source_type == "local_file":
        source_type = "local_video"
        if isinstance(source, dict):
            source["type"] = "local_video"

    # --- 4. Duration: probe (YouTube) or accept frontend estimate ---
    estimated_duration_seconds = request_data.get("estimated_duration_seconds")
    if estimated_duration_seconds is not None:
        try:
            estimated_duration_seconds = float(estimated_duration_seconds)
        except (TypeError, ValueError):
            estimated_duration_seconds = None

    # For YouTube URLs, attempt lightweight yt-dlp probe (5s timeout)
    if source_type == "youtube_url" and estimated_duration_seconds is None:
        probed = _probe_youtube_duration(source_value)
        if probed is not None:
            estimated_duration_seconds = probed
            logger.info("yt-dlp probe: %s → %.0fs", source_value, probed)

    # Duration limit check (if we have an estimate)
    if user and not is_admin and estimated_duration_seconds is not None:
        max_minutes = plan_info["max_duration_minutes"]
        estimated_minutes = estimated_duration_seconds / 60
        if estimated_minutes > max_minutes:
            return _error_response(
                403, "duration_limit",
                f"视频预估时长{estimated_minutes:.0f}分钟，超出套餐上限{max_minutes}分钟。",
                {"estimated_minutes": round(estimated_minutes, 1),
                 "max_minutes": max_minutes, "plan_code": user_plan},
            )

    # --- 5. Compute execution snapshot ---
    policy = compute_job_policy(user, service_mode) if user else {}

    # --- 5. Idempotency key ---
    idempotency_key = request_data.get("create_idempotency_key") or str(_uuid.uuid4())

    # Inject policy + snapshot fields into upstream request
    if policy:
        request_data.update(policy)
    request_data["estimated_duration_seconds"] = estimated_duration_seconds
    request_data["quota_state"] = "none"
    request_data["create_idempotency_key"] = idempotency_key
    # Inject user_id so Job API can build user-isolated workspace_dir
    if user is not None:
        request_data["user_id"] = str(user.id)

    # Forward to upstream with modified body
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
        override_body=json.dumps(request_data, ensure_ascii=False).encode("utf-8"),
    )

    # --- 6. Record in PostgreSQL ---
    job_id = None
    logger.info("intercept_create_job: upstream status=%s user=%s",
                upstream_response.status_code, user.id if user else None)
    if upstream_response.status_code in (200, 201, 202) and user is not None:
        try:
            raw_body = upstream_response.body
            data = json.loads(raw_body)
            job_data = data.get("job") or data
            job_id = job_data.get("job_id")
            if job_id:
                existing = await db.execute(select(Job).where(Job.job_id == job_id))
                if existing.scalar_one_or_none() is None:
                    job = Job(
                        job_id=job_id,
                        user_id=user.id,
                        source_type=job_data.get("source_type", "youtube_url"),
                        source_ref=job_data.get("youtube_url") or job_data.get("source_ref", ""),
                        title=job_data.get("title", ""),
                        speakers=job_data.get("speakers", "auto"),
                        status=job_data.get("status", "queued"),
                        current_stage=job_data.get("current_stage"),
                        project_dir=job_data.get("project_dir"),
                        # --- Full execution snapshot ---
                        service_mode=policy.get("service_mode"),
                        tts_provider=policy.get("tts_provider"),
                        tts_model=policy.get("tts_model"),
                        requires_review=policy.get("requires_review"),
                        voice_clone_enabled=policy.get("voice_clone_enabled"),
                        voice_strategy=policy.get("voice_strategy"),
                        plan_code_snapshot=policy.get("plan_code_snapshot"),
                        role_snapshot=policy.get("role_snapshot"),
                        estimated_duration_seconds=estimated_duration_seconds,
                        source_duration_seconds=None,
                        quota_cost=1,
                        quota_state="none",
                        create_idempotency_key=idempotency_key,
                    )
                    db.add(job)
                    # Reserve quota in the same transaction
                    reserved = await reserve_quota(db, user.id, job)
                    if not reserved and user_plan == "free":
                        # Quota reservation failed — rollback local record
                        await db.rollback()
                        # Compensate: cancel upstream job to prevent orphan
                        await _compensate_upstream_job(job_id)
                        return _error_response(
                            403, "quota_exhausted",
                            "免费额度已用完，无法创建任务。",
                            {"job_id": job_id},
                        )
                    await db.commit()
                    logger.info("Job %s recorded (mode=%s, plan=%s, quota=%s)",
                                job_id, service_mode, user_plan, job.quota_state)
                else:
                    logger.info("Job %s already in DB, skipping", job_id)
            else:
                logger.warning("No job_id in upstream response")
        except Exception as exc:
            logger.exception("Failed to record job %s in DB: %s", job_id, exc)
            try:
                await db.rollback()
            except Exception:
                pass

    # Wrap upstream conflict/error into structured error
    if upstream_response.status_code == 409:
        try:
            err_body = json.loads(upstream_response.body)
            err_msg = err_body.get("error", "任务冲突")
        except Exception:
            err_msg = "任务创建冲突"
        return _error_response(409, "job_create_conflict", err_msg)

    return upstream_response


async def intercept_get_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs/{job_id} — verify ownership, then forward. No auto-claim."""
    await _verify_job_ownership(job_id, db, user)
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def intercept_job_subresource(
    request: Request,
    job_id: str,
    subpath: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET/POST /job-api/jobs/{job_id}/{subpath} — verify ownership, then forward.

    Covers: logs, artifacts, result-summary, continue, review/*, download/*, etc.
    """
    await _verify_job_ownership(job_id, db, user)
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def intercept_delete_job_v2(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """DELETE /job-api/jobs/{job_id} — verify ownership, forward to Job API, then cleanup quota + PostgreSQL.

    Phase 3: replaces the old POST /api/job/delete flow for active callers.
    """
    await _verify_job_ownership(job_id, db, user)

    # Forward DELETE to Job API
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If upstream succeeded, release quota then remove from PostgreSQL
    if upstream_response.status_code == 200:
        try:
            result = await db.execute(select(Job).where(Job.job_id == job_id))
            job_row = result.scalar_one_or_none()
            if job_row is not None:
                from quota import release_quota as _release_quota
                await _release_quota(db, job_row)
            await db.execute(delete(Job).where(Job.job_id == job_id))
            await db.commit()
            logger.info("Deleted job %s from PostgreSQL (quota released)", job_id)
        except Exception:
            logger.exception("Failed to delete job %s from PostgreSQL", job_id)

    return upstream_response


async def _verify_job_ownership(
    job_id: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Check that authenticated user owns the job. Raises 403 if not."""
    if not settings.auth_required or user is None:
        return
    result = await db.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
    )
    if result.scalar_one_or_none() is None:
        result2 = await db.execute(select(Job).where(Job.job_id == job_id))
        if result2.scalar_one_or_none() is not None:
            raise HTTPException(status_code=403, detail="无权访问此任务")
        else:
            logger.warning("Job %s not found in DB — allowing access (legacy job?)", job_id)


async def update_source_metadata(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /job-api/jobs/{job_id}/source-metadata — internal callback from Pipeline S0.

    Allows the pipeline to report actual source_duration_seconds after download.
    Gateway uses this to update the PostgreSQL record.
    """
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    actual_duration = data.get("source_duration_seconds")
    title = data.get("title")

    if actual_duration is None and title is None:
        return Response(
            content=json.dumps({"error": "no_update_fields", "message": "至少提供 source_duration_seconds 或 title"}),
            status_code=400,
            headers={"content-type": "application/json"},
        )

    result = await db.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        return Response(
            content=json.dumps({"ok": True, "note": "job not in gateway DB, skipped"}),
            status_code=200,
            headers={"content-type": "application/json"},
        )

    if actual_duration is not None:
        try:
            job.source_duration_seconds = float(actual_duration)
        except (TypeError, ValueError):
            pass
    if title is not None:
        job.title = str(title)[:512]

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    logger.info("source-metadata updated for %s: duration=%s title=%s",
                job_id, actual_duration, title)
    return Response(
        content=json.dumps({"ok": True}),
        status_code=200,
        headers={"content-type": "application/json"},
    )

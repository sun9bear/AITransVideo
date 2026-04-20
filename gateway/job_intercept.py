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
import sys
import uuid as _uuid
from pathlib import Path

# Make src/ importable so we can reuse services.jobs.logs_redactor (D25).
# Mirrors the pattern in admin_settings.py — local dev vs Docker container.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

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
from credits_service import (
    ensure_free_bucket, estimate_credits,
    shadow_reserve, shadow_release, shadow_capture, shadow_safe,
)


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
# ``PLAN_CATALOG`` name is a frozen import-time snapshot preserved for backward-compatible
# test imports. Request-time code calls the live functions directly.
from plan_catalog import get_legacy_plan_gate_dict  # noqa: E402

PLAN_CATALOG = get_legacy_plan_gate_dict()


# Gateway-local allowed TTS providers (no cross-layer import from tts_strategy)
_VALID_EXPRESS_PROVIDERS = {"cosyvoice", "mimo", "volcengine"}
_VALID_STUDIO_PROVIDERS = {"minimax", "mimo", "volcengine", "cosyvoice"}
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
            # V3-6: single authoritative quality_tier source.
            # Current state: all jobs are "standard". When multi-tier
            # is productized, this is the one place to change.
            "quality_tier": "standard",
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
            "quality_tier": "standard",
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
                        # B-fix: mirror project_dir from upstream once the pipeline
                        # assigns it. The creation-time write at intercept_create_job
                        # always gets None (pipeline hasn't run yet when the Gateway
                        # mirror row is first inserted), so everything downstream
                        # that reads Job.project_dir (background_task_api,
                        # materials_api) would 404 without this backfill. Only
                        # write if upstream reports a value — don't clobber a good
                        # DB value with a transient upstream omission.
                        upstream_project_dir = upstream_job.get("project_dir")
                        if upstream_project_dir:
                            db_job.project_dir = upstream_project_dir
                        # Settle quota when transitioning to terminal status
                        if upstream_status in TERMINAL_STATUSES and old_status not in TERMINAL_STATUSES:
                            await settle_job_quota(db, db_job, upstream_status)
                            # V3-1 shadow settle (best-effort)
                            try:
                                if upstream_status == "succeeded":
                                    actual_min = None
                                    src_dur = getattr(db_job, "source_duration_seconds", None)
                                    if src_dur:
                                        actual_min = src_dur / 60.0
                                    db_job.actual_minutes = actual_min
                                    # Read quality_tier from saved snapshot (single truth source)
                                    _saved_tier = (db_job.metering_snapshot or {}).get("quality_tier", "standard")
                                    shadow_credits = estimate_credits(
                                        actual_min or db_job.estimated_minutes,
                                        service_mode=db_job.service_mode or "express",
                                        quality_tier=_saved_tier,
                                    )
                                    snap = db_job.metering_snapshot or {}
                                    snap["credits_actual"] = shadow_credits
                                    db_job.metering_snapshot = snap
                                    await shadow_safe(
                                        shadow_capture,
                                        db, user_id=db_job.user_id, job_id=jid,
                                        actual_credits=shadow_credits,
                                        service_mode=db_job.service_mode or "express",
                                    )
                                else:
                                    # failed / cancelled → release
                                    await shadow_safe(
                                        shadow_release,
                                        db, user_id=db_job.user_id, job_id=jid,
                                    )
                            except Exception as _se:
                                logger.warning("V3 shadow settle failed for %s: %s", jid, _se)
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
    plan_info = get_effective_plan_gate(user) if user else get_legacy_plan_gate_dict().get("free", {})

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
                # Concurrency limit counts any "active" job. editing is active
                # (user holds a paused editing session) and must count — see
                # docs/plans/2026-04-18-studio-post-edit-plan.md §4.3.
                Job.status.in_(["queued", "running", "waiting_for_review", "editing"]),
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

                    # --- V3-0/V3-1: Shadow metering + reserve (best-effort) ---
                    try:
                        est_min = (estimated_duration_seconds / 60.0) if estimated_duration_seconds else None
                        job.estimated_minutes = est_min
                        # Shadow reserve: consume quality_tier from policy (single truth source)
                        _quality_tier = policy.get("quality_tier", "standard")
                        # Always write basic metering snapshot (even if duration unknown)
                        shadow_credits = estimate_credits(
                            est_min, service_mode=service_mode, quality_tier=_quality_tier,
                        )
                        job.metering_snapshot = {
                            "credits_estimated": shadow_credits if shadow_credits > 0 else None,
                            "service_mode": service_mode,
                            "quality_tier": _quality_tier,
                            "tts_provider": policy.get("tts_provider"),
                            "tts_model": policy.get("tts_model"),
                        }
                        if shadow_credits > 0:
                            await shadow_safe(
                                shadow_reserve,
                                db, user_id=user.id, job_id=job_id,
                                estimated_credits=shadow_credits,
                                service_mode=service_mode,
                            )
                        await db.commit()
                    except Exception as _shadow_exc:
                        logger.warning("V3 shadow metering failed for job %s: %s (non-fatal)", job_id, _shadow_exc)
                        try:
                            await db.rollback()
                        except Exception:
                            pass
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

    # --- D25 server-side log redaction for non-admin users ---
    # Intercepts GET /logs BEFORE the generic proxy. Admins pass through
    # unchanged. Non-admins get events[].message + lines[] filtered through
    # the registry-aware redactor so provider names / UUIDs / internal IDs
    # are stripped. Frontend's ``isAdmin`` hide-LogViewer UI is only cosmetic;
    # this is the authoritative enforcement point.
    if subpath == "logs" and request.method == "GET":
        return await _serve_redacted_logs(request, user)

    # T2: state-transition endpoints need concurrency control at Gateway layer.
    # For POST /continue we hold a row lock across the upstream proxy call so
    # that (a) concurrent continues serialize at the DB, and (b) we only
    # promote status to 'running' AFTER we see upstream accepted the continue.
    # If upstream returns 409 / 5xx / times out, the row stays in
    # 'waiting_for_review' — the user can retry without being blocked by a
    # stale 'running' we wrote speculatively.
    if subpath == "continue" and request.method == "POST":
        return await _continue_with_gateway_lock(request, job_id, db)

    # V3-6 fix (2026-04-14): voice-selection/approve 要同步把用户选择的
    # MiniMax 音质档（turbo=高级/hd=旗舰）写回 Gateway DB 的
    # Job.tts_model 和 Job.metering_snapshot.quality_tier。
    # 否则 UI 显示 30/50 点/分钟，但 settle 时读到硬编码 standard=15
    # 就永远按最低档扣点，定价完全失效。
    if subpath == "review/voice-selection/approve" and request.method == "POST":
        return await _approve_voice_selection_with_quality_sync(request, job_id, db)

    # --- Studio post-edit endpoints (plan 2026-04-18 D29) ---
    # Two groups, both gated on the feature flag:
    #   1. State transitions (enter-edit / editing/cancel / editing/commit)
    #      get a FOR UPDATE row lock + conditional Gateway-DB sync.
    #   2. Segment mutations (segments/{sid}/update | /status) are editing-
    #      state job-scoped; no row lock is needed (upstream validates the
    #      editing state and refreshes editing_touched_at). Feature flag
    #      still gates to keep the surface fully dark when disabled.
    if request.method == "POST" and _is_post_edit_mutation_subpath(subpath):
        if not settings.enable_post_edit:
            # D29: refuse at HTTP level so probes can't distinguish "feature
            # disabled" from "endpoint unknown". Frontend learns flag state
            # via entitlements and doesn't expose the call when off.
            return _error_response(
                404,
                "post_edit_disabled",
                "Post-edit workflow is not enabled on this deployment.",
            )
        if subpath in _POST_EDIT_TRANSITION_SUBPATHS:
            return await _editing_transition_with_lock(request, job_id, db, subpath=subpath)
        # Segments mutation: proxy without DB lock. Upstream handles state check.
        return await proxy_request(
            request=request,
            upstream_base=settings.job_api_upstream,
            strip_prefix="/job-api",
        )

    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def _continue_with_gateway_lock(
    request: Request,
    job_id: str,
    db: AsyncSession,
) -> Response:
    """Acquire FOR UPDATE on the Job row, proxy /continue upstream, then
    commit the status transition only if upstream accepted.

    Flow:
      1. SELECT ... FOR UPDATE on Job row (serializes concurrent continues).
         Legacy jobs without a Gateway row skip the lock entirely; upstream
         handles validation for them.
      2. Assert status == 'waiting_for_review'. If not, raise 409 without
         proxying — another continue already committed, or the job isn't
         actually waiting.
      3. Proxy upstream. Lock is still held because we haven't committed yet.
         Concurrent requests block on FOR UPDATE until this function returns.
      4. If upstream returned a 2xx, promote status to 'running' so the next
         request (which will block on the lock and then read fresh state)
         correctly rejects with 409.
      5. If upstream returned a non-2xx, leave status alone — waiting_for_review
         stays, so the user can retry continue without first waiting for
         list_jobs to reconcile.
      6. Commit (releases the lock regardless of upstream outcome).

    Trade-off: the DB row lock is held through the proxy call (typically
    sub-second for /continue). /continue is an infrequent endpoint so this
    is an acceptable cost for correctness. If the proxy hangs, the lock
    holds until that request times out — the failure mode here is isolated
    to that single job_id's continue retries, not system-wide.
    """
    result = await db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is not None and job.status != "waiting_for_review":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not continuable (current status: {job.status})",
        )

    # Lock is held; proxy upstream.
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # Only promote status on upstream success. If upstream rejected (e.g.
    # review not actually approved per service.py:155-168) or blew up, we
    # leave the row in 'waiting_for_review' so retries work.
    if job is not None and 200 <= response.status_code < 300:
        job.status = "running"

    # Commit either way — releases the FOR UPDATE lock. A no-op commit is
    # cheap; the important thing is that no future request is blocked
    # waiting on this txn.
    await db.commit()
    return response


def _aggregate_quality_tier_from_speakers(
    speakers: list[dict],
) -> tuple[str, str | None]:
    """Aggregate per-speaker UI choices into a job-level (quality_tier, tts_model).

    Rules:
    - 任一 minimax speaker 选了 hd → ("flagship", "speech-2.8-hd")
    - 有 minimax speaker 但全部是 turbo → ("high", "speech-2.8-turbo")
    - 完全没有 minimax speaker → ("standard", None)  ← 保留原 tts_model

    This matches the UI pricing display in VoiceSelectionPanel.tsx:681/688
    (cpm.minimax_turbo=30pts/min from studio.high; cpm.minimax_hd=50pts/min
    from studio.flagship). Jobs using only CosyVoice/VolcEngine stay at
    studio.standard=15pts/min which is the frontend-advertised price for
    those providers (voice_selection_api.py:241-242).
    """
    any_minimax = False
    any_hd = False
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        provider = str(sp.get("tts_provider", "")).strip().lower()
        if provider == "minimax":
            any_minimax = True
            model_hint = str(sp.get("minimax_model", "") or "").strip().lower()
            if model_hint == "hd":
                any_hd = True

    if any_minimax and any_hd:
        return ("flagship", "speech-2.8-hd")
    if any_minimax:
        return ("high", "speech-2.8-turbo")
    return ("standard", None)


async def _approve_voice_selection_with_quality_sync(
    request: Request,
    job_id: str,
    db: AsyncSession,
) -> Response:
    """Intercept POST /review/voice-selection/approve to sync quality_tier + tts_model.

    Flow:
    1. Read and parse the request body to extract per-speaker minimax_model.
    2. Forward the body unchanged to the upstream Job API (which writes
       review_state.json for the pipeline).
    3. If upstream returns 2xx, update Gateway DB:
       - Job.tts_model (consumed by TTS generator at S4)
       - Job.metering_snapshot.quality_tier (consumed by settle at capture)
       - Job.metering_snapshot.per_speaker_provider (for provider-breakdown
         audit; tracks the actual per-speaker provider mix)
    4. Commit. If DB update fails, log but do NOT roll back upstream —
       upstream already wrote review_state.json and the pipeline will run
       with the (possibly stale) quality_tier; this is shadow-safe.

    If upstream returns non-2xx, body is returned verbatim and no DB update.
    """
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        payload = {}

    speakers = payload.get("speakers") if isinstance(payload, dict) else None
    if not isinstance(speakers, list):
        speakers = []

    # Forward upstream with the ORIGINAL body unchanged. Upstream doesn't
    # know about `minimax_model` but doesn't mind extra fields either.
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
        override_body=body_bytes,
    )

    if not (200 <= response.status_code < 300):
        return response

    # Aggregate job-level quality_tier + tts_model from per-speaker choices.
    tier, model = _aggregate_quality_tier_from_speakers(speakers)

    try:
        result = await db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            logger.info("voice-selection/approve: job %s not in Gateway DB, skip sync", job_id)
            return response

        snap = dict(job.metering_snapshot or {})
        snap["quality_tier"] = tier
        # Record per-speaker provider/model mix for audit (provider-breakdown
        # today only shows job-default provider; this future-proofs the
        # execution-provider view without requiring a schema change).
        per_speaker_mix = []
        for sp in speakers:
            if not isinstance(sp, dict):
                continue
            per_speaker_mix.append({
                "speaker_id": str(sp.get("speaker_id", "")),
                "tts_provider": str(sp.get("tts_provider", "")),
                "minimax_model": str(sp.get("minimax_model", "") or "") or None,
            })
        if per_speaker_mix:
            snap["per_speaker_provider"] = per_speaker_mix
        job.metering_snapshot = snap

        # Only overwrite tts_model when a minimax speaker explicitly chose
        # turbo/hd. For jobs using only CosyVoice/VolcEngine, keep whatever
        # job_intercept.py:120 wrote at create time.
        if model is not None:
            job.tts_model = model

        await db.commit()
        logger.info(
            "voice-selection/approve: job=%s tier=%s tts_model=%s speakers=%d",
            job_id, tier, model or job.tts_model, len(speakers),
        )
    except Exception as exc:
        logger.warning(
            "voice-selection/approve: DB sync failed for %s: %s (non-fatal, upstream already accepted)",
            job_id, exc,
        )
        await db.rollback()

    return response


async def _serve_redacted_logs(
    request: Request,
    user: User | None,
) -> Response:
    """Proxy GET /logs and, for non-admin users, strip sensitive fragments
    from ``events[].message`` and ``lines[]`` before returning.

    Failure modes:
    - Upstream non-200: return verbatim (nothing to redact).
    - Response body is not valid JSON or not the expected shape: return
      verbatim — we prefer "fail open" on unexpected schema changes rather
      than 500'ing the logs endpoint.
    """
    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )
    if response.status_code != 200:
        return response

    role = (getattr(user, "role", None) or "user") if user is not None else "user"
    if role == "admin":
        return response

    try:
        body = json.loads(response.body.decode("utf-8"))
    except Exception:
        logger.warning("redacted_logs: upstream response was not JSON; returning verbatim")
        return response

    if not isinstance(body, dict):
        return response

    try:
        from services.jobs.logs_redactor import build_default_redactor

        redactor = build_default_redactor()
    except Exception:
        logger.exception("redacted_logs: failed to build redactor; returning verbatim")
        return response

    events = body.get("events")
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict):
                msg = ev.get("message")
                if isinstance(msg, str) and msg:
                    ev["message"] = redactor.redact(msg)

    lines = body.get("lines")
    if isinstance(lines, list):
        body["lines"] = [
            redactor.redact(ln) if isinstance(ln, str) else ln
            for ln in lines
        ]

    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=200,
        media_type="application/json",
    )


# Set of subpaths that represent editing STATE TRANSITIONS (need FOR UPDATE
# lock + Gateway DB status sync). Segments mutations are covered by the
# broader _is_post_edit_mutation_subpath check below.
_POST_EDIT_TRANSITION_SUBPATHS: frozenset[str] = frozenset({
    "enter-edit",
    "editing/cancel",
    "editing/commit",
})

# Direct post-edit mutation subpaths (no templating). Union with the
# per-segment action allowlist in ``_is_post_edit_mutation_subpath``.
_POST_EDIT_SIMPLE_MUTATION_SUBPATHS: frozenset[str] = frozenset({
    "regenerate-all-tts",   # T1-6 batch
    "editing/voice-map",    # T1-6 set/clear voice override (POST only)
})


def _is_post_edit_mutation_subpath(subpath: str) -> bool:
    """Decide whether a job subresource subpath belongs to the post-edit
    surface (both state transitions and segment mutations). Used only for
    the feature flag gate + lock dispatch; ownership is verified separately
    for every subpath via ``_verify_job_ownership``."""
    if subpath in _POST_EDIT_TRANSITION_SUBPATHS:
        return True
    if subpath in _POST_EDIT_SIMPLE_MUTATION_SUBPATHS:
        return True
    parts = subpath.split("/")
    # segments/{sid}/{action} where action ∈ {update, status, regenerate-tts,
    # accept-draft, discard-draft}. Kept as an explicit allowlist rather than
    # "any segments/*" so that future non-post-edit segment actions are not
    # silently gated off when the flag is disabled.
    if (
        len(parts) == 3
        and parts[0] == "segments"
        and parts[2] in {"update", "status", "regenerate-tts", "accept-draft", "discard-draft"}
    ):
        return True
    return False


async def _editing_transition_with_lock(
    request: Request,
    job_id: str,
    db: AsyncSession,
    *,
    subpath: str,
) -> Response:
    """FOR UPDATE lock + pre-condition check + proxy + conditional DB sync.

    Per-subpath behaviour:

    - ``enter-edit``     : expect status='succeeded'; on upstream 2xx set
      status='editing' + editing_touched_at=now.
    - ``editing/cancel`` : expect status='editing'; on upstream 2xx set
      status='succeeded' + editing_touched_at=NULL.
    - ``editing/commit`` : expect status='editing'. Upstream T1-9 returns
      200 with a dict whose shape depends on strategy:
        overwrite     → {strategy, job_id, edit_generation, ...}
                        Gateway flips source row to running + bumps
                        edit_generation + clears editing_touched_at +
                        stamps current_stage='alignment'.
        copy_as_new   → {strategy, source_job_id, new_job_id,
                         new_project_dir, new_display_name, ...}
                        Gateway:
                          1. Resets source row: status='succeeded',
                             editing_touched_at=NULL (Phase B mirror).
                          2. INSERTs a new Jobs row carrying most fields
                             from source + new IDs + copy lineage +
                             expires_at computed via the same rule the
                             Job-API store uses.

    Legacy jobs without a Gateway row skip the lock (same as ``continue``);
    upstream handles validation for them.
    """
    expected_status_by_subpath = {
        "enter-edit": "succeeded",
        "editing/cancel": "editing",
        "editing/commit": "editing",
    }
    expected = expected_status_by_subpath[subpath]

    result = await db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is not None and job.status != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is not in the expected state for {subpath!r}: "
                f"expected {expected!r}, got {job.status!r}"
            ),
        )

    response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    if job is not None and 200 <= response.status_code < 300:
        from datetime import datetime, timezone as _tz, timedelta as _td

        now_utc = datetime.now(_tz.utc)
        if subpath == "enter-edit":
            job.status = "editing"
            job.editing_touched_at = now_utc
        elif subpath == "editing/cancel":
            job.status = "succeeded"
            job.editing_touched_at = None
        elif subpath == "editing/commit":
            await _apply_editing_commit_gateway_side(
                db, job, response, now_utc=now_utc,
            )

    await db.commit()
    return response


async def _apply_editing_commit_gateway_side(
    db: AsyncSession,
    source_job: Job,
    upstream_response: Response,
    *,
    now_utc,
) -> None:
    """After Job-API's editing/commit returns 2xx, sync Gateway DB.

    Reads the upstream body to decide which strategy was executed:

    - overwrite: promote source row status → running, edit_generation += 1,
      editing_touched_at cleared, current_stage stamped 'alignment'. Same
      row is re-used; no INSERT.
    - copy_as_new: reset source → succeeded (Phase B mirror) + INSERT a
      fresh Jobs row for the copy with lineage fields populated.

    Failure modes are soft: if parse / INSERT fails we log prominently but
    do not revert the upstream response — it already succeeded at Job-API
    layer, and flipping the source back would create a messier state. An
    admin can reconcile via list_jobs / PG direct edit.
    """
    from datetime import timedelta as _td

    try:
        body = json.loads(upstream_response.body.decode("utf-8"))
    except Exception:
        logger.warning(
            "editing/commit gateway-side: upstream body not JSON; skipping sync"
        )
        return

    strategy = body.get("strategy")
    if strategy == "overwrite":
        source_job.status = "running"
        source_job.current_stage = "alignment"
        source_job.edit_generation = (source_job.edit_generation or 0) + 1
        source_job.editing_touched_at = None
        return

    if strategy != "copy_as_new":
        logger.info(
            "editing/commit gateway-side: unknown strategy=%r; no DB mutation",
            strategy,
        )
        return

    # copy_as_new Phase B mirror
    new_job_id = str(body.get("new_job_id") or "").strip()
    new_display_name = str(body.get("new_display_name") or "").strip()
    new_project_dir = body.get("new_project_dir")
    if not new_job_id:
        logger.warning(
            "editing/commit copy_as_new: upstream response missing new_job_id; "
            "source job will still be reset to succeeded but new row will NOT "
            "be inserted into Gateway DB — admin must reconcile"
        )
    # Reset source row (Phase B)
    source_job.status = "succeeded"
    source_job.editing_touched_at = None

    if not new_job_id:
        return

    # Idempotency: if a prior run already inserted this row (retry), skip
    existing = await db.execute(
        select(Job).where(Job.job_id == new_job_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info(
            "editing/commit copy_as_new: job_id=%s already in Gateway DB; skipping INSERT",
            new_job_id,
        )
        return

    # TTL for the copy — plan §5.1 simplified form:
    #   min(now + 7d, most_recent_live_sibling.expires_at + 24h)
    # We scope by (user_id, root_job_id). If no live sibling exists,
    # fall back to now + 7d (same as first-copy rule).
    seven_days_later = now_utc + _td(days=7)
    source_root_id = source_job.root_job_id or source_job.job_id
    sibling_q = await db.execute(
        select(Job.expires_at)
        .where(
            Job.user_id == source_job.user_id,
            Job.root_job_id == source_root_id,
            Job.expires_at.isnot(None),
            Job.expires_at > now_utc,
            Job.job_id != source_job.job_id,
        )
        .order_by(Job.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    sibling_expires = sibling_q.scalar_one_or_none()
    if sibling_expires is not None:
        copy_expires = min(seven_days_later, sibling_expires + _td(hours=24))
    else:
        copy_expires = seven_days_later

    copy_row = Job(
        job_id=new_job_id,
        user_id=source_job.user_id,
        source_type=source_job.source_type,
        source_ref=source_job.source_ref,
        title=source_job.title,
        speakers=source_job.speakers,
        status="running",  # runner has already accepted the new job
        current_stage="alignment",
        project_dir=str(new_project_dir) if new_project_dir else None,
        review_gate=None,
        error_summary=None,
        service_mode=source_job.service_mode,
        tts_provider=source_job.tts_provider,
        tts_model=source_job.tts_model,
        requires_review=source_job.requires_review,
        voice_clone_enabled=source_job.voice_clone_enabled,
        voice_strategy=source_job.voice_strategy,
        plan_code_snapshot=source_job.plan_code_snapshot,
        role_snapshot=source_job.role_snapshot,
        source_duration_seconds=source_job.source_duration_seconds,
        quota_cost=0,
        quota_state="none",
        estimated_duration_seconds=source_job.estimated_duration_seconds,
        create_idempotency_key=None,
        # Post-edit lineage
        display_name=new_display_name or None,
        expires_at=copy_expires,
        editing_touched_at=None,
        copy_of_job_id=source_job.job_id,
        root_job_id=source_root_id,
        edit_generation=0,
        source_content_hash=source_job.source_content_hash,
    )
    db.add(copy_row)
    logger.info(
        "editing/commit copy_as_new: mirrored new job %s → Gateway DB (copy_of=%s, root=%s)",
        new_job_id, source_job.job_id, source_root_id,
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
            dur_float = float(actual_duration)
            job.source_duration_seconds = dur_float
            # V3-0: write actual source duration to actual_minutes.
            # estimated_minutes is preserved as the original pre-download estimate
            # so we can later compare estimate vs. actual for calibration.
            job.actual_minutes = dur_float / 60.0

            # V3 fix: if create-time had no estimated_duration, do late shadow reserve now
            # Idempotency: check ledger for existing reserve before doing another one
            snap = job.metering_snapshot or {}
            if dur_float > 0:
                try:
                    from models import CreditsLedger
                    existing_reserve = await db.execute(
                        select(CreditsLedger).where(
                            CreditsLedger.related_job_id == job_id,
                            CreditsLedger.direction == "reserve",
                        ).limit(1)
                    )
                    already_reserved = existing_reserve.scalar_one_or_none() is not None

                    if not already_reserved:
                        _quality_tier = snap.get("quality_tier", "standard")
                        _svc_mode = snap.get("service_mode") or job.service_mode or "express"
                        late_credits = estimate_credits(
                            dur_float / 60.0, service_mode=_svc_mode, quality_tier=_quality_tier,
                        )
                        if late_credits > 0:
                            snap["credits_estimated"] = late_credits
                            job.metering_snapshot = dict(snap)
                            await shadow_safe(ensure_free_bucket, db, job.user_id)
                            await shadow_safe(
                                shadow_reserve,
                                db, user_id=job.user_id, job_id=job_id,
                                estimated_credits=late_credits,
                                service_mode=_svc_mode,
                            )
                            logger.info("V3 late shadow reserve for %s: %d credits", job_id, late_credits)
                except Exception as _e:
                    logger.warning("V3 late shadow reserve failed for %s: %s (non-fatal)", job_id, _e)
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


async def update_job_metering(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /job-api/jobs/{job_id}/metering — internal callback from Pipeline.

    Allows the pipeline to report metering fields after TTS/alignment completion:
    - final_cn_chars: total Chinese characters in final translation
    - rewrite_triggered: whether any segment was rewritten
    - rewrite_count: total rewrite operations performed
    - tts_billed_chars: total characters sent to TTS provider

    These fields are merged into Job.metering_snapshot (JSONB).
    Best-effort: failures do not affect job status.
    """
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}

    if not data:
        return Response(
            content=json.dumps({"error": "empty_body", "message": "请提供 metering 字段"}),
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

    # Merge incoming fields into existing metering_snapshot
    snapshot = dict(job.metering_snapshot or {})
    allowed_keys = {
        # V3-4 baseline
        "final_cn_chars", "rewrite_triggered", "rewrite_count",
        # V3-5 partial
        "tts_billed_chars",
        # Phase 2 Task 0 — translation-duration-alignment metrics
        "total_segments",
        "catalog_hit_count", "catalog_hit_rate", "skip_probe",
        "needs_review_count", "needs_review_rate",
        "alignment_method_distribution", "speed_param_distribution",
        "first_pass_error_pct_avg", "first_pass_error_pct_p50",
        "first_pass_error_pct_p90", "first_pass_error_pct_n",
        "glossary_total_terms", "glossary_preserved_terms",
        "term_preservation_rate", "missing_glossary_terms",
    }
    updated_keys = []
    for key in allowed_keys:
        if key in data:
            snapshot[key] = data[key]
            updated_keys.append(key)

    if not updated_keys:
        return Response(
            content=json.dumps({"ok": True, "note": "no recognized metering keys"}),
            status_code=200,
            headers={"content-type": "application/json"},
        )

    job.metering_snapshot = snapshot

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    logger.info("metering updated for %s: %s", job_id, updated_keys)
    return Response(
        content=json.dumps({"ok": True}),
        status_code=200,
        headers={"content-type": "application/json"},
    )

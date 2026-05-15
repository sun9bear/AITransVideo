"""AIVideoTrans API Gateway.

Step 1: Transparent proxy to existing services.
Step 2: Auth (register/login/logout) + PostgreSQL.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from admin_settings import router as admin_router
from admin_disk_api import router as admin_disk_router
from admin_cost_api import router as admin_cost_router
from admin_support_api import router as admin_support_router
from pricing_admin import router as pricing_admin_router
from s2_monitor_api import router as s2_monitor_router
from admin_job_monitor_api import router as admin_job_monitor_router
from auth_email import router as auth_email_router
from auth_phone import router as auth_phone_router
from billing import router as billing_router
from cost_management import router as cost_management_router
from credits_observability import router as credits_observability_router
from credits_read import router as credits_read_router
from entitlements import router as entitlements_router
from plan_catalog import router as plan_catalog_router
from subscriptions import router as subscriptions_router
from materials_api import router as materials_router
from background_task_api import router as background_task_router
from voice_catalog_api import (
    router as voice_catalog_router,
    internal_router as voice_catalog_internal_router,
    _require_internal_access,
)
from traffic_analytics import router as traffic_analytics_router
from auth import (
    LoginRequest,
    RegisterRequest,
    bind_email_handler,
    change_password_handler,
    login_handler,
    logout_handler,
    me_handler,
    register_handler,
    require_auth,
)
import logging

from config import settings
from database import engine, init_db
from models import Base
from startup_checks import (
    is_startup_recovery_schema_missing_error,
    validate_internal_api_key,
    validate_production_safety,
    validate_r2_backend,
)

logger = logging.getLogger(__name__)
from job_intercept import (
    intercept_create_job,
    intercept_delete_job_v2,
    intercept_get_job,
    intercept_job_subresource,
    intercept_list_jobs,
    intercept_rename_job,
    intercept_suggested_copy_name,
    update_job_metering,
    update_source_metadata,
)
from proxy import close_client, init_client, proxy_request
from voice_selection_api import get_voice_selection_pricing, voice_clone_for_selection

# Customer support + notifications (plan 2026-05-08)
from support_api import router as support_router
from notifications_api import router as notifications_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup-time validations and init:
    # T6 — refuse prod + no-auth combination. Fail fast BEFORE touching DB
    # so misconfigured deploys surface immediately with a clear message.
    validate_production_safety(settings.env, settings.auth_required)
    # T4 — refuse startup without AVT_INTERNAL_API_KEY. Internal endpoints
    # require this to avoid fail-open misconfiguration. Runs before init_db
    # so the error surfaces before we touch the database.
    validate_internal_api_key(settings.internal_api_key)
    # Phase 2 — verify R2 config consistency. Downgrades to "local" if
    # AVT_DOWNLOAD_REDIRECT_BACKEND=r2 but any R2 credential is missing
    # (logs CRITICAL but does NOT raise — downloads must keep working).
    # The effective value is written back to settings so all request-time
    # code reads one source of truth.
    settings.download_redirect_backend = validate_r2_backend(
        settings.download_redirect_backend,
        settings.r2_endpoint,
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
    )
    # T3 — DB credentials are resolved and engine is built here. Raises if
    # neither AVT_PG_PASSWORD nor AVT_DATABASE_URL is set (no more hardcoded
    # avt:avt fallback).
    init_db()

    # Dev convenience: auto-create tables. In production use Alembic migrations.
    if not settings.auth_required:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    init_client()
    # Recover stale label tasks from previous Gateway crash
    try:
        from label_task_queue import recover_stale_tasks
        from database import async_session
        async with async_session() as db:
            recovered = await recover_stale_tasks(db)
            if recovered:
                logger.info("Recovered %d stale label tasks", recovered)
    except Exception as exc:
        if is_startup_recovery_schema_missing_error(exc):
            logger.warning(
                "Skipped stale label task recovery because the database schema is not ready: %s",
                exc,
            )
        else:
            logger.exception("Failed to recover stale label tasks during gateway startup")

    # Recover stale background tasks (materials_pack / generate_video)
    try:
        import background_task_queue as _bg_queue
        from database import async_session as _async_session
        async with _async_session() as db:
            recovered_bg = await _bg_queue.recover_stale(db)
            if recovered_bg:
                logger.info("Recovered %d stale background tasks", recovered_bg)
    except Exception as exc:
        if is_startup_recovery_schema_missing_error(exc):
            logger.warning(
                "Skipped stale background task recovery because the database schema is not ready: %s",
                exc,
            )
        else:
            logger.exception("Failed to recover stale background tasks during gateway startup")

    # Periodic cleanup of expired materials_pack zips (24h retention, plan
    # 2026-04-21). Disk pressure is the concern — the US host sits at 82%
    # use and each long task produces GB-scale zips that linger without
    # pruning. An asyncio task is enough at current scale; if multi-node
    # gateway ever lands, move this to a dedicated worker.
    import asyncio as _asyncio

    async def _periodic_pack_cleanup() -> None:
        import background_task_queue as _bg_q
        from database import async_session as _session
        # Short initial delay so container startup isn't contending with the
        # first cleanup pass — reduces log noise during rolling restarts.
        await _asyncio.sleep(60)
        while True:
            try:
                async with _session() as db:
                    expired = await _bg_q.cleanup_expired_pack_zips(db)
                    if expired:
                        logger.info(
                            "periodic cleanup: expired %d materials_pack zip(s)",
                            expired,
                        )
            except Exception as exc:
                # Never crash the loop — transient DB hiccups shouldn't
                # kill the scheduler for the rest of the gateway's life.
                logger.warning("periodic pack cleanup failed: %s", exc)
            # Run hourly. Coarse granularity is fine — 24h retention with
            # a 1h sweep worst-case keeps a zip 25h after completion.
            await _asyncio.sleep(3600)

    _cleanup_task = _asyncio.create_task(_periodic_pack_cleanup())
    # Stash on app.state so the lifespan shutdown can cancel cleanly.
    app.state.pack_cleanup_task = _cleanup_task

    # Gateway-side 7d project cleanup (plan 2026-04-21). Closes the
    # "ghost row" gap — the Job API cleanup already rm's project_dir
    # but never touched gateway DB, so expired terminal jobs were
    # accumulating as status=succeeded rows with dead project_dir
    # pointers. This sweeper flips them to status=purged and (if the
    # dir is still there) unlinks it through a path whitelist.
    async def _periodic_project_cleanup() -> None:
        import project_cleanup as _project_cleanup
        from database import async_session as _session
        # Offset relative to pack cleanup so the two sweepers don't
        # contend on the DB at the same moment.
        await _asyncio.sleep(180)
        while True:
            try:
                async with _session() as db:
                    purged = await _project_cleanup.cleanup_expired_projects(db)
                    if purged:
                        logger.info(
                            "periodic project cleanup: purged %d expired job record(s)",
                            purged,
                        )
            except Exception as exc:
                logger.warning("periodic project cleanup failed: %s", exc)
            # Plan 2026-05-07 B6: cleanup runs once per day at 3 AM Beijing
            # (= 19:00 UTC) instead of every 6h. Aligned with off-peak so
            # rmtree IO doesn't compete with users on the playback /
            # editing paths. Job API side (services.web_ui.cleanup) does
            # the same schedule change so the two sweeps still interleave
            # at the same wall-clock moment.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _now_utc = _dt.now(_tz.utc)
            _target = _now_utc.replace(hour=19, minute=0, second=0, microsecond=0)
            if _target <= _now_utc:
                _target = _target + _td(days=1)
            _sleep_s = max(60.0, (_target - _now_utc).total_seconds())
            await _asyncio.sleep(_sleep_s)

    _project_task = _asyncio.create_task(_periodic_project_cleanup())
    app.state.project_cleanup_task = _project_task

    # Plan 2026-05-07 §4.5-4.6: proactive R2 push sweeper. Two feature
    # flags gate this — both must be on for the loop to do anything.
    # With either off, sweeper_loop runs but each tick returns early.
    try:
        from r2_artifact_sweeper import sweeper_loop as _r2_sweeper_loop
        _sweeper_task = _asyncio.create_task(
            _r2_sweeper_loop(), name="r2-artifact-sweeper",
        )
        app.state.r2_artifact_sweeper_task = _sweeper_task
    except Exception:
        # Sweeper failures must never block gateway startup. Per the
        # plan's L4 rollback, a broken sweeper should leave the rest
        # of gateway operational and just stop pushing R2 — exactly
        # the no-op behavior of an unscheduled task.
        logger.exception("Failed to start r2_artifact_sweeper; continuing without proactive push")

    # Seed pricing runtime
    try:
        from pricing_runtime import get_runtime_pricing
        pricing = get_runtime_pricing(force_reload=True)
        logger.info("[pricing] Runtime pricing loaded: version=%d", pricing.version)
    except Exception:
        logger.warning("[pricing] Failed to initialize pricing runtime, using defaults")
    yield
    # Stop periodic cleaner tasks cleanly so shutdown doesn't hang on
    # their asyncio.sleep() inside the loops.
    for attr in ("pack_cleanup_task", "project_cleanup_task", "r2_artifact_sweeper_task"):
        handle = getattr(app.state, attr, None)
        if handle is not None:
            handle.cancel()
            try:
                await handle
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()
    await engine.dispose()


app = FastAPI(
    title="AIVideoTrans Gateway",
    version="0.2.0",
    docs_url="/gateway/docs" if not settings.auth_required else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# reconcile_job_middleware removed (2026-03-26).
# Reason: Had stale cookie/token field names (session_id vs avt_session/token).
# Owner binding is now handled solely by intercept_create_job at job creation time.
# No auto-claim of orphan jobs in list or get endpoints.


# --- Health check ---

@app.get("/gateway/health")
async def health():
    return {"status": "ok", "auth_required": settings.auth_required}


# --- Auth routes ---

app.post("/auth/register")(register_handler)
app.post("/auth/login")(login_handler)
app.post("/auth/logout")(logout_handler)
app.get("/auth/me")(me_handler)
app.post("/api/account/change-password")(change_password_handler)
app.post("/api/account/bind-email")(bind_email_handler)
app.include_router(auth_phone_router)
app.include_router(auth_email_router)
# P1-10b / S-HIGH-2: ``captcha_router`` removed along with the dead
# pre-verify pass-token flow (see auth_phone.py for the rationale).


# --- Admin settings routes (before catch-all) ---

app.include_router(admin_router)
app.include_router(admin_disk_router)
app.include_router(admin_cost_router)
app.include_router(admin_support_router)
app.include_router(pricing_admin_router)
app.include_router(s2_monitor_router)
app.include_router(admin_job_monitor_router)
app.include_router(billing_router)
app.include_router(cost_management_router)
app.include_router(credits_observability_router)
app.include_router(traffic_analytics_router)
app.include_router(credits_read_router)
app.include_router(entitlements_router)
app.include_router(plan_catalog_router)
app.include_router(subscriptions_router)
app.include_router(materials_router)
# Background task router — MUST precede any job-api proxy catch-all; it
# serves /api/jobs/{id}/tasks/* which are Gateway-native (not proxied).
app.include_router(background_task_router)
app.include_router(voice_catalog_router)
app.include_router(voice_catalog_internal_router)

from user_voice_api import router as user_voice_router, internal_router as user_voice_internal_router
app.include_router(user_voice_router)
app.include_router(user_voice_internal_router)

# Customer support API + notification center (plan 2026-05-08)
from notifications_api import internal_router as notifications_internal_router

app.include_router(support_router)
app.include_router(notifications_router)
app.include_router(notifications_internal_router)


# --- Gateway-native upload endpoint (before catch-all) ---

from upload import handle_upload_video


async def _gateway_upload_video(
    request: Request,
    _user: User | None = Depends(require_auth),
) -> Response:
    return await handle_upload_video(request, user=_user)

app.post("/gateway/upload-video")(_gateway_upload_video)

# Rename a job's user-visible display_name (plan §6.5 / D16). Lives on
# the /gateway/* namespace, not /job-api/*, because the collision +
# ownership logic is gateway-level rather than a transparent proxy.
app.patch("/gateway/jobs/{job_id}")(intercept_rename_job)

# Suggested "save as new copy" name for the edit-page modal (plan §6.4 / D17).
# Pure read; the user may edit the suggestion before committing.
app.get("/gateway/jobs/{job_id}/suggested-copy-name")(intercept_suggested_copy_name)


# --- Job API routes ---
# All /job-api/* routes go through intercept functions.
# The catch-all is LAST and uses a different path pattern to avoid
# swallowing the specific routes (FastAPI {path:path} bug).

app.get("/job-api/jobs")(intercept_list_jobs)
app.post("/job-api/jobs")(intercept_create_job)
app.get("/job-api/jobs/{job_id}")(intercept_get_job)
app.delete("/job-api/jobs/{job_id}")(intercept_delete_job_v2)
app.post(
    "/job-api/jobs/{job_id}/source-metadata",
    dependencies=[Depends(_require_internal_access)],
)(update_source_metadata)
app.post(
    "/job-api/jobs/{job_id}/metering",
    dependencies=[Depends(_require_internal_access)],
)(update_job_metering)
app.post("/job-api/jobs/{job_id}/voice-clone")(voice_clone_for_selection)
app.get("/api/voice-selection/pricing")(get_voice_selection_pricing)

# Job sub-resources: logs, artifacts, result-summary, continue, review/*, download/*, etc.
app.api_route(
    "/job-api/jobs/{job_id}/{subpath:path}",
    methods=["GET", "POST"],
)(intercept_job_subresource)


# --- Proxy: Job API catch-all (non-jobs paths only) ---
# NOTE: /job-api/jobs* are handled by intercept functions above.
# This also handles global endpoints like /job-api/voice-library.

@app.api_route(
    "/job-api/{path:path}",
    methods=["GET", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_job_api_other(
    request: Request,
    path: str,
    _user: User | None = Depends(require_auth),
) -> Response:
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
        reload=False,
        log_level="info",
    )

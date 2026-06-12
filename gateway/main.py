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
from admin_cosyvoice_control_api import router as admin_cosyvoice_control_router
from admin_disk_api import router as admin_disk_router
from pan.auth import router as pan_auth_router
from pan.admin_api import router as pan_admin_router
from admin_billing_api import router as admin_billing_router
from admin_cost_api import router as admin_cost_router
from admin_smart_analytics_api import router as admin_smart_analytics_router
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

# Configure root logger early so app-level WARN/INFO emits (pan.*, jobs, etc.)
# actually reach `docker logs`. Without this, modules that do
# ``logger = logging.getLogger(__name__)`` end up propagating to a handler-less
# root → silent drops. Production 2026-05-25: this was why ``_upload_chunk``
# retry warnings were invisible during the Anthropic 4.73 GB failure
# investigation. Uvicorn's own loggers (uvicorn.error, uvicorn.access) have
# their own handlers and are unaffected.
#
# If a handler is already attached (e.g. unit-test harness pre-configured one)
# we leave it alone — basicConfig is a no-op when root already has handlers.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _attach_rotating_file_log() -> None:
    """Attach a RotatingFileHandler to the root logger for persistent on-disk logs.

    Completely fail-safe: any OS / permission failure is printed to stderr and
    swallowed — gateway must never fail to start because the log directory is
    missing (e.g. Windows local dev where the path does not exist).
    """
    import os
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_dir_str = os.environ.get(
        "AIVIDEOTRANS_RUNTIME_LOGS_DIR",
        "/opt/aivideotrans/data/runtime_logs",
    )
    try:
        log_dir = Path(log_dir_str)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gateway.app.log"
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[gateway] WARNING: could not attach rotating file handler "
            f"(dir={log_dir_str!r}): {exc}",
            flush=True,
        )


_attach_rotating_file_log()

from config import settings
from csrf import require_same_origin_state_change
from database import engine, init_db
from models import Base
from startup_checks import (
    is_startup_recovery_schema_missing_error,
    validate_anonymous_preview_config,
    validate_environment_name,
    validate_internal_api_key,
    validate_mainland_voice_worker_config,
    validate_pan_backup_config,
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
from voice_selection_api import (
    get_voice_selection_pricing,
    voice_candidates_for_selection,
    voice_clone_for_selection,
    voice_match_for_selection,
)

# Customer support + notifications (plan 2026-05-08)
from support_api import router as support_router
from notifications_api import router as notifications_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup-time validations and init:
    # Refuse unknown AVT_ENV values so typos do not silently fall back to
    # development semantics in production-sensitive guards.
    settings.env = validate_environment_name(settings.env)
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
    # Pan backup (plan 2026-05-14 T2.2) — refuse startup if
    # AVT_ENABLE_PAN_BACKUP=true but any required OAuth credential or
    # Fernet key is missing/invalid. Fails hard (RuntimeError) so
    # misconfigured deploys surface immediately with a clear message.
    validate_pan_backup_config(settings)
    # Mainland voice worker (plan 2026-05-24 Phase 1.5) — fail-graceful：
    # 配置缺失时 CRITICAL log + 降级 enabled=False，不阻塞 gateway 启动。
    # 主路径不依赖 worker；mainland clone 是子能力。
    settings.mainland_voice_worker_enabled = validate_mainland_voice_worker_config(
        settings.mainland_voice_worker_enabled,
        settings.mainland_voice_worker_url,
        settings.mainland_voice_worker_hmac_key_id,
        settings.mainland_voice_worker_hmac_secret,
    )
    # APF T1 — anonymous preview HMAC secret validation. Downgrades
    # enable_anonymous_preview to False if secret is missing or too short.
    # Fail-graceful: logs CRITICAL + downgrade, does not crash gateway.
    validate_anonymous_preview_config(settings)
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

    # Reconcile + recover background tasks (materials_pack / generate_video
    # / pan_*). Order matters: reconciler runs FIRST so it can re-launch
    # recent pending rows (closing the "process crashed between create_task
    # and asyncio.create_task" gap, CodeX 2026-05-19). The reconciler calls
    # mark_running on each launched row, bumping updated_at past startup_dt
    # so the subsequent recover_stale pass leaves them alone.
    try:
        import background_task_queue as _bg_queue
        import background_task_reconciler as _bg_reconciler
        from database import async_session as _async_session
        # Production 2026-05-19 hotfix: sub-agent wrote ``timezone as _tz_utc``
        # which aliases the timezone CLASS, not the timezone.utc INSTANCE.
        # ``datetime.now(timezone)`` raises ``TypeError: tzinfo argument must be
        # None or of a tzinfo subclass, not type 'type'``. Alias the module to
        # ``_tz`` and call ``.utc`` on it to grab the instance.
        from datetime import datetime as _dt_now, timezone as _tz
        startup_dt = _dt_now.now(_tz.utc)
        async with _async_session() as db:
            stats = await _bg_reconciler.reconcile_pending_tasks(db)
            logger.info(
                "Pending-task reconciler scanned %d rows: launched=%d failed=%d "
                "skipped_duplicate=%d",
                stats["total"], stats["launched"], stats["failed"],
                stats["skipped_duplicate"],
            )
            recovered_bg = await _bg_queue.recover_stale(db, cutoff_dt=startup_dt)
            if recovered_bg:
                logger.info("Recovered %d stale background tasks", recovered_bg)
    except Exception as exc:
        if is_startup_recovery_schema_missing_error(exc):
            logger.warning(
                "Skipped background-task reconcile/recovery because the database schema is not ready: %s",
                exc,
            )
        else:
            logger.exception("Failed to reconcile/recover background tasks during gateway startup")

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

    # Phase 4.3a PR2-D: Express auto-clone reservation TTL sweeper. Reclaims
    # "reserved but never consumed" cap slots whose expires_at has passed
    # (e.g. the process crashed mid-reserve, or the user never started
    # another reserve so the per-reserve inline expire never ran). Pure DB
    # state flip — only calls expire_stale_reservations; never touches
    # user_voices and never calls a paid / external API. Same fail-safe
    # pattern as r2_artifact_sweeper above: a broken sweeper must not block
    # gateway startup. The per-user inline expire inside reserve()
    # (service §4.1 step 2) is the real-time primary defense; this loop is
    # only the background backstop.
    try:
        from express_reservation_sweeper import sweeper_loop as _express_resv_sweeper_loop
        _express_resv_task = _asyncio.create_task(
            _express_resv_sweeper_loop(), name="express-reservation-sweeper",
        )
        app.state.express_reservation_sweeper_task = _express_resv_task
    except Exception:
        logger.exception(
            "Failed to start express_reservation_sweeper; reservation TTL "
            "reclaim falls back to the per-reserve inline expire",
        )

    # Phase 4.3b-C: Express temporary-voice cleanup sweeper. Deletes the
    # DashScope voice for expired temporary cosyvoice clones (paid worker call)
    # then soft-deletes the user_voices row. Distinct from the reservation
    # sweeper above (that one only flips reservation status, never calls the
    # worker). Defaults to DRY-RUN (AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN=true) so
    # it only logs "would delete" until an operator flips it on. worker
    # unavailable -> fail-fast before claiming. Same fail-safe pattern: a broken
    # sweeper must not block gateway startup. migration 033 must be applied for
    # the cleanup_* columns to exist; until then a tick logs and continues.
    try:
        from express_voice_cleanup_sweeper import sweeper_loop as _express_voice_cleanup_loop
        _express_voice_cleanup_task = _asyncio.create_task(
            _express_voice_cleanup_loop(), name="express-voice-cleanup-sweeper",
        )
        app.state.express_voice_cleanup_sweeper_task = _express_voice_cleanup_task
    except Exception:
        logger.exception(
            "Failed to start express_voice_cleanup_sweeper; expired temporary "
            "voices will not be auto-reclaimed (manual CLI cleanup still works)",
        )

    # Phase 8 §T8.4: pan backup background schedulers.
    # 4 loops: archive_scanner (daily 03:30 BJT), token_refresh (6h),
    # orphan_cleanup (Sat 04:00 BJT), stale_reaper (30 min).
    # Same fail-safe pattern as the r2_sweeper above — failure does not
    # block startup, just leaves pan automation off.
    try:
        from pan.scheduler import register_pan_schedulers
        register_pan_schedulers(app)
    except Exception:
        logger.exception(
            "Failed to register pan schedulers; "
            "pan auto-archive / refresh / reap / orphan-cleanup will be OFF",
        )

    # APF P0 T9: Anonymous preview TTL & media cleanup sweeper. Deletes
    # gateway-side upload/teaser files for block/reject records and expired
    # records, appends audit JSONL (no transcription text / raw IP), and
    # prunes stale anonymous_sessions / daily_usage rows. Only started when
    # enable_anonymous_preview is True (feature-flag gate). Same fail-safe
    # pattern as all other sweepers above — startup failure must not block
    # gateway. Must be merged before flag is enabled on any environment.
    try:
        if settings.enable_anonymous_preview:
            from anonymous_preview_sweeper import sweeper_loop as _anon_preview_sweeper_loop
            _anon_preview_sweeper_task = _asyncio.create_task(
                _anon_preview_sweeper_loop(), name="anonymous-preview-sweeper",
            )
            app.state.anonymous_preview_sweeper_task = _anon_preview_sweeper_task
    except Exception:
        logger.exception(
            "Failed to start anonymous_preview_sweeper; anonymous preview media "
            "TTL cleanup will not run (manual cleanup required)",
        )

    # Chunked upload TTL sweeper (plan 2026-06-11 §3.8). Reclaims expired
    # part directories, orphan dirs, and unclaimed ready files (claim 闭环).
    # Deliberately NOT gated on chunked_upload_enabled — disk residue must
    # be reclaimed even while the admin kill-switch is off. Same fail-safe
    # pattern as all other sweepers above.
    try:
        from chunked_upload_sweeper import sweeper_loop as _chunked_upload_sweeper_loop
        _chunked_upload_sweeper_task = _asyncio.create_task(
            _chunked_upload_sweeper_loop(), name="chunked-upload-sweeper",
        )
        app.state.chunked_upload_sweeper_task = _chunked_upload_sweeper_task
    except Exception:
        logger.exception(
            "Failed to start chunked_upload_sweeper; chunked upload TTL "
            "cleanup will not run (manual cleanup required)",
        )

    # 支付对账 sweeper（audit 2026-06-12 P1）：周期扫 created/pending 滞留
    # 订单并主动调 provider query_order 对账，兜住「webhook 丢失/被拒后用户
    # 已付款但权益未发放」的静默故障。结算仍走 billing 单一入口
    # （_refresh_order_from_provider → _process_payment_event 幂等）。
    # Same fail-safe pattern as all other sweepers above.
    try:
        from billing_reconciliation import sweeper_loop as _billing_reconcile_loop
        _billing_reconcile_task = _asyncio.create_task(
            _billing_reconcile_loop(), name="billing-reconciliation-sweeper",
        )
        app.state.billing_reconciliation_sweeper_task = _billing_reconcile_task
    except Exception:
        logger.exception(
            "Failed to start billing_reconciliation sweeper; unsettled orders "
            "will only refresh via user-initiated order polling",
        )

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
    for attr in (
        "pack_cleanup_task",
        "project_cleanup_task",
        "r2_artifact_sweeper_task",
        # Phase 4.3a PR2-D: cancel the reservation TTL sweeper cleanly so
        # shutdown doesn't hang on its asyncio.sleep().
        "express_reservation_sweeper_task",
        # Phase 4.3b-C: cancel the temporary-voice cleanup sweeper on shutdown.
        "express_voice_cleanup_sweeper_task",
        # Phase 8 §T8.4 pan schedulers (CodeX P2-5: previously omitted,
        # causing potential race with engine.dispose() on shutdown).
        "pan_archive_scanner_task",
        "pan_token_refresh_task",
        "pan_orphan_cleanup_task",
        "pan_stale_reaper_task",
        # APF P0 T9: anonymous preview TTL sweeper.
        "anonymous_preview_sweeper_task",
        # Chunked upload TTL sweeper (plan 2026-06-11 §3.8).
        "chunked_upload_sweeper_task",
        # 支付对账 sweeper（audit 2026-06-12 P1）。
        "billing_reconciliation_sweeper_task",
    ):
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

app.post(
    "/auth/register",
    dependencies=[Depends(require_same_origin_state_change)],
)(register_handler)
app.post(
    "/auth/login",
    dependencies=[Depends(require_same_origin_state_change)],
)(login_handler)
app.post(
    "/auth/logout",
    dependencies=[Depends(require_same_origin_state_change)],
)(logout_handler)
app.get("/auth/me")(me_handler)
app.post(
    "/api/account/change-password",
    dependencies=[Depends(require_same_origin_state_change)],
)(change_password_handler)
app.post(
    "/api/account/bind-email",
    dependencies=[Depends(require_same_origin_state_change)],
)(bind_email_handler)
app.include_router(auth_phone_router)
app.include_router(auth_email_router)
# P1-10b / S-HIGH-2: ``captcha_router`` removed along with the dead
# pre-verify pass-token flow (see auth_phone.py for the rationale).


# --- Admin settings routes (before catch-all) ---

app.include_router(admin_router)
app.include_router(admin_cosyvoice_control_router)
app.include_router(admin_disk_router)
app.include_router(pan_auth_router)
app.include_router(pan_admin_router)
app.include_router(admin_cost_router)
app.include_router(admin_smart_analytics_router)
app.include_router(admin_support_router)
app.include_router(pricing_admin_router)
app.include_router(s2_monitor_router)
app.include_router(admin_job_monitor_router)
app.include_router(admin_billing_router)
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

# Phase 1.5 (plan 2026-05-24): mainland_voice_worker admin status / healthz.
# Read-only admin endpoints; secret never returned. Lifespan validate above
# has already降级 enabled=False if secret incomplete, so router handlers
# can safely call build_mainland_voice_worker_client() and get None.
from mainland_voice_worker import router as mainland_voice_worker_router  # noqa: E402
app.include_router(mainland_voice_worker_router)

# Phase 4.1 (plan 2026-05-24): user-facing CosyVoice clone endpoint.
# POST /api/voice/cosyvoice/clone — multipart upload + allowlist gate +
# 5-layer fail-closed pipeline before any paid worker call.
from cosyvoice_clone.api import router as cosyvoice_clone_router  # noqa: E402
app.include_router(cosyvoice_clone_router)
# Phase 4.3a E1: Express auto-clone internal sample upload endpoint
# (X-Internal-Key; pipeline → gateway OSS PUT → presigned GET URL; no worker call).
from cosyvoice_clone.api import internal_router as cosyvoice_clone_internal_router  # noqa: E402
app.include_router(cosyvoice_clone_internal_router)
app.include_router(voice_catalog_internal_router)

from user_voice_api import router as user_voice_router, internal_router as user_voice_internal_router
app.include_router(user_voice_router)
app.include_router(user_voice_internal_router)

# APF P0 anonymous preview (plan 2026-06-10 T7) — must be before catch-all.
from anonymous_preview_api import router as anonymous_preview_router  # noqa: E402
app.include_router(anonymous_preview_router)

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

app.post(
    "/gateway/upload-video",
    dependencies=[Depends(require_same_origin_state_change)],
)(_gateway_upload_video)

# Chunked upload R1-R6 (plan 2026-06-11 §3.1) — >95MB 大文件经 CF Tunnel 的
# 应用层分片通道。router 自带 CSRF dependency；登录态在各 handler 内显式
# 检查（require_auth + user 非 None）。
from chunked_upload_api import router as chunked_upload_router  # noqa: E402
app.include_router(chunked_upload_router)

# 匿名档分片 A1-A6 (plan 2026-06-11 §9 r1) — 试用弹窗 >95MB 文件的分片通道。
# 三与门 gate（env + admin 匿名预览 + admin 匿名分片）在各 handler 内显式
# 检查；CSRF 手动 try/except（/upload 同款）。
from anonymous_preview_chunked_api import router as anonymous_chunked_router  # noqa: E402
app.include_router(anonymous_chunked_router)

# Rename a job's user-visible display_name (plan §6.5 / D16). Lives on
# the /gateway/* namespace, not /job-api/*, because the collision +
# ownership logic is gateway-level rather than a transparent proxy.
app.patch(
    "/gateway/jobs/{job_id}",
    dependencies=[Depends(require_same_origin_state_change)],
)(intercept_rename_job)

# Suggested "save as new copy" name for the edit-page modal (plan §6.4 / D17).
# Pure read; the user may edit the suggestion before committing.
app.get("/gateway/jobs/{job_id}/suggested-copy-name")(intercept_suggested_copy_name)


# --- Job API routes ---
# All /job-api/* routes go through intercept functions.
# The catch-all is LAST and uses a different path pattern to avoid
# swallowing the specific routes (FastAPI {path:path} bug).

app.get("/job-api/jobs")(intercept_list_jobs)
app.post(
    "/job-api/jobs",
    dependencies=[Depends(require_same_origin_state_change)],
)(intercept_create_job)
app.get("/job-api/jobs/{job_id}")(intercept_get_job)
app.delete(
    "/job-api/jobs/{job_id}",
    dependencies=[Depends(require_same_origin_state_change)],
)(intercept_delete_job_v2)
app.post(
    "/job-api/jobs/{job_id}/source-metadata",
    dependencies=[Depends(_require_internal_access)],
)(update_source_metadata)
app.post(
    "/job-api/jobs/{job_id}/metering",
    dependencies=[Depends(_require_internal_access)],
)(update_job_metering)
app.post(
    "/job-api/jobs/{job_id}/voice-clone",
    dependencies=[Depends(require_same_origin_state_change)],
)(voice_clone_for_selection)
app.post(
    "/job-api/jobs/{job_id}/voice-match",
    dependencies=[Depends(require_same_origin_state_change)],
)(voice_match_for_selection)
# Plan 2026-05-17 §Phase 1: unified candidate endpoint. Defaults
# include_cross_source=True so Studio sees cross-video same-name
# candidates without each caller toggling the flag.
app.post(
    "/job-api/jobs/{job_id}/voice-candidates",
    dependencies=[Depends(require_same_origin_state_change)],
)(voice_candidates_for_selection)
app.get("/api/voice-selection/pricing")(get_voice_selection_pricing)

# Job sub-resources: logs, artifacts, result-summary, continue, review/*, download/*, etc.
app.api_route(
    "/job-api/jobs/{job_id}/{subpath:path}",
    methods=["GET", "POST"],
    dependencies=[Depends(require_same_origin_state_change)],
)(intercept_job_subresource)


# --- Proxy: Job API catch-all (non-jobs paths only) ---
# NOTE: /job-api/jobs* are handled by intercept functions above.
# This also handles global endpoints like /job-api/voice-library.

@app.api_route(
    "/job-api/{path:path}",
    methods=["GET", "PUT", "DELETE", "PATCH", "OPTIONS"],
    dependencies=[Depends(require_same_origin_state_change)],
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

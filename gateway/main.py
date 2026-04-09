"""AIVideoTrans API Gateway.

Step 1: Transparent proxy to existing services.
Step 2: Auth (register/login/logout) + PostgreSQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from admin_settings import router as admin_router
from auth_phone import router as auth_phone_router, captcha_router
from billing import router as billing_router
from credits_observability import router as credits_observability_router
from credits_read import router as credits_read_router
from entitlements import router as entitlements_router
from plan_catalog import router as plan_catalog_router
from subscriptions import router as subscriptions_router
from voice_catalog_api import router as voice_catalog_router, internal_router as voice_catalog_internal_router
from auth import (
    LoginRequest,
    RegisterRequest,
    login_handler,
    logout_handler,
    me_handler,
    register_handler,
    require_auth,
)
import logging

from config import settings
from database import engine
from models import Base

logger = logging.getLogger(__name__)
from job_intercept import (
    intercept_create_job,
    intercept_delete_job_v2,
    intercept_get_job,
    intercept_job_subresource,
    intercept_list_jobs,
    update_job_metering,
    update_source_metadata,
)
from proxy import close_client, init_client, proxy_request
from voice_selection_api import get_voice_selection_pricing, voice_clone_for_selection


@asynccontextmanager
async def lifespan(app: FastAPI):
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
                import logging
                logging.getLogger(__name__).info("Recovered %d stale label tasks", recovered)
    except Exception:
        pass  # Table may not exist yet before migration
    yield
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
app.include_router(auth_phone_router)
app.include_router(captcha_router)


# --- Admin settings routes (before catch-all) ---

app.include_router(admin_router)
app.include_router(billing_router)
app.include_router(credits_observability_router)
app.include_router(credits_read_router)
app.include_router(entitlements_router)
app.include_router(plan_catalog_router)
app.include_router(subscriptions_router)
app.include_router(voice_catalog_router)
app.include_router(voice_catalog_internal_router)

from user_voice_api import router as user_voice_router, internal_router as user_voice_internal_router
app.include_router(user_voice_router)
app.include_router(user_voice_internal_router)


# --- Gateway-native upload endpoint (before catch-all) ---

from upload import handle_upload_video


async def _gateway_upload_video(
    request: Request,
    _user: User | None = Depends(require_auth),
) -> Response:
    return await handle_upload_video(request, user=_user)

app.post("/gateway/upload-video")(_gateway_upload_video)


# --- Job API routes ---
# All /job-api/* routes go through intercept functions.
# The catch-all is LAST and uses a different path pattern to avoid
# swallowing the specific routes (FastAPI {path:path} bug).

app.get("/job-api/jobs")(intercept_list_jobs)
app.post("/job-api/jobs")(intercept_create_job)
app.get("/job-api/jobs/{job_id}")(intercept_get_job)
app.delete("/job-api/jobs/{job_id}")(intercept_delete_job_v2)
app.post("/job-api/jobs/{job_id}/source-metadata")(update_source_metadata)
app.post("/job-api/jobs/{job_id}/metering")(update_job_metering)
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

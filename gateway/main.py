"""AIVideoTrans API Gateway.

Step 1: Transparent proxy to existing services.
Step 2: Auth (register/login/logout) + PostgreSQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

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
from sqlalchemy import select
from config import settings
from database import async_session, engine
from models import Base, Job, Session as SessionModel, User

logger = logging.getLogger(__name__)
from job_intercept import (
    intercept_create_job,
    intercept_delete_job,
    intercept_get_job,
    intercept_job_subresource,
    intercept_list_jobs,
    intercept_project_file,
    intercept_result_download,
)
from proxy import close_client, init_client, proxy_request


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience: auto-create tables. In production use Alembic migrations.
    if not settings.auth_required:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    init_client()
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


# --- Middleware: auto-reconcile job records on every /job-api/ request ---

@app.middleware("http")
async def reconcile_job_middleware(request: Request, call_next):
    """After proxying /job-api/jobs/{id}, check if job exists in PostgreSQL.

    If not, auto-insert for the current user. This middleware runs AFTER
    the response is generated, so it doesn't block the user.
    Works regardless of which route handler processed the request.
    """
    response = await call_next(request)

    # Only reconcile on successful GET /job-api/jobs/{job_id} requests
    path = request.url.path
    if (
        request.method == "GET"
        and path.startswith("/job-api/jobs/job_")
        and response.status_code == 200
        and settings.auth_required
    ):
        import re as _re
        match = _re.match(r"^/job-api/jobs/(job_[a-f0-9]+)(?:/|$)", path)
        if match:
            job_id = match.group(1)
            try:
                session_id = request.cookies.get("session_id", "")
                if session_id:
                    async with async_session() as db:
                        # Find user from session
                        result = await db.execute(
                            select(SessionModel).where(SessionModel.session_id == session_id)
                        )
                        sess = result.scalar_one_or_none()
                        if sess is not None:
                            # Check if job already in DB
                            existing = await db.execute(
                                select(Job).where(Job.job_id == job_id)
                            )
                            if existing.scalar_one_or_none() is None:
                                job = Job(
                                    job_id=job_id,
                                    user_id=sess.user_id,
                                    source_type="youtube_url",
                                    source_ref="",
                                    title="",
                                    speakers="auto",
                                    status="running",
                                )
                                db.add(job)
                                await db.commit()
                                logger.info("Middleware reconciled job %s for user %s", job_id, sess.user_id)
            except Exception:
                pass  # Never block user request due to reconciliation failure

    return response


# --- Health check ---

@app.get("/gateway/health")
async def health():
    return {"status": "ok", "auth_required": settings.auth_required}


# --- Auth routes ---

app.post("/auth/register")(register_handler)
app.post("/auth/login")(login_handler)
app.post("/auth/logout")(logout_handler)
app.get("/auth/me")(me_handler)


# --- Web UI API intercept routes (before catch-all) ---

app.get("/api/result-download")(intercept_result_download)
app.get("/api/project-file")(intercept_project_file)
app.post("/api/job/delete")(intercept_delete_job)


# --- Proxy: Web UI API catch-all (/api/*) ---

@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_web_ui(
    request: Request,
    path: str,
    _user: User | None = Depends(require_auth),
) -> Response:
    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )


# --- Job API routes ---
# All /job-api/* routes go through intercept functions.
# The catch-all is LAST and uses a different path pattern to avoid
# swallowing the specific routes (FastAPI {path:path} bug).

app.get("/job-api/jobs")(intercept_list_jobs)
app.post("/job-api/jobs")(intercept_create_job)
app.get("/job-api/jobs/{job_id}")(intercept_get_job)

# Job sub-resources: logs, artifacts, result-summary, continue, etc.
app.api_route(
    "/job-api/jobs/{job_id}/{subpath:path}",
    methods=["GET", "POST"],
)(intercept_job_subresource)


# --- Proxy: Job API catch-all (non-jobs paths only) ---
# NOTE: /job-api/jobs* are handled by intercept functions above.
# This only handles other /job-api/ endpoints (if any).

@app.api_route(
    "/job-api/{path:path}",
    methods=["PUT", "DELETE", "PATCH", "OPTIONS"],
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


# --- Proxy: Web UI API (legacy /web-ui-api/*) ---

@app.api_route(
    "/web-ui-api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_web_ui_legacy(
    request: Request,
    path: str,
    _user: User | None = Depends(require_auth),
) -> Response:
    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="/web-ui-api",
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

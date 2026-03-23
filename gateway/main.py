"""AIVideoTrans API Gateway.

Step 1: Transparent proxy to existing services.
Step 2: Auth (register/login/logout) + PostgreSQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from auth import (
    LoginRequest,
    RegisterRequest,
    login_handler,
    logout_handler,
    me_handler,
    register_handler,
)
from config import settings
from database import engine
from models import Base
from job_intercept import (
    intercept_create_job,
    intercept_get_job,
    intercept_job_subresource,
    intercept_list_jobs,
    intercept_project_file,
    intercept_result_download,
)
from proxy import proxy_request


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (dev convenience; use Alembic in production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="AIVideoTrans Gateway",
    version="0.2.0",
    docs_url="/gateway/docs" if not settings.auth_required else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# --- Proxy: Web UI API catch-all (/api/*) ---

@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_web_ui(request: Request, path: str) -> Response:
    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )


# --- Job API intercept routes (specific routes before catch-all) ---

app.get("/job-api/jobs")(intercept_list_jobs)
app.post("/job-api/jobs")(intercept_create_job)
app.get("/job-api/jobs/{job_id}")(intercept_get_job)

# Job sub-resources: logs, artifacts, result-summary, continue, etc.
app.api_route(
    "/job-api/jobs/{job_id}/{subpath:path}",
    methods=["GET", "POST"],
)(intercept_job_subresource)


# --- Proxy: Job API catch-all (/job-api/*) ---

@app.api_route(
    "/job-api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_job_api(request: Request, path: str) -> Response:
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
async def proxy_web_ui_legacy(request: Request, path: str) -> Response:
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

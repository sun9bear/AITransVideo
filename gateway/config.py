"""Gateway configuration loaded from environment variables."""

from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    """All gateway configuration comes from environment variables."""

    # Upstream services
    job_api_upstream: str = "http://127.0.0.1:8877"
    jobs_dir: str = Field(
        default="/opt/aivideotrans/app/jobs",
        validation_alias="AIVIDEOTRANS_JOBS_DIR",
    )
    runtime_logs_dir: str = Field(
        default="/opt/aivideotrans/data/runtime_logs",
        validation_alias="AIVIDEOTRANS_RUNTIME_LOGS_DIR",
    )

    # Gateway server
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8880

    # Deployment environment ("dev" / "staging" / "production").
    # Read from AVT_ENV. Used by startup validators (see startup_checks.py);
    # production mode refuses to start with auth_required=False.
    env: str = "dev"

    # Database (Step 2)
    pg_password: str = ""
    database_url: str = ""

    # Internal API key (T4) — guards /api/internal/* endpoints.
    # Set via AVT_INTERNAL_API_KEY. Startup (validate_internal_api_key) refuses
    # to run if unset/too short; per-request dependency _require_internal_access
    # re-reads this at request time so tests can monkeypatch it.
    internal_api_key: str = ""

    # Auth (Step 2)
    auth_required: bool = True
    session_expire_days: int = 7
    session_cookie_name: str = "avt_session"

    # CORS — set via env var AVT_CORS_ORIGINS (comma-separated)
    # e.g. AVT_CORS_ORIGINS="https://aivideotrans.site,https://www.aivideotrans.site"
    cors_origins: str = "https://aivideotrans.site"

    # --- Phone auth (Task 3) ---
    # Default to "fake" so local dev, tests, and preview builds do not require any
    # real SMS or captcha vendor credentials. The real-provider path is intentionally
    # out of scope for Task 3.
    sms_provider: str = "fake"
    captcha_provider: str = "fake"
    geetest_api_server: str = "http://gcaptcha4.geetest.com"
    geetest_register_captcha_id: str = ""
    geetest_register_captcha_key: str = ""
    geetest_login_captcha_id: str = ""
    geetest_login_captcha_key: str = ""

    # OTP lifetime. Kept deliberately short.
    phone_code_ttl_seconds: int = 300  # 5 minutes
    phone_code_length: int = 6

    # Rate limits for `/auth/phone/send-code`. All thresholds are per window.
    # Phone-based limits block aggressive retries against a single number; IP-based
    # limits blunt bulk-spray attacks from a single origin.
    phone_send_code_window_seconds: int = 60
    phone_send_code_max_per_phone_window: int = 1  # one code per phone per minute
    phone_send_code_hour_window_seconds: int = 3600
    phone_send_code_max_per_phone_hour: int = 5
    phone_send_code_max_per_ip_hour: int = 20

    # Public email registration switch. Phone-first registration stays the
    # default UX, but email can be offered as a secondary path. When false,
    # `POST /auth/register` refuses to create email accounts (returns 403).
    email_registration_enabled: bool = True

    # --- Email auth (registration verification + password reset) ---
    # Default to "fake" so tests/local development do not depend on a live
    # external mail provider. Production can set AVT_EMAIL_AUTH_PROVIDER=resend
    # and reuse notifications.send_email / RESEND_API_KEY.
    email_auth_provider: str = "fake"
    email_code_ttl_seconds: int = 900  # 15 minutes
    email_code_length: int = 6
    email_send_code_window_seconds: int = 60
    email_send_code_max_per_email_window: int = 1
    email_send_code_hour_window_seconds: int = 3600
    email_send_code_max_per_email_hour: int = 5
    email_send_code_max_per_ip_hour: int = 20

    # --- Studio post-edit workflow (plan 2026-04-18 D29) ---
    # Backend gate for the editing endpoints (enter-edit / editing/cancel /
    # editing/commit). Disabled by default so Phase 0 can ship without
    # exposing the T1-1 skeleton to production users. Flip to True once
    # the full Phase 1 flow is ready for dogfooding. Mirrors the frontend
    # flag NEXT_PUBLIC_ENABLE_POST_EDIT which gates the UI entry points.
    enable_post_edit: bool = False

    # --- Phase 2 R2 download backend (plan 2026-04-23) ---
    # Pluggable artifact-download target. "local" (default) keeps the historic
    # gateway → Job API byte-passthrough. "r2" redirects the user with HTTP 302
    # to a short-lived Cloudflare R2 presigned URL. Any R2 error — missing
    # config / HEAD failure / upload timeout / signing exception — auto-falls
    # back to local so users never see a failure (see gateway/storage/
    # backend_router.py). Phase 2 only covers the ``publish.dubbed_video``
    # artifact key; other artifacts keep local path unconditionally.
    download_redirect_backend: Literal["local", "r2"] = "local"

    # R2 credentials & bucket. Env var names intentionally do NOT carry the
    # AVT_ prefix — they follow the upstream Cloudflare-R2 plan convention
    # (§10.1 of 2026-04-21-cloudflare-r2-deployment-plan.md) and match what
    # the existing scripts/phase0_probes/ tooling expects. `validation_alias`
    # bypasses the class-level AVT_ prefix for these four fields.
    r2_endpoint: str = Field(default="", validation_alias="R2_ENDPOINT")
    r2_access_key_id: str = Field(default="", validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(default="", validation_alias="R2_SECRET_ACCESS_KEY")
    r2_artifacts_bucket: str = Field(default="avt-artifacts", validation_alias="R2_ARTIFACTS_BUCKET")

    # Presigned URL TTL in seconds. Deliberately tight (120s = 2 min) so that
    # URL leakage has a very small replay window. User download clicks
    # follow the 302 immediately; slow network clients on the CF edge still
    # have plenty of headroom because the URL only needs to be *accepted*
    # during the TTL, not the full body transferred.
    r2_presigned_expires_s: int = 120

    # Plan 2026-05-07 §11 Stage C / CodeX P2 follow-up (2026-05-12):
    # ``<video src=...>`` players issue multiple Range requests over the
    # full playback window (pause / resume / seek may re-fetch minutes
    # apart). 120s would 403 mid-playback on any video > 2 min. Stream
    # presign uses this larger budget (default 30 min) so a typical
    # workspace play / pause / scrub session stays in one signature
    # window. URLs still aren't permanent — leak window is bounded —
    # and the object key path component itself is opaque
    # (``jobs/{job_id}/g{N}/...``) so it doesn't enumerate.
    r2_stream_presigned_expires_s: int = Field(
        default=1800,
        validation_alias="R2_STREAM_PRESIGNED_EXPIRES_S",
    )

    # Upload timeout when lazily pushing a never-seen-in-R2 artifact. If the
    # upload cannot complete inside this budget, the router gives up and
    # falls back to local. Kept tight to avoid holding user download
    # requests for too long on a bad day.
    r2_upload_timeout_s: int = 60

    model_config = {"env_prefix": "AVT_", "populate_by_name": True}


def resolve_database_url(raw: GatewaySettings) -> str:
    """Resolve final database URL or raise if no credentials provided.

    Pure function — does NOT mutate raw or trigger at import time.
    Caller must invoke this explicitly (typically at app startup).

    Precedence: explicit raw.database_url → pg_password → refuse fallback.
    """
    if raw.database_url:
        return raw.database_url
    if raw.pg_password:
        encoded = quote_plus(raw.pg_password)
        return f"postgresql+asyncpg://avt:{encoded}@127.0.0.1:5432/aivideotrans"
    raise RuntimeError(
        "Gateway startup refused: neither AVT_PG_PASSWORD nor AVT_DATABASE_URL is set. "
        "Refusing to fall back to default 'avt:avt' credentials. "
        "Set AVT_PG_PASSWORD (preferred) or AVT_DATABASE_URL explicitly."
    )


settings = GatewaySettings()
# NOTE: database_url is NOT populated here. gateway/main.py is responsible
# for calling resolve_database_url(settings) explicitly at startup. Import of
# this module must not raise on missing creds, so tests can import config
# in a clean env without side effects.

"""Gateway configuration loaded from environment variables."""

from urllib.parse import quote_plus

from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    """All gateway configuration comes from environment variables."""

    # Upstream services
    job_api_upstream: str = "http://127.0.0.1:8877"

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

    # Public registration switch. When False, `POST /auth/register` refuses to
    # create new accounts (returns 403). Legacy email LOGIN is unaffected.
    email_registration_enabled: bool = False

    # --- Studio post-edit workflow (plan 2026-04-18 D29) ---
    # Backend gate for the editing endpoints (enter-edit / editing/cancel /
    # editing/commit). Disabled by default so Phase 0 can ship without
    # exposing the T1-1 skeleton to production users. Flip to True once
    # the full Phase 1 flow is ready for dogfooding. Mirrors the frontend
    # flag NEXT_PUBLIC_ENABLE_POST_EDIT which gates the UI entry points.
    enable_post_edit: bool = False

    model_config = {"env_prefix": "AVT_"}


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

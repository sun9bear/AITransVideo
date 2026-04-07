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

    # Database (Step 2)
    pg_password: str = ""
    database_url: str = ""

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

    model_config = {"env_prefix": "AVT_"}


_raw = GatewaySettings()
# Build database_url with URL-encoded password if not explicitly set
if not _raw.database_url and _raw.pg_password:
    _encoded_pw = quote_plus(_raw.pg_password)
    _raw.database_url = (
        f"postgresql+asyncpg://avt:{_encoded_pw}@127.0.0.1:5432/aivideotrans"
    )
elif not _raw.database_url:
    _raw.database_url = "postgresql+asyncpg://avt:avt@localhost:5432/aivideotrans"
settings = _raw

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

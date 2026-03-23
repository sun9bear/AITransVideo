"""Gateway configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    """All gateway configuration comes from environment variables."""

    # Upstream services (existing, not modified)
    web_ui_upstream: str = "http://127.0.0.1:8876"
    job_api_upstream: str = "http://127.0.0.1:8877"

    # Gateway server
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8880

    # Database (Step 2)
    database_url: str = "postgresql+asyncpg://avt:avt@localhost:5432/aivideotrans"

    # Auth (Step 2)
    auth_required: bool = False
    session_expire_days: int = 7
    session_cookie_name: str = "avt_session"

    model_config = {"env_prefix": "AVT_"}


settings = GatewaySettings()

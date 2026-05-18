"""Startup-time validation helpers for the gateway.

Pure functions with no import-time side effects (no DB, no FastAPI, no network).
Designed to be called from gateway/main.py's `lifespan` startup block and to
be directly unit-testable without stubbing `database`, `auth`, etc.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def validate_production_safety(env: str, auth_required: bool) -> None:
    """Pure check: refuse to start if production mode has auth disabled.

    Standalone function so tests can call directly without reloading
    gateway.main (which triggers FastAPI app re-construction side effects).
    """
    if env == "production" and not auth_required:
        raise RuntimeError(
            "Refusing to start: AVT_ENV=production requires AVT_AUTH_REQUIRED=true. "
            "Disabling auth in production would expose all jobs to anonymous access."
        )


def validate_internal_api_key(key: str) -> None:
    """Refuse to start if AVT_INTERNAL_API_KEY is unset or too short (T4).

    Without a key, internal endpoints fail closed (the request-time check
    returns 503), but that's noisy. Force operators to set it explicitly
    so misconfigured deploys surface at startup, not at first 503.

    Minimum 16 chars. 32+ random chars recommended (see .env.example).
    """
    if not key or len(key) < 16:
        raise RuntimeError(
            "Gateway startup refused: AVT_INTERNAL_API_KEY must be set "
            "(minimum 16 chars, recommended: 32+ random chars). "
            "Generate: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
        )


def is_startup_recovery_schema_missing_error(exc: BaseException) -> bool:
    """Return whether a startup recovery failure is the expected pre-migration case.

    The stale-task recovery hooks run during Gateway startup. In a fresh local
    environment, or before a migration is applied, the queue tables may not
    exist yet. That case should be visible as a warning, while unrelated
    recovery failures should be logged with full exception details.

    This is intentionally string-based because startup may see different DB
    driver exception types. The safe failure direction is to return False: an
    unrecognized schema-missing error is logged with logger.exception instead
    of being silently swallowed.
    """
    text = f"{type(exc).__module__}.{type(exc).__name__}: {exc}".lower()
    if "undefinedtable" in text or "no such table" in text:
        return True
    if "doesn't exist" in text and "table" in text:
        return True
    return "relation" in text and "does not exist" in text


def validate_r2_backend(
    backend: str,
    r2_endpoint: str,
    r2_access_key_id: str,
    r2_secret_access_key: str,
) -> str:
    """Check R2 config consistency and return the effective backend name.

    Phase 2 R2 download backend (plan 2026-04-23).

    Contract:
      - When backend == "local" (default): returns "local" unconditionally,
        even if R2 credentials are missing. "local" is the always-safe path.
      - When backend == "r2" AND all three credentials are non-empty:
        returns "r2".
      - When backend == "r2" but any credential is missing: logs CRITICAL
        and DOWNGRADES to "local" instead of raising. Rationale: the
        gateway must keep serving downloads — a misconfigured R2 flag
        should never take the service down. Ops notice the CRITICAL log.

    The returned string should replace ``settings.download_redirect_backend``
    on the live settings object (or a per-process copy) so that request-time
    code reads the effective (not configured) backend. This is the single
    source of truth for "is R2 really on".

    Returns:
        "local" or "r2" — the effective backend after safety downgrade.
    """
    backend = (backend or "").strip().lower()
    if backend not in ("local", "r2"):
        logger.critical(
            "AVT_DOWNLOAD_REDIRECT_BACKEND=%r is not one of {local, r2}; "
            "downgrading to local.",
            backend,
        )
        return "local"

    if backend == "local":
        return "local"

    # backend == "r2"
    missing = [
        name
        for name, value in (
            ("R2_ENDPOINT", r2_endpoint),
            ("R2_ACCESS_KEY_ID", r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", r2_secret_access_key),
        )
        if not value
    ]
    if missing:
        logger.critical(
            "AVT_DOWNLOAD_REDIRECT_BACKEND=r2 but required credential(s) missing: %s. "
            "Downgrading to local. Downloads will continue via the legacy "
            "gateway -> Job API byte passthrough.",
            ", ".join(missing),
        )
        return "local"

    logger.info(
        "Phase 2 R2 download backend ENABLED (endpoint=%s, bucket inferred from settings).",
        r2_endpoint,
    )
    return "r2"


def validate_pan_backup_config(settings) -> None:
    """Validate pan backup env if feature enabled. CRITICAL at startup.

    Plan 2026-05-13 design §5.3 / 2026-05-14 impl T2.2.

    If AVT_ENABLE_PAN_BACKUP=false (default): no-op. Otherwise, all 4 of
    appkey / appsecret / redirect_uri / Fernet key must be set AND the Fernet
    key must decode as a valid 32-byte base64-encoded key.

    Raises:
        RuntimeError with actionable message naming the missing env var(s).
    """
    if not settings.enable_pan_backup:
        return

    required = [
        ("AVT_BAIDU_PAN_APPKEY", settings.baidu_pan_appkey),
        ("AVT_BAIDU_PAN_APPSECRET", settings.baidu_pan_appsecret),
        ("AVT_BAIDU_PAN_REDIRECT_URI", settings.baidu_pan_redirect_uri),
        ("AVT_PAN_TOKEN_ENCRYPTION_KEY", settings.pan_token_encryption_key),
    ]
    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            f"AVT_ENABLE_PAN_BACKUP=true but required env vars missing: {missing}. "
            f"Either set them in .env or AVT_ENABLE_PAN_BACKUP=false."
        )

    # Verify Fernet key is a real 32-byte url-safe base64 key
    try:
        from cryptography.fernet import Fernet
        Fernet(settings.pan_token_encryption_key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"AVT_PAN_TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}. "
            f"Generate one with: python -c \"from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())\""
        )

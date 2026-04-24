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

"""Startup-time validation helpers for the gateway.

Pure functions with no import-time side effects (no DB, no FastAPI, no network).
Designed to be called from gateway/main.py's `lifespan` startup block and to
be directly unit-testable without stubbing `database`, `auth`, etc.
"""

from __future__ import annotations


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

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

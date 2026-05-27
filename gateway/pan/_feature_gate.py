"""Feature flag gate for the Pan Backup HTTP API.

Plan 2026-05-26 postmortem P0a (Codex feedback). Prior to this gate,
``AVT_ENABLE_PAN_BACKUP=false`` only short-circuited scheduler ticks
in ``gateway/pan/scheduler.py``. The HTTP router endpoints under
``/api/admin/pan/*`` (admin_api.py + auth.py) were registered
unconditionally in ``gateway/main.py``, so any admin could still hit
"Create backup" and trigger the buggy concurrent executor path while
the flag was off — defeating the safety value of the flag.

This module exports ``require_pan_enabled``, a FastAPI dependency
that returns 503 when the flag is off. Both pan routers list it as
the FIRST dependency (before auth/admin checks) so the rejection is
unambiguous: "feature disabled" instead of "permission denied",
which would be misleading.

Why 503 and not 404 / 403:
  - 404 implies the route doesn't exist (operator could misread as
    deploy issue or version skew)
  - 403 implies the caller lacks permission (operator might try
    different credentials)
  - 503 with a Chinese-language detail string is unambiguous:
    feature is disabled by configuration, talk to ops

Why dependency-based vs conditional include in main.py:
  - The dependency lives in one well-tested place
  - Routes stay registered so OpenAPI docs / tests / smoke clients
    can still introspect the surface
  - Flipping the flag requires container restart anyway (env-driven
    settings), so there's no hot-reload benefit lost
"""
from __future__ import annotations

from fastapi import HTTPException, status

from config import settings


def require_pan_enabled() -> None:
    """Raise 503 unless ``settings.enable_pan_backup`` is True.

    Wire this as the FIRST entry in the router's ``dependencies`` list
    (before auth / csrf / admin gates) so the rejection reason is
    clear when the feature is off.
    """
    if not settings.enable_pan_backup:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="网盘备份功能未启用（AVT_ENABLE_PAN_BACKUP=false），请联系管理员开启",
        )

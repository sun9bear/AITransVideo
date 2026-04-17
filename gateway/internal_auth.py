"""Shared helpers for gateway → Job API internal calls.

Purpose: one place to build the ``X-Internal-Key`` header so that every
internal call (admin routes, voice-catalog labeling, CosyVoice verify,
future internal clients) shares the same shape.

Background: before 2026-04-17 T2.2, two copies of ``_internal_headers()``
lived in ``voice_catalog_api.py`` and ``voice_catalog_service.py``, and
the admin routes (``admin_job_monitor_api.py``, ``admin_settings.py``,
``s2_monitor_api.py``) did not send the key at all — they trusted
loopback + ``_require_admin()``. Defense-in-depth is cheap and keeps the
admin-route path consistent with the other internal callers.

Design:

* Key is read from ``AVT_INTERNAL_API_KEY`` at **call time**, not module
  import time — so ``monkeypatch.setenv`` in tests takes effect without a
  module reload. See tests/test_gateway_*.py for the pattern.
* If the env var is unset, the header is omitted and the Job API's
  internal-path check (see ``src/services/jobs/api.py`` X-Internal-Key
  validator) will 403. Fail-closed, not fail-open. The prior batch's
  ``startup_checks.validate_internal_api_key`` refuses to start a gateway
  without the var set in production, so in practice this function should
  always return a populated header.
"""
from __future__ import annotations

import os


def internal_headers() -> dict[str, str]:
    """Headers to send on gateway → Job API internal calls.

    Always includes ``Content-Type: application/json``. Adds
    ``X-Internal-Key: {AVT_INTERNAL_API_KEY}`` when that env var is set
    (startup validation guarantees it in production).
    """
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    h: dict[str, str] = {"Content-Type": "application/json"}
    if key:
        h["X-Internal-Key"] = key
    return h

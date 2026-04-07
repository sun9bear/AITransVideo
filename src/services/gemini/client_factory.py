"""Unified Gemini client factory — Vertex AI service account (preferred).

Priority order:
1. GOOGLE_APPLICATION_CREDENTIALS — Vertex AI service account JSON (RPM-based
   quotas, generous). Requires location=global for Gemini 3.x models.
2. VERTEX_AI_EXPRESS_KEY — Vertex AI Express API key (250/day hard limit on
   gemini-3.1-pro, use only as fallback).
3. GEMINI_API_KEY — AI Studio API key (legacy fallback).

All downstream code uses the same google-genai SDK — only the Client
initialization differs between the modes.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PROJECT = "gen-lang-client-0200199358"
_DEFAULT_LOCATION = "global"  # Required for Gemini 3.x models


def create_gemini_client(api_key: str | None = None) -> Any:
    """Create a google-genai Client.

    Parameters
    ----------
    api_key:
        Explicit API key override.  When provided AND no env-based
        credentials are available, uses this key directly.
    """
    import importlib
    genai = importlib.import_module("google.genai")

    # 1. Vertex AI service account JSON (preferred — generous RPM-based quotas)
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if gac and os.path.isfile(gac):
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", _DEFAULT_PROJECT).strip()
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", _DEFAULT_LOCATION).strip()
        logger.info("[Gemini] Vertex AI mode: project=%s, location=%s", project, location)
        return genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )

    # 2. Vertex AI Express key (fallback — has 250/day limit on gemini-3.1-pro)
    express_key = os.environ.get("VERTEX_AI_EXPRESS_KEY", "").strip()
    if express_key:
        logger.info("[Gemini] Vertex AI Express mode (API key)")
        return genai.Client(api_key=express_key)

    # 3. AI Studio fallback
    key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Gemini 未配置：需要 VERTEX_AI_EXPRESS_KEY / "
            "GOOGLE_APPLICATION_CREDENTIALS / GEMINI_API_KEY"
        )
    logger.info("[Gemini] AI Studio mode (API key)")
    return genai.Client(api_key=key)

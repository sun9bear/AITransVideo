"""web_ui package — re-exports for backward compatibility.

All external code imports from ``services.web_ui`` continue to work.
"""
from __future__ import annotations

# --- constants ---
from .constants import (
    WEB_UI_DEFAULT_HOST,
    WEB_UI_DEFAULT_PORT,
    WEB_UI_TITLE,
)

# --- models ---
from .models import ProcessJobSnapshot, WebUICommandArgs

# --- job managers ---
from .job_managers import (
    JobAPIBackedJobManager,
    JobAPIRequestError,
    ProcessJobManager,
)

# --- config helpers ---
from .config_helpers import (
    build_provider_key_options,
    build_route_visualization,
    build_translation_model_options,
    save_web_ui_settings,
    set_translation_primary_model,
)

# --- snapshot ---
from .snapshot import build_web_ui_snapshot

# --- server ---
from .server import create_web_ui_server, run_web_ui_server

# --- project resolver ---
from .project_resolver import _resolve_authoritative_review_project_dir

# --- speaker review ---
from .speaker_review import _save_speaker_review_submission

# --- translation review ---
from .translation_review import _save_translation_review_submission

__all__ = [
    "WEB_UI_DEFAULT_HOST",
    "WEB_UI_DEFAULT_PORT",
    "WEB_UI_TITLE",
    "ProcessJobSnapshot",
    "WebUICommandArgs",
    "JobAPIBackedJobManager",
    "JobAPIRequestError",
    "ProcessJobManager",
    "build_provider_key_options",
    "build_route_visualization",
    "build_translation_model_options",
    "build_web_ui_snapshot",
    "create_web_ui_server",
    "run_web_ui_server",
    "save_web_ui_settings",
    "set_translation_primary_model",
    "_resolve_authoritative_review_project_dir",
    "_save_speaker_review_submission",
    "_save_translation_review_submission",
]

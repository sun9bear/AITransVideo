"""web_ui package — retained library surface.

Originally this package backed the standalone Web UI server on port 8876.
That server was retired in the 2026-04-17 legacy migration cleanup (T1.6b);
what remains is a library of helpers still used by the Job API:

  - ProcessJobManager / JobAPIBackedJobManager — job lifecycle managers
  - build_web_ui_snapshot — UI state synthesis
  - config_helpers — provider-key / translation-model option builders
  - project_resolver / speaker_review / translation_review — review helpers
  - voice_library — voice catalog (imported by src/services/jobs/api.py)

The `server` and `handler` modules are gone. The constants `WEB_UI_TITLE`
and `WEB_UI_DEFAULT_HOST` are kept only to avoid breaking any historical
importer; remove them if a follow-up audit shows no remaining consumer.
"""
from __future__ import annotations

# --- constants (minimal, post-server retirement) ---
from .constants import WEB_UI_DEFAULT_HOST, WEB_UI_TITLE

# --- models ---
from .models import ProcessJobSnapshot

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

# --- project resolver ---
from .project_resolver import _resolve_authoritative_review_project_dir

# --- speaker review ---
from .speaker_review import _save_speaker_review_submission

# --- translation review ---
from .translation_review import _save_translation_review_submission

__all__ = [
    "WEB_UI_DEFAULT_HOST",
    "WEB_UI_TITLE",
    "ProcessJobSnapshot",
    "JobAPIBackedJobManager",
    "JobAPIRequestError",
    "ProcessJobManager",
    "build_provider_key_options",
    "build_route_visualization",
    "build_translation_model_options",
    "build_web_ui_snapshot",
    "save_web_ui_settings",
    "set_translation_primary_model",
    "_resolve_authoritative_review_project_dir",
    "_save_speaker_review_submission",
    "_save_translation_review_submission",
]

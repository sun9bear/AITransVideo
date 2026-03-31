"""Centralized path helpers for user-isolated workspaces and uploads.

All functions are pure (no I/O, no side effects) unless explicitly noted.
Directory layout:
    projects/<user_id>/<job_id>/          — per-job workspace
    uploads/<user_id>/<upload_id>_<name>  — per-user upload staging
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath


def build_workspace_dir(
    user_id: int | str,
    job_id: str,
    *,
    projects_root: str = "projects",
) -> str:
    """Return the canonical workspace path: ``<projects_root>/<user_id>/<job_id>``."""
    safe_user = _safe_segment(str(user_id))
    safe_job = _safe_segment(job_id)
    return f"{projects_root}/{safe_user}/{safe_job}"


def build_upload_path(
    user_id: int | str,
    upload_id: str,
    filename: str,
    *,
    uploads_root: str = "uploads",
) -> str:
    """Return the canonical upload path: ``<uploads_root>/<user_id>/<upload_id>_<safe_name>``."""
    safe_user = _safe_segment(str(user_id))
    safe_upload_id = _safe_segment(upload_id)
    safe_name = _sanitize_filename(filename)
    return f"{uploads_root}/{safe_user}/{safe_upload_id}_{safe_name}"


def is_legacy_workspace(project_dir: str | None) -> bool:
    """Return True if *project_dir* does NOT follow ``<root>/<user_id>/<job_id>`` layout.

    Useful for deciding whether to fall back to URL-based project resolution.
    """
    if not project_dir:
        return True
    # New layout always has at least <root>/<user_id>/<job_id> — 3 segments
    parts = _split_path(project_dir)
    if len(parts) < 3:
        return True
    # job_id segment should start with "job_"
    return not parts[-1].startswith("job_")


def extract_user_id_from_workspace(project_dir: str) -> str | None:
    """Extract the user_id segment from a new-layout workspace path.

    Returns None for legacy paths.
    """
    if is_legacy_workspace(project_dir):
        return None
    parts = _split_path(project_dir)
    # Layout: .../<user_id>/<job_id>
    return parts[-2] if len(parts) >= 2 else None


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_\-]")


def _safe_segment(value: str) -> str:
    """Sanitize a single path segment — strip, replace unsafe chars, forbid traversal."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Path segment must not be empty")
    result = _UNSAFE_CHARS.sub("_", cleaned)
    # Collapse any remaining dots that could form traversal patterns
    while ".." in result:
        result = result.replace("..", "_")
    return result


_UNSAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9_\-.]")


def _sanitize_filename(filename: str) -> str:
    """Produce a filesystem-safe filename, preserving extension."""
    cleaned = filename.strip()
    if not cleaned:
        return "unnamed"
    result = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    while ".." in result:
        result = result.replace("..", "_")
    return result


def _split_path(path_str: str) -> list[str]:
    """Split a path string into segments, handling both POSIX and Windows."""
    # Try POSIX first, then Windows
    parts = PurePosixPath(path_str).parts
    if len(parts) <= 1:
        parts = PureWindowsPath(path_str).parts
    return [p for p in parts if p not in ("/", "\\")]

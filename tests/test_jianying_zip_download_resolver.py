"""Resolver glob behavior for editor.jianying_draft_zip download key.

2026-05-04: zip filenames switched from ``jianying_draft_{job_id}.zip`` to
the user-friendly ``{title}_{date}.zip`` (with ``_2``, ``_3``... collision
suffixes). The download resolver in ``services/web_ui/project_resolver.py``
used to require the ``jianying_draft_`` prefix; we loosened it to ``*.zip``
since the ``jianying/exports/`` directory is jianying-specific (every zip
in it is a jianying draft by construction).

These tests guard:
  1. The new friendly filename is found by the resolver.
  2. Legacy ``jianying_draft_*`` filenames continue to work (mid-rollout
     existing projects keep their old zip files).
  3. With multiple zips, the most recently modified one is returned.
  4. Non-zip files in the directory are ignored.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Make src/ importable.
_repo_root = Path(__file__).resolve().parents[1]
_src = _repo_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def _set_mtime(path: Path, when: float) -> None:
    """Force a specific mtime so 'most recent' assertions are deterministic."""
    os.utime(path, (when, when))


def _build_project(
    tmp_path: Path,
    *,
    zip_basenames: list[str],
    extra_files: list[str] | None = None,
) -> Path:
    """Create a fake project with jianying/exports/<basenames>.zip + extras.

    Returns the project_dir path. Each zip's mtime is staggered (older first)
    so the resolver should return the LAST entry in zip_basenames.
    """
    project_dir = tmp_path / "projects" / "user_42" / "job_xxx"
    exports = project_dir / "jianying" / "exports"
    exports.mkdir(parents=True)
    base_time = time.time() - 1000
    for i, name in enumerate(zip_basenames):
        p = exports / f"{name}.zip"
        p.write_bytes(b"PK\x03\x04fake-zip")
        _set_mtime(p, base_time + i * 10)  # later index = newer
    for extra in extra_files or []:
        (exports / extra).write_bytes(b"")
    return project_dir


def test_resolver_finds_friendly_named_zip(tmp_path: Path) -> None:
    """A user-friendly basename without ``jianying_draft_`` prefix is matched."""
    from services.web_ui.project_resolver import (  # noqa: PLC0415
        _resolve_public_result_download_path,
    )

    project_dir = _build_project(
        tmp_path,
        zip_basenames=["如何在6到12个月内彻底重塑自我_2026-05-04"],
    )
    out = _resolve_public_result_download_path(
        project_root=tmp_path,
        project_dir=project_dir,
        download_key="editor.jianying_draft_zip",
    )
    assert out is not None
    assert out.name == "如何在6到12个月内彻底重塑自我_2026-05-04.zip"


def test_resolver_finds_legacy_prefixed_zip(tmp_path: Path) -> None:
    """Old-style ``jianying_draft_<id>.zip`` continues to be served."""
    from services.web_ui.project_resolver import (  # noqa: PLC0415
        _resolve_public_result_download_path,
    )

    project_dir = _build_project(
        tmp_path,
        zip_basenames=["jianying_draft_job_abc123"],
    )
    out = _resolve_public_result_download_path(
        project_root=tmp_path,
        project_dir=project_dir,
        download_key="editor.jianying_draft_zip",
    )
    assert out is not None
    assert out.name == "jianying_draft_job_abc123.zip"


def test_resolver_returns_most_recent_when_multiple(tmp_path: Path) -> None:
    """Mixed legacy + new + collision-suffix → return newest by mtime."""
    from services.web_ui.project_resolver import (  # noqa: PLC0415
        _resolve_public_result_download_path,
    )

    project_dir = _build_project(
        tmp_path,
        zip_basenames=[
            "jianying_draft_job_old",                 # oldest mtime
            "Title_2026-05-03",                        # mid mtime
            "Title_2026-05-04",                        # newest mtime
        ],
    )
    out = _resolve_public_result_download_path(
        project_root=tmp_path,
        project_dir=project_dir,
        download_key="editor.jianying_draft_zip",
    )
    assert out is not None
    assert out.name == "Title_2026-05-04.zip"


def test_resolver_ignores_non_zip_files(tmp_path: Path) -> None:
    """Stray .txt / .json files in jianying/exports/ are not candidates."""
    from services.web_ui.project_resolver import (  # noqa: PLC0415
        _resolve_public_result_download_path,
    )

    project_dir = _build_project(
        tmp_path,
        zip_basenames=["My Title_2026-05-04"],
        extra_files=["readme.txt", "manifest.json", "old_export.tar.gz"],
    )
    out = _resolve_public_result_download_path(
        project_root=tmp_path,
        project_dir=project_dir,
        download_key="editor.jianying_draft_zip",
    )
    assert out is not None
    assert out.suffix == ".zip"
    assert out.name == "My Title_2026-05-04.zip"


def test_resolver_returns_none_when_exports_empty(tmp_path: Path) -> None:
    """No zips at all → None (not an exception)."""
    from services.web_ui.project_resolver import (  # noqa: PLC0415
        _resolve_public_result_download_path,
    )

    project_dir = tmp_path / "projects" / "user" / "job_id"
    (project_dir / "jianying" / "exports").mkdir(parents=True)
    out = _resolve_public_result_download_path(
        project_root=tmp_path,
        project_dir=project_dir,
        download_key="editor.jianying_draft_zip",
    )
    assert out is None

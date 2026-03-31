from __future__ import annotations

import json
from pathlib import Path

from services.jobs.process_runner import (
    _parse_project_dir_from_line,
    _resolve_job_project_dir,
)


def _write_process_project(project_dir: Path, *, youtube_url: str) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "download_metadata.json").write_text(
        json.dumps(
            {
                "url": youtube_url,
                "video_title": project_dir.name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (project_dir / "project_state.json").write_text(
        json.dumps({"project_id": project_dir.name, "stages": {}}),
        encoding="utf-8",
    )
    return project_dir


def test_parse_project_dir_from_posix_log_line_preserves_linux_path_text() -> None:
    project_dir = _parse_project_dir_from_line(
        "[S6] Done /opt/aivideotrans/data/projects/demo/output",
        Path("D:/workspace/app"),
    )

    assert project_dir == "/opt/aivideotrans/data/projects/demo"


def test_resolve_job_project_dir_finds_project_under_data_projects_root(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir(parents=True, exist_ok=True)
    expected_project_dir = _write_process_project(
        tmp_path / "data" / "projects" / "demo_project",
        youtube_url="https://youtube.example/watch?v=data-project-root",
    )

    resolved_project_dir = _resolve_job_project_dir(
        project_root=app_root,
        source_ref="https://youtube.example/watch?v=data-project-root",
        preferred_project_dir=None,
    )

    assert resolved_project_dir == expected_project_dir.resolve(strict=False)

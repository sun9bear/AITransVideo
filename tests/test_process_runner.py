from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from services.jobs.models import JobRecord
from services.jobs.process_runner import (
    ProcessJobRunner,
    _parse_project_dir_from_line,
    _resolve_job_project_dir,
)
from services.jobs.store import JobStore


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


def _make_job(**overrides) -> JobRecord:
    base = {
        "job_id": "job_test001",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.example/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "created_at": "2026-03-31T00:00:00Z",
        "updated_at": "2026-03-31T00:00:00Z",
    }
    base.update(overrides)
    return JobRecord.from_dict(base)


def _make_runner(tmp_path: Path) -> ProcessJobRunner:
    store = JobStore(tmp_path / "jobs")
    return ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=MagicMock(),
        run_timeout_seconds=5,
    )


# ===================================================================
# _parse_project_dir_from_line
# ===================================================================


def test_parse_project_dir_from_posix_log_line_preserves_linux_path_text() -> None:
    project_dir = _parse_project_dir_from_line(
        "[S6] Done /opt/aivideotrans/data/projects/demo/output",
        Path("D:/workspace/app"),
    )

    assert project_dir == "/opt/aivideotrans/data/projects/demo"


def test_parse_project_dir_from_windows_log_line() -> None:
    project_dir = _parse_project_dir_from_line(
        "[S6] Done D:\\workspace\\projects\\my_video\\output",
        Path("D:/workspace"),
    )

    assert project_dir is not None
    assert "my_video" in project_dir


def test_parse_project_dir_returns_none_for_no_path() -> None:
    assert _parse_project_dir_from_line("[S3] Translating...", Path(".")) is None


# ===================================================================
# _resolve_job_project_dir
# ===================================================================


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


def test_resolve_job_project_dir_prefers_preferred_project_dir(tmp_path: Path) -> None:
    preferred = tmp_path / "preferred_dir"
    preferred.mkdir()
    (preferred / "dummy.txt").write_text("exists")

    resolved = _resolve_job_project_dir(
        project_root=tmp_path,
        source_ref="https://youtube.example/watch?v=irrelevant",
        preferred_project_dir=str(preferred),
    )

    assert resolved == preferred.resolve(strict=False)


def test_resolve_job_project_dir_returns_none_when_nothing_found(tmp_path: Path) -> None:
    resolved = _resolve_job_project_dir(
        project_root=tmp_path,
        source_ref="https://youtube.example/watch?v=nonexistent",
        preferred_project_dir=None,
    )

    assert resolved is None


# ===================================================================
# _resolve_job_project_dir — workspace_dir priority tests
# ===================================================================


class TestFinalizeProjectDirResolution:
    """Verify _resolve_job_project_dir priority: project_dir > workspace_dir > legacy search."""

    def test_project_dir_wins_over_workspace_dir(self, tmp_path: Path):
        project_dir = tmp_path / "project_actual"
        project_dir.mkdir()
        workspace_dir = tmp_path / "projects" / "42" / "job_abc"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=irrelevant",
            preferred_project_dir=str(project_dir),
            workspace_dir=str(workspace_dir),
        )

        assert resolved == project_dir.resolve(strict=False)

    def test_workspace_dir_used_when_project_dir_missing(self, tmp_path: Path):
        workspace_dir = tmp_path / "projects" / "42" / "job_xyz"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=irrelevant",
            preferred_project_dir=None,
            workspace_dir=str(workspace_dir),
        )

        assert resolved == workspace_dir.resolve(strict=False)

    def test_workspace_dir_prevents_fallback_to_same_url_legacy_dir(self, tmp_path: Path):
        """Even if a legacy dir has matching URL, workspace_dir should win."""
        # Set up legacy dir with matching URL
        legacy_dir = _write_process_project(
            tmp_path / "projects" / "old_slug",
            youtube_url="https://youtube.example/watch?v=same-url",
        )
        # Set up workspace dir
        workspace_dir = tmp_path / "projects" / "42" / "job_new"
        workspace_dir.mkdir(parents=True)

        resolved = _resolve_job_project_dir(
            project_root=tmp_path,
            source_ref="https://youtube.example/watch?v=same-url",
            preferred_project_dir=None,
            workspace_dir=str(workspace_dir),
        )

        # Must return workspace, not legacy
        assert resolved == workspace_dir.resolve(strict=False)
        assert resolved != legacy_dir.resolve(strict=False)

    def test_legacy_search_used_when_both_dirs_missing(self, tmp_path: Path):
        """Legacy fallback only triggers when project_dir and workspace_dir are both None."""
        app_root = tmp_path / "app"
        app_root.mkdir()
        legacy_dir = _write_process_project(
            tmp_path / "app" / "projects" / "legacy_project",
            youtube_url="https://youtube.example/watch?v=legacy",
        )

        resolved = _resolve_job_project_dir(
            project_root=app_root,
            source_ref="https://youtube.example/watch?v=legacy",
            preferred_project_dir=None,
            workspace_dir=None,
        )

        assert resolved == legacy_dir.resolve(strict=False)

    def test_nonexistent_workspace_dir_falls_through_to_legacy(self, tmp_path: Path):
        """If workspace_dir path doesn't exist on disk, fall through to legacy search."""
        app_root = tmp_path / "app"
        app_root.mkdir()
        legacy_dir = _write_process_project(
            tmp_path / "app" / "projects" / "legacy_project",
            youtube_url="https://youtube.example/watch?v=fallthrough",
        )

        resolved = _resolve_job_project_dir(
            project_root=app_root,
            source_ref="https://youtube.example/watch?v=fallthrough",
            preferred_project_dir=None,
            workspace_dir="/nonexistent/workspace/path",
        )

        assert resolved == legacy_dir.resolve(strict=False)


# ===================================================================
# _build_command — source type / ref / workspace_dir
# ===================================================================


class TestBuildCommand:
    def test_youtube_url_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="youtube_url", source_ref="https://yt.com/v=abc")
        cmd = runner._build_command(job, continue_existing=False)

        assert "--source-type" in cmd
        assert cmd[cmd.index("--source-type") + 1] == "youtube_url"
        assert "--source-ref" in cmd
        assert cmd[cmd.index("--source-ref") + 1] == "https://yt.com/v=abc"
        # No longer as positional arg after "process"
        assert cmd[3] == "process"
        assert cmd[4] == "--source-type"

    def test_local_video_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="local_video", source_ref="/uploads/42/video.mp4")
        cmd = runner._build_command(job, continue_existing=False)

        assert cmd[cmd.index("--source-type") + 1] == "local_video"
        assert cmd[cmd.index("--source-ref") + 1] == "/uploads/42/video.mp4"

    def test_local_audio_uses_explicit_source_params(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(source_type="local_audio", source_ref="D:/input.wav")
        cmd = runner._build_command(job, continue_existing=False)

        assert cmd[cmd.index("--source-type") + 1] == "local_audio"
        assert cmd[cmd.index("--source-ref") + 1] == "D:/input.wav"

    def test_new_job_with_workspace_dir_passes_project_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(workspace_dir="projects/42/job_test001")
        cmd = runner._build_command(job, continue_existing=False)

        assert "--project-dir" in cmd
        assert cmd[cmd.index("--project-dir") + 1] == "projects/42/job_test001"

    def test_new_job_without_workspace_dir_omits_project_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job()
        cmd = runner._build_command(job, continue_existing=False)

        assert "--project-dir" not in cmd

    def test_continue_prefers_project_dir_over_workspace_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(
            project_dir="/resolved/actual/dir",
            workspace_dir="projects/42/job_test001",
        )
        cmd = runner._build_command(job, continue_existing=True)

        assert cmd[cmd.index("--project-dir") + 1] == "/resolved/actual/dir"

    def test_continue_falls_back_to_workspace_dir(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(workspace_dir="projects/42/job_test001")
        # project_dir is None
        cmd = runner._build_command(job, continue_existing=True)

        assert "--project-dir" in cmd
        assert cmd[cmd.index("--project-dir") + 1] == "projects/42/job_test001"

    def test_preserves_job_id_and_other_flags(self, tmp_path: Path):
        runner = _make_runner(tmp_path)
        job = _make_job(voice_a="voice-001", voice_b="voice-002")
        # Set transcription_method directly (from_dict doesn't parse it)
        job.transcription_method = "gemini"
        cmd = runner._build_command(job, continue_existing=False)

        assert "--job-id" in cmd
        assert "--voice-a" in cmd
        assert cmd[cmd.index("--voice-a") + 1] == "voice-001"
        assert "--voice-b" in cmd
        assert cmd[cmd.index("--voice-b") + 1] == "voice-002"
        assert "--transcription-method" in cmd
        assert cmd[cmd.index("--transcription-method") + 1] == "gemini"
        assert "--wait-for-review" in cmd

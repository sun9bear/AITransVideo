"""Job API cleanup delegate-mode contract tests.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §5.2 (P1.4)

When ``AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY=true`` is set, Job API's
``cleanup_expired_projects`` MUST NOT call ``shutil.rmtree`` on
project_dirs. Disk delete becomes Gateway-exclusive so the R2 parity
gate (gateway/project_cleanup.py) can't be bypassed.

JSON / events-file unlink stays (Job API owns the JSON store).
status flip in JSON file content stays (other readers depend on it).

The default (flag absent or false) is identical to pre-Stage B behavior:
rmtree the project_dir directly.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _write_expired_job(
    jobs_dir: Path,
    projects_root: Path,
    job_id: str,
    *,
    status: str = "succeeded",
) -> Path:
    """Write a JSON store entry whose expires_at is in the past, plus
    a real on-disk project_dir under projects_root. Returns the
    project_dir path so the test can verify rmtree behavior."""
    project_dir = projects_root / job_id
    project_dir.mkdir(parents=True, exist_ok=True)
    # Put a sentinel file so we can detect rmtree
    (project_dir / "sentinel.txt").write_text("present", encoding="utf-8")

    now = datetime.now(timezone.utc)
    expired = (now - timedelta(days=8)).isoformat()
    data = {
        "job_id": job_id,
        "status": status,
        "project_dir": str(project_dir),
        "created_at": expired,
        "expires_at": expired,
    }
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps(data), encoding="utf-8",
    )
    (jobs_dir / f"{job_id}.events.jsonl").write_text(
        "", encoding="utf-8",
    )
    return project_dir


def _reload_cleanup_with_env(monkeypatch, env: dict[str, str]):
    """Re-import cleanup module so the module-level ``DELEGATE_RMTREE_TO_GATEWAY``
    reads our test env value."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("services.web_ui.cleanup", None)
    import services.web_ui.cleanup as cleanup
    return cleanup


def _whitelist_root(monkeypatch, cleanup_module, root: Path):
    """Override the safe project root whitelist so the test's tmp_path
    is allowed for rmtree. Without this, _is_safe_project_dir rejects
    the tmp path and skips rmtree even outside delegate mode — which
    would make both branches look identical and hide regressions."""
    monkeypatch.setattr(
        cleanup_module, "_SAFE_PROJECT_ROOTS", (root,), raising=True,
    )


def test_default_mode_rmtree_project_dir(monkeypatch, tmp_path):
    """Flag absent / false → cleanup rmtree's the project_dir as before."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(jobs_dir))

    cleanup = _reload_cleanup_with_env(monkeypatch, {})
    _whitelist_root(monkeypatch, cleanup, projects_root)
    assert cleanup.DELEGATE_RMTREE_TO_GATEWAY is False

    project_dir = _write_expired_job(jobs_dir, projects_root, "job_default")

    result = cleanup.cleanup_expired_projects()

    # Project directory and sentinel both deleted
    assert not project_dir.exists()
    assert "job_default" in result["deleted_jobs"]
    assert str(project_dir) in result["deleted_projects"]
    # JSON files unlinked
    assert not (jobs_dir / "job_default.json").exists()
    assert not (jobs_dir / "job_default.events.jsonl").exists()


def test_delegate_mode_skips_rmtree(monkeypatch, tmp_path):
    """Flag on → project_dir survives, but JSON/events files are still
    cleaned up (Job API owns the JSON store regardless of mode)."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(jobs_dir))

    cleanup = _reload_cleanup_with_env(
        monkeypatch, {"AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY": "true"},
    )
    _whitelist_root(monkeypatch, cleanup, projects_root)
    assert cleanup.DELEGATE_RMTREE_TO_GATEWAY is True

    project_dir = _write_expired_job(jobs_dir, projects_root, "job_delegated")

    result = cleanup.cleanup_expired_projects()

    # Project directory and sentinel SURVIVED
    assert project_dir.exists(), "delegate mode must not rmtree project_dir"
    assert (project_dir / "sentinel.txt").exists()
    # And deleted_projects list does NOT include it
    assert str(project_dir) not in result["deleted_projects"]
    # JSON files still unlinked (Job API store cleanup is unaffected)
    assert not (jobs_dir / "job_delegated.json").exists()
    assert not (jobs_dir / "job_delegated.events.jsonl").exists()
    assert "job_delegated" in result["deleted_jobs"]


def test_delegate_mode_with_unsafe_path_still_safe(monkeypatch, tmp_path):
    """Defense in depth: even if delegate mode is set, an unsafe path
    must not trip the rmtree branch."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(jobs_dir))
    cleanup = _reload_cleanup_with_env(
        monkeypatch, {"AVT_CLEANUP_DELEGATE_RMTREE_TO_GATEWAY": "true"},
    )
    # Force an unsafe project path far away from the whitelist
    unsafe = tmp_path / "definitely_unsafe"
    unsafe.mkdir()
    (unsafe / "sentinel.txt").write_text("present")

    now = datetime.now(timezone.utc)
    data = {
        "job_id": "job_unsafe",
        "status": "succeeded",
        "project_dir": str(unsafe),
        "created_at": (now - timedelta(days=8)).isoformat(),
        "expires_at": (now - timedelta(days=8)).isoformat(),
    }
    (jobs_dir / "job_unsafe.json").write_text(json.dumps(data))
    (jobs_dir / "job_unsafe.events.jsonl").write_text("")

    result = cleanup.cleanup_expired_projects()

    # Unsafe dir survived (because delegate mode skipped EVEN BEFORE
    # the whitelist check)
    assert unsafe.exists()
    assert (unsafe / "sentinel.txt").exists()
    # JSON still cleaned
    assert "job_unsafe" in result["deleted_jobs"]


def test_cron_schedule_helper_returns_seconds_until_3am_beijing(monkeypatch):
    """B6: _seconds_until_next_3am_beijing returns positive seconds
    until the next 19:00 UTC instant, regardless of input time of day."""
    cleanup = _reload_cleanup_with_env(monkeypatch, {})

    # 30s before the target — but helper clamps to >=60s so a clock-
    # edge race doesn't busy-loop the sleeper.
    just_before = datetime(2026, 5, 12, 18, 59, 30, tzinfo=timezone.utc)
    s = cleanup._seconds_until_next_3am_beijing(just_before)
    assert s == 60.0, "clamp floor must protect against tight target"

    # Exactly at the target → roll to next day (~86400s)
    on_target = datetime(2026, 5, 12, 19, 0, 0, tzinfo=timezone.utc)
    s = cleanup._seconds_until_next_3am_beijing(on_target)
    assert 86300 < s < 86500

    # Mid-morning UTC (early afternoon Beijing)
    midmorning = datetime(2026, 5, 12, 8, 0, 0, tzinfo=timezone.utc)
    s = cleanup._seconds_until_next_3am_beijing(midmorning)
    assert 11 * 3600 - 60 < s < 11 * 3600 + 60  # ~11 hours

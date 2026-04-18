"""Unit tests for post-edit-aware cleanup + editing idle scanner.

Covers:
- cleanup uses explicit ``expires_at`` when present
- cleanup falls back to legacy ``updated_at + 7d`` rule when ``expires_at`` absent
- cleanup skips every protected status (queued/running/waiting_for_review/editing)
- editing_idle_scanner detects only editing jobs past the idle threshold
- editing_idle_scanner dispatches to callback with correct reason
- editing_idle_scanner separates cancelled / failed / raised
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.services.web_ui import cleanup as cleanup_module
from src.services.web_ui.cleanup import cleanup_expired_projects
from src.services.web_ui.editing_idle_scanner import (
    IDLE_THRESHOLD_HOURS,
    REASON_IDLE_AUTO,
    find_idle_editing_jobs,
    scan_editing_idle,
)


# --- fixtures -------------------------------------------------------------


def _write_job(jobs_dir: Path, job_id: str, **fields) -> Path:
    payload = {"job_id": job_id, "status": "succeeded"}
    payload.update(fields)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- cleanup.cleanup_expired_projects ------------------------------------


def test_cleanup_uses_explicit_expires_at(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    # One expired (expires_at in past), one fresh (expires_at in future)
    _write_job(
        tmp_path,
        "j_expired",
        status="succeeded",
        updated_at=_iso(now - timedelta(days=1)),  # legacy would think "1d old → keep"
        expires_at=_iso(now - timedelta(minutes=5)),  # but explicit says expired
    )
    _write_job(
        tmp_path,
        "j_fresh",
        status="succeeded",
        updated_at=_iso(now - timedelta(days=30)),  # legacy would think "expired"
        expires_at=_iso(now + timedelta(days=3)),   # but explicit says still live
    )

    result = cleanup_expired_projects()

    assert "j_expired" in result["deleted_jobs"]
    assert "j_fresh" not in result["deleted_jobs"]


def test_cleanup_falls_back_to_updated_at_plus_7d(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    # No expires_at — fallback to updated_at + 7d
    _write_job(
        tmp_path,
        "j_legacy_expired",
        status="succeeded",
        updated_at=_iso(now - timedelta(days=10)),  # 10d > 7d
    )
    _write_job(
        tmp_path,
        "j_legacy_fresh",
        status="succeeded",
        updated_at=_iso(now - timedelta(days=3)),  # 3d < 7d
    )

    result = cleanup_expired_projects()

    assert "j_legacy_expired" in result["deleted_jobs"]
    assert "j_legacy_fresh" not in result["deleted_jobs"]


def test_cleanup_skips_protected_statuses(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    # Every one of these has an expired expires_at — status alone decides skip.
    for status in ("queued", "running", "waiting_for_review", "editing"):
        _write_job(
            tmp_path,
            f"j_{status}",
            status=status,
            expires_at=_iso(now - timedelta(days=10)),
        )

    result = cleanup_expired_projects()

    for status in ("queued", "running", "waiting_for_review", "editing"):
        assert f"j_{status}" not in result["deleted_jobs"], (
            f"{status} job must not be deleted by TTL cleanup"
        )


def test_cleanup_handles_malformed_expires_at_via_legacy_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    _write_job(
        tmp_path,
        "j_bad_expires",
        status="succeeded",
        expires_at="not-a-date",
        updated_at=_iso(now - timedelta(days=10)),
    )

    result = cleanup_expired_projects()

    # Malformed expires_at must not silently keep the row alive forever;
    # fallback kicks in and the 10-day-old job gets deleted.
    assert "j_bad_expires" in result["deleted_jobs"]


def test_cleanup_skips_jobs_without_any_timestamp(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVIDEOTRANS_JOBS_DIR", str(tmp_path))
    _write_job(tmp_path, "j_no_time", status="succeeded")
    result = cleanup_expired_projects()
    assert "j_no_time" not in result["deleted_jobs"]


# --- editing_idle_scanner -------------------------------------------------


def test_find_idle_editing_jobs_detects_only_editing_past_threshold(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    # editing, idle past threshold → detect
    _write_job(
        tmp_path,
        "j_idle",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=IDLE_THRESHOLD_HOURS + 1)),
    )
    # editing, recently touched → skip
    _write_job(
        tmp_path,
        "j_fresh",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=1)),
    )
    # not editing (running) → skip even if very old
    _write_job(
        tmp_path,
        "j_running",
        status="running",
        editing_touched_at=_iso(now - timedelta(days=5)),
    )
    # editing but missing touched_at → conservative skip
    _write_job(
        tmp_path,
        "j_missing_touched",
        status="editing",
    )

    result = find_idle_editing_jobs(tmp_path, now)

    assert result == ["j_idle"]


def test_find_idle_editing_jobs_returns_empty_when_dir_missing(tmp_path) -> None:
    missing = tmp_path / "nope"
    result = find_idle_editing_jobs(missing, datetime.now(timezone.utc))
    assert result == []


def test_scan_editing_idle_dispatches_to_callback(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    _write_job(
        tmp_path,
        "j1",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=25)),
    )
    _write_job(
        tmp_path,
        "j2",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=30)),
    )

    calls: list[tuple[str, str]] = []

    def ok_callback(job_id: str, reason: str) -> bool:
        calls.append((job_id, reason))
        return True

    result = scan_editing_idle(now, ok_callback, jobs_dir=tmp_path)

    assert set(result["cancelled"]) == {"j1", "j2"}
    assert result["failed"] == []
    assert all(reason == REASON_IDLE_AUTO for _, reason in calls)


def test_scan_editing_idle_separates_failed(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    _write_job(
        tmp_path,
        "j_ok",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=25)),
    )
    _write_job(
        tmp_path,
        "j_fails",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=25)),
    )

    def partial_callback(job_id: str, reason: str) -> bool:
        return job_id == "j_ok"

    result = scan_editing_idle(now, partial_callback, jobs_dir=tmp_path)

    assert result["cancelled"] == ["j_ok"]
    assert result["failed"] == ["j_fails"]


def test_scan_editing_idle_handles_callback_exception(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    _write_job(
        tmp_path,
        "j_boom",
        status="editing",
        editing_touched_at=_iso(now - timedelta(hours=25)),
    )

    def raising_callback(job_id: str, reason: str) -> bool:
        raise RuntimeError("simulated cancel failure")

    result = scan_editing_idle(now, raising_callback, jobs_dir=tmp_path)

    assert result["cancelled"] == []
    assert result["failed"] == ["j_boom"]


def test_scan_editing_idle_default_callback_is_noop() -> None:
    """Phase 0 must not mutate state — the default callback returns False
    and never raises, so every candidate shows up under ``failed``."""
    # Use a non-existent directory so the scan returns zero candidates. We
    # only assert that calling the scanner with its default callback does
    # not raise and returns the expected shape.
    result = scan_editing_idle(datetime.now(timezone.utc), jobs_dir=Path("/nonexistent"))
    assert result == {"candidates": [], "cancelled": [], "failed": []}


def test_registered_cancel_callback_default_is_noop_until_phase_1() -> None:
    """Phase 1 T1-1 swaps this to the real handler. Phase 0 guard."""
    from src.services.web_ui.editing_idle_scanner import (
        _noop_cancel,
        registered_cancel_callback,
    )

    assert registered_cancel_callback is _noop_cancel

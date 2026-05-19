"""Tests for gateway/pan/_events.py (Phase 9 §T9.4 + CodeX 2026-05-19 P1b).

This module is the shared JSONL emit helper used by all four pan
executors. The helper:
  - Must write to ``{jobs_dir}/{job_id}.events.jsonl`` with stage='pan'.
  - Must round-trip cleanly via JSON.
  - Must NEVER raise — write failures log WARNING and return.
  - Must support all 8 pan.* event types declared in
    services.jobs.events.SUPPORTED_EVENT_TYPES without WARNING drift.

Integration of the helper into actual executors (backup_executor /
restore_executor / residue_cleanup / auth) is covered by their own
test suites continuing to pass — see tests/test_pan_backup_executor.py
etc. End-to-end verification that emits land during a real workflow
is part of Phase 10's manual smoke (``r2_observability.py --since 1h``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# sys.path bootstrap — gateway uses bare imports because the Dockerfile
# sets WORKDIR /opt/gateway.
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


def _read_event_lines(events_path: Path) -> list[dict]:
    """Helper: load all events from a JSONL file."""
    assert events_path.exists(), f"events file missing: {events_path}"
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_emit_pan_event_safe_writes_jsonl_with_pan_stage(tmp_path, monkeypatch):
    """The helper writes a single JSONL row with stage='pan' to the
    correct file under settings.jobs_dir."""
    # Point settings.jobs_dir at tmp_path so the writer lands here.
    import config
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path), raising=False)

    from pan._events import emit_pan_event_safe

    emit_pan_event_safe(
        job_id="job_abc",
        event_type="pan.backup.started",
        message="pan backup started",
        payload={"user_id": "u1", "backup_id": "b1"},
    )

    rows = _read_event_lines(tmp_path / "job_abc.events.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "job_abc"
    assert row["event_type"] == "pan.backup.started"
    assert row["stage"] == "pan"
    assert row["level"] == "info"
    assert row["payload"]["user_id"] == "u1"
    assert row["payload"]["backup_id"] == "b1"


def test_emit_pan_event_safe_respects_level_kwarg(tmp_path, monkeypatch):
    """level='error' must propagate so log viewers can color failures."""
    import config
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path), raising=False)

    from pan._events import emit_pan_event_safe

    emit_pan_event_safe(
        job_id="job_fail",
        event_type="pan.backup.failed",
        message="oops",
        payload={"reason": "tar checksum mismatch"},
        level="error",
    )

    rows = _read_event_lines(tmp_path / "job_fail.events.jsonl")
    assert rows[0]["level"] == "error"
    assert rows[0]["event_type"] == "pan.backup.failed"
    assert rows[0]["payload"]["reason"] == "tar checksum mismatch"


def test_emit_pan_event_safe_never_raises_on_writer_failure(tmp_path, monkeypatch):
    """If the underlying emit_download_event raises (e.g. import error,
    disk full simulated), this helper must swallow it. Pan executor flow
    relies on this — observability writes are best-effort."""
    import config
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path), raising=False)

    # Force the underlying writer to raise.
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic writer failure")

    import storage.event_log as event_log
    monkeypatch.setattr(event_log, "emit_download_event", boom)

    from pan._events import emit_pan_event_safe

    # Must NOT raise.
    emit_pan_event_safe(
        job_id="job_boom",
        event_type="pan.backup.started",
        message="test",
        payload={},
    )


@pytest.mark.parametrize("event_type", [
    "pan.backup.started",
    "pan.backup.succeeded",
    "pan.backup.failed",
    "pan.restore.started",
    "pan.restore.succeeded",
    "pan.restore.failed",
    "pan.token_revoked",
    "pan.residue_cleanup.completed",
])
def test_emit_pan_event_safe_accepts_all_eight_pan_types(
    tmp_path, monkeypatch, event_type,
):
    """All 8 pan.* event types declared in SUPPORTED_EVENT_TYPES must be
    accepted by the writer's allowlist WITHOUT triggering the drift
    WARNING (which would mean event_log.py and events.py disagree).
    """
    import config
    monkeypatch.setattr(config.settings, "jobs_dir", str(tmp_path), raising=False)

    # Capture any WARNING the writer logs about unknown event_type.
    warnings: list[str] = []

    import storage.event_log as event_log
    import logging
    real_logger_warning = event_log.logger.warning

    def capture(msg, *args, **kwargs):
        warnings.append(msg % args if args else msg)
        return real_logger_warning(msg, *args, **kwargs)

    monkeypatch.setattr(event_log.logger, "warning", capture)

    from pan._events import emit_pan_event_safe

    emit_pan_event_safe(
        job_id=f"job_{event_type.replace('.', '_')}",
        event_type=event_type,
        message="vocab test",
        payload={},
    )

    # No drift WARNING. (Other WARNINGs about disk writes etc. would
    # also be caught — they shouldn't fire on a writable tmp_path.)
    drift_warnings = [
        w for w in warnings if "unexpected event_type" in str(w)
    ]
    assert drift_warnings == [], (
        f"event_log.py rejected pan event {event_type!r} as drift "
        f"despite it being in SUPPORTED_EVENT_TYPES: {drift_warnings}"
    )

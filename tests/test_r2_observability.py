"""Tests for scripts/r2_observability.py.

The script must:
1. Faithfully count download.* / stream.* events across jobs/ JSONL files.
2. Tolerate bad JSON lines / unknown event_types without raising
   (matches JobStore.load_events fail-open semantics, CodeX P1 follow-up
   2026-05-12).
3. Honor the ``--since`` cutoff in UTC.
4. Keep its inlined event vocabulary in lockstep with
   ``services.jobs.events.SUPPORTED_EVENT_TYPES`` — if events.py grows a
   new download.* / stream.* type, the script must learn about it too.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Load the script as a module without putting scripts/ on sys.path
# permanently. The script is intentionally pure stdlib so this is safe.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "r2_observability.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "r2_observability", str(_SCRIPT_PATH),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["r2_observability"] = module
    spec.loader.exec_module(module)
    return module


r2obs = _load_script_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )


def _event(
    *,
    event_type: str,
    job_id: str = "job_test",
    created_at: str | None = None,
    **extra,
) -> dict:
    base = {
        "job_id": job_id,
        "event_type": event_type,
        "created_at": created_at or "2026-05-13T12:00:00+00:00",
        "level": "info",
        "stage": None,
        "status": None,
        "payload": {},
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. Basic aggregation
# ---------------------------------------------------------------------------


def test_iter_events_counts_download_and_stream_types(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "job_a.events.jsonl",
        [
            _event(event_type="download.redirect.r2_registry", job_id="job_a"),
            _event(event_type="download.redirect.r2_registry", job_id="job_a"),
            _event(event_type="stream.redirect.r2_registry", job_id="job_a"),
            _event(event_type="stream.fallback.local", job_id="job_a"),
        ],
    )
    _write_jsonl(
        tmp_path / "job_b.events.jsonl",
        [
            _event(event_type="download.fallback.local", job_id="job_b"),
            _event(event_type="stream.local.direct", job_id="job_b"),
        ],
    )

    counter, jobs, scanned, failed, skipped = r2obs.iter_events(
        tmp_path, cutoff=None,
    )

    assert counter["download.redirect.r2_registry"] == 2
    assert counter["download.fallback.local"] == 1
    assert counter["stream.redirect.r2_registry"] == 1
    assert counter["stream.fallback.local"] == 1
    assert counter["stream.local.direct"] == 1
    assert jobs == {"job_a", "job_b"}
    assert scanned == 2
    assert failed == 0
    assert skipped == 0


def test_iter_events_ignores_log_and_status_types(tmp_path: Path) -> None:
    """``log`` / ``status`` are valid event types but outside the R2
    observability surface — they shouldn't inflate any counter."""
    _write_jsonl(
        tmp_path / "job_a.events.jsonl",
        [
            _event(event_type="log", message="hello", job_id="job_a"),
            _event(event_type="status", status="running", job_id="job_a"),
            _event(event_type="download.redirect.r2_registry", job_id="job_a"),
        ],
    )

    counter, jobs, scanned, failed, skipped = r2obs.iter_events(
        tmp_path, cutoff=None,
    )

    assert sum(counter.values()) == 1
    assert counter["download.redirect.r2_registry"] == 1
    assert "log" not in counter
    assert "status" not in counter
    assert skipped == 0  # log/status are not "skipped", they're "out of scope"


# ---------------------------------------------------------------------------
# 2. Fail-open contract (matches JobStore.load_events)
# ---------------------------------------------------------------------------


def test_iter_events_tolerates_malformed_lines(tmp_path: Path) -> None:
    """Bad JSON / missing event_type rows are silently skipped — must NOT
    raise. Mirrors the CodeX P1 follow-up that made JobStore.load_events
    fail-open so the /jobs/{id}/logs endpoint stays alive under
    Gateway/App vocab drift."""
    path = tmp_path / "job_dirty.events.jsonl"
    path.write_text(
        "\n".join([
            json.dumps(_event(
                event_type="download.redirect.r2_registry",
                job_id="job_dirty",
            )),
            "this-is-not-json",
            json.dumps({"job_id": "job_dirty"}),  # no event_type
            json.dumps({"event_type": ""}),  # empty event_type
            json.dumps("a string, not a dict"),  # wrong type
            json.dumps(_event(
                event_type="future.hypothetical.event_type",  # unknown — out of scope
                job_id="job_dirty",
            )),
            json.dumps(_event(
                event_type="stream.redirect.r2_registry",
                job_id="job_dirty",
            )),
        ]) + "\n",
        encoding="utf-8",
    )

    counter, jobs, scanned, failed, skipped = r2obs.iter_events(
        tmp_path, cutoff=None,
    )

    assert counter["download.redirect.r2_registry"] == 1
    assert counter["stream.redirect.r2_registry"] == 1
    assert "future.hypothetical.event_type" not in counter
    assert skipped == 4  # bad JSON + missing/empty event_type + wrong type
    assert failed == 0


def test_iter_events_handles_empty_dir(tmp_path: Path) -> None:
    counter, jobs, scanned, failed, skipped = r2obs.iter_events(
        tmp_path, cutoff=None,
    )
    assert sum(counter.values()) == 0
    assert jobs == set()
    assert scanned == 0


# ---------------------------------------------------------------------------
# 3. --since cutoff
# ---------------------------------------------------------------------------


def test_iter_events_respects_cutoff(tmp_path: Path) -> None:
    """Events older than cutoff are excluded; newer ones counted."""
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(days=8)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    _write_jsonl(
        tmp_path / "job_a.events.jsonl",
        [
            _event(
                event_type="download.redirect.r2_registry",
                job_id="job_a",
                created_at=old,
            ),
            _event(
                event_type="stream.redirect.r2_registry",
                job_id="job_a",
                created_at=recent,
            ),
        ],
    )

    cutoff = now - timedelta(days=1)  # 1d window
    counter, jobs, *_ = r2obs.iter_events(tmp_path, cutoff=cutoff)

    assert counter["download.redirect.r2_registry"] == 0  # too old
    assert counter["stream.redirect.r2_registry"] == 1  # within window


def test_parse_since_relative_formats() -> None:
    assert r2obs.parse_since("all") is None
    now = datetime.now(timezone.utc)
    # Each unit should subtract correctly. Allow 5s tolerance because the
    # function calls datetime.now() internally.
    for arg, expected_delta in [
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
    ]:
        cutoff = r2obs.parse_since(arg)
        assert cutoff is not None
        actual_delta = now - cutoff
        assert abs((actual_delta - expected_delta).total_seconds()) < 5, (
            f"{arg}: expected delta {expected_delta}, got {actual_delta}"
        )


def test_parse_since_rejects_garbage() -> None:
    import pytest

    for bad in ["", "foo", "1", "1x", "h24", "-1d", "1.5h"]:
        with pytest.raises(SystemExit):
            r2obs.parse_since(bad)


# ---------------------------------------------------------------------------
# 4. Contract guard — vocab in sync with services.jobs.events
# ---------------------------------------------------------------------------


def test_script_event_vocab_in_sync_with_jobs_events() -> None:
    """If services.jobs.events.SUPPORTED_EVENT_TYPES grows a new
    download.* / stream.* / pan.* member, this script's inlined sets must
    learn about it too — otherwise the new type would silently disappear
    from R2 observability output.

    The script intentionally only tracks download.* / stream.* / pan.*
    (log / status / future top-level types are out of scope), so we
    filter by prefix when comparing.

    Plan 2026-05-14 §Phase 9 T9.5: extended from download/stream to
    download/stream/pan tri-prefix sync.
    """
    from services.jobs.events import SUPPORTED_EVENT_TYPES

    upstream_download = frozenset(
        t for t in SUPPORTED_EVENT_TYPES if t.startswith("download.")
    )
    upstream_stream = frozenset(
        t for t in SUPPORTED_EVENT_TYPES if t.startswith("stream.")
    )
    upstream_pan = frozenset(
        t for t in SUPPORTED_EVENT_TYPES if t.startswith("pan.")
    )

    missing_download = upstream_download - r2obs.DOWNLOAD_EVENT_TYPES
    extra_download = r2obs.DOWNLOAD_EVENT_TYPES - upstream_download
    missing_stream = upstream_stream - r2obs.STREAM_EVENT_TYPES
    extra_stream = r2obs.STREAM_EVENT_TYPES - upstream_stream
    missing_pan = upstream_pan - r2obs.PAN_EVENT_TYPES
    extra_pan = r2obs.PAN_EVENT_TYPES - upstream_pan

    assert not missing_download, (
        f"r2_observability.py missing download.* types known to "
        f"services.jobs.events: {sorted(missing_download)}. "
        f"Add them to DOWNLOAD_EVENT_TYPES (and likely DOWNLOAD_R2_SERVED "
        f"or DOWNLOAD_LOCAL_SERVED)."
    )
    assert not extra_download, (
        f"r2_observability.py has unknown download.* types: "
        f"{sorted(extra_download)}. Either fix the typo or add them to "
        f"services.jobs.events SUPPORTED_EVENT_TYPES."
    )
    assert not missing_stream, (
        f"r2_observability.py missing stream.* types known to "
        f"services.jobs.events: {sorted(missing_stream)}. "
        f"Add them to STREAM_EVENT_TYPES (and likely STREAM_R2_SERVED "
        f"or STREAM_LOCAL_SERVED)."
    )
    assert not extra_stream, (
        f"r2_observability.py has unknown stream.* types: "
        f"{sorted(extra_stream)}."
    )
    assert not missing_pan, (
        f"r2_observability.py missing pan.* types known to "
        f"services.jobs.events: {sorted(missing_pan)}. "
        f"Add them to PAN_EVENT_TYPES (and likely PAN_SUCCESS / PAN_FAILURE "
        f"/ PAN_STARTED / PAN_OTHER)."
    )
    assert not extra_pan, (
        f"r2_observability.py has unknown pan.* types: "
        f"{sorted(extra_pan)}. Either fix the typo or add them to "
        f"services.jobs.events SUPPORTED_EVENT_TYPES."
    )


def test_r2_served_and_local_served_partition_each_track() -> None:
    """Within each track (download/stream), R2_SERVED and LOCAL_SERVED
    must partition the full set — no overlap, no leftover.
    """
    assert (
        r2obs.DOWNLOAD_R2_SERVED | r2obs.DOWNLOAD_LOCAL_SERVED
        == r2obs.DOWNLOAD_EVENT_TYPES
    )
    assert not (r2obs.DOWNLOAD_R2_SERVED & r2obs.DOWNLOAD_LOCAL_SERVED)
    assert (
        r2obs.STREAM_R2_SERVED | r2obs.STREAM_LOCAL_SERVED
        == r2obs.STREAM_EVENT_TYPES
    )
    assert not (r2obs.STREAM_R2_SERVED & r2obs.STREAM_LOCAL_SERVED)


# ---------------------------------------------------------------------------
# 5. Rendering — smoke + JSON shape
# ---------------------------------------------------------------------------


def test_render_text_smoke(tmp_path: Path) -> None:
    """Text rendering produces non-empty output with expected section
    headers. Doesn't assert exact formatting — those rules can shift
    without breaking the script's purpose."""
    _write_jsonl(
        tmp_path / "j.events.jsonl",
        [
            _event(event_type="download.redirect.r2_registry"),
            _event(event_type="stream.fallback.local"),
        ],
    )
    counter, jobs, *rest = r2obs.iter_events(tmp_path, cutoff=None)
    out = r2obs.render_text(counter, jobs, "all", *rest)
    assert "Download" in out
    assert "Stream" in out
    assert "fallback.local" in out
    # Highlights section must trigger (we have 1 stream fallback).
    # Match the label + count loosely so we don't over-fit on whitespace.
    import re
    assert re.search(r"stream\.fallback\.local:\s+1\b", out), (
        f"expected stream fallback callout in highlights section, got:\n{out}"
    )


def test_render_json_machine_readable(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "j.events.jsonl",
        [
            _event(event_type="download.redirect.r2_registry"),
            _event(event_type="stream.redirect.r2_registry"),
            _event(event_type="stream.fallback.local"),
        ],
    )
    counter, jobs, *rest = r2obs.iter_events(tmp_path, cutoff=None)
    raw = r2obs.render_json(counter, jobs, "all", *rest)
    payload = json.loads(raw)
    assert payload["download"]["total"] == 1
    assert payload["download"]["r2_served"] == 1
    assert payload["stream"]["total"] == 2
    assert payload["stream"]["r2_served"] == 1
    assert payload["stream"]["local_served"] == 1
    assert payload["jobs_observed"] == 1


# ---------------------------------------------------------------------------
# 6. Entry point — main() smoke
# ---------------------------------------------------------------------------


def test_main_exits_2_on_missing_jobs_dir(tmp_path: Path, capsys) -> None:
    nonexistent = tmp_path / "no_such_dir"
    rc = r2obs.main(["--jobs-dir", str(nonexistent), "--since", "all"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not a directory" in captured.err


def test_main_runs_against_real_dir(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "j.events.jsonl",
        [_event(event_type="download.redirect.r2_registry")],
    )
    rc = r2obs.main([
        "--jobs-dir", str(tmp_path), "--since", "all", "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["download"]["r2_served"] == 1

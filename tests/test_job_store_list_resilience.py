"""Tests for JobStore.list_jobs resilience to stray files.

Regression for 2026-05-11 production incident:
  - Operator left a stray JSON sidecar (``_correct-jianying-names.json``)
    in the jobs/ directory while bulk-patching r2_artifacts.
  - JobStore.list_jobs globbed ``*.json``, hit the sidecar, called
    ``JobRecord.from_dict(non-record-dict)``, that raised → entire
    list_jobs aborted → Job API returned 500 → gateway list_jobs got
    upstream=0 → joined with PG empty → workspace UI blank for ALL
    users for ~25 minutes.

Two defenses:
  1. ``glob("job_*.json")`` instead of ``"*.json"`` so sidecars are
     skipped by pattern.
  2. Per-file try/except around parse + from_dict so even if a record
     IS named ``job_*.json`` but corrupt, only that record is dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_valid_job_dict(job_id: str = "job_abc") -> dict:
    return {
        "job_id": job_id,
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.com/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "succeeded",
        "service_mode": "studio",
        "created_at": "2026-05-11T00:00:00Z",
        "updated_at": "2026-05-11T00:00:00Z",
    }


def test_list_jobs_ignores_underscore_prefixed_json_files(tmp_path):
    """Sidecars like ``_correct-jianying-names.json`` must be skipped
    by the filename filter (underscore prefix = operator file)."""
    from services.jobs.store import JobStore

    # One valid job record using production naming (underscore)
    (tmp_path / "job_abc.json").write_text(
        json.dumps(_make_valid_job_dict("job_abc")), encoding="utf-8",
    )
    # And one using legacy/test fixture naming (hyphen) — also valid
    (tmp_path / "job-legacy.json").write_text(
        json.dumps(_make_valid_job_dict("job-legacy")), encoding="utf-8",
    )
    # An operator sidecar: dict but NOT a job record schema. The
    # leading underscore is what marks it as "do not parse".
    (tmp_path / "_correct-jianying-names.json").write_text(
        json.dumps({"job_xxx": "some_filename.zip"}), encoding="utf-8",
    )
    (tmp_path / "_patch.json").write_text(
        json.dumps({"sql": "UPDATE ..."}), encoding="utf-8",
    )

    store = JobStore(tmp_path)
    jobs = store.list_jobs()
    job_ids = {j.job_id for j in jobs}
    assert job_ids == {"job_abc", "job-legacy"}, (
        f"list_jobs should pick up job records (both job_* and job-* "
        f"conventions) and skip underscore-prefixed sidecars. "
        f"Got: {job_ids}"
    )


def test_list_jobs_skips_corrupt_job_record_but_keeps_others(tmp_path):
    """Even if a file is named ``job_*.json`` but contains malformed
    JSON or a non-record schema, list_jobs must skip THAT file and
    return the rest — not abort the whole list."""
    from services.jobs.store import JobStore

    # Two valid records
    (tmp_path / "job_aaa.json").write_text(
        json.dumps(_make_valid_job_dict("job_aaa")), encoding="utf-8",
    )
    (tmp_path / "job_bbb.json").write_text(
        json.dumps(_make_valid_job_dict("job_bbb")), encoding="utf-8",
    )
    # One that is malformed JSON
    (tmp_path / "job_corrupt.json").write_text(
        "{not valid json", encoding="utf-8",
    )
    # One that parses but is the wrong schema
    (tmp_path / "job_wrong_schema.json").write_text(
        json.dumps({"hello": "world"}), encoding="utf-8",
    )

    store = JobStore(tmp_path)
    jobs = store.list_jobs()  # must not raise
    job_ids = {j.job_id for j in jobs}
    assert job_ids == {"job_aaa", "job_bbb"}, (
        f"list_jobs should skip corrupt entries and return the rest. "
        f"Got: {job_ids}"
    )


def test_list_jobs_returns_empty_when_dir_only_has_sidecars(tmp_path):
    """No valid job records but plenty of operator files: list must
    return [] cleanly without raising."""
    from services.jobs.store import JobStore

    (tmp_path / "_patch.sql").write_text("UPDATE ...;", encoding="utf-8")
    (tmp_path / "_correct-names.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    store = JobStore(tmp_path)
    jobs = store.list_jobs()
    assert jobs == []


def test_list_jobs_logs_skip_for_corrupt_file(tmp_path, caplog):
    """Operator visibility: when a job_*.json file IS broken, we
    should log a WARNING so ops sees it in runtime_logs and can
    fix the underlying file."""
    import logging
    from services.jobs.store import JobStore

    (tmp_path / "job_aaa.json").write_text(
        json.dumps(_make_valid_job_dict("job_aaa")), encoding="utf-8",
    )
    (tmp_path / "job_bad.json").write_text("not json", encoding="utf-8")

    store = JobStore(tmp_path)
    with caplog.at_level(logging.WARNING, logger="services.jobs.store"):
        jobs = store.list_jobs()

    job_ids = {j.job_id for j in jobs}
    assert job_ids == {"job_aaa"}
    # Surfaced via the logger
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("job_bad.json" in m or "skipping" in m.lower() for m in warning_msgs), (
        f"Expected a WARNING about skipping job_bad.json; got: {warning_msgs}"
    )

"""Read JobRecord JSON files from Gateway side without importing services.jobs.

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §4.3

Why this exists
---------------

The R2 sweeper (``gateway/r2_artifact_sweeper.py``) needs to enumerate
succeeded jobs to drive proactive R2 push. The "natural" way would be
``from services.jobs.store import JobStore``, but per CLAUDE.md the
Gateway container deliberately does NOT install pydub / ffmpeg, and
``services.jobs.__init__.py`` (and its transitive imports) drag those
in. Importing ``JobStore`` from Gateway crashes at import time on the
production container.

This module sidesteps the problem by reading ``jobs/<id>.json`` files
with pure stdlib ``json`` + ``glob``. The schema is deliberately a
narrow projection of ``services.jobs.models.JobRecord`` — only the
fields the sweeper, mirror helper, and parity check actually need.
Adding a field upstream does NOT require touching this reader unless
sweeper logic also needs it.

Schema notes
------------

Mirrors these fields from JobRecord:

- ``status``                   — primary candidate filter ("succeeded")
- ``completed_at``             — order key + grace window
- ``project_dir``              — passed to publisher
- ``current_stage``            — included so mirror helper can sync PG
- ``edit_generation``          — stamped into the R2 object key
- ``jianying_draft_zip_path``  — non-null triggers jianying push
- ``service_mode``             — eligible-set selection (express/studio)

Resilience
----------

- Corrupt JSON → log + skip; never crashes the sweeper loop.
- Missing field → fall back to safe default (None / 0 / "").
- Concurrent writes from app container → atomic per-file (the JSON
  store writes via temp + rename per ``services.jobs.store``); we
  may briefly read a stale snapshot but never a half-flushed file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


# Same precedence as gateway/config.py — env var override, otherwise the
# production default. We don't import ``settings`` directly because this
# module is intentionally dependency-light (matches the docstring claim
# "pure stdlib"); some unit tests run without a populated settings object.
_JOBS_DIR_ENV = "AIVIDEOTRANS_JOBS_DIR"
_DEFAULT_JOBS_DIR = "/opt/aivideotrans/app/jobs"


@dataclass(frozen=True)
class JobJsonRecord:
    """Narrow projection of ``services.jobs.models.JobRecord``.

    Frozen so callers can use it as a dict key / cache key without
    accidentally mutating it. ``service_mode`` is here for the rare
    case where Gateway PG and JSON store disagree — the sweeper
    should prefer the JSON value because the JSON write happens
    closer to the policy snapshot moment.

    ``edit_generation`` is ``int | None`` (not ``int = 0``) on purpose:
    a missing field in the upstream payload must NOT silently mirror
    a 0 onto Gateway PG (production saw post-edit jobs whose PG row
    drifted to a stale generation because intercept_list_jobs
    constructed records with hardcoded ``edit_generation=0`` —
    plan 2026-05-07 follow-up after Day 2 stuck-job triage).
    """

    job_id: str
    status: str
    completed_at: datetime | None
    project_dir: str | None
    current_stage: str | None
    edit_generation: int | None
    jianying_draft_zip_path: str | None
    service_mode: str | None
    # Smart MVP P2 (plan §4.2 末段) — pipeline emits [SMART_STATE] markers
    # that runner writes into JobRecord.smart_state. mirror_job_terminal_state
    # then propagates this dict into the Gateway DB Job.smart_state column so
    # the F4 settle dispatcher (credits_service._settle_smart_job_credit_ledger)
    # can read credits_policy. None = upstream payload didn't carry the field
    # (legacy / express / studio job — never overwritten on the DB side).
    smart_state: dict | None = None

    @property
    def is_succeeded(self) -> bool:
        return self.status == "succeeded"


def _jobs_dir() -> Path:
    return Path(os.environ.get(_JOBS_DIR_ENV) or _DEFAULT_JOBS_DIR)


def parse_iso_timestamp(raw: object) -> datetime | None:
    """Parse a JobRecord ISO-8601 timestamp into an aware UTC datetime.

    JobRecord writes timestamps with ``utc_now_iso()`` which returns
    ``"...Z"``. ``datetime.fromisoformat`` accepts the explicit offset
    form, so we replace the trailing Z first. Also exported so
    ``gateway/job_intercept.py:intercept_list_jobs`` can convert
    upstream JSON dicts into ``JobJsonRecord`` shape when calling
    the shared ``mirror_job_terminal_state``.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


# Backward-compat alias used internally; new callers should use the
# exported name above.
_parse_completed_at = parse_iso_timestamp


def _coerce_int(raw: object, default: int = 0) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_int_or_none(raw: object) -> int | None:
    """Like ``_coerce_int`` but returns ``None`` when the field is
    missing or unparseable. Used for mirror-target fields where a
    silent default would clobber Gateway PG with a stale 0.
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_str_or_none(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _record_from_payload(path: Path, data: dict) -> JobJsonRecord:
    return JobJsonRecord(
        job_id=str(data.get("job_id") or path.stem),
        status=str(data.get("status") or ""),
        completed_at=_parse_completed_at(data.get("completed_at")),
        project_dir=_coerce_str_or_none(data.get("project_dir")),
        current_stage=_coerce_str_or_none(data.get("current_stage")),
        # Mirror target — None when field absent so we don't silently
        # write 0 onto a job that's actually at gen N+1 in Gateway PG
        # (post-edit drift, plan 2026-05-07 Day 2 follow-up).
        edit_generation=_coerce_int_or_none(data.get("edit_generation")),
        jianying_draft_zip_path=_coerce_str_or_none(
            data.get("jianying_draft_zip_path")
        ),
        service_mode=_coerce_str_or_none(data.get("service_mode")),
        # Smart MVP P2 — pass through verbatim. dict→dict; non-dict→None
        # (don't try to coerce; runner always writes a dict so any other
        # shape is corruption and skipping the mirror is the safe default).
        smart_state=(
            data.get("smart_state")
            if isinstance(data.get("smart_state"), dict)
            else None
        ),
    )


def iter_records(jobs_dir: Path | None = None) -> Iterator[JobJsonRecord]:
    """Yield every parseable record. Order: lexicographic by filename.

    ``jobs_dir`` parameter is for tests; production callers should
    omit it and let the env var resolve.
    """
    base = jobs_dir if jobs_dir is not None else _jobs_dir()
    if not base.is_dir():
        return
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("job_store_reader: skip %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        yield _record_from_payload(path, data)


def iter_succeeded_in_grace(
    now: datetime,
    grace_s: int = 30,
    jobs_dir: Path | None = None,
) -> Iterator[JobJsonRecord]:
    """Yield succeeded records whose ``completed_at < now - grace_s``.

    The grace period exists to dodge a race: ``process_runner`` sets
    status=succeeded *and then* writes the manifest. If we run the
    publisher within milliseconds of the status flip, the manifest
    file may not exist yet and the strict-manifest gate will record
    everything as ``failed``. 30s is well over the manifest write
    latency observed in production logs.
    """
    cutoff_ts = now.timestamp() - grace_s
    for rec in iter_records(jobs_dir=jobs_dir):
        if not rec.is_succeeded or rec.completed_at is None:
            continue
        if rec.completed_at.timestamp() >= cutoff_ts:
            continue
        yield rec


def find_record(job_id: str, jobs_dir: Path | None = None) -> JobJsonRecord | None:
    """Single-record lookup by job_id. Returns None if file missing."""
    base = jobs_dir if jobs_dir is not None else _jobs_dir()
    path = base / f"{job_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("job_store_reader: read failed %s (%s)", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return _record_from_payload(path, data)

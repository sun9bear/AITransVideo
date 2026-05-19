"""Gateway-side download event writer (plan 2026-04-23 §7 / §11.1).

Scope
-----
Best-effort JSONL append for download observability. Writes directly to
``{settings.jobs_dir}/{job_id}.events.jsonl`` using the same schema as
``services.jobs.events.JobEvent.to_dict()`` so the Job API's existing
``JobStore.load_events`` reads these records without any new plumbing.

Why hand-rolled instead of importing ``services.jobs.events``
-------------------------------------------------------------
Importing ``services.jobs.events`` triggers ``services/jobs/__init__.py``
which eagerly loads the pipeline stack (pydub, aligner, etc.) — deps the
gateway container intentionally does not carry (see
``display_name_orchestrator.py:30-35``). That import would raise, and the
broad ``except Exception`` below would silently swallow every download
event. Hand-rolling the JSONL write keeps the audit trail intact without
dragging those deps into the gateway image.

Semantics — these events are **routing-decision events, not success events**
----------------------------------------------------------------------------
The three event types (``download.redirect.r2`` / ``download.fallback.local``
/ ``download.local.direct``) are emitted **before** the downstream response
is produced. They answer "which backend did we route to?" not "did the user
successfully download bytes?". Rollout dashboards must interpret them
accordingly — e.g. a spike in ``download.fallback.local`` means R2 was
degraded at routing time, but says nothing about whether the local
byte-passthrough then succeeded.

Why not move them after ``proxy_request`` / ``RedirectResponse``: the
redirect path hands the response back to the ASGI runtime *before* we
would know whether the browser actually followed the 302; and the local
passthrough path streams bytes — instrumenting post-stream success would
need a response middleware, out of scope for Phase 2.

Downloads must never fail because the JSONL audit path is unavailable —
any error here is logged at WARNING and the request path continues.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from config import settings

logger = logging.getLogger(__name__)

# These event types are duplicated from ``services.jobs.events`` on purpose —
# see the module docstring for why we don't import from there. Keep this
# tuple in sync with ``SUPPORTED_EVENT_TYPES`` in that file.
# (Plan 2026-05-07 §4.7: ``download.redirect.r2_registry`` added to
# distinguish registry-driven 302 from legacy lazy-upload 302.)
# (Plan 2026-05-07 §11.3 C6 / Stage C: ``stream.*`` mirrors download.*
# for /stream/{kind} R2 redirect. The name is kept (_DOWNLOAD_EVENT_TYPES)
# for git-blame continuity; the contents now cover both surfaces.)
# (Plan 2026-05-14 §Phase 9 T9.2: ``pan.*`` covers admin pan backup
# lifecycle. Symbol name kept as ``_DOWNLOAD_EVENT_TYPES`` for git-blame
# continuity per plan §3.2 — the original Phase 2 R2 download writer
# was the first caller; this set is now the gateway-side allow-list
# for **all** non-pipeline event types. Contract test
# tests/test_pan_event_vocab_in_sync.py asserts the pan slice of this
# set matches the pan slice of SUPPORTED_EVENT_TYPES exactly.)
_DOWNLOAD_EVENT_TYPES = frozenset({
    "download.redirect.r2",
    "download.redirect.r2_registry",
    "download.fallback.local",
    "download.local.direct",
    "stream.redirect.r2",
    "stream.redirect.r2_registry",
    "stream.fallback.local",
    "stream.local.direct",
    "pan.backup.started",
    "pan.backup.succeeded",
    "pan.backup.failed",
    "pan.restore.started",
    "pan.restore.succeeded",
    "pan.restore.failed",
    "pan.token_revoked",
    "pan.residue_cleanup.completed",
})


def emit_download_event(
    job_id: str,
    event_type: str,
    *,
    message: str,
    payload: Mapping[str, object],
    jobs_dir: str | Path | None = None,
    stage: str = "download",
    level: str = "info",
) -> None:
    """Append one event line to ``{jobs_dir}/{job_id}.events.jsonl``.

    Despite the historical name, this is now the gateway's general-purpose
    JSONL append for any non-pipeline event type (download.* / stream.* /
    pan.*). The function name is retained per plan §3.2 for git-blame
    continuity — see ``_DOWNLOAD_EVENT_TYPES`` docstring above.

    Parameters
    ----------
    job_id
        Gateway-level job identifier. Must be non-empty after strip.
    event_type
        One of the allow-listed types in ``_DOWNLOAD_EVENT_TYPES``.
        Values outside the allow-list are *still written* (we don't
        want to drop audit data for a typo), but a WARNING is logged
        so tests / ops can catch drift.
    message
        Human-readable summary. ``None`` / empty → stored as ``null``.
    payload
        Free-form dict. Download callers pass ``artifact_key`` +
        ``backend``; pan callers pass ``user_id`` + ``backup_id`` +
        provider-specific fields.
    jobs_dir
        Override the default ``settings.jobs_dir`` — useful for tests
        that want an isolated directory. In production, callers always
        pass ``None`` (or omit) so the single source of truth is
        ``settings``.
    stage
        Plan 2026-05-14 §Phase 9: stage label for the event row. Mirrors
        ``JobEvent.stage`` semantics in ``services.jobs.events``. Existing
        download/stream callers default to "download"; pan callers pass
        "pan" so the dashboards / logs renderer can group by stage.
    level
        ``info`` / ``warn`` / ``error`` / ``critical`` (mirrors
        ``services.jobs.events.SUPPORTED_EVENT_LEVELS``). Defaults to
        ``info`` to preserve existing download path. Pan failure events
        should pass ``"error"``; ``pan.token_revoked`` typically uses
        ``"warn"``.

    Never raises — all exceptions are caught and logged at WARNING.
    """
    try:
        normalized_type = str(event_type).strip().lower()
        if normalized_type not in _DOWNLOAD_EVENT_TYPES:
            # Don't drop; just flag so drift between this module and
            # services.jobs.events.SUPPORTED_EVENT_TYPES surfaces in tests.
            logger.warning(
                "emit_download_event called with unexpected event_type=%r; "
                "this may indicate drift from services.jobs.events.SUPPORTED_EVENT_TYPES",
                event_type,
            )

        root = Path(jobs_dir) if jobs_dir is not None else Path(settings.jobs_dir)
        root.mkdir(parents=True, exist_ok=True)
        events_path = root / f"{str(job_id).strip()}.events.jsonl"

        record: dict[str, object] = {
            "job_id": str(job_id).strip(),
            "event_type": normalized_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": (str(message).strip() or None) if message else None,
            "stage": str(stage).strip() or "download",
            "status": None,
            "level": str(level).strip() or "info",
            "payload": dict(payload or {}),
        }

        # Atomic append: single write() of a pre-serialized line, newline
        # appended separately so a partial write shows up as a truncated
        # line rather than corrupting the previous record.
        serialized = json.dumps(record, ensure_ascii=False)
        with events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
    except Exception as exc:
        logger.warning(
            "download event write failed job=%s type=%s: %s",
            job_id, event_type, exc,
        )

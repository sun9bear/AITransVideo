"""User-edit audit log — append-only sidecar capturing translation review,
voice-selection review, and Studio post-edit user actions.

Plan: docs/plans/2026-05-04-user-edit-audit-data-optimization-plan.md

Design contract (P0):

- Third sink, alongside ``JobEvent`` (lifecycle) and ``UsageMeter`` (cost).
  Each has a single responsibility — see plan §5.3 for the boundary
  rationale. This module never substitutes for either.
- Append-only JSONL at ``{project_dir}/audit/user_edit_events.jsonl``.
- ``effective`` state is recorded by APPENDING an ``effective_marker``
  event that points back at prior event_ids (plan §4.5). We never rewrite
  history.
- ``schema_version=1`` on every event from day one. Off-line dataset
  builders MUST be forward-compatible (read unknown fields without
  crashing).
- Job API is the only writer. Gateway must NOT import this module — it
  would pull pydub through the services.jobs package. If a Gateway-native
  endpoint ever needs to write audit, mirror the stdlib-only pattern from
  ``gateway/storage/event_log.py``.

Best-effort policy (plan §12 P0 / §13.5):

- Audit append failures NEVER raise into the user-facing main path.
- Caller wraps observer.observe() in service-layer try/except (use
  ``safe_observe``); the observer implementation itself stays simple.
- On audit write failure, additionally emit a deduplicated
  ``JobEvent(level=WARN)`` with payload {"audit_write_failed": True, ...}.
  Per-(job_id, event_type) dedup window: 1 hour, in-memory (resets on
  process restart — that's intentional, restart noise is fine, but
  per-event flooding is not).

Copy_as_new policy (plan §13.6):

- ``audit/`` directory is NOT copied to a copy_as_new target. The new
  job starts with a fresh audit slate; offline analysis joins parent +
  child via ``root_job_id``. ``copy_service.prepare_copy_project_dir``
  enumerates copies explicitly, so this is satisfied by omission, but
  ``test_no_audit_in_copy_as_new`` locks the invariant in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
AUDIT_DIR_NAME = "audit"
AUDIT_EVENTS_FILENAME = "user_edit_events.jsonl"

# Stage values (event.stage) — keeps offline parsers from string-typing themselves.
STAGE_TRANSLATION_REVIEW = "translation_review"
STAGE_VOICE_SELECTION_REVIEW = "voice_selection_review"
STAGE_POST_EDIT = "post_edit"

# Event types — one constant per plan §7 listed event. Keep this list in
# sync with the dataset builder (P1) and any admin dashboard.
EVENT_TYPE_EFFECTIVE_MARKER = "effective_marker"

EVENT_TYPE_TRANSLATION_SPEAKER_CHANGED = "translation_segment_speaker_changed"
EVENT_TYPE_TRANSLATION_SEGMENT_SPLIT_CONFIRMED = "translation_segment_split_confirmed"
EVENT_TYPE_TRANSLATION_REVIEW_APPROVED = "translation_review_approved"

EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED = "voice_selection_speaker_reassigned"
EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED = "voice_selection_dubbing_mode_changed"
EVENT_TYPE_VOICE_SELECTION_APPROVED = "voice_selection_approved"

EVENT_TYPE_EDITING_SESSION_STARTED = "editing_session_started"
EVENT_TYPE_POST_EDIT_TEXT_CHANGED = "post_edit_text_changed"
EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED = "post_edit_segment_speaker_changed"
EVENT_TYPE_POST_EDIT_SEGMENT_SPLIT_CONFIRMED = "post_edit_segment_split_confirmed"
EVENT_TYPE_POST_EDIT_TTS_REGENERATED = "post_edit_tts_regenerated"
EVENT_TYPE_POST_EDIT_DRAFT_TTS_ACCEPTED = "post_edit_draft_tts_accepted"
EVENT_TYPE_POST_EDIT_DRAFT_TTS_DISCARDED = "post_edit_draft_tts_discarded"
EVENT_TYPE_POST_EDIT_VOICE_OVERRIDE_CHANGED = "post_edit_voice_override_changed"
EVENT_TYPE_POST_EDIT_CANCELLED = "post_edit_cancelled"
EVENT_TYPE_POST_EDIT_COMMITTED = "post_edit_committed"

# effective_reason values referenced by effective_marker events.
EFFECTIVE_REASON_APPROVED = "approved"
EFFECTIVE_REASON_TTS_ACCEPTED = "tts_accepted"
EFFECTIVE_REASON_COMMITTED = "committed"

# Env var holding the per-deployment salt for actor.user_id_hash. We never
# write the salt itself to disk — only sha256(user_id + salt). Salt missing
# means we record actor.user_id_hash=None instead of leaking raw user_id.
USER_ID_HASH_SALT_ENV = "AVT_AUDIT_USER_ID_SALT"


# ---------------------------------------------------------------------------
# Audit context — carried by callers so per-event boilerplate stays small
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuditContext:
    """Per-job carrier for fields that appear on every audit event.

    Construct once at the top of a service method (e.g. from JobRecord),
    pass to as many event builders as needed. The builders attach the
    job_id / lineage / actor identity; callers only fill the
    event-specific before/after/context blob.
    """

    job_id: str
    root_job_id: str | None = None
    project_id: str | None = None
    actor_user_id_hash: str | None = None

    @classmethod
    def from_job_record(cls, record: Any) -> "AuditContext":
        """Build an AuditContext from a JobRecord. Tolerant: missing
        attributes default to None — useful when callers pass partial
        objects (tests, orphan recovery)."""
        job_id = str(getattr(record, "job_id", "") or "").strip()
        root_job_id = (
            getattr(record, "root_job_id", None)
            or getattr(record, "job_id", None)
        )
        project_dir = getattr(record, "project_dir", None)
        project_id = Path(project_dir).name if project_dir else None
        user_id = getattr(record, "user_id", None)
        return cls(
            job_id=job_id,
            root_job_id=str(root_job_id) if root_job_id else None,
            project_id=project_id,
            actor_user_id_hash=hash_user_id(user_id) if user_id else None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_event_id() -> str:
    return uuid.uuid4().hex


def hash_user_id(user_id: str | None) -> str | None:
    """Return ``sha256(user_id + per-deployment salt)`` hex digest.

    Returns None if user_id is falsy OR the salt env var is unset — we
    refuse to write a hash that could be brute-forced offline because the
    salt was missing. Per plan §6: salt MUST come from env, never disk.
    """
    if not user_id:
        return None
    salt = os.environ.get(USER_ID_HASH_SALT_ENV, "").strip()
    if not salt:
        return None
    raw = f"{user_id}:{salt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def text_hash(text: str | None) -> str | None:
    """Stable, short text fingerprint for audit context.

    Uses sha256, truncated to 16 hex chars. Enough to detect duplicates
    and same-line edits across event sequences without retaining the
    actual text on disk (plan §4.4 desensitization)."""
    if text is None:
        return None
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def manifest_audio_fingerprint(tts_segments_dir: Path) -> str | None:
    """Plan §7.3 / §editing_session_started: a single hash representing the
    state of the per-segment TTS audio at session start.

    Hashes the *manifest* (sorted sequence of (filename, size, mtime_ns))
    of all wav files in the directory, not their bytes — fast enough to
    run synchronously inside enter_editing for 100+ segment jobs while
    still detecting any wav add/remove/rewrite between sessions.

    Returns None if the directory doesn't exist (legacy path) — callers
    record the absence in the event payload via ``baseline_audio_present``.
    """
    if not tts_segments_dir.exists() or not tts_segments_dir.is_dir():
        return None
    entries: list[tuple[str, int, int]] = []
    try:
        for entry in sorted(tts_segments_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".wav":
                continue
            stat = entry.stat()
            entries.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError as exc:
        logger.warning("manifest_audio_fingerprint walk failed for %s: %s", tts_segments_dir, exc)
        return None
    payload = json.dumps(entries, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class UserEditAuditWriter:
    """Append-only writer for one project's user_edit_events.jsonl.

    Construct from a project_dir; each call to ``append_event`` writes one
    JSON line. Event_id deduplication happens on the consumer side
    (P1 dataset builder); the writer never reads the file back to check.
    """

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).resolve(strict=False)
        self.audit_dir = self.project_dir / AUDIT_DIR_NAME
        self.events_path = self.audit_dir / AUDIT_EVENTS_FILENAME

    def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist one event, normalizing required fields if missing.

        Mutates a copy of ``event``; returns the normalized event so the
        caller can observe the assigned ``event_id`` / ``created_at``.
        Raises OSError if disk write fails — callers must wrap in
        ``safe_observe`` to convert that into a JobEvent warning.
        """
        normalized = dict(event)
        normalized.setdefault("event_id", _new_event_id())
        normalized.setdefault("schema_version", SCHEMA_VERSION)
        normalized.setdefault("created_at", _utc_now_iso())
        # Required fields — let absence raise so tests catch missing
        # event_type / job_id / stage at PR time rather than in prod.
        for required in ("event_type", "job_id", "stage"):
            if not normalized.get(required):
                raise ValueError(
                    f"user_edit_audit: required field '{required}' missing for event"
                )
        # effective defaults to False; callers set True only when emitting
        # a marker that itself signals effectiveness (e.g. committed event).
        normalized.setdefault("effective", False)
        normalized.setdefault("effective_reason", None)
        normalized.setdefault("usage_event_ids", [])

        self.audit_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return normalized


# ---------------------------------------------------------------------------
# Observer pattern
# ---------------------------------------------------------------------------


class AuditObserver(Protocol):
    """Plan §12 P0 / D1: service-layer callback so business mutations don't
    each call a writer directly. A fake observer is the natural test seam.

    Implementations MUST be tolerant — any state they need (file paths,
    DB sessions) must be ready before the observer is wired into the
    service. ``observe`` itself should NOT raise; if it does, the
    service-level ``safe_observe`` wrapper catches it (we keep observers
    simple by NOT requiring them to swallow their own exceptions, but
    ``safe_observe`` IS the single chokepoint that enforces the
    "audit never breaks main path" contract).
    """

    def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
        ...


class JsonlAuditObserver:
    """Default observer: writes to ``{project_dir}/audit/user_edit_events.jsonl``.

    Stateless other than the WeakValueDictionary cache of writers per
    project_dir (avoids re-stat'ing the audit dir on every event).
    """

    def __init__(self) -> None:
        self._writer_cache: dict[str, UserEditAuditWriter] = {}
        self._cache_lock = threading.Lock()

    def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
        writer = self._get_writer(project_dir)
        writer.append_event(event)

    def _get_writer(self, project_dir: Path) -> UserEditAuditWriter:
        key = str(Path(project_dir).resolve(strict=False))
        with self._cache_lock:
            writer = self._writer_cache.get(key)
            if writer is None:
                writer = UserEditAuditWriter(project_dir)
                self._writer_cache[key] = writer
            return writer


# ---------------------------------------------------------------------------
# Service-level safe-observe wrapper (the "exception isolation" chokepoint)
# ---------------------------------------------------------------------------


# In-memory dedup state for audit-write-failed JobEvent emission.
# Keyed by (job_id, event_type, failure_kind). Process-local: restarting
# the worker reopens the dedup window, which is acceptable noise.
_AUDIT_FAILURE_DEDUP_WINDOW_SECONDS = 3600
_audit_failure_seen_at: dict[tuple[str, str, str], float] = {}
_audit_failure_lock = threading.Lock()


def _should_emit_audit_failure_alarm(
    job_id: str, event_type: str, failure_kind: str
) -> bool:
    """Return True iff we have NOT emitted this kind of audit-failure
    JobEvent for this (job_id, event_type) within the dedup window."""
    key = (job_id, event_type, failure_kind)
    now = time.monotonic()
    with _audit_failure_lock:
        last = _audit_failure_seen_at.get(key)
        if last is not None and (now - last) < _AUDIT_FAILURE_DEDUP_WINDOW_SECONDS:
            return False
        _audit_failure_seen_at[key] = now
    return True


def reset_audit_failure_dedup_for_tests() -> None:
    """Test-only: clear the in-memory alarm-dedup state between tests."""
    with _audit_failure_lock:
        _audit_failure_seen_at.clear()


def safe_observe(
    observer: AuditObserver | None,
    *,
    project_dir: Path | str | None,
    event: dict[str, Any],
    job_event_emitter: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    """Service-layer call site for emitting one user-edit audit event.

    - Returns silently if observer is None or project_dir unresolved
      (audit is best-effort; callers' main path keeps going).
    - Catches ALL exceptions from observer.observe() so observer
      implementations can stay simple (no need for them to wrap their
      own try/except).
    - On failure: log warning + (if ``job_event_emitter`` provided) emit
      one deduplicated JobEvent(level=WARN) with payload signalling that
      audit writes are broken for this (job, event_type) pair.

    ``job_event_emitter(message, payload)`` is a thin shim the caller
    provides — typically ``lambda msg, payload: store.append_event(job_id,
    JobEvent(...))``. Kept as a callback so this module doesn't have to
    import services.jobs.events / store directly (and doesn't tie itself
    to a particular event type).
    """
    if observer is None or project_dir is None:
        return
    event_type = str(event.get("event_type") or "unknown")
    job_id = str(event.get("job_id") or "unknown")
    project_path = Path(project_dir)
    try:
        observer.observe(project_dir=project_path, event=event)
    except Exception as exc:  # noqa: BLE001
        failure_kind = type(exc).__name__
        logger.warning(
            "user_edit_audit observer failed for job=%s event_type=%s: %s",
            job_id,
            event_type,
            exc,
        )
        if job_event_emitter is None:
            return
        if not _should_emit_audit_failure_alarm(job_id, event_type, failure_kind):
            return
        try:
            job_event_emitter(
                "user-edit audit write failed; audit chain may be incomplete",
                {
                    "audit_write_failed": True,
                    "error_code": "audit_write_failed",
                    "error_class": "audit",
                    "audit_event_type": event_type,
                    "failure_kind": failure_kind,
                    "recoverable": True,
                    "dedup_window_seconds": _AUDIT_FAILURE_DEDUP_WINDOW_SECONDS,
                },
            )
        except Exception:  # noqa: BLE001
            # We tried our best to surface the failure; don't recurse.
            logger.exception("audit-failure JobEvent emission also failed")


# ---------------------------------------------------------------------------
# Event builders — one per plan §7 event. Each returns a fully-formed dict
# that callers either pass directly to ``safe_observe`` or stash for later
# (e.g., to compute totals before emitting an effective_marker).
# ---------------------------------------------------------------------------


def _base_event(ctx: AuditContext, *, event_type: str, stage: str) -> dict[str, Any]:
    """Common skeleton for every audit event.

    The defaults (``effective=False``, ``effective_reason=None``,
    ``usage_event_ids=[]``) are filled here so consumers reading a
    builder's output (datasets, tests) get the stable shape without
    having to round-trip through ``UserEditAuditWriter`` first. The
    writer's ``setdefault`` calls remain as a safety net for callers
    constructing events manually.
    """
    return {
        "event_id": _new_event_id(),
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now_iso(),
        "event_type": event_type,
        "stage": stage,
        "job_id": ctx.job_id,
        "root_job_id": ctx.root_job_id,
        "project_id": ctx.project_id,
        "actor": {
            "type": "user",
            "user_id_hash": ctx.actor_user_id_hash,
        },
        "effective": False,
        "effective_reason": None,
        "usage_event_ids": [],
    }


def build_translation_speaker_changed_event(
    ctx: AuditContext,
    *,
    segment_id: int | str,
    before_speaker_id: str,
    after_speaker_id: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    source_text_chars: int | None = None,
    cn_text_chars: int | None = None,
    asr_speaker_id: str | None = None,
    s2_speaker_id: str | None = None,
    neighbor_speakers: list[str] | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_TRANSLATION_SPEAKER_CHANGED,
        stage=STAGE_TRANSLATION_REVIEW,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": (end_ms - start_ms) if (start_ms is not None and end_ms is not None) else None,
    }
    event["before"] = {"speaker_id": before_speaker_id}
    event["after"] = {"speaker_id": after_speaker_id}
    event["context"] = {
        "source_text_chars": source_text_chars,
        "cn_text_chars": cn_text_chars,
        "asr_speaker_id": asr_speaker_id,
        "s2_speaker_id": s2_speaker_id,
        "neighbor_speakers": list(neighbor_speakers) if neighbor_speakers else [],
    }
    return event


def build_translation_segment_split_confirmed_event(
    ctx: AuditContext,
    *,
    original_segment_id: int | str,
    new_segment_ids: list[str | int],
    split_source_index: int | None,
    split_cn_index: int | None,
    speaker_a: str | None,
    speaker_b: str | None,
    original_speaker: str | None = None,
    child_a_chars: int | None = None,
    child_b_chars: int | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_TRANSLATION_SEGMENT_SPLIT_CONFIRMED,
        stage=STAGE_TRANSLATION_REVIEW,
    )
    event["segment"] = {"segment_id": str(original_segment_id)}
    event["before"] = {"speaker_id": original_speaker}
    event["after"] = {
        "child_segment_ids": [str(s) for s in new_segment_ids],
        "speaker_a": speaker_a,
        "speaker_b": speaker_b,
    }
    event["context"] = {
        "split_source_index": split_source_index,
        "split_cn_index": split_cn_index,
        "child_a_chars": child_a_chars,
        "child_b_chars": child_b_chars,
        "child_speakers_different": bool(speaker_a and speaker_b and speaker_a != speaker_b),
    }
    return event


def build_translation_review_approved_event(
    ctx: AuditContext,
    *,
    speaker_change_count: int = 0,
    split_count: int = 0,
    text_edit_count: int = 0,
    changed_segment_ratio: float | None = None,
    total_segments: int | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_TRANSLATION_REVIEW_APPROVED,
        stage=STAGE_TRANSLATION_REVIEW,
    )
    event["context"] = {
        "speaker_change_count": int(speaker_change_count),
        "split_count": int(split_count),
        "text_edit_count": int(text_edit_count),
        "changed_segment_ratio": changed_segment_ratio,
        "total_segments": total_segments,
    }
    event["effective"] = True
    event["effective_reason"] = EFFECTIVE_REASON_APPROVED
    return event


def build_voice_selection_speaker_reassigned_event(
    ctx: AuditContext,
    *,
    segment_id: int | str,
    from_speaker_id: str,
    to_speaker_id: str,
    duration_ms: int | None = None,
    speaker_duration_share: float | None = None,
    is_short_segment: bool | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED,
        stage=STAGE_VOICE_SELECTION_REVIEW,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": duration_ms,
    }
    event["before"] = {"speaker_id": from_speaker_id}
    event["after"] = {"speaker_id": to_speaker_id}
    event["context"] = {
        "speaker_duration_share": speaker_duration_share,
        "is_short_segment": is_short_segment,
    }
    return event


def build_voice_selection_dubbing_mode_changed_event(
    ctx: AuditContext,
    *,
    segment_id: int | str,
    speaker_id: str,
    before_mode: str | None,
    after_mode: str,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED,
        stage=STAGE_VOICE_SELECTION_REVIEW,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": duration_ms,
    }
    event["before"] = {"dubbing_mode": before_mode, "speaker_id": speaker_id}
    event["after"] = {"dubbing_mode": after_mode, "speaker_id": speaker_id}
    event["context"] = {}
    return event


def build_editing_session_started_event(
    ctx: AuditContext,
    *,
    segment_count: int | None = None,
    speaker_count: int | None = None,
    speaker_distribution: dict[str, dict[str, Any]] | None = None,
    baseline_audio_fingerprint: str | None = None,
    baseline_audio_present: bool = True,
    legacy_lazy_backfill: bool = False,
    edit_generation: int | None = None,
) -> dict[str, Any]:
    """Plan §7.3: NOT a user correction — a baseline anchor written when the
    user enters editing. Subsequent before/after diff events refer back to
    this. Carries an audio manifest-level hash (NOT per-segment list, plan
    §6 / §7.3 note) so a 100-segment job's marker stays small."""
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_EDITING_SESSION_STARTED,
        stage=STAGE_POST_EDIT,
    )
    event["context"] = {
        "segment_count": segment_count,
        "speaker_count": speaker_count,
        "speaker_distribution": speaker_distribution or {},
        "baseline_audio_fingerprint": baseline_audio_fingerprint,
        "baseline_audio_present": baseline_audio_present,
        "legacy_lazy_backfill": legacy_lazy_backfill,
        "edit_generation": edit_generation,
    }
    return event


def build_post_edit_text_changed_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    before_chars: int | None,
    after_chars: int | None,
    before_text_hash: str | None,
    after_text_hash: str | None,
    field: str = "cn_text",
    duration_ms: int | None = None,
    over_duration_budget: bool | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_TEXT_CHANGED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": duration_ms,
    }
    event["before"] = {
        "field": field,
        "chars": before_chars,
        "text_hash": before_text_hash,
    }
    event["after"] = {
        "field": field,
        "chars": after_chars,
        "text_hash": after_text_hash,
    }
    event["context"] = {
        "char_delta": (after_chars - before_chars) if (after_chars is not None and before_chars is not None) else None,
        "over_duration_budget": over_duration_budget,
    }
    return event


def build_post_edit_segment_speaker_changed_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    before_speaker_id: str,
    after_speaker_id: str,
    asr_speaker_id: str | None = None,
    s2_speaker_id: str | None = None,
    voice_selection_speaker_id: str | None = None,
    duration_ms: int | None = None,
    neighbor_prev_speaker: str | None = None,
    neighbor_next_speaker: str | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": duration_ms,
    }
    event["before"] = {"speaker_id": before_speaker_id}
    event["after"] = {"speaker_id": after_speaker_id}
    event["context"] = {
        "asr_speaker_id": asr_speaker_id,
        "s2_speaker_id": s2_speaker_id,
        "voice_selection_speaker_id": voice_selection_speaker_id,
        "neighbor_prev_speaker": neighbor_prev_speaker,
        "neighbor_next_speaker": neighbor_next_speaker,
    }
    return event


def build_post_edit_segment_split_confirmed_event(
    ctx: AuditContext,
    *,
    original_segment_id: str,
    new_segment_ids: list[str],
    split_source_index: int | None,
    split_cn_index: int | None,
    speaker_a: str | None,
    speaker_b: str | None,
    original_speaker: str | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_SEGMENT_SPLIT_CONFIRMED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {"segment_id": str(original_segment_id)}
    event["before"] = {"speaker_id": original_speaker}
    event["after"] = {
        "child_segment_ids": [str(s) for s in new_segment_ids],
        "speaker_a": speaker_a,
        "speaker_b": speaker_b,
    }
    event["context"] = {
        "split_source_index": split_source_index,
        "split_cn_index": split_cn_index,
        "child_speakers_different": bool(speaker_a and speaker_b and speaker_a != speaker_b),
    }
    return event


def build_post_edit_tts_regenerated_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    trigger_reason: str,
    provider: str | None = None,
    voice_id: str | None = None,
    model: str | None = None,
    target_duration_ms: int | None = None,
    draft_audio_duration_ms: int | None = None,
    success: bool = True,
    usage_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_TTS_REGENERATED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": target_duration_ms,
    }
    event["context"] = {
        "trigger_reason": trigger_reason,
        "provider": provider,
        "voice_id": voice_id,
        "model": model,
        "target_duration_ms": target_duration_ms,
        "draft_audio_duration_ms": draft_audio_duration_ms,
        "success": success,
    }
    event["usage_event_ids"] = list(usage_event_ids or [])
    return event


def build_post_edit_draft_tts_accepted_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    draft_audio_duration_ms: int | None = None,
    target_duration_ms: int | None = None,
    voice_id: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_DRAFT_TTS_ACCEPTED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": target_duration_ms,
    }
    event["context"] = {
        "draft_audio_duration_ms": draft_audio_duration_ms,
        "target_duration_ms": target_duration_ms,
        "voice_id": voice_id,
        "provider": provider,
    }
    event["effective"] = True
    event["effective_reason"] = EFFECTIVE_REASON_TTS_ACCEPTED
    return event


def build_post_edit_draft_tts_discarded_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    voice_id: str | None = None,
    provider: str | None = None,
    draft_audio_duration_ms: int | None = None,
    target_duration_ms: int | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_DRAFT_TTS_DISCARDED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {
        "segment_id": str(segment_id),
        "duration_ms": target_duration_ms,
    }
    event["context"] = {
        "voice_id": voice_id,
        "provider": provider,
        "draft_audio_duration_ms": draft_audio_duration_ms,
    }
    return event


def build_post_edit_voice_override_changed_event(
    ctx: AuditContext,
    *,
    segment_id: str,
    operation: str,
    before_voice_id: str | None,
    after_voice_id: str | None,
    before_provider: str | None = None,
    after_provider: str | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_VOICE_OVERRIDE_CHANGED,
        stage=STAGE_POST_EDIT,
    )
    event["segment"] = {"segment_id": str(segment_id)}
    event["before"] = {"voice_id": before_voice_id, "provider": before_provider}
    event["after"] = {"voice_id": after_voice_id, "provider": after_provider}
    event["context"] = {"operation": operation}
    return event


def build_post_edit_cancelled_event(
    ctx: AuditContext,
    *,
    cancel_reason: str,
    session_duration_seconds: float | None = None,
    edit_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_CANCELLED,
        stage=STAGE_POST_EDIT,
    )
    event["context"] = {
        "cancel_reason": cancel_reason,
        "session_duration_seconds": session_duration_seconds,
        "edit_counts": dict(edit_counts) if edit_counts else {},
    }
    return event


def build_post_edit_committed_event(
    ctx: AuditContext,
    *,
    strategy: str,
    edit_counts: dict[str, int] | None = None,
    target_job_id: str | None = None,
) -> dict[str, Any]:
    event = _base_event(
        ctx,
        event_type=EVENT_TYPE_POST_EDIT_COMMITTED,
        stage=STAGE_POST_EDIT,
    )
    event["context"] = {
        "strategy": strategy,
        "edit_counts": dict(edit_counts) if edit_counts else {},
        "target_job_id": target_job_id,
    }
    event["effective"] = True
    event["effective_reason"] = EFFECTIVE_REASON_COMMITTED
    return event


def build_effective_marker_event(
    ctx: AuditContext,
    *,
    stage: str,
    effective_reason: str,
    marked_event_ids: Iterable[str] | None = None,
    marked_event_id_range: tuple[str, str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan §4.5: append-only effectiveness marker. We never rewrite past
    events to flip ``effective=False -> True``; instead we append this
    marker and let offline parsers compute the joined view.

    Pass either ``marked_event_ids`` (small list) OR
    ``marked_event_id_range`` (the [first, last] event_ids of a contiguous
    range — preferred when the list would explode at e.g. 100+ TTS
    accept events from a batch regenerate).
    """
    event = _base_event(
        ctx, event_type=EVENT_TYPE_EFFECTIVE_MARKER, stage=stage,
    )
    context: dict[str, Any] = {"marked_event_ids": list(marked_event_ids or [])}
    if marked_event_id_range is not None:
        context["marked_event_id_range"] = list(marked_event_id_range)
    if extra_context:
        context.update(extra_context)
    event["context"] = context
    event["effective"] = True
    event["effective_reason"] = effective_reason
    return event

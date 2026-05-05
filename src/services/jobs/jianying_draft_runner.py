"""Background-thread runner for on-demand Jianying draft generation.

Hardened per docs/plans/2026-05-03-runner-and-llm-audit-hardening-plan.md §A:

- Cross-process mutual exclusion via ``services._file_lock.file_lock`` so a
  second worker / second process can't double-spawn the same job.
- Artifact-content fingerprint so an identical input set short-circuits
  (cache-hit returns existing zip without re-running the backend).
- Sub-step state (``validating_inputs / building_draft / ...``) persisted on
  ``JobRecord`` and emitted as ``EVENT_TYPE_STATUS`` JobEvents for admin
  dashboards / orphan diagnosis.
- ``reap_stale()`` performs orphan recovery: a stale ``running`` whose zip
  matches the current fingerprint is rescued to ``succeeded``; otherwise it
  is failed with a ``stale_running_reaped`` (or ``orphaned_after_process_restart``)
  error code so ops sees a CRITICAL JobEvent.

Idempotent triggers:
  - idle           -> start thread, set running, return running
  - running        -> return running (in-progress)
  - succeeded(hit) -> return existing zip path (cache hit on fingerprint+file)
  - succeeded(miss)-> start fresh thread with new attempt
  - failed         -> clear error, start new thread, return running
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from services._file_lock import file_lock
from services.jobs.events import (
    EVENT_LEVEL_CRITICAL,
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_TYPE_STATUS,
    JobEvent,
)

if TYPE_CHECKING:
    from modules.output.jianying.jianying_draft_backend import JianyingDraftBackend
    from services.jobs.models import JobRecord
    from services.jobs.store import JobStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Versioning constants — fed into fingerprint so artifact-equivalent inputs
# stop matching after a writer / backend upgrade. Bump when output format
# semantically changes (e.g. new draft.json field, new zip layout).
# ---------------------------------------------------------------------------

JIANYING_DRAFT_BACKEND_VERSION = "1"
JIANYING_DRAFT_WRITER_VERSION = "1"
# CodeX P1 (2026-05-05): bump to 2 — Whisper alignment admin policy
# snapshot was added to the fingerprint inputs.
# CodeX P1 follow-up (2026-05-05): bump to 3 — env capability bool
# was added to the policy snapshot too, completing the effective-gate
# coverage. Old (schema=2) fingerprints stored from the brief window
# between the two fixes are also invalidated — intentional, since
# they could have been computed with admin=true while env was off,
# producing a fingerprint that wouldn't notice env later flipping on.
# Admins / users may see a single one-time rebuild on the next
# trigger after rollout; subsequent triggers cache-hit cleanly.
JIANYING_DRAFT_FINGERPRINT_SCHEMA = 3

# Lock target lives under jobs_dir/_locks/jianying_draft/{job_id}.run.
# .lock sidecar is created automatically by services._file_lock.file_lock
# (it appends ".lock" to the path's suffix).
JIANYING_DRAFT_LOCK_SUBDIR = "_locks/jianying_draft"


# ---------------------------------------------------------------------------
# Sub-step enumeration (plan §A7)
# ---------------------------------------------------------------------------

SUBSTEP_VALIDATING_INPUTS = "validating_inputs"
SUBSTEP_RESOLVING_ARTIFACTS = "resolving_artifacts"
# 2026-05-05 D-3: optional substep before BUILDING_DRAFT. Only emitted
# when the whisper double-gate is open; otherwise we skip straight to
# BUILDING_DRAFT and the user never sees this stage. Wall time on the
# fast path (already aligned + fingerprint match) is < 1s; on slow
# path (cache miss) ~3× audio realtime — front-end progress indicator
# uses this substep label to set user expectations.
SUBSTEP_ALIGNING_SUBTITLES = "aligning_subtitles"
SUBSTEP_BUILDING_DRAFT = "building_draft"
SUBSTEP_VALIDATING_COMPATIBILITY = "validating_compatibility"
SUBSTEP_ZIPPING_DRAFT = "zipping_draft"
SUBSTEP_REGISTERING_ARTIFACT = "registering_artifact"
SUBSTEP_COMPLETED = "completed"
SUBSTEP_FAILED = "failed"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JianyingNotAllowedError(Exception):
    """Raised by trigger() when caller's request violates a precondition.

    Carries a ``reason`` field for the API layer to map to an HTTP status.

    Reason codes:
    - ``service_mode_not_studio`` — job.service_mode != "studio"  -> 403
    - ``job_not_succeeded``       — job.status != "succeeded"     -> 409 / 422
    - ``job_not_found``           — job_id does not exist         -> 404
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class JianyingEngineUnavailable(Exception):
    """Raised when the pyJianYingDraft engine is not available (not installed)."""


class JianyingInvalidDraftRoot(ValueError):
    """Raised when user_draft_root fails validation in trigger().

    K12 (API endpoint) catches this and returns HTTP 400.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _validate_user_draft_root(value: str) -> str:
    """Validate and normalise user_draft_root."""
    stripped = value.strip()
    if not stripped:
        raise JianyingInvalidDraftRoot(
            "user_draft_root must not be empty after stripping whitespace."
        )
    if len(stripped) > 500:
        raise JianyingInvalidDraftRoot(
            f"user_draft_root is too long ({len(stripped)} chars, max 500)."
        )
    if "\0" in stripped:
        raise JianyingInvalidDraftRoot(
            "user_draft_root must not contain null bytes."
        )
    lower = stripped.lower()
    for scheme in ("http://", "https://", "ftp://"):
        if lower.startswith(scheme):
            raise JianyingInvalidDraftRoot(
                f"user_draft_root looks like a URL ({stripped[:40]!r}); "
                "please provide a local filesystem path instead."
            )
    return stripped


def _sha256_file(path: Path | None) -> str:
    """SHA256 of file contents. Returns the literal string ``"missing"`` when
    the path is None or the file doesn't exist — keeping the fingerprint
    deterministic for legacy / partially-resolved manifests instead of
    raising mid-trigger."""
    if path is None:
        return "missing"
    try:
        if not path.exists():
            return "missing"
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "missing"


def _whisper_policy_snapshot() -> dict:
    """Effective gate snapshot for fingerprint input.

    Includes BOTH halves of the double-gate:
      - ``env_capability_enabled`` — ``AVT_WHISPER_ALIGN_ENABLED=1`` (ops
        capability switch). CodeX P1 follow-up #2 (2026-05-05): if this
        is omitted from the fingerprint, the natural rollout sequence
        (admin opts in BEFORE ops opens env) leaves cached proportional
        drafts undisturbed when env is later flipped on. Including it
        means env state changes also invalidate caches.
      - ``admin_*`` fields from admin_settings.json (D-1 + D-5).

    CodeX P1 (2026-05-05): admin policy alone in the fingerprint
    invalidates caches when admin flips the master switch / trigger /
    model / skip_cache. Adding env_capability_enabled completes the
    picture — any change to the EFFECTIVE gate (env AND admin)
    triggers a rebuild on next trigger.

    Read failure / missing file → returns the dataclass defaults (the
    reader is defensive); fingerprint stays stable. We never raise
    here — fingerprint computation is a hot path.
    """
    env_capability = os.environ.get("AVT_WHISPER_ALIGN_ENABLED", "") == "1"
    try:
        from services.admin_settings import read_whisper_alignment_settings
        s = read_whisper_alignment_settings()
        return {
            "env_capability_enabled": env_capability,
            "admin_enabled": s.enabled,
            "trigger": s.trigger,
            "skip_cache": s.skip_cache,
            "model": s.model,
        }
    except Exception:  # noqa: BLE001 — fingerprint must never crash trigger
        # Fall back to the documented defaults.
        return {
            "env_capability_enabled": env_capability,
            "admin_enabled": False,
            "trigger": "deliverable",
            "skip_cache": False,
            "model": "small",
        }


def _compute_jianying_fingerprint(
    job: "JobRecord", user_draft_root: str | None
) -> str | None:
    """Compute deterministic fingerprint of the inputs that go into the
    Jianying draft. Returns None when project_dir / manifest are missing —
    callers treat None as "can't form a stable input set, skip cache check".

    Fingerprint inputs (plan §A4 + 2026-05-05 D-followup):
    - SHA256 of source.original_video / editor.dubbed_audio_complete /
      editor.subtitles / editor.ambient_audio file contents.
    - Normalized user_draft_root.
    - Backend / writer version constants.
    - Fingerprint schema version.
    - **Whisper alignment policy snapshot** (enabled / trigger / model /
      skip_cache). When admin flips any of these, succeeded drafts on
      disk become cache-misses on next trigger so the new policy
      actually takes effect. Without this, admin's toggle has zero
      effect on jobs that already produced a proportional zip.

    Explicitly NOT included: full manifest.json hash (would include
    timestamps / mtime / unrelated artifacts and break legitimate cache hits).
    """
    if not job.project_dir:
        return None
    project_dir = Path(job.project_dir)
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    artifact_index = (
        manifest_data.get("artifact_index", {})
        if isinstance(manifest_data, dict)
        else {}
    )

    def _resolve(key: str) -> Path | None:
        raw = artifact_index.get(key)
        if not raw:
            return None
        return Path(str(raw))

    payload = {
        "artifact_hashes": {
            "source_video": _sha256_file(_resolve("source.original_video")),
            "dubbed_audio": _sha256_file(_resolve("editor.dubbed_audio_complete")),
            "subtitle_input": _sha256_file(_resolve("editor.subtitles")),
            "ambient_audio": _sha256_file(_resolve("editor.ambient_audio")),
        },
        "user_draft_root": (user_draft_root or "").strip(),
        "backend_version": JIANYING_DRAFT_BACKEND_VERSION,
        "writer_version": JIANYING_DRAFT_WRITER_VERSION,
        "artifact_schema": JIANYING_DRAFT_FINGERPRINT_SCHEMA,
        "whisper_alignment_policy": _whisper_policy_snapshot(),
    }
    serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class JianyingDraftRunner:
    """Background-thread runner for on-demand Jianying draft generation.

    See module docstring for hardening details (lock / fingerprint / substep
    / orphan recovery).
    """

    STALE_THRESHOLD_SECONDS = 1800  # 30 minutes; aligned with reap_stale orphan recovery

    def __init__(
        self,
        *,
        store: "JobStore",
        backend: "JianyingDraftBackend | None" = None,
    ) -> None:
        self._store = store
        self._backend = backend  # may be None — lazy default inside _run_in_background

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger(self, job_id: str, *, user_draft_root: str | None = None) -> dict:
        """Idempotent trigger.

        All read-modify-write of JobRecord.jianying_draft_* happens inside the
        cross-process file_lock (plan §A6) so a concurrent trigger from a
        second worker / second process can't double-spawn.
        """
        # Validate user_draft_root early (before touching the store / lock)
        if user_draft_root is not None:
            user_draft_root = _validate_user_draft_root(user_draft_root)

        job = self._store.require_job(job_id)  # raises KeyError if not found

        # Gate 1: must be a studio job (cheap pre-check; re-checked inside lock)
        if job.service_mode != "studio":
            raise JianyingNotAllowedError(
                "service_mode_not_studio",
                f"Jianying draft is only available for Studio mode jobs (got {job.service_mode!r}).",
            )

        # Gate 2: overall job must be succeeded
        if job.status != "succeeded":
            raise JianyingNotAllowedError(
                "job_not_succeeded",
                f"Jianying draft can only be triggered for succeeded jobs (got {job.status!r}).",
            )

        # Lock contract: only the *short* state-machine transition runs inside
        # the lock — read job, decide cache hit vs spawn, write transition,
        # emit one substep event. The lock is released BEFORE spawning the
        # worker, and the worker itself does NOT hold this lock for the
        # duration of backend.write (which can take minutes for large videos).
        # A concurrent trigger from another HTTP request / worker / process
        # therefore sees status=="running" and returns immediately, instead
        # of blocking on the lock for the full draft generation. Cross-process
        # double-spawn protection comes from this state-machine transition,
        # not from a long-held lock — the JobRecord status itself is the
        # mutual-exclusion signal. Orphan recovery (worker crashed mid-write)
        # is handled by reap_stale's STALE_THRESHOLD_SECONDS time-based gate.
        lock_path = self._lock_path_for(job_id)
        spawn_args: tuple | None = None
        response: dict | None = None
        with file_lock(lock_path):
            # Re-read inside the critical section — another worker may have
            # transitioned state since we read above.
            job = self._store.require_job(job_id)

            # Compute fingerprint up-front. None means we can't form a stable
            # input set (no project_dir / no manifest); caller falls through
            # to the background thread which will surface the real error.
            try:
                fingerprint = _compute_jianying_fingerprint(job, user_draft_root)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Fingerprint computation failed for job %s: %s — "
                    "skipping cache check, proceeding with regeneration.",
                    job_id,
                    exc,
                )
                fingerprint = None

            jd_status = job.jianying_draft_status

            # Already running — reject to avoid duplicate threads. Lock held
            # only for the read; release immediately so this HTTP path is fast.
            if jd_status == "running":
                response = {
                    "status": "running",
                    "started_at": job.jianying_draft_started_at,
                    "message": "still in progress",
                    "attempt_id": job.jianying_draft_attempt_id,
                    "substep": job.jianying_draft_substep,
                    "fingerprint": job.jianying_draft_fingerprint,
                }
            elif jd_status == "succeeded":
                # Already succeeded — return cached unless fingerprint or root differs.
                cached_root = job.jianying_draft_user_root
                cached_fingerprint = job.jianying_draft_fingerprint
                root_matches = (not user_draft_root) or user_draft_root == cached_root

                if cached_fingerprint is None:
                    # Legacy succeeded without fingerprint — preserve historical
                    # behavior: trust the cache when user_draft_root matches (or
                    # was not specified).
                    if root_matches:
                        response = {
                            "status": "succeeded",
                            "completed_at": job.jianying_draft_completed_at,
                            "draft_zip_path": job.jianying_draft_zip_path,
                            "artifact_key": "editor.jianying_draft_zip",
                            "fingerprint": None,
                            "_idempotent": True,
                        }
                else:
                    fingerprint_matches = (
                        fingerprint is not None and fingerprint == cached_fingerprint
                    )
                    zip_path = job.jianying_draft_zip_path
                    zip_exists = bool(zip_path) and Path(zip_path).exists()
                    if root_matches and fingerprint_matches and zip_exists:
                        response = {
                            "status": "succeeded",
                            "completed_at": job.jianying_draft_completed_at,
                            "draft_zip_path": zip_path,
                            "artifact_key": "editor.jianying_draft_zip",
                            "fingerprint": cached_fingerprint,
                            "_idempotent": True,
                        }
                # else: fingerprint mismatch / file gone / root changed — fall through

            if response is None:
                # idle / failed / succeeded-needs-rebuild → transition to running
                attempt_id = uuid.uuid4().hex
                job.jianying_draft_status = "running"
                job.jianying_draft_started_at = _utc_now_iso()
                job.jianying_draft_completed_at = None
                job.jianying_draft_error = None
                job.jianying_draft_attempt_id = attempt_id
                job.jianying_draft_fingerprint = fingerprint  # may be None on legacy paths
                job.jianying_draft_substep = SUBSTEP_VALIDATING_INPUTS
                self._store.save_job(job)

                self._emit_status_event(
                    job_id,
                    substep=SUBSTEP_VALIDATING_INPUTS,
                    attempt_id=attempt_id,
                    fingerprint=fingerprint,
                    user_draft_root=user_draft_root,
                    message="开始生成剪映草稿",
                    level=EVENT_LEVEL_INFO,
                )

                spawn_args = (job_id, user_draft_root, attempt_id)
                response = {
                    "status": "running",
                    "started_at": job.jianying_draft_started_at,
                    "attempt_id": attempt_id,
                    "substep": SUBSTEP_VALIDATING_INPUTS,
                    "fingerprint": fingerprint,
                }
        # --- lock released ---

        # Spawn AFTER releasing the lock so the worker doesn't have to wait
        # for our own HTTP-thread lock release ordering, and so a racing
        # second trigger doesn't observe us still holding the lock.
        if spawn_args is not None:
            threading.Thread(
                target=self._run_in_background,
                args=spawn_args,
                daemon=True,
                name=f"jianying-draft-{job_id}",
            ).start()

        return response

    def get_status(self, job_id: str) -> dict:
        """Return current jianying_draft_* fields as a dict for the API."""
        job = self._store.require_job(job_id)
        return {
            "status": job.jianying_draft_status,
            "started_at": job.jianying_draft_started_at,
            "completed_at": job.jianying_draft_completed_at,
            "error": job.jianying_draft_error,
            "artifact_key": (
                "editor.jianying_draft_zip"
                if job.jianying_draft_status == "succeeded"
                else None
            ),
            "draft_zip_path": job.jianying_draft_zip_path,
            "substep": job.jianying_draft_substep,
            "attempt_id": job.jianying_draft_attempt_id,
            "fingerprint": job.jianying_draft_fingerprint,
        }

    def reap_stale(self, now: datetime | None = None) -> int:
        """Orphan recovery for stale ``running`` JobRecords (plan §A6).

        For each job whose ``jianying_draft_status == "running"`` started more
        than ``STALE_THRESHOLD_SECONDS`` ago:

        - If a cached zip exists AND a freshly computed fingerprint matches
          the recorded ``jianying_draft_fingerprint``, transition to
          ``succeeded`` with error_code ``stale_running_recovered`` (warn).
        - Otherwise transition to ``failed`` with error_code
          ``orphaned_after_process_restart`` / ``stale_running_reaped``
          (CRITICAL — needs ops attention).

        Each transition runs inside the per-job ``file_lock`` so it can't race
        a still-alive worker that just hasn't been killed yet.

        ⚠️ MAINTENANCE NOTE — invocation cadence assumption:
        This method is designed for the CURRENT call pattern: invoked ONCE at
        Job API process startup, before any worker thread is alive. Because
        of that, "status==running but started_at older than threshold" is a
        sufficient signal of an orphan (the previous process crashed; we are
        the new process taking over).

        If you ever schedule reap_stale() as a periodic in-process task while
        the same process still has live workers, this signal becomes UNSAFE —
        a long but legitimate backend.write would look identical to an
        orphan, because the lock-based liveness signal was deliberately
        removed in the 2026-05-04 lock-granularity fix (the worker no longer
        holds a long lock you can probe). To enable periodic reaping you'd
        need either:
          (a) a worker heartbeat written under the per-job lock that reap
              checks for freshness, or
          (b) a status-machine guard that prevents reap from touching a
              record whose substep advanced within the threshold window.
        Do not just shorten STALE_THRESHOLD_SECONDS and run on a timer.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        threshold = now - timedelta(seconds=self.STALE_THRESHOLD_SECONDS)
        reaped = 0
        for job in self._store.list_jobs():
            if job.jianying_draft_status != "running":
                continue
            if not job.jianying_draft_started_at:
                continue
            try:
                started = datetime.fromisoformat(job.jianying_draft_started_at)
            except ValueError:
                logger.warning(
                    "reap_stale: corrupt jianying_draft_started_at for job %s: %r",
                    job.job_id,
                    job.jianying_draft_started_at,
                )
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started >= threshold:
                continue

            if self._recover_orphan(job.job_id, threshold=threshold):
                reaped += 1
        return reaped

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lock_path_for(self, job_id: str) -> Path:
        return self._store.root_dir / JIANYING_DRAFT_LOCK_SUBDIR / f"{job_id}.run"

    def _emit_status_event(
        self,
        job_id: str,
        *,
        substep: str,
        attempt_id: str | None,
        fingerprint: str | None,
        user_draft_root: str | None = None,
        message: str | None = None,
        level: str = EVENT_LEVEL_INFO,
        status: str = "running",
        error_code: str | None = None,
        error_class: str | None = None,
        recoverable: bool | None = None,
    ) -> None:
        """Append a JobEvent for the current sub-step. Best-effort: any
        failure to write the event is logged but never raises into the
        caller — the user-visible state machine takes precedence."""
        payload: dict[str, object] = {
            "substep": substep,
            "attempt_id": attempt_id,
            "fingerprint": fingerprint,
        }
        if user_draft_root is not None:
            payload["user_draft_root_mode"] = (
                "absolute" if Path(user_draft_root).is_absolute() else "relative"
            )
        if error_code is not None:
            payload["error_code"] = error_code
        if error_class is not None:
            payload["error_class"] = error_class
        if recoverable is not None:
            payload["recoverable"] = recoverable

        try:
            event = JobEvent(
                job_id=job_id,
                event_type=EVENT_TYPE_STATUS,
                created_at=_utc_now_iso(),
                stage="jianying_draft",
                status=status,
                message=message,
                level=level,
                payload=payload,
            )
            self._store.append_event(job_id, event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to append jianying_draft JobEvent for %s (substep=%s)",
                job_id,
                substep,
            )

    def _set_substep(
        self,
        job_id: str,
        attempt_id: str,
        substep: str,
        *,
        message: str | None = None,
        user_draft_root: str | None = None,
    ) -> None:
        """Persist substep on JobRecord + emit JobEvent.

        Takes the per-job file_lock for the duration of the read-modify-write
        + JobEvent append (a few ms). This is intentionally a *short*
        critical section so a concurrent trigger() call can serialize behind
        it and still respond quickly.
        """
        lock_path = self._lock_path_for(job_id)
        with file_lock(lock_path):
            job = self._store.require_job(job_id)
            # Tolerate concurrent recovery: only update if our attempt is still
            # the current one. Otherwise a stale background thread would clobber
            # state owned by a fresh trigger.
            if job.jianying_draft_attempt_id and job.jianying_draft_attempt_id != attempt_id:
                logger.warning(
                    "Substep update for %s skipped — attempt_id changed (mine=%s, current=%s)",
                    job_id,
                    attempt_id,
                    job.jianying_draft_attempt_id,
                )
                return
            job.jianying_draft_substep = substep
            self._store.save_job(job)
            self._emit_status_event(
                job_id,
                substep=substep,
                attempt_id=attempt_id,
                fingerprint=job.jianying_draft_fingerprint,
                user_draft_root=user_draft_root,
                message=message,
            )

    def _recover_orphan(self, job_id: str, *, threshold: datetime) -> bool:
        """Run orphan recovery for one job inside the per-job file_lock.

        Returns True when a transition was made.

        ``threshold`` is a tz-aware ``datetime``. We compare datetimes
        directly rather than ISO strings — string comparison would silently
        misbehave if any historical record was written with a different
        precision (microseconds present/absent), timezone offset format
        (+00:00 vs Z), or fractional-second style.
        """
        lock_path = self._lock_path_for(job_id)
        with file_lock(lock_path):
            job = self._store.require_job(job_id)
            # Re-check inside lock — another worker may have completed it.
            if job.jianying_draft_status != "running":
                return False
            if not job.jianying_draft_started_at:
                return False
            try:
                started = datetime.fromisoformat(job.jianying_draft_started_at)
            except ValueError:
                return False
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started >= threshold:
                # Re-fresh — not stale anymore (worker just completed).
                return False

            # Try to rescue: if a zip exists and its fingerprint matches,
            # converge to succeeded. Otherwise mark failed.
            cached_fingerprint = job.jianying_draft_fingerprint
            current_fingerprint = None
            try:
                current_fingerprint = _compute_jianying_fingerprint(
                    job, job.jianying_draft_user_root
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reap_stale: fingerprint recompute failed for %s", job_id
                )
            zip_path = job.jianying_draft_zip_path
            zip_exists = bool(zip_path) and Path(zip_path).exists()
            fingerprint_matches = (
                cached_fingerprint is not None
                and current_fingerprint is not None
                and cached_fingerprint == current_fingerprint
            )

            if zip_exists and fingerprint_matches:
                job.jianying_draft_status = "succeeded"
                job.jianying_draft_error = None
                job.jianying_draft_completed_at = _utc_now_iso()
                job.jianying_draft_substep = SUBSTEP_COMPLETED
                self._store.save_job(job)
                self._emit_status_event(
                    job_id,
                    substep=SUBSTEP_COMPLETED,
                    attempt_id=job.jianying_draft_attempt_id,
                    fingerprint=cached_fingerprint,
                    message="reap_stale 检测到 zip 已存在且指纹一致，恢复为 succeeded",
                    level=EVENT_LEVEL_WARN,
                    status="succeeded",
                    error_code="stale_running_recovered",
                    error_class="orphan_recovery",
                    recoverable=False,
                )
                logger.warning(
                    "reap_stale: recovered orphan jianying_draft for %s (zip + fingerprint match)",
                    job_id,
                )
                return True

            # Mark failed
            job.jianying_draft_status = "failed"
            job.jianying_draft_error = (
                "Process restart while generation was in progress; "
                "marked stale by startup reaper. Trigger again to retry."
            )
            job.jianying_draft_completed_at = _utc_now_iso()
            job.jianying_draft_substep = SUBSTEP_FAILED
            self._store.save_job(job)
            self._emit_status_event(
                job_id,
                substep=SUBSTEP_FAILED,
                attempt_id=job.jianying_draft_attempt_id,
                fingerprint=cached_fingerprint,
                message="reap_stale 回收 stale running 任务",
                level=EVENT_LEVEL_CRITICAL,
                status="failed",
                error_code=(
                    "orphaned_after_process_restart"
                    if cached_fingerprint is None
                    else "stale_running_reaped"
                ),
                error_class="orphan_recovery",
                recoverable=True,
            )
            logger.warning(
                "reap_stale: marked jianying_draft as failed for job %s "
                "(started_at=%s, threshold=%s)",
                job_id,
                job.jianying_draft_started_at,
                threshold.isoformat(),
            )
            return True

    def _run_in_background(
        self,
        job_id: str,
        user_draft_root: str | None,
        attempt_id: str,
    ) -> None:
        """Execute draft generation in a background thread.

        Lock contract: this method does NOT hold the per-job file_lock for
        the long-running backend.write call. Each substep transition takes
        the lock briefly via _set_substep / _mark_failed; the actual draft
        generation work (potentially minutes) runs lock-free. A concurrent
        trigger from another HTTP request / process therefore returns
        immediately with status=="running" rather than blocking on the lock.

        Cross-process double-spawn is prevented by the JobRecord state
        machine — a second trigger sees status=="running" and bails out.
        Orphan recovery (crashed worker) is handled by reap_stale's
        time-based STALE_THRESHOLD_SECONDS gate, not by lock liveness.
        """
        try:
            self._do_generate(
                job_id, user_draft_root=user_draft_root, attempt_id=attempt_id
            )
        except Exception:  # noqa: BLE001
            # Outermost guard: defensive — _do_generate already classifies
            # known exception types. Anything escaping here is genuinely
            # unexpected (e.g. import failure, MemoryError) and we still
            # need to leave the JobRecord in a clean failed state.
            logger.exception(
                "Jianying draft outer guard failed for %s (attempt=%s)",
                job_id,
                attempt_id,
            )
            self._mark_failed(
                job_id,
                attempt_id=attempt_id,
                user_draft_root=user_draft_root,
                error_message="fatal runner error escaped _do_generate",
                error_code="unexpected_exception",
                error_class="unknown",
                level=EVENT_LEVEL_CRITICAL,
                substep=SUBSTEP_FAILED,
            )

    def _do_generate(
        self,
        job_id: str,
        *,
        user_draft_root: str | None,
        attempt_id: str,
    ) -> None:
        """Inner worker — runs lock-free for the long backend.write call.

        Per the lock contract on _run_in_background: this method does NOT
        hold the per-job file_lock for backend.write. Each substep update
        (_set_substep) and the final succeeded / failed write each take the
        lock briefly on their own. Concurrent triggers for the same job
        therefore see status=="running" and return immediately rather than
        blocking on the worker.
        """
        try:
            self._set_substep(
                job_id,
                attempt_id,
                SUBSTEP_RESOLVING_ARTIFACTS,
                message="正在整理素材",
                user_draft_root=user_draft_root,
            )
            job = self._store.require_job(job_id)

            # 2026-05-05 D-3: ensure subtitles are whisper-aligned before
            # we package the draft. Helper is a no-op when the double-gate
            # is closed (env capability + admin policy) — preserves
            # today's proportional path for tenants with whisper off.
            # Open-gate path: ~1s if cache hit, ~3× audio-duration if
            # cache miss; surfaced to UI as the SUBSTEP_ALIGNING_SUBTITLES
            # progress label. Failure inside the helper would already
            # have been swallowed by cue_pipeline's fallback path; we
            # ALSO wrap in try/except here so a totally unexpected
            # exception (file IO, OOM) can never block draft generation.
            self._maybe_align_subtitles(
                job_id, attempt_id, job,
                user_draft_root=user_draft_root,
            )

            request = self._build_jianying_request(job, user_draft_root=user_draft_root)

            # Lazy-import backend to avoid pulling in pyJianYingDraft at module
            # import time (optional dependency).
            if self._backend is not None:
                backend = self._backend
            else:
                from modules.output.jianying.jianying_draft_backend import (
                    JianyingDraftBackend,
                )

                backend = JianyingDraftBackend()

            self._set_substep(
                job_id,
                attempt_id,
                SUBSTEP_BUILDING_DRAFT,
                message="正在写入剪映草稿",
                user_draft_root=user_draft_root,
            )

            result = backend.write(request)

            self._set_substep(
                job_id,
                attempt_id,
                SUBSTEP_VALIDATING_COMPATIBILITY,
                message="正在校验草稿兼容性",
                user_draft_root=user_draft_root,
            )

            # validation_status: ok / skipped_no_engine / skipped_missing_input / failed
            if result.validation_status == "ok":
                self._set_substep(
                    job_id,
                    attempt_id,
                    SUBSTEP_REGISTERING_ARTIFACT,
                    message="正在打包草稿",
                    user_draft_root=user_draft_root,
                )
                # Final-state write also runs in a short critical section so
                # a concurrent trigger doesn't read a half-updated record.
                lock_path = self._lock_path_for(job_id)
                with file_lock(lock_path):
                    final_job = self._store.require_job(job_id)
                    if (
                        final_job.jianying_draft_attempt_id
                        and final_job.jianying_draft_attempt_id != attempt_id
                    ):
                        logger.warning(
                            "Final-state write skipped for %s — attempt_id changed",
                            job_id,
                        )
                        return
                    # CodeX P1 (2026-05-05): re-compute the fingerprint
                    # AFTER ensure_helper has had a chance to rewrite
                    # editor.subtitles. The fingerprint stamped at
                    # trigger() time was based on pre-alignment SRT
                    # bytes; if we kept that, the next identical
                    # trigger would compute a different fingerprint
                    # (because the SRT is now whisper-aligned) and
                    # rebuild needlessly. Recomputing here means
                    # "stamp the fingerprint that matches the artifact
                    # set we actually shipped", so subsequent triggers
                    # cache-hit cleanly.
                    final_fp = _compute_jianying_fingerprint(
                        final_job, user_draft_root,
                    )
                    final_job.jianying_draft_status = "succeeded"
                    final_job.jianying_draft_zip_path = result.draft_zip_path
                    final_job.jianying_draft_completed_at = _utc_now_iso()
                    final_job.jianying_draft_error = None
                    final_job.jianying_draft_user_root = user_draft_root
                    final_job.jianying_draft_substep = SUBSTEP_COMPLETED
                    if final_fp is not None:
                        final_job.jianying_draft_fingerprint = final_fp
                    self._store.save_job(final_job)
                    self._emit_status_event(
                        job_id,
                        substep=SUBSTEP_COMPLETED,
                        attempt_id=attempt_id,
                        fingerprint=final_job.jianying_draft_fingerprint,
                        user_draft_root=user_draft_root,
                        message="剪映草稿生成成功",
                        status="succeeded",
                    )
                logger.info(
                    "Jianying draft succeeded for job %s: %s",
                    job_id,
                    result.draft_zip_path,
                )
            else:
                # skipped_no_engine / skipped_missing_input / failed all map to failed
                error_code, error_class, level = _classify_validation_status(
                    result.validation_status
                )
                self._mark_failed(
                    job_id,
                    attempt_id=attempt_id,
                    user_draft_root=user_draft_root,
                    error_message=(
                        f"backend returned {result.validation_status}: "
                        f"see {result.compatibility_report_path}"
                    ),
                    error_code=error_code,
                    error_class=error_class,
                    level=level,
                    substep=SUBSTEP_VALIDATING_COMPATIBILITY,
                )

        except FileNotFoundError as exc:
            logger.exception("Jianying draft missing artifact for %s", job_id)
            self._mark_failed(
                job_id,
                attempt_id=attempt_id,
                user_draft_root=user_draft_root,
                error_message=f"{type(exc).__name__}: {exc}",
                error_code="missing_manifest",
                error_class="precondition",
                level=EVENT_LEVEL_ERROR,
                substep=SUBSTEP_RESOLVING_ARTIFACTS,
            )
        except ValueError as exc:
            logger.exception("Jianying draft invalid input for %s", job_id)
            self._mark_failed(
                job_id,
                attempt_id=attempt_id,
                user_draft_root=user_draft_root,
                error_message=f"{type(exc).__name__}: {exc}",
                error_code="missing_source_artifact",
                error_class="precondition",
                level=EVENT_LEVEL_ERROR,
                substep=SUBSTEP_RESOLVING_ARTIFACTS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Jianying draft generation failed for %s", job_id)
            self._mark_failed(
                job_id,
                attempt_id=attempt_id,
                user_draft_root=user_draft_root,
                error_message=f"{type(exc).__name__}: {exc}",
                error_code="unexpected_exception",
                error_class="unknown",
                level=EVENT_LEVEL_CRITICAL,
                substep=SUBSTEP_FAILED,
            )

    def _mark_failed(
        self,
        job_id: str,
        *,
        attempt_id: str,
        user_draft_root: str | None,
        error_message: str,
        error_code: str,
        error_class: str,
        level: str,
        substep: str,
    ) -> None:
        """Mark JobRecord as failed + emit a typed JobEvent. Best-effort:
        store unreachable still gets logged. Wraps the read-modify-write
        in a short file_lock so it can't race a concurrent trigger."""
        lock_path = self._lock_path_for(job_id)
        try:
            with file_lock(lock_path):
                job = self._store.require_job(job_id)
                if (
                    job.jianying_draft_attempt_id
                    and job.jianying_draft_attempt_id != attempt_id
                ):
                    logger.warning(
                        "Mark-failed for %s skipped — attempt_id changed", job_id
                    )
                    return
                job.jianying_draft_status = "failed"
                job.jianying_draft_error = error_message
                job.jianying_draft_completed_at = _utc_now_iso()
                job.jianying_draft_substep = substep
                self._store.save_job(job)
                self._emit_status_event(
                    job_id,
                    substep=substep,
                    attempt_id=attempt_id,
                    fingerprint=job.jianying_draft_fingerprint,
                    user_draft_root=user_draft_root,
                    message=error_message,
                    level=level,
                    status="failed",
                    error_code=error_code,
                    error_class=error_class,
                    recoverable=True,
                )
        except Exception:
            logger.exception(
                "Failed to record jianying error for job %s — store unreachable",
                job_id,
            )

    def _maybe_align_subtitles(
        self,
        job_id: str,
        attempt_id: str,
        job,
        *,
        user_draft_root: str | None,
    ) -> None:
        """D-3: bring subtitles to whisper-aligned state if both gates open.

        Called from ``_do_generate`` after RESOLVING_ARTIFACTS, before
        BUILDING_DRAFT. The helper is a no-op (returns early with
        ``skipped_admin_disabled``) when the double-gate is closed —
        which is the production default — so tenants without whisper
        opt-in pay nothing. When the gates open, work happens here:

          - Cache hit (already whisper-aligned + fingerprint matches):
            ~1s wall-time, no subprocess. UI flickers SUBSTEP_ALIGNING_SUBTITLES.
          - Cache miss (cues are proportional, or audio re-TTS'd
            underneath stale whisper run): full re-run. ~3× audio
            realtime per uncached segment.

        Defensive: any exception from the helper is logged and swallowed
        — draft generation continues with whatever cues are currently
        on disk. The whisper path itself has its own fallback (cue
        pipeline drops back to proportional on whisper errors), so
        reaching this except is unlikely; defense-in-depth.

        2026-05-05 D-3: this is the FIRST entry point that triggers
        whisper alignment at deliverable time (instead of every publish).
        D-4 will add the same call to the materials_pack handler.
        """
        if not job.project_dir:
            return  # JobRecord without project_dir — can't run; let
                    # _build_jianying_request fail with a clearer error.

        # Pre-check the gates: if closed, skip the substep label
        # entirely so users never see a transient "正在精准对齐字幕" blip.
        # D-5: context="deliverable" — Jianying-draft is the prototypical
        # deliverable handoff. trigger ∈ {"publish", "deliverable"} both
        # permit; "manual" blocks the auto-trigger (admin must invoke
        # the manual endpoint instead).
        try:
            from modules.subtitles.cue_pipeline import _whisper_align_enabled
            if not _whisper_align_enabled(context="deliverable"):
                return
        except Exception:  # noqa: BLE001 — flag check should never fail loudly
            logger.exception(
                "whisper-align gate check failed for job %s; skipping align step",
                job_id,
            )
            return

        # Both gates open — surface progress to the user.
        self._set_substep(
            job_id,
            attempt_id,
            SUBSTEP_ALIGNING_SUBTITLES,
            message="正在精准对齐字幕（首次约 10 分钟）",
            user_draft_root=user_draft_root,
        )

        try:
            from services.subtitles.ensure_whisper_alignment import (
                ensure_whisper_aligned_subtitles,
            )
            status = ensure_whisper_aligned_subtitles(job.project_dir)
            logger.info(
                "whisper-align for job %s: action=%s elapsed=%dms blocks=%d",
                job_id,
                status.get("action"),
                status.get("elapsed_ms", 0),
                status.get("blocks_processed", 0),
            )
        except Exception:  # noqa: BLE001 — never block draft generation
            logger.exception(
                "whisper-align helper raised for job %s — proceeding with "
                "existing on-disk subtitles", job_id,
            )

    def _build_jianying_request(self, job, *, user_draft_root: str | None = None) -> "object":
        """Construct JianyingDraftRequest from JobRecord.

        Reads manifest.json from {project_dir}/manifest.json to resolve
        artifact paths.
        """
        from modules.output.jianying.jianying_draft_models import JianyingDraftRequest

        if not job.project_dir:
            raise ValueError(f"job {job.job_id} has no project_dir — cannot build JianyingDraftRequest")

        project_dir = Path(job.project_dir)
        manifest_path = project_dir / "manifest.json"

        if not manifest_path.exists():
            raise FileNotFoundError(
                f"manifest.json not found at {manifest_path} for job {job.job_id}"
            )

        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_index: dict[str, str] = manifest_data.get("artifact_index", {})

        source_video_path = artifact_index.get("source.original_video", "")
        dubbed_audio_path = artifact_index.get("editor.dubbed_audio_complete", "")
        subtitle_path = artifact_index.get("editor.subtitles", "")
        ambient_audio_path = artifact_index.get("editor.ambient_audio") or None

        project_title = job.display_name or job.job_id

        return JianyingDraftRequest(
            project_id=job.job_id,
            project_title=project_title,
            source_video_path=source_video_path,
            dubbed_audio_path=dubbed_audio_path,
            subtitle_path=subtitle_path,
            output_dir=str(project_dir),
            ambient_audio_path=ambient_audio_path,
            width=1920,
            height=1080,
            user_draft_root=user_draft_root,
        )


# ---------------------------------------------------------------------------
# Validation-status → (error_code, error_class, level) mapping
# ---------------------------------------------------------------------------


def _classify_validation_status(status: str) -> tuple[str, str, str]:
    """Map backend validation_status to (error_code, error_class, level)."""
    mapping = {
        "skipped_no_engine": ("engine_unavailable", "environment", EVENT_LEVEL_ERROR),
        "skipped_missing_input": ("missing_source_artifact", "precondition", EVENT_LEVEL_ERROR),
        "failed": ("compatibility_validation_failed", "validation", EVENT_LEVEL_ERROR),
    }
    return mapping.get(
        status, ("draft_write_failed", "writer", EVENT_LEVEL_ERROR)
    )

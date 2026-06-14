"""P3e-3c-2: preview→full smart-clone reuse contract (server-side).

plan 2026-06-14-p3e2-preview-lane-design.md §7. Validates that a finished
smart *preview* job's 600-credit clone was actually CAPTURED for the
**same** user, then retrieves the server-authoritative ``voice_id`` + original
source reference so a full paid job can reuse them WITHOUT re-cloning or
re-charging the 600. The frontend only sends ``reuse_preview_job_id``; this
module is the 越权 (overreach) defense — ``voice_id`` is NEVER taken from
the client.

Money / security invariants:
- ``voice_id`` is derived ONLY from a CAPTURED ``SmartCloneReservation``
  (status ``captured`` + ``settled_at`` set + ``captured_voice_id``) cross-
  checked against a chargeable ``CloneBillingEvent`` — reader B's 唯一权威
  计费信号. A reserve-then-released / never-cloned / denied preview yields
  no captured row → reject (never silently treated as "paid").
- ownership: the preview Job must belong to ``user_id`` (cross-user → reject).
- voice liveness: the cloned voice must still be a live ``UserVoice`` row
  (``expired_at IS NULL``) for that user.
- pure DB reads — no paid API, no writes. The caller (create path) does the
  ``request_data`` override and lets the normal full create flow charge
  minutes; this module decides nothing about money beyond "is the prior
  clone genuinely captured and reusable".
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from models import CloneBillingEvent, Job, SmartCloneReservation, UserVoice
from smart_clone_reservation_service import CAPTURED as _CAPTURED_STATUS

# Rejection reason codes (stable; surfaced to the create-path 4xx response).
REASON_NOT_FOUND = "preview_not_found"
REASON_FORBIDDEN = "preview_forbidden"
REASON_NOT_CAPTURED = "preview_clone_not_captured"
REASON_VOICE_UNAVAILABLE = "preview_voice_unavailable"
REASON_SOURCE_UNAVAILABLE = "preview_source_unavailable"


@dataclass(frozen=True)
class PreviewReuseResolution:
    """Server-derived inputs for the full reuse job (never client-supplied)."""

    preview_job_id: str
    voice_id: str
    source_type: str
    source_ref: str


async def resolve_preview_reuse(
    db, *, user_id, preview_job_id: str
) -> tuple[PreviewReuseResolution | None, str | None]:
    """Validate + resolve a preview→full reuse request.

    Returns ``(PreviewReuseResolution, None)`` on success, or
    ``(None, reason_code)`` on any rejection. Does NOT raise for validation
    failures — typed rejection so the caller maps to a 4xx response. No
    writes, no paid API.
    """
    pjid = str(preview_job_id or "").strip()
    if not pjid:
        return None, REASON_NOT_FOUND

    # 1. Preview Job must exist.
    job = (
        await db.execute(select(Job).where(Job.job_id == pjid))
    ).scalar_one_or_none()
    if job is None:
        return None, REASON_NOT_FOUND

    # 2. Ownership — 防越权：the preview must belong to the requesting user.
    #    Without this a user could pass another user's preview_job_id and
    #    reuse THEIR paid clone voice.
    if str(getattr(job, "user_id", "") or "") != str(user_id or ""):
        return None, REASON_FORBIDDEN

    # 3. Authoritative capture proof: a CAPTURED reservation for (task, user)
    #    with settled_at set + captured_voice_id present (reader B). A
    #    reserve-then-released or never-captured preview has no such row.
    reservation = (
        await db.execute(
            select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == pjid,
                SmartCloneReservation.user_id == user_id,
                SmartCloneReservation.status == _CAPTURED_STATUS,
            )
        )
    ).scalar_one_or_none()
    if (
        reservation is None
        or getattr(reservation, "settled_at", None) is None
        or not str(getattr(reservation, "captured_voice_id", "") or "").strip()
    ):
        return None, REASON_NOT_CAPTURED
    voice_id = str(reservation.captured_voice_id).strip()

    # 4. Cross-check the 唯一权威计费信号: a chargeable CloneBillingEvent for
    #    this reservation. Defense in depth — capture without a chargeable
    #    event would be an inconsistent ledger state; refuse to reuse on it.
    billing = (
        await db.execute(
            select(CloneBillingEvent).where(
                CloneBillingEvent.reservation_id == reservation.id,
                CloneBillingEvent.chargeable.is_(True),
            )
        )
    ).scalar_one_or_none()
    if billing is None:
        return None, REASON_NOT_CAPTURED

    # 5. Voice liveness: must still be a live UserVoice row for this user
    #    (not expired / soft-deleted).
    voice = (
        await db.execute(
            select(UserVoice).where(
                UserVoice.user_id == user_id,
                UserVoice.voice_id == voice_id,
                UserVoice.expired_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if voice is None:
        return None, REASON_VOICE_UNAVAILABLE

    # 6. Original source reference for reuse. P3e-3b only trimmed the derived
    #    ``preview_teaser.wav`` — the Job's source_ref still points at the
    #    original full-length source (YouTube URL / upload final_path).
    source_type = str(getattr(job, "source_type", "") or "").strip()
    source_ref = str(getattr(job, "source_ref", "") or "").strip()
    if not source_type or not source_ref:
        return None, REASON_SOURCE_UNAVAILABLE

    return (
        PreviewReuseResolution(
            preview_job_id=pjid,
            voice_id=voice_id,
            source_type=source_type,
            source_ref=source_ref,
        ),
        None,
    )


__all__ = [
    "PreviewReuseResolution",
    "resolve_preview_reuse",
    "REASON_NOT_FOUND",
    "REASON_FORBIDDEN",
    "REASON_NOT_CAPTURED",
    "REASON_VOICE_UNAVAILABLE",
    "REASON_SOURCE_UNAVAILABLE",
]

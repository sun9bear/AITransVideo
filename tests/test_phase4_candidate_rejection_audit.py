"""Phase 4 — voice candidate rejection audit on the Gateway approve handler.

Plan: docs/plans/2026-05-17-user-voice-candidate-first-plan.md §Phase 4
+ §计费和审计 ``smart_possible_user_voice_match_rejected``.

When Smart pipeline pauses on a possible (non-strong) personal-voice
candidate and the user picks a different voice (official catalog or new
clone), the Gateway POST /review/voice-selection/approve interceptor must
write a non-billable audit event so support/dispute can trace what was
offered vs picked.

These tests exercise ``_record_voice_candidate_rejection_events`` directly
because the wider approve handler depends on DB transactions and HTTP
plumbing that distract from the audit invariants. The handler-level
integration is locked by the existing ``test_b3e_fix_clone_decision_processing``
suite + this file's unit-level coverage of the audit helper.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
_SRC = _REPO / "src"
for _path in (_GATEWAY, _SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# job_intercept imports gateway.database at module load; stub it as the
# other gateway-unit tests do.
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _write_review_state_with_offered_candidates(
    project_dir: Path,
    *,
    offered_by_speaker: dict[str, list[dict]],
) -> None:
    """Write a review_state.json mirroring what process.py Phase 4 writes
    at the smart pause moment — voice_selection_review payload with
    speakers carrying ``smart_offered_candidates`` lists.
    """
    from services.review_state import (
        REVIEW_STATUS_PENDING,
        VOICE_SELECTION_REVIEW_STAGE,
        ReviewStateManager,
    )

    manager = ReviewStateManager(project_dir / "review_state.json")
    speakers_payload = []
    for sid, candidates in offered_by_speaker.items():
        speakers_payload.append({
            "speaker_id": sid,
            "voice_id": "auto",
            "voice_source": "auto_matched",
            "smart_offered_candidates": candidates,
        })
    manager.set_stage(
        VOICE_SELECTION_REVIEW_STAGE,
        status=REVIEW_STATUS_PENDING,
        payload={
            "speakers": speakers_payload,
            "smart_paused_reason": "possible_user_voice_match_requires_confirmation",
        },
        activate=True,
    )


def _fake_job(project_dir: Path, *, user_id: str = "user-1", job_id: str = "job-phase4") -> object:
    return SimpleNamespace(
        job_id=job_id,
        user_id=user_id,
        metering_snapshot={"project_dir": str(project_dir)},
    )


def _make_db_returning(user_voice: object | None) -> AsyncMock:
    """Build an AsyncMock DB that returns the same ``user_voice`` from
    every SELECT — the audit helper does one SELECT per offered
    candidate to fetch ``UserVoice.provider`` / ``source_*`` fields."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user_voice
    db.execute = AsyncMock(return_value=result)
    return db


class TestCandidateRejectionDetection:
    """``_record_voice_candidate_rejection_events`` writes a
    non-billable audit event when the user picks a voice that differs
    from any Smart-offered candidate."""

    def test_rejection_event_written_when_user_picks_different_voice(
        self, tmp_path: Path,
    ) -> None:
        """Plan §Phase 4 验收 #5: user picked a non-offered voice
        (official catalog or new clone) → emit one audit per offered
        candidate that was NOT chosen."""
        _write_review_state_with_offered_candidates(
            tmp_path,
            offered_by_speaker={
                "speaker_a": [
                    {
                        "voice_id": "vt_possible_one",
                        "user_voice_id": "7",
                        "label": "查理·芒格(其他视频)",
                        "confidence": "weak",
                        "match_scope": "cross_source_named",
                        "reason": "speaker_name_match_only",
                    },
                ],
            },
        )

        from job_intercept import _record_voice_candidate_rejection_events
        user_voice = SimpleNamespace(
            id="7",
            provider="minimax_voice_clone",
            source_content_hash="hash-xyz",
            source_speaker_id="speaker_a",
        )
        db = _make_db_returning(user_voice)
        job = _fake_job(tmp_path)
        speakers = [
            {
                "speaker_id": "speaker_a",
                "voice_id": "moss_audio_official_zh",
                "voice_reuse": False,
            },
        ]

        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        # Read the meter back to confirm event was written.
        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        events = meter.events
        reject_events = [e for e in events if e.get("model") == "voice_candidate_rejected"]
        assert len(reject_events) == 1
        event = reject_events[0]
        assert event["billable"] is False
        assert event["clone_count"] == 0
        assert event["voice_id"] == "vt_possible_one"
        assert event["speaker_id"] == "speaker_a"
        assert event["rejected_voice_id"] == "vt_possible_one"
        assert event["chosen_voice_id"] == "moss_audio_official_zh"
        assert event["rejected_match_confidence"] == "weak"
        assert event["user_action"] == "rejected"
        assert event["reuse"] is False
        assert (
            event["billing_policy"] == "candidate_rejected_no_clone_charge"
        )
        # Idempotent event ID with the rejected_voice_id, NOT the chosen one.
        assert (
            event["event_id"]
            == "voice_candidate_rejected:job-phase4:speaker_a:vt_possible_one"
        )
        # Source metadata from UserVoice DB record surfaces in extra.
        assert event["source_user_voice_id"] == "7"
        assert event["source_content_hash"] == "hash-xyz"
        assert event["source_speaker_id"] == "speaker_a"

    def test_rejection_skipped_when_user_picks_offered_candidate(
        self, tmp_path: Path,
    ) -> None:
        """When the user picks one of the offered candidates, that's
        a confirmation (audited via ``voice_reuse`` path), NOT a
        rejection. The detection must skip it."""
        _write_review_state_with_offered_candidates(
            tmp_path,
            offered_by_speaker={
                "speaker_a": [
                    {
                        "voice_id": "vt_possible_one",
                        "confidence": "medium",
                        "match_scope": "same_source_other_speaker",
                    },
                ],
            },
        )

        from job_intercept import _record_voice_candidate_rejection_events
        user_voice = SimpleNamespace(
            id="7", provider="minimax_voice_clone",
            source_content_hash=None, source_speaker_id=None,
        )
        db = _make_db_returning(user_voice)
        job = _fake_job(tmp_path)
        # User picked the offered voice. voice_reuse=True is the normal
        # signal — but even when explicit reuse flag is missing, an
        # identical voice_id should not be flagged as a rejection.
        speakers = [
            {
                "speaker_id": "speaker_a",
                "voice_id": "vt_possible_one",
                "voice_reuse": False,
            },
        ]

        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        reject_events = [
            e for e in meter.events
            if e.get("model") == "voice_candidate_rejected"
        ]
        assert reject_events == [], (
            "User picked the offered candidate — that's a confirmation, "
            "not a rejection. No audit event should be written."
        )

    def test_rejection_skipped_for_voice_reuse_true(
        self, tmp_path: Path,
    ) -> None:
        """``voice_reuse=True`` is the confirmation path — audited by
        ``_record_voice_reuse_events``, not this helper. Skip even if
        the picked voice differs from offered."""
        _write_review_state_with_offered_candidates(
            tmp_path,
            offered_by_speaker={
                "speaker_a": [
                    {"voice_id": "vt_possible_one", "confidence": "weak"},
                ],
            },
        )

        from job_intercept import _record_voice_candidate_rejection_events
        user_voice = SimpleNamespace(
            id="7", provider="minimax_voice_clone",
            source_content_hash=None, source_speaker_id=None,
        )
        db = _make_db_returning(user_voice)
        job = _fake_job(tmp_path)
        speakers = [
            {
                "speaker_id": "speaker_a",
                # picked a DIFFERENT voice but voice_reuse=True means
                # user confirmed reuse — rejection audit must skip.
                "voice_id": "vt_user_picked_clone",
                "voice_reuse": True,
            },
        ]

        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        reject_events = [
            e for e in meter.events
            if e.get("model") == "voice_candidate_rejected"
        ]
        assert reject_events == [], (
            "voice_reuse=True is the confirmation path. "
            "candidate_rejected audit must NOT fire here."
        )

    def test_rejection_idempotent_on_repeated_approve(
        self, tmp_path: Path,
    ) -> None:
        """Approve handler may run twice (network retry, polling
        re-approve, …). Event_id dedup must prevent double-recording
        the same rejection."""
        _write_review_state_with_offered_candidates(
            tmp_path,
            offered_by_speaker={
                "speaker_a": [
                    {"voice_id": "vt_possible_one", "confidence": "weak"},
                ],
            },
        )

        from job_intercept import _record_voice_candidate_rejection_events
        user_voice = SimpleNamespace(
            id="7", provider="minimax_voice_clone",
            source_content_hash=None, source_speaker_id=None,
        )
        db = _make_db_returning(user_voice)
        job = _fake_job(tmp_path)
        speakers = [
            {"speaker_id": "speaker_a", "voice_id": "other", "voice_reuse": False},
        ]

        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))
        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        reject_events = [
            e for e in meter.events
            if e.get("model") == "voice_candidate_rejected"
        ]
        assert len(reject_events) == 1, (
            "Double-approve must not produce duplicate audit events. "
            f"Got {len(reject_events)} events."
        )

    def test_no_review_state_short_circuits_to_no_op(
        self, tmp_path: Path,
    ) -> None:
        """When review_state.json doesn't exist (legacy job, sandbox
        race, etc.), the helper must NOT raise — best-effort audit."""
        from job_intercept import _record_voice_candidate_rejection_events
        db = _make_db_returning(None)
        job = _fake_job(tmp_path)
        speakers = [
            {"speaker_id": "speaker_a", "voice_id": "x", "voice_reuse": False},
        ]

        # No review_state.json file at project_dir → must return cleanly.
        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        # Nothing was recorded — assertion is "no crash + no event".
        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        assert meter.events == []

    def test_rejection_continues_when_user_voice_was_deleted(
        self, tmp_path: Path,
    ) -> None:
        """If the offered ``user_voice`` was deleted between pause and
        approve, the helper must still emit the audit (the rejection
        happened — we want the ledger record). Provider falls back to
        a sane default."""
        _write_review_state_with_offered_candidates(
            tmp_path,
            offered_by_speaker={
                "speaker_a": [
                    {
                        "voice_id": "vt_now_deleted",
                        "user_voice_id": "99",
                        "confidence": "weak",
                    },
                ],
            },
        )

        from job_intercept import _record_voice_candidate_rejection_events
        # DB returns None — user_voice has been deleted/expired.
        db = _make_db_returning(None)
        job = _fake_job(tmp_path)
        speakers = [
            {"speaker_id": "speaker_a", "voice_id": "other_clone", "voice_reuse": False},
        ]

        _run(_record_voice_candidate_rejection_events(
            db, job=job, speakers=speakers,
        ))

        from services.usage_meter import UsageMeter
        meter = UsageMeter(tmp_path, job_id="job-phase4")
        reject_events = [
            e for e in meter.events
            if e.get("model") == "voice_candidate_rejected"
        ]
        # Audit was still written — rejection happened, ledger record kept.
        assert len(reject_events) == 1
        # Fallback provider is the minimax_voice_clone default.
        assert reject_events[0]["provider"] == "minimax_voice_clone"
        # Fallback user_voice_id from the candidate metadata.
        assert reject_events[0]["source_user_voice_id"] == "99"

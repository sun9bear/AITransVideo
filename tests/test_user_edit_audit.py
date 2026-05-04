"""Tests for the user_edit_audit P0 module.

Plan: docs/plans/2026-05-04-user-edit-audit-data-optimization-plan.md

Coverage:
- UserEditAuditWriter: append, normalization, append-only, required fields,
  schema_version=1, default fields (effective=False, usage_event_ids=[])
- AuditObserver / JsonlAuditObserver: writes through to disk; same writer
  is reused per project_dir
- safe_observe: best-effort exception isolation; deduplicated audit-write-
  failed JobEvent emission via callback; never raises into caller
- hash_user_id: per-deployment salt enforced; missing salt -> None
- text_hash: stable, short fingerprint
- manifest_audio_fingerprint: directory-of-wavs deterministic hash
- AuditContext.from_job_record: tolerant of partial records
- effective_marker append-only semantics (separate event, no rewrite)
- Event builders: 11 P0 event types serialize the right shape
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from services.jobs.user_edit_audit import (
    AUDIT_DIR_NAME,
    AUDIT_EVENTS_FILENAME,
    EFFECTIVE_REASON_APPROVED,
    EFFECTIVE_REASON_COMMITTED,
    EFFECTIVE_REASON_TTS_ACCEPTED,
    EVENT_TYPE_EDITING_SESSION_STARTED,
    EVENT_TYPE_EFFECTIVE_MARKER,
    EVENT_TYPE_POST_EDIT_CANCELLED,
    EVENT_TYPE_POST_EDIT_COMMITTED,
    EVENT_TYPE_POST_EDIT_DRAFT_TTS_ACCEPTED,
    EVENT_TYPE_POST_EDIT_DRAFT_TTS_DISCARDED,
    EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED,
    EVENT_TYPE_POST_EDIT_SEGMENT_SPLIT_CONFIRMED,
    EVENT_TYPE_POST_EDIT_TEXT_CHANGED,
    EVENT_TYPE_POST_EDIT_TTS_REGENERATED,
    EVENT_TYPE_POST_EDIT_VOICE_OVERRIDE_CHANGED,
    EVENT_TYPE_TRANSLATION_REVIEW_APPROVED,
    EVENT_TYPE_TRANSLATION_SEGMENT_SPLIT_CONFIRMED,
    EVENT_TYPE_TRANSLATION_SPEAKER_CHANGED,
    EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED,
    EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED,
    SCHEMA_VERSION,
    STAGE_POST_EDIT,
    STAGE_TRANSLATION_REVIEW,
    STAGE_VOICE_SELECTION_REVIEW,
    USER_ID_HASH_SALT_ENV,
    AuditContext,
    JsonlAuditObserver,
    UserEditAuditWriter,
    build_editing_session_started_event,
    build_effective_marker_event,
    build_post_edit_cancelled_event,
    build_post_edit_committed_event,
    build_post_edit_draft_tts_accepted_event,
    build_post_edit_draft_tts_discarded_event,
    build_post_edit_segment_speaker_changed_event,
    build_post_edit_segment_split_confirmed_event,
    build_post_edit_text_changed_event,
    build_post_edit_tts_regenerated_event,
    build_post_edit_voice_override_changed_event,
    build_translation_review_approved_event,
    build_translation_segment_split_confirmed_event,
    build_translation_speaker_changed_event,
    build_voice_selection_dubbing_mode_changed_event,
    build_voice_selection_speaker_reassigned_event,
    hash_user_id,
    manifest_audio_fingerprint,
    reset_audit_failure_dedup_for_tests,
    safe_observe,
    text_hash,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup() -> None:
    reset_audit_failure_dedup_for_tests()
    yield
    reset_audit_failure_dedup_for_tests()


@pytest.fixture
def ctx() -> AuditContext:
    return AuditContext(
        job_id="job-test-001",
        root_job_id="job-root-001",
        project_id="project-001",
        actor_user_id_hash=None,
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class TestUserEditAuditWriter:
    def test_append_creates_audit_dir_and_jsonl(self, tmp_path: Path) -> None:
        writer = UserEditAuditWriter(tmp_path)
        writer.append_event({
            "event_type": "voice_selection_dubbing_mode_changed",
            "job_id": "job-1",
            "stage": STAGE_VOICE_SELECTION_REVIEW,
        })
        events_path = tmp_path / AUDIT_DIR_NAME / AUDIT_EVENTS_FILENAME
        assert events_path.is_file()
        line = events_path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["event_type"] == "voice_selection_dubbing_mode_changed"
        assert record["job_id"] == "job-1"
        assert record["stage"] == STAGE_VOICE_SELECTION_REVIEW

    def test_normalizes_event_id_schema_version_created_at(self, tmp_path: Path) -> None:
        writer = UserEditAuditWriter(tmp_path)
        result = writer.append_event({
            "event_type": "voice_selection_dubbing_mode_changed",
            "job_id": "job-1",
            "stage": STAGE_VOICE_SELECTION_REVIEW,
        })
        assert result["event_id"]
        assert result["schema_version"] == SCHEMA_VERSION
        assert result["created_at"]
        assert result["effective"] is False
        assert result["effective_reason"] is None
        assert result["usage_event_ids"] == []

    def test_append_is_append_only(self, tmp_path: Path) -> None:
        writer = UserEditAuditWriter(tmp_path)
        for i in range(5):
            writer.append_event({
                "event_type": "voice_selection_dubbing_mode_changed",
                "job_id": f"job-{i}",
                "stage": STAGE_VOICE_SELECTION_REVIEW,
            })
        events_path = tmp_path / AUDIT_DIR_NAME / AUDIT_EVENTS_FILENAME
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        ids = [json.loads(l)["job_id"] for l in lines]
        assert ids == [f"job-{i}" for i in range(5)]

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        writer = UserEditAuditWriter(tmp_path)
        with pytest.raises(ValueError, match="event_type"):
            writer.append_event({
                "job_id": "job-1",
                "stage": STAGE_POST_EDIT,
            })
        with pytest.raises(ValueError, match="job_id"):
            writer.append_event({
                "event_type": "post_edit_text_changed",
                "stage": STAGE_POST_EDIT,
            })
        with pytest.raises(ValueError, match="stage"):
            writer.append_event({
                "event_type": "post_edit_text_changed",
                "job_id": "job-1",
            })

    def test_caller_provided_event_id_preserved(self, tmp_path: Path) -> None:
        writer = UserEditAuditWriter(tmp_path)
        result = writer.append_event({
            "event_id": "custom-event-id-001",
            "event_type": "post_edit_committed",
            "job_id": "job-1",
            "stage": STAGE_POST_EDIT,
        })
        assert result["event_id"] == "custom-event-id-001"


# ---------------------------------------------------------------------------
# Observer + safe_observe
# ---------------------------------------------------------------------------


class TestJsonlAuditObserver:
    def test_observer_writes_to_project_dir_audit(self, tmp_path: Path) -> None:
        observer = JsonlAuditObserver()
        observer.observe(
            project_dir=tmp_path,
            event={
                "event_type": "post_edit_text_changed",
                "job_id": "job-1",
                "stage": STAGE_POST_EDIT,
            },
        )
        events_path = tmp_path / AUDIT_DIR_NAME / AUDIT_EVENTS_FILENAME
        assert events_path.is_file()

    def test_observer_caches_writer_per_project_dir(self, tmp_path: Path) -> None:
        observer = JsonlAuditObserver()
        observer.observe(
            project_dir=tmp_path,
            event={"event_type": "x", "job_id": "j", "stage": "s"},
        )
        first_writer = observer._get_writer(tmp_path)
        second_writer = observer._get_writer(tmp_path)
        assert first_writer is second_writer


class TestSafeObserve:
    def test_none_observer_silently_returns(self, tmp_path: Path) -> None:
        # Must not raise even though observer is None
        safe_observe(None, project_dir=tmp_path, event={"event_type": "x", "job_id": "j", "stage": "s"})

    def test_none_project_dir_silently_returns(self) -> None:
        observer = JsonlAuditObserver()
        # Must not raise — no project_dir means audit can't write anywhere
        safe_observe(observer, project_dir=None, event={"event_type": "x", "job_id": "j", "stage": "s"})

    def test_observer_exception_does_not_propagate(self, tmp_path: Path) -> None:
        class ExplodingObserver:
            def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
                raise RuntimeError("audit disk full")

        emitted: list[tuple[str, dict[str, Any]]] = []

        def emitter(message: str, payload: dict[str, Any]) -> None:
            emitted.append((message, payload))

        # Critical contract: this must not raise.
        safe_observe(
            ExplodingObserver(),
            project_dir=tmp_path,
            event={
                "event_type": "post_edit_text_changed",
                "job_id": "job-1",
                "stage": STAGE_POST_EDIT,
            },
            job_event_emitter=emitter,
        )

        # Failure surface: one JobEvent emitted with audit_write_failed payload
        assert len(emitted) == 1
        msg, payload = emitted[0]
        assert "audit" in msg.lower()
        assert payload["audit_write_failed"] is True
        assert payload["error_code"] == "audit_write_failed"
        assert payload["audit_event_type"] == "post_edit_text_changed"
        assert payload["failure_kind"] == "RuntimeError"

    def test_audit_failure_jobevent_deduped_within_window(self, tmp_path: Path) -> None:
        class ExplodingObserver:
            def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
                raise RuntimeError("nope")

        emitted: list[tuple[str, dict[str, Any]]] = []

        def emitter(message: str, payload: dict[str, Any]) -> None:
            emitted.append((message, payload))

        # Fire 5 audits with the same (job_id, event_type, failure_kind)
        for _ in range(5):
            safe_observe(
                ExplodingObserver(),
                project_dir=tmp_path,
                event={
                    "event_type": "post_edit_text_changed",
                    "job_id": "job-1",
                    "stage": STAGE_POST_EDIT,
                },
                job_event_emitter=emitter,
            )

        # Only 1 JobEvent emitted thanks to dedup; the other 4 silently logged
        assert len(emitted) == 1

    def test_audit_failure_dedup_distinguishes_event_types(self, tmp_path: Path) -> None:
        class ExplodingObserver:
            def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
                raise RuntimeError("nope")

        emitted: list[tuple[str, dict[str, Any]]] = []

        def emitter(message: str, payload: dict[str, Any]) -> None:
            emitted.append((message, payload))

        # Different event_types within the same job should each get their
        # own dedup slot — otherwise we'd suppress real signal.
        for et in (
            EVENT_TYPE_POST_EDIT_TEXT_CHANGED,
            EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED,
            EVENT_TYPE_POST_EDIT_TTS_REGENERATED,
        ):
            safe_observe(
                ExplodingObserver(),
                project_dir=tmp_path,
                event={"event_type": et, "job_id": "job-1", "stage": STAGE_POST_EDIT},
                job_event_emitter=emitter,
            )

        assert len(emitted) == 3

    def test_emitter_failure_does_not_recurse(self, tmp_path: Path) -> None:
        """If both observer AND the JobEvent emitter explode, safe_observe
        still must not raise — main path is the priority."""
        class ExplodingObserver:
            def observe(self, *, project_dir: Path, event: dict[str, Any]) -> None:
                raise RuntimeError("disk full")

        def broken_emitter(message: str, payload: dict[str, Any]) -> None:
            raise OSError("emitter also broken")

        # No raise expected
        safe_observe(
            ExplodingObserver(),
            project_dir=tmp_path,
            event={"event_type": "post_edit_text_changed", "job_id": "j", "stage": STAGE_POST_EDIT},
            job_event_emitter=broken_emitter,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHashUserId:
    def test_returns_none_when_user_id_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(USER_ID_HASH_SALT_ENV, "salty")
        assert hash_user_id(None) is None
        assert hash_user_id("") is None

    def test_returns_none_when_salt_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(USER_ID_HASH_SALT_ENV, raising=False)
        # Refuse to hash without salt — would be brute-forceable offline
        assert hash_user_id("user-123") is None

    def test_deterministic_with_salt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(USER_ID_HASH_SALT_ENV, "salty")
        a = hash_user_id("user-123")
        b = hash_user_id("user-123")
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_different_salts_produce_different_hashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USER_ID_HASH_SALT_ENV, "salt-a")
        a = hash_user_id("user-123")
        monkeypatch.setenv(USER_ID_HASH_SALT_ENV, "salt-b")
        b = hash_user_id("user-123")
        assert a != b


class TestTextHash:
    def test_returns_none_for_none(self) -> None:
        assert text_hash(None) is None

    def test_short_stable_hash(self) -> None:
        a = text_hash("hello world")
        b = text_hash("hello world")
        c = text_hash("hello worlD")
        assert a == b
        assert a != c
        assert len(a) == 16


class TestManifestAudioFingerprint:
    def test_empty_dir_returns_deterministic_value(self, tmp_path: Path) -> None:
        d = tmp_path / "tts_segments"
        d.mkdir()
        a = manifest_audio_fingerprint(d)
        b = manifest_audio_fingerprint(d)
        assert a == b

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        assert manifest_audio_fingerprint(tmp_path / "absent") is None

    def test_file_added_changes_hash(self, tmp_path: Path) -> None:
        d = tmp_path / "tts_segments"
        d.mkdir()
        before = manifest_audio_fingerprint(d)
        (d / "segment_001.wav").write_bytes(b"fake")
        after = manifest_audio_fingerprint(d)
        assert before != after

    def test_non_wav_files_ignored(self, tmp_path: Path) -> None:
        d = tmp_path / "tts_segments"
        d.mkdir()
        (d / "segment_001.wav").write_bytes(b"a")
        with_wav_only = manifest_audio_fingerprint(d)
        (d / "notes.txt").write_text("ignore me")
        with_extras = manifest_audio_fingerprint(d)
        assert with_wav_only == with_extras


# ---------------------------------------------------------------------------
# AuditContext
# ---------------------------------------------------------------------------


class TestAuditContext:
    def test_from_job_record_uses_record_fields(self, tmp_path: Path) -> None:
        @dataclass
        class FakeRecord:
            job_id: str = "job-abc"
            root_job_id: str | None = "job-root"
            project_dir: str | None = None
            user_id: str | None = None

        rec = FakeRecord(project_dir=str(tmp_path / "project_xyz"))
        ctx = AuditContext.from_job_record(rec)
        assert ctx.job_id == "job-abc"
        assert ctx.root_job_id == "job-root"
        assert ctx.project_id == "project_xyz"
        assert ctx.actor_user_id_hash is None  # no salt configured

    def test_from_job_record_fills_root_from_job_id_when_missing(self) -> None:
        @dataclass
        class FakeRecord:
            job_id: str = "solo-job"
            root_job_id: str | None = None
            project_dir: str | None = None
            user_id: str | None = None

        ctx = AuditContext.from_job_record(FakeRecord())
        assert ctx.root_job_id == "solo-job"

    def test_from_job_record_hashes_user_id_when_salt_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(USER_ID_HASH_SALT_ENV, "deployment-salt")

        @dataclass
        class FakeRecord:
            job_id: str = "j"
            root_job_id: str | None = "j"
            project_dir: str | None = None
            user_id: str | None = "user-42"

        ctx = AuditContext.from_job_record(FakeRecord())
        assert ctx.actor_user_id_hash is not None
        assert len(ctx.actor_user_id_hash) == 64


# ---------------------------------------------------------------------------
# Event builders — sanity checks on shape, type, defaults
# ---------------------------------------------------------------------------


class TestEventBuilders:
    def test_builders_emit_stable_schema_fields(self, ctx: AuditContext) -> None:
        ev = build_editing_session_started_event(ctx)
        assert ev["event_id"]
        assert ev["schema_version"] == SCHEMA_VERSION
        assert ev["created_at"]
        assert ev["usage_event_ids"] == []

    def test_translation_speaker_changed_shape(self, ctx: AuditContext) -> None:
        ev = build_translation_speaker_changed_event(
            ctx,
            segment_id=12,
            before_speaker_id="speaker_b",
            after_speaker_id="speaker_a",
            start_ms=10000,
            end_ms=18000,
            source_text_chars=86,
            cn_text_chars=34,
        )
        assert ev["event_type"] == EVENT_TYPE_TRANSLATION_SPEAKER_CHANGED
        assert ev["stage"] == STAGE_TRANSLATION_REVIEW
        assert ev["before"]["speaker_id"] == "speaker_b"
        assert ev["after"]["speaker_id"] == "speaker_a"
        assert ev["segment"]["duration_ms"] == 8000
        assert ev["context"]["source_text_chars"] == 86

    def test_translation_split_event(self, ctx: AuditContext) -> None:
        ev = build_translation_segment_split_confirmed_event(
            ctx,
            original_segment_id=5,
            new_segment_ids=["5_a", "5_b"],
            split_source_index=20,
            split_cn_index=12,
            speaker_a="speaker_a",
            speaker_b="speaker_b",
        )
        assert ev["event_type"] == EVENT_TYPE_TRANSLATION_SEGMENT_SPLIT_CONFIRMED
        assert ev["after"]["child_segment_ids"] == ["5_a", "5_b"]
        assert ev["context"]["child_speakers_different"] is True

    def test_translation_review_approved_marks_effective(self, ctx: AuditContext) -> None:
        ev = build_translation_review_approved_event(
            ctx,
            speaker_change_count=3,
            split_count=1,
            text_edit_count=7,
            changed_segment_ratio=0.21,
            total_segments=42,
        )
        assert ev["event_type"] == EVENT_TYPE_TRANSLATION_REVIEW_APPROVED
        assert ev["effective"] is True
        assert ev["effective_reason"] == EFFECTIVE_REASON_APPROVED
        assert ev["context"]["speaker_change_count"] == 3

    def test_voice_selection_speaker_reassigned(self, ctx: AuditContext) -> None:
        ev = build_voice_selection_speaker_reassigned_event(
            ctx,
            segment_id=7,
            from_speaker_id="speaker_a",
            to_speaker_id="speaker_c",
            duration_ms=2400,
            speaker_duration_share=0.05,
            is_short_segment=True,
        )
        assert ev["event_type"] == EVENT_TYPE_VOICE_SELECTION_SPEAKER_REASSIGNED
        assert ev["before"]["speaker_id"] == "speaker_a"
        assert ev["after"]["speaker_id"] == "speaker_c"
        assert ev["context"]["is_short_segment"] is True

    def test_voice_selection_dubbing_mode_changed(self, ctx: AuditContext) -> None:
        ev = build_voice_selection_dubbing_mode_changed_event(
            ctx,
            segment_id=9,
            speaker_id="speaker_b",
            before_mode="dub",
            after_mode="keep_original",
            duration_ms=1500,
        )
        assert ev["event_type"] == EVENT_TYPE_VOICE_SELECTION_DUBBING_MODE_CHANGED
        assert ev["before"]["dubbing_mode"] == "dub"
        assert ev["after"]["dubbing_mode"] == "keep_original"

    def test_editing_session_started_baseline(self, ctx: AuditContext) -> None:
        ev = build_editing_session_started_event(
            ctx,
            segment_count=42,
            speaker_count=2,
            speaker_distribution={"speaker_a": {"segments": 30, "duration_ms": 90000}},
            baseline_audio_fingerprint="abc123",
            baseline_audio_present=True,
            legacy_lazy_backfill=False,
            edit_generation=0,
        )
        assert ev["event_type"] == EVENT_TYPE_EDITING_SESSION_STARTED
        assert ev["effective"] is False  # baseline is NOT a correction
        assert ev["context"]["baseline_audio_fingerprint"] == "abc123"

    def test_post_edit_text_changed_records_delta(self, ctx: AuditContext) -> None:
        ev = build_post_edit_text_changed_event(
            ctx,
            segment_id="seg-001",
            before_chars=30,
            after_chars=42,
            before_text_hash="aaa",
            after_text_hash="bbb",
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_TEXT_CHANGED
        assert ev["context"]["char_delta"] == 12

    def test_post_edit_segment_speaker_changed(self, ctx: AuditContext) -> None:
        ev = build_post_edit_segment_speaker_changed_event(
            ctx,
            segment_id="seg-005",
            before_speaker_id="speaker_a",
            after_speaker_id="speaker_b",
            asr_speaker_id="speaker_a",
            s2_speaker_id="speaker_a",
            voice_selection_speaker_id="speaker_a",
            duration_ms=3500,
            neighbor_prev_speaker="speaker_b",
            neighbor_next_speaker="speaker_b",
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_SEGMENT_SPEAKER_CHANGED
        assert ev["context"]["asr_speaker_id"] == "speaker_a"
        assert ev["context"]["voice_selection_speaker_id"] == "speaker_a"

    def test_post_edit_segment_split_confirmed(self, ctx: AuditContext) -> None:
        ev = build_post_edit_segment_split_confirmed_event(
            ctx,
            original_segment_id="seg-007",
            new_segment_ids=["seg-007_a", "seg-007_b"],
            split_source_index=22,
            split_cn_index=15,
            speaker_a="speaker_a",
            speaker_b="speaker_b",
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_SEGMENT_SPLIT_CONFIRMED
        assert ev["context"]["child_speakers_different"] is True

    def test_post_edit_tts_regenerated_with_usage_correlation(self, ctx: AuditContext) -> None:
        ev = build_post_edit_tts_regenerated_event(
            ctx,
            segment_id="seg-001",
            trigger_reason="text_dirty",
            provider="minimax",
            voice_id="voice_xyz",
            model="speech-2.8-hd",
            target_duration_ms=4000,
            draft_audio_duration_ms=4200,
            success=True,
            usage_event_ids=["usage-event-uuid-1"],
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_TTS_REGENERATED
        assert ev["context"]["trigger_reason"] == "text_dirty"
        assert ev["usage_event_ids"] == ["usage-event-uuid-1"]

    def test_post_edit_draft_tts_accepted_marks_effective(self, ctx: AuditContext) -> None:
        ev = build_post_edit_draft_tts_accepted_event(
            ctx,
            segment_id="seg-001",
            draft_audio_duration_ms=4200,
            target_duration_ms=4000,
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_DRAFT_TTS_ACCEPTED
        assert ev["effective"] is True
        assert ev["effective_reason"] == EFFECTIVE_REASON_TTS_ACCEPTED

    def test_post_edit_draft_tts_discarded_is_negative_signal(self, ctx: AuditContext) -> None:
        ev = build_post_edit_draft_tts_discarded_event(
            ctx,
            segment_id="seg-001",
            voice_id="voice_xyz",
            provider="minimax",
            draft_audio_duration_ms=5000,
            target_duration_ms=4000,
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_DRAFT_TTS_DISCARDED
        assert ev["effective"] is False  # discard is NOT effective by itself

    def test_post_edit_voice_override_changed(self, ctx: AuditContext) -> None:
        ev = build_post_edit_voice_override_changed_event(
            ctx,
            segment_id="seg-001",
            operation="set",
            before_voice_id="voice_a",
            after_voice_id="voice_b",
            before_provider="minimax",
            after_provider="cosyvoice",
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_VOICE_OVERRIDE_CHANGED
        assert ev["before"]["voice_id"] == "voice_a"
        assert ev["after"]["voice_id"] == "voice_b"

    def test_post_edit_cancelled(self, ctx: AuditContext) -> None:
        ev = build_post_edit_cancelled_event(
            ctx,
            cancel_reason="user_cancel",
            session_duration_seconds=312.5,
            edit_counts={"text_edit_count": 4, "speaker_change_count": 1},
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_CANCELLED
        assert ev["effective"] is False  # cancel is NOT effective
        assert ev["context"]["cancel_reason"] == "user_cancel"
        assert ev["context"]["edit_counts"]["text_edit_count"] == 4

    def test_post_edit_committed_marks_effective(self, ctx: AuditContext) -> None:
        ev = build_post_edit_committed_event(
            ctx,
            strategy="overwrite",
            edit_counts={"text_edit_count": 8, "tts_regenerated_count": 11},
        )
        assert ev["event_type"] == EVENT_TYPE_POST_EDIT_COMMITTED
        assert ev["effective"] is True
        assert ev["effective_reason"] == EFFECTIVE_REASON_COMMITTED


# ---------------------------------------------------------------------------
# effective_marker
# ---------------------------------------------------------------------------


class TestEffectiveMarker:
    def test_effective_marker_event_shape(self, ctx: AuditContext) -> None:
        ev = build_effective_marker_event(
            ctx,
            stage=STAGE_POST_EDIT,
            effective_reason=EFFECTIVE_REASON_COMMITTED,
            marked_event_ids=["evt-1", "evt-2", "evt-3"],
        )
        assert ev["event_type"] == EVENT_TYPE_EFFECTIVE_MARKER
        assert ev["effective"] is True
        assert ev["effective_reason"] == EFFECTIVE_REASON_COMMITTED
        assert ev["context"]["marked_event_ids"] == ["evt-1", "evt-2", "evt-3"]

    def test_effective_marker_with_id_range(self, ctx: AuditContext) -> None:
        ev = build_effective_marker_event(
            ctx,
            stage=STAGE_POST_EDIT,
            effective_reason=EFFECTIVE_REASON_COMMITTED,
            marked_event_id_range=("evt-001", "evt-100"),
        )
        assert ev["context"]["marked_event_id_range"] == ["evt-001", "evt-100"]

    def test_effective_marker_appended_alongside_original_events(
        self, tmp_path: Path, ctx: AuditContext
    ) -> None:
        """Plan §4.5: marker is a SEPARATE event, original events are not
        rewritten."""
        writer = UserEditAuditWriter(tmp_path)
        # First, the original mutation event (effective=False default)
        text_event = writer.append_event(
            build_post_edit_text_changed_event(
                ctx,
                segment_id="seg-001",
                before_chars=20,
                after_chars=30,
                before_text_hash="a",
                after_text_hash="b",
            )
        )
        assert text_event["effective"] is False

        # Then the marker, which IS effective
        marker_event = writer.append_event(
            build_effective_marker_event(
                ctx,
                stage=STAGE_POST_EDIT,
                effective_reason=EFFECTIVE_REASON_COMMITTED,
                marked_event_ids=[text_event["event_id"]],
            )
        )

        # File contains BOTH events; original is unchanged on disk
        events_path = tmp_path / AUDIT_DIR_NAME / AUDIT_EVENTS_FILENAME
        lines = events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["event_type"] == EVENT_TYPE_POST_EDIT_TEXT_CHANGED
        assert first["effective"] is False  # NEVER rewritten
        assert second["event_type"] == EVENT_TYPE_EFFECTIVE_MARKER
        assert second["context"]["marked_event_ids"] == [first["event_id"]]

"""Tests for JobRecord snapshot field serialization (Phase 2)."""
from __future__ import annotations

from src.services.jobs.models import JobRecord


def _make_job(**overrides) -> dict:
    base = {
        "job_id": "test-001",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.com/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "created_at": "2026-03-29T00:00:00Z",
        "updated_at": "2026-03-29T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestJobRecordSnapshotFields:
    def test_round_trip_preserves_all_snapshot_fields(self):
        payload = _make_job(
            service_mode="studio",
            tts_provider="minimax",
            tts_model="speech-2.8-hd",
            requires_review=True,
            voice_clone_enabled=True,
            voice_strategy="user_selected",
            plan_code_snapshot="pro",
            role_snapshot="user",
            source_duration_seconds=532.0,
            estimated_duration_seconds=540.0,
            quota_cost=1,
            quota_state="none",
            create_idempotency_key="idem-key-001",
        )
        job = JobRecord.from_dict(payload)
        d = job.to_dict()

        assert d["service_mode"] == "studio"
        assert d["tts_provider"] == "minimax"
        assert d["tts_model"] == "speech-2.8-hd"
        assert d["requires_review"] is True
        assert d["voice_clone_enabled"] is True
        assert d["voice_strategy"] == "user_selected"
        assert d["plan_code_snapshot"] == "pro"
        assert d["role_snapshot"] == "user"
        assert d["source_duration_seconds"] == 532.0
        assert d["estimated_duration_seconds"] == 540.0
        assert d["quota_cost"] == 1
        assert d["quota_state"] == "none"
        assert d["create_idempotency_key"] == "idem-key-001"

    def test_missing_optional_fields_default_correctly(self):
        payload = _make_job()
        job = JobRecord.from_dict(payload)
        d = job.to_dict()

        assert d["service_mode"] is None
        assert d["tts_provider"] is None
        assert d["quota_state"] == "none"
        assert d["create_idempotency_key"] is None
        assert d["estimated_duration_seconds"] is None

    def test_quota_state_defaults_to_none(self):
        job = JobRecord.from_dict(_make_job())
        assert job.quota_state == "none"

    def test_from_dict_to_dict_round_trip_is_stable(self):
        payload = _make_job(
            service_mode="express",
            quota_state="reserved",
            create_idempotency_key="abc-123",
        )
        job1 = JobRecord.from_dict(payload)
        job2 = JobRecord.from_dict(job1.to_dict())
        assert job1.to_dict() == job2.to_dict()

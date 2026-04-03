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
            user_id="42",
            workspace_dir="projects/42/test-001",
            source_content_hash="sha256:abc123",
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
        assert d["user_id"] == "42"
        assert d["workspace_dir"] == "projects/42/test-001"
        assert d["source_content_hash"] == "sha256:abc123"

    def test_missing_optional_fields_default_correctly(self):
        payload = _make_job()
        job = JobRecord.from_dict(payload)
        d = job.to_dict()

        assert d["service_mode"] is None
        assert d["tts_provider"] is None
        assert d["quota_state"] == "none"
        assert d["create_idempotency_key"] is None
        assert d["estimated_duration_seconds"] is None
        assert d["user_id"] is None
        assert d["workspace_dir"] is None
        assert d["source_content_hash"] is None

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

    def test_transcription_method_round_trip(self):
        payload = _make_job(transcription_method="gemini")
        job = JobRecord.from_dict(payload)
        assert job.transcription_method == "gemini"
        d = job.to_dict()
        assert d["transcription_method"] == "gemini"
        job2 = JobRecord.from_dict(d)
        assert job2.transcription_method == "gemini"

    def test_transcription_method_defaults_to_assemblyai(self):
        payload = _make_job()
        job = JobRecord.from_dict(payload)
        assert job.transcription_method == "assemblyai"


# ===================================================================
# B2: volcengine tts_model round-trip
# ===================================================================

class TestVolcengineSnapshotRoundTrip:
    """Verify volcengine-specific tts_model values survive serialization."""

    def test_express_volcengine_tts_model_round_trip(self):
        """tts_model='seed-tts-1.1' (volcengine express) round-trips correctly."""
        payload = _make_job(
            tts_provider="volcengine",
            tts_model="seed-tts-1.1",
            service_mode="express",
            voice_clone_enabled=False,
        )
        job = JobRecord.from_dict(payload)
        d = job.to_dict()
        assert d["tts_provider"] == "volcengine"
        assert d["tts_model"] == "seed-tts-1.1"
        assert d["voice_clone_enabled"] is False

        # Double round-trip
        job2 = JobRecord.from_dict(d)
        assert job2.tts_model == "seed-tts-1.1"

    def test_studio_volcengine_tts_model_none_round_trip(self):
        """tts_model=None (volcengine studio) round-trips as None."""
        payload = _make_job(
            tts_provider="volcengine",
            tts_model=None,
            service_mode="studio",
            voice_clone_enabled=False,
        )
        job = JobRecord.from_dict(payload)
        d = job.to_dict()
        assert d["tts_provider"] == "volcengine"
        assert d["tts_model"] is None
        assert d["voice_clone_enabled"] is False

    def test_no_tts_resource_id_field_exists(self):
        """This round verifies we did NOT add a tts_resource_id field to JobRecord."""
        payload = _make_job(tts_provider="volcengine", tts_model="seed-tts-1.1")
        job = JobRecord.from_dict(payload)
        assert not hasattr(job, "tts_resource_id"), (
            "tts_resource_id should NOT exist on JobRecord in B2 (simplified plan)"
        )
        d = job.to_dict()
        assert "tts_resource_id" not in d

"""
Test JobRecord jianying_draft_* fields (K2 task).

Covers:
- Default values on construction
- Explicit values preserved
- Round-trip serialization
- Backward compat on read (missing fields in old data)
- VALID_JIANYING_DRAFT_STATUSES constant
- Status validation NOT enforced at dataclass level
"""

import pytest
from services.jobs.models import JobRecord, VALID_JIANYING_DRAFT_STATUSES


def _minimal_job_dict(**overrides) -> dict:
    """Helper: minimal valid JobRecord dict for testing."""
    base = {
        "job_id": "test-job-123",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.com/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "succeeded",
        "created_at": "2026-05-02T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestJobRecordJianyingFieldsDefaults:
    """Test 1: Default values on construction."""

    def test_jianying_fields_default_on_construction(self):
        """JobRecord() with required args only → jianying_draft fields default correctly."""
        record = JobRecord(
            job_id="test-job",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="succeeded",
            current_stage=None,
            progress_message=None,
            created_at="2026-05-02T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

        assert record.jianying_draft_status == "idle"
        assert record.jianying_draft_started_at is None
        assert record.jianying_draft_completed_at is None
        assert record.jianying_draft_error is None


class TestJobRecordJianyingFieldsExplicit:
    """Test 2: Explicit values preserved."""

    def test_jianying_fields_explicit_values(self):
        """JobRecord with explicit jianying fields → all values preserved."""
        record = JobRecord(
            job_id="test-job",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="succeeded",
            current_stage=None,
            progress_message=None,
            created_at="2026-05-02T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
            jianying_draft_status="succeeded",
            jianying_draft_started_at="2026-05-02T10:00:00Z",
            jianying_draft_completed_at="2026-05-02T10:30:00Z",
            jianying_draft_error=None,
        )

        assert record.jianying_draft_status == "succeeded"
        assert record.jianying_draft_started_at == "2026-05-02T10:00:00Z"
        assert record.jianying_draft_completed_at == "2026-05-02T10:30:00Z"
        assert record.jianying_draft_error is None


class TestJobRecordJianyingFieldsSerialization:
    """Test 3: Round-trip serialization."""

    def test_jianying_fields_round_trip(self):
        """Serialize JobRecord with jianying fields → deserialize → all equal."""
        original = JobRecord(
            job_id="test-job",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="running",
            current_stage=None,
            progress_message=None,
            created_at="2026-05-02T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
            jianying_draft_status="running",
            jianying_draft_started_at="2026-05-02T10:00:00Z",
            jianying_draft_completed_at=None,
            jianying_draft_error=None,
        )

        # Serialize
        serialized = original.to_dict()

        # Verify fields appear in dict
        assert "jianying_draft_status" in serialized
        assert "jianying_draft_started_at" in serialized
        assert "jianying_draft_completed_at" in serialized
        assert "jianying_draft_error" in serialized

        assert serialized["jianying_draft_status"] == "running"
        assert serialized["jianying_draft_started_at"] == "2026-05-02T10:00:00Z"
        assert serialized["jianying_draft_completed_at"] is None
        assert serialized["jianying_draft_error"] is None

        # Deserialize
        restored = JobRecord.from_dict(serialized)

        # Verify all jianying fields match
        assert restored.jianying_draft_status == original.jianying_draft_status
        assert restored.jianying_draft_started_at == original.jianying_draft_started_at
        assert restored.jianying_draft_completed_at == original.jianying_draft_completed_at
        assert restored.jianying_draft_error == original.jianying_draft_error


class TestJobRecordJianyingFieldsBackwardCompat:
    """Test 4: Backward compat on read (THE critical test)."""

    def test_backward_compat_old_data_missing_all_jianying_fields(self):
        """Old JobRecord dict (from before K2) lacking jianying fields → from_dict defaults them."""
        old_data = _minimal_job_dict()

        # Explicitly verify old_data does NOT have jianying fields
        assert "jianying_draft_status" not in old_data
        assert "jianying_draft_started_at" not in old_data
        assert "jianying_draft_completed_at" not in old_data
        assert "jianying_draft_error" not in old_data

        # from_dict should NOT raise KeyError and should default fields
        record = JobRecord.from_dict(old_data)

        assert record.jianying_draft_status == "idle"
        assert record.jianying_draft_started_at is None
        assert record.jianying_draft_completed_at is None
        assert record.jianying_draft_error is None


class TestJobRecordJianyingFieldsConstant:
    """Test 5: VALID_JIANYING_DRAFT_STATUSES constant."""

    def test_valid_jianying_draft_statuses_defined(self):
        """VALID_JIANYING_DRAFT_STATUSES constant exists and contains expected values."""
        # The constant must be importable from the module
        assert isinstance(VALID_JIANYING_DRAFT_STATUSES, frozenset), \
            "VALID_JIANYING_DRAFT_STATUSES must be a frozenset"
        assert VALID_JIANYING_DRAFT_STATUSES == {"idle", "running", "succeeded", "failed"}, \
            "VALID_JIANYING_DRAFT_STATUSES must contain exactly the 4 valid statuses"


class TestJobRecordJianyingFieldsNoValidationAtDataclass:
    """Test 6: Status validation NOT enforced at dataclass level."""

    def test_bogus_status_allowed_at_dataclass_level(self):
        """JobRecord(jianying_draft_status='bogus') should NOT raise at construction."""
        # Validation belongs at API layer (K4), not the data class.
        # This keeps the dataclass dumb.
        record = JobRecord(
            job_id="test-job",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="succeeded",
            current_stage=None,
            progress_message=None,
            created_at="2026-05-02T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
            jianying_draft_status="bogus",  # Invalid, but should not raise here
        )

        # Should accept it (validation is API-layer responsibility)
        assert record.jianying_draft_status == "bogus"


class TestJobRecordJianyingUserRootField:
    """Test K11: jianying_draft_user_root field."""

    def test_jianying_draft_user_root_defaults_to_none(self):
        """jianying_draft_user_root defaults to None on construction (K11)."""
        record = JobRecord(
            job_id="test-job",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="succeeded",
            current_stage=None,
            progress_message=None,
            created_at="2026-05-02T00:00:00Z",
            updated_at="2026-05-02T00:00:00Z",
        )

        assert record.jianying_draft_user_root is None

    def test_jianying_draft_user_root_explicit_value_preserved(self):
        """Explicit jianying_draft_user_root is preserved on construction (K11)."""
        win_path = r"F:\剪映缓存\草稿\JianyingPro Drafts"
        record = JobRecord.from_dict(
            _minimal_job_dict(jianying_draft_user_root=win_path)
        )
        assert record.jianying_draft_user_root == win_path

    def test_jianying_draft_user_root_round_trip(self):
        """jianying_draft_user_root survives to_dict → from_dict round-trip (K11)."""
        win_path = r"F:\剪映缓存\草稿\JianyingPro Drafts"
        original = JobRecord.from_dict(
            _minimal_job_dict(jianying_draft_user_root=win_path)
        )
        d = original.to_dict()
        assert d["jianying_draft_user_root"] == win_path

        restored = JobRecord.from_dict(d)
        assert restored.jianying_draft_user_root == win_path

    def test_jianying_draft_user_root_backward_compat_missing_field(self):
        """Old JobRecord dict without jianying_draft_user_root defaults to None (K11)."""
        old_data = _minimal_job_dict()
        assert "jianying_draft_user_root" not in old_data

        record = JobRecord.from_dict(old_data)
        assert record.jianying_draft_user_root is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Unit tests for JianyingDraftRequest and JianyingDraftResult dataclasses (Task J1).

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §5.1
"""

import pytest

from modules.output.jianying.jianying_draft_models import (
    JianyingDraftRequest,
    JianyingDraftResult,
)


def test_jianying_draft_request_with_all_required_fields():
    """Test creating a valid JianyingDraftRequest with all required fields."""
    req = JianyingDraftRequest(
        project_id="proj_001",
        project_title="My Project",
        source_video_path="/path/to/source.mp4",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
    )

    assert req.project_id == "proj_001"
    assert req.project_title == "My Project"
    assert req.source_video_path == "/path/to/source.mp4"
    assert req.dubbed_audio_path == "/path/to/dubbed.wav"
    assert req.subtitle_path == "/path/to/subtitles.srt"
    assert req.output_dir == "/output"
    assert req.ambient_audio_path is None
    assert req.width == 1920
    assert req.height == 1080


def test_jianying_draft_request_with_custom_width_height_ambient():
    """Test JianyingDraftRequest with custom width, height, and ambient audio."""
    req = JianyingDraftRequest(
        project_id="proj_002",
        project_title="Custom Dimensions",
        source_video_path="/path/to/source.mov",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
        ambient_audio_path="/path/to/ambient.wav",
        width=3840,
        height=2160,
    )

    assert req.project_id == "proj_002"
    assert req.ambient_audio_path == "/path/to/ambient.wav"
    assert req.width == 3840
    assert req.height == 2160


def test_jianying_draft_result_with_all_fields():
    """Test creating a valid JianyingDraftResult with all fields."""
    result = JianyingDraftResult(
        draft_dir="/output/jianying/draft",
        draft_zip_path="/output/jianying/exports/jianying_draft.zip",
        draft_content_path="/output/jianying/draft/draft_content.json",
        draft_meta_info_path="/output/jianying/draft/draft_meta_info.json",
        manifest_path="/output/manifest.json",
        compatibility_report_path="/output/jianying/jianying_compatibility_report.json",
        validation_status="ok",
    )

    assert result.draft_dir == "/output/jianying/draft"
    assert result.draft_zip_path == "/output/jianying/exports/jianying_draft.zip"
    assert result.draft_content_path == "/output/jianying/draft/draft_content.json"
    assert result.draft_meta_info_path == "/output/jianying/draft/draft_meta_info.json"
    assert result.manifest_path == "/output/manifest.json"
    assert result.compatibility_report_path == (
        "/output/jianying/jianying_compatibility_report.json"
    )
    assert result.validation_status == "ok"


def test_jianying_draft_result_with_manifest_path_none():
    """Test that JianyingDraftResult accepts manifest_path=None."""
    result = JianyingDraftResult(
        draft_dir="/output/jianying/draft",
        draft_zip_path="/output/jianying/exports/jianying_draft.zip",
        draft_content_path="/output/jianying/draft/draft_content.json",
        draft_meta_info_path="/output/jianying/draft/draft_meta_info.json",
        manifest_path=None,
        compatibility_report_path="/output/jianying/jianying_compatibility_report.json",
        validation_status="skipped_no_engine",
    )

    assert result.manifest_path is None
    assert result.validation_status == "skipped_no_engine"


def test_jianying_draft_request_slots_prevents_unknown_attributes():
    """Test that slots=True prevents assigning unknown attributes to JianyingDraftRequest."""
    req = JianyingDraftRequest(
        project_id="proj_003",
        project_title="Test",
        source_video_path="/path/to/source.mp4",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
    )

    with pytest.raises(AttributeError):
        req.unknown_attribute = "value"  # type: ignore


def test_jianying_draft_result_slots_prevents_unknown_attributes():
    """Test that slots=True prevents assigning unknown attributes to JianyingDraftResult."""
    result = JianyingDraftResult(
        draft_dir="/output/jianying/draft",
        draft_zip_path="/output/jianying/exports/jianying_draft.zip",
        draft_content_path="/output/jianying/draft/draft_content.json",
        draft_meta_info_path="/output/jianying/draft/draft_meta_info.json",
        manifest_path="/output/manifest.json",
        compatibility_report_path="/output/jianying/jianying_compatibility_report.json",
        validation_status="ok",
    )

    with pytest.raises(AttributeError):
        result.unknown_attribute = "value"  # type: ignore


# ---------------------------------------------------------------------------
# K11: user_draft_root field
# ---------------------------------------------------------------------------


def test_jianying_draft_request_user_draft_root_defaults_to_none():
    """user_draft_root defaults to None when not provided (K11)."""
    req = JianyingDraftRequest(
        project_id="proj_udr",
        project_title="Root Test",
        source_video_path="/path/to/source.mp4",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
    )

    assert req.user_draft_root is None


def test_jianying_draft_request_user_draft_root_accepts_windows_style():
    """user_draft_root accepts a Windows-style backslash path (K11)."""
    win_path = r"F:\剪映缓存\草稿\JianyingPro Drafts"
    req = JianyingDraftRequest(
        project_id="proj_win",
        project_title="Windows Root",
        source_video_path="/path/to/source.mp4",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
        user_draft_root=win_path,
    )

    assert req.user_draft_root == win_path


def test_jianying_draft_request_user_draft_root_accepts_unix_style():
    """user_draft_root accepts a Unix-style forward-slash path (K11)."""
    unix_path = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
    req = JianyingDraftRequest(
        project_id="proj_unix",
        project_title="Unix Root",
        source_video_path="/path/to/source.mp4",
        dubbed_audio_path="/path/to/dubbed.wav",
        subtitle_path="/path/to/subtitles.srt",
        output_dir="/output",
        user_draft_root=unix_path,
    )

    assert req.user_draft_root == unix_path

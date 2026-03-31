"""Tests for the centralized path helpers in services.job_paths."""
from __future__ import annotations

import pytest

from src.services.job_paths import (
    build_workspace_dir,
    build_upload_path,
    is_legacy_workspace,
    extract_user_id_from_workspace,
)


class TestBuildWorkspaceDir:
    def test_basic(self):
        result = build_workspace_dir(42, "job_abc123")
        assert result == "projects/42/job_abc123"

    def test_string_user_id(self):
        result = build_workspace_dir("7", "job_def")
        assert result == "projects/7/job_def"

    def test_custom_root(self):
        result = build_workspace_dir(1, "job_x", projects_root="data/projects")
        assert result == "data/projects/1/job_x"

    def test_sanitizes_unsafe_chars(self):
        result = build_workspace_dir("user@evil/../", "job_../../etc")
        # unsafe chars replaced with _
        assert ".." not in result
        assert "@" not in result
        assert "/" not in result.split("/", 1)[1].split("/")[0]  # user segment safe

    def test_empty_user_id_raises(self):
        with pytest.raises(ValueError):
            build_workspace_dir("", "job_x")

    def test_empty_job_id_raises(self):
        with pytest.raises(ValueError):
            build_workspace_dir(1, "")


class TestBuildUploadPath:
    def test_basic(self):
        result = build_upload_path(5, "uuid-abc", "my video.mp4")
        assert result == "uploads/5/uuid-abc_my_video.mp4"

    def test_preserves_extension(self):
        result = build_upload_path(1, "uid", "test.tar.gz")
        assert result.endswith(".tar.gz")

    def test_custom_root(self):
        result = build_upload_path(1, "uid", "f.mp4", uploads_root="data/uploads")
        assert result.startswith("data/uploads/")

    def test_empty_filename_gives_unnamed(self):
        result = build_upload_path(1, "uid", "")
        assert "unnamed" in result


class TestIsLegacyWorkspace:
    def test_none_is_legacy(self):
        assert is_legacy_workspace(None) is True

    def test_empty_is_legacy(self):
        assert is_legacy_workspace("") is True

    def test_old_slug_layout_is_legacy(self):
        assert is_legacy_workspace("projects/my-video-slug") is True

    def test_new_layout_is_not_legacy(self):
        assert is_legacy_workspace("projects/42/job_abc123") is False

    def test_absolute_new_layout(self):
        assert is_legacy_workspace("/opt/data/projects/42/job_abc123") is False

    def test_windows_new_layout(self):
        assert is_legacy_workspace("D:\\data\\projects\\42\\job_abc123") is False


class TestExtractUserIdFromWorkspace:
    def test_new_layout(self):
        assert extract_user_id_from_workspace("projects/42/job_abc") == "42"

    def test_absolute_new_layout(self):
        assert extract_user_id_from_workspace("/opt/data/projects/7/job_xyz") == "7"

    def test_legacy_returns_none(self):
        assert extract_user_id_from_workspace("projects/my-slug") is None

    def test_empty_returns_none(self):
        assert extract_user_id_from_workspace("") is None

"""Phase 2 tests: POST /gateway/upload-video (Gateway-native, no 8876 proxy).

Tests the upload handler directly using a lightweight FastAPI TestClient,
without importing the full gateway auth/database stack.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure gateway and src are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def upload_app(tmp_path, monkeypatch):
    """Create a lightweight FastAPI app with just the upload handler."""
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))

    from upload import handle_upload_video
    from starlette.requests import Request

    app = FastAPI()

    @app.post("/gateway/upload-video")
    async def upload_no_auth(request: Request):
        return await handle_upload_video(request, user=None)

    @app.post("/gateway/upload-video-with-user")
    async def upload_with_user(request: Request):
        fake_user = MagicMock()
        fake_user.id = "user_42"
        return await handle_upload_video(request, user=fake_user)

    return TestClient(app)


class TestGatewayUploadVideo:

    def test_upload_succeeds_with_valid_file(self, upload_app) -> None:
        content = b"fake video content " * 60000  # ~1.1 MB
        response = upload_app.post(
            "/gateway/upload-video",
            files={"file": ("test_video.mp4", content, "video/mp4")},
        )
        assert response.status_code == 200
        data = response.json()
        assert "file_path" in data
        assert data["file_name"] == "test_video.mp4"
        assert data["file_size_mb"] > 0
        assert Path(data["file_path"]).exists()

    def test_upload_rejects_non_multipart(self, upload_app) -> None:
        response = upload_app.post(
            "/gateway/upload-video",
            content=b"not multipart",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400

    def test_upload_rejects_missing_file_field(self, upload_app) -> None:
        response = upload_app.post(
            "/gateway/upload-video",
            files={"wrong_field": ("test.mp4", b"data", "video/mp4")},
        )
        assert response.status_code == 400

    def test_upload_rejects_empty_file(self, upload_app) -> None:
        response = upload_app.post(
            "/gateway/upload-video",
            files={"file": ("empty.mp4", b"", "video/mp4")},
        )
        assert response.status_code == 400

    def test_upload_without_user_falls_back_to_global_path(self, upload_app) -> None:
        """No authenticated user -> file goes to uploads/ (global fallback)."""
        response = upload_app.post(
            "/gateway/upload-video",
            files={"file": ("my_video.mp4", b"video data here", "video/mp4")},
        )
        assert response.status_code == 200
        file_path = response.json()["file_path"]
        assert "uploads" in file_path
        # Should NOT be under a user-scoped path
        assert "user_42" not in file_path

    def test_upload_with_user_writes_to_isolated_path(self, upload_app) -> None:
        """Authenticated user -> file goes to uploads/<user_id>/... (isolated)."""
        response = upload_app.post(
            "/gateway/upload-video-with-user",
            files={"file": ("user_video.mp4", b"user video data", "video/mp4")},
        )
        assert response.status_code == 200
        file_path = response.json()["file_path"]
        assert "user_42" in file_path
        assert Path(file_path).exists()

    def test_upload_does_not_proxy_to_8876(self, upload_app) -> None:
        """The upload endpoint must be handled natively (no proxy to 8876).
        If it tried to proxy, it would fail because 8876 is not running.
        Success here proves it's native."""
        response = upload_app.post(
            "/gateway/upload-video",
            files={"file": ("native.mp4", b"native upload content", "video/mp4")},
        )
        assert response.status_code == 200

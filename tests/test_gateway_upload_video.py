"""Phase 2 tests: POST /gateway/upload-video (Gateway-native, no 8876 proxy).

Tests the upload handler directly using a lightweight FastAPI TestClient,
without importing the full gateway auth/database stack.
"""
import os
import sys
import hashlib
import json
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

    from upload import (
        handle_upload_video,
        handle_upload_video_chunk,
        handle_upload_video_complete,
        handle_upload_video_session,
        handle_upload_video_session_status,
    )
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

    @app.post("/gateway/upload-video/session")
    async def upload_session_no_auth(request: Request):
        return await handle_upload_video_session(request, user=None)

    @app.get("/gateway/upload-video/session/{upload_id}")
    async def upload_session_status_no_auth(upload_id: str):
        return await handle_upload_video_session_status(upload_id, user=None)

    @app.post("/gateway/upload-video/chunk")
    async def upload_chunk_no_auth(request: Request):
        return await handle_upload_video_chunk(request, user=None)

    @app.post("/gateway/upload-video/complete")
    async def upload_complete_no_auth(request: Request):
        return await handle_upload_video_complete(request, user=None)

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
        assert "upload_id" in data
        assert data["file_name"] == "test_video.mp4"
        assert data["file_size_mb"] > 0
        assert data["sha256"] == f"sha256:{hashlib.sha256(content).hexdigest()}"
        assert Path(data["file_path"]).exists()
        sidecar = Path(f"{data['file_path']}.asset.json")
        assert sidecar.exists()
        asset = json.loads(sidecar.read_text(encoding="utf-8"))
        assert asset["upload_id"] == data["upload_id"]
        assert asset["file_path"] == data["file_path"]
        assert asset["sha256"] == data["sha256"]

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

    def test_resumable_upload_completes_from_chunks(self, upload_app) -> None:
        content = (b"chunked video content " * 70000) + b"tail"
        session_response = upload_app.post(
            "/gateway/upload-video/session",
            json={
                "file_name": "long_video.mp4",
                "file_size": len(content),
                "chunk_size": 256 * 1024,
            },
        )
        assert session_response.status_code == 200
        session = session_response.json()
        upload_id = session["upload_id"]
        chunk_size = session["chunk_size"]
        total_chunks = session["total_chunks"]
        assert total_chunks > 1

        chunks = [
            content[offset : offset + chunk_size]
            for offset in range(0, len(content), chunk_size)
        ]
        assert len(chunks) == total_chunks
        for chunk_index in reversed(range(total_chunks)):
            response = upload_app.post(
                "/gateway/upload-video/chunk",
                data={
                    "upload_id": upload_id,
                    "chunk_index": str(chunk_index),
                    "total_chunks": str(total_chunks),
                },
                files={
                    "chunk": (
                        f"{chunk_index}.part",
                        chunks[chunk_index],
                        "application/octet-stream",
                    ),
                },
            )
            assert response.status_code == 200

        status_response = upload_app.get(f"/gateway/upload-video/session/{upload_id}")
        assert status_response.status_code == 200
        assert status_response.json()["received_chunks"] == list(range(total_chunks))

        complete_response = upload_app.post(
            "/gateway/upload-video/complete",
            json={"upload_id": upload_id, "total_chunks": total_chunks},
        )
        assert complete_response.status_code == 200
        data = complete_response.json()
        assert data["upload_id"] == upload_id
        assert data["file_name"] == "long_video.mp4"
        assert data["sha256"] == f"sha256:{hashlib.sha256(content).hexdigest()}"
        output_path = Path(data["file_path"])
        assert output_path.exists()
        assert output_path.read_bytes() == content

        sidecar = Path(f"{data['file_path']}.asset.json")
        asset = json.loads(sidecar.read_text(encoding="utf-8"))
        assert asset["upload_id"] == upload_id
        assert asset["sha256"] == data["sha256"]

        completed_status = upload_app.get(f"/gateway/upload-video/session/{upload_id}")
        assert completed_status.status_code == 200
        completed_data = completed_status.json()
        assert completed_data["completed"] is True
        assert completed_data["asset"]["file_path"] == data["file_path"]

    def test_resumable_upload_complete_reports_missing_chunks(self, upload_app) -> None:
        content = b"a" * (600 * 1024)
        session_response = upload_app.post(
            "/gateway/upload-video/session",
            json={
                "file_name": "missing_chunk.mp4",
                "file_size": len(content),
                "chunk_size": 256 * 1024,
            },
        )
        assert session_response.status_code == 200
        session = session_response.json()
        upload_id = session["upload_id"]
        chunk_size = session["chunk_size"]
        total_chunks = session["total_chunks"]
        assert total_chunks == 3

        response = upload_app.post(
            "/gateway/upload-video/chunk",
            data={"upload_id": upload_id, "chunk_index": "0", "total_chunks": str(total_chunks)},
            files={"chunk": ("0.part", content[:chunk_size], "application/octet-stream")},
        )
        assert response.status_code == 200

        complete_response = upload_app.post(
            "/gateway/upload-video/complete",
            json={"upload_id": upload_id, "total_chunks": total_chunks},
        )
        assert complete_response.status_code == 409
        data = complete_response.json()
        assert data["missing_chunks"] == [1, 2]

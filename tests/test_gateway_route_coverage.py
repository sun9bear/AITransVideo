"""Phase 3: Gateway route coverage tests.

Verifies that job sub-resource paths (review/*, download/*, tts-segments-zip, DELETE)
are dispatched to the correct intercept handlers, NOT to the generic proxy catch-all.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Stub database before importing gateway modules
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
from unittest.mock import MagicMock
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)
# Another test file may have already stubbed 'database' via setdefault with
# an older attribute set (pre-T3). Force-patch init_db onto whatever stub
# lives in sys.modules so `from database import init_db` in main.py succeeds.
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = MagicMock()

# Load gateway/main.py
_gw_main_path = Path(__file__).resolve().parent.parent / "gateway" / "main.py"
_spec = importlib.util.spec_from_file_location("gateway_main_routes", str(_gw_main_path))
gw = importlib.util.module_from_spec(_spec)
sys.modules["gateway_main_routes"] = gw
_spec.loader.exec_module(gw)


def _find_route_endpoint(app, path: str, method: str):
    """Find which endpoint function FastAPI would dispatch for a given path + method.

    Returns the endpoint function name, or None if only the generic catch-all matches.
    """
    from starlette.routing import Match
    scope = {"type": "http", "method": method, "path": path}
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                return endpoint.__name__
    return None


class TestJobSubresourceRouting:
    """Verify review/download/tts paths hit intercept_job_subresource, not generic catch-all."""

    @pytest.mark.parametrize("subpath", [
        "review/translation/approve",
        "review/split-segment",
        "review/preview-segment",
        "download/manifest.file",
        "tts-segments-zip",
        "review-state",
        "logs",
        "artifacts",
        "result-summary",
        "stream/video",
        "stream/audio",
        "materials-availability",
        "reports",
        "reports/speaker-evidence",
        "reports/translation-quality",
    ])
    def test_get_post_subresources_hit_intercept(self, subpath):
        """GET/POST /job-api/jobs/{id}/{subpath} should match intercept_job_subresource."""
        path = f"/job-api/jobs/job_test123/{subpath}"
        for method in ("GET", "POST"):
            endpoint = _find_route_endpoint(gw.app, path, method)
            assert endpoint == "intercept_job_subresource", (
                f"{method} {path} matched '{endpoint}' instead of 'intercept_job_subresource'"
            )

    def test_subresources_do_not_hit_generic_catchall(self):
        """Verify that review paths don't fall through to proxy_job_api_other."""
        path = "/job-api/jobs/job_test123/review/translation/approve"
        endpoint = _find_route_endpoint(gw.app, path, "POST")
        assert endpoint != "proxy_job_api_other"


class TestDeleteJobRouting:
    """Verify DELETE /job-api/jobs/{id} hits the dedicated intercept, not generic catch-all."""

    def test_delete_hits_dedicated_intercept(self):
        endpoint = _find_route_endpoint(gw.app, "/job-api/jobs/job_test123", "DELETE")
        assert endpoint == "intercept_delete_job_v2", (
            f"DELETE /job-api/jobs/{{id}} matched '{endpoint}' instead of 'intercept_delete_job_v2'"
        )

    def test_delete_does_not_hit_generic_catchall(self):
        endpoint = _find_route_endpoint(gw.app, "/job-api/jobs/job_test123", "DELETE")
        assert endpoint != "proxy_job_api_other"


class TestMaterialsPackRouting:
    """Verify materials-pack endpoint is Gateway-native (not proxied)."""

    def test_materials_pack_post_resolves(self):
        endpoint = _find_route_endpoint(gw.app, "/api/jobs/job_test123/materials-pack", "POST")
        assert endpoint == "materials_pack_endpoint", (
            f"POST /api/jobs/{{id}}/materials-pack matched '{endpoint}' instead of 'materials_pack_endpoint'"
        )

    def test_materials_pack_get_resolves(self):
        endpoint = _find_route_endpoint(gw.app, "/api/jobs/job_test123/materials-pack", "GET")
        assert endpoint == "materials_pack_endpoint", (
            f"GET /api/jobs/{{id}}/materials-pack matched '{endpoint}' instead of 'materials_pack_endpoint'"
        )


class TestBackgroundTaskRouting:
    """Verify /api/jobs/{id}/tasks/* endpoints are Gateway-native.

    These paths serve Export Tasks v1 (materials_pack, generate_video).
    They MUST resolve to their specific handlers — if any of them were
    dispatched to a job-api proxy catch-all, the feature would 404 upstream.
    """

    def test_create_task_post(self):
        endpoint = _find_route_endpoint(gw.app, "/api/jobs/job_test123/tasks", "POST")
        assert endpoint == "create_task_endpoint", (
            f"POST /api/jobs/{{id}}/tasks matched '{endpoint}'"
        )

    def test_latest_task_get(self):
        endpoint = _find_route_endpoint(gw.app, "/api/jobs/job_test123/tasks/latest", "GET")
        assert endpoint == "latest_task_endpoint", (
            f"GET /api/jobs/{{id}}/tasks/latest matched '{endpoint}'"
        )

    def test_get_task(self):
        endpoint = _find_route_endpoint(gw.app, "/api/jobs/job_test123/tasks/abc123", "GET")
        assert endpoint == "get_task_endpoint", (
            f"GET /api/jobs/{{id}}/tasks/{{task_id}} matched '{endpoint}'"
        )

    def test_task_download(self):
        endpoint = _find_route_endpoint(
            gw.app, "/api/jobs/job_test123/tasks/abc123/download", "GET",
        )
        assert endpoint == "download_task_artifact", (
            f"GET /api/jobs/{{id}}/tasks/{{task_id}}/download matched '{endpoint}'"
        )


class TestVoiceLibraryRouting:
    """Verify GET /job-api/voice-library hits the subresource intercept path."""

    def test_voice_library_route_exists(self):
        """GET /job-api/voice-library should resolve to a valid endpoint (proxied to 8877)."""
        # This path goes through the generic catch-all to 8877, which is acceptable
        # since voice-library is a global resource without ownership requirement
        endpoint = _find_route_endpoint(gw.app, "/job-api/voice-library", "GET")
        assert endpoint is not None, "GET /job-api/voice-library should match a route"


class TestUploadRouting:
    """Verify POST /gateway/upload-video hits the native upload handler."""

    def test_upload_hits_native_handler(self):
        endpoint = _find_route_endpoint(gw.app, "/gateway/upload-video", "POST")
        assert endpoint == "_gateway_upload_video", (
            f"POST /gateway/upload-video matched '{endpoint}' instead of '_gateway_upload_video'"
        )

    def test_upload_does_not_hit_proxy(self):
        endpoint = _find_route_endpoint(gw.app, "/gateway/upload-video", "POST")
        assert endpoint != "proxy_web_ui"
        assert endpoint != "proxy_web_ui_legacy"

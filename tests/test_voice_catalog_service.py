"""Tests for voice_catalog_service — protocol-level verify tests.

These tests verify that the VolcEngine verify adapter uses the correct
endpoint, headers, payload, and response parsing — identical to the
production volcengine_tts_provider.py.

All HTTP calls are mocked via httpx mock transport; no real TTS API calls.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

# Save original before any monkeypatching
_OriginalAsyncClient = httpx.AsyncClient

# Stub gateway dependencies before import
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from voice_catalog_service import (
    _VOLC_CODE_AUDIO,
    _VOLC_CODE_FINISH,
    _VOLC_ENDPOINT,
    _VOLC_PCM_SAMPLE_RATE,
    VERIFY_MIN_PCM_BYTES,
    VERIFY_TEST_TEXT_ZH,
    _volc_synthesize_check,
    verify_volcengine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio_response(pcm_bytes: int = 2000) -> str:
    """Build a valid V3 response body with audio chunks + finish event."""
    fake_pcm = b"\x00" * pcm_bytes
    audio_event = json.dumps({
        "code": _VOLC_CODE_AUDIO,
        "data": base64.b64encode(fake_pcm).decode(),
        "message": "",
    })
    finish_event = json.dumps({
        "code": _VOLC_CODE_FINISH,
        "message": "success",
    })
    return f"{audio_event}\n{finish_event}"


def _make_error_response(code: int, message: str) -> str:
    """Build a V3 error response body."""
    return json.dumps({"code": code, "message": message})


def _set_volc_env(monkeypatch):
    """Set valid VolcEngine credentials in env."""
    monkeypatch.setenv("VOLCENGINE_TTS_APP_ID", "test-app-id")
    monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_KEY", "test-access-key")


# ---------------------------------------------------------------------------
# Protocol: endpoint
# ---------------------------------------------------------------------------

class TestVolcEndpoint:
    def test_endpoint_is_unidirectional(self) -> None:
        """Must use /api/v3/tts/unidirectional, NOT /api/v3/tts/chunked."""
        assert _VOLC_ENDPOINT == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
        assert "chunked" not in _VOLC_ENDPOINT


# ---------------------------------------------------------------------------
# Protocol: headers
# ---------------------------------------------------------------------------

class TestVolcHeaders:
    @pytest.mark.anyio
    async def test_correct_header_names(self, monkeypatch) -> None:
        """Headers must use X-Api-App-Id (not X-Api-App-Key)."""
        _set_volc_env(monkeypatch)
        captured_request = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            captured_request["headers"] = dict(request.headers)
            captured_request["url"] = str(request.url)
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        await _volc_synthesize_check("test_voice", "seed-tts-1.0")

        h = captured_request["headers"]
        # Correct header names
        assert "x-api-app-id" in h, f"Missing X-Api-App-Id, got: {list(h.keys())}"
        assert "x-api-access-key" in h, f"Missing X-Api-Access-Key"
        assert "x-api-resource-id" in h, f"Missing X-Api-Resource-Id"
        assert "x-api-request-id" in h, f"Missing X-Api-Request-Id"
        assert "content-type" in h

        # Verify values
        assert h["x-api-app-id"] == "test-app-id"
        assert h["x-api-access-key"] == "test-access-key"
        assert h["x-api-resource-id"] == "seed-tts-1.0"

        # Must NOT use the wrong header name
        assert "x-api-app-key" not in h, "Must use X-Api-App-Id, not X-Api-App-Key"

    @pytest.mark.anyio
    async def test_uses_correct_endpoint_url(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        captured_request = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        await _volc_synthesize_check("test_voice", "seed-tts-2.0")
        assert captured_request["url"] == _VOLC_ENDPOINT


# ---------------------------------------------------------------------------
# Protocol: payload
# ---------------------------------------------------------------------------

class TestVolcPayload:
    @pytest.mark.anyio
    async def test_payload_structure(self, monkeypatch) -> None:
        """Payload must match volcengine_tts_provider._build_payload() exactly."""
        _set_volc_env(monkeypatch)
        captured_payload = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        await _volc_synthesize_check("my_voice_id", "seed-tts-1.0")

        # user.uid
        assert captured_payload["user"]["uid"] == "aivideotrans"

        # req_params structure
        rp = captured_payload["req_params"]
        assert rp["speaker"] == "my_voice_id"
        assert rp["text"] == VERIFY_TEST_TEXT_ZH

        # audio_params
        ap = rp["audio_params"]
        assert ap["format"] == "pcm"
        assert ap["sample_rate"] == _VOLC_PCM_SAMPLE_RATE
        assert ap["sample_rate"] == 24000

    @pytest.mark.anyio
    async def test_no_legacy_payload_fields(self, monkeypatch) -> None:
        """Must NOT use the old wrong payload format (config.encoding, text at top level)."""
        _set_volc_env(monkeypatch)
        captured_payload = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        await _volc_synthesize_check("v", "seed-tts-1.0")

        # Must not have top-level "text" or "config" (that was the old wrong format)
        assert "text" not in captured_payload, "text must be inside req_params, not top level"
        assert "config" not in captured_payload, "must use req_params.audio_params, not config"


# ---------------------------------------------------------------------------
# Response parsing: success
# ---------------------------------------------------------------------------

class TestVolcVerifySuccess:
    @pytest.mark.anyio
    async def test_success_with_enough_audio(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=_make_audio_response(2000))
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is True
        assert result["error"] is None
        assert result["at"] is not None


# ---------------------------------------------------------------------------
# Response parsing: errors
# ---------------------------------------------------------------------------

class TestVolcVerifyErrors:
    @pytest.mark.anyio
    async def test_non_2xx_not_misreported_as_audio_too_short(self, monkeypatch) -> None:
        """Non-2xx must report HTTP status, NOT 'audio too short'."""
        _set_volc_env(monkeypatch)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(401, text="Unauthorized")
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "HTTP 401" in result["error"]
        assert "音频太短" not in result["error"]

    @pytest.mark.anyio
    async def test_404_error(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(404, text="Not Found")
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "HTTP 404" in result["error"]

    @pytest.mark.anyio
    async def test_500_error(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(500, text="Internal Server Error")
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "HTTP 500" in result["error"]

    @pytest.mark.anyio
    async def test_resource_mismatch_55000000(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        body = _make_error_response(55000000, "resource does not match")
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=body)
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "resource mismatch" in result["error"]

    @pytest.mark.anyio
    async def test_voice_not_found_45000000(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)
        body = _make_error_response(45000000, "speaker not found")
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=body)
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "音色不存在" in result["error"]

    @pytest.mark.anyio
    async def test_unparseable_response_body(self, monkeypatch) -> None:
        """200 but garbage body should report format error, not 'audio too short'."""
        _set_volc_env(monkeypatch)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text="this is not json")
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "非预期响应格式" in result["error"]

    @pytest.mark.anyio
    async def test_timeout(self, monkeypatch) -> None:
        _set_volc_env(monkeypatch)

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Connection timed out")

        transport = httpx.MockTransport(timeout_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "timeout" in result["error"].lower()

    @pytest.mark.anyio
    async def test_audio_too_short_only_when_pcm_received(self, monkeypatch) -> None:
        """'音频太短' only when we actually received some PCM but not enough."""
        _set_volc_env(monkeypatch)
        # 100 bytes of PCM — below threshold
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, text=_make_audio_response(100))
        )
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "音频太短" in result["error"]

    @pytest.mark.anyio
    async def test_missing_credentials(self, monkeypatch) -> None:
        monkeypatch.delenv("VOLCENGINE_TTS_APP_ID", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_KEY", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_APPID", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_TOKEN", raising=False)

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is False
        assert "凭据" in result["error"]

    @pytest.mark.anyio
    async def test_missing_credentials_propagates_skip_flag(self, monkeypatch) -> None:
        """verify_volcengine must propagate _skip_db_update to top level."""
        monkeypatch.delenv("VOLCENGINE_TTS_APP_ID", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_KEY", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_APPID", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_TOKEN", raising=False)

        result = await verify_volcengine("v", {"resource_id": "seed-tts-1.0"})
        # _skip_db_update must be at TOP level, not nested inside "default"
        assert result.get("_skip_db_update") is True
        # inner result must NOT have _skip_db_update (already popped)
        assert "_skip_db_update" not in result["default"]


# ---------------------------------------------------------------------------
# Auto-detect resource_id
# ---------------------------------------------------------------------------

class TestResourceIdAutoDetect:
    @pytest.mark.anyio
    async def test_detect_2_0_success(self, monkeypatch) -> None:
        """When resource_id is None and 2.0 succeeds, return 2.0."""
        _set_volc_env(monkeypatch)

        call_count = {"n": 0}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            # First call (2.0) succeeds
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await verify_volcengine("test_voice", {})
        assert result["default"]["verified"] is True
        assert result.get("_detected_resource_id") == "seed-tts-2.0"

    @pytest.mark.anyio
    async def test_detect_fallback_to_1_0(self, monkeypatch) -> None:
        """When 2.0 fails but 1.0 succeeds, return 1.0."""
        _set_volc_env(monkeypatch)

        call_count = {"n": 0}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            rid = request.headers.get("x-api-resource-id", "")
            if rid == "seed-tts-2.0":
                # 2.0 fails with resource mismatch
                return httpx.Response(200, text=_make_error_response(55000000, "mismatch"))
            else:
                # 1.0 succeeds
                return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await verify_volcengine("test_voice", {})
        assert result["default"]["verified"] is True
        assert result.get("_detected_resource_id") == "seed-tts-1.0"

    @pytest.mark.anyio
    async def test_detect_both_fail(self, monkeypatch) -> None:
        """When both 2.0 and 1.0 fail, return failure without _detected_resource_id."""
        _set_volc_env(monkeypatch)

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_make_error_response(45000000, "not found"))

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await verify_volcengine("test_voice", {})
        assert result["default"]["verified"] is False
        assert "_detected_resource_id" not in result


# ---------------------------------------------------------------------------
# Credential env var fallback
# ---------------------------------------------------------------------------

class TestCredentialFallback:
    @pytest.mark.anyio
    async def test_legacy_env_vars(self, monkeypatch) -> None:
        """Falls back to VOLCENGINE_TTS_APPID / ACCESS_TOKEN."""
        monkeypatch.delenv("VOLCENGINE_TTS_APP_ID", raising=False)
        monkeypatch.delenv("VOLCENGINE_TTS_ACCESS_KEY", raising=False)
        monkeypatch.setenv("VOLCENGINE_TTS_APPID", "legacy-app-id")
        monkeypatch.setenv("VOLCENGINE_TTS_ACCESS_TOKEN", "legacy-key")

        captured_headers = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, text=_make_audio_response())

        transport = httpx.MockTransport(mock_handler)
        monkeypatch.setattr(
            "voice_catalog_service.httpx.AsyncClient",
            lambda **kw: _OriginalAsyncClient(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
        )

        result = await _volc_synthesize_check("v", "seed-tts-1.0")
        assert result["verified"] is True
        assert captured_headers["x-api-app-id"] == "legacy-app-id"
        assert captured_headers["x-api-access-key"] == "legacy-key"

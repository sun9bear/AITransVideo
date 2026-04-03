"""Tests for Phase 3 dynamic voice catalog — Gateway API + cache + fallback.

Mocks the HTTP call to Gateway internal API to test the caching logic,
fallback to static lists, and public function contracts.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gateway_response(resource_id: str = "seed-tts-1.0", count: int = 3):
    """Build a mock Gateway response."""
    voices = [
        {
            "voice_id": f"voice_{i}",
            "display_name": f"Voice {i}",
            "gender": "female" if i % 2 == 0 else "male",
            "age_group": "young",
            "persona_style": "warm",
            "energy_level": "medium",
            "resource_id": resource_id,
            "scene": "通用",
            "language": "zh",
            "matchable": True,
        }
        for i in range(count)
    ]
    default_vid = "zh_female_shuangkuaisisi_moon_bigtts" if "1.0" in resource_id else "zh_female_shuangkuaisisi_uranus_bigtts"
    return {"voices": voices, "default_voice_id": default_vid, "ts": "2026-04-02T12:00:00Z"}


def _mock_requests_get(resource_id: str = "seed-tts-1.0", count: int = 3):
    """Return a mock for requests.get that returns a valid response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _gateway_response(resource_id, count)
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDynamicCatalog:
    def setup_method(self):
        """Clear cache before each test."""
        from services.tts import volcengine_voice_catalog as mod
        mod._cache.clear()

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_gateway_success_returns_voices(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-1.0", 5)

        from services.tts.volcengine_voice_catalog import get_voices_for_resource
        voices = get_voices_for_resource("seed-tts-1.0")

        assert len(voices) == 5
        assert voices[0]["voice_id"] == "voice_0"
        mock_get.assert_called_once()

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_cache_hit_skips_request(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-1.0", 3)

        from services.tts.volcengine_voice_catalog import get_voices_for_resource
        # First call — cache miss
        get_voices_for_resource("seed-tts-1.0")
        assert mock_get.call_count == 1

        # Second call — cache hit
        get_voices_for_resource("seed-tts-1.0")
        assert mock_get.call_count == 1  # no new request

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_cache_expiry_triggers_refresh(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-1.0", 3)

        from services.tts import volcengine_voice_catalog as mod
        from services.tts.volcengine_voice_catalog import get_voices_for_resource

        # First call
        get_voices_for_resource("seed-tts-1.0")
        assert mock_get.call_count == 1

        # Expire cache
        entry = mod._cache["seed-tts-1.0"]
        mod._cache["seed-tts-1.0"] = (entry[0], entry[1], entry[2], time.time() - 120)

        # Should re-fetch
        get_voices_for_resource("seed-tts-1.0")
        assert mock_get.call_count == 2

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_gateway_failure_falls_back_to_static(self, mock_get) -> None:
        mock_get.side_effect = ConnectionError("Gateway down")

        from services.tts.volcengine_voice_catalog import get_voices_for_resource
        voices = get_voices_for_resource("seed-tts-1.0")

        # Should return static fallback — VOICES_1_0 has 280+ voices
        assert len(voices) > 100
        assert all(v.get("matchable", True) for v in voices)

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_gateway_failure_falls_back_2_0(self, mock_get) -> None:
        mock_get.side_effect = ConnectionError("Gateway down")

        from services.tts.volcengine_voice_catalog import get_voices_for_resource
        voices = get_voices_for_resource("seed-tts-2.0")

        # Static VOICES_2_0 has ~36 voices
        assert len(voices) > 10
        assert all("uranus" in v["voice_id"] or "saturn" in v["voice_id"] or "ICL_en" in v["voice_id"] for v in voices)

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_get_default_voice_id(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-2.0", 2)

        from services.tts.volcengine_voice_catalog import get_default_voice_id
        vid = get_default_voice_id("seed-tts-2.0")
        assert vid == "zh_female_shuangkuaisisi_uranus_bigtts"

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_is_voice_in_resource(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-1.0", 3)

        from services.tts.volcengine_voice_catalog import is_voice_in_resource
        assert is_voice_in_resource("voice_0", "seed-tts-1.0") is True
        assert is_voice_in_resource("nonexistent", "seed-tts-1.0") is False

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_get_all_voice_ids(self, mock_get) -> None:
        mock_get.return_value = _mock_requests_get("seed-tts-1.0", 3)

        from services.tts.volcengine_voice_catalog import get_all_voice_ids_for_resource
        ids = get_all_voice_ids_for_resource("seed-tts-1.0")
        assert isinstance(ids, frozenset)
        assert len(ids) == 3

    @patch("services.tts.volcengine_voice_catalog._requests.get")
    def test_different_resources_cached_separately(self, mock_get) -> None:
        def side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            rid = params.get("resource_id", "seed-tts-1.0")
            return _mock_requests_get(rid, 3 if "1.0" in rid else 2)

        mock_get.side_effect = side_effect

        from services.tts.volcengine_voice_catalog import get_voices_for_resource
        v1 = get_voices_for_resource("seed-tts-1.0")
        v2 = get_voices_for_resource("seed-tts-2.0")

        assert len(v1) == 3
        assert len(v2) == 2
        assert mock_get.call_count == 2

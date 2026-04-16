"""Tests for the user_voices fallback path in voice_speed_catalog.

Phase 4 UX added load_user_voice_speeds() + extended lookup_per_speaker()
so cloned voices (which never appear in voice_catalog) can still resolve
a calibrated cps via the user_voices table.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.tts import voice_speed_catalog as vsc


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset module-level caches between tests so they don't leak."""
    vsc._speed_cache.clear()
    vsc._user_voices_cache.clear()
    yield
    vsc._speed_cache.clear()
    vsc._user_voices_cache.clear()


# ---------------------------------------------------------------------------
# load_user_voice_speeds
# ---------------------------------------------------------------------------

def _mock_response(json_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code != 200:
        resp.raise_for_status.side_effect = RuntimeError(f"HTTP {status_code}")
    return resp


def test_load_user_voice_speeds_returns_calibrated_voices():
    """Happy path: gateway returns 2 calibrated voices, helper parses them."""
    fake_resp = _mock_response({
        "voices": [
            {
                "voice_id": "vt_speaker_a_111",
                "chars_per_second": 3.34,
                "chars_per_second_by_model": {"speech-2.8-turbo": 3.34},
                "speed_calibrated_at": "2026-04-15T11:32:15+00:00",
                "tts_provider": "minimax_tts",
                "platform": "minimax_domestic",
            },
            {
                "voice_id": "vt_speaker_b_222",
                "chars_per_second": 4.12,
                "chars_per_second_by_model": None,
                "speed_calibrated_at": "2026-04-15T12:00:00+00:00",
                "tts_provider": "minimax_tts",
                "platform": "minimax_domestic",
            },
        ]
    })
    with patch.object(vsc.requests, "get", return_value=fake_resp):
        result = vsc.load_user_voice_speeds(["vt_speaker_a_111", "vt_speaker_b_222"], user_id="user-uuid-1")

    assert "vt_speaker_a_111" in result
    assert result["vt_speaker_a_111"]["chars_per_second"] == 3.34
    assert result["vt_speaker_a_111"]["chars_per_second_by_model"] == {"speech-2.8-turbo": 3.34}
    assert result["vt_speaker_b_222"]["chars_per_second"] == 4.12
    # NULL by_model becomes empty dict (avoid NoneType issues downstream)
    assert result["vt_speaker_b_222"]["chars_per_second_by_model"] == {}


def test_load_user_voice_speeds_skips_uncalibrated_entries():
    """Voices with chars_per_second=None are filtered out (gateway shouldn't
    return them but defensive)."""
    fake_resp = _mock_response({
        "voices": [
            {"voice_id": "vt_with_cps", "chars_per_second": 4.0, "chars_per_second_by_model": {}},
            {"voice_id": "vt_no_cps", "chars_per_second": None, "chars_per_second_by_model": {}},
        ]
    })
    with patch.object(vsc.requests, "get", return_value=fake_resp):
        result = vsc.load_user_voice_speeds(["vt_with_cps", "vt_no_cps"], user_id="user-uuid-1")

    assert "vt_with_cps" in result
    assert "vt_no_cps" not in result


def test_load_user_voice_speeds_empty_input_short_circuits():
    """Empty list → no HTTP call, return empty dict."""
    with patch.object(vsc.requests, "get") as mock_get:
        result = vsc.load_user_voice_speeds([], user_id="user-uuid-1")

    assert result == {}
    mock_get.assert_not_called()


def test_load_user_voice_speeds_caches_per_id_list():
    """Same voice_ids → cache hit, no second HTTP call."""
    fake_resp = _mock_response({
        "voices": [{"voice_id": "vt_cached", "chars_per_second": 4.5, "chars_per_second_by_model": {}}]
    })
    with patch.object(vsc.requests, "get", return_value=fake_resp) as mock_get:
        first = vsc.load_user_voice_speeds(["vt_cached"], user_id="user-uuid-1")
        second = vsc.load_user_voice_speeds(["vt_cached"], user_id="user-uuid-1")

    assert first == second
    assert mock_get.call_count == 1


def test_load_user_voice_speeds_cache_key_is_order_insensitive():
    """Different list orderings hit the same cache entry (sort cache key)."""
    fake_resp = _mock_response({
        "voices": [
            {"voice_id": "vt_a", "chars_per_second": 3.0, "chars_per_second_by_model": {}},
            {"voice_id": "vt_b", "chars_per_second": 4.0, "chars_per_second_by_model": {}},
        ]
    })
    with patch.object(vsc.requests, "get", return_value=fake_resp) as mock_get:
        vsc.load_user_voice_speeds(["vt_a", "vt_b"], user_id="user-uuid-1")
        vsc.load_user_voice_speeds(["vt_b", "vt_a"], user_id="user-uuid-1")  # reversed

    assert mock_get.call_count == 1


def test_load_user_voice_speeds_returns_empty_on_http_error():
    """Gateway down / 5xx → empty dict, never raise."""
    with patch.object(vsc.requests, "get", side_effect=RuntimeError("connection refused")):
        result = vsc.load_user_voice_speeds(["vt_x"], user_id="user-uuid-1")

    assert result == {}


def test_load_user_voice_speeds_skips_when_user_id_missing():
    """Security: no user_id → skip the lookup entirely. Without this the
    gateway query would be unscoped and could leak another user's cps for
    the same voice_id (only (user_id, voice_id) is unique, not voice_id alone)."""
    with patch.object(vsc.requests, "get") as mock_get:
        result_none = vsc.load_user_voice_speeds(["vt_x"], user_id=None)
        result_empty = vsc.load_user_voice_speeds(["vt_x"], user_id="")

    assert result_none == {}
    assert result_empty == {}
    mock_get.assert_not_called()


def test_load_user_voice_speeds_caches_per_user_id():
    """Same voice_ids, different user_ids → SEPARATE cache entries (not shared)."""
    fake_a = _mock_response({
        "voices": [{"voice_id": "vt_x", "chars_per_second": 4.0, "chars_per_second_by_model": {}}]
    })
    fake_b = _mock_response({
        "voices": [{"voice_id": "vt_x", "chars_per_second": 5.0, "chars_per_second_by_model": {}}]
    })
    with patch.object(vsc.requests, "get", side_effect=[fake_a, fake_b]) as mock_get:
        ra = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")
        rb = vsc.load_user_voice_speeds(["vt_x"], user_id="user-B")

    assert mock_get.call_count == 2
    assert ra["vt_x"]["chars_per_second"] == 4.0
    assert rb["vt_x"]["chars_per_second"] == 5.0


def test_load_user_voice_speeds_passes_user_id_in_params():
    """HTTP call must include user_id so the gateway scopes the query."""
    fake_resp = _mock_response({"voices": []})
    with patch.object(vsc.requests, "get", return_value=fake_resp) as mock_get:
        vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")

    _, kwargs = mock_get.call_args
    params = kwargs.get("params", {})
    assert params.get("user_id") == "user-A"
    assert params.get("voice_ids") == "vt_x"


# ----- Negative-caching safety (CodeX 2026-04-15) -----
# Background: user runs a job with a cloned voice (no cps yet) →
# pipeline lookup returns {}. If we'd cached that {}, the user's next
# click on "测试语速" would write cps to the DB but the pipeline's next
# run would still read the stale empty cache for up to _CACHE_TTL_SECONDS.
# So we intentionally do NOT cache empty results.

def test_load_user_voice_speeds_empty_result_not_cached():
    """First call returns empty → second call must re-fetch (not cache hit)."""
    fake_empty = _mock_response({"voices": []})
    with patch.object(vsc.requests, "get", return_value=fake_empty) as mock_get:
        first = vsc.load_user_voice_speeds(["vt_uncal"], user_id="user-A")
        second = vsc.load_user_voice_speeds(["vt_uncal"], user_id="user-A")

    assert first == {}
    assert second == {}
    # Each call went over the wire — no negative caching.
    assert mock_get.call_count == 2


def test_load_user_voice_speeds_fresh_calibration_replaces_empty():
    """Simulates the real workflow:
      1. lookup returns {} (voice not yet calibrated)
      2. user presses "测试语速" — gateway writes cps to DB
      3. next lookup returns the fresh cps
    Without the negative-cache fix, step 3 would see the stale {}.
    """
    fake_empty = _mock_response({"voices": []})
    fake_populated = _mock_response({
        "voices": [{
            "voice_id": "vt_x",
            "chars_per_second": 3.34,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "2026-04-15T12:00:00+00:00",
        }]
    })
    with patch.object(vsc.requests, "get", side_effect=[fake_empty, fake_populated]) as mock_get:
        before = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")
        after = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")

    assert before == {}
    assert after["vt_x"]["chars_per_second"] == 3.34
    # Two distinct HTTP calls — the first wasn't cached because empty.
    assert mock_get.call_count == 2


def test_load_user_voice_speeds_non_empty_to_empty_clears_stale():
    """Edge case: cache was populated, then the voice got deleted (next
    fetch returns empty). Ensure the stale non-empty entry is dropped
    so a later call sees the real empty state."""
    fake_populated = _mock_response({
        "voices": [{
            "voice_id": "vt_x",
            "chars_per_second": 4.0,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "",
        }]
    })
    fake_empty = _mock_response({"voices": []})
    with patch.object(vsc.requests, "get", side_effect=[fake_populated, fake_empty, fake_empty]):
        first = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")
        # Bust the TTL by advancing the module's time-based cache check:
        # simplest way is to mutate the cache timestamp to expire it.
        cache_key = "user-A:vt_x"
        entry = vsc._user_voices_cache.get(cache_key)
        if entry:
            vsc._user_voices_cache[cache_key] = (entry[0], 0.0)
        second = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")
        third = vsc.load_user_voice_speeds(["vt_x"], user_id="user-A")

    assert first["vt_x"]["chars_per_second"] == 4.0
    assert second == {}
    # third also empty, and definitely not the stale non-empty first result
    assert third == {}
    # Cache key was cleared when the second call returned empty
    assert "user-A:vt_x" not in vsc._user_voices_cache


# ---------------------------------------------------------------------------
# lookup_per_speaker integration: catalog miss → user_voices fallback
# ---------------------------------------------------------------------------

def test_lookup_per_speaker_falls_back_to_user_voices_for_cloned():
    """speaker_a uses a cloned voice (not in catalog), speaker_b uses a
    catalog voice. Both should resolve."""
    # voice_catalog returns nothing for the cloned voice but covers the system one.
    catalog_data = {
        "Chinese_Female_Anchor": {
            "chars_per_second": 4.5,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "...",
        },
    }
    user_voices_data = {
        "vt_speaker_a_111": {
            "chars_per_second": 3.34,
            "chars_per_second_by_model": {"speech-2.8-turbo": 3.34},
            "speed_calibrated_at": "...",
        },
    }

    with patch.object(vsc, "load_speed_catalog", return_value=catalog_data), \
         patch.object(vsc, "load_user_voice_speeds", return_value=user_voices_data) as mock_user:
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {
                "speaker_a": "vt_speaker_a_111",      # cloned
                "speaker_b": "Chinese_Female_Anchor",  # system
            },
            default_provider="minimax",
            user_id="user-uuid-1",
        )

    assert by_speaker == {
        "speaker_a": 3.34,
        "speaker_b": 4.5,
    }
    assert global_cps == round((3.34 + 4.5) / 2, 4)
    # Only the catalog miss (vt_speaker_a_111) should be queried in user_voices
    mock_user.assert_called_once()
    asked_for = mock_user.call_args[0][0]
    assert "vt_speaker_a_111" in asked_for
    assert "Chinese_Female_Anchor" not in asked_for


def test_lookup_per_speaker_skips_user_voice_lookup_when_catalog_covers_all():
    """Pure system-voice job → no user_voices request at all."""
    catalog_data = {
        "Chinese_Female_Anchor": {
            "chars_per_second": 4.5,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "...",
        },
    }
    with patch.object(vsc, "load_speed_catalog", return_value=catalog_data), \
         patch.object(vsc, "load_user_voice_speeds") as mock_user:
        vsc.lookup_per_speaker(
            {"speaker_a": "Chinese_Female_Anchor"},
            default_provider="minimax",
        )

    mock_user.assert_not_called()


def test_lookup_per_speaker_all_cloned_voices():
    """3-speaker job, all cloned voices. All three should resolve via
    user_voices and the global cps averages them."""
    user_voices_data = {
        "vt_a": {"chars_per_second": 3.34, "chars_per_second_by_model": {}, "speed_calibrated_at": ""},
        "vt_b": {"chars_per_second": 4.12, "chars_per_second_by_model": {}, "speed_calibrated_at": ""},
        "vt_c": {"chars_per_second": 3.78, "chars_per_second_by_model": {}, "speed_calibrated_at": ""},
    }
    with patch.object(vsc, "load_speed_catalog", return_value={}), \
         patch.object(vsc, "load_user_voice_speeds", return_value=user_voices_data):
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {
                "speaker_a": "vt_a",
                "speaker_b": "vt_b",
                "speaker_c": "vt_c",
            },
            default_provider="minimax",
            user_id="user-uuid-1",
        )

    assert by_speaker == {"speaker_a": 3.34, "speaker_b": 4.12, "speaker_c": 3.78}
    assert global_cps == round((3.34 + 4.12 + 3.78) / 3, 4)


def test_lookup_per_speaker_uncalibrated_clone_returns_none():
    """Cloned voice exists in user_voices but never got 测试语速 → uncalibrated.
    user_voices endpoint excludes those, so result is empty → falls through."""
    with patch.object(vsc, "load_speed_catalog", return_value={}), \
         patch.object(vsc, "load_user_voice_speeds", return_value={}):
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {"speaker_a": "vt_uncalibrated"},
            default_provider="minimax",
            user_id="user-uuid-1",
        )

    assert global_cps is None
    assert by_speaker == {}


def test_lookup_per_speaker_uses_by_model_when_available_for_user_voice():
    """tts_model match in user_voices.chars_per_second_by_model takes priority
    over the scalar chars_per_second (mirrors the catalog behaviour)."""
    user_voices_data = {
        "vt_a": {
            "chars_per_second": 4.0,  # scalar fallback
            "chars_per_second_by_model": {"speech-2.8-turbo": 3.34, "speech-2.8-hd": 3.50},
            "speed_calibrated_at": "",
        },
    }
    with patch.object(vsc, "load_speed_catalog", return_value={}), \
         patch.object(vsc, "load_user_voice_speeds", return_value=user_voices_data):
        _, by_speaker = vsc.lookup_per_speaker(
            {"speaker_a": "vt_a"},
            default_provider="minimax",
            tts_model="speech-2.8-turbo",
            user_id="user-uuid-1",
        )

    # Should pick by_model[speech-2.8-turbo] = 3.34, not scalar 4.0
    assert by_speaker == {"speaker_a": 3.34}


def test_lookup_per_speaker_skips_auto_voice():
    """voice_id="auto" means downstream matcher will pick — never lookup."""
    with patch.object(vsc, "load_speed_catalog") as mock_catalog, \
         patch.object(vsc, "load_user_voice_speeds") as mock_user:
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {"speaker_a": "auto"},
            default_provider="minimax",
        )

    assert global_cps is None
    assert by_speaker == {}
    # Nothing to look up → catalog might still get called (depends on impl),
    # but user_voices definitely shouldn't.
    mock_user.assert_not_called()


def test_lookup_per_speaker_partial_user_voice_calibration():
    """3-speaker job, catalog covers 1, user_voices covers 1, third uncalibrated.
    Only the 2 calibrated ones come back; uncalibrated speaker_c absent."""
    catalog_data = {
        "system_voice_1": {
            "chars_per_second": 4.5,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "",
        },
    }
    user_voices_data = {
        "vt_calibrated": {
            "chars_per_second": 3.5,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "",
        },
    }
    with patch.object(vsc, "load_speed_catalog", return_value=catalog_data), \
         patch.object(vsc, "load_user_voice_speeds", return_value=user_voices_data):
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {
                "speaker_a": "system_voice_1",
                "speaker_b": "vt_calibrated",
                "speaker_c": "vt_uncalibrated",  # no result anywhere
            },
            default_provider="minimax",
            user_id="user-uuid-1",
        )

    assert by_speaker == {"speaker_a": 4.5, "speaker_b": 3.5}
    assert "speaker_c" not in by_speaker
    assert global_cps == round((4.5 + 3.5) / 2, 4)


def test_lookup_per_speaker_skips_user_voices_fallback_when_no_user_id():
    """Security: callers without user_id MUST NOT trigger the user_voices
    lookup (which would be unscoped and could leak another user's cps).
    Catalog hits still work; cloned voices fall through to probe."""
    catalog_data = {
        "system_voice": {
            "chars_per_second": 4.5,
            "chars_per_second_by_model": {},
            "speed_calibrated_at": "",
        },
    }
    with patch.object(vsc, "load_speed_catalog", return_value=catalog_data), \
         patch.object(vsc, "load_user_voice_speeds") as mock_user:
        global_cps, by_speaker = vsc.lookup_per_speaker(
            {
                "speaker_a": "vt_cloned_voice",  # would normally fall back
                "speaker_b": "system_voice",     # catalog hit
            },
            default_provider="minimax",
            user_id=None,
        )

    # speaker_a falls through (no user_voices lookup attempted)
    assert by_speaker == {"speaker_b": 4.5}
    mock_user.assert_not_called()

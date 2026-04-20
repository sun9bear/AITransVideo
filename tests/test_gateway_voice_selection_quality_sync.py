"""Tests for V3 voice-selection quality_tier sync (fix for UI/DB price mismatch).

The bug this fixes: UI displayed 30/50 pts/min for MiniMax turbo/hd but
Gateway DB was hardcoded to standard=15 pts/min. This test locks in the
aggregation rules that feed into the Gateway-side DB sync.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from job_intercept import _aggregate_quality_tier_from_speakers


class TestAggregateQualityTier:
    def test_empty_speakers_falls_back_to_standard(self):
        tier, model = _aggregate_quality_tier_from_speakers([])
        assert tier == "standard"
        assert model is None

    def test_all_turbo_becomes_high(self):
        speakers = [
            {"speaker_id": "a", "tts_provider": "minimax", "minimax_model": "turbo"},
            {"speaker_id": "b", "tts_provider": "minimax", "minimax_model": "turbo"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "high"
        assert model == "speech-2.8-turbo"

    def test_any_hd_triggers_flagship(self):
        speakers = [
            {"speaker_id": "a", "tts_provider": "minimax", "minimax_model": "turbo"},
            {"speaker_id": "b", "tts_provider": "minimax", "minimax_model": "hd"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "flagship"
        assert model == "speech-2.8-hd"

    def test_all_hd_flagship(self):
        speakers = [
            {"speaker_id": "a", "tts_provider": "minimax", "minimax_model": "hd"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "flagship"
        assert model == "speech-2.8-hd"

    def test_no_minimax_keeps_standard_and_null_model(self):
        """CosyVoice/VolcEngine-only jobs stay at studio.standard.

        UI shows 15 pts/min for these providers (voice_selection_api.py:241-242),
        and we must NOT overwrite their tts_model (which was set at create time
        by job_intercept.py:146 / :144 to "cosyvoice-v3-flash" or "seed-tts-1.1").
        """
        speakers = [
            {"speaker_id": "a", "tts_provider": "cosyvoice"},
            {"speaker_id": "b", "tts_provider": "volcengine"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "standard"
        assert model is None

    def test_mixed_providers_minimax_hd_wins(self):
        """If any MiniMax speaker chose HD, job upgrades to flagship even if
        other speakers use CosyVoice. tts_model only reflects MiniMax's choice
        because TTS generator dispatches per-provider at runtime."""
        speakers = [
            {"speaker_id": "a", "tts_provider": "cosyvoice"},
            {"speaker_id": "b", "tts_provider": "minimax", "minimax_model": "hd"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "flagship"
        assert model == "speech-2.8-hd"

    def test_mixed_minimax_turbo_with_other_providers_high(self):
        speakers = [
            {"speaker_id": "a", "tts_provider": "cosyvoice"},
            {"speaker_id": "b", "tts_provider": "minimax", "minimax_model": "turbo"},
            {"speaker_id": "c", "tts_provider": "volcengine"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "high"
        assert model == "speech-2.8-turbo"

    def test_minimax_missing_model_field_defaults_to_turbo(self):
        """Defensive: if frontend somehow omits minimax_model, treat as turbo
        (cheaper) rather than hd, to avoid accidentally charging flagship."""
        speakers = [
            {"speaker_id": "a", "tts_provider": "minimax"},  # no minimax_model
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "high"
        assert model == "speech-2.8-turbo"

    def test_minimax_null_model_field_defaults_to_turbo(self):
        speakers = [
            {"speaker_id": "a", "tts_provider": "minimax", "minimax_model": None},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "high"
        assert model == "speech-2.8-turbo"

    def test_case_insensitive_provider_match(self):
        """Provider string comparison should tolerate casing drift from frontend."""
        speakers = [
            {"speaker_id": "a", "tts_provider": "MiniMax", "minimax_model": "HD"},
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "flagship"
        assert model == "speech-2.8-hd"

    def test_non_dict_speakers_ignored(self):
        """Defensive against payload schema drift — non-dict entries skipped."""
        speakers = [
            "invalid",
            {"speaker_id": "a", "tts_provider": "minimax", "minimax_model": "hd"},
            42,
            None,
        ]
        tier, model = _aggregate_quality_tier_from_speakers(speakers)
        assert tier == "flagship"
        assert model == "speech-2.8-hd"

"""Tests for voice_label_tasks — subprocess-based labeling.

Tests mock subprocess.run to avoid calling real Gemini/TTS APIs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from services.jobs.voice_label_tasks import (
    MAX_AUDIO_VOICES,
    MAX_TEXT_VOICES,
    run_audio_profiling,
    run_text_labeling,
)


def _mock_subprocess_ok(labels: dict) -> MagicMock:
    """Create a mock subprocess.CompletedProcess with OK result."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = json.dumps({"ok": True, "labels": labels})
    result.stderr = ""
    return result


def _mock_subprocess_fail(error: str) -> MagicMock:
    result = MagicMock()
    result.returncode = 1
    result.stdout = json.dumps({"ok": False, "error": error})
    result.stderr = error
    return result


# Sample voice metadata (as Gateway would send)
_VOICE_META = [
    {"voice_id": "v1", "display_name": "测试 1", "scene": "通用", "language": "zh", "provider_config": {"resource_id": "seed-tts-1.0"}},
    {"voice_id": "v2", "display_name": "测试 2", "scene": "视频配音", "language": "zh", "provider_config": {"resource_id": "seed-tts-2.0"}},
]

# Dynamic voice NOT in static catalog
_DYNAMIC_VOICE = [
    {"voice_id": "new_dynamic_voice_xyz", "display_name": "全新动态音色", "scene": "通用", "language": "zh", "provider_config": {"resource_id": "seed-tts-1.0"}},
]


class TestRunTextLabeling:
    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_success_with_metadata(self, mock_run) -> None:
        mock_run.return_value = _mock_subprocess_ok({
            "v1": {"age_group": "young", "persona_style": "warm", "energy_level": "medium"},
            "v2": {"age_group": "middle", "persona_style": "serious", "energy_level": "low"},
        })

        result = run_text_labeling(_VOICE_META)

        assert len(result) == 2
        assert result[0]["voice_id"] == "v1"
        assert result[0]["age_group"] == "young"

        # Verify script received voices metadata (not just voice_ids)
        stdin_data = json.loads(mock_run.call_args.kwargs.get("input", "{}"))
        assert "voices" in stdin_data
        assert stdin_data["voices"][0]["display_name"] == "测试 1"

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_dynamic_voice_not_in_static_catalog(self, mock_run) -> None:
        """Dynamic voice (not in static VOICES_1_0/VOICES_2_0) still works."""
        mock_run.return_value = _mock_subprocess_ok({
            "new_dynamic_voice_xyz": {"age_group": "young", "persona_style": "neutral", "energy_level": "medium"},
        })

        result = run_text_labeling(_DYNAMIC_VOICE)

        assert len(result) == 1
        assert result[0]["voice_id"] == "new_dynamic_voice_xyz"

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_script_failure(self, mock_run) -> None:
        mock_run.return_value = _mock_subprocess_fail("GEMINI_API_KEY not set")

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            run_text_labeling(_VOICE_META[:1])

    def test_exceeds_limit(self) -> None:
        voices = [{"voice_id": f"v{i}", "display_name": f"V{i}", "scene": ""} for i in range(MAX_TEXT_VOICES + 1)]
        with pytest.raises(ValueError, match=f"最多 {MAX_TEXT_VOICES}"):
            run_text_labeling(voices)

    def test_empty_voices_raises(self) -> None:
        with pytest.raises(ValueError, match="为空"):
            run_text_labeling([])

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_empty_labels_raises(self, mock_run) -> None:
        """Script returns ok=true but labels={} → must raise, not silent success."""
        mock_run.return_value = _mock_subprocess_ok({})
        with pytest.raises(RuntimeError, match="未生成"):
            run_text_labeling(_VOICE_META[:1])


class TestRunAudioProfiling:
    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_success(self, mock_run) -> None:
        mock_run.return_value = _mock_subprocess_ok({
            "v1": {
                "pitch_level": "high", "warmth": "medium", "authority": "low",
                "intimacy": "medium", "energy_level": "medium", "brightness": "high",
                "maturity": "young", "delivery_style": "companion",
                "texture_tags": ["soft", "airy"], "childlike": False,
            },
        })

        result = run_audio_profiling(_VOICE_META[:1], "round1")

        assert len(result) == 1
        assert result[0]["voice_id"] == "v1"
        assert result[0]["pitch_level"] == "high"

        # Verify script received voices metadata + round_name
        stdin_data = json.loads(mock_run.call_args.kwargs.get("input", "{}"))
        assert "voices" in stdin_data
        assert stdin_data["round_name"] == "round1"

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_dynamic_voice_for_audio(self, mock_run) -> None:
        """Dynamic voice works for audio profiling too."""
        mock_run.return_value = _mock_subprocess_ok({
            "new_dynamic_voice_xyz": {"pitch_level": "mid", "warmth": "high"},
        })

        result = run_audio_profiling(_DYNAMIC_VOICE, "round2")
        assert len(result) == 1
        assert result[0]["voice_id"] == "new_dynamic_voice_xyz"

    def test_invalid_round(self) -> None:
        with pytest.raises(ValueError, match="Invalid round"):
            run_audio_profiling(_VOICE_META[:1], "round4")

    def test_exceeds_limit(self) -> None:
        voices = [{"voice_id": f"v{i}", "display_name": f"V{i}"} for i in range(MAX_AUDIO_VOICES + 1)]
        with pytest.raises(ValueError, match=f"最多 {MAX_AUDIO_VOICES}"):
            run_audio_profiling(voices, "round1")

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_subprocess_timeout(self, mock_run) -> None:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=300)

        with pytest.raises(TimeoutError, match="超时"):
            run_audio_profiling(_VOICE_META[:1], "round1")

    @patch("services.jobs.voice_label_tasks.subprocess.run")
    def test_empty_labels_raises(self, mock_run) -> None:
        mock_run.return_value = _mock_subprocess_ok({})
        with pytest.raises(RuntimeError, match="未生成"):
            run_audio_profiling(_VOICE_META[:1], "round1")

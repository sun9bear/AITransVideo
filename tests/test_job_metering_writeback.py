"""Tests for V3-4 pipeline metering writeback endpoint.

Tests the update_job_metering handler and the pipeline _report_job_metering callback.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from job_intercept import update_job_metering


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _make_job(*, job_id="job-m-1", metering_snapshot=None):
    return SimpleNamespace(
        job_id=job_id,
        metering_snapshot=metering_snapshot,
    )


class TestUpdateJobMetering:
    def test_merges_fields_into_snapshot(self):
        job = _make_job(metering_snapshot={"credits_estimated": 50, "service_mode": "express"})
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = _make_request({
            "final_cn_chars": 1200,
            "rewrite_triggered": True,
            "rewrite_count": 3,
            "tts_billed_chars": 1200,
            "first_tts_billed_chars": 900,
            "probe_tts_billed_chars": 40,
            "post_tts_resynth_billed_chars": 220,
            "post_edit_resynth_billed_chars": 40,
            "llm_call_count": 7,
            "llm_input_tokens": 1234,
            "llm_output_tokens": 456,
            "pre_tts_rewrite_llm_calls": 2,
            "pre_tts_rewrite_llm_tokens": 300,
            "s2_pass1_llm_calls": 1,
            "transcription_method": "assemblyai",
            "asr_provider_cost_status": "ignored_low_cost",
            "pre_tts_rewrite_count": 1,
            "pre_tts_contradiction_count": 1,
            "pre_tts_contradiction_rate": 1.0,
            "harmful_pre_tts_contradiction_count": 1,
            "harmful_pre_tts_contradiction_rate": 1.0,
            "pre_tts_rewrite_rejected_count": 1,
            "pre_tts_rewrite_rejected_reason_distribution": {"strict_below_floor": 1},
            "pre_tts_rewrite_retry_attempt_count": 1,
            "pre_tts_rewrite_retry_accepted_count": 0,
            "micro_segment_count": 2,
            "short_segment_count": 4,
            "short_segment_needs_review_count": 3,
            "short_segment_force_dsp_count": 3,
            "force_dsp_severity_distribution": {"low": 2, "medium": 1},
            "force_dsp_review_suppressed_count": 2,
            "short_merge_candidate_count": 1,
            "short_merge_blocked_cross_speaker_count": 1,
            "short_merge_applied_count": 1,
            "short_merge_absorbed_count": 2,
            "pre_tts_rewrite_events": [
                {
                    "segment_id": 1,
                    "direction": "overshoot",
                    "task": "s5_rewrite",
                    "estimate_ms": 26000,
                    "target_ms": 20000,
                    "pre_chars": 120,
                    "post_chars": 80,
                    "post_tts_first_pass_ms": 16000,
                    "contradiction": True,
                    "harmful_contradiction": True,
                    "retry_attempted": False,
                    "retry_accepted": False,
                    "initial_rejected_reason": "",
                }
            ],
            "pre_tts_rewrite_rejected_events": [
                {
                    "segment_id": 2,
                    "direction": "overshoot",
                    "reason": "strict_below_floor",
                    "estimate_ms": 30000,
                    "target_ms": 22000,
                    "pre_chars": 160,
                    "post_chars": 100,
                    "lower_chars": 130,
                    "upper_chars": 145,
                    "retry_attempted": True,
                }
            ],
        })

        resp = _run(update_job_metering(req, "job-m-1", db))
        body = json.loads(resp.body)

        assert resp.status_code == 200
        assert body["ok"] is True
        # Existing fields preserved
        assert job.metering_snapshot["credits_estimated"] == 50
        assert job.metering_snapshot["service_mode"] == "express"
        # New fields merged
        assert job.metering_snapshot["final_cn_chars"] == 1200
        assert job.metering_snapshot["rewrite_triggered"] is True
        assert job.metering_snapshot["rewrite_count"] == 3
        assert job.metering_snapshot["tts_billed_chars"] == 1200
        assert job.metering_snapshot["first_tts_billed_chars"] == 900
        assert job.metering_snapshot["probe_tts_billed_chars"] == 40
        assert job.metering_snapshot["post_tts_resynth_billed_chars"] == 220
        assert job.metering_snapshot["post_edit_resynth_billed_chars"] == 40
        assert job.metering_snapshot["llm_call_count"] == 7
        assert job.metering_snapshot["llm_input_tokens"] == 1234
        assert job.metering_snapshot["llm_output_tokens"] == 456
        assert job.metering_snapshot["pre_tts_rewrite_llm_calls"] == 2
        assert job.metering_snapshot["pre_tts_rewrite_llm_tokens"] == 300
        assert job.metering_snapshot["s2_pass1_llm_calls"] == 1
        assert job.metering_snapshot["transcription_method"] == "assemblyai"
        assert job.metering_snapshot["asr_provider_cost_status"] == "ignored_low_cost"
        assert job.metering_snapshot["pre_tts_rewrite_count"] == 1
        assert job.metering_snapshot["pre_tts_contradiction_count"] == 1
        assert job.metering_snapshot["harmful_pre_tts_contradiction_count"] == 1
        assert job.metering_snapshot["pre_tts_rewrite_rejected_count"] == 1
        assert job.metering_snapshot["pre_tts_rewrite_retry_attempt_count"] == 1
        assert job.metering_snapshot["micro_segment_count"] == 2
        assert job.metering_snapshot["short_segment_count"] == 4
        assert job.metering_snapshot["short_segment_needs_review_count"] == 3
        assert job.metering_snapshot["short_segment_force_dsp_count"] == 3
        assert job.metering_snapshot["force_dsp_severity_distribution"] == {"low": 2, "medium": 1}
        assert job.metering_snapshot["force_dsp_review_suppressed_count"] == 2
        assert job.metering_snapshot["short_merge_candidate_count"] == 1
        assert job.metering_snapshot["short_merge_blocked_cross_speaker_count"] == 1
        assert job.metering_snapshot["short_merge_applied_count"] == 1
        assert job.metering_snapshot["short_merge_absorbed_count"] == 2
        assert job.metering_snapshot["pre_tts_rewrite_events"][0]["contradiction"] is True

    def test_creates_snapshot_if_none(self):
        job = _make_job(metering_snapshot=None)
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = _make_request({"final_cn_chars": 800, "rewrite_triggered": False})
        resp = _run(update_job_metering(req, "job-m-2", db))

        assert resp.status_code == 200
        assert job.metering_snapshot["final_cn_chars"] == 800
        assert job.metering_snapshot["rewrite_triggered"] is False

    def test_ignores_unknown_keys(self):
        job = _make_job(metering_snapshot={})
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        req = _make_request({"unknown_field": 42, "evil_key": "hack"})
        resp = _run(update_job_metering(req, "job-m-3", db))
        body = json.loads(resp.body)

        assert resp.status_code == 200
        assert body.get("note") == "no recognized metering keys"
        assert "unknown_field" not in (job.metering_snapshot or {})

    def test_empty_body_returns_400(self):
        db = AsyncMock()
        req = MagicMock()
        req.body = AsyncMock(return_value=b"")
        resp = _run(update_job_metering(req, "job-m-4", db))
        assert resp.status_code == 400

    def test_job_not_found_returns_200_skipped(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        req = _make_request({"final_cn_chars": 500})
        resp = _run(update_job_metering(req, "nonexistent", db))
        body = json.loads(resp.body)

        assert resp.status_code == 200
        assert "skipped" in body.get("note", "")


class TestTTSResultBilledChars:
    """V3-5: TTSResult includes billed_chars from TTS generator layer."""

    def test_tts_result_has_billed_chars_field(self):
        _src_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
        if _src_dir not in sys.path:
            sys.path.insert(0, _src_dir)
        from services.tts.tts_generator import TTSResult

        result = TTSResult(segment_id=1, audio_path="/tmp/test.wav", duration_ms=3000, voice_id="v1")
        assert result.billed_chars == 0  # default

        result.billed_chars = 42
        assert result.billed_chars == 42


class TestBilledCharsPerProvider:
    """V3-5 truth gap: verify per-provider billing multipliers at generator layer.

    These tests verify the billing rules match the frozen V3 doc:
    - MiniMax: 1 姹夊瓧 = 2 璁¤垂瀛楃
    - CosyVoice: 1 姹夊瓧 = 2 璁¤垂瀛楃
    - VolcEngine: direct char billing (no multiplier)
    - MiMo: token-based, billed_chars = 0 (unknown)
    """

    def _setup(self):
        _src_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
        if _src_dir not in sys.path:
            sys.path.insert(0, _src_dir)

    def test_minimax_billed_chars_is_2x(self):
        """MiniMax: 10 CN chars -> 20 billed chars (2x multiplier)."""
        self._setup()
        from services.tts.tts_generator import TTSGenerator, TTSConfig, TTSResult
        from services.gemini.translator import DubbingSegment
        from unittest.mock import patch, MagicMock

        seg = DubbingSegment(
            segment_id=1,
            speaker_id="spk_0",
            display_name="S",
            voice_id="v1",
            start_ms=0,
            end_ms=3000,
            target_duration_ms=2800,
            source_text="test",
            cn_text="十个中文字符的测试",
        )
        assert len("十个中文字符的测试") == 9

        # Mock the actual MiniMax API call to avoid network
        fake_response = {
            "base_resp": {"status_code": 0, "status_msg": "ok"},
            "data": {"audio": "00" * 100},
        }
        with patch("services.tts.tts_generator._post_json", return_value=fake_response), \
             patch("services.tts.tts_generator._ffprobe_duration_ms", return_value=2500), \
             patch("services.tts.tts_generator.atomic_write_bytes"):
            config = TTSConfig(api_key="test-key")
            gen = TTSGenerator(config)
            gen._job_provider = "minimax"
            import tempfile
            result = gen._generate_one(seg, tempfile.gettempdir(), provider="minimax")

        # Frozen doc: 1 姹夊瓧 = 2 璁¤垂瀛楃
        assert result.billed_chars == 9 * 2  # 18

    def test_cosyvoice_billed_chars_is_2x(self):
        """CosyVoice: 5 CN chars 鈫?10 billed chars (2x multiplier)."""
        self._setup()
        from services.tts.tts_generator import TTSGenerator, TTSConfig, TTSResult
        from services.gemini.translator import DubbingSegment
        from unittest.mock import patch

        seg = DubbingSegment(
            segment_id=1,
            speaker_id="spk_0",
            display_name="S",
            voice_id="v1",
            start_ms=0,
            end_ms=3000,
            target_duration_ms=2800,
            source_text="test",
            cn_text="五个字测试",
        )
        assert len("五个字测试") == 5

        # Mock the CosyVoice generation method
        from services.tts.tts_generator import TTSResult as TR
        fake_result = TR(segment_id=1, audio_path="/tmp/fake.wav", duration_ms=2500, voice_id="v1")
        with patch.object(TTSGenerator, "_generate_one_cosyvoice", return_value=fake_result):
            config = TTSConfig(api_key="test-key")
            gen = TTSGenerator(config)
            result = gen._generate_one(seg, "/tmp", provider="cosyvoice")

        assert result.billed_chars == 5 * 2  # 10

    def test_volcengine_billed_chars_is_1x(self):
        """VolcEngine: 7 CN chars 鈫?7 billed chars (no multiplier)."""
        self._setup()
        from services.tts.tts_generator import TTSGenerator, TTSConfig, TTSResult
        from services.gemini.translator import DubbingSegment
        from unittest.mock import patch

        seg = DubbingSegment(
            segment_id=1, speaker_id="spk_0", display_name="S",
            voice_id="v1", start_ms=0, end_ms=3000, target_duration_ms=2800,
            source_text="test", cn_text="七字豆包测试呀",
        )
        assert len("七字豆包测试呀") == 7

        from services.tts.tts_generator import TTSResult as TR
        fake_result = TR(segment_id=1, audio_path="/tmp/fake.wav", duration_ms=2500, voice_id="v1")
        with patch.object(TTSGenerator, "_generate_one_volcengine", return_value=fake_result):
            config = TTSConfig(api_key="test-key")
            gen = TTSGenerator(config)
            result = gen._generate_one(seg, "/tmp", provider="volcengine")

        assert result.billed_chars == 7  # direct, no multiplier

    def test_mimo_billed_chars_is_zero(self):
        """MiMo: token-based billing 鈫?billed_chars stays 0 (unknown)."""
        self._setup()
        from services.tts.tts_generator import TTSGenerator, TTSConfig, TTSResult
        from services.gemini.translator import DubbingSegment
        from unittest.mock import patch

        seg = DubbingSegment(
            segment_id=1, speaker_id="spk_0", display_name="S",
            voice_id="v1", start_ms=0, end_ms=3000, target_duration_ms=2800,
            source_text="test", cn_text="MiMo测试文本",
        )

        from services.tts.tts_generator import TTSResult as TR
        fake_result = TR(segment_id=1, audio_path="/tmp/fake.wav", duration_ms=2500, voice_id="v1")
        with patch.object(TTSGenerator, "_generate_one_mimo", return_value=fake_result):
            config = TTSConfig(api_key="test-key")
            gen = TTSGenerator(config)
            result = gen._generate_one(seg, "/tmp", provider="mimo")

        assert result.billed_chars == 0  # token-based, truthful value unavailable


class TestReportJobMeteringCallback:
    """Test the pipeline-side _report_job_metering function."""

    def _setup(self):
        _src_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
        if _src_dir not in sys.path:
            sys.path.insert(0, _src_dir)
        from pipeline.process import _report_job_metering
        return _report_job_metering

    def _capture_call(self, _report_job_metering, job_id, segments, **kwargs):
        """Call _report_job_metering and capture the HTTP request body."""
        import unittest.mock

        captured = {}

        class FakeResp:
            status = 200
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, **kw):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        with unittest.mock.patch("urllib.request.urlopen", fake_urlopen):
            _report_job_metering(job_id, segments, **kwargs)

        return captured.get("body", {})

    def _capture_call_with_billed(self, _report_job_metering, job_id, segments, *, tts_billed_chars):
        return self._capture_call(_report_job_metering, job_id, segments, tts_billed_chars=tts_billed_chars)

    def test_compat_with_merged_cn_text_objects(self):
        """Legacy/compat path: objects with merged_cn_text (SemanticBlock shape)."""
        fn = self._setup()
        blocks = [
            SimpleNamespace(merged_cn_text="你好世界这是测试", rewrite_count=0),
            SimpleNamespace(merged_cn_text="第二段文本长一点哦", rewrite_count=2),
            SimpleNamespace(merged_cn_text="第三段", rewrite_count=0),
        ]
        body = self._capture_call(fn, "test-compat", blocks)

        assert body["final_cn_chars"] == 20  # 8 + 9 + 3
        assert body["rewrite_triggered"] is True
        assert body["rewrite_count"] == 2
        # Without tts_billed_chars kwarg, field is not included
        assert "tts_billed_chars" not in body

    def test_tts_billed_chars_from_tts_layer(self):
        """V3-5: tts_billed_chars passed from TTS generator layer."""
        fn = self._setup()
        blocks = [
            SimpleNamespace(cn_text="你好", rewrite_count=0),
        ]
        body = self._capture_call_with_billed(fn, "test-billed", blocks, tts_billed_chars=42)

        assert body["tts_billed_chars"] == 42
        assert body["final_cn_chars"] == 2

    def test_extra_usage_metering_fields_are_reported(self):
        fn = self._setup()
        blocks = [
            SimpleNamespace(cn_text="你好", rewrite_count=0),
        ]
        body = self._capture_call(
            fn,
            "test-usage-metering",
            blocks,
            extra_metering={
                "usage_events_count": 12,
                "first_tts_billed_chars": 120,
                "probe_tts_billed_chars": 10,
                "post_tts_resynth_billed_chars": 20,
                "post_edit_resynth_tts_billed_chars": 8,
                "llm_call_count": 5,
                "pre_tts_rewrite_llm_calls": 2,
                "pre_tts_rewrite_llm_tokens": 300,
                "s2_speaker_verifier_llm_calls": 1,
                "transcription_method": "assemblyai",
                "asr_provider_cost_status": "ignored_low_cost",
            },
        )

        assert body["usage_events_count"] == 12
        assert body["first_tts_billed_chars"] == 120
        assert body["probe_tts_billed_chars"] == 10
        assert body["post_tts_resynth_billed_chars"] == 20
        assert body["post_edit_resynth_tts_billed_chars"] == 8
        assert body["llm_call_count"] == 5
        assert body["pre_tts_rewrite_llm_calls"] == 2
        assert body["pre_tts_rewrite_llm_tokens"] == 300
        assert body["s2_speaker_verifier_llm_calls"] == 1
        assert body["transcription_method"] == "assemblyai"
        assert body["asr_provider_cost_status"] == "ignored_low_cost"

    def test_pre_tts_rewrite_events_are_reported(self):
        fn = self._setup()
        from services.gemini.translator import DubbingSegment

        segment = DubbingSegment(
            segment_id=7,
            speaker_id="spk_0",
            display_name="Speaker",
            voice_id="v1",
            start_ms=0,
            end_ms=4_000,
            target_duration_ms=4_000,
            source_text="Hello",
            cn_text="改写后文本",
            rewrite_count=1,
            pre_tts_rewrite_direction="overshoot",
            pre_tts_estimate_ms=26_666,
            pre_tts_target_ms=20_000,
            pre_tts_pre_chars=120,
            pre_tts_post_chars=5,
            pre_tts_post_tts_first_pass_ms=16_000,
            pre_tts_contradiction=True,
            pre_tts_harmful_contradiction=True,
        )

        body = self._capture_call(fn, "test-pre-tts", [segment])

        assert body["pre_tts_rewrite_count"] == 1
        assert body["pre_tts_contradiction_count"] == 1
        assert body["pre_tts_contradiction_rate"] == 1.0
        assert body["harmful_pre_tts_contradiction_count"] == 1
        assert body["harmful_pre_tts_contradiction_rate"] == 1.0
        assert body["micro_segment_count"] == 0
        assert body["short_segment_count"] == 1
        assert body["short_segment_needs_review_count"] == 0
        assert body["short_segment_force_dsp_count"] == 0
        assert body["pre_tts_rewrite_events"] == [
            {
                "segment_id": 7,
                "direction": "overshoot",
                "task": "s5_rewrite",
                "estimate_ms": 26_666,
                "target_ms": 20_000,
                "pre_chars": 120,
                "post_chars": 5,
                "post_tts_first_pass_ms": 16_000,
                "contradiction": True,
                "harmful_contradiction": True,
                "retry_attempted": False,
                "retry_accepted": False,
                "initial_rejected_reason": "",
            }
        ]

    def test_pre_tts_rewrite_rejected_events_are_reported(self):
        fn = self._setup()
        from services.gemini.translator import DubbingSegment

        segment = DubbingSegment(
            segment_id=8,
            speaker_id="spk_0",
            display_name="Speaker",
            voice_id="v1",
            start_ms=0,
            end_ms=30_000,
            target_duration_ms=30_000,
            source_text="Hello",
            cn_text="原始文本",
            pre_tts_rewrite_rejected=True,
            pre_tts_rewrite_rejected_reason="strict_below_floor",
            pre_tts_rewrite_rejected_direction="overshoot",
            pre_tts_rewrite_rejected_estimate_ms=44_444,
            pre_tts_rewrite_rejected_target_ms=30_000,
            pre_tts_rewrite_rejected_pre_chars=200,
            pre_tts_rewrite_rejected_post_chars=140,
            pre_tts_rewrite_rejected_lower_chars=160,
            pre_tts_rewrite_rejected_upper_chars=173,
            pre_tts_rewrite_retry_attempted=True,
        )

        body = self._capture_call(fn, "test-pre-tts-rejected", [segment])

        assert body["pre_tts_rewrite_rejected_count"] == 1
        assert body["pre_tts_rewrite_rejected_reason_distribution"] == {
            "strict_below_floor": 1
        }
        assert body["pre_tts_rewrite_retry_attempt_count"] == 1
        assert body["pre_tts_rewrite_retry_accepted_count"] == 0
        assert body["pre_tts_rewrite_rejected_events"] == [
            {
                "segment_id": 8,
                "direction": "overshoot",
                "reason": "strict_below_floor",
                "estimate_ms": 44_444,
                "target_ms": 30_000,
                "pre_chars": 200,
                "post_chars": 140,
                "lower_chars": 160,
                "upper_chars": 173,
                "retry_attempted": True,
            }
        ]

    def test_real_dubbing_segment_path(self):
        """Real production path: DubbingSegment with cn_text."""
        fn = self._setup()
        from services.gemini.translator import DubbingSegment

        segments = [
            DubbingSegment(
                segment_id=1, speaker_id="spk_0", display_name="Speaker",
                voice_id="v1", start_ms=0, end_ms=5000, target_duration_ms=4500,
                source_text="Hello world", cn_text="你好世界呀",
                rewrite_count=0,
            ),
            DubbingSegment(
                segment_id=2, speaker_id="spk_0", display_name="Speaker",
                voice_id="v1", start_ms=5000, end_ms=10000, target_duration_ms=4500,
                source_text="Good morning", cn_text="早上好",
                rewrite_count=1,
            ),
        ]

        body = self._capture_call(fn, "test-real", segments)

        # "浣犲ソ涓栫晫鍛€" (5) + "鏃╀笂濂? (3) = 8
        assert body["final_cn_chars"] == 8
        assert body["rewrite_triggered"] is True
        assert body["rewrite_count"] == 1
        assert body["short_segment_count"] == 2

    def test_real_dubbing_segment_no_rewrite(self):
        """DubbingSegment with no rewrites 鈫?rewrite_triggered=False."""
        fn = self._setup()
        from services.gemini.translator import DubbingSegment

        seg = DubbingSegment(
            segment_id=1, speaker_id="spk_0", display_name="S",
            voice_id="v1", start_ms=0, end_ms=3000, target_duration_ms=2800,
            source_text="Test", cn_text="测试文本",
            rewrite_count=0,
        )
        body = self._capture_call(fn, "test-no-rewrite", [seg])

        assert body["final_cn_chars"] == 4
        assert body["rewrite_triggered"] is False
        assert body["rewrite_count"] == 0

    def test_short_merge_applied_metrics_are_reported(self):
        fn = self._setup()
        from services.gemini.translator import DubbingSegment

        segment = DubbingSegment(
            segment_id=5,
            speaker_id="spk_0",
            display_name="S",
            voice_id="v1",
            start_ms=0,
            end_ms=5_000,
            target_duration_ms=5_000,
            source_text="Merged text",
            cn_text="合并文本",
            short_merge_applied=True,
            short_merge_absorbed_segment_ids="2,4",
        )

        body = self._capture_call(fn, "test-short-merge", [segment])

        assert body["short_merge_applied_count"] == 1
        assert body["short_merge_absorbed_count"] == 2


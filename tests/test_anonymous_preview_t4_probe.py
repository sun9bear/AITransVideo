"""T4 unit + integration tests for gateway/anonymous_preview_probe.py.

Unit tests mock subprocess — no real ffmpeg required.
Integration tests are skipped when ffmpeg is not on PATH.

180.04s boundary conclusion (documented here per plan §6 T4 spec):
    evaluate_free_duration_cap(180040ms, max_minutes=3) uses strict ``>``
    comparison: (180040 / 60000) = 3.0006... > 3.0 → REJECT_OVER_CAP.
    Therefore 180.04s teaser FAILS the cap gate.
    The re-encode teaser cut at exactly 180s will typically produce a
    teaser slightly *below* 180s (re-encode rounds to frame boundary), so
    in practice the cap gate passes for a 180s-capped teaser.
    Only a teaser whose measured duration > 180.000s triggers REJECT_OVER_CAP.

Test matrix:
    Unit:
      - probe_source: normal / missing duration / non-numeric duration /
        NaN / inf / 0 / negative / timeout / non-zero exit / not found
      - cut_teaser: ffmpeg not found / timeout / non-zero exit /
        post-cut probe fails / happy path
      - build_probe_fn: source probe fails / cut fails / cap gate
        (179s / 180s / 180.04s / 181s) / happy path
      - failure reason redaction: no 'ffmpeg', 'ffprobe', path sep, '-i'
    Integration (requires real ffmpeg):
      - generate 5s lavfi video → cut_teaser → duration ≈ 5s, file exists
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

from gateway.anonymous_preview_probe import (
    TeaserResult,
    build_probe_fn,
    choose_teaser_cut_seconds,
    cut_teaser,
    probe_source,
)

# ---------------------------------------------------------------------------
# Helpers — build fake ffprobe JSON output
# ---------------------------------------------------------------------------


def _ffprobe_json(
    duration: Any = "120.0",
    streams: list[dict] | None = None,
    format_name: str = "mov,mp4,m4a,3gp,3g2,mj2",
) -> str:
    """Build minimal ffprobe JSON output."""
    result: dict = {
        "format": {"format_name": format_name},
        "streams": streams if streams is not None else [{"codec_type": "video"}, {"codec_type": "audio"}],
    }
    if duration is not None:
        result["format"]["duration"] = duration
    return json.dumps(result)


def _make_completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# probe_source — unit tests
# ---------------------------------------------------------------------------


class TestProbeSourceUnit:
    def test_happy_path_returns_ok_with_duration_and_audio(self, tmp_path):
        fake = _make_completed(stdout=_ffprobe_json("120.5"))
        with patch("subprocess.run", return_value=fake) as mock_run:
            result = probe_source(tmp_path / "video.mp4")
        mock_run.assert_called_once()
        assert result["ok"] is True
        assert abs(result["duration_seconds"] - 120.5) < 1e-6
        assert result["has_audio"] is True
        assert result["failure_reason"] is None

    def test_missing_duration_key_returns_fail_closed(self, tmp_path):
        payload = json.dumps({"format": {"format_name": "mp4"}, "streams": []})
        fake = _make_completed(stdout=payload)
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False
        assert result["failure_reason"] is not None

    def test_non_numeric_duration_returns_fail_closed(self, tmp_path):
        fake = _make_completed(stdout=_ffprobe_json(duration="not_a_number"))
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_nan_duration_returns_fail_closed(self, tmp_path):
        # JSON cannot represent NaN natively; simulate by patching float conversion
        payload = json.dumps({"format": {"format_name": "mp4", "duration": "nan"}, "streams": []})
        fake = _make_completed(stdout=payload)
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_inf_duration_returns_fail_closed(self, tmp_path):
        payload = json.dumps({"format": {"format_name": "mp4", "duration": "inf"}, "streams": []})
        fake = _make_completed(stdout=payload)
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_zero_duration_returns_fail_closed(self, tmp_path):
        fake = _make_completed(stdout=_ffprobe_json(duration="0"))
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_negative_duration_returns_fail_closed(self, tmp_path):
        fake = _make_completed(stdout=_ffprobe_json(duration="-5.0"))
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_timeout_returns_fail_closed(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False
        assert "timed out" in result["failure_reason"]

    def test_nonzero_exit_returns_fail_closed(self, tmp_path):
        fake = _make_completed(returncode=1)
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False

    def test_file_not_found_returns_fail_closed(self, tmp_path):
        with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe")):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is False
        assert result["failure_reason"] is not None

    def test_no_audio_stream_has_audio_false(self, tmp_path):
        fake = _make_completed(stdout=_ffprobe_json(streams=[{"codec_type": "video"}]))
        with patch("subprocess.run", return_value=fake):
            result = probe_source(tmp_path / "v.mp4")
        assert result["ok"] is True
        assert result["has_audio"] is False


# ---------------------------------------------------------------------------
# cut_teaser — unit tests
# ---------------------------------------------------------------------------


class TestCutTeaserUnit:
    def _ffprobe_ok(self) -> MagicMock:
        return _make_completed(stdout=_ffprobe_json("179.8"))

    def test_happy_path_returns_teaser_result(self, tmp_path):
        ffmpeg_ok = _make_completed(returncode=0)
        ffprobe_ok = self._ffprobe_ok()
        with patch("subprocess.run", side_effect=[ffmpeg_ok, ffprobe_ok]):
            result = cut_teaser(tmp_path / "src.mp4", tmp_path / "teaser.mp4")
        assert result.failure_reason is None
        assert abs(result.duration_seconds - 179.8) < 1e-6
        assert result.has_audio is True

    def test_ffmpeg_not_found_returns_failure(self, tmp_path):
        with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
            result = cut_teaser(tmp_path / "src.mp4", tmp_path / "teaser.mp4")
        assert result.failure_reason is not None
        assert "unavailable" in result.failure_reason

    def test_ffmpeg_timeout_returns_failure(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 600)):
            result = cut_teaser(tmp_path / "src.mp4", tmp_path / "teaser.mp4")
        assert result.failure_reason is not None
        assert "timed out" in result.failure_reason

    def test_ffmpeg_nonzero_exit_returns_failure(self, tmp_path):
        fake = _make_completed(returncode=1)
        with patch("subprocess.run", return_value=fake):
            result = cut_teaser(tmp_path / "src.mp4", tmp_path / "teaser.mp4")
        assert result.failure_reason is not None

    def test_post_cut_ffprobe_fails_returns_failure(self, tmp_path):
        ffmpeg_ok = _make_completed(returncode=0)
        ffprobe_fail = _make_completed(returncode=1)
        with patch("subprocess.run", side_effect=[ffmpeg_ok, ffprobe_fail]):
            result = cut_teaser(tmp_path / "src.mp4", tmp_path / "teaser.mp4")
        assert result.failure_reason is not None
        assert "teaser probe" in result.failure_reason


# ---------------------------------------------------------------------------
# build_probe_fn — unit tests
# ---------------------------------------------------------------------------


class TestChooseTeaserCutSeconds:
    """软上限切点选择（产品裁定 2026-06-12）：静音对齐 / 整段保留 / 回落。"""

    def test_unknown_duration_falls_back_to_target(self):
        assert choose_teaser_cut_seconds(Path("x.mp4"), None, 180.0) == 180.0

    def test_source_within_soft_cap_keeps_whole_video(self):
        # 183s 源不该在 180s 处腰斩——返回 195（-t 超源时长 = 整段保留）
        assert choose_teaser_cut_seconds(Path("x.mp4"), 183.0, 180.0) == 195.0

    def test_silence_aligned_pick_closest_to_target(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 10.2\n"
            "[silencedetect @ 0x1] silence_start: 22.9\n"
        )
        proc = _make_completed(stderr=stderr, returncode=0)
        with patch("subprocess.run", return_value=proc):
            cut = choose_teaser_cut_seconds(Path("x.mp4"), 300.0, 180.0)
        # 窗口 lo=165 → 候选绝对时间 175.2 / 187.9；离 180 最近 = 175.2
        assert abs(cut - 175.2) < 1e-6

    def test_no_silence_falls_back_to_target(self):
        proc = _make_completed(stderr="", returncode=0)
        with patch("subprocess.run", return_value=proc):
            assert choose_teaser_cut_seconds(Path("x.mp4"), 300.0, 180.0) == 180.0

    def test_scan_failure_falls_back_to_target(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert choose_teaser_cut_seconds(Path("x.mp4"), 300.0, 180.0) == 180.0


class TestBuildProbeFnUnit:
    """Tests for the assembled probe callable returned by build_probe_fn."""

    def _settings(self):
        return None  # settings unused in current impl; future-proof placeholder

    def _run_probe(self, source_probe_ok, source_dur, ffmpeg_rc, teaser_dur_str):
        """Helper: mock source ffprobe → ffmpeg → teaser ffprobe, run probe_fn."""
        settings = self._settings()
        probe_fn = build_probe_fn(settings)

        source_json = _ffprobe_json(str(source_dur)) if source_probe_ok else ""
        source_ffprobe = _make_completed(
            stdout=source_json, returncode=0 if source_probe_ok else 1
        )
        # 软上限切点（2026-06-12）：源 >195s 会先跑一次 silencedetect；
        # 空 stderr = 窗口内无静音 → 回落 180s 硬剪（legacy 行为）。
        silence_scan = _make_completed(stderr="", returncode=0)
        ffmpeg_proc = _make_completed(returncode=ffmpeg_rc)
        teaser_json = _ffprobe_json(teaser_dur_str)
        teaser_ffprobe = _make_completed(stdout=teaser_json, returncode=0)

        import uuid
        source_hash = uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "video.mp4"
            src.touch()
            with patch("subprocess.run", side_effect=[source_ffprobe, silence_scan, ffmpeg_proc, teaser_ffprobe]):
                return probe_fn(src, source_hash), source_hash

    def test_source_hash_is_echoed_back(self):
        result, source_hash = self._run_probe(True, 300.0, 0, "179.5")
        assert result.source_hash == source_hash

    def test_source_probe_fail_returns_failure_reason(self):
        settings = self._settings()
        probe_fn = build_probe_fn(settings)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "video.mp4"
            src.touch()
            with patch("subprocess.run", return_value=_make_completed(returncode=1)):
                result = probe_fn(src, "deadbeef")
        assert result.failure_reason is not None

    def test_cut_fail_returns_failure_reason(self):
        settings = self._settings()
        probe_fn = build_probe_fn(settings)
        src_ok = _make_completed(stdout=_ffprobe_json("300.0"), returncode=0)
        silence_scan = _make_completed(stderr="", returncode=0)
        ffmpeg_fail = _make_completed(returncode=1)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "video.mp4"
            src.touch()
            with patch("subprocess.run", side_effect=[src_ok, silence_scan, ffmpeg_fail]):
                result = probe_fn(src, "deadbeef")
        assert result.failure_reason is not None

    # ---- Duration boundary tests ----

    def _probe_with_teaser_duration(self, teaser_dur_s: float):
        """Run probe_fn with a teaser that ffprobe reports as teaser_dur_s."""
        settings = self._settings()
        probe_fn = build_probe_fn(settings)
        src_ok = _make_completed(stdout=_ffprobe_json("300.0"), returncode=0)
        silence_scan = _make_completed(stderr="", returncode=0)
        ffmpeg_ok = _make_completed(returncode=0)
        teaser_ok = _make_completed(stdout=_ffprobe_json(str(teaser_dur_s)), returncode=0)
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "video.mp4"
            src.touch()
            with patch("subprocess.run", side_effect=[src_ok, silence_scan, ffmpeg_ok, teaser_ok]):
                return probe_fn(src, "aabbccdd")

    def test_180_014s_encode_overshoot_passes(self):
        """ffmpeg 重编码切割实测越界（2026-06-12 现网）：180.014s 通过软上限
        gate 且时长如实上报——严格 >180s 比较曾把所有 >3min 源拒成 probe
        failure。admission 层自身 min(duration, max) 钳制，不需要这里撒谎。"""
        result = self._probe_with_teaser_duration(180.014)
        assert result.failure_reason is None
        assert abs(result.duration_seconds - 180.014) < 1e-6

    def test_grace_window_195s_passes(self):
        """软上限（产品裁定 2026-06-12）：target 180 + grace 15 内的静音
        对齐切点合法——195.0s ≤ 195.5s 硬拒线 → 通过。"""
        result = self._probe_with_teaser_duration(195.0)
        assert result.failure_reason is None

    def test_beyond_hard_cap_196s_fails(self):
        """硬拒线 = 180 + 15 + 0.5 = 195.5s：196s 不是冗余而是切割故障。"""
        result = self._probe_with_teaser_duration(196.0)
        assert result.failure_reason is not None

    def test_179s_teaser_passes_cap_gate(self):
        """179 s → 179000 ms / 60000 = 2.983... ≤ 3.0 → None → pass."""
        result = self._probe_with_teaser_duration(179.0)
        assert result.failure_reason is None
        assert abs(result.duration_seconds - 179.0) < 1e-6

    def test_180s_teaser_passes_cap_gate(self):
        """180 s → 180000 ms / 60000 = 3.0 — gate uses strict >, so 3.0 is NOT > 3.0 → pass."""
        result = self._probe_with_teaser_duration(180.0)
        assert result.failure_reason is None
        assert abs(result.duration_seconds - 180.0) < 1e-6

    def test_180_04s_teaser_clamped_passes(self):
        """180.04 s → 软上限窗口内 → 通过，时长如实上报 180.04。

        本测试原断言"180.04s FAILS"并注释"实践中 ffmpeg 会切出略低于
        180s，此边界不太可能发生"——2026-06-12 现网恰好证伪（实测
        180.014s，把所有 >3min 源拒成 probe failure）。现在钉死钳制行为。
        """
        result = self._probe_with_teaser_duration(180.04)
        assert result.failure_reason is None
        assert abs(result.duration_seconds - 180.04) < 1e-6

    def test_181s_within_grace_passes(self):
        """181 s → 软上限冗余窗口内（≤195.5s）→ 通过（2026-06-12 起）。"""
        result = self._probe_with_teaser_duration(181.0)
        assert result.failure_reason is None

    def test_happy_path_returns_probe_result_with_correct_fields(self):
        result, source_hash = self._run_probe(True, 300.0, 0, "179.5")
        assert result.failure_reason is None
        assert result.duration_seconds == pytest.approx(179.5, abs=1e-6)
        assert result.source_duration_seconds == pytest.approx(300.0, abs=1e-6)
        assert result.source_hash == source_hash
        assert result.audio_present is True
        # teaser_candidate_range should be (0, duration)
        assert result.teaser_candidate_range[0] == 0.0
        assert result.teaser_candidate_range[1] == pytest.approx(179.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Failure reason redaction — assert no sensitive strings leak
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    "ffmpeg",
    "ffprobe",
    "-i",
    "\\",  # Windows path separator
    "/",   # POSIX path separator (also appears in paths)
    "pipe:",
    "stderr",
    "subprocess",
    "CalledProcess",
    "TimeoutExpired",
]

# We only check path separators in the context of a longer path-like string.
# Simple "/" in words like "fail closed" is fine, so we use more specific patterns:
_STRICT_SENSITIVE_PATTERNS = [
    "ffmpeg",
    "ffprobe",
    " -i ",
    "pipe:0",
    "pipe:1",
    "CalledProcess",
    "TimeoutExpired",
    "subprocess.run",
]


class TestFailureReasonRedaction:
    """Ensure no failure reason leaks sensitive tool/path information."""

    def _collect_all_failure_reasons(self) -> list[str]:
        """Run various failure scenarios and collect all failure_reason strings."""
        reasons = []

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "video.mp4"
            src.touch()

            # probe_source failures
            with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe not found")):
                r = probe_source(src)
                if r["failure_reason"]:
                    reasons.append(r["failure_reason"])

            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
                r = probe_source(src)
                if r["failure_reason"]:
                    reasons.append(r["failure_reason"])

            with patch("subprocess.run", return_value=_make_completed(returncode=1, stderr="error")):
                r = probe_source(src)
                if r["failure_reason"]:
                    reasons.append(r["failure_reason"])

            # cut_teaser failures
            with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")):
                t = cut_teaser(src, Path(td) / "t.mp4")
                if t.failure_reason:
                    reasons.append(t.failure_reason)

            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 600)):
                t = cut_teaser(src, Path(td) / "t2.mp4")
                if t.failure_reason:
                    reasons.append(t.failure_reason)

            # build_probe_fn cap failure（软上限后硬拒线 195.5s → 用 196.0）
            probe_fn = build_probe_fn(None)
            src_ok = _make_completed(stdout=_ffprobe_json("300.0"), returncode=0)
            silence_scan = _make_completed(stderr="", returncode=0)
            ffmpeg_ok = _make_completed(returncode=0)
            teaser_over = _make_completed(stdout=_ffprobe_json("196.0"), returncode=0)
            with patch("subprocess.run", side_effect=[src_ok, silence_scan, ffmpeg_ok, teaser_over]):
                pr = probe_fn(src, "abc123")
                if pr.failure_reason:
                    reasons.append(pr.failure_reason)

        return reasons

    def test_no_sensitive_pattern_in_failure_reasons(self):
        reasons = self._collect_all_failure_reasons()
        assert reasons, "Expected at least some failure reasons to test"
        for reason in reasons:
            for pattern in _STRICT_SENSITIVE_PATTERNS:
                assert pattern not in reason, (
                    f"Sensitive pattern {pattern!r} found in failure_reason: {reason!r}"
                )


# ---------------------------------------------------------------------------
# Integration tests — require real ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH — skipping integration tests",
)
class TestCutTeaserIntegration:
    """Integration tests using ffmpeg lavfi to generate a real 5s test video."""

    @pytest.fixture()
    def five_second_video(self, tmp_path) -> Path:
        """Generate a 5s test video using ffmpeg lavfi (no network, no real media)."""
        out = tmp_path / "test_input.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=160x90:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            pytest.skip("ffmpeg lavfi failed to generate test video")
        return out

    def test_cut_teaser_produces_file_with_correct_duration(self, tmp_path, five_second_video):
        dest = tmp_path / "teaser_out.mp4"
        result = cut_teaser(five_second_video, dest, max_seconds=180.0)

        assert result.failure_reason is None, f"Unexpected failure: {result.failure_reason}"
        assert dest.exists(), "Teaser file was not created"
        assert result.duration_seconds is not None
        # Source is 5s; teaser at max 180s should be ≈ 5s (within 1s tolerance)
        assert abs(result.duration_seconds - 5.0) < 1.0, (
            f"Expected teaser ≈ 5s, got {result.duration_seconds}s"
        )

    def test_build_probe_fn_integration(self, tmp_path, five_second_video):
        probe_fn = build_probe_fn(None)
        result = probe_fn(five_second_video, "integration_test_hash")

        assert result.failure_reason is None, f"Unexpected failure: {result.failure_reason}"
        assert result.source_hash == "integration_test_hash"
        assert abs(result.duration_seconds - 5.0) < 1.0
        assert result.audio_present is True

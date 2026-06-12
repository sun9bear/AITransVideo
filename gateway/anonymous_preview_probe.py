"""APF P0 — T4: teaser re-encode cut + ffprobe + probe fn adapter.

Produces a ``ProbeResult``-compatible dict (and the real ``ProbeResult``
dataclass) for the anonymous preview intake pipeline.

Design decisions (AD-1):
* ffmpeg **re-encodes** (``-c:v libx264 -preset veryfast -c:a aac``), never
  ``-c copy``, to avoid keyframe overshoot that pushes the actual duration
  above 180 s and trips the ``>`` gate in ``evaluate_free_duration_cap``.
* ``probe_source`` and ``cut_teaser`` are pure subprocess wrappers — no
  imports from ``services.*`` or ``gateway.*`` to avoid pydub / FastAPI
  contamination (AD-3 import namespace constraint).
* Failure reasons are always **fixed redacted strings** — no ffmpeg/ffprobe
  stderr, no filesystem paths, no command-line fragments.

Public API consumed by T7 adapter wiring:
    build_probe_fn(settings) -> Callable[[Path, str], ProbeResult]
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redacted failure reason constants — NO sensitive text may appear here.
# ---------------------------------------------------------------------------

_REASON_FFPROBE_UNAVAILABLE = "media probe unavailable (tool missing)"
_REASON_FFPROBE_TIMEOUT = "media probe timed out (fail closed)"
_REASON_FFPROBE_FAILED = "media probe failed (fail closed)"
_REASON_FFPROBE_PARSE = "media probe output unparseable (fail closed)"
_REASON_DURATION_UNTRUSTWORTHY = "media probe duration untrustworthy (fail closed)"
_REASON_FFMPEG_UNAVAILABLE = "teaser cut unavailable (tool missing)"
_REASON_FFMPEG_TIMEOUT = "teaser cut timed out (fail closed)"
_REASON_FFMPEG_FAILED = "teaser cut failed (fail closed)"
_REASON_TEASER_PROBE_FAILED = "teaser probe failed after cut (fail closed)"
_REASON_DURATION_CAP = "teaser duration exceeds cap (fail closed)"

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

FFPROBE_TIMEOUT_SECONDS: float = 30.0
FFMPEG_TIMEOUT_SECONDS: float = 600.0  # 10 min

# ---------------------------------------------------------------------------
# TeaserResult
# ---------------------------------------------------------------------------


@dataclass
class TeaserResult:
    """Output of ``cut_teaser``.

    Attributes
    ----------
    dest_path:
        Absolute path to the re-encoded teaser file.
    duration_seconds:
        ffprobe-measured duration of the teaser (may differ slightly from
        ``max_seconds`` due to re-encode; used as the canonical duration).
    has_audio:
        Whether the teaser has at least one audio stream.
    container_format:
        Container format reported by ffprobe (e.g. ``"mov,mp4,m4a...``).
    failure_reason:
        Non-None when the cut or post-probe failed.  Always a fixed
        redacted string — never contains tool stderr, paths, or CLI args.
    """

    dest_path: Path
    duration_seconds: Optional[float]
    has_audio: bool
    container_format: Optional[str]
    failure_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# probe_source — run ffprobe on a file path, return structured dict
# ---------------------------------------------------------------------------


def probe_source(path: Path) -> Dict[str, Any]:
    """Run ffprobe on *path* and return a structured result dict.

    Returns a dict with keys:
        ``ok`` (bool), ``duration_seconds`` (float | None),
        ``has_audio`` (bool), ``container_format`` (str | None),
        ``failure_reason`` (str | None).

    Never raises — all subprocess / parse failures are captured and
    returned as ``ok=False`` with a redacted ``failure_reason``.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,format_name:stream=codec_type",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("[probe_source] ffprobe not found on PATH")
        return {
            "ok": False,
            "duration_seconds": None,
            "has_audio": False,
            "container_format": None,
            "failure_reason": _REASON_FFPROBE_UNAVAILABLE,
        }
    except subprocess.TimeoutExpired:
        logger.warning("[probe_source] ffprobe timed out")
        return {
            "ok": False,
            "duration_seconds": None,
            "has_audio": False,
            "container_format": None,
            "failure_reason": _REASON_FFPROBE_TIMEOUT,
        }

    if proc.returncode != 0:
        logger.warning("[probe_source] ffprobe non-zero exit rc=%d", proc.returncode)
        return {
            "ok": False,
            "duration_seconds": None,
            "has_audio": False,
            "container_format": None,
            "failure_reason": _REASON_FFPROBE_FAILED,
        }

    # Parse JSON output
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[probe_source] ffprobe output not valid JSON")
        return {
            "ok": False,
            "duration_seconds": None,
            "has_audio": False,
            "container_format": None,
            "failure_reason": _REASON_FFPROBE_PARSE,
        }

    # Extract duration
    try:
        raw_duration = data.get("format", {}).get("duration")
        duration_seconds = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    # Validate duration is a trustworthy positive finite number
    if duration_seconds is None or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        logger.warning("[probe_source] duration untrustworthy: %r", duration_seconds)
        return {
            "ok": False,
            "duration_seconds": duration_seconds,
            "has_audio": False,
            "container_format": None,
            "failure_reason": _REASON_DURATION_UNTRUSTWORTHY,
        }

    # Extract audio presence
    streams = data.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    # Container format
    container_format = data.get("format", {}).get("format_name")

    return {
        "ok": True,
        "duration_seconds": duration_seconds,
        "has_audio": has_audio,
        "container_format": container_format,
        "failure_reason": None,
    }


# ---------------------------------------------------------------------------
# cut_teaser — ffmpeg re-encode cut
# ---------------------------------------------------------------------------


def teaser_dest_for(source_path: Path) -> Path:
    """teaser 落盘路径的唯一真源。

    build_probe_fn 切割时与 T8b ``/create`` 回找文件时必须走同一条规则——
    teaser 路径不持久化在契约 record 上（status-only），create 端点靠
    本函数 + audit 里的 stored_upload_path 复原。
    """
    return source_path.parent / f"teaser_{source_path.stem}.mp4"


def cut_teaser(
    source_path: Path,
    dest_path: Path,
    *,
    max_seconds: float = 180.0,
) -> TeaserResult:
    """Re-encode first ``max_seconds`` of *source_path* into *dest_path*.

    Uses ``-c:v libx264 -preset veryfast -c:a aac`` (AD-1: not ``-c copy``)
    to avoid keyframe overshoot that could push the actual duration past the
    cap threshold.

    After cutting, ffprobe is run on the teaser to obtain the *actual*
    duration (re-encode may result in a duration slightly below ``max_seconds``).

    All failures are returned as a ``TeaserResult`` with ``failure_reason``
    set to a fixed redacted string.  This function never raises.
    """
    # Build ffmpeg command — re-encode, not stream copy
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source_path),
        "-t", str(max_seconds),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-c:a", "aac",
        str(dest_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("[cut_teaser] ffmpeg not found on PATH")
        return TeaserResult(
            dest_path=dest_path,
            duration_seconds=None,
            has_audio=False,
            container_format=None,
            failure_reason=_REASON_FFMPEG_UNAVAILABLE,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[cut_teaser] ffmpeg timed out")
        return TeaserResult(
            dest_path=dest_path,
            duration_seconds=None,
            has_audio=False,
            container_format=None,
            failure_reason=_REASON_FFMPEG_TIMEOUT,
        )

    if proc.returncode != 0:
        logger.warning("[cut_teaser] ffmpeg non-zero exit rc=%d", proc.returncode)
        return TeaserResult(
            dest_path=dest_path,
            duration_seconds=None,
            has_audio=False,
            container_format=None,
            failure_reason=_REASON_FFMPEG_FAILED,
        )

    # ffprobe the teaser to get actual duration after re-encode
    probe = probe_source(dest_path)
    if not probe["ok"]:
        logger.warning("[cut_teaser] post-cut ffprobe failed: %s", probe["failure_reason"])
        return TeaserResult(
            dest_path=dest_path,
            duration_seconds=None,
            has_audio=False,
            container_format=None,
            failure_reason=_REASON_TEASER_PROBE_FAILED,
        )

    return TeaserResult(
        dest_path=dest_path,
        duration_seconds=probe["duration_seconds"],
        has_audio=probe["has_audio"],
        container_format=probe["container_format"],
        failure_reason=None,
    )


# ---------------------------------------------------------------------------
# 软上限切点选择（产品裁定 2026-06-12：预览尾部保段落完整，180s 上下可冗余）
# ---------------------------------------------------------------------------

# teaser 目标 180s 不再是硬剪线：在 ±grace 窗口内优先选自然停顿
# （silencedetect）收尾，保住最后一句话/段落的完整性；"多出十几秒可以
# 接受"（项目主 2026-06-12）。硬拒线 = target + grace + 编码噪声容差。
_TEASER_GRACE_SECONDS = 15.0

# ffmpeg -t 重编码切割会因帧边界/音频 priming 轻微越界切点（2026-06-12 现网
# 实测 180.014s——严格 >180s 比较曾把所有 >3min 源一刀切成 FAILED "probe
# failure"；短视频 teaser=源时长 ≤180s 故从未暴露）。本容差叠加在硬拒线上，
# 给编码噪声留余量；真实时长如实上报（admission 层自身 min(duration, max)
# 钳制预览时长，不会二次误判）。
_TEASER_OVERSHOOT_TOLERANCE_S = 0.5


def _detect_silence_starts(
    source_path: Path, window_lo: float, window_hi: float
) -> list[float]:
    """ffmpeg silencedetect 扫源视频 [window_lo, window_hi]，返回绝对时间的
    silence_start 升序列表。任何失败 → []（调用方回落 target 硬剪——静音
    对齐是优化项，不是 gate，绝不因它 fail）。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-ss", str(window_lo),
        "-i", str(source_path),
        "-t", str(max(0.0, window_hi - window_lo)),
        "-vn",
        "-af", "silencedetect=noise=-35dB:d=0.4",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[silence_scan] silencedetect failed", exc_info=True)
        return []
    points: list[float] = []
    # silencedetect 输出在 stderr；-ss 在 -i 之前 → 时间戳相对 window_lo。
    for m in re.finditer(r"silence_start:\s*([0-9.]+)", proc.stderr or ""):
        try:
            points.append(window_lo + float(m.group(1)))
        except ValueError:
            continue
    return sorted(points)


def choose_teaser_cut_seconds(
    source_path: Path,
    source_duration_s: Optional[float],
    target_s: float,
) -> float:
    """选 teaser 切点（软上限语义）：

    * 源时长未知（probe 异常/测试桩）→ target 硬剪（legacy 行为）。
    * 源 ≤ target+grace → 返回 target+grace（-t 超过源时长 = 整段保留，
      不在 target 处把 183s 的视频腰斩）。
    * 否则取 ±grace 窗口内最靠近 target 的静音起点（= 上一句刚说完）；
      窗口内无停顿（连续讲话/音乐）→ target 硬剪。
    """
    hi = target_s + _TEASER_GRACE_SECONDS
    try:
        src_dur = float(source_duration_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return target_s
    if not math.isfinite(src_dur) or src_dur <= 0:
        return target_s
    if src_dur <= hi:
        return hi
    lo = max(0.0, target_s - _TEASER_GRACE_SECONDS)
    candidates = [
        p for p in _detect_silence_starts(source_path, lo, hi) if lo <= p <= hi
    ]
    if not candidates:
        return target_s
    best = min(candidates, key=lambda p: abs(p - target_s))
    logger.info(
        "[choose_teaser_cut] silence-aligned cut at %.2fs (target %.1fs)",
        best,
        target_s,
    )
    return best


# ---------------------------------------------------------------------------
# build_probe_fn — assemble the adapter-compatible probe callable
# ---------------------------------------------------------------------------


def build_probe_fn(
    settings: Any,
    *,
    max_teaser_seconds: float = 180.0,
) -> Callable[[Path, str], Any]:
    """Return a probe callable compatible with the T3 adapter stub interface.

    Signature of the returned callable::

        probe_fn(source_path: Path, source_hash: str) -> ProbeResult

    The returned callable:
    1. ffprobe the source (source-level duration guard is IntakeConfig's job;
       we only validate the duration is trustworthy here).
    2. Cut the teaser (re-encode, max_teaser_seconds=180).
    3. ffprobe the teaser — canonical duration.
    4. Call ``evaluate_free_duration_cap(teaser_ms, max_minutes=3)`` — non-None
       → failure (teaser over cap).
    5. Return ``ProbeResult(duration_seconds=teaser_dur, source_hash=<passthrough>,
       media_type=container_format, audio_present=...,
       audio_quality_score=0.0,
       teaser_candidate_range=(0.0, teaser_dur))``.

    ``source_hash`` is passed in externally (from the upload layer) and is
    echoed back unchanged on ``ProbeResult.source_hash`` — per AD-1 the
    probe layer does not re-hash the source file.

    On any failure the returned ``ProbeResult`` has ``failure_reason`` set
    to a fixed redacted string.
    """
    # Import here to keep module-level imports stdlib-only; these are
    # lightweight pure modules with no pydub/FastAPI deps.
    # We do a lazy import so unit tests can mock subprocess without importing
    # the full src tree.
    from services.anonymous_preview_intake import ProbeResult  # noqa: PLC0415
    from src.utils.free_duration_gate import evaluate_free_duration_cap  # noqa: PLC0415

    def _probe(source_path: Path, source_hash: str) -> ProbeResult:  # type: ignore[return]
        # Step 1 — probe source (validate duration is trustworthy)
        src_probe = probe_source(source_path)
        if not src_probe["ok"]:
            return ProbeResult(
                duration_seconds=0.0,
                source_hash=source_hash,
                media_type="unknown",
                audio_present=False,
                audio_quality_score=0.0,
                teaser_candidate_range=(0.0, 0.0),
                failure_reason=src_probe["failure_reason"],
            )

        # Step 2 — cut teaser（软上限：静音对齐切点，产品裁定 2026-06-12）
        cut_at = choose_teaser_cut_seconds(
            source_path,
            src_probe.get("duration_seconds"),
            float(max_teaser_seconds),
        )
        dest_path = teaser_dest_for(source_path)
        teaser = cut_teaser(source_path, dest_path, max_seconds=cut_at)
        if teaser.failure_reason is not None:
            return ProbeResult(
                duration_seconds=0.0,
                source_hash=source_hash,
                media_type="unknown",
                audio_present=False,
                audio_quality_score=0.0,
                teaser_candidate_range=(0.0, 0.0),
                failure_reason=teaser.failure_reason,
            )

        teaser_dur = teaser.duration_seconds  # already validated by cut_teaser

        # Step 3 — 软上限 gate：target + grace + 编码噪声容差。真实时长如实
        # 上报（admission 层自身 min(duration, max) 钳制预览时长，超 180 的
        # 冗余部分不会被二次误判）。
        teaser_ms = teaser_dur * 1000.0  # type: ignore[operator]
        hard_cap_minutes = (
            float(max_teaser_seconds)
            + _TEASER_GRACE_SECONDS
            + _TEASER_OVERSHOOT_TOLERANCE_S
        ) / 60.0
        cap_reason = evaluate_free_duration_cap(teaser_ms, max_minutes=hard_cap_minutes)
        if cap_reason is not None:
            logger.warning(
                "[build_probe_fn] teaser duration %.3fs failed cap gate: %s",
                teaser_dur,
                cap_reason,
            )
            return ProbeResult(
                duration_seconds=teaser_dur,
                source_hash=source_hash,
                media_type=teaser.container_format or "unknown",
                audio_present=teaser.has_audio,
                audio_quality_score=0.0,
                teaser_candidate_range=(0.0, teaser_dur),
                failure_reason=_REASON_DURATION_CAP,
            )

        return ProbeResult(
            duration_seconds=teaser_dur,
            source_hash=source_hash,
            media_type=teaser.container_format or "unknown",
            audio_present=teaser.has_audio,
            audio_quality_score=1.0,
            teaser_candidate_range=(0.0, teaser_dur),
            failure_reason=None,
        )

    return _probe


def build_intake_probe_fn(
    settings: Any,
    *,
    max_teaser_seconds: float = 180.0,
) -> Callable[[Any], Any]:
    """Return the adapter-contract probe callable used by the router.

    ``AnonymousPreviewBackendAdapter._safe_probe`` invokes the injected
    ``probe_fn`` as ``probe_fn(upload)`` — a SINGLE ``UploadFacts`` arg.
    ``build_probe_fn`` returns a TWO-arg ``_probe(source_path, source_hash)``
    callable, so passing it to the adapter raw makes every upload raise
    ``TypeError`` → ``_safe_probe`` fail-closes → a FAILED record (after the
    rate-limit slot is already burned). This wrapper bridges the arity:
    it unpacks ``stored_path`` / ``source_hash`` off the ``UploadFacts`` so
    the router can wire ONE named callable and the seam is unit-testable.

    Regression guard: ``tests/test_anonymous_preview_t4_probe_wiring.py``.
    """
    raw = build_probe_fn(settings, max_teaser_seconds=max_teaser_seconds)

    def _intake_probe_fn(upload: Any) -> Any:
        # Duck-typed against UploadFacts; kept loose so this module avoids
        # importing the src dataclasses at module load (stdlib-only header).
        return raw(upload.stored_path, upload.source_hash)

    return _intake_probe_fn


__all__ = [
    "TeaserResult",
    "probe_source",
    "cut_teaser",
    "build_probe_fn",
    "build_intake_probe_fn",
    "FFPROBE_TIMEOUT_SECONDS",
    "FFMPEG_TIMEOUT_SECONDS",
]

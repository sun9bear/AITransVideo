"""CosyVoice clone 样本硬性校验（Phase 4.1 B）。

plan §样本硬性校验规则 5 维：

| 维度 | 规则 |
|---|---|
| 格式 | WAV (PCM 16-bit) / MP3 / M4A 之一 |
| 时长 | 推荐 10~20 秒；最长 60 秒 |
| 大小 | ≤ 10 MB |
| 采样率 | ≥ 16 kHz |
| 内容质量 | 至少 5 秒连续清晰朗读；不允许背景音乐 / 显著噪音 / 多人声 / 歌曲（主观，不强校验） |

实施策略：

1. 廉价校验先做（size / magic bytes，微秒级）
2. ffprobe subprocess 探取真实 metadata（duration / sample_rate / channels）
   —— ffmpeg / ffprobe 已经在 gateway docker image 里
3. 内容质量只产 hint，不阻断（plan §样本硬性校验规则 "主观，不强校验"）

返回 ``SampleValidationResult``：``is_valid`` + ``error_code`` + metadata +
hints。任一硬规则失败 → ``is_valid=False`` + ``error_code``，Gateway clone
endpoint 拒 clone 请求、不调 worker、不扣费。

测试约束（AGENTS.md）：测试用 monkeypatch mock ``subprocess.run`` 模拟
ffprobe 输出，**不依赖** 真实 ffprobe 可执行文件，**永不真实联网**。
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---- 硬性上下限（plan §样本硬性校验规则 + Codex 2026-05-25 决策对齐 DashScope）----
#
# 决策口径：DashScope ``max_prompt_audio_length`` 官方默认 10s 上限，
# Phase 4.1 显式传 30s 让服务端取最多 30s 参考音频。Gateway 接受范围：
#
#   [3s, 60s]
#   - < 3s     硬拒（克隆相似度低到不可用）
#   - 3-10s    允许通过，附 warning hint（提示相似度可能下降）
#   - 10-20s   推荐区间
#   - 20-60s   允许，由 audio_processor 裁剪到 30s 送给 DashScope
#   - > 60s    硬拒

MAX_SAMPLE_BYTES = 10 * 1024 * 1024  # 10 MB
MIN_SAMPLE_BYTES = 1024              # 1 KB（避免空文件 / 无声 stub）
MAX_DURATION_MS = 60_000             # 60 秒
MIN_DURATION_MS = 3_000              # 3 秒（Codex 2026-05-25：太短克隆效果差）
MIN_SAMPLE_RATE_HZ = 16_000          # 16 kHz

# 推荐范围（仅产 hint）
RECOMMENDED_MIN_DURATION_MS = 10_000   # 10 秒
RECOMMENDED_MAX_DURATION_MS = 20_000   # 20 秒

# Magic bytes 表：检测前 16 字节是否匹配
# - WAV: 'RIFF' 在 offset 0；'WAVE' 在 offset 8
# - MP3: 帧同步 0xFFE0 / 0xFFF0（首字节 0xFF + 第二字节高位）或 'ID3' tag
# - M4A: 'ftyp' 在 offset 4（ISO BMFF）
_FORMAT_WAV = "wav"
_FORMAT_MP3 = "mp3"
_FORMAT_M4A = "m4a"

# WAV 必须是 PCM 16-bit（plan §样本硬性校验规则 明确）。WAV 容器允许
# 24-bit / 32-bit float / ADPCM 等编码，但 DashScope clone API 文档建议
# 16-bit PCM；非 PCM-16 在送到 DashScope 之前就拒掉，避免付费 API 浪费。
# Codex 2026-05-25 三轮 P1 finding。
_WAV_PCM16_CODECS = frozenset({"pcm_s16le", "pcm_s16be"})


# ---- 错误码 ----

class ErrorCode:
    EMPTY = "sample_empty"
    SIZE_TOO_LARGE = "sample_size_too_large"
    SIZE_TOO_SMALL = "sample_size_too_small"
    FORMAT_UNSUPPORTED = "sample_format_unsupported"
    DECODE_FAILED = "sample_decode_failed"
    DURATION_TOO_LONG = "sample_duration_too_long"
    DURATION_TOO_SHORT = "sample_duration_too_short"
    SAMPLE_RATE_TOO_LOW = "sample_sample_rate_too_low"
    WAV_ENCODING_UNSUPPORTED = "sample_wav_encoding_unsupported"  # 4.1 B 二轮修
    PROBE_TIMEOUT = "sample_probe_timeout"


# ---- 数据合约 ----

@dataclass(frozen=True, slots=True)
class SampleValidationResult:
    """样本校验结果。

    Attributes
    ----------
    is_valid:
        True 表示所有硬规则通过；False 则 ``error_code`` 非 None。
    error_code:
        失败时的稳定错误码（``ErrorCode.*`` 常量之一），供前端 i18n /
        Gateway audit 用。
    error_message:
        人可读错误信息，含具体数值（如"60123 ms > 60000 ms"）。
    detected_format:
        ``"wav"`` / ``"mp3"`` / ``"m4a"`` / None（格式校验失败时）。
    duration_ms / sample_rate_hz / channels:
        ffprobe 探测到的 metadata，仅在 is_valid=True 或部分校验通过时填。
    codec_name:
        ffprobe 探测到的 audio codec（``"pcm_s16le"`` / ``"mp3"`` /
        ``"aac"`` 等）。WAV 容器的 PCM 16-bit 校验依赖此字段。
    bits_per_sample:
        ffprobe 探测到的 bit depth（PCM 类 codec 才有意义）。
    size_bytes:
        始终填，是上传 body 的长度。
    hints:
        非阻断性提示（如"建议时长 10-20 秒"），供前端展示软提示。
    """
    is_valid: bool
    error_code: str | None = None
    error_message: str = ""
    detected_format: str | None = None
    duration_ms: int | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    codec_name: str | None = None
    bits_per_sample: int | None = None
    size_bytes: int = 0
    hints: tuple[str, ...] = field(default_factory=tuple)


# ---- 主入口 ----

def validate_sample_bytes(
    data: bytes,
    *,
    ffprobe_path: str = "ffprobe",
    ffprobe_timeout_s: float = 10.0,
) -> SampleValidationResult:
    """Run all 5 hard checks against an audio sample.

    Parameters
    ----------
    data:
        Raw audio file bytes (uploaded sample).
    ffprobe_path:
        ``ffprobe`` 可执行文件路径（默认 PATH 查找）。测试可注入 sentinel。
    ffprobe_timeout_s:
        ffprobe subprocess 超时阈值，防止 ffprobe 卡死。

    Returns
    -------
    SampleValidationResult
    """
    size_bytes = len(data)

    # 1. size — 廉价
    if size_bytes == 0:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.EMPTY,
            error_message="sample is empty (0 bytes)",
            size_bytes=0,
        )
    if size_bytes < MIN_SAMPLE_BYTES:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.SIZE_TOO_SMALL,
            error_message=f"{size_bytes} bytes < min {MIN_SAMPLE_BYTES}",
            size_bytes=size_bytes,
        )
    if size_bytes > MAX_SAMPLE_BYTES:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.SIZE_TOO_LARGE,
            error_message=f"{size_bytes} bytes > max {MAX_SAMPLE_BYTES} (10 MB)",
            size_bytes=size_bytes,
        )

    # 2. magic bytes —— 廉价
    detected_format = _detect_format(data)
    if detected_format is None:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.FORMAT_UNSUPPORTED,
            error_message=f"format not in {{wav, mp3, m4a}}; magic bytes head={data[:16]!r}",
            size_bytes=size_bytes,
        )

    # 3. ffprobe metadata
    try:
        probe = _probe_with_ffprobe(data, ffprobe_path, ffprobe_timeout_s)
    except _ProbeTimeoutError as exc:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.PROBE_TIMEOUT,
            error_message=str(exc),
            detected_format=detected_format,
            size_bytes=size_bytes,
        )
    except _ProbeError as exc:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.DECODE_FAILED,
            error_message=str(exc),
            detected_format=detected_format,
            size_bytes=size_bytes,
        )

    duration_ms = probe.get("duration_ms")
    sample_rate_hz = probe.get("sample_rate_hz")
    channels = probe.get("channels")
    codec_name = probe.get("codec_name")
    bits_per_sample = probe.get("bits_per_sample")

    # 4. WAV PCM 16-bit 编码校验（Codex 2026-05-25 三轮 P1 finding）：
    # plan §样本硬性校验规则 写"WAV (PCM 16-bit)"；24-bit / float / ADPCM
    # 等 WAV 编码 DashScope clone 不支持，必须在 Gateway 拦截，避免送到
    # 付费 API 才失败。
    if detected_format == _FORMAT_WAV:
        if codec_name not in _WAV_PCM16_CODECS:
            return SampleValidationResult(
                is_valid=False,
                error_code=ErrorCode.WAV_ENCODING_UNSUPPORTED,
                error_message=(
                    f"WAV codec={codec_name!r} bits_per_sample={bits_per_sample!r}; "
                    f"required: PCM 16-bit (codec in {sorted(_WAV_PCM16_CODECS)})"
                ),
                detected_format=detected_format,
                duration_ms=duration_ms,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                codec_name=codec_name,
                bits_per_sample=bits_per_sample,
                size_bytes=size_bytes,
            )

    # 5. duration
    if duration_ms is None or duration_ms <= 0:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.DECODE_FAILED,
            error_message=f"ffprobe returned no valid duration: {probe!r}",
            detected_format=detected_format,
            size_bytes=size_bytes,
        )
    if duration_ms > MAX_DURATION_MS:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.DURATION_TOO_LONG,
            error_message=(
                f"{duration_ms} ms > max {MAX_DURATION_MS} ms "
                f"({MAX_DURATION_MS // 1000}s)"
            ),
            detected_format=detected_format,
            duration_ms=duration_ms,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            codec_name=codec_name,
            bits_per_sample=bits_per_sample,
            size_bytes=size_bytes,
        )
    if duration_ms < MIN_DURATION_MS:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.DURATION_TOO_SHORT,
            error_message=(
                f"{duration_ms} ms < min {MIN_DURATION_MS} ms "
                f"({MIN_DURATION_MS // 1000}s)"
            ),
            detected_format=detected_format,
            duration_ms=duration_ms,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            codec_name=codec_name,
            bits_per_sample=bits_per_sample,
            size_bytes=size_bytes,
        )

    # 6. sample rate
    if sample_rate_hz is None or sample_rate_hz < MIN_SAMPLE_RATE_HZ:
        return SampleValidationResult(
            is_valid=False,
            error_code=ErrorCode.SAMPLE_RATE_TOO_LOW,
            error_message=f"{sample_rate_hz} Hz < min {MIN_SAMPLE_RATE_HZ} Hz (16 kHz)",
            detected_format=detected_format,
            duration_ms=duration_ms,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            codec_name=codec_name,
            bits_per_sample=bits_per_sample,
            size_bytes=size_bytes,
        )

    # 7. 非阻断性 hints（内容质量提示）
    hints = _build_quality_hints(duration_ms)

    return SampleValidationResult(
        is_valid=True,
        detected_format=detected_format,
        duration_ms=duration_ms,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        codec_name=codec_name,
        bits_per_sample=bits_per_sample,
        size_bytes=size_bytes,
        hints=hints,
    )


# ---- 内部 helper ----

def _detect_format(data: bytes) -> str | None:
    """Magic bytes 检测；返回 ``"wav"`` / ``"mp3"`` / ``"m4a"`` 或 None。"""
    if len(data) < 12:
        return None

    head = data[:16]

    # WAV: RIFF....WAVE
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _FORMAT_WAV

    # M4A: ....ftyp 在 offset 4
    if head[4:8] == b"ftyp":
        return _FORMAT_M4A

    # MP3: ID3 tag 或 帧同步 0xFFE0/0xFFF0
    if head[:3] == b"ID3":
        return _FORMAT_MP3
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return _FORMAT_MP3

    return None


class _ProbeError(Exception):
    """ffprobe 解码失败（非超时）。"""


class _ProbeTimeoutError(Exception):
    """ffprobe subprocess 超时。"""


def _probe_with_ffprobe(
    data: bytes,
    ffprobe_path: str,
    timeout_s: float,
) -> dict:
    """通过 stdin 喂数据给 ffprobe，提取 audio stream 的 duration / rate / channels。

    ffprobe 命令：

    ::

        ffprobe -v error -print_format json -show_format -show_streams \\
            -read_intervals "%+#5" -i pipe:0

    输出 JSON，第一个 audio stream 的 sample_rate / channels / duration 即可。
    """
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-i", "pipe:0",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ProbeTimeoutError(
            f"ffprobe timeout after {timeout_s}s; sample may be malformed"
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover — 部署环境 always 有 ffprobe
        raise _ProbeError(f"ffprobe not found in PATH ({ffprobe_path!r})") from exc

    if proc.returncode != 0:
        stderr_snippet = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise _ProbeError(
            f"ffprobe failed rc={proc.returncode}: {stderr_snippet}"
        )

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise _ProbeError(f"ffprobe stdout not JSON: {exc}") from exc

    # Find first audio stream
    streams = parsed.get("streams") or []
    audio_stream = next(
        (s for s in streams if s.get("codec_type") == "audio"),
        None,
    )
    if audio_stream is None:
        raise _ProbeError("ffprobe found no audio stream in sample")

    # 解析 sample_rate / channels
    sample_rate_hz: int | None = None
    sr_raw = audio_stream.get("sample_rate")
    if sr_raw is not None:
        try:
            sample_rate_hz = int(sr_raw)
        except (TypeError, ValueError):
            sample_rate_hz = None

    channels: int | None = None
    ch_raw = audio_stream.get("channels")
    if ch_raw is not None:
        try:
            channels = int(ch_raw)
        except (TypeError, ValueError):
            channels = None

    # 解析 codec_name / bits_per_sample（4.1 B 二轮：WAV PCM 16-bit 校验依赖）
    codec_name = audio_stream.get("codec_name")
    if codec_name is not None:
        codec_name = str(codec_name).strip().lower() or None

    bits_per_sample: int | None = None
    for k in ("bits_per_sample", "bits_per_raw_sample"):
        v = audio_stream.get(k)
        if v in (None, 0, "0"):
            continue
        try:
            bits_per_sample = int(v)
            break
        except (TypeError, ValueError):
            continue

    # duration：优先 stream.duration，回落 format.duration
    duration_seconds: float | None = None
    for source in (audio_stream, parsed.get("format") or {}):
        d_raw = source.get("duration")
        if d_raw is None:
            continue
        try:
            duration_seconds = float(d_raw)
            break
        except (TypeError, ValueError):
            continue

    duration_ms: int | None = (
        int(duration_seconds * 1000) if duration_seconds and duration_seconds > 0 else None
    )

    return {
        "duration_ms": duration_ms,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "codec_name": codec_name,
        "bits_per_sample": bits_per_sample,
    }


def _build_quality_hints(duration_ms: int) -> tuple[str, ...]:
    """构造非阻断性 hints（plan §样本硬性校验规则 "内容质量主观提示"）。

    Codex 2026-05-25 决策对齐 DashScope ``max_prompt_audio_length=30s``：
    - 3-10 秒：克隆相似度明显下降，强烈建议补到 10-20 秒
    - 10-20 秒：推荐区间，不附时长 hint
    - 20-60 秒：被 audio_processor 裁剪到 30 秒送给 DashScope
    """
    hints: list[str] = []
    if duration_ms < RECOMMENDED_MIN_DURATION_MS:
        # 3-10 秒：硬性允许通过，但克隆相似度可能下降
        hints.append(
            f"⚠️ 样本时长 {duration_ms / 1000:.1f} 秒偏短，"
            f"少于 {RECOMMENDED_MIN_DURATION_MS // 1000} 秒可能让克隆相似度明显下降；"
            f"建议提供 {RECOMMENDED_MIN_DURATION_MS // 1000}-{RECOMMENDED_MAX_DURATION_MS // 1000} 秒连续清晰朗读以获得最佳效果"
        )
    elif duration_ms > RECOMMENDED_MAX_DURATION_MS:
        hints.append(
            f"样本时长 {duration_ms / 1000:.1f} 秒偏长，"
            f"系统会自动裁剪至最多 30 秒用于克隆"
            f"（推荐区间 {RECOMMENDED_MIN_DURATION_MS // 1000}-{RECOMMENDED_MAX_DURATION_MS // 1000} 秒）"
        )

    hints.append("请确保样本中只有您本人的清晰朗读，避免背景音乐 / 噪音 / 多人声 / 歌曲")
    return tuple(hints)

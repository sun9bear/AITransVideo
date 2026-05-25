"""音频样本转码 / 裁剪到 DashScope clone API 友好格式（Phase 4.1 C 子模块）。

**为什么需要这个模块**（Codex 2026-05-25 三轮 P1 + 四轮决策对齐）：

Gateway 接受用户上传 3-60 秒的 WAV / MP3 / M4A（plan §样本硬性校验
规则）。但 DashScope CosyVoice clone API 要求样本 URL **必须 ≤ 1 MB**
（Phase -1 实测：> 1MB 触发 ``BadRequest.InputDownloadFailed``）；
``max_prompt_audio_length`` 官方默认 10 秒，Phase 4.1 显式传 30 秒
让服务端取更长参考音频以获得更好相似度。

中间差值由本模块的 ``normalize_sample_for_dashscope()`` 处理：

- 裁剪到前 N 秒（默认 30 秒，与 worker 端
  ``RealCosyvoiceProvider.clone(max_prompt_audio_length=30.0)`` 对齐）
- 重采样到 16 kHz（DashScope 最低）
- 转 mono（DashScope clone 不需要双声道）
- 转 PCM 16-bit LE（DashScope CosyVoice 推荐编码）
- WAV 容器（worker 端 ``_validate_sample_size`` HEAD 期望 wav/mp3/m4a）

转码后 size 估算：``16000 Hz × 2 bytes × 30s = 960 KB``，仍低于 1 MB 上限。

**与 sample_validator 关系**：

- ``sample_validator.validate_sample_bytes`` 是 Gateway endpoint 第一道闸：
  对**原始上传**做 5 维校验（≤10 MB、合法格式、ffprobe 探测）
- 验证通过后，``normalize_sample_for_dashscope()`` 是第二道闸：转码
  让 DashScope 可消费
- 两个模块独立单测；endpoint 串联调用顺序：validate → normalize → upload → clone

**测试约束**：subprocess.run 全程 monkeypatch，**永不依赖真实 ffmpeg**
（AGENTS.md 红线）。
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


# DashScope clone 友好输出参数（Codex 2026-05-25 决策对齐
# ``max_prompt_audio_length=30s``）。
DEFAULT_TARGET_DURATION_S = 30
TARGET_SAMPLE_RATE_HZ = 16_000
TARGET_CHANNELS = 1
TARGET_CODEC = "pcm_s16le"
TARGET_CONTAINER = "wav"

# Safety ceiling：worker 端 ``_validate_sample_size`` 用 1 MB 拒。
# 30 秒 16k mono PCM 16-bit ≈ 960 KB，仍 < 1 MB；> 1 MB 直接 fail-closed。
# 因此 sanity ceiling 提到 950 KB（接近 30s 标称值，但 > 980 KB log warning
# 让运维注意是否参数被改坏）。
SAFETY_OUTPUT_CEILING_BYTES = 980 * 1024
WORKER_HARD_LIMIT_BYTES = 1 * 1024 * 1024  # DashScope InputDownloadFailed 阈值


class AudioProcessingError(Exception):
    """ffmpeg 转码失败。"""

    def __init__(self, message: str, *, code: str = "audio_processing_failed"):
        super().__init__(message)
        self.code = code


class AudioProcessingTimeoutError(AudioProcessingError):
    """ffmpeg 子进程超时。"""

    def __init__(self, message: str):
        super().__init__(message, code="audio_processing_timeout")


def normalize_sample_for_dashscope(
    data: bytes,
    *,
    target_duration_s: int = DEFAULT_TARGET_DURATION_S,
    ffmpeg_path: str = "ffmpeg",
    timeout_s: float = 30.0,
) -> bytes:
    """Transcode + 裁剪样本到 DashScope clone 友好格式。

    Parameters
    ----------
    data : bytes
        Validated raw sample bytes (sample_validator 已通过)。
    target_duration_s : int
        裁剪长度（默认 30 秒，与 worker
        ``RealCosyvoiceProvider.clone(max_prompt_audio_length=30.0)`` 对齐）。
        worker 端 ``_validate_sample_size`` 仍按 1 MB 兜底。
    ffmpeg_path : str
        ``ffmpeg`` 可执行文件路径。测试可注入 sentinel。
    timeout_s : float
        subprocess 超时阈值。

    Returns
    -------
    bytes
        16 kHz / mono / PCM 16-bit / WAV 容器的转码后 bytes，30 秒约 960 KB。

    Raises
    ------
    AudioProcessingError
        ffmpeg 非零退出 / 输出为空 / 输出超 ``SAFETY_OUTPUT_CEILING_BYTES``。
    AudioProcessingTimeoutError
        ffmpeg 在 ``timeout_s`` 内未结束。
    ValueError
        ``target_duration_s`` 非正 / ``data`` 空。
    """
    if not data:
        raise ValueError("data must be non-empty bytes")
    if target_duration_s <= 0:
        raise ValueError(f"target_duration_s must be positive, got {target_duration_s}")

    cmd = [
        ffmpeg_path,
        "-v", "error",
        "-i", "pipe:0",
        "-t", str(target_duration_s),
        "-ac", str(TARGET_CHANNELS),
        "-ar", str(TARGET_SAMPLE_RATE_HZ),
        "-acodec", TARGET_CODEC,
        "-f", TARGET_CONTAINER,
        "pipe:1",
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
        raise AudioProcessingTimeoutError(
            f"ffmpeg timeout after {timeout_s}s; sample may be malformed"
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover — 部署环境 always 有 ffmpeg
        raise AudioProcessingError(
            f"ffmpeg not found in PATH ({ffmpeg_path!r})",
            code="ffmpeg_not_found",
        ) from exc

    if proc.returncode != 0:
        stderr_snippet = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise AudioProcessingError(
            f"ffmpeg failed rc={proc.returncode}: {stderr_snippet}"
        )

    output = proc.stdout
    if not output:
        raise AudioProcessingError(
            "ffmpeg produced empty output (no audio decoded?)",
            code="audio_processing_empty",
        )

    # Safety ceiling — 30 秒 16k mono PCM 16-bit 标称 ~960 KB。
    # > 1 MB 直接抛（明知会触发 DashScope InputDownloadFailed）；
    # > 980 KB log warning（接近上限，运维关注是否参数漂移）。
    if len(output) > WORKER_HARD_LIMIT_BYTES:
        raise AudioProcessingError(
            f"ffmpeg output {len(output)} bytes > 1 MB worker hard limit; "
            "transcode parameters drifted (check target_duration_s / sample rate)",
            code="audio_processing_oversized",
        )
    if len(output) > SAFETY_OUTPUT_CEILING_BYTES:
        logger.warning(
            "[audio_processor] transcoded output %d bytes > sanity ceiling %d; "
            "worker side 1MB HEAD will still gate it but parameters may need review",
            len(output),
            SAFETY_OUTPUT_CEILING_BYTES,
        )

    # 简单的 sanity check：输出应该是 WAV
    if output[:4] != b"RIFF" or output[8:12] != b"WAVE":
        raise AudioProcessingError(
            f"ffmpeg output does not look like a WAV file (head={output[:16]!r})",
            code="audio_processing_invalid_output",
        )

    return output

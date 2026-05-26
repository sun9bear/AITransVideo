"""Shared ffmpeg-based audio assembly for voice clone sample preparation.

Phase 4.2 A.2a 抽公共：原 ``voice_selection_api._concat_segments_ffmpeg``
（MiniMax 克隆路径独占）→ 提到本模块，**保持 MiniMax 调用语义字节级不变**
（默认 24kHz / mono / PCM s16le / cache 路径与原函数一致），仅新增可选的
``target_sample_rate_hz`` 参数让 CosyVoice 路径（Phase 4.2 A.2b 落地）传
16000 拿到 CosyVoice 期待的采样率。

为什么单独成模块：

- Phase 4.1 + 4.2 引入两条克隆路径（MiniMax via ``voice_selection_api`` /
  CosyVoice via ``cosyvoice_clone``），都需要"按 segment_ids 从源音频拼一个
  WAV 给克隆 API"的能力。
- 复制 + 修改原函数会让 MiniMax / CosyVoice 路径偷偷漂移（采样率、cache
  目录、subprocess 超时、错误消息、filter 表达式格式等），违反"MiniMax
  字节级不变"硬约束（plan v4-followup §1.2 / §6.1）。
- 单一来源的 helper + 一个 sample rate 参数是最小可行抽象。

调用语义合同（实施者 MUST 看完再改）：

- **MiniMax 路径**：调用时**不传** ``target_sample_rate_hz`` → 默认 24000，
  与原 ``_concat_segments_ffmpeg`` 行为字节级一致。任何 MiniMax callers
  改动必须 P/A 6.1 守卫测试 + AST 扫断言不漂移。
- **CosyVoice 路径**（Phase 4.2 A.2b）：调用时显式传
  ``target_sample_rate_hz=16000``（CosyVoice DashScope API 期待 16kHz）。
  其它参数（mono、PCM s16le、cache 目录布局）保持共享，无需 CosyVoice
  端覆盖。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


# Default sample rate = MiniMax 路径历史值（24kHz）。
# CosyVoice 路径（Phase 4.2 A.2b）调用时显式传 16000。
DEFAULT_TARGET_SAMPLE_RATE_HZ = 24000


# subprocess.run timeout —— 沿用原 ``_concat_segments_ffmpeg`` 写死的 60s。
# 改动需先发 plan diff（影响 long-running 拼接 / 用户感知延迟）。
FFMPEG_SUBPROCESS_TIMEOUT_S = 60


def concat_segments_to_wav(
    source_audio: Path,
    segments: list[dict],
    project_dir: Path,
    speaker_id: str,
    *,
    target_sample_rate_hz: int = DEFAULT_TARGET_SAMPLE_RATE_HZ,
) -> Path:
    """Concat selected segments into a single WAV file.

    Output format（共享，与原 MiniMax 路径一致）：
    - PCM s16le
    - mono
    - sample rate: ``target_sample_rate_hz`` 参数控制（默认 24kHz =
      MiniMax 历史值；CosyVoice 调用方传 16000）

    Output path：``{project_dir}/speaker_audio/{speaker_id}/clone_sample.wav``
    （与原 MiniMax 路径一致，便于排障 + 复用既有 cache 清理逻辑）

    Args:
        source_audio: 源音频文件路径（任务的 speech_for_asr.wav / original.wav）
        segments: 段列表，每项 dict 含 ``start_ms`` / ``end_ms``（整数毫秒）
        project_dir: job 项目根目录，用于派生 cache 目录 + 路径越权检查
        speaker_id: 说话人 ID，用于 cache 子目录命名
        target_sample_rate_hz: ffmpeg ``-ar`` 输出采样率，默认 24000

    Returns:
        拼接结果 WAV 文件的 Path

    Raises:
        ValueError: cache 目录解析后越出 project_dir（防符号链接攻击）
        RuntimeError: ffmpeg subprocess 失败（returncode != 0），消息含
            stderr 前 500 字符
    """
    # Path traversal protection (Codex review iterations):
    # - A.2a: 原 ``startswith(resolve())`` 容易被边界 case 绕过
    # - A.2b 第 1 轮: 改 ``relative_to(speaker_audio_root)`` + 移 check 到 mkdir 前
    # - A.2b 第 2 轮 (本次，Codex PR #11 v2 review)：补 3 条边界
    #   1) speaker_id 含 ``/`` 或 ``\`` 直接拒（speaker_id 不该是路径片段）
    #   2) ``project_dir/speaker_audio`` 自身若是符号链接指外 → 拒（防 symlink 绕开）
    #   3) speaker_id 解析后等于 speaker_audio 根 → 拒（防 per-speaker 隔离失效）
    #
    # 这 3 条都在 mkdir **之前**完成，任何攻击向量都不写盘。

    # 1) speaker_id 不允许含路径分隔符（双平台）
    if "/" in speaker_id or "\\" in speaker_id:
        raise ValueError(
            f"路径验证失败：speaker_id={speaker_id!r} 含路径分隔符 "
            f"(/ 或 \\)，禁止用作目录名"
        )

    speaker_audio_root = project_dir / "speaker_audio"

    # 2) speaker_audio_root 解析后必须仍在 project_dir 之下 —— 防有人把
    # ``project_dir/speaker_audio`` 整体指向外部目录的 symlink 攻击
    try:
        speaker_audio_root.resolve().relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            f"路径验证失败：speaker_audio_root={speaker_audio_root!r} "
            f"解析后越出 project_dir={project_dir!r}（symlink 绕过攻击）"
        ) from exc

    # 3) cache_dir 必须在 speaker_audio_root 严格子树之下
    cache_dir = speaker_audio_root / speaker_id
    try:
        rel = cache_dir.resolve().relative_to(speaker_audio_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"路径验证失败：cache_dir={cache_dir!r} 不在 "
            f"{speaker_audio_root!r} 之下（speaker_id={speaker_id!r}）"
        ) from exc
    if rel == Path("."):
        raise ValueError(
            f"路径验证失败：speaker_id={speaker_id!r} 解析后等于 "
            f"speaker_audio 根目录自身，无法做 per-speaker 隔离"
        )

    # 全部边界过了再 mkdir
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Build ffmpeg filter for segment extraction + concat
    filter_parts = []
    inputs = []
    for i, seg in enumerate(segments):
        start_s = int(seg["start_ms"]) / 1000.0
        end_s = int(seg["end_ms"]) / 1000.0
        filter_parts.append(
            f"[0:a]atrim=start={start_s}:end={end_s},asetpts=PTS-STARTPTS[s{i}]"
        )
        inputs.append(f"[s{i}]")

    concat_filter = ";".join(filter_parts) + ";"
    concat_filter += "".join(inputs) + f"concat=n={len(segments)}:v=0:a=1[out]"

    output_path = cache_dir / "clone_sample.wav"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source_audio),
        "-filter_complex", concat_filter,
        "-map", "[out]",
        "-acodec", "pcm_s16le",
        "-ar", str(target_sample_rate_hz),
        "-ac", "1",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_SUBPROCESS_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg concat failed: {result.stderr.decode('utf-8', errors='replace')[:500]}"
        )

    return output_path

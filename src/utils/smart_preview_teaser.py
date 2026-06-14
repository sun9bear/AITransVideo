"""智能版 3 分钟预览 teaser 裁剪（P3e-3b，consumer 侧 / 钱-关键耦合）.

把登录智能版预览任务（``job_smart_preview=True``）的源**音频**裁剪到 ~180s
teaser，**在 separation / ASR / clone / TTS 等付费阶段之前**执行。音频 teaser 同时
驱动两条边界：

1. **付费 AI 阶段有界**：分离 / ASR / 克隆 / TTS 全部读 teaser 派生的音频 →
   工作量限到 3 分钟。
2. **成片时长有界**：pipeline 用 ``actual_duration_ms =
   _ffprobe_duration_ms(source_audio_path)`` 喂给 EditorPackageWriter 造 base
   silence（成片音轨长度），再经 render ``-shortest`` 把成片收口到 ~3 分钟。

**视频不裁**：原 ``original.mp4`` 保留 + render 经 ``-shortest`` + 3min base
silence 收口成片。这样 artifact index 的 ``source.original_video`` 仍指向真原视频
（供 P3e-3c-2「preview → 正式」转完整复用原视频），不被 teaser 污染。

⚠️ 钱-关键耦合（plan 2026-06-14-p3e2 §4 / handoff §4）：本步**仍扣分钟**（create
端 reserve 不变 → settle_job_quota / credit_ledger 照常 = 无漏收），只把工作 / 产物
限到 3 分钟。后续 **P3e-3c 跳分钟必须在本步之后**——否则 pipeline 跑完整视频却不
收分钟 = 免费完整任务（漏收全部分钟点 ≫ 克隆 600）。

**fail-closed**：任何裁剪失败（ffmpeg 不可用 / 非零退出 / 超时 / 产物缺失 / 产物
时长不可信或越界 / src==dest）→ 抛 ``SmartPreviewTeaserError`` 使任务 terminal
failed，**绝不**退回处理完整源。镜像 ``AnonymousExpressPass3Failed`` 的"不降级
出片"语义。

**原文件保留**：teaser 写新文件（``audio/preview_teaser.wav``），``original.wav`` /
``original.mp4`` 不被删 / 改。

**gemini-on-URL 边界**：本模块只裁剪本地音频。``transcription_method == "gemini"``
读 ``normalized_url``（原始 URL）做多模态转录，本地 teaser 无法有界它 → caller
（process.py）用 ``smart_preview_gemini_url_unbounded`` 守卫提前 fail-closed。
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# teaser 目标时长（秒）。镜像匿名预览 ``anonymous_preview_max_seconds`` 默认 180
# （gateway/config.py / gateway/admin_settings.py）+ ``DEFAULT_MAX_PREVIEW_
# DURATION_SECONDS``（src/services/anonymous_preview_admission.py）。smart 预览复用
# 同一 3 分钟 teaser（plan 2026-06-14-p3e2 §4）。未来如需独立 smart_preview_max_
# seconds 旋钮再加，本期硬编 180 与匿名一致。
SMART_PREVIEW_TEASER_SECONDS: float = 180.0

# 硬上限容差（秒）：ffmpeg ``-t`` 重编码会因帧边界 / 音频 priming 轻微越界切点
# （匿名侧现网实测 180.014s）。裁剪后产物时长 ≤ target + 容差 才算成功，否则
# fail-closed。容差给编码噪声留余量，同时仍能抓出「``-t`` 未生效 / 源根本没被裁」
# 的完整源（远 > target + 容差）。
_TEASER_OVERSHOOT_TOLERANCE_S: float = 5.0

FFMPEG_TIMEOUT_SECONDS: float = 600.0
FFPROBE_TIMEOUT_SECONDS: float = 30.0

# teaser 音频落盘文件名（与 original.wav 同目录、不同名 → 原文件保留；resume/
# publish 路径据此回找）。
_TEASER_AUDIO_NAME = "preview_teaser.wav"

Runner = Callable[..., "subprocess.CompletedProcess"]
Prober = Callable[[Path], Optional[float]]


class SmartPreviewTeaserError(RuntimeError):
    """智能版预览 teaser 裁剪 fail-closed（plan 2026-06-14-p3e2 §4 钱-关键耦合）。

    裁剪失败时抛出，使任务 terminal failed；**绝不**退回处理完整源——那会让
    P3e-3c 跳分钟之后变成免费完整任务（漏收全部分钟点 ≫ 克隆 600）。
    """


def smart_preview_gemini_url_unbounded(
    transcription_method: object, normalized_url: object
) -> bool:
    """smart 预览的 gemini-on-URL 转录是否**无法被本地 teaser 有界**（纯谓词）。

    ``transcription_method == "gemini"`` 时 pipeline 把 ``normalized_url``（原始
    URL）直接交给 Gemini 多模态转录（process.py 的 gemini 分支），读的是 URL 而非
    本地裁剪后的 teaser 音频 → 本地 teaser 裁剪无法有界它，会对**完整**源跑付费
    转录 / 后续 TTS。

    返回 True（=未有界，caller 须 fail-closed）当且仅当：method 为 gemini **且**
    ``normalized_url`` 非空。smart 预览默认 assemblyai + 本地上传（normalized_url
    为空 → 走本地 speech_audio_path，已被 teaser 有界）→ 返回 False。
    """
    method = str(transcription_method or "").strip().lower()
    url = str(normalized_url or "").strip()
    return method == "gemini" and bool(url)


def build_teaser_ffmpeg_cmd(
    src: Path, dest: Path, *, max_seconds: float
) -> list[str]:
    """构造把 ``src`` 前 ``max_seconds`` 秒裁成 ``dest`` 音频 teaser 的 ffmpeg 命令
    （纯函数）。丢弃视频流（``-vn``）+ ``pcm_s16le``（original.wav 同制式）。

    ``dest`` 始终是命令最后一个参数（runner 桩据此定位产物）。
    """
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-t",
        str(max_seconds),
        "-vn",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]


def _default_prober(path: Path) -> Optional[float]:
    """ffprobe 读 ``path`` 时长（秒）；任何失败 → None（caller fail-closed）。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
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
    except Exception:  # noqa: BLE001 — fail-closed（caller 视 None 为失败）
        logger.warning("[smart_teaser] ffprobe failed", exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        raw = data.get("format", {}).get("duration")
        return float(raw) if raw is not None else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def trim_to_teaser(
    src: Path,
    dest: Path,
    *,
    max_seconds: float,
    runner: Runner = subprocess.run,
    prober: Prober = _default_prober,
) -> float:
    """把 ``src`` 前 ``max_seconds`` 秒 re-encode 成音频 teaser ``dest``，返回产物
    真实时长（秒）。

    fail-closed：以下任一 → 抛 ``SmartPreviewTeaserError``（**绝不**返回未裁剪源）：
    ``src == dest``（防 ``ffmpeg -y`` clobber 原文件，CodeX P3）；ffmpeg 不可用 /
    超时 / 非零退出；产物缺失或为空；产物时长不可信（None / ≤0 / 非有限）；产物
    时长 > ``max_seconds + 容差``（=源根本没被裁 / ``-t`` 未生效）。
    """
    src = Path(src)
    dest = Path(dest)
    if src.resolve(strict=False) == dest.resolve(strict=False):
        # ``ffmpeg -y`` 会用半成品覆盖原文件 → 原文件丢失 + 可能未真正裁剪。
        raise SmartPreviewTeaserError(
            "teaser src 与 dest 相同（防 ffmpeg -y clobber 原文件），fail-closed"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_teaser_ffmpeg_cmd(src, dest, max_seconds=max_seconds)
    try:
        proc = runner(
            cmd,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SmartPreviewTeaserError(
            "ffmpeg 不可用（teaser 裁剪 fail-closed）"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SmartPreviewTeaserError(
            "ffmpeg 超时（teaser 裁剪 fail-closed）"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — 任何 runner 异常都 fail-closed
        raise SmartPreviewTeaserError(
            f"teaser 裁剪 runner 异常（fail-closed）：{type(exc).__name__}"
        ) from exc

    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        raise SmartPreviewTeaserError(
            f"ffmpeg 非零退出 rc={rc}（teaser 裁剪 fail-closed）"
        )
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise SmartPreviewTeaserError(
            "teaser 产物缺失或为空（fail-closed）"
        )

    duration = prober(dest)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise SmartPreviewTeaserError(
            "teaser 产物时长不可信（fail-closed）"
        )
    if duration > max_seconds + _TEASER_OVERSHOOT_TOLERANCE_S:
        # 最关键的钱-防线：产物时长越界 = 源根本没被裁（完整源），绝不放行。
        raise SmartPreviewTeaserError(
            f"teaser 产物时长 {duration:.3f}s 越界（> {max_seconds}+容差），"
            "源未被有界，fail-closed"
        )
    return duration


def apply_smart_preview_teaser(
    *,
    source_audio_path: Path,
    project_dir: Path,
    max_seconds: float = SMART_PREVIEW_TEASER_SECONDS,
    runner: Runner = subprocess.run,
    prober: Prober = _default_prober,
) -> Path:
    """把 smart 预览任务的源**音频**裁到 teaser，返回 teaser 音频路径。

    * **音频必裁**（fail-closed）：音频边界 = separation → ASR → clone → TTS 的
      工作量边界，也经 ``actual_duration_ms`` → EditorPackageWriter base-silence +
      render ``-shortest`` 决定成片时长。源音频缺失或裁剪失败 → 抛错。
    * **视频不动**：原 ``original.mp4`` 保留；render 经 ``-shortest`` + 3min base
      silence 收口成片，无需裁视频（且避免污染 artifact index source.original_video）。
    * **原文件保留**：teaser 写 ``audio/preview_teaser.wav``，``original.wav`` 不动。

    repoint 由 caller 完成：``source_audio_path = apply_smart_preview_teaser(...)``
    （video_path **保持原值**）。
    """
    source_audio_path = Path(source_audio_path)
    project_dir = Path(project_dir)

    if not source_audio_path.is_file():
        raise SmartPreviewTeaserError(
            "smart 预览源音频缺失，无法有界 ASR/TTS/base-silence（fail-closed）"
        )

    teaser_audio = project_dir / "audio" / _TEASER_AUDIO_NAME
    trim_to_teaser(
        source_audio_path,
        teaser_audio,
        max_seconds=max_seconds,
        runner=runner,
        prober=prober,
    )
    return teaser_audio


__all__ = [
    "SMART_PREVIEW_TEASER_SECONDS",
    "FFMPEG_TIMEOUT_SECONDS",
    "FFPROBE_TIMEOUT_SECONDS",
    "SmartPreviewTeaserError",
    "smart_preview_gemini_url_unbounded",
    "build_teaser_ffmpeg_cmd",
    "trim_to_teaser",
    "apply_smart_preview_teaser",
]

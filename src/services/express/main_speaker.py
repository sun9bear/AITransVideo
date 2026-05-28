"""Express 主说话人识别（轻量版，spec §4.2）。

纯函数，无副作用，零外部依赖（不碰 audio / DB / 网络）。Smart 用
``eligibility_gate._smart_main_speaker_ids``；Express **不引入** eligibility_gate
（Smart 专属），改用这个最小实现：占比最高 + 占比 ≥ min_ratio + 至少
min_line_count 行。

阈值默认 0.30 / 5（admin_settings 可调，由 caller 传入）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class MainSpeakerStats:
    """主说话人占比统计（喂 audit）。"""

    speaker_id: str
    line_count: int
    total_lines: int
    ratio: float


def speaker_of(line: object) -> str:
    """从 transcript line 取 speaker_id，兼容对象属性与 dict。"""
    sid = getattr(line, "speaker_id", None)
    if sid is None and isinstance(line, dict):
        sid = line.get("speaker_id") or line.get("speaker")
    return str(sid or "").strip()


def speaker_distribution(transcript_lines) -> tuple[Counter, int]:
    """统计每个 speaker 的行数 + 总行数（忽略无 speaker 的行）。"""
    counts: Counter = Counter()
    for ln in transcript_lines or []:
        sid = speaker_of(ln)
        if sid:
            counts[sid] += 1
    return counts, sum(counts.values())


def identify_express_main_speaker(
    transcript_lines,
    *,
    min_ratio: float = 0.30,
    min_line_count: int = 5,
) -> str | None:
    """选主说话人：占比最高且占比 ≥ min_ratio 且至少 min_line_count 行。

    返回 speaker_id 或 None。None 表示不适合自动 clone（单段噪音 / 平均分配
    的多 speaker / 行数过少）。spec §4.2 契约。
    """
    counts, total = speaker_distribution(transcript_lines)
    if not counts or total <= 0:
        return None
    top_speaker, top_count = counts.most_common(1)[0]
    if top_count < min_line_count:
        return None
    if top_count / total < min_ratio:
        return None
    return top_speaker


def main_speaker_stats(transcript_lines, speaker_id: str | None) -> MainSpeakerStats | None:
    """给定 speaker_id 的占比统计（line_count / total / ratio），喂 audit。"""
    if not speaker_id:
        return None
    counts, total = speaker_distribution(transcript_lines)
    if speaker_id not in counts or total <= 0:
        return None
    line_count = counts[speaker_id]
    return MainSpeakerStats(
        speaker_id=speaker_id,
        line_count=line_count,
        total_lines=total,
        ratio=line_count / total,
    )


__all__ = [
    "MainSpeakerStats",
    "speaker_of",
    "speaker_distribution",
    "identify_express_main_speaker",
    "main_speaker_stats",
]

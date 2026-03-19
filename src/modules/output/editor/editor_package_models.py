from __future__ import annotations

from dataclasses import dataclass


ALIGNMENT_METHOD_LABELS = {
    "direct": "直接使用（误差<5%）",
    "dsp": "DSP变速",
    "rewrite_direct": "Gemini重写后直接使用",
    "rewrite_dsp": "Gemini重写后DSP对齐",
    "force_dsp": "强制DSP兜底",
}


@dataclass(slots=True)
class AlignedSegment:
    segment_id: int
    speaker_id: str
    display_name: str
    start_ms: int
    end_ms: int
    cn_text: str
    aligned_audio_path: str
    actual_duration_ms: int
    alignment_method: str
    needs_review: bool


@dataclass(slots=True)
class ProjectOutput:
    project_id: str
    youtube_url: str
    video_title: str
    total_duration_ms: int
    segments: list[AlignedSegment]
    output_dir: str


@dataclass(slots=True)
class ProjectOutputResult:
    dubbed_audio_path: str
    ambient_audio_path: str
    segments_dir: str
    segment_count: int
    subtitles_path: str
    background_sounds_path: str
    alignment_report_path: str
    needs_review_count: int

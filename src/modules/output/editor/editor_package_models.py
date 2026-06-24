from __future__ import annotations

from dataclasses import dataclass, field


ALIGNMENT_METHOD_LABELS = {
    "direct": "直接使用（误差<5%）",
    "dsp": "DSP变速",
    "rewrite_direct": "Gemini重写后直接使用",
    "rewrite_dsp": "Gemini重写后DSP对齐",
    "force_dsp": "强制DSP兜底",
    "capped_dsp_overflow": "短段听感保护DSP",
    "capped_dsp_underflow": "限速DSP并补静音",
    "keep_original": "保留原音",
}


@dataclass(slots=True)
class AlignedSegment:
    segment_id: int
    speaker_id: str
    display_name: str
    start_ms: int
    end_ms: int
    cn_text: str
    en_text: str
    aligned_audio_path: str
    actual_duration_ms: int
    alignment_method: str
    needs_review: bool
    dubbing_mode: str = "dub"


@dataclass(slots=True)
class ProjectOutput:
    project_id: str
    youtube_url: str
    video_title: str
    total_duration_ms: int
    segments: list[AlignedSegment]
    output_dir: str
    # Optional canonical cues from subtitle-cue-generation-v2 pipeline (T8).
    # When non-empty, EditorPackageWriter routes SRT generation through
    # srt_writer (canonical path). When empty (default), the legacy
    # _build_subtitle_slices segment-based path is used as fallback.
    # T9 (OutputDispatcher) wires the cue builder output here; until then
    # this field is populated only in tests via direct ProjectOutput construction.
    subtitle_cues: list = field(default_factory=list)


@dataclass(slots=True)
class ProjectOutputResult:
    dubbed_audio_path: str
    ambient_audio_path: str
    segments_dir: str
    segment_count: int
    subtitles_path: str
    subtitles_en_path: str
    subtitles_bilingual_path: str
    background_sounds_path: str
    alignment_report_path: str
    needs_review_count: int
    # PR-F: script-neutral source/target SRT paths (cue.en_text=SOURCE, cue.text=TARGET).
    # Defaulted so existing positional constructors / tests stay back-compatible; the
    # writer populates them by keyword. For en->zh these are byte-identical to the
    # subtitles_en/subtitles(zh) files.
    subtitles_source_path: str = ""
    subtitles_target_path: str = ""

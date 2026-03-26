"""扫描项目目录，确定精确的恢复点。"""
import glob
import json
import os
from dataclasses import dataclass, field

@dataclass
class ResumePoint:
    stage: str
    start_segment: int = 0
    start_batch: int = 0
    metadata: dict = field(default_factory=dict)

def find_resume_point(project_dir: str) -> ResumePoint:
    from src.utils.atomic_io import cleanup_tmp_files
    cleanup_tmp_files(project_dir)

    def exists(relpath):
        return os.path.isfile(os.path.join(project_dir, relpath))

    def count_valid(pattern):
        matches = glob.glob(os.path.join(project_dir, pattern))
        return sum(1 for m in matches if os.path.getsize(m) > 0 and not m.endswith(".tmp"))

    def get_total_segments():
        cp = os.path.join(project_dir, "checkpoint.json")
        if os.path.isfile(cp):
            try:
                with open(cp) as f:
                    return json.load(f).get("total_segments", 0)
            except Exception:
                pass
        return 0

    # 从后往前检查
    if exists("output/dubbed_audio.wav"):
        return ResumePoint(stage="completed")

    total = get_total_segments()

    aligned = count_valid("alignment/segment_*_aligned.wav")
    if aligned > 0:
        if total > 0 and aligned >= total:
            return ResumePoint(stage="output_merge")
        return ResumePoint(stage="alignment", start_segment=aligned)

    tts_done = count_valid("tts/segment_*.wav")
    if tts_done > 0:
        if total > 0 and tts_done >= total:
            return ResumePoint(stage="alignment", start_segment=0)
        return ResumePoint(stage="tts", start_segment=tts_done)

    if exists("translation/translation_merged.json"):
        return ResumePoint(stage="tts", start_segment=0)

    batch_count = count_valid("translation/batch_*.json")
    if batch_count > 0:
        return ResumePoint(stage="translation", start_batch=batch_count)

    if exists("transcript/transcript.json"):
        return ResumePoint(stage="review_or_translate")

    if exists("transcript/raw_assemblyai.json"):
        return ResumePoint(stage="segmentation")

    if exists("video/original.mp4"):
        return ResumePoint(stage="audio_extraction")

    return ResumePoint(stage="ingestion")

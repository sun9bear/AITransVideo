"""
Checkpoint & resume utilities for the video translation pipeline.

Resume point detection via filesystem scanning.
Atomic writes are in utils/atomic_io.py (no duplication).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


@dataclass
class ResumePoint:
    """Describes where to resume a pipeline run.

    Stages (in pipeline order):
        ingestion, audio_extraction, transcription, segmentation,
        review_or_translate, translation (+ start_batch),
        tts (+ start_segment), alignment (+ start_segment),
        output_merge, completed
    """

    stage: str
    start_segment: int = 0
    start_batch: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "start_segment": self.start_segment,
            "start_batch": self.start_batch,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ResumePoint:
        return cls(
            stage=d.get("stage", "ingestion"),
            start_segment=d.get("start_segment", 0),
            start_batch=d.get("start_batch", 0),
            metadata=d.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Resume-point detection
# ---------------------------------------------------------------------------

def _count_valid_files(directory: Path, pattern: str) -> int:
    """Count files matching *pattern* inside *directory* that are non-empty."""
    count = 0
    for p in sorted(directory.glob(pattern)):
        # Skip .tmp files and zero-byte files
        if p.suffix == ".tmp":
            continue
        if p.stat().st_size > 0:
            count += 1
    return count


def _count_total_segments(project_dir: Path) -> int:
    """Determine the total number of segments from the translation merge or
    transcript file.

    Looks (in order) at:
      1. translation/translation_merged.json  (list of segment dicts)
      2. transcript/transcript.json           (``segments`` key)

    Returns 0 if neither file exists or is parseable.
    """
    merged = project_dir / "translation" / "translation_merged.json"
    if merged.is_file():
        try:
            data = json.loads(merged.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict) and "segments" in data:
                return len(data["segments"])
        except (json.JSONDecodeError, OSError):
            pass

    transcript = project_dir / "transcript" / "transcript.json"
    if transcript.is_file():
        try:
            data = json.loads(transcript.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict) and "segments" in data:
                return len(data["segments"])
        except (json.JSONDecodeError, OSError):
            pass

    return 0


def _cleanup_tmp_files(project_dir: Path) -> int:
    """Remove all ``*.tmp`` files under *project_dir* (recursive).

    Returns the number of files removed.
    """
    removed = 0
    for tmp in project_dir.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def find_resume_point(project_dir: Union[str, Path]) -> ResumePoint:
    """Scan *project_dir* to determine the exact resume point.

    The function is called every time a pipeline run starts.  It:

    1. Cleans up leftover ``.tmp`` files (incomplete writes from a crash).
    2. Walks the directory tree **backwards** (from output → ingestion) and
       returns a :class:`ResumePoint` describing where to continue.

    Stage detection order (latest first):
        completed → output_merge → alignment → tts → translation →
        review_or_translate → segmentation → transcription →
        audio_extraction → ingestion
    """
    project_dir = Path(project_dir)

    # 1. Clean up incomplete .tmp files from a previous crash
    _cleanup_tmp_files(project_dir)

    # 2. Walk from the end of the pipeline back to the start

    # --- completed ---
    if (project_dir / "output" / "dubbed_audio.wav").is_file():
        return ResumePoint(stage="completed")

    total = _count_total_segments(project_dir)

    # --- alignment ---
    alignment_dir = project_dir / "alignment"
    if alignment_dir.is_dir():
        done = _count_valid_files(alignment_dir, "segment_*_aligned.wav")
        if total > 0 and done >= total:
            return ResumePoint(stage="output_merge")
        return ResumePoint(stage="alignment", start_segment=done)

    # --- tts ---
    tts_dir = project_dir / "tts"
    if tts_dir.is_dir():
        done = _count_valid_files(tts_dir, "segment_*.wav")
        if total > 0 and done >= total:
            return ResumePoint(stage="alignment", start_segment=0)
        return ResumePoint(stage="tts", start_segment=done)

    # --- translation (merged) ---
    if (project_dir / "translation" / "translation_merged.json").is_file():
        return ResumePoint(stage="tts", start_segment=0)

    # --- translation (batch files) ---
    translation_dir = project_dir / "translation"
    if translation_dir.is_dir():
        done = _count_valid_files(translation_dir, "batch_*.json")
        if done > 0:
            return ResumePoint(stage="translation", start_batch=done)

    # --- review_or_translate ---
    if (project_dir / "transcript" / "transcript.json").is_file():
        return ResumePoint(stage="review_or_translate")

    # --- segmentation ---
    if (project_dir / "transcript" / "segmented.json").is_file():
        return ResumePoint(stage="segmentation")

    # --- transcription ---
    if (project_dir / "transcript" / "raw_assemblyai.json").is_file():
        return ResumePoint(stage="segmentation")

    # --- audio_extraction ---
    if (project_dir / "audio" / "original.wav").is_file():
        return ResumePoint(stage="transcription")

    if (project_dir / "video" / "original.mp4").is_file():
        return ResumePoint(stage="audio_extraction")

    # --- ingestion (nothing done yet) ---
    return ResumePoint(stage="ingestion")

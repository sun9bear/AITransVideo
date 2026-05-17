"""Phase 2b v2 — LLM-backed suggest_split tests.

Covers (plan 2026-05-17 §5.4 v2):
- happy path: LLM returns needs_split + valid at_text → cuts parsed
- happy path: LLM returns needs_split=false → returned cleanly
- at_text not in source → that cut dropped; if all dropped → downgrade
- speaker_name reverse lookup
- per-segment cap (1)
- per-job cap = MAX(MIN(0.2*N, anomaly_count), 5)
- usage persisted across calls
- no audio file → SplitSuggestNoAudioError

LLM call is mocked via monkeypatching _call_llm.
"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from services.jobs.editing import enter_editing
from services.jobs.editing_split_suggest import (
    SplitSuggestCapExhaustedError,
    SplitSuggestNoAudioError,
    SplitSuggestSegmentUsedError,
    _compute_initial_cap,
    _find_source_index_for_at_text,
    _reverse_lookup_speaker_id,
    get_suggest_split_quota,
    suggest_split_for_segment,
)
from services.jobs.models import JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.store import JobStore


def _make_fake_wav(path: Path) -> None:
    """Write a minimal valid WAV header + 1 sample so audio readers
    (ffmpeg-based clip extraction) don't reject it during tests.
    The LLM call itself is mocked, so audio content doesn't matter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 44-byte RIFF header + 1 sample of silence (mono 16k 16-bit)
    sample_rate = 16000
    num_samples = 16000  # 1s of silence
    data_size = num_samples * 2
    header = b"".join([
        b"RIFF",
        struct.pack("<I", 36 + data_size),
        b"WAVE",
        b"fmt ",
        struct.pack("<I", 16),
        struct.pack("<H", 1),       # PCM
        struct.pack("<H", 1),       # mono
        struct.pack("<I", sample_rate),
        struct.pack("<I", sample_rate * 2),
        struct.pack("<H", 2),
        struct.pack("<H", 16),
        b"data",
        struct.pack("<I", data_size),
    ])
    path.write_bytes(header + b"\x00\x00" * num_samples)


def _make_project(
    tmp_path: Path,
    *,
    segments: list[dict] | None = None,
    with_audio: bool = True,
) -> Path:
    """Build a multi-segment editing-mode project with fake audio."""
    project_dir = tmp_path / "projects" / "job_ss"
    (project_dir / "editor").mkdir(parents=True)
    default_segments = [
        {
            "segment_id": "seg_001",
            "speaker_id": "speaker_a",
            "cn_text": "测试段落一里面包含一些内容",
            "source_text": "this is test segment one with some content",
            "start_ms": 5000,
            "end_ms": 15000,
        },
        {
            "segment_id": "seg_002",
            "speaker_id": "speaker_b",
            "cn_text": "段落二",
            "source_text": "segment two",
            "start_ms": 15000,
            "end_ms": 18000,
        },
    ]
    use = segments if segments is not None else default_segments
    (project_dir / "editor" / "segments.json").write_text(
        json.dumps(use, ensure_ascii=False), encoding="utf-8"
    )
    if with_audio:
        _make_fake_wav(project_dir / "audio" / "speech_for_asr.wav")

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_ss",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="x",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=now_iso,
        updated_at=now_iso,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    store = JobStore(tmp_path / "jobs")
    store.save_job(record)
    enter_editing(record, store)
    return project_dir


# ---------------------------------------------------------------------------
# Pure-helper tests (no mock needed)
# ---------------------------------------------------------------------------


def test_compute_cap_floor_5_when_no_anomalies() -> None:
    """0 anomaly segments → cap = MAX(MIN(0.2*N, 0), 5) = 5."""
    segs = [{"segment_id": f"s{i}", "alignment_method": "speech_align"} for i in range(50)]
    assert _compute_initial_cap(segs) == 5


def test_compute_cap_min_of_ratio_and_anomaly() -> None:
    """20 anomalies in 100 → cap = MAX(MIN(20, 20), 5) = 20."""
    segs = []
    for i in range(100):
        segs.append({
            "segment_id": f"s{i}",
            "alignment_method": "force_dsp" if i < 20 else "speech_align",
        })
    assert _compute_initial_cap(segs) == 20


def test_compute_cap_ratio_clips_anomaly() -> None:
    """80 anomalies in 100 → MIN(20, 80) = 20 → MAX(20, 5) = 20."""
    segs = []
    for i in range(100):
        segs.append({
            "segment_id": f"s{i}",
            "alignment_method": "force_dsp" if i < 80 else "speech_align",
        })
    assert _compute_initial_cap(segs) == 20


def test_find_at_text_exact() -> None:
    src = "this is test segment one with some content"
    end = _find_source_index_for_at_text(src, "test segment one")
    assert end == src.find("test segment one") + len("test segment one")


def test_find_at_text_case_insensitive() -> None:
    src = "This Is A Test Segment"
    end = _find_source_index_for_at_text(src, "test segment")
    assert end == len("This Is A Test Segment")


def test_find_at_text_not_found_returns_none() -> None:
    assert _find_source_index_for_at_text("hello world", "nonexistent phrase") is None


def test_reverse_lookup_by_name() -> None:
    smap = {"speaker_a": "彼得·戴曼迪斯", "speaker_b": "埃隆·马斯克"}
    assert _reverse_lookup_speaker_id(
        "埃隆·马斯克", smap, "speaker_a", ["speaker_a", "speaker_b"]
    ) == "speaker_b"


def test_reverse_lookup_unknown_name_falls_back_first_other() -> None:
    smap = {"speaker_a": "彼得·戴曼迪斯", "speaker_b": "埃隆·马斯克"}
    # Unknown name "嘉宾 C" → fallback to first sid that's NOT the current one
    assert _reverse_lookup_speaker_id(
        "嘉宾 C", smap, "speaker_a", ["speaker_a", "speaker_b"]
    ) == "speaker_b"


# ---------------------------------------------------------------------------
# Kernel integration tests (LLM + audio prep mocked)
# ---------------------------------------------------------------------------


def _patch_kernel_audio_and_llm(llm_response: dict):
    """Return a context manager that patches the audio-clip extractor
    to return a placeholder path and the LLM call to return the given
    response. Avoids real ffmpeg / Gemini calls in tests."""
    from contextlib import ExitStack

    stack = ExitStack()

    def _fake_prepare_clip(_audio_path, tmp_dir, *, start_ms, end_ms, clip_index=0, bitrate=None):
        out = Path(tmp_dir) / f"fake_clip_{clip_index}.ogg"
        out.write_bytes(b"fake-opus-bytes")
        return out

    stack.enter_context(patch(
        "services.transcript_reviewer._prepare_review_audio_clip",
        _fake_prepare_clip,
    ))
    stack.enter_context(patch(
        "services.jobs.editing_split_suggest._call_llm",
        lambda **_kw: llm_response,
    ))
    return stack


def test_suggest_split_needs_split_true_happy(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    smap = {"speaker_a": "彼得·戴曼迪斯", "speaker_b": "埃隆·马斯克"}
    llm = {
        "needs_split": True,
        "reason": "段中有清晰说话人切换",
        "cuts": [
            {
                "at_text": "test segment one",
                "speaker_before": "彼得·戴曼迪斯",
                "speaker_after": "埃隆·马斯克",
            }
        ],
    }
    with _patch_kernel_audio_and_llm(llm):
        result = suggest_split_for_segment(
            project_dir,
            "seg_001",
            speaker_name_map=smap,
            available_speaker_ids=["speaker_a", "speaker_b"],
            review_model="gemini-2.5-pro",  # value doesn't matter — LLM is mocked
        )
    assert result["needs_split"] is True
    assert len(result["cuts"]) == 1
    cut = result["cuts"][0]
    src = "this is test segment one with some content"
    assert cut["source_index"] == src.find("test segment one") + len("test segment one")
    assert cut["speaker_id"] == "speaker_b"
    # Usage tracked
    assert result["usage"]["used"] == 1
    assert result["usage"]["remaining"] == result["usage"]["cap"] - 1


def test_suggest_split_needs_split_false_clean(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    llm = {"needs_split": False, "reason": "整段为同一说话人"}
    with _patch_kernel_audio_and_llm(llm):
        result = suggest_split_for_segment(
            project_dir,
            "seg_001",
            speaker_name_map={"speaker_a": "A"},
            available_speaker_ids=["speaker_a"],
            review_model="gemini-2.5-pro",
        )
    assert result["needs_split"] is False
    assert result["cuts"] == []
    assert result["usage"]["used"] == 1  # the call still counts toward cap


def test_suggest_split_all_cuts_unparseable_downgrades(tmp_path: Path) -> None:
    """LLM says needs_split but at_text not in source → drop cut + downgrade."""
    project_dir = _make_project(tmp_path)
    llm = {
        "needs_split": True,
        "reason": "x",
        "cuts": [
            {"at_text": "this phrase does not exist in source", "speaker_after": "X"}
        ],
    }
    with _patch_kernel_audio_and_llm(llm):
        result = suggest_split_for_segment(
            project_dir,
            "seg_001",
            speaker_name_map={"speaker_a": "A"},
            available_speaker_ids=["speaker_a"],
            review_model="gemini-2.5-pro",
        )
    assert result["needs_split"] is False
    assert result["cuts"] == []
    assert "未能在原文中精确定位" in result["reason"]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


def test_per_segment_cap_blocks_second_call(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    llm = {"needs_split": False, "reason": "x"}
    with _patch_kernel_audio_and_llm(llm):
        suggest_split_for_segment(
            project_dir,
            "seg_001",
            speaker_name_map={"speaker_a": "A"},
            available_speaker_ids=["speaker_a"],
            review_model="gemini-2.5-pro",
        )
        with pytest.raises(SplitSuggestSegmentUsedError):
            suggest_split_for_segment(
                project_dir,
                "seg_001",
                speaker_name_map={"speaker_a": "A"},
                available_speaker_ids=["speaker_a"],
                review_model="gemini-2.5-pro",
            )


def test_per_job_cap_blocks_after_threshold(tmp_path: Path) -> None:
    """Floor cap = 5 for clean-alignment jobs. Allow 5 successful
    calls (on distinct segments), then 6th raises."""
    segments = [
        {
            "segment_id": f"seg_{i:03d}",
            "speaker_id": "speaker_a",
            "cn_text": f"段{i}",
            "source_text": f"segment {i} content here for testing purposes",
            "start_ms": i * 5000,
            "end_ms": (i + 1) * 5000,
        }
        for i in range(10)
    ]
    project_dir = _make_project(tmp_path, segments=segments)
    llm = {"needs_split": False, "reason": "x"}
    with _patch_kernel_audio_and_llm(llm):
        for i in range(5):
            suggest_split_for_segment(
                project_dir,
                f"seg_{i:03d}",
                speaker_name_map={"speaker_a": "A"},
                available_speaker_ids=["speaker_a"],
                review_model="gemini-2.5-pro",
            )
        # 6th distinct-segment call hits cap
        with pytest.raises(SplitSuggestCapExhaustedError) as exc_info:
            suggest_split_for_segment(
                project_dir,
                "seg_005",
                speaker_name_map={"speaker_a": "A"},
                available_speaker_ids=["speaker_a"],
                review_model="gemini-2.5-pro",
            )
        assert exc_info.value.cap == 5
        assert exc_info.value.used == 5


def test_quota_endpoint_reads_current_state(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    q0 = get_suggest_split_quota(project_dir)
    assert q0["used"] == 0
    assert q0["cap"] >= 5
    llm = {"needs_split": False, "reason": "x"}
    with _patch_kernel_audio_and_llm(llm):
        suggest_split_for_segment(
            project_dir,
            "seg_001",
            speaker_name_map={"speaker_a": "A"},
            available_speaker_ids=["speaker_a"],
            review_model="gemini-2.5-pro",
        )
    q1 = get_suggest_split_quota(project_dir)
    assert q1["used"] == 1
    assert "seg_001" in q1["segment_ids_used"]


def test_no_audio_raises(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path, with_audio=False)
    llm = {"needs_split": False, "reason": "x"}
    with _patch_kernel_audio_and_llm(llm):
        with pytest.raises(SplitSuggestNoAudioError):
            suggest_split_for_segment(
                project_dir,
                "seg_001",
                speaker_name_map={"speaker_a": "A"},
                available_speaker_ids=["speaker_a"],
                review_model="gemini-2.5-pro",
            )

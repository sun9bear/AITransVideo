import json
import os
import tempfile
from src.utils.resume_point import find_resume_point, ResumePoint

def _touch(base, relpath, content=b"data"):
    path = os.path.join(base, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)

def test_empty_project_returns_ingestion():
    with tempfile.TemporaryDirectory() as d:
        rp = find_resume_point(d)
        assert rp.stage == "ingestion"

def test_video_exists_returns_audio_extraction():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "video/original.mp4")
        rp = find_resume_point(d)
        assert rp.stage == "audio_extraction"

def test_raw_assemblyai_returns_segmentation():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/raw_assemblyai.json")
        rp = find_resume_point(d)
        assert rp.stage == "segmentation"

def test_transcript_exists_returns_review():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        rp = find_resume_point(d)
        assert rp.stage == "review_or_translate"

def test_translation_batches_returns_correct_offset():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/batch_001.json")
        _touch(d, "translation/batch_002.json")
        rp = find_resume_point(d)
        assert rp.stage == "translation"
        assert rp.start_batch == 2

def test_translation_merged_returns_tts():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        rp = find_resume_point(d)
        assert rp.stage == "tts"
        assert rp.start_segment == 0

def test_partial_tts_returns_correct_offset():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav")
        _touch(d, "tts/segment_003.wav.tmp")  # 不完整
        rp = find_resume_point(d)
        assert rp.stage == "tts"
        assert rp.start_segment == 2

def test_tmp_files_cleaned_on_resume():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav.tmp")
        find_resume_point(d)
        assert not os.path.exists(os.path.join(d, "tts/segment_002.wav.tmp"))

def test_all_tts_done_returns_alignment():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "checkpoint.json", json.dumps({"total_segments": 3}).encode())
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav")
        _touch(d, "tts/segment_003.wav")
        rp = find_resume_point(d)
        assert rp.stage == "alignment"
        assert rp.start_segment == 0

def test_partial_alignment_returns_correct_offset():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "checkpoint.json", json.dumps({"total_segments": 5}).encode())
        _touch(d, "transcript/transcript.json")
        _touch(d, "translation/translation_merged.json")
        _touch(d, "tts/segment_001.wav")
        _touch(d, "tts/segment_002.wav")
        _touch(d, "tts/segment_003.wav")
        _touch(d, "tts/segment_004.wav")
        _touch(d, "tts/segment_005.wav")
        _touch(d, "alignment/segment_001_aligned.wav")
        _touch(d, "alignment/segment_002_aligned.wav")
        rp = find_resume_point(d)
        assert rp.stage == "alignment"
        assert rp.start_segment == 2

def test_all_aligned_returns_output_merge():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "checkpoint.json", json.dumps({"total_segments": 2}).encode())
        _touch(d, "alignment/segment_001_aligned.wav")
        _touch(d, "alignment/segment_002_aligned.wav")
        rp = find_resume_point(d)
        assert rp.stage == "output_merge"

def test_output_exists_returns_completed():
    with tempfile.TemporaryDirectory() as d:
        _touch(d, "output/dubbed_audio.wav")
        rp = find_resume_point(d)
        assert rp.stage == "completed"

"""集成测试：模拟各种中断场景，验证恢复逻辑。"""
import json
import os
import tempfile

from src.utils.atomic_io import atomic_write_bytes, atomic_write_json, cleanup_tmp_files
from src.utils.resume_point import find_resume_point


def _touch(base, relpath, content=b"data"):
    path = os.path.join(base, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


class TestFullResumeScenarios:
    """完整的中断恢复场景测试"""

    def test_fresh_project(self):
        """全新项目，从 ingestion 开始"""
        with tempfile.TemporaryDirectory() as d:
            rp = find_resume_point(d)
            assert rp.stage == "ingestion"
            assert rp.start_segment == 0

    def test_video_downloaded_audio_not_extracted(self):
        """视频下载完成，音频未提取"""
        with tempfile.TemporaryDirectory() as d:
            _touch(d, "video/original.mp4")
            rp = find_resume_point(d)
            assert rp.stage == "audio_extraction"

    def test_transcription_complete_awaiting_review(self):
        """转录完成，等待审核"""
        with tempfile.TemporaryDirectory() as d:
            _touch(d, "video/original.mp4")
            _touch(d, "transcript/raw_assemblyai.json")
            _touch(d, "transcript/transcript.json")
            rp = find_resume_point(d)
            assert rp.stage == "review_or_translate"

    def test_translation_interrupted_at_batch_3(self):
        """翻译在第 3 批中断"""
        with tempfile.TemporaryDirectory() as d:
            _touch(d, "transcript/transcript.json")
            _touch(d, "translation/batch_001.json")
            _touch(d, "translation/batch_002.json")
            _touch(d, "translation/batch_003.json")
            rp = find_resume_point(d)
            assert rp.stage == "translation"
            assert rp.start_batch == 3

    def test_tts_interrupted_at_segment_5(self):
        """TTS 在第 5 段处中断（第 6 段有 .tmp 残留）"""
        with tempfile.TemporaryDirectory() as d:
            atomic_write_json(
                os.path.join(d, "checkpoint.json"),
                {"total_segments": 10},
            )
            _touch(d, "transcript/transcript.json")
            _touch(d, "translation/translation_merged.json")
            for i in range(1, 6):
                atomic_write_bytes(
                    os.path.join(d, f"tts/segment_{i:03d}.wav"),
                    b"audio data",
                )
            # 第 6 段中断，留下 .tmp
            _touch(d, "tts/segment_006.wav.tmp", b"partial")

            rp = find_resume_point(d)
            assert rp.stage == "tts"
            assert rp.start_segment == 5
            # .tmp 应该被清理
            assert not os.path.exists(os.path.join(d, "tts/segment_006.wav.tmp"))

    def test_tts_complete_alignment_not_started(self):
        """TTS 全部完成，对齐未开始"""
        with tempfile.TemporaryDirectory() as d:
            atomic_write_json(
                os.path.join(d, "checkpoint.json"),
                {"total_segments": 3},
            )
            _touch(d, "translation/translation_merged.json")
            for i in range(1, 4):
                _touch(d, f"tts/segment_{i:03d}.wav")
            rp = find_resume_point(d)
            assert rp.stage == "alignment"
            assert rp.start_segment == 0

    def test_alignment_interrupted_at_segment_2(self):
        """对齐在第 2 段完成后中断"""
        with tempfile.TemporaryDirectory() as d:
            atomic_write_json(
                os.path.join(d, "checkpoint.json"),
                {"total_segments": 5},
            )
            for i in range(1, 6):
                _touch(d, f"tts/segment_{i:03d}.wav")
            _touch(d, "alignment/segment_001_aligned.wav")
            _touch(d, "alignment/segment_002_aligned.wav")
            # 第 3 段有 .tmp
            _touch(d, "alignment/segment_003_aligned.wav.tmp", b"partial")

            rp = find_resume_point(d)
            assert rp.stage == "alignment"
            assert rp.start_segment == 2
            assert not os.path.exists(
                os.path.join(d, "alignment/segment_003_aligned.wav.tmp")
            )

    def test_all_aligned_ready_for_merge(self):
        """全部对齐完成，准备合并"""
        with tempfile.TemporaryDirectory() as d:
            atomic_write_json(
                os.path.join(d, "checkpoint.json"),
                {"total_segments": 2},
            )
            _touch(d, "alignment/segment_001_aligned.wav")
            _touch(d, "alignment/segment_002_aligned.wav")
            rp = find_resume_point(d)
            assert rp.stage == "output_merge"

    def test_output_exists_completed(self):
        """最终输出已完成"""
        with tempfile.TemporaryDirectory() as d:
            _touch(d, "output/dubbed_audio.wav")
            rp = find_resume_point(d)
            assert rp.stage == "completed"

    def test_multiple_tmp_files_all_cleaned(self):
        """多个 .tmp 文件全部被清理"""
        with tempfile.TemporaryDirectory() as d:
            _touch(d, "tts/segment_001.wav")
            _touch(d, "tts/segment_002.wav.tmp")
            _touch(d, "tts/segment_003.wav.tmp")
            _touch(d, "alignment/segment_001_aligned.wav.tmp")
            _touch(d, "translation/batch_005.json.tmp")

            find_resume_point(d)

            assert not os.path.exists(os.path.join(d, "tts/segment_002.wav.tmp"))
            assert not os.path.exists(os.path.join(d, "tts/segment_003.wav.tmp"))
            assert not os.path.exists(os.path.join(d, "alignment/segment_001_aligned.wav.tmp"))
            assert not os.path.exists(os.path.join(d, "translation/batch_005.json.tmp"))

    def test_atomic_write_survives_crash_simulation(self):
        """验证原子写入的 .tmp 不会被误判为已完成"""
        with tempfile.TemporaryDirectory() as d:
            # 模拟写入中断：.tmp 存在但目标不存在
            _touch(d, "tts/segment_001.wav.tmp", b"partial write")
            from src.utils.atomic_io import is_valid_output
            # .tmp 不是有效输出
            assert is_valid_output(os.path.join(d, "tts/segment_001.wav")) is False
            # 清理后 .tmp 消失
            cleanup_tmp_files(d)
            assert not os.path.exists(os.path.join(d, "tts/segment_001.wav.tmp"))

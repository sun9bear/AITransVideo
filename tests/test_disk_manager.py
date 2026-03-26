import os
import tempfile
from src.utils.disk_manager import (
    check_disk_space,
    estimate_required_gb,
    cleanup_intermediate,
    get_project_size_mb,
)

def test_estimate_required_gb():
    assert estimate_required_gb(30) == 30 * 0.035
    assert estimate_required_gb(60) == 60 * 0.035
    assert estimate_required_gb(0) == 0

def test_check_disk_space_with_temp():
    # 临时目录应该有空间
    with tempfile.TemporaryDirectory() as d:
        assert check_disk_space(0.001, d) is True

def test_cleanup_intermediate():
    with tempfile.TemporaryDirectory() as d:
        # 创建模拟文件
        os.makedirs(os.path.join(d, "audio"))
        mp3 = os.path.join(d, "audio/original_upload.mp3")
        with open(mp3, "wb") as f:
            f.write(b"fake mp3")
        # 清理
        count = cleanup_intermediate(d, "transcription_done")
        assert count == 1
        assert not os.path.exists(mp3)

def test_cleanup_unknown_stage():
    with tempfile.TemporaryDirectory() as d:
        count = cleanup_intermediate(d, "unknown_stage")
        assert count == 0

def test_get_project_size():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "test.bin"), "wb") as f:
            f.write(b"x" * 1024)  # 1 KB
        size = get_project_size_mb(d)
        assert 0.0009 < size < 0.002  # ~0.001 MB

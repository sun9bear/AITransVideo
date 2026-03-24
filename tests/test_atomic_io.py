import os
import json
import tempfile
from src.utils.atomic_io import atomic_write_bytes, atomic_write_json, is_valid_output, cleanup_tmp_files

def test_atomic_write_bytes_creates_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"fake audio data")
        assert os.path.exists(path)
        assert open(path, "rb").read() == b"fake audio data"

def test_atomic_write_no_tmp_residue():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"data")
        assert not os.path.exists(path + ".tmp")

def test_atomic_write_json_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        data = {"stage": "tts", "done": 42, "total": 100}
        atomic_write_json(path, data)
        assert json.loads(open(path).read()) == data

def test_atomic_write_overwrites_existing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        atomic_write_bytes(path, b"old")
        atomic_write_bytes(path, b"new")
        assert open(path, "rb").read() == b"new"

def test_atomic_write_creates_nested_dirs():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a", "b", "c", "test.wav")
        atomic_write_bytes(path, b"nested")
        assert open(path, "rb").read() == b"nested"

def test_is_valid_output_true():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.wav")
        with open(path, "wb") as f:
            f.write(b"data")
        assert is_valid_output(path) is True

def test_is_valid_output_false_missing():
    assert is_valid_output("/nonexistent/file.wav") is False

def test_is_valid_output_false_empty():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "empty.wav")
        with open(path, "wb") as f:
            pass
        assert is_valid_output(path) is False

def test_cleanup_tmp_files():
    with tempfile.TemporaryDirectory() as d:
        # 创建正常文件和 .tmp 文件
        with open(os.path.join(d, "good.wav"), "wb") as f:
            f.write(b"good")
        with open(os.path.join(d, "bad.wav.tmp"), "wb") as f:
            f.write(b"bad")
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "nested.tmp"), "wb") as f:
            f.write(b"nested bad")

        count = cleanup_tmp_files(d)
        assert count == 2
        assert os.path.exists(os.path.join(d, "good.wav"))
        assert not os.path.exists(os.path.join(d, "bad.wav.tmp"))
        assert not os.path.exists(os.path.join(d, "sub", "nested.tmp"))

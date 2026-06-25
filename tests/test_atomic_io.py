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


# ---------------------------------------------------------------------------
# TU-04 契约测试：升级后 atomic_write_json 的 Path / list / fsync / trailing_newline 语义
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402


def test_atomic_write_json_accepts_path_object():
    """Path 对象也能写入（以前只接受 str）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        atomic_write_json(p, {"k": 1})
        assert json.loads(p.read_text()) == {"k": 1}


def test_atomic_write_json_accepts_list():
    """list 也是合法 data（以前签名只接受 dict）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "arr.json"
        atomic_write_json(p, [1, 2, 3])
        assert json.loads(p.read_text()) == [1, 2, 3]


def test_atomic_write_json_fsync_false_still_writes():
    """fsync=False 仍然成功写入内容（跳 fsync 不影响功能）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fast.json"
        atomic_write_json(p, {"fast": True}, fsync=False)
        assert json.loads(p.read_text()) == {"fast": True}


def test_atomic_write_json_trailing_newline():
    """trailing_newline=True 时文件末尾有换行符。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nl.json"
        atomic_write_json(p, {"x": 1}, trailing_newline=True)
        raw = p.read_bytes()
        assert raw.endswith(b"\n"), f"期望末尾有 \\n，实际: {raw[-3:]!r}"


def test_atomic_write_json_no_trailing_newline_by_default():
    """默认不追加换行符。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "no_nl.json"
        atomic_write_json(p, {"x": 1})
        raw = p.read_bytes()
        assert not raw.rstrip(b" ").endswith(b"\n"), f"期望无末尾 \\n，实际: {raw[-5:]!r}"


def test_atomic_write_json_sort_keys_false_preserves_order():
    """sort_keys=False 保持插入顺序（迁移 editing_* 时依赖此语义）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "order.json"
        atomic_write_json(p, {"b": 1, "a": 2}, sort_keys=False)
        text = p.read_text()
        assert text.index('"b"') < text.index('"a"'), f"期望保持 b,a 顺序: {text!r}"


def test_atomic_write_json_no_tmp_residue_on_success():
    """成功写入后临时文件不残留。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "out.json"
        atomic_write_json(p, {"ok": True})
        tmps = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert tmps == [], f"残留临时文件: {tmps}"


def test_atomic_write_json_creates_nested_dirs():
    """自动创建父目录。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a" / "b" / "c.json"
        atomic_write_json(p, {"deep": True})
        assert p.exists()


def test_atomic_write_bytes_accepts_path_object():
    """atomic_write_bytes 也接受 Path 对象。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audio.wav"
        atomic_write_bytes(p, b"\x00\x01\x02")
        assert p.read_bytes() == b"\x00\x01\x02"

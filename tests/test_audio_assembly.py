"""Phase 4.2 A.2a 守卫：``gateway.audio_assembly.concat_segments_to_wav``
是 ``voice_selection_api._concat_segments_ffmpeg`` 抽公共后的唯一实现。

本测试集守护三件事：

1. **MiniMax 路径字节级不变**：调用时不传 ``target_sample_rate_hz`` →
   ffmpeg 命令、subprocess 调用参数、filter 表达式、输出路径、错误处理
   与原函数行为完全相同（24kHz / mono / PCM s16le / 60s timeout）
2. **CosyVoice 16kHz 参数化**：传 ``target_sample_rate_hz=16000`` →
   ffmpeg ``-ar 16000``，其余参数与默认一致
3. **抽公共 refactor 守卫**：``voice_selection_api`` 不再有本地
   ``_concat_segments_ffmpeg`` 函数定义；``gateway/`` 树内唯一定义该
   concat 逻辑的地方是 ``audio_assembly.py``（防 copy-paste 漂移）

测试设计：subprocess.run 用 monkey-patch 拦截，断言**实际**传给 ffmpeg
的 cmd list 字节级正确。不真跑 ffmpeg。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = REPO_ROOT / "gateway"

for p in (str(GATEWAY_PATH), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class _FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess. Lets tests control
    returncode + stderr without invoking real ffmpeg."""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr


def _make_temp_project(tmp_path: Path, source_filename: str = "src.wav") -> tuple[Path, Path]:
    """构造 ``project_dir`` + 源音频文件占位 (不需要真音频内容；subprocess
    被 mock，ffmpeg 不会真读)。返回 (project_dir, source_audio_path)。"""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    source = project_dir / source_filename
    source.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")  # fake WAV header
    return project_dir, source


SAMPLE_SEGMENTS = [
    {"start_ms": 0, "end_ms": 2500},
    {"start_ms": 3000, "end_ms": 5800},
]


# ---------------------------------------------------------------------
# MiniMax 路径字节级不变（A.2a 核心契约）
# ---------------------------------------------------------------------


def test_default_sample_rate_is_minimax_legacy_24khz(tmp_path):
    """默认 ``target_sample_rate_hz`` 必须是 24000，与原
    ``_concat_segments_ffmpeg`` 行为一致。MiniMax 路径不传参数时拿这个值。"""
    from audio_assembly import DEFAULT_TARGET_SAMPLE_RATE_HZ
    assert DEFAULT_TARGET_SAMPLE_RATE_HZ == 24000


def test_subprocess_timeout_is_60_seconds(tmp_path):
    """subprocess 超时必须是 60s，与原函数硬编码值一致。"""
    from audio_assembly import FFMPEG_SUBPROCESS_TIMEOUT_S
    assert FFMPEG_SUBPROCESS_TIMEOUT_S == 60


def test_minimax_default_call_produces_24khz_ffmpeg_cmd(tmp_path, monkeypatch):
    """**核心守卫**：MiniMax 路径调用（不传 sample_rate）→ ffmpeg cmd 含
    ``-ar 24000``，所有其它参数与原函数完全一致。

    若哪天本测试 red，说明 MiniMax 既有 clone 路径行为被破坏。
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return _FakeCompletedProcess(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("audio_assembly.subprocess.run", fake_run)

    result_path = concat_segments_to_wav(
        source, SAMPLE_SEGMENTS, project_dir, "spk_a"
    )

    cmd = captured["cmd"]
    # Header
    assert cmd[0] == "ffmpeg"
    assert cmd[1] == "-y"
    # Input
    assert cmd[2] == "-i"
    assert cmd[3] == str(source)
    # filter_complex 紧跟，下一个是 -map [out]
    assert cmd[4] == "-filter_complex"
    assert cmd[6] == "-map"
    assert cmd[7] == "[out]"
    # 编码 + 采样率（**核心**）
    assert cmd[8] == "-acodec"
    assert cmd[9] == "pcm_s16le"
    assert cmd[10] == "-ar"
    assert cmd[11] == "24000"  # MiniMax 历史值
    # mono
    assert cmd[12] == "-ac"
    assert cmd[13] == "1"
    # output path 最后
    assert cmd[14] == str(project_dir / "speaker_audio" / "spk_a" / "clone_sample.wav")

    # subprocess kwargs
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["timeout"] == 60

    # 返回 output path
    assert result_path == project_dir / "speaker_audio" / "spk_a" / "clone_sample.wav"


def test_minimax_default_filter_complex_byte_level_identical(tmp_path, monkeypatch):
    """filter_complex 表达式必须与原函数字节级一致：
    ``[0:a]atrim=start=X:end=Y,asetpts=PTS-STARTPTS[sN]; ... [s0][s1]concat=n=2:v=0:a=1[out]``
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: (captured.setdefault("cmd", list(cmd)),
                            _FakeCompletedProcess())[1],
    )

    concat_segments_to_wav(source, SAMPLE_SEGMENTS, project_dir, "spk_a")

    filter_complex = captured["cmd"][5]
    expected = (
        "[0:a]atrim=start=0.0:end=2.5,asetpts=PTS-STARTPTS[s0];"
        "[0:a]atrim=start=3.0:end=5.8,asetpts=PTS-STARTPTS[s1];"
        "[s0][s1]concat=n=2:v=0:a=1[out]"
    )
    assert filter_complex == expected, (
        f"filter_complex 字节级漂移：\n  expected: {expected!r}\n  actual:   {filter_complex!r}"
    )


# ---------------------------------------------------------------------
# CosyVoice 16kHz 参数化（A.2b 即将使用）
# ---------------------------------------------------------------------


def test_cosyvoice_16khz_explicit_sample_rate(tmp_path, monkeypatch):
    """显式传 ``target_sample_rate_hz=16000`` → ffmpeg ``-ar 16000``。
    其它所有参数（mono、pcm_s16le、timeout、cache 路径、filter 格式）
    保持与默认一致 —— 单一参数化，无其它分支。
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: (captured.setdefault("cmd", list(cmd)),
                            captured.setdefault("kwargs", dict(kw)),
                            _FakeCompletedProcess())[2],
    )

    concat_segments_to_wav(
        source, SAMPLE_SEGMENTS, project_dir, "spk_a",
        target_sample_rate_hz=16000,
    )

    cmd = captured["cmd"]
    # 与 MiniMax case 相同的位置，但 `-ar` 值改 16000
    assert cmd[10] == "-ar"
    assert cmd[11] == "16000"
    assert cmd[8] == "-acodec"
    assert cmd[9] == "pcm_s16le"
    assert cmd[12] == "-ac"
    assert cmd[13] == "1"
    # subprocess 超时仍是 60s
    assert captured["kwargs"]["timeout"] == 60


@pytest.mark.parametrize("sr", [8000, 16000, 22050, 24000, 44100, 48000])
def test_arbitrary_sample_rate_is_stringified_into_ar_arg(tmp_path, monkeypatch, sr):
    """无论传什么数值，``-ar`` 参数必须是该数值的 ``str()``。"""
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: (captured.setdefault("cmd", list(cmd)),
                            _FakeCompletedProcess())[1],
    )

    concat_segments_to_wav(
        source, SAMPLE_SEGMENTS, project_dir, "spk_a",
        target_sample_rate_hz=sr,
    )

    cmd = captured["cmd"]
    ar_idx = cmd.index("-ar")
    assert cmd[ar_idx + 1] == str(sr)


# ---------------------------------------------------------------------
# Cache 路径 / 安全 / 错误处理
# ---------------------------------------------------------------------


def test_cache_dir_layout_unchanged_from_legacy(tmp_path, monkeypatch):
    """Cache 目录布局必须是 ``{project_dir}/speaker_audio/{speaker_id}/``。
    输出文件名 ``clone_sample.wav``。与原函数一致，方便排障 + 复用既有
    cache 清理脚本。"""
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(),
    )

    result = concat_segments_to_wav(source, SAMPLE_SEGMENTS, project_dir, "spk_xyz")

    assert result == project_dir / "speaker_audio" / "spk_xyz" / "clone_sample.wav"
    # Cache 目录必须已被创建
    assert (project_dir / "speaker_audio" / "spk_xyz").is_dir()


def test_ffmpeg_failure_raises_runtime_error_with_stderr_excerpt(tmp_path, monkeypatch):
    """ffmpeg returncode != 0 时抛 RuntimeError，message 含 stderr 前 500 字符。
    与原函数错误处理一致。"""
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    err = b"Error: input file unrecognized format" * 100  # > 500 chars
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(returncode=1, stderr=err),
    )

    with pytest.raises(RuntimeError) as exc_info:
        concat_segments_to_wav(source, SAMPLE_SEGMENTS, project_dir, "spk_a")

    msg = str(exc_info.value)
    assert "ffmpeg concat failed:" in msg
    # 截断 500 字符
    err_in_msg = msg.split("ffmpeg concat failed: ", 1)[1]
    assert len(err_in_msg) <= 500


def test_invalid_stderr_bytes_are_replaced_not_raised(tmp_path, monkeypatch):
    """非 UTF-8 stderr 不能让函数自身崩；errors='replace' 兜底。"""
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    bad_bytes = b"\xff\xfe\xfd not utf-8"
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(returncode=2, stderr=bad_bytes),
    )

    with pytest.raises(RuntimeError):
        concat_segments_to_wav(source, SAMPLE_SEGMENTS, project_dir, "spk_a")


# ---------------------------------------------------------------------
# Refactor 守卫：MiniMax callers 不再持有本地副本
# ---------------------------------------------------------------------


def test_voice_selection_api_no_longer_defines_local_concat_function() -> None:
    """**Refactor 守卫**：``voice_selection_api.py`` 不能再有本地
    ``_concat_segments_ffmpeg`` 函数定义。已迁移到 ``audio_assembly.py``。
    若有人 cherry-pick 旧代码不小心带回来，本守卫立刻 red。
    """
    src_path = GATEWAY_PATH / "voice_selection_api.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    bad = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_concat_segments_ffmpeg"
    ]
    assert not bad, (
        f"voice_selection_api.py 不能再定义 `_concat_segments_ffmpeg` —— "
        f"已抽到 audio_assembly.py。重新出现的函数: {bad}"
    )


def test_voice_selection_api_imports_audio_assembly_helper() -> None:
    """``voice_selection_api.py`` 必须从 ``audio_assembly`` 导入新 helper。
    否则 caller 拿到的是 NameError。
    """
    src_path = GATEWAY_PATH / "voice_selection_api.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "audio_assembly":
            for alias in node.names:
                if alias.name == "concat_segments_to_wav":
                    found = True
                    break

    assert found, (
        "voice_selection_api.py 必须 `from audio_assembly import "
        "concat_segments_to_wav`"
    )


def test_only_audio_assembly_defines_concat_segments_to_wav() -> None:
    """**单一来源守卫**：``gateway/`` 树内 ``concat_segments_to_wav`` 函数
    只能在 ``audio_assembly.py`` 里定义。防 copy-paste 漂移。
    """
    offenders: list[str] = []
    for py_path in GATEWAY_PATH.rglob("*.py"):
        if "alembic/versions" in str(py_path).replace("\\", "/"):
            continue
        if py_path.name == "audio_assembly.py":
            continue
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "concat_segments_to_wav":
                offenders.append(f"{py_path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not offenders, (
        f"`concat_segments_to_wav` 只能在 gateway/audio_assembly.py 定义。"
        f"违规：{offenders}"
    )


# ---------------------------------------------------------------------
# Path-traversal safety — Codex 2026-05-26 A.2a review follow-up,
# tightened by A.2b: replaced startswith(resolve()) with Path.relative_to()
# ---------------------------------------------------------------------


@pytest.mark.parametrize("malicious_speaker_id", [
    # cache_dir = project_dir/speaker_audio/{spk_id}
    # 要真正逃出 project_dir，至少需要 2 个 ``..``（speaker_audio 占 1 层）
    "../../etc/passwd",          # 双 .. 真出去
    "../../../../etc/passwd",    # 多重 .. 更稳
    "..\\..\\windows\\system32", # Windows 反斜杠
    "/absolute/escape/path",     # Unix 绝对路径会重置 Path 拼接
])
def test_path_traversal_in_speaker_id_is_rejected(tmp_path, monkeypatch, malicious_speaker_id):
    """``speaker_id`` 含**真正**逃出 project_dir 的 traversal → ValueError，
    **不调** subprocess.

    A.2a 原实现 ``startswith(resolve())`` 边界 case 会漏；A.2b 改
    ``Path.relative_to()`` 后逃出 project_dir 的 case 全部明确 raise。

    注意：``speaker_id="../etc"`` 只逃了 1 层 ``speaker_audio``，落在
    ``project_dir/etc`` 仍是 project_dir 子路径 —— **不算越权**。要至少
    ``../../`` 才真出 project_dir。这是 ``Path.relative_to()`` 的正确语义。
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    subprocess_called = {"v": False}

    def fake_run(cmd, **kwargs):
        subprocess_called["v"] = True
        return _FakeCompletedProcess()

    monkeypatch.setattr("audio_assembly.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="路径验证失败"):
        concat_segments_to_wav(
            source, SAMPLE_SEGMENTS, project_dir, malicious_speaker_id,
        )

    assert not subprocess_called["v"], (
        f"speaker_id={malicious_speaker_id!r} 越权检查必须在 subprocess 之前 raise"
    )


def test_path_traversal_does_not_leave_orphan_directories(tmp_path, monkeypatch):
    """**关键回归（Codex PR #11 review #3）**：traversal 拒绝必须发生在
    ``mkdir`` 之前，否则恶意 ``speaker_id`` 已经在 project_dir 之外创建了
    目录后才报错，留下越权产物。

    本测试断言：
    1. ``project_dir.parent/sibling_evil/abc`` 在调用前**不存在**
    2. 抛 ValueError
    3. ``project_dir.parent/sibling_evil/abc`` 在调用后**仍不存在**
       （未被 mkdir 创建出来）
    4. sibling_evil 自己的预存 marker 不变（防 mkdir 误删除）
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(),
    )

    # Sibling dir outside project_dir — pre-create with marker file
    sibling = tmp_path / "sibling_evil"
    sibling.mkdir()
    sibling_marker = sibling / "untouched.marker"
    sibling_marker.write_text("untouched", encoding="utf-8")

    # 关键 assert：调用前越权路径不存在
    malicious_target = sibling / "abc"
    assert not malicious_target.exists()

    # speaker_id 真正越到 sibling（双 ``..`` 出 speaker_audio + 出
    # project_dir 2 层）— ``Path.relative_to`` 必抛 ValueError
    with pytest.raises(ValueError):
        concat_segments_to_wav(
            source, SAMPLE_SEGMENTS, project_dir, "../../sibling_evil/abc",
        )

    # 关键 assert：调用后**仍不存在**（mkdir 没在校验前执行）
    assert not malicious_target.exists(), (
        f"Path-traversal 校验必须在 mkdir 之前。"
        f"{malicious_target} 不该被创建出来。"
    )
    # sibling 自己的 marker 文件应该原封不动
    assert sibling_marker.read_text(encoding="utf-8") == "untouched"


def test_dot_speaker_id_rejected_to_avoid_root_collision(tmp_path, monkeypatch):
    """**Codex PR #11 review #3 补强**：``speaker_id="."`` 会让 cache_dir
    解析后等于 ``speaker_audio`` 根目录自身，破坏 per-speaker 隔离。
    新校验严限 cache_dir 严格在 ``speaker_audio`` 子树**之下**。
    """
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(),
    )

    with pytest.raises(ValueError, match="路径验证失败"):
        concat_segments_to_wav(source, SAMPLE_SEGMENTS, project_dir, ".")


def test_legitimate_speaker_id_passes(tmp_path, monkeypatch):
    """合法 speaker_id（无 traversal）正常通过路径校验。"""
    from audio_assembly import concat_segments_to_wav

    project_dir, source = _make_temp_project(tmp_path)
    monkeypatch.setattr(
        "audio_assembly.subprocess.run",
        lambda cmd, **kw: _FakeCompletedProcess(),
    )

    # 不应 raise
    result = concat_segments_to_wav(
        source, SAMPLE_SEGMENTS, project_dir, "speaker_abc_123",
    )
    assert result == project_dir / "speaker_audio" / "speaker_abc_123" / "clone_sample.wav"


def test_audio_assembly_module_does_not_import_minimax_or_cosyvoice() -> None:
    """**层级守卫**：``audio_assembly.py`` 是底层 helper，不能依赖任何
    provider-specific 模块（MiniMax 客户端、CosyVoice clone API 等）。
    """
    src_path = GATEWAY_PATH / "audio_assembly.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    forbidden_substrings = (
        "minimax",
        "cosyvoice_clone",
        "voice_selection_api",
        "user_voice_service",
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forb in forbidden_substrings:
                    if forb in alias.name.lower():
                        offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lower()
            for forb in forbidden_substrings:
                if forb in mod:
                    offenders.append(f"from {node.module} import ...")

    assert not offenders, (
        f"audio_assembly.py 不能依赖 provider-specific 模块（层级反向）。"
        f"违规：{offenders}"
    )

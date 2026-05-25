"""Phase 4.1 C 子模块：audio_processor 转码 / 裁剪测试。

策略：全程 monkeypatch ``subprocess.run``，**永不依赖真实 ffmpeg**。
"""
from __future__ import annotations

import subprocess

import pytest

from services.mainland_worker.silent_wav import generate_silent_wav

# gateway/ 在 conftest.py sys.path
from cosyvoice_clone.audio_processor import (  # type: ignore[import-not-found]
    AudioProcessingError,
    AudioProcessingTimeoutError,
    DEFAULT_TARGET_DURATION_S,
    SAFETY_OUTPUT_CEILING_BYTES,
    TARGET_CHANNELS,
    TARGET_CODEC,
    TARGET_SAMPLE_RATE_HZ,
    TARGET_CONTAINER,
    WORKER_HARD_LIMIT_BYTES,
    normalize_sample_for_dashscope,
)


class _CompletedProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_ffmpeg_mock(
    monkeypatch,
    *,
    stdout: bytes | None = None,
    returncode: int = 0,
    raise_timeout: bool = False,
    raise_file_not_found: bool = False,
):
    """注入 fake ``subprocess.run``。返回 ``calls`` 列表。"""
    calls: list[dict] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 30))
        if raise_file_not_found:
            raise FileNotFoundError("ffmpeg not in PATH")
        return _CompletedProcess(
            returncode=returncode,
            stdout=stdout if stdout is not None else generate_silent_wav(10_000),
        )

    monkeypatch.setattr(
        "cosyvoice_clone.audio_processor.subprocess.run",
        fake_run,
    )
    return calls


def _silent_wav_10s() -> bytes:
    return generate_silent_wav(10_000)


# ---------------------------------------------------------------------------
# 1. happy path：默认参数转码成功
# ---------------------------------------------------------------------------

def test_normalize_happy_path(monkeypatch) -> None:
    calls = _install_ffmpeg_mock(monkeypatch, stdout=_silent_wav_10s())
    raw = b"FAKE-MP3-DATA" * 1000  # 任意 input；ffmpeg 被 mock
    output = normalize_sample_for_dashscope(raw)
    assert output[:4] == b"RIFF"
    assert output[8:12] == b"WAVE"
    assert len(calls) == 1


def test_normalize_command_locks_target_format(monkeypatch) -> None:
    """ffmpeg 命令必须含 -ar 16000 / -ac 1 / -acodec pcm_s16le / -f wav。"""
    calls = _install_ffmpeg_mock(monkeypatch, stdout=_silent_wav_10s())
    normalize_sample_for_dashscope(b"data" * 500, target_duration_s=10)
    cmd = calls[0]["args"][0]
    # 关键参数全部在 cmd 中
    assert "-ar" in cmd and str(TARGET_SAMPLE_RATE_HZ) in cmd
    assert "-ac" in cmd and str(TARGET_CHANNELS) in cmd
    assert "-acodec" in cmd and TARGET_CODEC in cmd
    assert "-f" in cmd and TARGET_CONTAINER in cmd
    assert "-t" in cmd
    # 通过 stdin / stdout（pipe）
    assert "pipe:0" in cmd
    assert "pipe:1" in cmd


def test_normalize_target_duration_passed(monkeypatch) -> None:
    calls = _install_ffmpeg_mock(monkeypatch, stdout=_silent_wav_10s())
    normalize_sample_for_dashscope(b"data" * 500, target_duration_s=15)
    cmd = calls[0]["args"][0]
    idx = cmd.index("-t")
    assert cmd[idx + 1] == "15"


def test_normalize_default_duration_is_30s() -> None:
    """Codex 2026-05-25 决策对齐 DashScope ``max_prompt_audio_length=30s``。"""
    assert DEFAULT_TARGET_DURATION_S == 30


# ---------------------------------------------------------------------------
# 2. 输入校验
# ---------------------------------------------------------------------------

def test_normalize_rejects_empty_input(monkeypatch) -> None:
    """空 bytes 直接拒，不调用 ffmpeg。"""
    calls = _install_ffmpeg_mock(monkeypatch)
    with pytest.raises(ValueError, match="non-empty"):
        normalize_sample_for_dashscope(b"")
    assert calls == [], "空输入不应触发 ffmpeg"


def test_normalize_rejects_zero_duration(monkeypatch) -> None:
    calls = _install_ffmpeg_mock(monkeypatch)
    with pytest.raises(ValueError, match="positive"):
        normalize_sample_for_dashscope(b"data" * 500, target_duration_s=0)
    assert calls == []


def test_normalize_rejects_negative_duration(monkeypatch) -> None:
    calls = _install_ffmpeg_mock(monkeypatch)
    with pytest.raises(ValueError, match="positive"):
        normalize_sample_for_dashscope(b"data" * 500, target_duration_s=-5)
    assert calls == []


# ---------------------------------------------------------------------------
# 3. ffmpeg 错误路径
# ---------------------------------------------------------------------------

def test_normalize_ffmpeg_nonzero_exit_raises(monkeypatch) -> None:
    _install_ffmpeg_mock(monkeypatch, returncode=1, stdout=b"")
    with pytest.raises(AudioProcessingError) as exc:
        normalize_sample_for_dashscope(b"data" * 500)
    assert exc.value.code == "audio_processing_failed"


def test_normalize_ffmpeg_timeout_raises(monkeypatch) -> None:
    _install_ffmpeg_mock(monkeypatch, raise_timeout=True)
    with pytest.raises(AudioProcessingTimeoutError) as exc:
        normalize_sample_for_dashscope(b"data" * 500, timeout_s=0.1)
    assert exc.value.code == "audio_processing_timeout"


def test_normalize_ffmpeg_empty_output_raises(monkeypatch) -> None:
    """ffmpeg returncode=0 但 stdout 是空 bytes — 不正常。"""
    _install_ffmpeg_mock(monkeypatch, stdout=b"")
    with pytest.raises(AudioProcessingError) as exc:
        normalize_sample_for_dashscope(b"data" * 500)
    assert exc.value.code == "audio_processing_empty"


def test_normalize_oversize_output_raises(monkeypatch) -> None:
    """ffmpeg 返超 1 MB 时直接抛——明知 worker 端 HEAD 会拒，提前阻断。"""
    # 1.5 MB WAV stub（RIFF header + padding）
    base = _silent_wav_10s()
    oversize = base + b"\x00" * (1_500_000 - len(base))
    _install_ffmpeg_mock(monkeypatch, stdout=oversize)
    with pytest.raises(AudioProcessingError) as exc:
        normalize_sample_for_dashscope(b"data" * 500)
    assert exc.value.code == "audio_processing_oversized"


def test_normalize_output_not_wav_raises(monkeypatch) -> None:
    """ffmpeg 输出不是 RIFF/WAVE 头 — 转码出 bug。"""
    _install_ffmpeg_mock(monkeypatch, stdout=b"NOT-A-WAV-FILE" + b"\x00" * 1000)
    with pytest.raises(AudioProcessingError) as exc:
        normalize_sample_for_dashscope(b"data" * 500)
    assert exc.value.code == "audio_processing_invalid_output"


# ---------------------------------------------------------------------------
# 4. Safety ceiling warning（不抛错，让 worker 1 MB HEAD 兜底）
# ---------------------------------------------------------------------------

def test_normalize_logs_warning_when_above_safety_ceiling_but_below_1mb(
    monkeypatch, caplog,
) -> None:
    """转码后 size 在 980KB ~ 1MB 之间 → log warning 但不抛错。

    Codex 2026-05-25 决策：default 30s → 标称 ~960 KB；新 sanity ceiling 980 KB。
    """
    import logging

    base = _silent_wav_10s()
    # 990 KB（> 980 KB safety ceiling 但 < 1 MB）
    payload = base + b"\x00" * (990 * 1024 - len(base))
    _install_ffmpeg_mock(monkeypatch, stdout=payload)
    with caplog.at_level(logging.WARNING, logger="cosyvoice_clone.audio_processor"):
        output = normalize_sample_for_dashscope(b"data" * 500)
    assert len(output) > SAFETY_OUTPUT_CEILING_BYTES
    assert any("sanity ceiling" in r.getMessage() for r in caplog.records)


def test_normalize_silent_below_safety_ceiling_no_warning(
    monkeypatch, caplog,
) -> None:
    """转码后 < 980 KB → 不 warn。"""
    import logging

    _install_ffmpeg_mock(monkeypatch, stdout=_silent_wav_10s())  # 10s ~320 KB
    with caplog.at_level(logging.WARNING, logger="cosyvoice_clone.audio_processor"):
        normalize_sample_for_dashscope(b"data" * 500)
    # 不应有 sanity ceiling warning
    assert not any("sanity ceiling" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. 理论大小不变量（Codex 2026-05-25 四轮 finding）
# ---------------------------------------------------------------------------

def test_default_target_30s_pcm16_size_below_worker_hard_limit() -> None:
    """**关键不变量**：30 秒 / 16k / mono / PCM 16-bit / WAV 容器的理论
    输出大小必须 < ``WORKER_HARD_LIMIT_BYTES``（1 MB），且 <=
    ``SAFETY_OUTPUT_CEILING_BYTES``（980 KB）。

    这是 Codex 2026-05-25 四轮 P2：如果未来有人改 ``DEFAULT_TARGET_DURATION_S``
    / 采样率 / 声道，这个测试会立刻揭示"是否撞 1 MB 上限"。
    """
    # WAV header: 44 bytes (RIFF12 + fmt 16+8 + data 8)
    # PCM samples: sample_rate × channels × bytes_per_sample × duration
    bytes_per_sample = 2  # 16-bit
    pcm_bytes = (
        TARGET_SAMPLE_RATE_HZ
        * TARGET_CHANNELS
        * bytes_per_sample
        * DEFAULT_TARGET_DURATION_S
    )
    header_bytes = 44
    expected_total = header_bytes + pcm_bytes

    # 1. 理论值 < 1 MB worker 硬上限（防 DashScope InputDownloadFailed）
    assert expected_total < WORKER_HARD_LIMIT_BYTES, (
        f"30s PCM16 WAV 理论 {expected_total} bytes >= {WORKER_HARD_LIMIT_BYTES} "
        f"(1 MB) — 撞 DashScope 上限！检查 DEFAULT_TARGET_DURATION_S / "
        f"采样率 / 声道是否被改大"
    )
    # 2. 理论值 <= safety ceiling（应当不触发 sanity warning）
    assert expected_total <= SAFETY_OUTPUT_CEILING_BYTES, (
        f"30s PCM16 WAV 理论 {expected_total} bytes > sanity ceiling "
        f"{SAFETY_OUTPUT_CEILING_BYTES}; ceiling 应当略大于理论值"
    )
    # 3. Sanity ceiling 与 worker hard limit 的关系：留出 margin 但不太宽
    assert SAFETY_OUTPUT_CEILING_BYTES < WORKER_HARD_LIMIT_BYTES, (
        "sanity ceiling 必须严格小于 worker hard limit，否则 sanity 警告"
        "和 oversized error 会冲突"
    )


def test_default_constants_pcm16_mono_16k() -> None:
    """关键不变量锁定：默认参数必须是 mono / 16 kHz / PCM 16-bit。

    Codex 2026-05-25 四轮：如果有人改这三个参数，30s 文件大小会爆。
    """
    assert TARGET_SAMPLE_RATE_HZ == 16_000
    assert TARGET_CHANNELS == 1
    assert TARGET_CODEC == "pcm_s16le"
    assert TARGET_CONTAINER == "wav"

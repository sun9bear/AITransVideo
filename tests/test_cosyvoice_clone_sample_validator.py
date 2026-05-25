"""Phase 4.1 B：sample_validator 5 维硬校验测试。

测试策略：
- size + magic bytes 校验完全用 stdlib + ``services.mainland_worker.silent_wav``
  生成真实 WAV header
- ffprobe 路径全程 monkeypatch ``subprocess.run``，**永不依赖真实
  ffprobe 可执行文件**（AGENTS.md "tests prefer mocks over live external"）

覆盖：
- 5 维硬规则的 happy path + 各错误路径
- magic bytes：WAV / MP3 ID3 / MP3 frame sync / M4A / 未知格式
- ffprobe 子进程异常：timeout / 非零退出 / 非 JSON 输出 / 无 audio stream
- hints：偏短 / 偏长 / 正常时长
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from typing import Any

import pytest

from services.mainland_worker.silent_wav import generate_silent_wav

# gateway/ 已在 conftest.py sys.path
from cosyvoice_clone.sample_validator import (  # type: ignore[import-not-found]
    MAX_DURATION_MS,
    MAX_SAMPLE_BYTES,
    MIN_DURATION_MS,
    MIN_SAMPLE_BYTES,
    MIN_SAMPLE_RATE_HZ,
    RECOMMENDED_MAX_DURATION_MS,
    RECOMMENDED_MIN_DURATION_MS,
    ErrorCode,
    SampleValidationResult,
    _detect_format,
    validate_sample_bytes,
)


# ---------------------------------------------------------------------------
# Helpers: mock subprocess.run 模拟 ffprobe 输出
# ---------------------------------------------------------------------------

class _CompletedProcess:
    """``subprocess.CompletedProcess`` stand-in（避免构造真 Popen）。"""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_ffprobe_stdout(
    *,
    duration_s: float | None = 15.0,
    sample_rate_hz: int | None = 16000,
    channels: int | None = 1,
    codec_name: str | None = "pcm_s16le",
    bits_per_sample: int | None = 16,
    has_audio_stream: bool = True,
) -> bytes:
    """生成符合 sample_validator 期望解析的 ffprobe JSON 输出。

    默认值反映 plan §样本硬性校验规则 的 WAV happy path（PCM 16-bit 16k mono）。
    单 case 可以覆盖任意字段测试错误路径。
    """
    streams: list[dict] = []
    if has_audio_stream:
        stream = {"codec_type": "audio"}
        if codec_name is not None:
            stream["codec_name"] = codec_name
        if bits_per_sample is not None:
            stream["bits_per_sample"] = bits_per_sample
        if sample_rate_hz is not None:
            stream["sample_rate"] = str(sample_rate_hz)
        if channels is not None:
            stream["channels"] = channels
        if duration_s is not None:
            stream["duration"] = str(duration_s)
        streams.append(stream)

    payload = {
        "streams": streams,
        "format": {
            "duration": str(duration_s) if duration_s is not None else "0",
        },
    }
    return json.dumps(payload).encode("utf-8")


def _install_ffprobe_mock(
    monkeypatch,
    *,
    stdout: bytes | None = None,
    returncode: int = 0,
    raise_timeout: bool = False,
    raise_file_not_found: bool = False,
):
    """注入 fake subprocess.run。返回调用记录列表（便于断言 cmd）。"""
    calls: list[dict] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 10))
        if raise_file_not_found:
            raise FileNotFoundError("ffprobe not in PATH")
        return _CompletedProcess(
            returncode=returncode,
            stdout=stdout if stdout is not None else _make_ffprobe_stdout(),
        )

    monkeypatch.setattr(
        "cosyvoice_clone.sample_validator.subprocess.run",
        fake_run,
    )
    return calls


# ---------------------------------------------------------------------------
# 1. size 校验
# ---------------------------------------------------------------------------

def test_empty_sample_rejected(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch)  # 不应被调用
    result = validate_sample_bytes(b"")
    assert result.is_valid is False
    assert result.error_code == ErrorCode.EMPTY
    assert result.size_bytes == 0


def test_sample_too_small_rejected(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch)
    result = validate_sample_bytes(b"x" * (MIN_SAMPLE_BYTES - 1))
    assert result.is_valid is False
    assert result.error_code == ErrorCode.SIZE_TOO_SMALL


def test_sample_too_large_rejected(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch)
    # 不实际生成 11 MB；用一个合法 WAV header + 拼接到 10 MB + 1
    base = generate_silent_wav(1000)
    data = base + b"\x00" * (MAX_SAMPLE_BYTES + 1 - len(base))
    result = validate_sample_bytes(data)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.SIZE_TOO_LARGE
    assert result.size_bytes > MAX_SAMPLE_BYTES


# ---------------------------------------------------------------------------
# 2. Magic bytes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data, expected_format", [
    pytest.param(generate_silent_wav(2000), "wav", id="wav-riff"),
    pytest.param(
        b"\x00\x00\x00\x18" + b"ftyp" + b"isom" + b"\x00" * 16,
        "m4a", id="m4a-ftyp",
    ),
    pytest.param(b"ID3\x04\x00\x00" + b"\x00" * 100, "mp3", id="mp3-id3"),
    pytest.param(b"\xff\xfb\x90\x00" + b"\x00" * 100, "mp3", id="mp3-frame-sync-fb"),
    pytest.param(b"\xff\xf3\x80\x00" + b"\x00" * 100, "mp3", id="mp3-frame-sync-f3"),
])
def test_magic_bytes_detection(data: bytes, expected_format: str) -> None:
    assert _detect_format(data) == expected_format


@pytest.mark.parametrize("data", [
    pytest.param(b"\x00" * 32, id="all-zero"),
    pytest.param(b"OggS" + b"\x00" * 30, id="ogg-header"),
    pytest.param(b"fLaC" + b"\x00" * 30, id="flac-header"),
    pytest.param(b"too_short", id="too-short-9bytes"),
])
def test_unknown_format_rejected(monkeypatch, data: bytes) -> None:
    _install_ffprobe_mock(monkeypatch)
    # 大小到 MIN 之上，避免被 size 拦截
    padded = data + b"\x00" * (MIN_SAMPLE_BYTES - len(data))
    result = validate_sample_bytes(padded)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.FORMAT_UNSUPPORTED


# ---------------------------------------------------------------------------
# 3. ffprobe metadata —— happy path
# ---------------------------------------------------------------------------

def test_valid_wav_15s_16khz_pcm16_passes(monkeypatch) -> None:
    calls = _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(
            duration_s=15.0, sample_rate_hz=16000, channels=1,
            codec_name="pcm_s16le", bits_per_sample=16,
        ),
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert result.error_code is None
    assert result.detected_format == "wav"
    assert result.duration_ms == 15_000
    assert result.sample_rate_hz == 16_000
    assert result.channels == 1
    assert result.codec_name == "pcm_s16le"
    assert result.bits_per_sample == 16
    assert len(calls) == 1  # ffprobe 调一次


def test_valid_mp3_passes(monkeypatch) -> None:
    # MP3 容器：codec_name 是 "mp3"，但 WAV-PCM-16 校验不影响它
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(
            duration_s=12.5, sample_rate_hz=44100, channels=2,
            codec_name="mp3", bits_per_sample=None,  # MP3 没 bits_per_sample
        ),
    )
    mp3_stub = b"ID3\x04\x00\x00" + b"\x00" * MIN_SAMPLE_BYTES
    result = validate_sample_bytes(mp3_stub)
    assert result.is_valid is True
    assert result.detected_format == "mp3"
    assert result.codec_name == "mp3"


def test_valid_m4a_aac_passes(monkeypatch) -> None:
    """M4A 容器 + AAC codec：WAV-PCM-16 校验不应阻塞非 WAV 容器。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(
            duration_s=18.0, sample_rate_hz=48000, channels=2,
            codec_name="aac", bits_per_sample=None,
        ),
    )
    m4a_stub = b"\x00\x00\x00\x18" + b"ftyp" + b"isom" + b"\x00" * MIN_SAMPLE_BYTES
    result = validate_sample_bytes(m4a_stub)
    assert result.is_valid is True
    assert result.detected_format == "m4a"
    assert result.codec_name == "aac"


def test_valid_wav_pcm_s16be_passes(monkeypatch) -> None:
    """big-endian PCM 16-bit 也接受（WAV BE 罕见但合法）。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(
            codec_name="pcm_s16be", bits_per_sample=16,
        ),
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert result.codec_name == "pcm_s16be"


# ---------------------------------------------------------------------------
# 3.5 WAV codec 校验（Codex 2026-05-25 三轮 P1 finding）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec_name, bits_per_sample", [
    pytest.param("pcm_s24le", 24, id="pcm-24bit-le"),
    pytest.param("pcm_s32le", 32, id="pcm-32bit-le"),
    pytest.param("pcm_f32le", 32, id="pcm-float32-le"),
    pytest.param("pcm_f64le", 64, id="pcm-float64-le"),
    pytest.param("adpcm_ima_wav", 4, id="adpcm-ima"),
    pytest.param("adpcm_ms", 4, id="adpcm-ms"),
    pytest.param("pcm_alaw", 8, id="a-law"),
    pytest.param("pcm_mulaw", 8, id="mu-law"),
])
def test_wav_non_pcm16_rejected(monkeypatch, codec_name: str, bits_per_sample: int) -> None:
    """plan §样本硬性校验规则 写 'WAV (PCM 16-bit)'；其它 WAV 编码全部拒。

    防 DashScope clone 在 24-bit / float / ADPCM 上失败浪费付费调用。
    """
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(
            codec_name=codec_name, bits_per_sample=bits_per_sample,
        ),
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.WAV_ENCODING_UNSUPPORTED
    assert result.codec_name == codec_name
    assert result.bits_per_sample == bits_per_sample


def test_wav_missing_codec_name_rejected(monkeypatch) -> None:
    """ffprobe 没返 codec_name 时也拒（保守路径）。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(codec_name=None, bits_per_sample=None),
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.WAV_ENCODING_UNSUPPORTED


def test_wav_codec_check_does_not_block_mp3_m4a(monkeypatch) -> None:
    """WAV 容器才需要 PCM 16-bit；MP3 / M4A 走自己的 codec（mp3 / aac）。"""
    # MP3：codec_name="mp3"（不在 _WAV_PCM16_CODECS 白名单），但容器不是 WAV → 不拒
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(codec_name="mp3", bits_per_sample=None),
    )
    mp3 = b"ID3\x04\x00\x00" + b"\x00" * MIN_SAMPLE_BYTES
    result = validate_sample_bytes(mp3)
    assert result.is_valid is True
    assert result.error_code is None


# ---------------------------------------------------------------------------
# 4. duration 边界
# ---------------------------------------------------------------------------

def test_duration_too_long_rejected(monkeypatch) -> None:
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=60.5),  # 60.5s > 60s
    )
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DURATION_TOO_LONG
    assert result.duration_ms == 60_500


def test_duration_too_short_rejected(monkeypatch) -> None:
    """Codex 2026-05-25 决策：MIN 改 3 秒。2.5 秒应当被拒。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=2.5),  # 2500ms < 3000ms
    )
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DURATION_TOO_SHORT
    assert result.duration_ms == 2500
    # Codex 四轮 small fix：错误消息必须动态反映 MIN_DURATION_MS=3 秒
    # （之前残留 "(1s)" 与新阈值不一致）
    assert f"({MIN_DURATION_MS // 1000}s)" in result.error_message, (
        f"error_message 应含动态秒数 ({MIN_DURATION_MS // 1000}s)，"
        f"实际：{result.error_message!r}"
    )


def test_duration_too_long_error_message_uses_dynamic_seconds(monkeypatch) -> None:
    """同样守住 too_long 的动态秒数（防 MAX_DURATION_MS 改后文案不同步）。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=120.0),  # 远超 60s
    )
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert f"({MAX_DURATION_MS // 1000}s)" in result.error_message


def test_duration_at_boundaries(monkeypatch) -> None:
    """``duration_ms = MIN_DURATION_MS=3000`` 通过；``= MAX_DURATION_MS=60000`` 也通过。"""
    for duration_s, should_pass in [
        (3.0, True),       # = MIN_DURATION_MS
        (60.0, True),      # = MAX_DURATION_MS
        (2.999, False),    # < MIN
        (60.001, False),   # > MAX
    ]:
        _install_ffprobe_mock(
            monkeypatch,
            stdout=_make_ffprobe_stdout(duration_s=duration_s),
        )
        wav = generate_silent_wav(2000)
        result = validate_sample_bytes(wav)
        if should_pass:
            assert result.is_valid is True, f"duration_s={duration_s} should pass"
        else:
            assert result.is_valid is False, f"duration_s={duration_s} should fail"


def test_short_sample_3_to_10s_passes_with_warning_hint(monkeypatch) -> None:
    """3-10 秒区间允许通过，但带"相似度可能下降"warning hint
    （Codex 2026-05-25 决策对齐 DashScope max_prompt_audio_length）。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=5.0),  # 5 秒，3-10 区间
    )
    wav = generate_silent_wav(5000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert result.duration_ms == 5000
    # 含强警告
    assert any(
        "偏短" in h and "相似度" in h and "明显下降" in h
        for h in result.hints
    ), f"3-10 秒区间应该附强警告 hint，实际：{result.hints!r}"


# ---------------------------------------------------------------------------
# 5. sample rate 边界
# ---------------------------------------------------------------------------

def test_sample_rate_too_low_rejected(monkeypatch) -> None:
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(sample_rate_hz=8000),  # 8 kHz < 16 kHz
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.SAMPLE_RATE_TOO_LOW


def test_sample_rate_at_min_passes(monkeypatch) -> None:
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(sample_rate_hz=MIN_SAMPLE_RATE_HZ),
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# 6. ffprobe 错误路径
# ---------------------------------------------------------------------------

def test_ffprobe_timeout_returns_probe_timeout(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch, raise_timeout=True)
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav, ffprobe_timeout_s=0.1)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.PROBE_TIMEOUT


def test_ffprobe_nonzero_exit_decode_failed(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch, returncode=1, stdout=b"")
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DECODE_FAILED


def test_ffprobe_invalid_json_decode_failed(monkeypatch) -> None:
    _install_ffprobe_mock(monkeypatch, stdout=b"not-json-at-all")
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DECODE_FAILED


def test_ffprobe_no_audio_stream_decode_failed(monkeypatch) -> None:
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(has_audio_stream=False),
    )
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DECODE_FAILED


def test_ffprobe_zero_duration_decode_failed(monkeypatch) -> None:
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=0.0),
    )
    wav = generate_silent_wav(2000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is False
    assert result.error_code == ErrorCode.DECODE_FAILED


# ---------------------------------------------------------------------------
# 7. Hints（非阻断性提示）
# ---------------------------------------------------------------------------

def test_hint_appears_when_duration_too_short(monkeypatch) -> None:
    """5 秒（< RECOMMENDED_MIN 10 秒）会附带 hint，但仍 is_valid=True。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=5.0),
    )
    wav = generate_silent_wav(5000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert any("偏短" in h for h in result.hints)


def test_hint_appears_when_duration_too_long(monkeypatch) -> None:
    """25 秒（> RECOMMENDED_MAX 20 秒）附带 hint，提醒系统自动裁剪到 30 秒。

    Codex 2026-05-25 决策：DashScope max_prompt_audio_length=30s，所以
    20-60 秒的提示改成"裁剪至最多 30 秒"。
    """
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=25.0),
    )
    wav = generate_silent_wav(25_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert any("偏长" in h for h in result.hints)
    assert any("30" in h and "秒" in h for h in result.hints), (
        f"应当提示裁剪到 30 秒；实际 hints: {result.hints!r}"
    )


def test_hint_always_includes_content_quality_reminder(monkeypatch) -> None:
    """无论时长，hints 一定包含"本人朗读 / 避免背景音乐"提示。"""
    _install_ffprobe_mock(
        monkeypatch,
        stdout=_make_ffprobe_stdout(duration_s=15.0),  # 在推荐区间内
    )
    wav = generate_silent_wav(15_000)
    result = validate_sample_bytes(wav)
    assert result.is_valid is True
    assert any("本人" in h or "朗读" in h or "背景音乐" in h for h in result.hints)


# ---------------------------------------------------------------------------
# 8. Validation order: 廉价校验先做，ffprobe 后做
# ---------------------------------------------------------------------------

def test_size_check_runs_before_ffprobe(monkeypatch) -> None:
    """size_too_large 时不应调用 ffprobe（避免在已知失败时浪费 subprocess）。"""
    calls = _install_ffprobe_mock(monkeypatch)
    # 11 MB 数据（合法 WAV header + padding）
    base = generate_silent_wav(1000)
    data = base + b"\x00" * (MAX_SAMPLE_BYTES + 1 - len(base))
    validate_sample_bytes(data)
    assert calls == [], "size 校验失败时不该调用 ffprobe"


def test_format_check_runs_before_ffprobe(monkeypatch) -> None:
    """未知格式时不调 ffprobe。"""
    calls = _install_ffprobe_mock(monkeypatch)
    data = b"\x00" * MIN_SAMPLE_BYTES  # 大小过关，但 magic bytes 不匹配
    validate_sample_bytes(data)
    assert calls == [], "格式校验失败时不该调用 ffprobe"


def test_ffprobe_command_uses_pipe_stdin(monkeypatch) -> None:
    """通过 stdin 喂数据（``-i pipe:0``），不写临时文件。"""
    calls = _install_ffprobe_mock(monkeypatch)
    wav = generate_silent_wav(2000)
    validate_sample_bytes(wav)
    assert len(calls) == 1
    cmd_args = calls[0]["args"][0]
    assert "-i" in cmd_args
    assert "pipe:0" in cmd_args
    assert calls[0]["kwargs"]["input"] == wav


# ---------------------------------------------------------------------------
# 9. SampleValidationResult is frozen dataclass
# ---------------------------------------------------------------------------

def test_result_is_frozen() -> None:
    r = SampleValidationResult(is_valid=True, size_bytes=1234)
    with pytest.raises(AttributeError):
        r.is_valid = False  # type: ignore[misc]


def test_result_serializable_to_dict() -> None:
    """asdict() 应该成功（用于 API response / audit）。"""
    r = SampleValidationResult(
        is_valid=True,
        detected_format="wav",
        duration_ms=15000,
        sample_rate_hz=16000,
        channels=1,
        size_bytes=480000,
    )
    d = asdict(r)
    assert d["is_valid"] is True
    assert d["detected_format"] == "wav"


# ---------------------------------------------------------------------------
# 10. 防回退：硬性常量值锁定
# ---------------------------------------------------------------------------

def test_hard_limit_constants_match_plan() -> None:
    """plan §样本硬性校验规则 + Codex 2026-05-25 决策对齐锁定值，防误改。"""
    assert MAX_SAMPLE_BYTES == 10 * 1024 * 1024  # 10 MB
    assert MAX_DURATION_MS == 60_000              # 60 秒
    assert MIN_DURATION_MS == 3_000               # 3 秒（Codex 2026-05-25 决策）
    assert MIN_SAMPLE_RATE_HZ == 16_000           # 16 kHz
    assert RECOMMENDED_MIN_DURATION_MS == 10_000  # 推荐 10 秒
    assert RECOMMENDED_MAX_DURATION_MS == 20_000  # 推荐 20 秒

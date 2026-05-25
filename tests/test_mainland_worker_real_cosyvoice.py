"""Phase 2 RealCosyvoiceProvider 单元测试。

策略：用 monkeypatch 替换 ``sys.modules`` 中的 ``dashscope`` /
``dashscope.audio.tts_v2``，让 RealCosyvoiceProvider 的 lazy import 拿到
fake module。**永不真实联网**（AGENTS.md 红线）。

覆盖：
1. __init__ 空 api_key 抛 ValueError
2. clone 成功路径（HEAD OK → create_voice → query_voice OK）
3. clone 样本过大被拒（HEAD content-length > 1MB）
4. clone HEAD 网络错误
5. clone HEAD 返回 4xx
6. clone create_voice 抛 → ProviderError(create_voice_failed)
7. clone query_voice 轮询多次后 OK
8. clone query_voice 永远不 OK → ProviderError(query_voice_timeout)
9. clone consent 在 _build_clone_request 已经校验，provider 这层不重复
10. synthesize_segment 成功 + 返 silent WAV bytes
11. synthesize_segment 空文本 → ProviderError(empty_text)
12. synthesize_segment SDK 抛 → ProviderError(synthesize_failed)
13. synthesize_segment 返非 bytes → ProviderError(synthesize_empty)
14. synthesize_segment 返 0 时长 → ProviderError(zero_duration_audio)
15. synthesize_segment 完成后 dashscope.api_key 被恢复
16. delete_voice 成功
17. delete_voice 抛 → ProviderError(delete_voice_failed)
18. _retryable_keywords 真值表
19. _is_voice_ready 各种 status 形态
20. _sanitize_prefix
21. app.create_app: WORKER_MODE=live 且 DASHSCOPE_API_KEY 配齐 → 挂 RealCosyvoiceProvider
22. app.create_app: WORKER_MODE=live 但缺 DASHSCOPE_API_KEY → RuntimeError
23. app.create_app: WORKER_MODE 未知 → RuntimeError
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from services.mainland_worker.silent_wav import generate_silent_wav
from services.mainland_worker.types import (
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerSegmentRequest,
)
from services.mainland_worker.worker.providers.base import ProviderError


# ---------------------------------------------------------------------------
# Fake DashScope SDK 工厂
# ---------------------------------------------------------------------------

def _install_fake_dashscope(
    monkeypatch,
    *,
    create_voice_result: str | Exception = "mock_voice_xyz",
    query_voice_sequence: list[Any] | None = None,
    synthesize_result: bytes | Exception | None = None,
    delete_voice_raises: Exception | None = None,
) -> dict[str, Any]:
    """注入 fake ``dashscope`` / ``dashscope.audio.tts_v2`` 到 sys.modules。

    返回一个调用记录 dict 供测试断言用。注意 ``RealCosyvoiceProvider`` 在
    方法体内 lazy import，所以这里只要在调用前注入完成即可。
    """
    calls: dict[str, list] = {
        "create_voice": [],
        "query_voice": [],
        "synthesize_init": [],
        "synthesize_call": [],
        "delete_voice": [],
    }

    class _FakeVoiceEnrollmentService:
        def __init__(self, api_key=None, **kwargs):
            self.api_key = api_key

        def create_voice(self, **kw):
            calls["create_voice"].append(kw)
            if isinstance(create_voice_result, Exception):
                raise create_voice_result
            return create_voice_result

        def query_voice(self, voice_id):
            calls["query_voice"].append(voice_id)
            seq = query_voice_sequence or ["{'status': 'OK'}"]
            idx = min(len(seq) - 1, len(calls["query_voice"]) - 1)
            value = seq[idx]
            if isinstance(value, Exception):
                raise value
            return value

        def delete_voice(self, voice_id):
            calls["delete_voice"].append(voice_id)
            if delete_voice_raises:
                raise delete_voice_raises

    class _FakeSpeechSynthesizer:
        def __init__(self, **kw):
            calls["synthesize_init"].append(kw)
            self.ws = None

        def call(self, text):
            calls["synthesize_call"].append(text)
            if isinstance(synthesize_result, Exception):
                raise synthesize_result
            return synthesize_result if synthesize_result is not None else generate_silent_wav(1500)

        def close(self):
            pass

    class _FakeAudioFormat:
        WAV_16000HZ_MONO_16BIT = "WAV_16000HZ_MONO_16BIT"

    # ``dashscope.audio.tts_v2`` 子模块
    tts_v2_mod = types.ModuleType("dashscope.audio.tts_v2")
    tts_v2_mod.VoiceEnrollmentService = _FakeVoiceEnrollmentService
    tts_v2_mod.SpeechSynthesizer = _FakeSpeechSynthesizer
    tts_v2_mod.AudioFormat = _FakeAudioFormat

    # ``dashscope`` 顶层（保留 api_key 可写）
    dashscope_mod = types.ModuleType("dashscope")
    dashscope_mod.api_key = None
    dashscope_mod.audio = types.ModuleType("dashscope.audio")
    dashscope_mod.audio.tts_v2 = tts_v2_mod

    monkeypatch.setitem(sys.modules, "dashscope", dashscope_mod)
    monkeypatch.setitem(sys.modules, "dashscope.audio", dashscope_mod.audio)
    monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts_v2_mod)

    return calls


def _install_fake_httpx_head(
    monkeypatch,
    *,
    content_length: int = 500_000,
    status_code: int = 200,
    raise_exc: Exception | None = None,
) -> None:
    """让 httpx.Client.head 返回伪造响应（不打真实 HEAD 请求）。"""
    import httpx

    class _FakeResp:
        def __init__(self, status_code, content_length):
            self.status_code = status_code
            self.headers = {"content-length": str(content_length)}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def head(self, url):
            if raise_exc:
                raise raise_exc
            return _FakeResp(status_code, content_length)

    monkeypatch.setattr(httpx, "Client", _FakeClient)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_clone_req(speaker_id: str = "speaker_a") -> WorkerCloneRequest:
    return WorkerCloneRequest(
        job_id="j",
        user_id="u",
        speaker_id=speaker_id,
        speaker_name=speaker_id,
        target_model="cosyvoice-v3.5-flash",
        sample=WorkerCloneSample(
            kind="download_url",
            url="https://example.com/sample.wav",
            sha256="a" * 64,
        ),
        source_segments=(1,),
        consent=WorkerCloneConsent(
            voice_clone_confirmed=True,
            confirmed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def _make_segment(text: str = "你好世界", voice_id: str = "v1") -> WorkerSegmentRequest:
    from services.mainland_worker.types import compute_text_hash
    return WorkerSegmentRequest(
        segment_id=1,
        speaker_id="a",
        voice_id=voice_id,
        text=text,
        speech_rate=1.0,
        text_hash=compute_text_hash(text),
    )


def _provider(**kwargs):
    """RealCosyvoiceProvider with 小 poll interval 让测试快"""
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider
    defaults = {
        "api_key": "test-dashscope-key",
        "query_poll_interval_s": 0.0,
        "query_max_polls": 5,
    }
    defaults.update(kwargs)
    return RealCosyvoiceProvider(**defaults)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

def test_init_rejects_empty_api_key() -> None:
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider
    with pytest.raises(ValueError, match="non-empty api_key"):
        RealCosyvoiceProvider(api_key="")


def test_init_with_api_key_ok() -> None:
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider
    p = RealCosyvoiceProvider(api_key="x")
    assert p._api_key == "x"


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def test_clone_happy_path(monkeypatch) -> None:
    calls = _install_fake_dashscope(monkeypatch)
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider()
    outcome = p.clone(_make_clone_req())
    # Phase 4.0b: clone 返 CloneOutcome dataclass，不再是裸 voice_id 字符串
    assert outcome.voice_id == "mock_voice_xyz"
    # mock 模式 fake SDK 不暴露 get_last_request_id → None
    assert outcome.provider_request_id is None
    assert len(calls["create_voice"]) == 1
    assert calls["create_voice"][0]["target_model"] == "cosyvoice-v3.5-flash"
    assert calls["create_voice"][0]["url"] == "https://example.com/sample.wav"
    assert calls["create_voice"][0]["language_hints"] == ["zh"]
    # Codex 2026-05-25 决策：显式传 max_prompt_audio_length=30.0
    assert calls["create_voice"][0]["max_prompt_audio_length"] == 30.0
    assert len(calls["query_voice"]) == 1


def test_clone_passes_max_prompt_audio_length_to_dashscope(monkeypatch) -> None:
    """Codex 2026-05-25 决策：必须显式传 max_prompt_audio_length=30.0
    覆盖 DashScope 官方默认 10s，让相似度更高。
    """
    calls = _install_fake_dashscope(monkeypatch)
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    # 自定义 25.0
    p = _provider(max_prompt_audio_length_s=25.0)
    p.clone(_make_clone_req())
    assert calls["create_voice"][0]["max_prompt_audio_length"] == 25.0


def test_clone_rejects_sample_too_large(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch)
    _install_fake_httpx_head(monkeypatch, content_length=2 * 1024 * 1024)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "sample_too_large"


def test_clone_rejects_head_network_error(monkeypatch) -> None:
    import httpx
    _install_fake_dashscope(monkeypatch)
    _install_fake_httpx_head(monkeypatch, raise_exc=httpx.ConnectError("DNS fail"))
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "sample_head_failed"


def test_clone_rejects_head_4xx(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch)
    _install_fake_httpx_head(monkeypatch, status_code=403, content_length=100)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "sample_url_unreachable"


def test_clone_create_voice_raises_maps_to_provider_error(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, create_voice_result=RuntimeError("BadRequest.InputDownloadFailed"))
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "create_voice_failed"
    # InputDownloadFailed 不是 retryable 关键字 → retryable=False
    assert exc.value.retryable is False


def test_clone_create_voice_5xx_marked_retryable(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, create_voice_result=RuntimeError("503 service unavailable"))
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.retryable is True


def test_clone_query_polls_until_ok(monkeypatch) -> None:
    sequence = [
        {"status": "DEPLOYING"},
        "DEPLOYING",
        {"status": "OK"},  # 第三次轮询 OK
    ]
    calls = _install_fake_dashscope(
        monkeypatch,
        query_voice_sequence=sequence,
    )
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider()
    outcome = p.clone(_make_clone_req())
    assert outcome.voice_id == "mock_voice_xyz"
    assert len(calls["query_voice"]) == 3


def test_clone_query_timeout(monkeypatch) -> None:
    _install_fake_dashscope(
        monkeypatch,
        query_voice_sequence=["DEPLOYING"] * 10,
    )
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider(query_max_polls=3)
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "query_voice_timeout"


def test_clone_create_voice_returns_empty_voice_id(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, create_voice_result="")
    _install_fake_httpx_head(monkeypatch, content_length=500_000)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.clone(_make_clone_req())
    assert exc.value.code == "create_voice_empty"


# ---------------------------------------------------------------------------
# Synthesize segment
# ---------------------------------------------------------------------------

def test_synthesize_happy_path(monkeypatch) -> None:
    calls = _install_fake_dashscope(monkeypatch)
    p = _provider()
    text = "你好世界，测试合成"
    outcome = p.synthesize_segment(
        _make_segment(text),
        target_model="cosyvoice-v3.5-flash",
    )
    # Phase 4.0b: synthesize_segment 返 SegmentSynthesisOutcome dataclass
    assert outcome.audio_bytes[:4] == b"RIFF"
    assert outcome.duration_ms >= 1000
    # Phase 4.0b §B: billed_chars 用 billing_character_count（CJK = 2）
    # "你好世界，测试合成" = 8 CJK + 1 中文标点 → 8*2 + 1 = 17
    from services.mainland_worker.billing_chars import billing_character_count
    assert outcome.billed_chars == billing_character_count(text)
    assert outcome.billed_chars > len(text)  # 中文必然大于字符长度
    # mock 模式 fake SDK 不暴露 get_last_request_id → None
    assert outcome.provider_request_id is None
    assert len(calls["synthesize_init"]) == 1
    assert calls["synthesize_init"][0]["model"] == "cosyvoice-v3.5-flash"
    assert calls["synthesize_init"][0]["voice"] == "v1"


def test_synthesize_empty_text_rejected(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.synthesize_segment(_make_segment(""), target_model="cosyvoice-v3.5-flash")
    assert exc.value.code == "empty_text"


def test_synthesize_sdk_raises(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, synthesize_result=RuntimeError("InvalidParameter"))
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.synthesize_segment(_make_segment("a"), target_model="cosyvoice-v3.5-flash")
    assert exc.value.code == "synthesize_failed"


def test_synthesize_returns_non_bytes(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, synthesize_result="")  # 空字符串触发 empty check
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.synthesize_segment(_make_segment("a"), target_model="cosyvoice-v3.5-flash")
    assert exc.value.code == "synthesize_empty"


def test_synthesize_returns_zero_duration(monkeypatch) -> None:
    # 注入一个最小合法 WAV header 但 0 frames
    _install_fake_dashscope(monkeypatch, synthesize_result=generate_silent_wav(0))
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.synthesize_segment(_make_segment("a"), target_model="cosyvoice-v3.5-flash")
    assert exc.value.code == "zero_duration_audio"


def test_synthesize_restores_module_api_key(monkeypatch) -> None:
    """关键：synthesize 完成后 dashscope.api_key 必须恢复原值。

    多 provider 共存场景下，临时改全局 api_key 不能污染其他模块。
    """
    _install_fake_dashscope(monkeypatch)
    import dashscope
    dashscope.api_key = "original-other-key"

    p = _provider(api_key="real-cosy-key")
    p.synthesize_segment(_make_segment("a"), target_model="cosyvoice-v3.5-flash")

    assert dashscope.api_key == "original-other-key"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_voice_success(monkeypatch) -> None:
    calls = _install_fake_dashscope(monkeypatch)
    p = _provider()
    p.delete_voice("v1")
    assert calls["delete_voice"] == ["v1"]


def test_delete_voice_empty_rejected(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch)
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.delete_voice("")
    assert exc.value.code == "invalid_input"


def test_delete_voice_sdk_raises(monkeypatch) -> None:
    _install_fake_dashscope(monkeypatch, delete_voice_raises=RuntimeError("NotFound"))
    p = _provider()
    with pytest.raises(ProviderError) as exc:
        p.delete_voice("v1")
    assert exc.value.code == "delete_voice_failed"
    assert exc.value.retryable is False  # NotFound 不是 retryable


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, retryable", [
    ("Connection timeout", True),
    ("HTTP 503 service unavailable", True),
    ("502 bad gateway", True),
    ("429 Too Many Requests", True),
    ("Rate limit exceeded", True),
    ("Throttled by quota", True),
    ("InvalidParameter", False),
    ("BadRequest.InputDownloadFailed", False),
    ("NotFound", False),
    ("", False),
])
def test_retryable_keywords(text: str, retryable: bool) -> None:
    from services.mainland_worker.worker.providers.real_cosyvoice import _retryable_keywords
    assert _retryable_keywords(text) is retryable


@pytest.mark.parametrize("status, ready", [
    ({"status": "OK"}, True),
    ("OK", True),
    ({"status": "DEPLOYING"}, False),
    ("DEPLOYING", False),
    ({"status": "FAILED"}, False),
    ({"status": "OK", "details": "FAILED to clean up old"}, False),  # FAIL keyword wins
    (None, False),
])
def test_is_voice_ready(status: Any, ready: bool) -> None:
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider
    assert RealCosyvoiceProvider._is_voice_ready(status) is ready


@pytest.mark.parametrize("speaker_id, expected_prefix", [
    ("speaker_a", "avtspeak"),
    ("speaker_a_long_name", "avtspeak"),
    ("a1b2c3", "avta1b2c"),
    ("", "avtspk"),
    ("!!!", "avtspk"),  # 全非 alnum 退化到 default
])
def test_sanitize_prefix(speaker_id: str, expected_prefix: str) -> None:
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider
    actual = RealCosyvoiceProvider._sanitize_prefix(speaker_id)
    assert actual == expected_prefix
    # 永远 ≤ 10 字符（DashScope 文档约束）
    assert len(actual) <= 10


# ---------------------------------------------------------------------------
# app.create_app live mode 装配
# ---------------------------------------------------------------------------

def test_create_app_live_mode_with_dashscope_key(monkeypatch, tmp_path: Path) -> None:
    from services.mainland_worker.hmac_auth import HmacKey
    from services.mainland_worker.worker.app import create_app
    from services.mainland_worker.worker.audit import InMemoryAuditLogger
    from services.mainland_worker.worker.config import WORKER_MODE_LIVE, WorkerConfig
    from services.mainland_worker.worker.providers.real_cosyvoice import RealCosyvoiceProvider

    _install_fake_dashscope(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-key-for-test")

    cfg = WorkerConfig(
        mode=WORKER_MODE_LIVE,
        hmac_keys=(HmacKey(key_id="k", secret="s"),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "art",
    )
    app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())
    assert isinstance(app.state.cosyvoice_provider, RealCosyvoiceProvider)


def test_create_app_live_mode_without_dashscope_key_raises(monkeypatch, tmp_path: Path) -> None:
    from services.mainland_worker.hmac_auth import HmacKey
    from services.mainland_worker.worker.app import create_app
    from services.mainland_worker.worker.audit import InMemoryAuditLogger
    from services.mainland_worker.worker.config import WORKER_MODE_LIVE, WorkerConfig

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    cfg = WorkerConfig(
        mode=WORKER_MODE_LIVE,
        hmac_keys=(HmacKey(key_id="k", secret="s"),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "art",
    )
    with pytest.raises(RuntimeError, match="WORKER_MODE=live requires DASHSCOPE_API_KEY"):
        create_app(config=cfg, audit_logger=InMemoryAuditLogger())


def test_create_app_mock_mode_default_still_works(monkeypatch, tmp_path: Path) -> None:
    """Phase 1 兼容性：默认 mock 模式不依赖任何 env / SDK。"""
    from services.mainland_worker.hmac_auth import HmacKey
    from services.mainland_worker.worker.app import create_app
    from services.mainland_worker.worker.audit import InMemoryAuditLogger
    from services.mainland_worker.worker.config import WORKER_MODE_MOCK, WorkerConfig
    from services.mainland_worker.worker.providers.mock_cosyvoice import MockCosyvoiceProvider

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    cfg = WorkerConfig(
        mode=WORKER_MODE_MOCK,
        hmac_keys=(HmacKey(key_id="k", secret="s"),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "art",
    )
    app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())
    assert isinstance(app.state.cosyvoice_provider, MockCosyvoiceProvider)

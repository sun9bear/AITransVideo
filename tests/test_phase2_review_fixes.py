"""Phase 2 联调 review fix 的回归测试 + 守卫。

Codex 2026-05-24 在武汉真实联调时发现并修复 3 处真实 bug：

A. ``language_hints`` 默认 ``("zh", "en")`` 触发 DashScope ``InvalidLanguageHints`` —
   改成只传 ``("zh",)``
B. DashScope SDK 全局 globals 没有显式锁到 mainland endpoint —
   加 ``_dashscope_mainland_context`` 上下文管理器 + ``RLock`` 保护并发
C. ``wav_duration_ms`` 在 DashScope 返回 placeholder header size 时算出
   多小时的虚假时长 — 用 data chunk 实际字节数兜底（Codex 已经加了
   ``test_wav_duration_uses_actual_data_bytes_when_header_size_is_placeholder``）

本文件补的回归测试 + AST/常量守卫覆盖 A、B 两类，C 类已经有 Codex 加的
silent_wav 测试覆盖。每条守卫对应一条"未来不能回退"的契约。

设计原则：
- 仍然不打真实网络（沿用 ``_install_fake_dashscope`` 注入 fake SDK）
- 守卫优先 AST + 常量，避免依赖运行时调用
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from services.mainland_worker.silent_wav import generate_silent_wav


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_COSYVOICE_PATH = (
    REPO_ROOT
    / "src"
    / "services"
    / "mainland_worker"
    / "worker"
    / "providers"
    / "real_cosyvoice.py"
)


# ---------------------------------------------------------------------------
# Reusable fake DashScope（context manager 可见 + 可写 api_key / endpoint URLs）
# ---------------------------------------------------------------------------

def _install_full_fake_dashscope(monkeypatch) -> types.ModuleType:
    """注入 fake dashscope module，含 api_key / base_http_api_url /
    base_websocket_api_url 三个 writable attribute，供 _dashscope_mainland_context
    上下文管理器读 / 写 / restore。
    """
    tts_v2_mod = types.ModuleType("dashscope.audio.tts_v2")

    class _FakeVoiceEnrollmentService:
        def __init__(self, *args, **kwargs):
            pass

        def create_voice(self, **kw):
            return "fake_voice"

        def query_voice(self, voice_id):
            return {"status": "OK"}

        def delete_voice(self, voice_id):
            return None

    class _FakeSpeechSynthesizer:
        def __init__(self, **kw):
            self.ws = None

        def call(self, text):
            return generate_silent_wav(1500)

        def close(self):
            pass

    class _FakeAudioFormat:
        WAV_16000HZ_MONO_16BIT = "WAV_16000HZ_MONO_16BIT"

    tts_v2_mod.VoiceEnrollmentService = _FakeVoiceEnrollmentService
    tts_v2_mod.SpeechSynthesizer = _FakeSpeechSynthesizer
    tts_v2_mod.AudioFormat = _FakeAudioFormat

    dashscope_mod = types.ModuleType("dashscope")
    # 三个 SDK 全局属性 — context manager 进出会读 / 写它们
    dashscope_mod.api_key = "preexisting-other-key"
    dashscope_mod.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    dashscope_mod.base_websocket_api_url = (
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
    )
    dashscope_mod.audio = types.ModuleType("dashscope.audio")
    dashscope_mod.audio.tts_v2 = tts_v2_mod

    monkeypatch.setitem(sys.modules, "dashscope", dashscope_mod)
    monkeypatch.setitem(sys.modules, "dashscope.audio", dashscope_mod.audio)
    monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts_v2_mod)

    return dashscope_mod


def _install_fake_httpx_head(monkeypatch, *, content_length: int = 500_000) -> None:
    import httpx

    class _FakeResp:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-length": str(content_length)}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def head(self, url):
            return _FakeResp()

    monkeypatch.setattr(httpx, "Client", _FakeClient)


# ---------------------------------------------------------------------------
# Fix A: language_hints 默认 ("zh",) 锁定（防回退 ("zh", "en")）
# ---------------------------------------------------------------------------

def test_fixA_default_language_hints_is_zh_only() -> None:
    """plan §Phase 2 实测：传 `language_hints=["zh", "en"]` 会被 DashScope
    返回 `InvalidLanguageHints`。默认必须是 `["zh"]` 单元素。"""
    from services.mainland_worker.worker.providers.real_cosyvoice import (
        RealCosyvoiceProvider,
    )
    p = RealCosyvoiceProvider(api_key="x")
    assert p._language_hints == ["zh"], (
        f"language_hints 默认必须只含 'zh'（Codex 2026-05-24 实测：'en' 会触发 "
        f"InvalidLanguageHints）；当前值: {p._language_hints}"
    )


def test_fixA_language_hints_passed_through_to_create_voice(monkeypatch) -> None:
    """end-to-end：clone 调用时 create_voice 接收到的 language_hints 必须是
    `["zh"]`。这条与单元侧字段值是双重守护——防有人改 default 不改传参。"""
    from services.mainland_worker.types import (
        WorkerCloneConsent,
        WorkerCloneRequest,
        WorkerCloneSample,
    )
    from services.mainland_worker.worker.providers.real_cosyvoice import (
        RealCosyvoiceProvider,
    )

    captured: dict[str, Any] = {}

    tts_v2_mod = types.ModuleType("dashscope.audio.tts_v2")

    class _FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def create_voice(self, **kw):
            captured.update(kw)
            return "v1"

        def query_voice(self, voice_id):
            return "OK"

    tts_v2_mod.VoiceEnrollmentService = _FakeService

    dashscope_mod = types.ModuleType("dashscope")
    dashscope_mod.api_key = None
    dashscope_mod.base_http_api_url = "intl"
    dashscope_mod.base_websocket_api_url = "intl-ws"
    dashscope_mod.audio = types.ModuleType("dashscope.audio")
    dashscope_mod.audio.tts_v2 = tts_v2_mod

    monkeypatch.setitem(sys.modules, "dashscope", dashscope_mod)
    monkeypatch.setitem(sys.modules, "dashscope.audio", dashscope_mod.audio)
    monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts_v2_mod)
    _install_fake_httpx_head(monkeypatch)

    req = WorkerCloneRequest(
        job_id="j",
        user_id="u",
        speaker_id="s",
        speaker_name="s",
        target_model="cosyvoice-v3.5-flash",
        sample=WorkerCloneSample(kind="download_url", url="http://x/y.wav", sha256="a" * 64),
        source_segments=(1,),
        consent=WorkerCloneConsent(voice_clone_confirmed=True, confirmed_at="2026-01-01T00:00:00Z"),
    )
    p = RealCosyvoiceProvider(api_key="k", query_poll_interval_s=0.0, query_max_polls=2)
    p.clone(req)

    assert captured.get("language_hints") == ["zh"], (
        f"create_voice 收到的 language_hints 必须是 ['zh']；实际 {captured.get('language_hints')!r}"
    )


def test_fixA_ast_guard_no_english_in_default_language_hints() -> None:
    """AST 守卫：``RealCosyvoiceProvider.__init__`` 默认参数里不允许出现 "en"。

    防"有人未来好心改成 ('zh', 'en') 想多语言"导致回归。如果未来真的要
    支持 multi-language，应该让上层显式传 language_hints，而不是动 default。
    """
    import ast

    tree = ast.parse(REAL_COSYVOICE_PATH.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "RealCosyvoiceProvider"):
            continue
        for member in node.body:
            if not (isinstance(member, ast.FunctionDef) and member.name == "__init__"):
                continue
            # 检查 kw_defaults 里 language_hints 的默认值
            for default in member.args.kw_defaults:
                if default is None:
                    continue
                # 默认值字符串值集合
                strings: list[str] = []
                for sub in ast.walk(default):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        strings.append(sub.value)
                if "en" in strings and "zh" in strings:
                    pytest.fail(
                        "RealCosyvoiceProvider.__init__ default 含 ('zh', 'en')；"
                        "Codex 2026-05-24 实测 'en' 会让 DashScope 返 InvalidLanguageHints。"
                        "如果需要多语言请上层显式传 language_hints。"
                    )


# ---------------------------------------------------------------------------
# Fix B: mainland endpoint 锁定 + globals 恢复
# ---------------------------------------------------------------------------

def test_fixB_mainland_endpoint_urls_locked_to_mainland() -> None:
    """常量必须指向 mainland 端点，不能是 dashscope-intl。

    plan §CosyVoice 产品约束："海外 DashScope endpoint 不足以覆盖 clone/design path"。
    """
    from services.mainland_worker.worker.providers.real_cosyvoice import (
        MAINLAND_HTTP_API_URL,
        MAINLAND_WEBSOCKET_API_URL,
    )
    assert MAINLAND_HTTP_API_URL == "https://dashscope.aliyuncs.com/api/v1"
    assert MAINLAND_WEBSOCKET_API_URL == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    # 反向断言：禁止 intl
    for url in (MAINLAND_HTTP_API_URL, MAINLAND_WEBSOCKET_API_URL):
        assert "dashscope-intl" not in url, (
            f"endpoint URL 不能切到 intl: {url}"
        )


def test_fixB_context_manager_sets_mainland_and_restores(monkeypatch) -> None:
    """context manager 进入时：api_key/http/ws 三个全局被设到指定值；
    退出时全部恢复原值。"""
    fake = _install_full_fake_dashscope(monkeypatch)
    original_api_key = fake.api_key
    original_http = fake.base_http_api_url
    original_ws = fake.base_websocket_api_url

    from services.mainland_worker.worker.providers.real_cosyvoice import (
        MAINLAND_HTTP_API_URL,
        MAINLAND_WEBSOCKET_API_URL,
        _dashscope_mainland_context,
    )

    with _dashscope_mainland_context("new-key") as ds:
        assert ds.api_key == "new-key"
        assert ds.base_http_api_url == MAINLAND_HTTP_API_URL
        assert ds.base_websocket_api_url == MAINLAND_WEBSOCKET_API_URL

    # 退出后恢复
    assert fake.api_key == original_api_key
    assert fake.base_http_api_url == original_http
    assert fake.base_websocket_api_url == original_ws


def test_fixB_context_manager_restores_on_exception(monkeypatch) -> None:
    """body 抛异常时 globals 仍要恢复（finally 路径）。"""
    fake = _install_full_fake_dashscope(monkeypatch)
    original_api_key = fake.api_key
    original_http = fake.base_http_api_url
    original_ws = fake.base_websocket_api_url

    from services.mainland_worker.worker.providers.real_cosyvoice import (
        _dashscope_mainland_context,
    )

    with pytest.raises(RuntimeError, match="kaboom"):
        with _dashscope_mainland_context("new-key"):
            raise RuntimeError("kaboom")

    assert fake.api_key == original_api_key
    assert fake.base_http_api_url == original_http
    assert fake.base_websocket_api_url == original_ws


def test_fixB_context_manager_uses_lock() -> None:
    """``_DASHSCOPE_LOCK`` 必须是 RLock 实例 — 防有人误改成普通 Lock
    导致同一线程里嵌套调用（例如未来 retry 包装）死锁。"""
    import threading
    from services.mainland_worker.worker.providers import real_cosyvoice
    lock = real_cosyvoice._DASHSCOPE_LOCK
    # RLock 实例的类有 _RLock 或 RLock 字样；threading.RLock() 返工厂函数
    # 实例。判断：能在同一线程获取两次就是 RLock
    assert lock.acquire(blocking=False)
    try:
        # 同线程再 acquire 不死锁就是 RLock
        assert lock.acquire(blocking=False)
        lock.release()
    finally:
        lock.release()


def test_fixB_ast_guard_clone_and_synthesize_use_mainland_context() -> None:
    """AST 守卫：``clone`` 和 ``synthesize_segment`` 必须包裹在
    ``_dashscope_mainland_context`` 内调用 SDK。

    防有人未来"重构"把 with 块拆掉，直接调 SDK 时漏掉 endpoint 锁定。
    """
    import ast

    tree = ast.parse(REAL_COSYVOICE_PATH.read_text(encoding="utf-8"))

    methods_must_use_context = {"clone", "synthesize_segment", "delete_voice"}
    found: dict[str, bool] = {m: False for m in methods_must_use_context}

    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "RealCosyvoiceProvider"):
            continue
        for member in node.body:
            if not (isinstance(member, ast.FunctionDef) and member.name in methods_must_use_context):
                continue
            body_src = ast.unparse(member)
            if "_dashscope_mainland_context" in body_src:
                found[member.name] = True

    missing = [name for name, ok in found.items() if not ok]
    assert not missing, (
        f"下列方法未使用 _dashscope_mainland_context 包裹 DashScope 调用：{missing}。"
        f"plan §CosyVoice 产品约束：endpoint 必须 mainland-only。"
    )


# ---------------------------------------------------------------------------
# Fix C: WAV duration 已被 Codex 的 silent_wav 测试覆盖。这里加一条
# 反向断言 — 防有人误删掉 silent_wav.py 里的 chunks fallback 逻辑。
# ---------------------------------------------------------------------------

def test_fixC_wav_duration_helper_has_chunks_fallback() -> None:
    """AST 守卫：``silent_wav.py`` 必须保留 ``_wav_duration_ms_from_chunks``
    fallback 函数。"""
    import ast

    silent_wav_path = (
        REPO_ROOT / "src" / "services" / "mainland_worker" / "silent_wav.py"
    )
    tree = ast.parse(silent_wav_path.read_text(encoding="utf-8"))

    fallback_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_wav_duration_ms_from_chunks":
            fallback_found = True
            break
    assert fallback_found, (
        "silent_wav.py 缺少 _wav_duration_ms_from_chunks fallback；"
        "Codex 2026-05-24 实测 DashScope 返回 WAV header size 是 placeholder，"
        "wave.getnframes() 会算出多小时虚假时长，必须有此 fallback 兜底。"
    )

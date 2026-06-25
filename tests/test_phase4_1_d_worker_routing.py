"""Phase 4.1 D 守卫测试集（Codex 2026-05-25 v3 三签字版本）。

覆盖 5 项 Codex review 重点 + 3 项 v2 硬约束 + 3 项 v3 细节：

| 重点 | 测试 |
|---|---|
| Codex 1: target_model 来自 segment | test_worker_path_uses_segment_worker_target_model |
| Codex 1: 无 hardcode | test_no_target_model_literal_in_tts_modules (AST) |
| Codex 2: requires_worker 路由 | test_requires_worker_true_routes_to_worker / test_requires_worker_false_keeps_legacy |
| Codex 3: 单段复用 batch | test_worker_path_uses_synthesize_batch_with_single_segment |
| Codex 4: 无 silent fallback | test_worker_network_error_raises_no_minimax_fallback / AST guard |
| Codex 5: env-only secret | test_client_factory_only_reads_from_environ / test_dubbing_segment_has_no_secret_field / AST guard |
| HC#1 无外层 retry | test_worker_path_bypasses_outer_backoff_no_sleep_no_fallback |
| HC#2 billed_chars preserved | test_worker_billed_chars_preserved_from_worker_result |
| HC#3a artifact 解包 | test_worker_synthesize_extract_artifact_writes_wav_and_returns_tts_result |
| HC#3b client close | test_worker_client_close_called_on_success_and_failure |
| D v3 #1 (early branch) | test_outer_backoff_calls_generate_one_exactly_once_for_worker_path |
| D v3 #2 (字段必填) | test_worker_path_requires_voice_id / _requires_worker_target_model |
| D v3 #2 (no matcher) | test_worker_path_does_not_call_matcher_or_default_voice |
| D v3 #3 (speed_decision) | test_worker_request_speech_rate_from_decide_tts_speed |
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
for p in (str(SRC_PATH), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.gemini.translator import DubbingSegment  # noqa: E402
from services.mainland_worker.client_factory import (  # noqa: E402
    ENV_ENABLED,
    ENV_KEY_ID,
    ENV_SECRET,
    ENV_URL,
    build_client_from_env,
)
from services.mainland_worker.silent_wav import generate_silent_wav  # noqa: E402
from services.mainland_worker.types import (  # noqa: E402
    WorkerArtifactPackage,
    WorkerSegmentResult,
    WorkerSynthesizeBatchResponse,
    compute_text_hash,
)
from services.tts.tts_generator import (  # noqa: E402
    TTSConfig,
    TTSGenerationError,
    TTSGenerator,
)
from services.usage_meter import UsageMeter  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_segment() -> DubbingSegment:
    """legacy DubbingSegment（requires_worker=False，所有 worker 字段空）。"""
    return DubbingSegment(
        segment_id=42,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="cosyvoice_custom_test",
        start_ms=0,
        end_ms=2000,
        target_duration_ms=2000,
        source_text="Hello world",
        cn_text="你好世界。",
        tts_provider="cosyvoice",
    )


@pytest.fixture
def worker_segment(base_segment: DubbingSegment) -> DubbingSegment:
    """DubbingSegment 含 worker 标志 + voice_id + target_model。"""
    base_segment.requires_worker = True
    base_segment.worker_target_model = "cosyvoice-v3.5-flash"
    return base_segment


@pytest.fixture
def make_generator():
    def _build() -> TTSGenerator:
        cfg = TTSConfig(api_key="test", base_url="http://x", model="speech-2.8-turbo")
        gen = TTSGenerator(cfg)
        return gen
    return _build


def _make_inline_zip(audio_path: str, wav_bytes: bytes) -> tuple[bytes, str]:
    """构造 inline zip 包 + sha256."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(audio_path, wav_bytes)
    payload = buf.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def _make_batch_response(
    *,
    target_model: str = "cosyvoice-v3.5-flash",
    voice_id: str = "cosyvoice_custom_test",
    billed_chars: int = 26,
    duration_ms: int = 1900,
    text_for_hash: str = "你好世界。",
) -> WorkerSynthesizeBatchResponse:
    audio_path = f"segments/seg_001.wav"
    wav_bytes = generate_silent_wav(duration_ms)
    payload, pkg_sha = _make_inline_zip(audio_path, wav_bytes)
    seg_sha = hashlib.sha256(wav_bytes).hexdigest()
    return WorkerSynthesizeBatchResponse(
        ok=True,
        job_id="no_job",
        target_model=target_model,
        segments=(WorkerSegmentResult(
            segment_id=42,
            speaker_id="speaker_a",
            voice_id=voice_id,
            audio_path=audio_path,
            duration_ms=duration_ms,
            billed_chars=billed_chars,
            sha256=seg_sha,
            provider_request_id="dashscope_req_test",
        ),),
        package=WorkerArtifactPackage(
            kind="inline_base64",
            download_url="",
            sha256=pkg_sha,
            expires_at="2026-05-25T04:00:00Z",
            inline_bytes=payload,
        ),
        worker_request_id="wrk_test_d",
    )


class _FakeClient:
    """Minimal MainlandWorkerClient stand-in for the test surface we care about."""

    def __init__(
        self,
        response: WorkerSynthesizeBatchResponse | None = None,
        raise_on_batch: Exception | None = None,
    ) -> None:
        self.response = response or _make_batch_response()
        self.raise_on_batch = raise_on_batch
        self.batch_calls: list = []
        self.close_count = 0

    def synthesize_batch(self, req):
        self.batch_calls.append(req)
        if self.raise_on_batch is not None:
            raise self.raise_on_batch
        return self.response

    def close(self):
        self.close_count += 1


# ---------------------------------------------------------------------------
# Codex 2: requires_worker routing
# ---------------------------------------------------------------------------

def test_requires_worker_true_routes_to_worker(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker_segment 走 worker path（call synthesize_batch），不调 cosyvoice_synthesize."""
    gen = make_generator()
    fake = _FakeClient()
    monkeypatch.setattr(
        "services.mainland_worker.client_factory.build_client_from_env",
        lambda: fake,
    )
    # 让 legacy 路径函数被调即 fail（验证 worker 路径根本不引用它们）
    def _explode(*args, **kwargs):
        raise AssertionError("legacy cosyvoice path was called on worker_segment")
    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.synthesize", _explode, raising=False,
    )

    result = gen._generate_one_cosyvoice_via_worker(
        worker_segment, "你好世界。", tmp_path,
    )
    assert len(fake.batch_calls) == 1
    assert result.voice_id == "cosyvoice_custom_test"


def test_requires_worker_false_keeps_legacy_dashscope_path(
    monkeypatch, make_generator, base_segment, tmp_path
):
    """requires_worker=False 走 legacy ``cosyvoice_synthesize``，不调 worker."""
    gen = make_generator()
    captured = {}

    def _fake_synth(text, voice, speech_rate):
        captured["text"] = text
        captured["voice"] = voice
        captured["speech_rate"] = speech_rate
        return generate_silent_wav(1000)

    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.synthesize", _fake_synth,
    )
    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.DEFAULT_VOICE", "cosyvoice-default",
        raising=False,
    )

    # 让 worker 工厂被调即 fail（验证 legacy 路径根本不引用它们）
    def _explode():
        raise AssertionError("worker client factory called on legacy segment")
    monkeypatch.setattr(
        "services.tts.tts_generator.__name__", gen.__class__.__module__,
        raising=False,
    )
    # 不能直接 patch build_client_from_env 因为 tts_generator 用 inline import；
    # 用 patch on module 路径
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", _explode)

    # Speaker cache 跳过 matcher
    gen._speaker_voice_cache[base_segment.speaker_id] = ("cosyvoice_v3_voice_a", "high")

    result = gen._generate_one_cosyvoice(base_segment, "你好世界。", tmp_path)
    assert captured["voice"] == "cosyvoice_v3_voice_a"
    assert result.audio_path.endswith(".wav")


# ---------------------------------------------------------------------------
# Codex 1: target_model from segment.worker_target_model, no hardcode
# ---------------------------------------------------------------------------

def test_worker_path_uses_segment_worker_target_model(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker request 的 target_model 必须等于 segment.worker_target_model（不 hardcode）."""
    worker_segment.worker_target_model = "cosyvoice-v3.5-plus"
    fake = _FakeClient(response=_make_batch_response(target_model="cosyvoice-v3.5-plus"))

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    gen._generate_one_cosyvoice_via_worker(worker_segment, "你好世界。", tmp_path)
    assert fake.batch_calls[0].target_model == "cosyvoice-v3.5-plus"


# ---------------------------------------------------------------------------
# Codex 3: 单段 batch endpoint reuse
# ---------------------------------------------------------------------------

def test_worker_path_uses_synthesize_batch_with_single_segment(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """单段 re-TTS 仍走 synthesize_batch（segments=(one,)）—— plan §Studio Post-Edit."""
    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    gen._generate_one_cosyvoice_via_worker(worker_segment, "你好世界。", tmp_path)
    req = fake.batch_calls[0]
    assert len(req.segments) == 1
    assert req.audio_format == "wav"


# ---------------------------------------------------------------------------
# Codex 4: no silent fallback
# ---------------------------------------------------------------------------

def test_worker_network_error_raises_no_minimax_fallback(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 网络错误 → TTSGenerationError；**不调用** minimax / cosyvoice_provider."""
    from services.mainland_worker.client import WorkerNetworkError

    fake = _FakeClient(raise_on_batch=WorkerNetworkError("simulated DNS fail"))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    # MiniMax / 国际 cosyvoice 路径触发即 fail
    def _explode(*args, **kwargs):
        raise AssertionError("silent fallback to other provider on worker failure")
    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.synthesize", _explode, raising=False,
    )

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="Worker"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "你好。", tmp_path)


# ---------------------------------------------------------------------------
# Codex 5: env-only secret
# ---------------------------------------------------------------------------

def test_client_factory_only_reads_from_environ(monkeypatch):
    """build_client_from_env 不接受任何参数；只看 os.environ."""
    for k in (ENV_ENABLED, ENV_URL, ENV_KEY_ID, ENV_SECRET):
        monkeypatch.delenv(k, raising=False)

    assert build_client_from_env() is None  # disabled by default

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setenv(ENV_URL, "http://test.example/worker")
    monkeypatch.setenv(ENV_KEY_ID, "k1")
    monkeypatch.setenv(ENV_SECRET, "s" * 32)
    client = build_client_from_env()
    assert client is not None
    client.close()


def test_dubbing_segment_has_no_secret_field(base_segment):
    """``DubbingSegment`` 的任何字段名都不包含 secret / hmac."""
    field_names = {f.name for f in DubbingSegment.__dataclass_fields__.values()}
    for forbidden in ("hmac_secret", "secret", "api_key", "hmac"):
        for name in field_names:
            assert forbidden not in name.lower(), (
                f"DubbingSegment field {name!r} contains forbidden token {forbidden!r}; "
                "secrets must be env-only per Codex D #5"
            )


# ---------------------------------------------------------------------------
# HC#1: no outer retry
# ---------------------------------------------------------------------------

def test_worker_path_bypasses_outer_backoff_no_sleep_no_fallback(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """_generate_one_with_backoff 对 requires_worker=True 走早分支：
    - _generate_one 只调一次
    - time.sleep 未调用
    - get_fallback_provider 未调用
    """
    gen = make_generator()
    call_count = {"_generate_one": 0, "time.sleep": 0, "get_fallback_provider": 0}

    def _fake_generate_one(segment, output_dir, *, provider=None, usage_bucket=None):
        call_count["_generate_one"] += 1
        raise TTSGenerationError("worker failed")

    def _fake_sleep(*args, **kwargs):
        call_count["time.sleep"] += 1

    def _fake_get_fallback(*args, **kwargs):
        call_count["get_fallback_provider"] += 1
        return "mimo"

    monkeypatch.setattr(gen, "_generate_one", _fake_generate_one)
    monkeypatch.setattr("services.tts.tts_generator.time.sleep", _fake_sleep)
    monkeypatch.setattr("services.tts.tts_generator.get_fallback_provider", _fake_get_fallback)

    with pytest.raises(TTSGenerationError):
        gen._generate_one_with_backoff(worker_segment, str(tmp_path))

    assert call_count["_generate_one"] == 1, "worker path must call _generate_one exactly once"
    assert call_count["time.sleep"] == 0, "worker path must not sleep"
    assert call_count["get_fallback_provider"] == 0, "worker path must not consult fallback"


def test_outer_backoff_calls_generate_one_exactly_once_for_worker_path(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """D v3 #1：worker path 必须直接 return / raise，不进入 backoff loop."""
    gen = make_generator()
    captured: list = []

    def _fake_generate_one(segment, output_dir, *, provider=None, usage_bucket=None):
        captured.append({"provider": provider, "usage_bucket": usage_bucket})
        return "dummy_result"

    monkeypatch.setattr(gen, "_generate_one", _fake_generate_one)
    result = gen._generate_one_with_backoff(worker_segment, str(tmp_path))
    assert len(captured) == 1
    assert result == "dummy_result"


def test_segment_regenerate_worker_path_zero_caller_retries(monkeypatch, tmp_path):
    """segment_regenerate 对 requires_worker=True 段 max_retries=0：
    failure 后 _generate_one 只调一次，time.sleep 未调用.
    """
    from services.tts import segment_regenerate as sr_mod

    call_count = {"_generate_one": 0, "time.sleep": 0}

    class _FakeGen:
        def _generate_one(self, ds, tmpdir, *, provider=None, usage_bucket=None):
            call_count["_generate_one"] += 1
            raise TTSGenerationError("worker boom")

        def set_usage_meter(self, meter):  # no-op
            pass

    def _fake_get_generator():
        return _FakeGen()

    monkeypatch.setattr(sr_mod, "load_tts_config", lambda: TTSConfig(api_key="t", base_url="http://x", model="m"))
    monkeypatch.setattr(sr_mod, "TTSGenerator", lambda cfg: _FakeGen())
    monkeypatch.setattr(sr_mod.time, "sleep", lambda *_a, **_k: call_count.__setitem__("time.sleep", call_count["time.sleep"] + 1))

    caller = sr_mod.build_real_segment_tts_caller(max_retries=3)
    seg_dict = {
        "segment_id": "42",
        "speaker_id": "speaker_a",
        "display_name": "X",
        "voice_id": "cosyvoice_custom_test",
        "start_ms": 0,
        "end_ms": 1000,
        "target_duration_ms": 1000,
        "source_text": "hi",
        "cn_text": "你好",
        "tts_provider": "cosyvoice",
        "requires_worker": True,  # the gate
        "worker_target_model": "cosyvoice-v3.5-flash",
    }
    output = tmp_path / "draft.wav"
    with pytest.raises(RuntimeError):
        caller(seg_dict, output)
    assert call_count["_generate_one"] == 1, "worker path must not retry on caller layer"
    assert call_count["time.sleep"] == 0


# ---------------------------------------------------------------------------
# HC#2: billed_chars preserved
# ---------------------------------------------------------------------------

def test_worker_billed_chars_preserved_from_worker_result(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回 billed_chars=26（混合 CJK/EN/punct 真实计费）→ result.billed_chars 保留 26.
    不被 ``_generate_one`` 的 ``_cn_chars * 2 == 26`` 巧合掩盖 → 用 23 让差异显现."""
    fake = _FakeClient(response=_make_batch_response(billed_chars=23))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    result = gen._generate_one_cosyvoice_via_worker(
        worker_segment, "你好世界，hello!", tmp_path,
    )
    assert result.billed_chars == 23


def test_generate_one_does_not_overwrite_worker_billed_chars(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """走 _generate_one 入口时，requires_worker=True 段不应被 ``len(text)*2`` 覆盖."""
    fake = _FakeClient(response=_make_batch_response(billed_chars=23))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    # 直接调 _generate_one 看路由 + billed_chars 保护
    gen = make_generator()
    result = gen._generate_one(worker_segment, str(tmp_path), provider="cosyvoice")
    assert result.billed_chars == 23


# ---------------------------------------------------------------------------
# HC#3a: artifact 解包
# ---------------------------------------------------------------------------

def test_worker_tts_usage_records_worker_target_model(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """Worker-routed CosyVoice TTS must report the worker target model.

    Production regression: usage_events recorded provider=cosyvoice but
    model=speech-2.8-turbo, making admin reports attribute worker TTS to the
    legacy MiniMax model instead of cosyvoice-v3.5-*.
    """
    worker_segment.worker_target_model = "cosyvoice-v3.5-plus"
    fake = _FakeClient(
        response=_make_batch_response(
            target_model="cosyvoice-v3.5-plus",
            billed_chars=23,
        )
    )
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    meter = UsageMeter(tmp_path / "project", job_id="job-worker-usage")
    gen.set_usage_meter(meter)

    gen._generate_one(
        worker_segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    events = [event for event in meter.events if event.get("kind") == "tts"]
    assert len(events) == 1
    assert events[0]["provider"] == "cosyvoice"
    assert events[0]["model"] == "cosyvoice-v3.5-plus"
    assert events[0]["worker_target_model"] == "cosyvoice-v3.5-plus"
    assert events[0]["requires_worker"] is True
    assert events[0]["billed_chars"] == 23

    summary = meter.summarize()
    assert summary["tts_call_count_by_provider_model"] == {
        "cosyvoice:cosyvoice-v3.5-plus": 1,
    }
    assert summary["tts_billed_chars_by_provider_model"] == {
        "cosyvoice:cosyvoice-v3.5-plus": 23,
    }


def test_worker_synthesize_extract_artifact_writes_wav_and_returns_tts_result(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    fake = _FakeClient(response=_make_batch_response(duration_ms=1500))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    result = gen._generate_one_cosyvoice_via_worker(
        worker_segment, "你好。", tmp_path,
    )
    assert Path(result.audio_path).is_file()
    assert Path(result.audio_path).stat().st_size > 0
    assert result.voice_id == "cosyvoice_custom_test"
    assert result.selected_voice == "cosyvoice_custom_test"
    assert result.duration_ms > 0


# ---------------------------------------------------------------------------
# HC#3b: client close
# ---------------------------------------------------------------------------

def test_worker_client_close_called_on_success(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_client_close_called_on_network_error(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    from services.mainland_worker.client import WorkerNetworkError

    fake = _FakeClient(raise_on_batch=WorkerNetworkError("net"))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_client_close_called_on_signature_error(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    from services.mainland_worker.client import WorkerSignatureRejectedError

    fake = _FakeClient(raise_on_batch=WorkerSignatureRejectedError(
        "bad sig", code="signature_invalid", http_status=401,
    ))
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


# ---------------------------------------------------------------------------
# D v3 #2: 必填校验
# ---------------------------------------------------------------------------

def test_worker_path_requires_voice_id(monkeypatch, make_generator, worker_segment, tmp_path):
    worker_segment.voice_id = ""  # empty
    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="voice_id"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)


def test_worker_path_requires_worker_target_model(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    worker_segment.worker_target_model = ""
    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="worker_target_model"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)


def test_worker_path_does_not_call_matcher_or_default_voice(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """Codex v3 #2c：worker path 既不走 matcher，也不 fallback default_voice."""
    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    # 任何 matcher / default voice 被引用即 fail
    def _explode_matcher(*args, **kwargs):
        raise AssertionError("voice_match_resolver called on worker path")
    monkeypatch.setattr(
        "services.tts.voice_match_resolver.resolve_voice_match",
        _explode_matcher,
        raising=False,
    )
    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.DEFAULT_VOICE",
        "SHOULD_NOT_BE_USED",
        raising=False,
    )
    def _explode_synth(*args, **kwargs):
        raise AssertionError("cosyvoice_provider.synthesize called on worker path")
    monkeypatch.setattr(
        "services.tts.cosyvoice_provider.synthesize", _explode_synth, raising=False,
    )

    gen = make_generator()
    gen._generate_one_cosyvoice_via_worker(worker_segment, "你好。", tmp_path)


# ---------------------------------------------------------------------------
# D v3 #3: speech_rate from decide_tts_speed
# ---------------------------------------------------------------------------

def test_worker_request_speech_rate_from_decide_tts_speed(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    from services.tts.speed_decision import SpeedDecision

    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    monkeypatch.setattr(
        "services.tts.speed_decision.decide_tts_speed",
        lambda **kw: SpeedDecision(speed=0.92, reason="in_range", estimated_ms=1900, ratio=0.95),
    )

    gen = make_generator()
    gen._generate_one_cosyvoice_via_worker(worker_segment, "你好。", tmp_path)
    seg_req = fake.batch_calls[0].segments[0]
    assert abs(seg_req.speech_rate - 0.92) < 1e-6


# ---------------------------------------------------------------------------
# AST guards
# ---------------------------------------------------------------------------

def _strings_in_file(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def test_no_target_model_literal_in_tts_modules():
    """tts_generator.py / segment_regenerate.py 不得包含 ``cosyvoice-v3.5-flash`` /
    ``cosyvoice-v3.5-plus`` 字符串字面量（除文档 / 错误消息中的引用）."""
    tts_gen = SRC_PATH / "services" / "tts" / "tts_generator.py"
    seg_regen = SRC_PATH / "services" / "tts" / "segment_regenerate.py"
    for path in (tts_gen, seg_regen):
        strings = _strings_in_file(path)
        bad = {s for s in strings if s.startswith("cosyvoice-v3.5-")}
        assert not bad, (
            f"{path.name} contains hardcoded target_model literal(s): {bad}; "
            "must come from segment.worker_target_model"
        )


def test_services_tts_does_not_import_gateway():
    """src/services/tts/ 子树不得 import gateway/* 模块（守住命名空间隔离）."""
    tts_dir = SRC_PATH / "services" / "tts"
    for py in tts_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("gateway"), (
                        f"{py.relative_to(SRC_PATH)}: forbidden import {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith("gateway"), (
                        f"{py.relative_to(SRC_PATH)}: forbidden 'from {node.module} import ...'"
                    )


def test_no_minimax_branch_in_cosyvoice_worker_path():
    """``_generate_one_cosyvoice_via_worker`` 函数体不得**调用 / 引用**任何
    其它 provider 的 module / function（``minimax`` / ``mimo`` / ``volcengine`` /
    ``cosyvoice_provider``）—— 防 silent fallback 渗入。

    实现：检查 ast.Name / ast.Attribute 标识符（**不**检查 docstring / 注释
    里的英文 prose；那里出现 "refusing to fall back to MiniMax" 是合法的文档说明，
    不是代码分支）。
    """
    tts_gen = SRC_PATH / "services" / "tts" / "tts_generator.py"
    src = tts_gen.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_generate_one_cosyvoice_via_worker":
            found = True
            # 收集函数体里的 identifier 引用（Name / Attribute），跳过 docstring
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]  # skip docstring
            for sub in body:
                for inner in ast.walk(sub):
                    name: str | None = None
                    if isinstance(inner, ast.Name):
                        name = inner.id
                    elif isinstance(inner, ast.Attribute):
                        name = inner.attr
                    if not name:
                        continue
                    low = name.lower()
                    for forbidden in ("minimax", "mimo_synth", "volcengine",
                                       "fallback_used_provider", "get_fallback_provider"):
                        assert forbidden not in low, (
                            f"_generate_one_cosyvoice_via_worker references identifier "
                            f"{name!r} containing forbidden token {forbidden!r} — "
                            f"fail-closed path must raise, never silent fallback"
                        )
            break
    assert found, "_generate_one_cosyvoice_via_worker not found in tts_generator.py"


def test_no_hmac_secret_attr_in_job_spec_or_segment():
    """``DubbingSegment`` 任何字段都不允许携带 secret；segment dataclass 是 plumbing 输入,
    不是 secret 通道."""
    field_names = {f.name for f in DubbingSegment.__dataclass_fields__.values()}
    for forbidden in ("hmac", "secret", "api_key", "credential"):
        assert not any(forbidden in n.lower() for n in field_names), (
            f"DubbingSegment field name contains {forbidden!r}; secrets must be env-only"
        )


# ===========================================================================
# P1 fix tests: requires_worker provider 漂移防护（Codex 2026-05-25 D 第四轮）
# ===========================================================================

def test_requires_worker_with_empty_tts_provider_does_not_call_minimax(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """``requires_worker=True`` + ``tts_provider=""`` + job-level ``minimax`` 时，
    fail-closed 强制锁定 ``provider="cosyvoice"`` 走 worker —— **不允许** segment
    漂到 MiniMax 路径。"""
    worker_segment.tts_provider = ""  # 模拟 E 阶段漏写
    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    # 任何 MiniMax / MiMo / VolcEngine 路径触发即 fail
    def _explode_minimax(*args, **kwargs):
        raise AssertionError("MiniMax HTTP / synth called for requires_worker segment")

    monkeypatch.setattr("services.tts.tts_generator.requests", None, raising=False)

    gen = make_generator()
    # Job-level 设到 minimax 模拟漂移源
    gen._job_provider = "minimax"
    # 用 generator._generate_one 入口走完整路径
    result = gen._generate_one(worker_segment, str(tmp_path), provider=None)
    assert result.voice_id == "cosyvoice_custom_test"
    # worker 被调用 (P1 fix 正确强制 cosyvoice → worker)
    assert len(fake.batch_calls) == 1


def test_requires_worker_with_non_cosyvoice_tts_provider_fails_closed(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """``requires_worker=True`` + ``tts_provider="minimax"`` (数据不一致) → 抛错，
    **不调用任何 provider**（既不调 worker 也不调 minimax）."""
    worker_segment.tts_provider = "minimax"  # 数据不一致：clone voice 不能配 minimax

    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="requires_worker=True"):
        gen._generate_one(worker_segment, str(tmp_path), provider=None)

    # 严格：worker 不被调，minimax 不被调
    assert fake.batch_calls == []


@pytest.mark.parametrize("bad_provider", ["mimo", "volcengine", "minimax"])
def test_requires_worker_with_any_non_cosyvoice_tts_provider_fails(
    monkeypatch, make_generator, worker_segment, tmp_path, bad_provider,
):
    """三个其它 provider 都必须被 P1 fix 拒绝."""
    worker_segment.tts_provider = bad_provider

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="requires_worker=True"):
        gen._generate_one(worker_segment, str(tmp_path), provider=None)


def test_requires_worker_with_job_provider_minimax_still_forces_cosyvoice(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """``requires_worker=True`` + ``tts_provider`` 空 + ``_job_provider="minimax"`` →
    强制走 cosyvoice / worker. 同 D 的核心约束."""
    worker_segment.tts_provider = ""

    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    gen._job_provider = "minimax"
    gen._generate_one(worker_segment, str(tmp_path))
    assert len(fake.batch_calls) == 1


# ===========================================================================
# P2 fix tests: worker response invariant + close 覆盖
# ===========================================================================

def test_worker_target_model_echo_mismatch_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回的 target_model ≠ 请求 → TTSGenerationError + client.close 调用."""
    worker_segment.worker_target_model = "cosyvoice-v3.5-flash"
    # worker 错误地回 plus
    mismatched = _make_batch_response(target_model="cosyvoice-v3.5-plus")
    fake = _FakeClient(response=mismatched)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="target_model"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_response_with_zero_segments_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回 0 segments → TTSGenerationError + close. 0-segment 受
    ``WorkerSynthesizeBatchRequest`` __post_init__ 阻挡，但响应端要单独防漂移."""
    # 直接构造一个有效 (但 segments 长度伪造的) 响应包；用 0 segment 模拟漂移
    # 注意：用 1 元组通过 frozen dataclass 后再操纵到 0 — 用 namedtuple-ish 替身
    class _ResponseTwo:
        ok = True
        job_id = "no_job"
        target_model = "cosyvoice-v3.5-flash"
        segments: tuple = ()  # ★ 漂移
        package = None
        worker_request_id = "wrk_test"
    fake = _FakeClient(response=_ResponseTwo())  # type: ignore[arg-type]

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="expected 1 segment"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_response_with_too_many_segments_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回 >1 segments → TTSGenerationError + close."""
    # 直接构造 2-segment response
    audio_path_1 = "segments/seg_001.wav"
    audio_path_2 = "segments/seg_002.wav"
    wav1 = generate_silent_wav(1000)
    wav2 = generate_silent_wav(1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(audio_path_1, wav1)
        zf.writestr(audio_path_2, wav2)
    payload = buf.getvalue()
    pkg_sha = hashlib.sha256(payload).hexdigest()
    sha1 = hashlib.sha256(wav1).hexdigest()
    sha2 = hashlib.sha256(wav2).hexdigest()

    two_seg = WorkerSynthesizeBatchResponse(
        ok=True,
        job_id="no_job",
        target_model="cosyvoice-v3.5-flash",
        segments=(
            WorkerSegmentResult(
                segment_id=42, speaker_id="speaker_a", voice_id="v1",
                audio_path=audio_path_1, duration_ms=1000, billed_chars=10,
                sha256=sha1, provider_request_id=None,
            ),
            WorkerSegmentResult(
                segment_id=43, speaker_id="speaker_a", voice_id="v1",
                audio_path=audio_path_2, duration_ms=1000, billed_chars=10,
                sha256=sha2, provider_request_id=None,
            ),
        ),
        package=WorkerArtifactPackage(
            kind="inline_base64",
            download_url="",
            sha256=pkg_sha,
            expires_at="2026-05-25T04:00:00Z",
            inline_bytes=payload,
        ),
        worker_request_id="wrk_test",
    )
    fake = _FakeClient(response=two_seg)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="expected 1 segment"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_artifact_integrity_error_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """artifact zip 解包失败（sha 不匹配）→ TTSGenerationError + close."""
    # 构造一个 sha mismatch 的响应
    audio_path = "segments/seg_001.wav"
    wav_bytes = generate_silent_wav(1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(audio_path, wav_bytes)
    payload = buf.getvalue()
    # **故意**用错误 sha
    bad_pkg_sha = "0" * 64
    seg_sha = hashlib.sha256(wav_bytes).hexdigest()
    bad_resp = WorkerSynthesizeBatchResponse(
        ok=True,
        job_id="no_job",
        target_model="cosyvoice-v3.5-flash",
        segments=(WorkerSegmentResult(
            segment_id=42, speaker_id="speaker_a",
            voice_id="cosyvoice_custom_test",
            audio_path=audio_path, duration_ms=1000, billed_chars=10,
            sha256=seg_sha, provider_request_id=None,
        ),),
        package=WorkerArtifactPackage(
            kind="inline_base64",
            download_url="",
            sha256=bad_pkg_sha,  # ★ 不匹配
            expires_at="2026-05-25T04:00:00Z",
            inline_bytes=payload,
        ),
        worker_request_id="wrk_test",
    )
    fake = _FakeClient(response=bad_resp)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="artifact"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_business_error_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """``WorkerError``（业务错误，非 network / signature）也必须触发 client.close.

    覆盖 D.3 实现 ``except`` 链里 ``WorkerError`` 分支的 close 覆盖。
    """
    from services.mainland_worker.client import WorkerError

    fake = _FakeClient(raise_on_batch=WorkerError(
        "provider quota exceeded", code="quota_exceeded", http_status=429,
    ))

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="Worker"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


# ===========================================================================
# P2-1 fix tests: segment_id / speaker_id / voice_id echo invariant
# ===========================================================================

def _make_response_with_segment_overrides(
    *,
    segment_id: int = 42,
    speaker_id: str = "speaker_a",
    voice_id: str = "cosyvoice_custom_test",
    target_model: str = "cosyvoice-v3.5-flash",
) -> WorkerSynthesizeBatchResponse:
    """构造可改 segment_id / speaker_id / voice_id 的单段响应（用于 echo 漂移测试）."""
    audio_path = "segments/seg_001.wav"
    wav_bytes = generate_silent_wav(1000)
    payload, pkg_sha = _make_inline_zip(audio_path, wav_bytes)
    seg_sha = hashlib.sha256(wav_bytes).hexdigest()
    return WorkerSynthesizeBatchResponse(
        ok=True,
        job_id="no_job",
        target_model=target_model,
        segments=(WorkerSegmentResult(
            segment_id=segment_id,
            speaker_id=speaker_id,
            voice_id=voice_id,
            audio_path=audio_path,
            duration_ms=1000,
            billed_chars=10,
            sha256=seg_sha,
            provider_request_id=None,
        ),),
        package=WorkerArtifactPackage(
            kind="inline_base64",
            download_url="",
            sha256=pkg_sha,
            expires_at="2026-05-25T04:00:00Z",
            inline_bytes=payload,
        ),
        worker_request_id="wrk_test",
    )


def test_worker_segment_id_echo_mismatch_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回的 segment_id ≠ 请求 → TTSGenerationError + close."""
    # 请求 segment_id=42，worker 漂移返回 99
    drift = _make_response_with_segment_overrides(segment_id=99)
    fake = _FakeClient(response=drift)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="segment_id echo mismatch"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_speaker_id_echo_mismatch_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回的 speaker_id ≠ 请求 → TTSGenerationError + close."""
    drift = _make_response_with_segment_overrides(speaker_id="speaker_z")
    fake = _FakeClient(response=drift)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="speaker_id echo mismatch"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


def test_worker_voice_id_echo_mismatch_raises_and_closes(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 返回的 voice_id ≠ 请求 → TTSGenerationError + close.

    这是最关键的 voice 漂移防护：worker / provider bug 让请求 cosyvoice_custom_a
    回了 cosyvoice_custom_b，落库后所有该 voice 的 segment 都漂到错误音色。
    """
    drift = _make_response_with_segment_overrides(voice_id="cosyvoice_drifted_voice")
    fake = _FakeClient(response=drift)

    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    with pytest.raises(TTSGenerationError, match="voice_id echo mismatch"):
        gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert fake.close_count == 1


# ===========================================================================
# P2-2 fix test: match_confidence 锁定在 high/medium/low 枚举
# ===========================================================================

def test_worker_path_result_match_confidence_is_high(
    monkeypatch, make_generator, worker_segment, tmp_path
):
    """worker 路径返回的 TTSResult.match_confidence 必须是 ``"high"``（既有
    selector 契约枚举），不能引入新的 ``"explicit_worker_voice"`` 等枚举外值。
    """
    fake = _FakeClient()
    import services.mainland_worker.client_factory as cf_mod
    monkeypatch.setattr(cf_mod, "build_client_from_env", lambda: fake)

    gen = make_generator()
    result = gen._generate_one_cosyvoice_via_worker(worker_segment, "hi", tmp_path)
    assert result.match_confidence == "high", (
        f"worker path must return match_confidence='high' (per existing selector "
        f"enum); got {result.match_confidence!r}"
    )

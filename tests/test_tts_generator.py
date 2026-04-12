from io import BytesIO
import json
from pathlib import Path

from pydub import AudioSegment
import pytest

from services.gemini.translator import DubbingSegment
from services.tts.tts_generator import (
    TTSConfig,
    TTSGenerationError,
    TTSGenerator,
    TTSResult,
    load_tts_config,
)
import services.tts.tts_generator as tts_generator_module


def _build_segment(
    *,
    segment_id: int,
    start_ms: int = 0,
    end_ms: int = 1_000,
    cn_text: str = "测试中文配音",
    voice_id: str = "voice_demo_001",
) -> DubbingSegment:
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id=voice_id,
        start_ms=start_ms,
        end_ms=end_ms,
        target_duration_ms=end_ms - start_ms,
        source_text="Demo English text.",
        cn_text=cn_text,
    )


def _build_wav_bytes(*, duration_ms: int = 1_000) -> bytes:
    buffer = BytesIO()
    AudioSegment.silent(duration=duration_ms).export(buffer, format="wav")
    return buffer.getvalue()


def _install_fake_requests(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, object] | Exception],
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeRequests:
        @staticmethod
        def post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int) -> FakeResponse:
            calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            response = responses[len(calls) - 1]
            if isinstance(response, Exception):
                raise response
            return FakeResponse(
                status_code=int(response["status_code"]),
                payload=dict(response.get("payload", {})),
            )

    monkeypatch.setattr(tts_generator_module, "requests", FakeRequests())
    return calls


def test_tts_generator_generates_single_segment_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_000)
    audio_hex = wav_bytes.hex()
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            }
        ],
    )
    segment = _build_segment(segment_id=1)

    results = TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        [segment],
        str(tmp_path / "tts"),
    )

    assert len(results) == 1
    result = results[0]
    assert Path(result.audio_path).exists()
    assert Path(result.audio_path).name == "segment_001_speaker_a.wav"
    assert result.duration_ms > 0
    assert segment.tts_audio_path == result.audio_path
    assert calls[0]["url"] == "https://api.minimaxi.com/v1/t2a_v2"


def test_tts_generator_generates_multiple_segments_and_updates_alignment_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_hex = _build_wav_bytes(duration_ms=800).hex()
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            },
        ],
    )
    segments = [
        _build_segment(segment_id=1, start_ms=0, end_ms=1_000),
        _build_segment(segment_id=2, start_ms=1_000, end_ms=2_000),
        _build_segment(segment_id=3, start_ms=2_000, end_ms=3_000),
    ]

    results = TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        segments,
        str(tmp_path / "tts"),
    )

    assert len(results) == 3
    for index, segment in enumerate(segments, start=1):
        assert Path(results[index - 1].audio_path).exists()
        assert Path(results[index - 1].audio_path).name == f"segment_{index:03d}_speaker_a.wav"
        assert segment.actual_duration_ms > 0
        assert segment.alignment_ratio > 0
        assert segment.tts_audio_path is not None


def test_tts_generator_raises_on_non_200_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provide enough responses for outer backoff retries (up to 6 attempts)
    _install_fake_requests(
        monkeypatch,
        [{"status_code": 500, "payload": {}} for _ in range(50)],
    )

    with pytest.raises(TTSGenerationError, match="status_code=500"):
        TTSGenerator(TTSConfig(api_key="secret", max_retries=0)).generate_all(
            [_build_segment(segment_id=1)],
            str(tmp_path / "tts"),
        )


def test_tts_generator_raises_on_business_error_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    biz_error_resp = {
        "status_code": 200,
        "payload": {
            "base_resp": {"status_code": 1039, "status_msg": "invalid voice_id"},
            "data": {"audio": ""},
        },
    }
    # Provide enough responses for outer backoff retries
    calls = _install_fake_requests(
        monkeypatch,
        [biz_error_resp for _ in range(50)],
    )

    with pytest.raises(TTSGenerationError, match="invalid voice_id"):
        TTSGenerator(TTSConfig(api_key="secret")).generate_all(
            [_build_segment(segment_id=1)],
            str(tmp_path / "tts"),
        )
    assert len(calls) >= 1


def test_tts_generator_decodes_hex_audio_and_writes_expected_wav_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_200)
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            }
        ],
    )
    segment = _build_segment(segment_id=1)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
    )

    written_bytes = Path(result.audio_path).read_bytes()
    assert written_bytes == wav_bytes
    assert len(AudioSegment.from_wav(result.audio_path)) > 0


def test_tts_generator_uses_cn_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=900)
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            }
        ],
    )
    segment = _build_segment(
        segment_id=1,
        cn_text="配音文本",
    )

    TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        [segment],
        str(tmp_path / "tts"),
    )

    assert calls[0]["json"]["text"] == "配音文本"


def test_tts_generator_prints_progress_every_five_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audio_hex = _build_wav_bytes(duration_ms=800).hex()
    _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": audio_hex},
                },
            }
            for _ in range(6)
        ],
    )
    segments = [
        _build_segment(segment_id=index, start_ms=(index - 1) * 1_000, end_ms=index * 1_000)
        for index in range(1, 7)
    ]

    TTSGenerator(TTSConfig(api_key="secret")).generate_all(
        segments,
        str(tmp_path / "tts"),
    )

    captured = capsys.readouterr().out
    # Progress prints at index%15==0 or index==total.  With 6 segments,
    # only the final 6/6 is printed (not 5/6).
    assert "6/6" in captured


def test_load_tts_config_reads_existing_tts_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "api_key": "config-secret",
                    "api_key_env_var": "AUTODUB_TTS_API_KEY",
                    "base_url": "https://api.minimaxi.com",
                    "model_name": "speech-2.8-turbo",
                    "audio_format": "wav",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tts_generator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_tts_config()

    assert config.api_key == "config-secret"
    assert config.base_url == "https://api.minimaxi.com"
    assert config.model == "speech-2.8-turbo"
    assert config.audio_format == "wav"


def test_tts_generator_retries_transient_request_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wav_bytes = _build_wav_bytes(duration_ms=1_000)
    calls = _install_fake_requests(
        monkeypatch,
        [
            RuntimeError("tls eof"),
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                    "data": {"audio": wav_bytes.hex()},
                },
            },
        ],
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(tts_generator_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    results = TTSGenerator(
        TTSConfig(
            api_key="secret",
            timeout_seconds=12,
            max_retries=2,
            retry_backoff_seconds=0.5,
        )
    ).generate_all(
        [_build_segment(segment_id=1)],
        str(tmp_path / "tts"),
    )

    assert len(results) == 1
    assert len(calls) == 2
    assert sleep_calls == [0.5]
    captured = capsys.readouterr().out
    assert "MiniMax请求失败，0.5秒后重试（1/2）" in captured


def test_tts_generator_does_not_fallback_from_cosyvoice_to_mimo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = TTSGenerator(TTSConfig(api_key="secret"))
    generator._job_provider = "cosyvoice"
    generator._OUTER_BACKOFF_SCHEDULE = [0]
    generator._OUTER_PAUSE_SECONDS = 0

    attempted_providers: list[str] = []

    def _fake_generate_one(segment, output_dir, *, provider=None):
        attempted_providers.append(str(provider))
        if len(attempted_providers) == 1:
            raise TTSGenerationError("primary failed")
        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(tmp_path / "tts" / "segment.wav"),
            duration_ms=800,
            voice_id=segment.voice_id,
        )

    monkeypatch.setattr(generator, "_generate_one", _fake_generate_one)
    monkeypatch.setattr(tts_generator_module.time, "sleep", lambda seconds: None)

    result = generator._generate_one_with_backoff(
        _build_segment(segment_id=1),
        str(tmp_path / "tts"),
    )

    assert result.duration_ms == 800
    assert attempted_providers == ["cosyvoice", "cosyvoice"]


def test_tts_generator_retries_invalid_cosyvoice_voice_with_safe_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.tts.cosyvoice_provider as cosyvoice_provider_module
    import services.tts.voice_match_resolver as resolver_module
    from services.tts.voice_match_types import VoiceMatchResult

    wav_bytes = _build_wav_bytes(duration_ms=700)
    attempts: list[str] = []
    segment = _build_segment(segment_id=1)
    segment.gender = "male"
    segment.age_group = "middle"
    segment.persona_style = ""
    segment.energy_level = ""

    # Mock the resolver to return longsanshu_v3 (which will be rejected by the provider)
    monkeypatch.setattr(
        resolver_module,
        "resolve_voice_match",
        lambda req: VoiceMatchResult(
            voice_id="longsanshu_v3", match_reason="test", match_score=0.60,
            match_confidence="medium", backup_voices=(),
        ),
    )

    def _fake_cosyvoice_synthesize(*, text: str, voice: str, model: str = "cosyvoice-v3-flash", api_key=None) -> bytes:
        del text, model, api_key
        attempts.append(voice)
        if voice == "longsanshu_v3":
            raise cosyvoice_provider_module.CosyVoiceTTSError(
                "DashScope SDK returned None for voice=longsanshu_v3, model=cosyvoice-v3-flash. "
                "This usually means the voice parameter does not match the selected model."
            )
        assert voice == cosyvoice_provider_module.DEFAULT_VOICE
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_cosyvoice_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    assert result.duration_ms > 0
    assert attempts == ["longsanshu_v3", cosyvoice_provider_module.DEFAULT_VOICE]


def test_tts_generator_prefers_explicit_cosyvoice_builtin_voice_id_over_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.tts.cosyvoice_provider as cosyvoice_provider_module
    import services.tts.voice_match_resolver as resolver_module

    wav_bytes = _build_wav_bytes(duration_ms=650)
    attempts: list[str] = []
    segment = _build_segment(segment_id=1, voice_id="longshu_v3")
    segment.gender = "female"
    segment.age_group = "young"
    segment.persona_style = "energetic"
    segment.energy_level = "high"

    def _unexpected_resolver(req):
        raise AssertionError("explicit builtin cosyvoice voice_id should bypass resolver")

    def _fake_cosyvoice_synthesize(*, text: str, voice: str, model: str = "cosyvoice-v3-flash", api_key=None) -> bytes:
        del text, model, api_key
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(resolver_module, "resolve_voice_match", _unexpected_resolver)
    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_cosyvoice_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    assert result.duration_ms > 0
    assert attempts == ["longshu_v3"]


def test_load_tts_config_reads_retry_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "api_key": "config-secret",
                    "timeout_seconds": 45,
                    "max_retries": 5,
                    "retry_backoff_seconds": 2.5,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tts_generator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_tts_config()

    assert config.timeout_seconds == 45
    assert config.max_retries == 5
    assert config.retry_backoff_seconds == 2.5


def test_cosyvoice_resolver_path_populates_selected_voice_and_confidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that _generate_one_cosyvoice uses the resolver and writes
    selected_voice + match_confidence into TTSResult."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module
    import services.tts.voice_match_resolver as resolver_module
    from services.tts.voice_match_types import VoiceMatchResult

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []

    # Segment with non-builtin voice_id triggers resolver path
    segment = _build_segment(segment_id=1, voice_id="some_cloned_voice")
    segment.gender = "female"
    segment.age_group = "middle"
    segment.persona_style = "warm"
    segment.energy_level = "medium"

    # Mock resolver to return longanwen_v3 (female, middle, warm)
    monkeypatch.setattr(
        resolver_module,
        "resolve_voice_match",
        lambda req: VoiceMatchResult(
            voice_id="longanwen_v3", match_reason="combined_rerank(female,pool=5)",
            match_score=0.65, match_confidence="high", backup_voices=(),
        ),
    )

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    assert result.selected_voice == "longanwen_v3"
    assert result.match_confidence == "high"
    assert attempts == ["longanwen_v3"]


def test_cosyvoice_explicit_builtin_voice_populates_selected_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that explicit builtin voice_id still populates selected_voice."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    segment = _build_segment(segment_id=1, voice_id="longshu_v3")
    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    assert result.selected_voice == "longshu_v3"
    assert result.match_confidence == "high"


def test_cosyvoice_childlike_segment_routes_to_child_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that childlike segment metadata routes to a child voice."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module
    import services.tts.voice_match_resolver as resolver_module
    from services.tts.voice_match_types import VoiceMatchResult

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []

    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "child"
    segment.age_group = "young"

    monkeypatch.setattr(
        resolver_module,
        "resolve_voice_match",
        lambda req: VoiceMatchResult(
            voice_id="longhuhu_v3", match_reason="combined_rerank(child,pool=3)",
            match_score=0.50, match_confidence="medium", backup_voices=(),
        ),
    )

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    assert result.selected_voice == "longhuhu_v3"
    assert result.match_confidence == "medium"
    assert attempts == ["longhuhu_v3"]


def test_cosyvoice_childlike_inferred_from_voice_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that voice_description containing childlike keywords triggers child routing."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module
    import services.tts.voice_match_resolver as resolver_module
    from services.tts.voice_match_types import VoiceMatchResult

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []
    resolver_reqs: list = []

    # gender=female but voice_description says "小朋友" → should infer childlike
    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "female"
    segment.age_group = "young"
    segment.voice_description = "活泼的小朋友童声"

    def _fake_resolver(req):
        resolver_reqs.append(req)
        return VoiceMatchResult(
            voice_id="longhuhu_v3", match_reason="combined_rerank(child,pool=3)",
            match_score=0.50, match_confidence="medium", backup_voices=(),
        )

    monkeypatch.setattr(resolver_module, "resolve_voice_match", _fake_resolver)

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    # Resolver should receive voice_description for childlike inference
    assert len(resolver_reqs) == 1
    assert resolver_reqs[0].voice_description == "活泼的小朋友童声"
    assert result.selected_voice == "longhuhu_v3"
    assert attempts == ["longhuhu_v3"]


def test_cosyvoice_result_written_back_to_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that selected_voice and match_confidence are written back to segment."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)

    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "female"
    segment.age_group = "middle"
    segment.persona_style = "warm"

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)
    monkeypatch.setattr(tts_generator_module, "get_tts_provider", lambda: "cosyvoice")

    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen.generate_all([segment], str(tmp_path / "tts"))

    # After generate_all, segment should have the fields populated
    assert segment.selected_voice == "longanwen_v3"
    assert segment.match_confidence == "high"


def test_non_childlike_adult_not_affected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that normal adult segments are not affected by childlike inference."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)

    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "male"
    segment.age_group = "elderly"
    segment.voice_description = "沧桑岁月的老人声"

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment, str(tmp_path / "tts"), provider="cosyvoice",
    )

    assert result.selected_voice == "longlaobo_v3"
    assert result.match_confidence == "medium"


def test_cache_hit_preserves_selected_voice_from_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cache-hit path keeps selected_voice/match_confidence from segment."""
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()

    # Pre-create a valid cached wav file
    wav_bytes = _build_wav_bytes(duration_ms=500)
    cached_path = tts_dir / "segment_001_speaker_a.wav"
    cached_path.write_bytes(wav_bytes)

    segment = _build_segment(segment_id=1)
    # Simulate fields already populated from a previous run
    segment.selected_voice = "longanwen_v3"
    segment.match_confidence = "high"

    monkeypatch.setattr(tts_generator_module, "get_tts_provider", lambda: "cosyvoice")

    gen = TTSGenerator(TTSConfig(api_key="secret"))
    results = gen.generate_all([segment], str(tts_dir))

    assert len(results) == 1
    assert results[0].selected_voice == "longanwen_v3"
    assert results[0].match_confidence == "high"
    assert segment.selected_voice == "longanwen_v3"
    assert segment.match_confidence == "high"


def test_cache_hit_derives_from_explicit_builtin_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cache-hit derives selected_voice from explicit builtin voice_id when segment fields are empty."""
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()

    wav_bytes = _build_wav_bytes(duration_ms=500)
    cached_path = tts_dir / "segment_001_speaker_a.wav"
    cached_path.write_bytes(wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="longshu_v3")
    # selected_voice/match_confidence are empty (first resume after upgrade)

    monkeypatch.setattr(tts_generator_module, "get_tts_provider", lambda: "cosyvoice")

    gen = TTSGenerator(TTSConfig(api_key="secret"))
    results = gen.generate_all([segment], str(tts_dir))

    assert results[0].selected_voice == "longshu_v3"
    assert results[0].match_confidence == "high"


# --- VolcEngine dual-mode dispatch tests (B5) ---

def _setup_volcengine_mocks(monkeypatch, wav_bytes):
    """Set up common mocks for volcengine tests: fake synthesize + fake resolver."""
    import services.tts.volcengine_tts_provider as vc_module
    from services.tts.voice_match_types import VoiceMatchResult

    synth_calls: list[dict] = []

    def _fake_synthesize(text, voice_id=None, *, resource_id=None, model=None, **kw):
        synth_calls.append({
            "voice_id": voice_id, "resource_id": resource_id, "model": model,
        })
        return wav_bytes

    monkeypatch.setattr(vc_module, "synthesize", _fake_synthesize)

    resolver_calls: list[dict] = []

    def _fake_resolve(request):
        resolver_calls.append({
            "resource_id": request.resource_id,
            "gender": request.gender,
            "mode": request.mode,
        })
        return VoiceMatchResult(
            voice_id=f"auto_matched_{request.resource_id or 'default'}",
            match_reason="test_auto",
            match_score=0.70,
            match_confidence="medium",
        )

    import services.tts.voice_match_resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_voice_match", _fake_resolve)

    return synth_calls, resolver_calls


def test_volcengine_express_passes_resource_and_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express volcengine: resource=seed-tts-1.0, model=seed-tts-1.1."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, _ = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="zh_female_shuangkuaisisi_moon_bigtts")
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    assert synth_calls[0]["resource_id"] == "seed-tts-1.0"
    assert synth_calls[0]["model"] == "seed-tts-1.1"
    assert result.selected_voice == "zh_female_shuangkuaisisi_moon_bigtts"


def test_volcengine_studio_passes_resource_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Studio volcengine: resource=seed-tts-2.0, model=None."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, _ = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="zh_female_vv_uranus_bigtts")
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "studio", "tts_model": None, "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    assert synth_calls[0]["resource_id"] == "seed-tts-2.0"
    assert synth_calls[0]["model"] is None
    assert result.selected_voice == "zh_female_vv_uranus_bigtts"


def test_volcengine_express_uses_matcher_when_no_explicit_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Express + empty voice_id → shared resolver auto-match for 1.0."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, resolver_calls = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="")
    segment.gender = "male"
    segment.age_group = "middle"
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["resource_id"] == "seed-tts-1.0"
    assert resolver_calls[0]["gender"] == "male"
    assert "auto_matched" in result.selected_voice


def test_volcengine_studio_uses_explicit_selected_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Studio + explicit voice_id → uses it directly, no resolver."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, resolver_calls = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="zh_male_m191_uranus_bigtts")
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "studio", "tts_model": None, "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    # Resolver not called — explicit voice takes priority
    assert len(resolver_calls) == 0
    assert result.selected_voice == "zh_male_m191_uranus_bigtts"
    assert result.match_confidence == "high"


def test_volcengine_studio_auto_uses_matcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Studio + empty voice_id → resolver auto-match for 2.0."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, resolver_calls = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="")
    segment.gender = "female"
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "studio", "tts_model": None, "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["resource_id"] == "seed-tts-2.0"
    assert "auto_matched" in result.selected_voice


def test_volcengine_reads_active_job_record_not_only_default_job_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_active_job_record (set by generate_all) should be used, not just _default_job_record."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, _ = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="zh_female_vv_uranus_bigtts")
    # Default says express, but active says studio
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._default_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}
    gen._active_job_record = {"service_mode": "studio", "tts_model": None, "tts_provider": "volcengine"}

    gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    # Should use studio (active), not express (default)
    assert synth_calls[0]["resource_id"] == "seed-tts-2.0"
    assert synth_calls[0]["model"] is None


def test_volcengine_invalid_speaker_retries_with_resource_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismatch error → retry with resource default voice."""
    import services.tts.volcengine_tts_provider as vc_module
    from services.tts.volcengine_tts_provider import VolcEngineTTSError

    wav_bytes = _build_wav_bytes(duration_ms=600)
    call_count = {"n": 0}

    def _flaky_synthesize(text, voice_id=None, *, resource_id=None, model=None, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise VolcEngineTTSError("VolcEngine TTS error: code=55000000, message=speaker mismatch")
        return wav_bytes

    monkeypatch.setattr(vc_module, "synthesize", _flaky_synthesize)

    # Mock resolver to return a bad voice
    from services.tts.voice_match_types import VoiceMatchResult
    import services.tts.voice_match_resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_voice_match", lambda req: VoiceMatchResult(
        voice_id="zh_male_wrong_voice", match_reason="test", match_score=0.5, match_confidence="low",
    ))

    segment = _build_segment(segment_id=1, voice_id="")
    segment.gender = "male"
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    assert call_count["n"] == 2  # first failed, second succeeded with default
    assert result.selected_voice == vc_module.DEFAULT_SPEAKER_1_0
    assert result.match_confidence == "low"


def test_volcengine_dispatch_wraps_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify non-TTSGenerationError exceptions from volcengine are wrapped."""
    import services.tts.volcengine_tts_provider as vc_module

    def _failing_synthesize(text, voice_id=None, **kw):
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(vc_module, "synthesize", _failing_synthesize)

    # Mock resolver
    from services.tts.voice_match_types import VoiceMatchResult
    import services.tts.voice_match_resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_voice_match", lambda req: VoiceMatchResult(
        voice_id="test", match_reason="test", match_score=0.5, match_confidence="low",
    ))

    segment = _build_segment(segment_id=1, voice_id="")
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}
    with pytest.raises(TTSGenerationError, match="VolcEngine.*connection timeout"):
        gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")


def test_volcengine_studio_auto_string_uses_matcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Studio + voice_id='auto' → treated as no explicit voice, uses resolver."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, resolver_calls = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    segment = _build_segment(segment_id=1, voice_id="auto")
    segment.gender = "female"
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "studio", "tts_model": None, "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    # "auto" should NOT be treated as an explicit voice
    assert len(resolver_calls) == 1
    assert resolver_calls[0]["resource_id"] == "seed-tts-2.0"
    assert "auto_matched" in result.selected_voice


def test_volcengine_incompatible_explicit_voice_uses_matcher_instead_of_direct_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.0 resource + 2.0 voice_id → should NOT send to provider, should use resolver instead."""
    wav_bytes = _build_wav_bytes(duration_ms=600)
    synth_calls, resolver_calls = _setup_volcengine_mocks(monkeypatch, wav_bytes)

    # Segment has a 2.0 voice but job is express (1.0)
    segment = _build_segment(segment_id=1, voice_id="zh_female_vv_uranus_bigtts")
    segment.gender = "female"
    gen = TTSGenerator(TTSConfig(api_key="secret"))
    gen._active_job_record = {"service_mode": "express", "tts_model": "seed-tts-1.1", "tts_provider": "volcengine"}

    result = gen._generate_one(segment, str(tmp_path / "tts"), provider="volcengine")

    # Should have gone through resolver, not direct send
    assert len(resolver_calls) == 1
    assert resolver_calls[0]["resource_id"] == "seed-tts-1.0"
    # The voice sent to provider should be from the resolver (1.0 pool), not the incompatible 2.0 voice
    assert synth_calls[0]["voice_id"] != "zh_female_vv_uranus_bigtts"
    assert "auto_matched" in result.selected_voice


# --- Speaker voice cache tests ---

def test_speaker_cache_reuses_auto_matched_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same speaker_id across segments should get the same auto-matched voice."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)
    voice_selections: list[str] = []

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        voice_selections.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    # Two segments from the same speaker, both with non-builtin voice_id → trigger enhancer
    seg1 = _build_segment(segment_id=1, voice_id="nonexistent")
    seg1.gender = "female"
    seg1.age_group = "middle"
    seg1.persona_style = "warm"

    seg2 = _build_segment(segment_id=2, voice_id="nonexistent")
    seg2.speaker_id = "speaker_a"  # same speaker
    seg2.gender = "female"
    seg2.age_group = "middle"
    seg2.persona_style = "warm"

    gen = TTSGenerator(TTSConfig(api_key="secret"))

    r1 = gen._generate_one(seg1, str(tmp_path / "tts"), provider="cosyvoice")
    r2 = gen._generate_one(seg2, str(tmp_path / "tts"), provider="cosyvoice")

    # Both should use the same voice (cache hit on second call)
    assert r1.selected_voice == r2.selected_voice
    assert voice_selections[0] == voice_selections[1]


def test_speaker_cache_does_not_override_explicit_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit builtin voice_id should NOT be overridden by speaker cache."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)
    voice_selections: list[str] = []

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        voice_selections.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    # First segment: non-builtin → triggers enhancer → cached
    seg1 = _build_segment(segment_id=1, voice_id="nonexistent")
    seg1.gender = "female"
    seg1.age_group = "middle"
    seg1.persona_style = "warm"

    # Second segment: same speaker but with explicit builtin voice
    seg2 = _build_segment(segment_id=2, voice_id="longshu_v3")  # explicit builtin
    seg2.speaker_id = "speaker_a"

    gen = TTSGenerator(TTSConfig(api_key="secret"))

    r1 = gen._generate_one(seg1, str(tmp_path / "tts"), provider="cosyvoice")
    r2 = gen._generate_one(seg2, str(tmp_path / "tts"), provider="cosyvoice")

    # seg2 should use its explicit voice_id, not the cached auto-match
    assert r2.selected_voice == "longshu_v3"
    assert r2.match_confidence == "high"
    # seg1 used auto-matched voice (enhancer)
    assert r1.selected_voice != "longshu_v3"


def test_speaker_cache_cleared_between_generate_all_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speaker cache should be reset for each generate_all invocation."""
    gen = TTSGenerator(TTSConfig(api_key="secret"))

    # Manually populate the cache
    gen._speaker_voice_cache["speaker_a"] = ("longanyang", "high")
    assert "speaker_a" in gen._speaker_voice_cache

    # Calling generate_all should clear the cache (even if it returns early)
    monkeypatch.setattr(tts_generator_module, "get_tts_provider", lambda: "cosyvoice")
    try:
        gen.generate_all([], str(tmp_path / "tts"))
    except Exception:
        pass

    assert gen._speaker_voice_cache == {}


def test_different_speakers_get_different_cached_voices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different speaker_ids should get independently cached voices."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    # Speaker A: female middle warm → longanwen_v3
    seg_a1 = _build_segment(segment_id=1, voice_id="nonexistent")
    seg_a1.speaker_id = "speaker_a"
    seg_a1.gender = "female"
    seg_a1.age_group = "middle"
    seg_a1.persona_style = "warm"

    # Speaker B: male middle serious → longanzhi_v3
    seg_b1 = _build_segment(segment_id=2, voice_id="nonexistent")
    seg_b1.speaker_id = "speaker_b"
    seg_b1.gender = "male"
    seg_b1.age_group = "middle"
    seg_b1.persona_style = "serious"

    # Second segments from each speaker
    seg_a2 = _build_segment(segment_id=3, voice_id="nonexistent")
    seg_a2.speaker_id = "speaker_a"
    seg_a2.gender = "female"
    seg_a2.age_group = "middle"
    seg_a2.persona_style = "warm"

    seg_b2 = _build_segment(segment_id=4, voice_id="nonexistent")
    seg_b2.speaker_id = "speaker_b"
    seg_b2.gender = "male"
    seg_b2.age_group = "middle"
    seg_b2.persona_style = "serious"

    gen = TTSGenerator(TTSConfig(api_key="secret"))

    r_a1 = gen._generate_one(seg_a1, str(tmp_path / "tts"), provider="cosyvoice")
    r_b1 = gen._generate_one(seg_b1, str(tmp_path / "tts"), provider="cosyvoice")
    r_a2 = gen._generate_one(seg_a2, str(tmp_path / "tts"), provider="cosyvoice")
    r_b2 = gen._generate_one(seg_b2, str(tmp_path / "tts"), provider="cosyvoice")

    # Same speaker → same voice (cache)
    assert r_a1.selected_voice == r_a2.selected_voice
    assert r_b1.selected_voice == r_b2.selected_voice
    # Different speakers → different voices
    assert r_a1.selected_voice != r_b1.selected_voice

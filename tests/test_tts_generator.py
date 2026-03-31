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
    tts_cn_text: str | None = None,
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
        tts_cn_text=cn_text if tts_cn_text is None else tts_cn_text,
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
    _install_fake_requests(
        monkeypatch,
        [{"status_code": 500, "payload": {}}],
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
    calls = _install_fake_requests(
        monkeypatch,
        [
            {
                "status_code": 200,
                "payload": {
                    "base_resp": {"status_code": 1039, "status_msg": "invalid voice_id"},
                    "data": {"audio": ""},
                },
            }
        ],
    )

    with pytest.raises(TTSGenerationError, match="invalid voice_id"):
        TTSGenerator(TTSConfig(api_key="secret")).generate_all(
            [_build_segment(segment_id=1)],
            str(tmp_path / "tts"),
        )
    assert len(calls) == 1


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


def test_tts_generator_prefers_tts_cn_text_over_cn_text(
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
        cn_text="字幕文本",
        tts_cn_text="配音文本",
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
    assert "[S4] TTS进度：5/6 段" in captured
    assert "[S4] TTS进度：6/6 段" in captured


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
    import services.tts.cosyvoice_voice_selector as cosyvoice_voice_selector_module
    import services.tts.cosyvoice_instruction_enhancer as enhancer_module
    from services.tts.cosyvoice_instruction_enhancer import EnhancedVoiceResult

    wav_bytes = _build_wav_bytes(duration_ms=700)
    attempts: list[str] = []
    segment = _build_segment(segment_id=1)
    segment.gender = "male"
    segment.age_group = "middle"
    segment.persona_style = ""
    segment.energy_level = ""

    # Mock the enhancer to return longsanshu_v3 (which will be rejected by the provider)
    monkeypatch.setattr(
        enhancer_module,
        "enhance_voice_selection",
        lambda **kw: EnhancedVoiceResult(
            voice_id="longsanshu_v3", match_reason="test", match_score=0.60,
            match_confidence="medium", backup_voices=(), instruction=None, instruct_supported=False,
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
    import services.tts.cosyvoice_instruction_enhancer as enhancer_module

    wav_bytes = _build_wav_bytes(duration_ms=650)
    attempts: list[str] = []
    segment = _build_segment(segment_id=1, voice_id="longshu_v3")
    segment.gender = "female"
    segment.age_group = "young"
    segment.persona_style = "energetic"
    segment.energy_level = "high"

    def _unexpected_enhancer(**kwargs):
        raise AssertionError("explicit builtin cosyvoice voice_id should bypass enhancer")

    def _fake_cosyvoice_synthesize(*, text: str, voice: str, model: str = "cosyvoice-v3-flash", api_key=None) -> bytes:
        del text, model, api_key
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(enhancer_module, "enhance_voice_selection", _unexpected_enhancer)
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


def test_cosyvoice_enhancer_path_populates_selected_voice_and_confidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that _generate_one_cosyvoice uses the enhancer and writes
    selected_voice + match_confidence into TTSResult."""
    import services.tts.cosyvoice_provider as cosyvoice_provider_module

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []

    # Segment with non-builtin voice_id triggers enhancer path
    segment = _build_segment(segment_id=1, voice_id="some_cloned_voice")
    segment.gender = "female"
    segment.age_group = "middle"
    segment.persona_style = "warm"
    segment.energy_level = "medium"

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    # Enhancer should select longanwen_v3 (female, middle, warm → style override)
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

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []

    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "child"
    segment.age_group = "young"

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

    wav_bytes = _build_wav_bytes(duration_ms=600)
    attempts: list[str] = []

    # gender=female but voice_description says "小朋友" → should infer childlike
    segment = _build_segment(segment_id=1, voice_id="nonexistent")
    segment.gender = "female"
    segment.age_group = "young"
    segment.voice_description = "活泼的小朋友童声"

    def _fake_synthesize(*, text, voice, model="cosyvoice-v3-flash", api_key=None):
        attempts.append(voice)
        return wav_bytes

    monkeypatch.setattr(cosyvoice_provider_module, "synthesize", _fake_synthesize)

    result = TTSGenerator(TTSConfig(api_key="secret"))._generate_one(
        segment,
        str(tmp_path / "tts"),
        provider="cosyvoice",
    )

    # infer_is_childlike("young", "活泼的小朋友童声") → True → routes to child voice
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

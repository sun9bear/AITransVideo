import json

import pytest

from pipeline.process import ProcessPipeline
from services.gemini.translator import DubbingSegment


def _segment(
    *,
    segment_id: int,
    text: str,
    first_pass_duration_ms: int,
    voice_id: str = "clone_voice_a",
    dsp_speed_param: float = 1.0,
    rewrite_count: int = 0,
    first_pass_text: str | None = None,
    tts_model_key: str = "",
) -> DubbingSegment:
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id=voice_id,
        start_ms=0,
        end_ms=first_pass_duration_ms,
        target_duration_ms=first_pass_duration_ms,
        source_text="hello world",
        cn_text=text,
        selected_voice=voice_id,
        tts_provider="minimax",
        first_pass_cn_text=first_pass_text if first_pass_text is not None else text,
        tts_model_key=tts_model_key,
        first_pass_duration_ms=first_pass_duration_ms,
        actual_duration_ms=first_pass_duration_ms,
        dsp_speed_param=dsp_speed_param,
        rewrite_count=rewrite_count,
    )


def test_build_user_voice_speed_profiles_uses_first_pass_text_and_speed() -> None:
    first_pass_text = "你好世界" * 25  # 100 spoken chars
    rewritten_text = "你好世界" * 5
    segments = [
        _segment(
            segment_id=1,
            text=rewritten_text,
            first_pass_text=first_pass_text,
            first_pass_duration_ms=20_000,
            dsp_speed_param=1.25,
            rewrite_count=1,
        ),
        _segment(
            segment_id=2,
            text=rewritten_text,
            first_pass_text=first_pass_text,
            first_pass_duration_ms=20_000,
            dsp_speed_param=1.25,
            rewrite_count=1,
        ),
    ]

    profiles, skipped = ProcessPipeline._build_user_voice_speed_profiles(
        segments,
        default_provider="minimax",
        tts_model="speech-2.8-hd",
    )

    assert skipped == {}
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["voice_id"] == "clone_voice_a"
    assert profile["tts_provider"] == "minimax"
    assert profile["model_key"] == "speech-2.8-hd"
    assert profile["sample_count"] == 2
    assert profile["spoken_chars"] == 200
    assert profile["natural_duration_ms"] == 50_000
    assert profile["chars_per_second"] == pytest.approx(4.0)


def test_build_user_voice_speed_profiles_prefers_segment_model_key() -> None:
    text = "你好世界" * 25
    segments = [
        _segment(
            segment_id=1,
            text=text,
            first_pass_duration_ms=25_000,
            tts_model_key="speech-2.8-turbo",
        ),
        _segment(
            segment_id=2,
            text=text,
            first_pass_duration_ms=25_000,
            tts_model_key="speech-2.8-turbo",
        ),
    ]

    profiles, _ = ProcessPipeline._build_user_voice_speed_profiles(
        segments,
        default_provider="minimax",
        tts_model="speech-2.8-hd",
    )

    assert profiles[0]["model_key"] == "speech-2.8-turbo"


def test_build_user_voice_speed_profiles_rejects_rewritten_segments_without_first_pass_text() -> None:
    segments = [
        _segment(
            segment_id=1,
            text="你好世界" * 30,
            first_pass_text="",
            first_pass_duration_ms=20_000,
            rewrite_count=1,
        ),
        _segment(
            segment_id=2,
            text="你好世界" * 30,
            first_pass_text="",
            first_pass_duration_ms=20_000,
            rewrite_count=1,
        ),
    ]

    profiles, skipped = ProcessPipeline._build_user_voice_speed_profiles(segments)

    assert profiles == []
    assert skipped["missing_first_pass_text"] == 2


def test_persist_user_voice_speed_profiles_posts_internal_payload(monkeypatch) -> None:
    calls = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "updated_count": 1,
                "skipped_count": 1,
                "skipped": [{"voice_id": "system_voice", "reason": "missing_user_voice"}],
            }).encode("utf-8")

    def _fake_urlopen(req, timeout):
        calls["url"] = req.full_url
        calls["headers"] = dict(req.header_items())
        calls["body"] = json.loads(req.data.decode("utf-8"))
        calls["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("AVT_GATEWAY_URL", "http://gateway.local")
    monkeypatch.setenv("AVT_INTERNAL_API_KEY", "secret-key")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    summary = ProcessPipeline._persist_user_voice_speed_profiles(
        job_id="job_1",
        user_id="00000000-0000-0000-0000-000000000001",
        profiles=[{
            "voice_id": "clone_voice_a",
            "tts_provider": "minimax",
            "model_key": "speech-2.8-hd",
            "chars_per_second": 4.0,
            "sample_count": 2,
            "spoken_chars": 200,
            "natural_duration_ms": 50_000,
        }],
        skipped_reasons={"insufficient_samples": 3},
    )

    assert calls["url"] == "http://gateway.local/internal/user-voices/speed-profiles"
    assert calls["headers"]["X-internal-key"] == "secret-key"
    assert calls["body"]["job_id"] == "job_1"
    assert calls["body"]["profiles"][0]["voice_id"] == "clone_voice_a"
    assert calls["timeout"] == 5
    assert summary["voice_speed_profile_candidate_count"] == 1
    assert summary["voice_speed_profile_sent_count"] == 1
    assert summary["voice_speed_profile_updated_count"] == 1
    assert summary["voice_speed_profile_skipped_count"] == 4
    assert summary["voice_speed_profile_skipped_reason_distribution"] == {
        "insufficient_samples": 3,
        "missing_user_voice": 1,
    }

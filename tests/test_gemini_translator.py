import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.assemblyai.transcriber import TranscriptLine
from services.gemini.translator import GeminiTranslator, TranslationError, load_gemini_config
import services.gemini.translator as gemini_translator_module


def _make_line(
    index: int,
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    speaker_id: str = "speaker_a",
    speaker_label: str = "A",
) -> TranscriptLine:
    return TranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_label=speaker_label,
        source_text=text,
    )


def _make_review_lines() -> list[TranscriptLine]:
    return [
        _make_line(1, 0, 800, "Welcome back.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(2, 800, 1_600, "Thanks for having me.", speaker_id="speaker_b", speaker_label="B"),
        _make_line(3, 1_600, 2_000, "Yeah.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(4, 2_000, 2_800, "Let's dig in.", speaker_id="speaker_b", speaker_label="B"),
        _make_line(5, 2_800, 3_600, "Sounds good.", speaker_id="speaker_a", speaker_label="A"),
    ]


def _build_translator(
    *,
    model_name: str = "gemini-3.1-pro-preview",
    sdk_backend: str = "google-genai",
    llm_router=None,
    speaker_infer_prompt_template: str | None = None,
    translation_prompt_template: str | None = None,
) -> GeminiTranslator:
    return GeminiTranslator(
        api_key="test_key",
        model_name=model_name,
        sdk_backend=sdk_backend,
        llm_router=llm_router,
        speaker_infer_prompt_template=speaker_infer_prompt_template,
        translation_prompt_template=translation_prompt_template,
        _skip_init=True,
    )


def _extract_groups_from_prompt(prompt: str) -> list[dict]:
    marker_start = "输入（JSON数组）：\n"
    marker_end = "\n\n请输出JSON数组"
    start_index = prompt.index(marker_start) + len(marker_start)
    end_index = prompt.index(marker_end, start_index)
    return json.loads(prompt[start_index:end_index])


def test_gemini_translator_translates_single_speaker_lines_into_one_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 1_000, "Hello there."),
        _make_line(2, 1_000, 2_000, "This is a test."),
        _make_line(3, 2_000, 3_000, "We are building something useful."),
    ]

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: json.dumps(
            [{"segment_id": 1, "cn_text": "大家好，这是一个测试，我们在做个有用的东西。"}],
            ensure_ascii=False,
        ),
    )

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert translator.model_name == "gemini-3.1-pro-preview"
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.speaker_id == "speaker_a"
    assert segment.voice_id == "voice_demo_001"
    assert segment.cn_text != ""
    assert segment.tts_cn_text == segment.cn_text
    assert segment.start_ms == 0
    assert segment.end_ms == 3_000
    assert segment.target_duration_ms == 3_000


def test_gemini_translator_splits_groups_when_total_duration_exceeds_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 30_000, "Part one."),
        _make_line(2, 30_000, 60_000, "Part two."),
        _make_line(3, 60_000, 90_000, "Part three."),
    ]

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        assert json_mode is False
        groups = _extract_groups_from_prompt(prompt)
        assert len(groups) == 2
        assert groups[0]["start_ms"] == 0
        assert groups[0]["end_ms"] == 60_000
        assert groups[0]["target_duration_ms"] == 60_000
        assert groups[0]["target_duration_seconds"] == 60.0
        assert groups[0]["target_chars"] == 270
        assert groups[0]["min_chars"] == 229
        assert groups[0]["max_chars"] == 310
        assert groups[1]["start_ms"] == 60_000
        assert groups[1]["end_ms"] == 90_000
        assert groups[1]["target_duration_ms"] == 30_000
        assert groups[1]["target_duration_seconds"] == 30.0
        assert groups[1]["target_chars"] == 135
        assert groups[1]["min_chars"] == 114
        assert groups[1]["max_chars"] == 155
        return json.dumps(
            [
                {"segment_id": 1, "cn_text": "第一部分和第二部分。"},
                {"segment_id": 2, "cn_text": "第三部分。"},
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
        max_segment_duration_ms=60_000,
    )

    assert len(result.segments) == 2
    assert result.segments[0].end_ms <= result.segments[1].start_ms
    assert result.segments[0].start_ms == 0
    assert result.segments[0].end_ms == 60_000
    assert result.segments[1].start_ms == 60_000
    assert result.segments[1].end_ms == 90_000


def test_gemini_translator_prefers_long_pause_boundaries_within_same_speaker() -> None:
    lines = [
        _make_line(1, 0, 8_000, "First point.", speaker_id="speaker_a"),
        _make_line(2, 11_500, 18_000, "Second point after a long pause.", speaker_id="speaker_a"),
        _make_line(3, 18_000, 24_000, "Immediate follow-up.", speaker_id="speaker_a"),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=90_000)

    assert len(groups) == 2
    assert groups[0]["start_ms"] == 0
    assert groups[0]["end_ms"] == 8_000
    assert groups[0]["target_duration_ms"] == 8_000
    assert groups[1]["start_ms"] == 11_500
    assert groups[1]["end_ms"] == 24_000
    assert groups[1]["target_duration_ms"] == 12_500


def test_gemini_translator_keeps_short_same_speaker_pause_in_one_group() -> None:
    lines = [
        _make_line(1, 0, 8_000, "First point.", speaker_id="speaker_a"),
        _make_line(2, 10_500, 18_000, "Second point after a short pause.", speaker_id="speaker_a"),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=90_000)

    assert len(groups) == 1
    assert groups[0]["start_ms"] == 0
    assert groups[0]["end_ms"] == 18_000
    assert groups[0]["target_duration_ms"] == 18_000


def test_gemini_translator_translates_ten_groups_in_two_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(index + 1, index * 60_000, (index + 1) * 60_000, f"Part {index + 1}.")
        for index in range(10)
    ]
    seen_group_counts: list[int] = []

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        groups = _extract_groups_from_prompt(prompt)
        seen_group_counts.append(len(groups))
        return json.dumps(
            [
                {"segment_id": group["segment_id"], "cn_text": f"第{group['segment_id']}段翻译"}
                for group in groups
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert seen_group_counts == [5, 5]
    assert len(result.segments) == 10
    assert [segment.segment_id for segment in result.segments] == list(range(1, 11))


def test_gemini_translator_call_with_retry_uses_google_genai_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    attempts = {"count": 0}
    captured_configs: list[object] = []

    class FakeGenerateContentConfig:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeModels:
        def generate_content(self, *, model: str, contents: str, config: object):
            attempts["count"] += 1
            assert model == "gemini-3.1-pro-preview"
            assert contents == "prompt text"
            captured_configs.append(config)
            if attempts["count"] == 1:
                raise RuntimeError("gateway timeout")
            return SimpleNamespace(text='[{"segment_id": 1, "cn_text": "重试成功"}]')

    translator.client = SimpleNamespace(models=FakeModels())
    translator._types_module = SimpleNamespace(GenerateContentConfig=FakeGenerateContentConfig)
    monkeypatch.setattr(gemini_translator_module.time, "sleep", lambda seconds: None)

    response_text = translator._call_gemini_with_retry("prompt text", json_mode=True)

    assert attempts["count"] == 2
    assert response_text == '[{"segment_id": 1, "cn_text": "重试成功"}]'
    assert captured_configs[-1].kwargs["response_mime_type"] == "application/json"
    assert captured_configs[-1].kwargs["http_options"] == {"timeout": 120000}
    assert captured_configs[-1].kwargs["temperature"] == 0.3
    assert captured_configs[-1].kwargs["max_output_tokens"] == 8192


def test_gemini_translator_supports_two_speaker_translation_and_voice_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 10_000, "Host intro.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(2, 10_000, 20_000, "Guest reply.", speaker_id="speaker_b", speaker_label="B"),
        _make_line(3, 20_000, 30_000, "Host follow-up.", speaker_id="speaker_a", speaker_label="A"),
    ]

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        groups = _extract_groups_from_prompt(prompt)
        assert [group["speaker_id"] for group in groups] == ["speaker_a", "speaker_b", "speaker_a"]
        return json.dumps(
            [
                {"segment_id": 1, "cn_text": "主持人开场。"},
                {"segment_id": 2, "cn_text": "嘉宾回应。"},
                {"segment_id": 3, "cn_text": "主持人追问。"},
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_a_001",
        display_name="Host",
        voice_id_b="voice_b_001",
        display_name_b="Guest",
    )

    assert len(result.segments) == 3
    assert result.segments[0].speaker_id == "speaker_a"
    assert result.segments[0].voice_id == "voice_a_001"
    assert result.segments[0].display_name == "Host"
    assert result.segments[0].tts_cn_text == result.segments[0].cn_text
    assert result.segments[1].speaker_id == "speaker_b"
    assert result.segments[1].voice_id == "voice_b_001"
    assert result.segments[1].display_name == "Guest"
    assert result.segments[1].tts_cn_text == result.segments[1].cn_text
    assert result.segments[2].speaker_id == "speaker_a"
    assert result.segments[2].voice_id == "voice_a_001"


def test_gemini_translator_infers_speaker_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 1_000, "Welcome back, Dan.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(2, 1_000, 2_000, "Thanks for having me.", speaker_id="speaker_b", speaker_label="B"),
    ]
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: json.dumps(
            {"speaker_a": "Interviewer", "speaker_b": "Dan Koe"},
            ensure_ascii=False,
        ),
    )

    inferred_names = translator.infer_speaker_names(lines, num_speakers=2)

    assert inferred_names == {"speaker_a": "Interviewer", "speaker_b": "Dan Koe"}


def test_gemini_translator_infer_speaker_names_falls_back_on_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 1_000, "Welcome back, Dan.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(2, 1_000, 2_000, "Thanks for having me.", speaker_id="speaker_b", speaker_label="B"),
    ]
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "not json",
    )

    inferred_names = translator.infer_speaker_names(lines, num_speakers=2)

    assert inferred_names == {"speaker_a": "Speaker A", "speaker_b": "Speaker B"}


def test_gemini_translator_infer_single_speaker_name_uses_video_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 1_000, "I spent years writing online about leverage and focus."),
        _make_line(2, 1_000, 2_000, "This channel is about building a life around your work."),
    ]
    observed: dict[str, object] = {}

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        observed["prompt"] = prompt
        assert json_mode is False
        return json.dumps({"speaker_a": "Dan Koe"}, ensure_ascii=False)

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)

    inferred_names = translator.infer_speaker_names(
        lines,
        num_speakers=1,
        video_title="Dan Koe: How to Think",
        youtube_url="https://youtube.example/watch?v=dan-koe-demo",
        video_description="Dan Koe shares ideas about leverage, writing, and one-person businesses.",
    )

    assert inferred_names == {"speaker_a": "Dan Koe"}
    assert "[Video title]: Dan Koe: How to Think" in observed["prompt"]
    assert "[Video URL]: https://youtube.example/watch?v=dan-koe-demo" in observed["prompt"]
    assert (
        "[Video description]:\nDan Koe shares ideas about leverage, writing, and one-person businesses."
        in observed["prompt"]
    )


def test_gemini_translator_review_speaker_labels_applies_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = _make_review_lines()
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: json.dumps(
            [{"index": 3, "corrected_speaker_id": "speaker_b", "reason": "短促回应"}],
            ensure_ascii=False,
        ),
    )

    reviewed_lines = translator.review_speaker_labels(
        lines,
        {"speaker_a": "Host", "speaker_b": "Guest"},
    )

    assert reviewed_lines[2].speaker_id == "speaker_b"
    assert reviewed_lines[2].speaker_label == "B"
    assert reviewed_lines[0] == lines[0]
    assert reviewed_lines[1] == lines[1]
    assert reviewed_lines[3] == lines[3]
    assert reviewed_lines[4] == lines[4]


def test_gemini_translator_review_speaker_labels_keeps_original_when_no_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = _make_review_lines()
    monkeypatch.setattr(translator, "_call_gemini_with_retry", lambda prompt, json_mode=False: "[]")

    reviewed_lines = translator.review_speaker_labels(
        lines,
        {"speaker_a": "Host", "speaker_b": "Guest"},
    )

    assert reviewed_lines == lines


def test_gemini_translator_review_speaker_labels_degrades_on_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = _make_review_lines()
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "I cannot review this transcript",
    )

    reviewed_lines = translator.review_speaker_labels(
        lines,
        {"speaker_a": "Host", "speaker_b": "Guest"},
    )

    assert reviewed_lines == lines


def test_gemini_translator_retries_same_non_gemini_alias_once_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    monkeypatch.setattr(gemini_translator_module.time, "sleep", lambda seconds: None)
    calls: list[str] = []

    class FakeRouter:
        def get_route(self, task: str) -> list[str]:
            assert task == "s3_translate"
            return ["deepseek_chat", "gpt_41"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            return {"provider": "deepseek" if alias == "deepseek_chat" else "openai"}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del prompt, json_mode
            calls.append(alias)
            if alias == "deepseek_chat" and calls.count("deepseek_chat") == 1:
                raise gemini_translator_module.LLMProviderError(
                    "DeepSeek request failed: HTTPSConnectionPool(host='api.deepseek.com', port=443): SSLError"
                )
            return "重试成功"

    translator.llm_router = FakeRouter()

    response = translator._call_task_with_fallback("s3_translate", "prompt")

    assert response == "重试成功"
    assert calls == ["deepseek_chat", "deepseek_chat"]


def test_gemini_translator_falls_back_immediately_for_non_transient_non_gemini_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    monkeypatch.setattr(gemini_translator_module.time, "sleep", lambda seconds: None)
    calls: list[str] = []

    class FakeRouter:
        def get_route(self, task: str) -> list[str]:
            assert task == "s3_translate"
            return ["deepseek_chat", "gpt_41"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            return {"provider": "deepseek" if alias == "deepseek_chat" else "openai"}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del prompt, json_mode
            calls.append(alias)
            if alias == "deepseek_chat":
                raise gemini_translator_module.LLMProviderError(
                    "DeepSeek request failed: 400 invalid_request_error"
                )
            return "fallback success"

    translator.llm_router = FakeRouter()

    response = translator._call_task_with_fallback("s3_translate", "prompt")

    assert response == "fallback success"
    assert calls == ["deepseek_chat", "gpt_41"]


def test_gemini_translator_review_speaker_labels_ignores_invalid_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = _make_review_lines()
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: json.dumps(
            [
                {"index": 99, "corrected_speaker_id": "speaker_b"},
                {"index": "abc", "corrected_speaker_id": "speaker_a"},
            ],
            ensure_ascii=False,
        ),
    )

    reviewed_lines = translator.review_speaker_labels(
        lines,
        {"speaker_a": "Host", "speaker_b": "Guest"},
    )

    assert reviewed_lines == lines


def test_gemini_translator_parse_response_removes_markdown_code_fence() -> None:
    translator = _build_translator()
    groups = [{"segment_id": 1, "speaker_id": "speaker_a", "start_ms": 0, "end_ms": 1000, "source_text": "Hello"}]

    parsed = translator._parse_response("```json\n[{\"segment_id\": 1, \"cn_text\": \"你好\"}]\n```", groups)

    assert parsed == [{"segment_id": 1, "cn_text": "你好"}]


def test_gemini_translator_raises_on_invalid_json_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [_make_line(1, 0, 1_000, "Hello there.")]
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "I cannot translate this",
    )

    with pytest.raises(TranslationError, match="invalid JSON"):
        translator.translate(
            lines,
            str(tmp_path / "translation"),
            voice_id="voice_demo_001",
        )


def test_gemini_translator_writes_segments_json_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(1, 0, 1_000, "Hello there."),
        _make_line(2, 1_000, 2_000, "This is a test."),
        _make_line(3, 2_000, 3_000, "We are building something useful."),
    ]
    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: json.dumps(
            [{"segment_id": 1, "cn_text": "大家好，这是一个测试。"}],
            ensure_ascii=False,
        ),
    )

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    output_path = Path(result.output_path)
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert len(payload["segments"]) == 1
    assert payload["total_segments"] == 1
    assert payload["segments"][0]["voice_id"] == "voice_demo_001"
    assert payload["segments"][0]["tts_cn_text"] == payload["segments"][0]["cn_text"]


def test_gemini_translator_preserves_checkpoint_after_partial_batch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(index + 1, index * 60_000, (index + 1) * 60_000, f"Part {index + 1}.")
        for index in range(10)
    ]
    observed_calls = {"count": 0}

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        observed_calls["count"] += 1
        groups = _extract_groups_from_prompt(prompt)
        if observed_calls["count"] == 1:
            return json.dumps(
                [
                    {"segment_id": group["segment_id"], "cn_text": f"第{group['segment_id']}段翻译"}
                    for group in groups
                ],
                ensure_ascii=False,
            )
        raise TranslationError("batch failed")

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    with pytest.raises(TranslationError, match="batch failed"):
        translator.translate(
            lines,
            str(tmp_path / "translation"),
            voice_id="voice_demo_001",
        )

    checkpoint_path = tmp_path / "translation" / "segments.checkpoint.json"
    assert checkpoint_path.exists()
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert payload["completed_batches"] == 1
    assert payload["total_groups"] == 10
    assert [item["segment_id"] for item in payload["translated_items"]] == [1, 2, 3, 4, 5]
    assert not (tmp_path / "translation" / "segments.json").exists()


def test_gemini_translator_resumes_from_checkpoint_and_removes_it_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(index + 1, index * 60_000, (index + 1) * 60_000, f"Part {index + 1}.")
        for index in range(10)
    ]
    groups = gemini_translator_module._build_groups(
        translator._pre_split_long_lines(lines),
        max_segment_duration_ms=60_000,
    )
    checkpoint_path = tmp_path / "translation" / "segments.checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = translator._build_translation_fingerprint(
        groups,
        video_title="",
        youtube_url="",
    )
    translator._write_translation_checkpoint(
        checkpoint_path,
        fingerprint=fingerprint,
        translated_items=[
            {"segment_id": index, "cn_text": f"已缓存第{index}段"}
            for index in range(1, 6)
        ],
        total_groups=len(groups),
    )

    seen_batch_segment_ids: list[list[int]] = []

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        groups_in_prompt = _extract_groups_from_prompt(prompt)
        seen_batch_segment_ids.append([group["segment_id"] for group in groups_in_prompt])
        return json.dumps(
            [
                {"segment_id": group["segment_id"], "cn_text": f"第{group['segment_id']}段新翻译"}
                for group in groups_in_prompt
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert seen_batch_segment_ids == [[6, 7, 8, 9, 10]]
    assert len(result.segments) == 10
    assert result.segments[0].cn_text == "已缓存第1段"
    assert result.segments[-1].cn_text == "第10段新翻译"
    assert not checkpoint_path.exists()
    assert Path(result.output_path).exists()


def test_gemini_translator_ignores_checkpoint_when_fingerprint_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(index + 1, index * 60_000, (index + 1) * 60_000, f"Part {index + 1}.")
        for index in range(10)
    ]
    checkpoint_path = tmp_path / "translation" / "segments.checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 1,
                "input_fingerprint": "stale-fingerprint",
                "translated_items": [{"segment_id": index, "cn_text": "旧缓存"} for index in range(1, 6)],
                "completed_batches": 1,
                "total_groups": 10,
                "updated_at": "2026-03-16T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    seen_batch_segment_ids: list[list[int]] = []

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        groups = _extract_groups_from_prompt(prompt)
        seen_batch_segment_ids.append([group["segment_id"] for group in groups])
        return json.dumps(
            [
                {"segment_id": group["segment_id"], "cn_text": f"第{group['segment_id']}段翻译"}
                for group in groups
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert seen_batch_segment_ids == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]]
    assert not checkpoint_path.exists()


def test_gemini_translator_ignores_invalid_checkpoint_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [
        _make_line(index + 1, index * 60_000, (index + 1) * 60_000, f"Part {index + 1}.")
        for index in range(10)
    ]
    checkpoint_path = tmp_path / "translation" / "segments.checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text("{invalid json", encoding="utf-8")
    observed_calls = {"count": 0}

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del prompt, json_mode
        observed_calls["count"] += 1
        return json.dumps(
            [{"segment_id": observed_calls["count"], "cn_text": "占位"}],
            ensure_ascii=False,
        )

    def fake_parse(response_text: str, groups: list[dict]) -> list[dict]:
        del response_text
        return [
            {"segment_id": group["segment_id"], "cn_text": f"第{group['segment_id']}段翻译"}
            for group in groups
        ]

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)
    monkeypatch.setattr(translator, "_parse_response", fake_parse)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert observed_calls["count"] == 2
    assert not checkpoint_path.exists()


def test_gemini_translator_pre_splits_long_lines_and_reindexes() -> None:
    translator = _build_translator()
    long_text = " ".join(["This is a very long sentence for testing."] * 140)
    lines = [
        _make_line(
            99,
            0,
            300_000,
            long_text,
            speaker_id="speaker_b",
            speaker_label="B",
        )
    ]

    split_lines = translator._pre_split_long_lines(lines)

    assert len(split_lines) > 1
    assert [line.index for line in split_lines] == list(range(1, len(split_lines) + 1))
    assert split_lines[0].start_ms == 0
    assert split_lines[-1].end_ms == 300_000
    assert all(
        current.end_ms <= next_line.start_ms
        for current, next_line in zip(split_lines, split_lines[1:])
    )
    assert all(line.speaker_id == "speaker_b" for line in split_lines)


def test_gemini_translator_pre_split_merges_too_short_subline_fragments() -> None:
    translator = _build_translator()
    long_text = (
        "Yes. No. Sure. "
        + " ".join(["This is a long explanation that keeps going."] * 18)
    )
    lines = [_make_line(1, 0, 90_000, long_text)]

    split_lines = translator._pre_split_long_lines(
        lines,
        max_line_duration_ms=15_000,
        max_line_chars=150,
        min_subline_duration_ms=1_500,
    )

    assert len(split_lines) > 1
    assert all((line.end_ms - line.start_ms) >= 1_500 for line in split_lines)


def test_gemini_translator_translate_prompt_includes_video_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [_make_line(1, 0, 1_000, "Hello there.")]

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        assert json_mode is False
        assert "标题：Demo Video" in prompt
        assert "来源：https://youtube.example/watch?v=demo" in prompt
        assert "target_duration_seconds" in prompt
        assert "min_chars ~ max_chars" in prompt
        assert "Elon Musk -> 埃隆·马斯克" in prompt
        assert "Sam Altman -> 萨姆·奥特曼" in prompt
        assert "Naval Ravikant -> 纳瓦尔·拉维坎特" in prompt
        assert "公司、产品、品牌、模型名称" in prompt
        groups = _extract_groups_from_prompt(prompt)
        assert groups[0]["target_duration_ms"] == 1_000
        assert groups[0]["target_duration_seconds"] == 1.0
        assert groups[0]["target_chars"] == 4
        assert groups[0]["min_chars"] == 3
        assert groups[0]["max_chars"] == 4
        return json.dumps(
            [{"segment_id": 1, "cn_text": "你好"}],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
        video_title="Demo Video",
        youtube_url="https://youtube.example/watch?v=demo",
    )

    assert result.total_segments == 1


def test_gemini_translator_build_prompt_mentions_soft_duration_constraints_and_name_rules() -> None:
    translator = _build_translator()

    prompt = translator._build_prompt(
        [
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "start_ms": 0,
                "end_ms": 20_000,
                "target_duration_ms": 20_000,
                "target_duration_seconds": 20.0,
                "target_chars": 90,
                "min_chars": 76,
                "max_chars": 103,
                "source_text": "Sam Altman spoke with Elon Musk about OpenAI.",
            }
        ],
        video_title="Demo Video",
        youtube_url="https://youtube.example/watch?v=demo",
    )

    assert "这些翻译将直接用于中文 TTS 配音" in prompt
    assert "字数范围是软约束" in prompt
    assert "target_duration_seconds：目标配音时长（秒）" in prompt
    assert "target_chars：按 4.5 字/秒估算的目标中文字数" in prompt
    assert "所有人物姓名必须优先使用中文常见译名" in prompt
    assert "公司、产品、品牌、模型名称若已有常见中文译法" in prompt
    assert "可适度保留原文中的口语连接词、语气词和缓冲表达" in prompt


def test_gemini_translator_build_prompt_supports_custom_template_tokens() -> None:
    translator = _build_translator(
        translation_prompt_template=(
            "标题=__VIDEO_TITLE__\n"
            "链接=__YOUTUBE_URL__\n"
            "__SPEAKER_INSTRUCTION__"
            "__STRICT_LENGTH_INSTRUCTION__"
            "数据:\n__GROUPS_JSON__"
        )
    )

    prompt = translator._build_prompt(
        [
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "start_ms": 0,
                "end_ms": 20_000,
                "target_duration_ms": 20_000,
                "target_duration_seconds": 20.0,
                "target_chars": 90,
                "min_chars": 76,
                "max_chars": 103,
                "source_text": "Hello there.",
            },
            {
                "segment_id": 2,
                "speaker_id": "speaker_b",
                "start_ms": 20_000,
                "end_ms": 40_000,
                "target_duration_ms": 20_000,
                "target_duration_seconds": 20.0,
                "target_chars": 90,
                "min_chars": 76,
                "max_chars": 103,
                "source_text": "General Kenobi.",
            },
        ],
        video_title="Demo Video",
        youtube_url="https://youtube.example/watch?v=demo",
        strict_length_control=True,
    )

    assert "标题=Demo Video" in prompt
    assert "链接=https://youtube.example/watch?v=demo" in prompt
    assert "这是双人访谈" in prompt
    assert "Length reminder" in prompt
    assert '"segment_id": 1' in prompt


def test_gemini_translator_infer_prompt_supports_custom_template_tokens() -> None:
    translator = _build_translator(
        speaker_infer_prompt_template=(
            "上下文如下：\n__CONTEXT_EXCERPT__\n"
            "输出格式：__EXPECTED_OUTPUT_JSON__"
        )
    )

    prompt = translator._build_infer_speaker_prompt(
        context_excerpt="[Video title]: Demo\n[A]: Hello",
        num_speakers=2,
    )

    assert "上下文如下：" in prompt
    assert "[Video title]: Demo" in prompt
    assert '{"speaker_a": "推断的姓名或角色", "speaker_b": "推断的姓名或角色"}' in prompt


def test_load_gemini_config_reads_api_key_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "gemini": {
                    "api_key": None,
                    "api_key_env_var": "GEMINI_API_KEY",
                    "model_name": "gemini-3.1-pro-preview",
                    "temperature": 0.3,
                    "max_output_tokens": 8192,
                },
                "prompts": {
                    "s2_infer": "识别说话人\\n__CONTEXT_EXCERPT__",
                    "s3_translate": "自定义提示词\\n__GROUPS_JSON__",
                    "s5_rewrite": "改写\\n__DIRECTION_DESC__\\n__DIRECTION_INSTRUCTION__\\n__TTS_CN_TEXT__\\n__TARGET_CHARS__",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gemini_translator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")

    config = load_gemini_config()

    assert config["api_key"] == "test_key"
    assert config["model_name"] == "gemini-3.1-pro-preview"
    assert config["temperature"] == 0.3
    assert config["max_output_tokens"] == 8192
    assert config["sdk_backend"] == "google-genai"
    assert config["speaker_infer_prompt_template"] == "识别说话人\\n__CONTEXT_EXCERPT__"
    assert config["translation_prompt_template"] == "自定义提示词\\n__GROUPS_JSON__"
    assert (
        config["rewrite_prompt_template"]
        == "改写\\n__DIRECTION_DESC__\\n__DIRECTION_INSTRUCTION__\\n__TTS_CN_TEXT__\\n__TARGET_CHARS__"
    )


def test_load_gemini_config_raises_when_api_key_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "gemini": {
                    "api_key": None,
                    "api_key_env_var": "GEMINI_API_KEY",
                    "model_name": "gemini-3.1-pro-preview",
                    "temperature": 0.3,
                    "max_output_tokens": 8192,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gemini_translator_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(TranslationError, match="GEMINI_API_KEY"):
        load_gemini_config()


def test_gemini_translator_returns_empty_result_without_calling_sdk_for_empty_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: (_ for _ in ()).throw(
            AssertionError("Gemini should not be called for empty input")
        ),
    )

    result = translator.translate(
        [],
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert result.segments == []
    assert result.total_segments == 0


def test_gemini_translator_builds_dynamic_target_chars_from_source_density() -> None:
    lines = [
        _make_line(1, 0, 10_000, "One short point.", speaker_id="speaker_a"),
        _make_line(
            2,
            10_000,
            20_000,
            "This is a much denser point with many more words packed into the same duration window.",
            speaker_id="speaker_b",
            speaker_label="B",
        ),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=10_000)

    assert len(groups) == 2
    assert groups[0]["dynamic_target_chars"] < groups[1]["dynamic_target_chars"]
    assert groups[0]["source_word_count"] < groups[1]["source_word_count"]
    assert groups[0]["target_chars"] == groups[0]["dynamic_target_chars"]


def test_gemini_translator_clamps_density_factor_for_extreme_speech_rates() -> None:
    lines = [
        _make_line(1, 0, 10_000, "Slow.", speaker_id="speaker_a"),
        _make_line(
            2,
            10_000,
            20_000,
            "This is a moderately dense segment with enough words to set the reference point cleanly.",
            speaker_id="speaker_b",
            speaker_label="B",
        ),
        _make_line(
            3,
            20_000,
            30_000,
            " ".join(["rapid"] * 50),
            speaker_id="speaker_c",
            speaker_label="C",
        ),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=10_000)

    assert groups[0]["density_factor"] == pytest.approx(0.65, abs=0.001)
    assert groups[2]["density_factor"] == pytest.approx(1.5, abs=0.001)


def test_gemini_translator_uses_speaker_specific_reference_words_per_second_when_available() -> None:
    lines = [
        _make_line(1, 0, 10_000, "one two", speaker_id="speaker_a"),
        _make_line(2, 10_000, 20_000, "one two three", speaker_id="speaker_a"),
        _make_line(3, 20_000, 30_000, "one two three four", speaker_id="speaker_a"),
        _make_line(4, 30_000, 40_000, "one two three four five", speaker_id="speaker_b", speaker_label="B"),
        _make_line(5, 40_000, 50_000, "one two three four five six", speaker_id="speaker_b", speaker_label="B"),
        _make_line(6, 50_000, 60_000, "one two three four five six seven", speaker_id="speaker_b", speaker_label="B"),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=10_000)

    assert len(groups) == 6
    assert groups[0]["density_factor_source"] == "speaker"
    assert groups[3]["density_factor_source"] == "speaker"
    assert groups[0]["reference_words_per_second"] == pytest.approx(0.3, abs=0.001)
    assert groups[3]["reference_words_per_second"] == pytest.approx(0.6, abs=0.001)
    assert groups[0]["dynamic_target_chars"] < groups[3]["dynamic_target_chars"]
    assert groups[0]["density_factor"] == pytest.approx(0.667, abs=0.001)
    assert groups[5]["density_factor"] == pytest.approx(1.167, abs=0.001)


def test_gemini_translator_falls_back_to_global_reference_when_speaker_samples_are_insufficient() -> None:
    lines = [
        _make_line(1, 0, 10_000, "one two", speaker_id="speaker_a"),
        _make_line(2, 10_000, 20_000, "one two three four five", speaker_id="speaker_b", speaker_label="B"),
        _make_line(3, 20_000, 30_000, "one two three four five six", speaker_id="speaker_b", speaker_label="B"),
        _make_line(4, 30_000, 40_000, "one two three four five six seven", speaker_id="speaker_b", speaker_label="B"),
    ]

    groups = gemini_translator_module._build_groups(lines, max_segment_duration_ms=10_000)

    assert groups[0]["density_factor_source"] == "global"
    assert groups[0]["reference_words_per_second"] == pytest.approx(0.55, abs=0.001)


def test_gemini_translator_prompt_includes_dynamic_length_fields() -> None:
    translator = _build_translator()

    prompt = translator._build_prompt(
        [
            {
                "segment_id": 1,
                "speaker_id": "speaker_a",
                "start_ms": 0,
                "end_ms": 20_000,
                "target_duration_ms": 20_000,
                "target_duration_seconds": 20.0,
                "source_word_count": 8,
                "source_words_per_second": 0.4,
                "reference_words_per_second": 0.5,
                "density_factor_source": "speaker",
                "density_factor": 1.0,
                "dynamic_target_chars": 90,
                "target_chars": 90,
                "min_chars": 76,
                "max_chars": 103,
                "source_text": "Sam Altman spoke with Elon Musk about OpenAI.",
            }
        ],
        video_title="Demo Video",
        youtube_url="https://youtube.example/watch?v=demo",
        strict_length_control=True,
    )

    assert "source_word_count" in prompt
    assert "source_words_per_second" in prompt
    assert "reference_words_per_second" in prompt
    assert "density_factor_source" in prompt
    assert "dynamic_target_chars" in prompt
    assert "Length reminder" in prompt


def test_gemini_translator_retries_batch_once_when_length_is_out_of_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [_make_line(1, 0, 10_000, "This segment has enough English words to need a more compact translation.")]
    observed_prompts: list[str] = []

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del json_mode
        observed_prompts.append(prompt)
        if len(observed_prompts) == 1:
            return json.dumps(
                [{"segment_id": 1, "cn_text": "超长文本" * 40}],
                ensure_ascii=False,
            )
        return json.dumps(
            [{"segment_id": 1, "cn_text": "这是一段更合适的中文配音稿。"}],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert len(observed_prompts) == 2
    assert "Length reminder" in observed_prompts[1]
    assert result.segments[0].cn_text == "这是一段更合适的中文配音稿。"


def test_gemini_translator_accepts_second_attempt_even_when_still_out_of_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translator = _build_translator()
    lines = [_make_line(1, 0, 10_000, "This segment still resists strict length control.")]
    observed_calls = {"count": 0}

    def fake_call(prompt: str, json_mode: bool = False) -> str:
        del prompt, json_mode
        observed_calls["count"] += 1
        return json.dumps(
            [{"segment_id": 1, "cn_text": "超长文本" * 35}],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_call)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert observed_calls["count"] == 2
    assert result.segments[0].cn_text != ""


def test_gemini_translator_falls_back_to_secondary_model_on_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        def get_route(self, task: str) -> list[str]:
            assert task == "s3_translate"
            return ["default_llm", "gpt_41"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            del alias
            return {}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del prompt, json_mode
            assert alias == "gpt_41"
            return json.dumps(
                [{"segment_id": 1, "cn_text": "Fallback translation."}],
                ensure_ascii=False,
            )

    translator = _build_translator(llm_router=FakeRouter())
    lines = [_make_line(1, 0, 5_000, "Fallback test.")]

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: (_ for _ in ()).throw(TranslationError("Gemini cap hit")),
    )
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert result.segments[0].cn_text == "Fallback translation."


def test_gemini_translator_uses_gemini_alias_model_before_cross_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        def get_route(self, task: str) -> list[str]:
            assert task == "s3_translate"
            return ["gemini_3_1_flash_lite_preview", "deepseek_chat", "gpt_41"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            if alias == "gemini_3_1_flash_lite_preview":
                return {"provider": "gemini", "model_name": "gemini-3.1-flash-lite-preview"}
            return {}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del alias, prompt, json_mode
            raise AssertionError("Cross-provider fallback should not be reached when Gemini alias succeeds.")

    translator = _build_translator(llm_router=FakeRouter())
    lines = [_make_line(1, 0, 5_000, "Gemini alias fallback test.")]
    observed_model_names: list[str | None] = []

    def fake_gemini_call(prompt: str, json_mode: bool = False, *, model_name: str | None = None) -> str:
        del prompt, json_mode
        observed_model_names.append(model_name)
        if model_name is None:
            raise TranslationError("primary Gemini failed")
        return json.dumps(
            [{"segment_id": 1, "cn_text": "Gemini alias translation."}],
            ensure_ascii=False,
        )

    monkeypatch.setattr(translator, "_call_gemini_with_retry", fake_gemini_call)
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert observed_model_names == ["gemini-3.1-flash-lite-preview"]
    assert result.segments[0].cn_text == "Gemini alias translation."


def test_gemini_translator_falls_back_when_primary_returns_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        def get_route(self, task: str) -> list[str]:
            assert task == "s3_translate"
            return ["default_llm", "claude_sonnet_46"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            del alias
            return {}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del prompt, json_mode
            assert alias == "claude_sonnet_46"
            return json.dumps(
                [{"segment_id": 1, "cn_text": "Recovered from fallback."}],
                ensure_ascii=False,
            )

    translator = _build_translator(llm_router=FakeRouter())
    lines = [_make_line(1, 0, 5_000, "JSON fallback test.")]

    monkeypatch.setattr(translator, "_call_gemini_with_retry", lambda prompt, json_mode=False: "{broken json")
    monkeypatch.setattr(translator, "_batch_needs_length_retry", lambda parsed_items, groups: False)

    result = translator.translate(
        lines,
        str(tmp_path / "translation"),
        voice_id="voice_demo_001",
    )

    assert result.segments[0].cn_text == "Recovered from fallback."


def test_gemini_translator_routes_speaker_inference_through_task_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_route(self, task: str) -> list[str]:
            self.calls.append(task)
            return ["gpt_41"]

        def get_model_config(self, alias: str) -> dict[str, object]:
            del alias
            return {}

        def generate_via_alias(self, alias: str, *, prompt: str, json_mode: bool = False) -> str:
            del prompt, json_mode
            assert alias == "gpt_41"
            return json.dumps(
                {"speaker_a": "Interviewer", "speaker_b": "Founder"},
                ensure_ascii=False,
            )

    router = FakeRouter()
    translator = _build_translator(llm_router=router)
    lines = [
        _make_line(1, 0, 1_000, "Welcome back.", speaker_id="speaker_a", speaker_label="A"),
        _make_line(2, 1_000, 2_000, "Thanks for having me.", speaker_id="speaker_b", speaker_label="B"),
    ]

    inferred_names = translator.infer_speaker_names(lines, num_speakers=2)

    assert inferred_names == {"speaker_a": "Interviewer", "speaker_b": "Founder"}
    assert router.calls == ["s2_infer"]

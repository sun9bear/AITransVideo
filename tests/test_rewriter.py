import re

from services.gemini.rewriter import GeminiRewriter
from services.gemini.translator import GeminiTranslator


def _build_translator() -> GeminiTranslator:
    return GeminiTranslator(
        api_key="test_key",
        model_name="gemini-3.1-pro-preview",
        _skip_init=True,
    )


def test_rewriter_shrinks_text_when_actual_duration_is_longer(monkeypatch) -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "更短的配音文本",
    )

    rewritten = rewriter.rewrite_for_duration(
        "这是一段比较长的配音文本，需要压缩。",
        actual_duration_ms=12_000,
        target_duration_ms=8_000,
    )

    assert rewritten == "更短的配音文本"


def test_rewriter_expands_text_when_actual_duration_is_shorter(monkeypatch) -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "这是扩充后的配音文本，会更适合更长的目标时长。",
    )

    rewritten = rewriter.rewrite_for_duration(
        "这段太短了。",
        actual_duration_ms=4_000,
        target_duration_ms=8_000,
    )

    assert "扩充后" in rewritten


def test_rewriter_returns_original_text_when_gemini_returns_empty(monkeypatch) -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)

    monkeypatch.setattr(
        translator,
        "_call_gemini_with_retry",
        lambda prompt, json_mode=False: "   ",
    )

    original_text = "保持原文"
    rewritten = rewriter.rewrite_for_duration(
        original_text,
        actual_duration_ms=8_000,
        target_duration_ms=6_000,
        source_text="Keep the meaning.",
    )

    assert rewritten == original_text


def test_rewriter_prompt_contains_direction_target_count_and_source_text() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)

    prompt = rewriter._build_rewrite_prompt(
        "这是原始文本",
        direction="shrink",
        current_chars=6,
        target_chars=4,
        target_lower_chars=4,
        target_upper_chars=5,
        target_lower_ratio_pct=95.0,
        target_upper_ratio_pct=112.0,
        change_pct=33.3,
        source_text="This is the original English reference.",
    )

    assert "缩短" in prompt
    assert "当前文本（6字）" in prompt
    assert "目标字数：约4字" in prompt
    assert "英文原文（参考，不要直接翻译）" in prompt
    assert "This is the original English reference." in prompt
    assert "不是重新翻译" in prompt
    assert re.search(r"33", prompt)


def test_rewriter_prompt_supports_custom_template_tokens() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(
        translator,
        rewrite_prompt_template=(
            "方向=__DIRECTION_DESC__\n"
            "动作=__DIRECTION_INSTRUCTION__\n"
            "当前=__TTS_CN_TEXT__\n"
            "目标=__TARGET_CHARS__\n"
            "原文=__SOURCE_TEXT__"
        ),
    )

    prompt = rewriter._build_rewrite_prompt(
        "这是原始文本",
        direction="shrink",
        current_chars=6,
        target_chars=4,
        target_lower_chars=4,
        target_upper_chars=5,
        target_lower_ratio_pct=95.0,
        target_upper_ratio_pct=112.0,
        change_pct=33.3,
        source_text="This is the original English reference.",
    )

    assert "方向=缩短" in prompt
    assert "动作=删减冗余词汇、连接词，精简表达" in prompt
    assert "当前=这是原始文本" in prompt
    assert "目标=4" in prompt
    assert "原文=This is the original English reference." in prompt


def test_rewriter_uses_s5_rewrite_fallback_route_when_router_is_available() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)
    captured: dict[str, object] = {}

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        captured["task"] = task
        captured["json_mode"] = json_mode
        captured["prompt"] = prompt
        return "Fallback rewritten text"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    rewritten = rewriter.rewrite_for_duration(
        "需要重写的配音文本",
        actual_duration_ms=10_000,
        target_duration_ms=7_000,
        source_text="This is the original English sentence.",
    )

    assert rewritten == "Fallback rewritten text"
    assert captured["task"] == "s5_rewrite"
    assert captured["json_mode"] is False


def test_rewriter_short_content_compact_uses_dedicated_task_and_prompt() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)
    captured: dict[str, object] = {}

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        captured["task"] = task
        captured["json_mode"] = json_mode
        captured["prompt"] = prompt
        return "现在还建议买股票吗"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    rewritten = rewriter.rewrite_short_content_compact(
        "您现在还会重复那句话吗？如果麻烦要来了，您还会建议现在买入股票吗？",
        source_text=(
            "Would you repeat that this time? If trouble's coming, "
            "would you still say buy stocks right now?"
        ),
        target_duration_ms=3_145,
        target_lower_chars=8,
        target_upper_chars=13,
    )

    assert rewritten == "现在还建议买股票吗"
    assert captured["task"] == "s5_short_content_compact"
    assert captured["json_mode"] is False
    prompt = str(captured["prompt"])
    assert "口播压缩" in prompt
    assert "目标 spoken chars：8~13" in prompt
    assert "多个连续问题可合并为一个核心问题" in prompt
    assert "Would you repeat that this time?" in prompt
    # 2026-05-09 fix regression guard: when strict_retry_reason is empty
    # (the default first-call case), the prompt MUST NOT contain the
    # 严格重试 tail block; otherwise the LLM gets a useless "上一版输出因
    # 未通过字数保护" sentence on the first attempt.
    assert "严格重试" not in prompt


def test_rewriter_short_content_compact_accepts_strict_retry_reason() -> None:
    """Regression for 2026-05-09 production failure:
    ``_rewrite_short_content_compact_with_guardrails`` (process.py:5614)
    forwards ``strict_retry_reason`` to ``rewrite_short_content_compact``
    when the first compact attempt was rejected (e.g. ``above_ceiling``).
    Pre-fix the method's signature didn't accept that kwarg, so the
    retry call raised TypeError and the whole pipeline failed at S4.

    This test pins:
    1. The method accepts the kwarg without TypeError.
    2. The kwarg's content surfaces in the prompt as a 严格重试 block,
       so the LLM knows what to fix.
    3. Behaviour mirrors ``rewrite_for_duration_with_profile`` strict
       retry (rewriter.py:263-269).
    """

    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)
    captured: dict[str, object] = {}

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        captured["task"] = task
        captured["prompt"] = prompt
        return "更短的口播"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    # The kwarg form that process.py:5614 uses — must not raise TypeError.
    rewritten = rewriter.rewrite_short_content_compact(
        "这是要被压缩的中文，含很多冗余表达。",
        source_text="A very contentful question.",
        target_duration_ms=2_500,
        target_lower_chars=8,
        target_upper_chars=12,
        strict_retry_reason="above_ceiling:18>12",
    )

    assert rewritten == "更短的口播"
    prompt = str(captured["prompt"])
    # 严格重试 block (mirrors line 263-269 of rewriter.py for the generic path)
    assert "严格重试" in prompt
    assert "above_ceiling:18>12" in prompt
    assert "未通过字数保护" in prompt
    assert "必须优先满足上述字数下限和上限" in prompt
    # Original compact-prompt body still present.
    assert "口播压缩" in prompt
    assert "目标 spoken chars：8~12" in prompt


def test_rewriter_prefers_speaker_specific_chars_per_second() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(
        translator,
        chars_per_second=4.5,
        chars_per_second_by_speaker={"speaker_a": 3.0, "speaker_b": 6.0},
    )

    shrink_prompt = rewriter._build_rewrite_prompt(
        "需要缩短的文本",
        direction="shrink",
        current_chars=6,
        target_chars=max(1, int(10_000 / 1000 * 3.0)),
        target_lower_chars=28,
        target_upper_chars=34,
        target_lower_ratio_pct=95.0,
        target_upper_ratio_pct=112.0,
        change_pct=10.0,
        source_text="reference",
    )
    assert "目标字数：约30字" in shrink_prompt

    used_target_chars: list[int] = []

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        del task, json_mode
        match = re.search(r"目标字数：约(\d+)字", prompt)
        assert match is not None
        used_target_chars.append(int(match.group(1)))
        return "改写文本"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    rewriter.rewrite_for_duration(
        "按说话人语速改写",
        actual_duration_ms=12_000,
        target_duration_ms=10_000,
        speaker_id="speaker_a",
    )
    rewriter.rewrite_for_duration(
        "按说话人语速改写",
        actual_duration_ms=12_000,
        target_duration_ms=10_000,
        speaker_id="speaker_b",
    )

    assert used_target_chars == [30, 60]


def test_rewriter_falls_back_to_global_chars_per_second_when_speaker_calibration_is_missing() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(
        translator,
        chars_per_second=4.5,
        chars_per_second_by_speaker={"speaker_a": 3.0},
    )

    captured_target_chars: dict[str, int] = {}

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        del task, json_mode
        match = re.search(r"目标字数：约(\d+)字", prompt)
        assert match is not None
        captured_target_chars["value"] = int(match.group(1))
        return "改写文本"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    rewriter.rewrite_for_duration(
        "全局回退测试",
        actual_duration_ms=10_000,
        target_duration_ms=10_000,
        speaker_id="speaker_b",
    )

    assert captured_target_chars["value"] == 45


def test_rewriter_prompt_includes_directional_bounds() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)

    prompt = rewriter._build_rewrite_prompt(
        "这是原始文本",
        direction="expand",
        current_chars=6,
        target_chars=45,
        target_lower_chars=40,
        target_upper_chars=48,
        target_lower_ratio_pct=88.0,
        target_upper_ratio_pct=108.0,
        change_pct=25.0,
        source_text="reference",
    )

    assert "40~48" in prompt
    assert "88%~108%" in prompt


def test_rewriter_profile_uses_explicit_char_bounds() -> None:
    translator = _build_translator()
    rewriter = GeminiRewriter(translator, chars_per_second=4.5)
    captured: dict[str, str] = {}

    def fake_call(task: str, prompt: str, *, json_mode: bool = False) -> str:
        del task, json_mode
        captured["prompt"] = prompt
        return "改写后的中文文本"

    translator._call_task_with_fallback = fake_call  # type: ignore[method-assign]

    rewritten = rewriter.rewrite_for_duration_with_profile(
        "需要压缩的中文文本",
        actual_duration_ms=26_000,
        target_duration_ms=20_000,
        preferred_min_ratio=1.0,
        preferred_max_ratio=1.12,
        target_lower_chars=90,
        target_upper_chars=101,
    )

    assert rewritten == "改写后的中文文本"
    assert "90~101" in captured["prompt"]
    assert "spoken chars" in captured["prompt"]

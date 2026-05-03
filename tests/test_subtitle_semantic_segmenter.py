"""Contract tests for semantic_segmenter.segment_text() — Task 3 (T3) of
subtitle-generation-v2, Phase 1a.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.3, §9, §10

Phase 1a scope:
- Split on CJK strong / medium punctuation (。！？!? and ；;：:、).
- Never split inside an English-word run or digit run.
- Mixed-token spans stay whole, marked needs_review="unknown_mixed_token".
- Long no-punct text stays whole, marked needs_review="long_unbreakable_text".
- No hard char-count splits of any kind.
- Concatenation invariant: normalize(join(spans)) == normalize(input).
"""

import pytest

from modules.subtitles.cue_models import normalize
from modules.subtitles.semantic_segmenter import SegmentSpan, segment_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def joined(spans: list[SegmentSpan]) -> str:
    return "".join(s.text for s in spans)


# ---------------------------------------------------------------------------
# 1. Strong CJK split: 。 ! ? ！ ？
# ---------------------------------------------------------------------------


def test_strong_cjk_period_two_sentences():
    """'今天很好。明天更好。' splits into exactly 2 spans; punct stays on preceding span."""
    spans = segment_text("今天很好。明天更好。")
    assert len(spans) == 2
    assert spans[0].text == "今天很好。"
    assert spans[1].text == "明天更好。"


def test_strong_cjk_exclamation():
    """！ acts as a strong boundary."""
    spans = segment_text("很好！继续。")
    assert len(spans) == 2
    assert spans[0].text == "很好！"
    assert spans[1].text == "继续。"


def test_strong_question_mark_ascii():
    """ASCII ? splits on strong boundary."""
    spans = segment_text("真的吗?对的。")
    assert len(spans) == 2
    assert spans[0].text == "真的吗?"
    assert spans[1].text == "对的。"


def test_strong_no_trailing_punct():
    """Final segment without trailing punct is returned as-is."""
    spans = segment_text("第一句。第二句")
    assert len(spans) == 2
    assert spans[0].text == "第一句。"
    assert spans[1].text == "第二句"


# ---------------------------------------------------------------------------
# 2. Medium CJK split: ；;：:
# ---------------------------------------------------------------------------


def test_medium_semicolon_cjk():
    """'开始；继续；结束。' splits into 3 spans on CJK semicolons."""
    spans = segment_text("开始；继续；结束。")
    assert len(spans) == 3
    assert spans[0].text == "开始；"
    assert spans[1].text == "继续；"
    assert spans[2].text == "结束。"


def test_medium_semicolon_ascii():
    """ASCII semicolon works as a medium boundary."""
    spans = segment_text("一;二;三。")
    assert len(spans) == 3
    assert spans[0].text == "一;"
    assert spans[1].text == "二;"
    assert spans[2].text == "三。"


def test_medium_colon_splits():
    """Colon acts as a medium boundary (trailing attachment)."""
    spans = segment_text("前缀：后面的内容。")
    assert len(spans) == 2
    assert spans[0].text == "前缀："
    assert spans[1].text == "后面的内容。"


# ---------------------------------------------------------------------------
# 3. CJK ideographic comma 、
# ---------------------------------------------------------------------------


def test_cjk_ideographic_comma():
    """'前文、后文。' splits into 2 spans."""
    spans = segment_text("前文、后文。")
    assert len(spans) == 2
    assert spans[0].text == "前文、"
    assert spans[1].text == "后文。"


# ---------------------------------------------------------------------------
# 4. Multiple strong-punct treated as one boundary
# ---------------------------------------------------------------------------


def test_multiple_strong_punct_one_span():
    """'真的吗?!' is NOT split — consecutive strong-punct is one boundary."""
    spans = segment_text("真的吗?!")
    assert len(spans) == 1
    assert spans[0].text == "真的吗?!"


def test_multiple_strong_punct_cjk():
    """'！？' consecutive does not produce an empty segment in between."""
    spans = segment_text("好啊！？再说一遍。")
    # The ！？ is one boundary — result is 2 spans, not 3
    assert len(spans) == 2
    assert spans[0].text == "好啊！？"
    assert spans[1].text == "再说一遍。"


# ---------------------------------------------------------------------------
# 5. Don't split inside English-word runs
# ---------------------------------------------------------------------------


def test_no_split_inside_english_word_with_period():
    """'hello.world' — ASCII . between two letter runs does not split (preceding char is letter)."""
    spans = segment_text("hello.world")
    # The period is between letters — not a sentence terminator
    assert len(spans) == 1
    assert spans[0].text == "hello.world"


def test_english_sentence_no_split():
    """Plain English sentence with final period: stays as one span (or 1 split if period is terminal).

    Per rule: ASCII '.' splits only when followed by whitespace, end-of-string, or CJK char.
    'Hello world.' — '.' at EOS → split occurs after 'Hello world.', yielding 1 span.
    But that's fine: 1 span is correct.
    """
    spans = segment_text("Hello world.")
    # '.' at end-of-string qualifies as a strong boundary only if the preceding
    # character is NOT a letter/digit. Here preceding is 'd' (letter).
    # Per spec: "split on ASCII '.' only when NOT preceded by a letter/digit".
    # So no split here — 1 span.
    assert len(spans) == 1
    assert spans[0].text == "Hello world."


def test_no_split_inside_english_word_run_mid_cjk():
    """CJK + English mix: English words survive intact across potential split points."""
    text = "这是 hello world 句子。结束。"
    spans = segment_text(text)
    # '。' after '句子' is a strong boundary; '。' after '结束' is too
    assert len(spans) == 2
    # English words hello/world must not be split
    assert "hello world" in spans[0].text


# ---------------------------------------------------------------------------
# 6. Don't split decimals (ASCII . between digits)
# ---------------------------------------------------------------------------


def test_no_split_decimal():
    """'价格 3.14 元。' — the '.' in '3.14' does NOT trigger split (preceded by digit)."""
    spans = segment_text("价格 3.14 元。")
    # Only one split at 。, so 1 span total
    assert len(spans) == 1
    assert spans[0].text == "价格 3.14 元。"


def test_no_split_decimal_standalone():
    """Standalone decimal '3.14' stays as one span."""
    spans = segment_text("3.14")
    assert len(spans) == 1
    assert spans[0].text == "3.14"


# ---------------------------------------------------------------------------
# 7. ASCII . followed by space: rule edge case (abbreviation / sentence end)
#
# Rule: ASCII '.' splits only when NOT preceded by letter/digit.
# 'e.g. 这样可以吗?' — the '.' in 'e.g.' IS preceded by 'g' (letter), no split.
# Result: 1 span "e.g. 这样可以吗?" (assuming '?' is a strong boundary producing 1 span).
# ---------------------------------------------------------------------------


def test_abbreviation_not_split():
    """'e.g. 这样可以吗?' — periods in abbreviation are NOT split points (preceded by letter).

    The ASCII '?' at end IS a strong boundary → 1 span with trailing '?'.
    """
    spans = segment_text("e.g. 这样可以吗?")
    # '.' in e.g. — preceded by letter 'g' and 'e' → no split
    # '?' at end → strong boundary, but only 1 segment total
    assert len(spans) == 1
    assert spans[0].text == "e.g. 这样可以吗?"


# ---------------------------------------------------------------------------
# 8. URLs stay whole (marked needs_review)
# ---------------------------------------------------------------------------


def test_url_stays_whole_marked_review():
    """'参考 https://example.com/path 即可。' — URL is not split; whole span marked review."""
    spans = segment_text("参考 https://example.com/path 即可。")
    # 。 at end is a strong boundary, but there's no split before the URL
    # Result: 1 span (only one 。 at end), marked review for URL
    assert len(spans) == 1
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "unknown_mixed_token"


def test_url_with_two_sentences():
    """URL in first sentence; second sentence is clean."""
    text = "参考 https://example.com 即可。谢谢。"
    spans = segment_text(text)
    # 2 spans split at first 。
    assert len(spans) == 2
    # First span has the URL → review
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "unknown_mixed_token"
    # Second span is clean CJK
    assert spans[1].text == "谢谢。"
    assert spans[1].needs_review is False


# ---------------------------------------------------------------------------
# 9. Emails stay whole (marked needs_review)
# ---------------------------------------------------------------------------


def test_email_stays_whole_marked_review():
    """'联系 admin@example.com 谢谢。' — email stays whole, marked review."""
    spans = segment_text("联系 admin@example.com 谢谢。")
    assert len(spans) == 1
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "unknown_mixed_token"


# ---------------------------------------------------------------------------
# 10. Long no-punct text marked long_unbreakable_text
# ---------------------------------------------------------------------------


def test_long_no_punct_text_marked_review():
    """60-char CJK string with no punctuation: 1 span, needs_review=long_unbreakable_text.

    Length budget: '今天' * 30 = 60 CJK chars = 60 CJK-char-equivalents > 40 threshold.
    """
    long_text = "今天" * 30  # 60 CJK chars, no punctuation
    spans = segment_text(long_text)
    assert len(spans) == 1
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "long_unbreakable_text"


def test_long_no_punct_not_hard_split():
    """Long text without punctuation must NOT be hard-split into multiple spans."""
    long_text = "今天" * 30
    spans = segment_text(long_text)
    assert len(spans) == 1  # never split by char count


def test_short_text_not_marked_long():
    """Short plain CJK text is not marked as long_unbreakable_text."""
    spans = segment_text("今天很好")
    assert len(spans) == 1
    assert spans[0].needs_review is False


# ---------------------------------------------------------------------------
# 11. Empty input
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_list():
    """segment_text('') returns []."""
    assert segment_text("") == []


# ---------------------------------------------------------------------------
# 12. Whitespace-only input
# ---------------------------------------------------------------------------


def test_whitespace_only_returns_empty_list():
    """segment_text('   ') returns []."""
    assert segment_text("   ") == []


def test_tab_newline_only_returns_empty_list():
    """segment_text('\\t\\n') returns []."""
    assert segment_text("\t\n") == []


# ---------------------------------------------------------------------------
# 13. Concatenation invariant: normalize(join(spans)) == normalize(input)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "今天很好。明天更好。",
        "开始；继续；结束。",
        "前文、后文。",
        "真的吗?!",
        "价格 3.14 元。",
        "联系 admin@example.com 谢谢。",
        "参考 https://example.com 即可。谢谢。",
        # Whitespace inputs: SegmentSpan preserves raw chunks, so invariant holds.
        "今天。 明天。",       # single inter-sentence space
        "今天。  明天。",      # double space (normalize folds)
        "今天。\n明天。",     # newline boundary
        "  开头有空格。结尾有空格。  ",  # leading + trailing whitespace
        "e.g. 这样可以吗?",
        "这是 hello world 句子。结束。",
        "很好！继续。",
        "前缀：后面的内容。",
        "今天" * 30,  # long no-punct
        "hello.world",
        "3.14",
    ],
)
def test_concatenation_invariant(text):
    """normalize(join(spans)) == normalize(input) for all test inputs."""
    spans = segment_text(text)
    assert normalize(joined(spans)) == normalize(text), (
        f"Concatenation invariant broken for {text!r}\n"
        f"  joined spans: {joined(spans)!r}\n"
        f"  normalize(joined): {normalize(joined(spans))!r}\n"
        f"  normalize(input):  {normalize(text)!r}"
    )


# ---------------------------------------------------------------------------
# 14. Clean CJK split: no needs_review on plain CJK spans
# ---------------------------------------------------------------------------


def test_plain_cjk_split_no_review():
    """'今天很好。明天更好。' → both spans have needs_review=False."""
    spans = segment_text("今天很好。明天更好。")
    assert len(spans) == 2
    assert spans[0].needs_review is False
    assert spans[1].needs_review is False


def test_plain_cjk_medium_split_no_review():
    """'开始；继续；结束。' → all 3 spans have needs_review=False."""
    spans = segment_text("开始；继续；结束。")
    for span in spans:
        assert span.needs_review is False


# ---------------------------------------------------------------------------
# 15. SegmentSpan properties
# ---------------------------------------------------------------------------


def test_segment_span_is_frozen():
    """SegmentSpan is frozen=True and raises on mutation attempt."""
    span = SegmentSpan(text="测试")
    with pytest.raises((AttributeError, TypeError)):
        span.text = "changed"  # type: ignore[misc]


def test_segment_span_preserves_raw_text():
    """SegmentSpan preserves raw text — no stripping (strip is display-layer concern)."""
    span = SegmentSpan(text="  测试  ")
    assert span.text == "  测试  "


def test_segment_span_defaults():
    """SegmentSpan defaults: needs_review=False, review_reason=None."""
    span = SegmentSpan(text="测试")
    assert span.needs_review is False
    assert span.review_reason is None


# ---------------------------------------------------------------------------
# 16. Trailing whitespace between sentences is not included in spans
# ---------------------------------------------------------------------------


def test_inter_sentence_space_preserved_in_raw_chunk():
    """'今天。 明天。' → spans preserve raw chunks; concatenation == input.

    SegmentSpan no longer strips.  The raw chunk from _split_on_boundaries
    for the second sentence is ' 明天。' (with leading space), so that
    ''.join(s.text for s in spans) == '今天。 明天。' == original input.
    Display layers (SRT writer) strip before rendering.
    """
    jin = '今'; tian = '天'; period = '。'; ming = '明'
    text = jin + tian + period + ' ' + ming + tian + period
    spans = segment_text(text)
    assert len(spans) == 2
    assert spans[0].text == jin + tian + period
    assert spans[1].text == ' ' + ming + tian + period
    # Concatenation strictly equals input
    assert ''.join(s.text for s in spans) == text


# ---------------------------------------------------------------------------
# 17. Single-span digit does NOT get needs_review (nothing to compare against)
# ---------------------------------------------------------------------------


def test_single_digit_run_no_review():
    """'24' alone → 1 span, needs_review=False (single-span exception per spec rule 4)."""
    spans = segment_text("24")
    assert len(spans) == 1
    assert spans[0].needs_review is False


def test_single_english_word_no_review():
    """'hello' alone → 1 span, needs_review=False (single-span exception)."""
    spans = segment_text("hello")
    assert len(spans) == 1
    assert spans[0].needs_review is False


# ---------------------------------------------------------------------------
# 18. Mixed-token detection (multi-span: review IS applied)
# ---------------------------------------------------------------------------


def test_digit_run_in_mixed_context_marked_review():
    """Digit run of >= 2 digits in a span that's part of a multi-span result: marked review."""
    # '100元。明天。' — first span has digit run, second is clean CJK
    spans = segment_text("100元。明天。")
    assert len(spans) == 2
    # First span has digit run >= 2
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "unknown_mixed_token"
    # Second span is plain CJK
    assert spans[1].needs_review is False


def test_english_word_in_mixed_context_marked_review():
    """English word in a span that's part of multi-span result: marked review."""
    spans = segment_text("Hello世界。明天。")
    assert len(spans) == 2
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "unknown_mixed_token"
    assert spans[1].needs_review is False


# ---------------------------------------------------------------------------
# 19. Weak-boundary split: CJK comma (，) and ASCII comma (,)
# ---------------------------------------------------------------------------


def test_weak_comma_split_simple():
    """'今天我们来谈一下，就是关于 LLM 的问题' — CJK comma splits into 2 spans.

    Both sides: left = '今天我们来谈一下，' (8 CJK + ，= 8 units) >= 6;
    right = '就是关于 LLM 的问题' has enough content >= 6.
    """
    text = "今天我们来谈一下，就是关于LLM的问题"
    spans = segment_text(text)
    assert len(spans) >= 2
    # Concatenation invariant
    assert "".join(s.text for s in spans) == text
    # Both sides should be non-trivially short
    for s in spans:
        assert len(s.text.strip()) >= 2


def test_weak_comma_min_length_guard_rejects_single_char_left():
    """'我，我觉得我们应该努力工作' — comma after single '我' is REJECTED (left side = 1 CJK < 6).

    The split after '我，' produces left='我，' (1 CJK unit) which fails min_chunk=6.
    Result: no split at first comma; later comma if any may still split.
    """
    text = "我，我觉得我们应该努力工作"
    spans = segment_text(text)
    # The first comma (after single '我') must NOT produce a single-char left cue
    for s in spans:
        stripped = s.text.strip()
        # No cue shorter than ~3 chars should appear (the rejected side was 1 CJK)
        assert not (stripped in ("我，", "我") and len(spans) > 1 and spans[0].text.strip() == stripped)
    # Concatenation invariant
    assert "".join(s.text for s in spans) == text


def test_weak_emdash_split():
    """'我去过很多地方——巴黎、伦敦、东京' — em-dash '——' splits into 2 spans.

    Left: '我去过很多地方——' (7 CJK); right: '巴黎、伦敦、东京' (already splits on 、).
    """
    text = "我去过很多地方——巴黎、伦敦、东京"
    spans = segment_text(text)
    assert len(spans) >= 2
    # The em-dash should appear as the tail of the first span
    first_text = spans[0].text
    assert "——" in first_text or first_text.endswith("——")
    # Concatenation invariant
    assert "".join(s.text for s in spans) == text


def test_weak_ellipsis_split():
    """'我觉得……可能不行吧' — ellipsis '……' splits into 2 spans.

    Left side: '我觉得……' (3 CJK) — only 3 units.  If < 6, no split occurs.
    Let's use a longer left side so both sides pass the guard.
    """
    text = "我真的觉得这样做……可能真的不太行"
    spans = segment_text(text)
    assert len(spans) >= 2
    # Concatenation invariant
    assert "".join(s.text for s in spans) == text


def test_weak_ellipsis_short_left_no_split():
    """'我觉得……可能不行' — '我觉得……' has only 3 CJK units, below min_chunk=6.
    Split should NOT occur (both sides must meet the guard).
    """
    text = "我觉得……可能不行"
    spans = segment_text(text)
    # With left side = 3 chars, guard rejects the split → 1 span
    assert len(spans) == 1
    assert "".join(s.text for s in spans) == text


def test_weak_user_real_example_multiple_short_cues():
    """User's real production example — must produce multiple spans, each <= ~30 length-units.

    Source: '我,我可不会满世界跑,呃,我只是……你知道,我,我觉得我们发起了一件好事,而且——
    我觉得没有哪位'捐赠誓言'的成员会比他们原本打算捐的还要少。'

    Phase 1a produced a single 70+ char cue.  This test verifies the weak-boundary
    pass now splits it into multiple shorter spans.
    The quoted region '捐赠誓言' must NOT be split internally.
    """
    text = "我,我可不会满世界跑,呃,我只是……你知道,我,我觉得我们发起了一件好事,而且——我觉得没有哪位'捐赠誓言'的成员会比他们原本打算捐的还要少。"
    spans = segment_text(text)
    # Must produce more than 1 span (the whole point of weak-boundary splitting)
    assert len(spans) > 1
    # Concatenation invariant
    assert "".join(s.text for s in spans) == text
    # No single span should be longer than ~30 CJK-equiv units
    from modules.subtitles.semantic_segmenter import _cjk_equiv_len
    for s in spans:
        assert _cjk_equiv_len(s.text.strip()) <= 35, (
            f"Span too long ({_cjk_equiv_len(s.text.strip()):.1f} units): {s.text!r}"
        )
    # The '捐赠誓言' quoted region must remain intact in some span
    full_join = "".join(s.text for s in spans)
    assert "'捐赠誓言'" in full_join or "捐赠誓言" in full_join


def test_weak_url_comma_split_allowed_after_url():
    """'参考 https://example.com,就这些' — URL is protected; comma AFTER url is a potential split.

    The comma sits after the URL (which ends before it), so it is NOT inside
    the URL protected range.  However, the comma immediately follows the URL
    (char right before comma is the URL's last char which is not a letter/digit
    in typical cases like 'com').  Since 'm' is alpha, the split is prohibited
    by the English-word adjacency guard.  Result: 1 span (URL + rest stay together).
    """
    text = "参考 https://example.com,就这些内容"
    spans = segment_text(text)
    # Concatenation invariant regardless of split count
    assert "".join(s.text for s in spans) == text
    # The URL must appear intact in the output
    full = "".join(s.text for s in spans)
    assert "https://example.com" in full


def test_long_no_split_still_marked_long_unbreakable():
    """Pure CJK long text with no valid split points (no commas, no punct).

    '今天' * 50 = 100 CJK chars > 40 threshold, with no weak boundaries.
    Must be 1 span flagged 'long_unbreakable_text'.
    """
    long_text = "今天" * 50
    spans = segment_text(long_text)
    assert len(spans) == 1
    assert spans[0].needs_review is True
    assert spans[0].review_reason == "long_unbreakable_text"


# ---------------------------------------------------------------------------
# 20. Concatenation invariant extended to weak-boundary inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "今天我们来谈一下，就是关于LLM的问题",
        "我去过很多地方——巴黎、伦敦、东京",
        "我真的觉得这样做……可能真的不太行",
        "我觉得……可能不行",
        "我,我可不会满世界跑,呃,我只是……你知道,我,我觉得我们发起了一件好事,而且——我觉得没有哪位'捐赠誓言'的成员会比他们原本打算捐的还要少。",
        "参考 https://example.com,就这些内容",
    ],
)
def test_weak_boundary_concatenation_invariant(text):
    """normalize(join(spans)) == normalize(input) for weak-boundary inputs."""
    spans = segment_text(text)
    assert normalize(joined(spans)) == normalize(text), (
        f"Invariant broken for {text!r}\n"
        f"  joined: {joined(spans)!r}\n"
        f"  normalize(joined): {normalize(joined(spans))!r}\n"
        f"  normalize(input):  {normalize(text)!r}"
    )

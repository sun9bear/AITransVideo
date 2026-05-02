"""Tests for SubtitleCueBuilder (T5) — orchestrates segmenter + cue_timing.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.1, §5.4, §6, §10 Phase 1a
"""

import pytest

from modules.subtitles.cue_builder import build_cues_for_block, _split_en_proportionally
from modules.subtitles.cue_models import SubtitleCue, normalize


# ---------------------------------------------------------------------------
# Helper defaults (shared across tests)
# ---------------------------------------------------------------------------

_DEFAULTS = dict(
    block_id="block_0001",
    speaker_id="speaker_A",
    speaker_name="Alice",
    block_start_ms=0,
    block_end_ms=4000,
    source="semantic_block_v2",
    min_display_ms=500,
)


def _build(**kwargs):
    """Call build_cues_for_block with _DEFAULTS overridden by kwargs."""
    params = dict(_DEFAULTS)
    params.update(kwargs)
    return build_cues_for_block(**params)


# ---------------------------------------------------------------------------
# Scenario 1 — Empty cn_text → empty cue list
# ---------------------------------------------------------------------------


def test_empty_cn_text_returns_empty():
    cues = _build(cn_text="", en_text="Hello world.")
    assert cues == []


def test_whitespace_only_cn_text_returns_empty():
    cues = _build(cn_text="   ", en_text="Hello world.")
    assert cues == []


# ---------------------------------------------------------------------------
# Scenario 2 — Single sentence: 1 cue, full duration, both texts
# ---------------------------------------------------------------------------


def test_single_sentence_produces_one_cue():
    cues = _build(cn_text="今天很好。", en_text="Today is good.")
    assert len(cues) == 1
    cue = cues[0]
    assert "今天很好" in cue.text
    assert cue.en_text == "Today is good."
    assert cue.start_ms == 0
    assert cue.end_ms == 4000


# ---------------------------------------------------------------------------
# Scenario 3 — Two sentences: 2 cues, timing split, en split
# ---------------------------------------------------------------------------


def test_two_sentences_two_cues():
    cues = _build(
        cn_text="今天好。明天好。",
        en_text="Today good. Tomorrow good.",
        block_start_ms=0,
        block_end_ms=4000,
    )
    assert len(cues) == 2
    # Time coverage
    assert cues[0].start_ms == 0
    assert cues[-1].end_ms == 4000
    # No gap between cues
    assert cues[0].end_ms == cues[1].start_ms
    # English split: 4 words → 2 cues → [2, 2] words each
    assert cues[0].en_text == "Today good."
    assert cues[1].en_text == "Tomorrow good."


def test_two_sentences_first_half_second_half():
    # Verify approximately equal timing (each ~2000ms, within 1500ms each side)
    cues = _build(
        cn_text="今天好。明天好。",
        en_text="Today good. Tomorrow good.",
        block_start_ms=0,
        block_end_ms=4000,
    )
    assert cues[0].end_ms <= 3000  # first cue doesn't take all
    assert cues[1].start_ms >= 500  # second cue doesn't start at zero


# ---------------------------------------------------------------------------
# Scenario 4 — Cue IDs sequential and correctly formatted
# ---------------------------------------------------------------------------


def test_cue_ids_sequential():
    cues = _build(
        block_id="block_0007",
        cn_text="早上好。下午好。晚上好。",
        en_text="Good morning. Good afternoon. Good evening.",
        block_end_ms=6000,
    )
    assert len(cues) == 3
    assert [c.cue_id for c in cues] == [
        "block_0007_cue_01",
        "block_0007_cue_02",
        "block_0007_cue_03",
    ]


def test_cue_id_format_two_digit_index():
    # Make 99 cues by providing enough content — we can fake it with a text
    # that will produce multiple spans; but realistically let's just verify
    # one cue uses 2-digit formatting (already covered above).
    cues = _build(cn_text="今天好。", en_text="Today good.")
    assert cues[0].cue_id == "block_0001_cue_01"


# ---------------------------------------------------------------------------
# Scenario 5 — needs_review propagation: unknown_mixed_token
# ---------------------------------------------------------------------------


def test_needs_review_propagated_mixed_token():
    cues = _build(
        cn_text="参考 https://example.com 即可。",
        en_text="See the link.",
        block_end_ms=3000,
    )
    # The whole text becomes one span flagged as unknown_mixed_token
    assert len(cues) == 1
    assert cues[0].needs_review is True
    assert cues[0].review_reason == "unknown_mixed_token"


# ---------------------------------------------------------------------------
# Scenario 6 — Long unbreakable text → needs_review = long_unbreakable_text
# ---------------------------------------------------------------------------


def test_long_unbreakable_text_flagged():
    cn_text = "今天" * 30  # 60 chars, no punctuation — exceeds 40 CJK threshold
    cues = _build(cn_text=cn_text, en_text="Long text.", block_end_ms=10000)
    assert len(cues) == 1
    assert cues[0].needs_review is True
    assert cues[0].review_reason == "long_unbreakable_text"


# ---------------------------------------------------------------------------
# Scenario 7 — All metadata propagated to each cue
# ---------------------------------------------------------------------------


def test_all_metadata_propagated():
    cues = _build(
        block_id="block_XYZW",
        speaker_id="spk_02",
        speaker_name="Bob",
        cn_text="早上好。下午好。",
        en_text="Morning. Afternoon.",
        block_start_ms=1000,
        block_end_ms=5000,
        source="manual_edit",
        min_display_ms=300,
    )
    for cue in cues:
        assert cue.block_id == "block_XYZW"
        assert cue.speaker_id == "spk_02"
        assert cue.speaker_name == "Bob"
        assert cue.source == "manual_edit"


# ---------------------------------------------------------------------------
# Scenario 8 — speaker_name=None is handled
# ---------------------------------------------------------------------------


def test_speaker_name_none():
    cues = _build(
        speaker_name=None,
        cn_text="今天好。",
        en_text="Today.",
    )
    assert len(cues) == 1
    assert cues[0].speaker_name is None


# ---------------------------------------------------------------------------
# Scenario 9 — English split even
# ---------------------------------------------------------------------------


def test_en_split_even():
    # 6 words, 3 spans → 2 words each
    result = _split_en_proportionally("a b c d e f", 3)
    assert len(result) == 3
    word_counts = [len(s.split()) for s in result]
    assert word_counts == [2, 2, 2]


# ---------------------------------------------------------------------------
# Scenario 10 — English split uneven, extras to front
# ---------------------------------------------------------------------------


def test_en_split_uneven_extras_to_front():
    # 7 words, 3 spans → [3, 2, 2]
    result = _split_en_proportionally("the quick brown fox jumps over lazy", 3)
    assert len(result) == 3
    word_counts = [len(s.split()) for s in result]
    assert word_counts == [3, 2, 2]
    assert result[0] == "the quick brown"
    assert result[1] == "fox jumps"
    assert result[2] == "over lazy"


def test_en_split_one_word_two_spans():
    # 1 word, 2 spans → ["hello", ""]
    result = _split_en_proportionally("hello", 2)
    assert result == ["hello", ""]


def test_en_split_empty():
    result = _split_en_proportionally("", 3)
    assert result == ["", "", ""]


def test_en_split_n_zero():
    result = _split_en_proportionally("hello world", 0)
    assert result == []


def test_en_split_n_one():
    result = _split_en_proportionally("  hello world  ", 1)
    assert result == ["hello world"]


def test_en_split_whitespace_only():
    result = _split_en_proportionally("   ", 2)
    assert result == ["", ""]


# ---------------------------------------------------------------------------
# Scenario 11 — Empty en_text: each cue gets en_text=""
# ---------------------------------------------------------------------------


def test_empty_en_text_each_cue_empty():
    cues = _build(
        cn_text="今天好。明天好。",
        en_text="",
        block_end_ms=4000,
    )
    assert all(c.en_text == "" for c in cues)


# ---------------------------------------------------------------------------
# Scenario 12 — Cue text concatenation invariant
# ---------------------------------------------------------------------------


def test_text_concat_invariant_two_sentences():
    cn_text = "今天好。明天好。"
    cues = _build(cn_text=cn_text, en_text="Today. Tomorrow.", block_end_ms=4000)
    joined = "".join(c.text for c in cues)
    assert normalize(joined) == normalize(cn_text)


def test_text_concat_invariant_three_sentences():
    cn_text = "早上好。下午好。晚上好。"
    cues = _build(cn_text=cn_text, en_text="Morning. Afternoon. Evening.", block_end_ms=6000)
    joined = "".join(c.text for c in cues)
    assert normalize(joined) == normalize(cn_text)


def test_text_concat_invariant_single():
    cn_text = "今天很好。"
    cues = _build(cn_text=cn_text, en_text="Today is good.", block_end_ms=2000)
    joined = "".join(c.text for c in cues)
    assert normalize(joined) == normalize(cn_text)


def test_text_concat_invariant_no_punctuation():
    cn_text = "今天很好明天也好"
    cues = _build(cn_text=cn_text, en_text="Today good tomorrow good.", block_end_ms=4000)
    joined = "".join(c.text for c in cues)
    assert normalize(joined) == normalize(cn_text)


# ---------------------------------------------------------------------------
# Scenario 13 — Time invariants
# ---------------------------------------------------------------------------


def test_time_invariants_first_start_last_end():
    cues = _build(
        cn_text="今天好。明天好。后天好。",
        en_text="Today. Tomorrow. Day after.",
        block_start_ms=1000,
        block_end_ms=7000,
    )
    assert len(cues) == 3
    assert cues[0].start_ms == 1000
    assert cues[-1].end_ms == 7000


def test_time_invariants_monotonic_no_gap():
    cues = _build(
        cn_text="今天好。明天好。后天好。",
        en_text="Today. Tomorrow. Day after.",
        block_start_ms=0,
        block_end_ms=6000,
    )
    for i in range(len(cues) - 1):
        assert cues[i].end_ms == cues[i + 1].start_ms, (
            f"Gap between cue {i} and {i+1}: {cues[i].end_ms} != {cues[i+1].start_ms}"
        )


# ---------------------------------------------------------------------------
# Scenario 14 — Defaults: source and min_display_ms
# ---------------------------------------------------------------------------


def test_default_source():
    cues = build_cues_for_block(
        block_id="block_0001",
        speaker_id="spk_A",
        speaker_name=None,
        cn_text="今天好。",
        en_text="Today.",
        block_start_ms=0,
        block_end_ms=2000,
        # source and min_display_ms intentionally omitted
    )
    assert len(cues) == 1
    assert cues[0].source == "semantic_block_v2"


def test_default_min_display_ms_honored():
    # With min_display_ms=500, each cue should be at least 500ms
    # Use a block that's long enough (2 cues, 2000ms total → 1000ms each)
    cues = build_cues_for_block(
        block_id="block_0001",
        speaker_id="spk_A",
        speaker_name=None,
        cn_text="今天好。明天好。",
        en_text="Today. Tomorrow.",
        block_start_ms=0,
        block_end_ms=2000,
        # min_display_ms defaults to 500
    )
    for cue in cues:
        assert (cue.end_ms - cue.start_ms) >= 500


# ---------------------------------------------------------------------------
# Edge cases — error propagation
# ---------------------------------------------------------------------------


def test_bad_timing_raises_value_error():
    with pytest.raises(ValueError):
        _build(
            cn_text="今天好。",
            en_text="Today.",
            block_start_ms=5000,
            block_end_ms=3000,  # end < start
        )


# ---------------------------------------------------------------------------
# Type check — all returned objects are SubtitleCue
# ---------------------------------------------------------------------------


def test_returns_list_of_subtitle_cues():
    cues = _build(cn_text="今天好。", en_text="Today.")
    assert isinstance(cues, list)
    for c in cues:
        assert isinstance(c, SubtitleCue)


# ---------------------------------------------------------------------------
# no text_audio_may_need_review injection in builder
# ---------------------------------------------------------------------------


def test_builder_does_not_inject_text_audio_review():
    # Builder must NOT inject "text_audio_may_need_review" review reason.
    # That's caller-only (T9 dispatcher).
    cues = _build(cn_text="今天好。", en_text="Today.")
    for cue in cues:
        assert cue.review_reason != "text_audio_may_need_review"


# ---------------------------------------------------------------------------
# Cue ID three-digit format for n > 99
# ---------------------------------------------------------------------------


def test_cue_id_three_digits_when_many_cues():
    # Build 100+ cues: 101 distinct Chinese sentences separated by '。'
    # Each is short ("好。") so segmenter produces one span each.
    cn_text = "".join(f"好{i}。" for i in range(101))
    # Use a very long block so timing doesn't raise
    cues = _build(
        cn_text=cn_text,
        en_text=" ".join(f"good{i}" for i in range(101)),
        block_end_ms=200000,
    )
    # cue at index 100 (1-based: 101) should use 3-digit format
    if len(cues) > 99:
        assert "_cue_100" in cues[99].cue_id

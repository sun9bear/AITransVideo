"""Tests for ``services.whisper_align.dtw.align_chars_to_words``.

Phase C of 2026-05-04-subtitle-audio-sync-plan. The DTW (dynamic time
warping) helper takes our ``cn_text`` and whisper's word-timestamp
list, and produces a per-char timestamp series so the cue pipeline
can place sub-cue boundaries on actual speech rhythms (not on text-
length proportional fractions of the SRT window).

Robust to small ASR mistakes:
- Number normalization (ASR writes "20" while cn_text has "二十").
- Punctuation drift (ASR omits commas / periods).
- Single-char substitutions / deletions / insertions.

Bails out (returns empty list) when ASR transcript and cn_text are too
disjoint to align meaningfully — caller's job is to fall back to the
existing proportional layout in that case.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Happy path: identical text
# ---------------------------------------------------------------------------


def test_dtw_aligns_identical_text_word_by_word():
    """When whisper transcript matches cn_text exactly, each cn_text char
    gets the whisper word's proportional time slice."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    words = [
        {"start_ms": 100, "end_ms": 500, "text": "你好"},
        {"start_ms": 500, "end_ms": 900, "text": "世界"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert len(char_times) == 4

    # All char times are within the words' full span
    for ct in char_times:
        assert 100 <= ct["start_ms"] <= ct["end_ms"] <= 900

    # And in monotonically non-decreasing order
    for i in range(1, len(char_times)):
        assert char_times[i]["start_ms"] >= char_times[i - 1]["start_ms"]


def test_dtw_returns_text_aligned_to_cn_chars_not_whisper_chars():
    """The output's ``text`` field reports the cn_text char (our text,
    what we'll display), NOT the whisper transcript char (which may
    differ due to ASR transcription style)."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好"
    words = [{"start_ms": 0, "end_ms": 1000, "text": "你好"}]
    char_times = align_chars_to_words(cn_text, words)
    assert [ct["text"] for ct in char_times] == ["你", "好"]


def test_dtw_char_times_within_word_are_evenly_spaced():
    """Within a single whisper word, the cn_text chars get evenly-divided
    sub-times (linear interp). This is fine because sub-word timing
    granularity from whisper is rarely accurate anyway."""
    from services.whisper_align.dtw import align_chars_to_words

    # 4-char word over 1000ms → each char gets ~250ms slice
    words = [{"start_ms": 0, "end_ms": 1000, "text": "你好世界"}]
    char_times = align_chars_to_words("你好世界", words)
    assert len(char_times) == 4
    expected_step = 1000 / 4
    for i, ct in enumerate(char_times):
        assert abs(ct["start_ms"] - i * expected_step) < 5
        assert abs(ct["end_ms"] - (i + 1) * expected_step) < 5


# ---------------------------------------------------------------------------
# Robustness: ASR-style differences from cn_text
# ---------------------------------------------------------------------------


def test_dtw_tolerates_arabic_to_chinese_digit_difference():
    """ASR commonly outputs '20' while our LLM-translated cn_text has '二十'.
    Normalization should treat these as matchable so alignment continues."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "二十多岁"
    words = [{"start_ms": 0, "end_ms": 800, "text": "20多岁"}]
    char_times = align_chars_to_words(cn_text, words)
    # All 4 cn_text chars get aligned; times within the word's span.
    assert len(char_times) == 4
    for ct in char_times:
        assert 0 <= ct["start_ms"] < ct["end_ms"] <= 800
        assert ct["text"] in cn_text


def test_dtw_tolerates_missing_punctuation():
    """ASR omits commas / periods. We strip ASCII + CJK punctuation in
    the comparison only — the output preserves cn_text characters."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好,世界。"  # with punctuation
    words = [
        {"start_ms": 0, "end_ms": 400, "text": "你好"},
        {"start_ms": 400, "end_ms": 800, "text": "世界"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    # All 6 cn_text chars (incl. comma + period) get a time.
    assert len(char_times) == 6
    # Output preserves the comma + period chars (we don't drop user
    # punctuation just because ASR did).
    cn_chars_out = "".join(ct["text"] for ct in char_times)
    assert cn_chars_out == cn_text


def test_dtw_tolerates_one_char_substitution():
    """ASR substituted one char (homophone confusion). Alignment still
    proceeds and assigns a sensible time to every cn_text char."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    # ASR heard "他" instead of "你" — single char drift
    words = [
        {"start_ms": 0, "end_ms": 400, "text": "他好"},
        {"start_ms": 400, "end_ms": 800, "text": "世界"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert len(char_times) == 4
    # Times still monotone, still cover the audio span.
    assert char_times[0]["start_ms"] >= 0
    assert char_times[-1]["end_ms"] <= 800
    for i in range(1, len(char_times)):
        assert char_times[i]["start_ms"] >= char_times[i - 1]["start_ms"]


# ---------------------------------------------------------------------------
# Disjoint fallback: caller falls back to proportional layout
# ---------------------------------------------------------------------------


def test_dtw_returns_empty_when_text_completely_disjoint():
    """When cn_text and whisper transcript share no normalizable chars
    (severe ASR failure / wrong audio), DTW returns []. Caller falls
    back to proportional layout."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    words = [{"start_ms": 0, "end_ms": 1000, "text": "totally different english"}]
    char_times = align_chars_to_words(cn_text, words)
    assert char_times == []


def test_dtw_returns_empty_for_empty_cn_text():
    """Trivial: no cn_text chars to align."""
    from services.whisper_align.dtw import align_chars_to_words

    words = [{"start_ms": 0, "end_ms": 500, "text": "你好"}]
    assert align_chars_to_words("", words) == []


def test_dtw_returns_empty_for_empty_words():
    """Trivial: no whisper words to align against."""
    from services.whisper_align.dtw import align_chars_to_words

    assert align_chars_to_words("你好世界", []) == []


def test_dtw_returns_empty_when_words_have_zero_duration():
    """Defensive: a malformed word with start >= end can't divide time
    evenly. Treat as no-op rather than crashing."""
    from services.whisper_align.dtw import align_chars_to_words

    words = [{"start_ms": 500, "end_ms": 500, "text": "你好"}]  # 0 duration
    char_times = align_chars_to_words("你好", words)
    assert char_times == []


# ---------------------------------------------------------------------------
# Multi-word: verify boundary placement matches speech, not text-length
# ---------------------------------------------------------------------------


def test_dtw_assigns_first_cn_char_to_first_word_time():
    """The cn_text's first char's start_ms should land near the FIRST
    whisper word's start_ms, not at the global block start. This is
    the whole point of the rewrite: skip leading silence."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "在某个阶段"
    # Speech starts at 300ms (whisper detected leading silence)
    words = [
        {"start_ms": 300, "end_ms": 1400, "text": "在某个阶段"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert char_times[0]["start_ms"] >= 280  # near 300, allow jitter
    assert char_times[0]["start_ms"] <= 320


def test_dtw_assigns_last_cn_char_to_last_word_time():
    """Last char's end_ms tracks the LAST whisper word's end_ms — also
    skipping trailing silence in the audio."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    # Speech ends at 800ms even if the WAV is longer (caller handles slot)
    words = [
        {"start_ms": 100, "end_ms": 800, "text": "你好世界"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert char_times[-1]["end_ms"] <= 800
    assert char_times[-1]["end_ms"] >= 780  # near 800


# ---------------------------------------------------------------------------
# CodeX P1: whisper word text containing leading/inter-word/CJK punctuation
# previously triggered KeyError because the ws-side norm→orig index map
# was wired backwards. These three are now regression tests.
# ---------------------------------------------------------------------------


def test_dtw_handles_whisper_word_with_leading_space():
    """faster-whisper commonly emits ' 你好' (with leading space) for the
    first word in a segment. The space normalizes to '' which used to
    trigger a KeyError in the orig↔norm index lookup. Must not raise."""
    from services.whisper_align.dtw import align_chars_to_words

    char_times = align_chars_to_words(
        "你好",
        [{"start_ms": 0, "end_ms": 1000, "text": " 你好"}],
    )
    # 2 chars aligned, all within the word's span.
    assert len(char_times) == 2
    for ct in char_times:
        assert 0 <= ct["start_ms"] < ct["end_ms"] <= 1000


def test_dtw_handles_whisper_word_with_cjk_comma():
    """ASR often inserts CJK commas (，) which normalize to ''. Must not
    raise; the cn_text chars get sensible times regardless of where the
    punctuation sits in the whisper transcript."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    char_times = align_chars_to_words(
        cn_text,
        [{"start_ms": 0, "end_ms": 1000, "text": "你好，世界"}],
    )
    assert len(char_times) == 4
    # All chars get a time within the word's span.
    for ct in char_times:
        assert 0 <= ct["start_ms"] <= ct["end_ms"] <= 1000


def test_dtw_handles_whisper_word_with_inter_word_space():
    """Common ASR output: '你好 世界' (space between words for English-style
    word separation). Space normalizes to ''; previous bug was a KeyError
    when the alignment mapped a cn char to the post-space norm position."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    char_times = align_chars_to_words(
        cn_text,
        [{"start_ms": 0, "end_ms": 1000, "text": "你好 世界"}],
    )
    assert len(char_times) == 4
    # Times monotone, within span.
    for i in range(1, len(char_times)):
        assert char_times[i]["start_ms"] >= char_times[i - 1]["start_ms"]


def test_dtw_handles_punctuation_dense_whisper_output():
    """Stress-test: whisper output dense with punctuation that all
    normalize to ''. The code must handle it without index errors —
    even when the alignment map is heavily fragmented."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    # Lots of punctuation ASR might emit
    char_times = align_chars_to_words(
        cn_text,
        [
            {"start_ms": 0, "end_ms": 500, "text": "你好,"},
            {"start_ms": 500, "end_ms": 1000, "text": " 世界。"},
        ],
    )
    assert len(char_times) == 4
    # And the comma/period aren't in the cn output text (cn_text had none).
    cn_chars_out = "".join(ct["text"] for ct in char_times)
    assert cn_chars_out == cn_text

"""Contract tests for normalize() text-normalization helper (Task 2 of subtitle-generation-v2).

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §8, §9
"""

import unicodedata

import pytest

from modules.subtitles.cue_models import normalize


# ---------------------------------------------------------------------------
# 1. NFKC: full-width ASCII forms
# ---------------------------------------------------------------------------


def test_nfkc_fullwidth_latin_and_digits():
    """NFKC converts full-width ASCII letters and digits to half-width."""
    assert normalize("ＡＢＣ１２３") == "ABC123"


def test_nfkc_fullwidth_latin_mixed():
    """NFKC handles a mix of full-width and half-width characters."""
    assert normalize("ＡBC１23") == "ABC123"


# ---------------------------------------------------------------------------
# 2. Zero-width character removal
# ---------------------------------------------------------------------------


def test_zwsp_removed():
    """Zero-Width Space (U+200B) is removed."""
    text_with_zwsp = "hel​lo"
    assert normalize(text_with_zwsp) == "hello"


def test_zwnj_removed():
    """Zero-Width Non-Joiner (U+200C) is removed."""
    assert normalize("hel‌lo") == "hello"


def test_zwj_removed():
    """Zero-Width Joiner (U+200D) is removed."""
    assert normalize("hel‍lo") == "hello"


def test_lrm_removed():
    """Left-to-Right Mark (U+200E) is removed."""
    assert normalize("hel‎lo") == "hello"


def test_rlm_removed():
    """Right-to-Left Mark (U+200F) is removed."""
    assert normalize("hel‏lo") == "hello"


# ---------------------------------------------------------------------------
# 3. Format-class (Cf) characters including BOM
# ---------------------------------------------------------------------------


def test_bom_at_start_removed():
    """BOM / ZWNBSP (U+FEFF) at the start of text is removed."""
    text_with_bom = "﻿first line"
    assert normalize(text_with_bom) == "first line"


def test_wj_removed():
    """Word Joiner (U+2060) is removed as a Cf-category char."""
    assert normalize("hel⁠lo") == "hello"


def test_normalize_strips_cf_category_char():
    """Any char whose Unicode category is Cf is stripped by normalize."""
    # ZWSP (U+200B) is Cf; normalize should strip it
    target = "​"
    assert normalize(f"a{target}b") == "ab"


# ---------------------------------------------------------------------------
# 4. Whitespace folding and stripping
# ---------------------------------------------------------------------------


def test_whitespace_fold_and_strip():
    """Consecutive whitespace is collapsed to a single space; leading/trailing stripped."""
    assert normalize("  a  b\t\nc  ") == "a b c"


def test_leading_trailing_whitespace_stripped():
    """Leading and trailing whitespace is removed."""
    assert normalize("  hello world  ") == "hello world"


def test_internal_tab_collapsed():
    """Internal tab is collapsed to a single space."""
    assert normalize("a\t\tb") == "a b"


# ---------------------------------------------------------------------------
# 5. CJK punctuation mapping
# ---------------------------------------------------------------------------


def test_cjk_period_equals_ascii_period():
    """。 (U+3002) normalizes to ASCII '.', so CJK and ASCII period are equal."""
    assert normalize("今天，我说。") == normalize("今天,我说.")


def test_fullwidth_comma_handled_by_nfkc():
    """NFKC turns full-width ，(U+FF0C) to ','; result equals plain ASCII comma."""
    # U+FF0C is full-width comma handled by NFKC
    assert normalize("好，继续") == normalize("好,继续")


def test_cjk_ideographic_comma_maps_to_ascii_comma():
    """、(U+3001) maps to ',' so it equals ASCII comma in comparison."""
    assert normalize("第一、第二") == normalize("第一,第二")


def test_cjk_corner_brackets_map_to_double_quote():
    """「」and 『』 normalize to double-quote for comparison."""
    assert normalize("「你好」") == normalize('"你好"')
    assert normalize("『你好』") == normalize('"你好"')


def test_fullwidth_exclamation_handled_by_nfkc():
    """！(U+FF01) is NFKC-normalized to ASCII '!', no explicit mapping needed."""
    assert normalize("很好！") == normalize("很好!")


def test_fullwidth_question_handled_by_nfkc():
    """？(U+FF1F) is NFKC-normalized to ASCII '?', no explicit mapping needed."""
    assert normalize("真的？") == normalize("真的?")


def test_fullwidth_parens_handled_by_nfkc():
    """（）(U+FF08/FF09) are NFKC-normalized to ASCII '()', no explicit mapping needed."""
    assert normalize("（括号内）") == normalize("(括号内)")


def test_full_sentence_cjk_ascii_equivalence():
    """A sentence with CJK punctuation equals its ASCII-punctuation version after normalize."""
    cjk_version = "今天，我们学习第一课。"
    ascii_version = "今天,我们学习第一课."
    assert normalize(cjk_version) == normalize(ascii_version)


# ---------------------------------------------------------------------------
# 6. Preservation — digits, ASCII letters, CJK, semantic symbols
# ---------------------------------------------------------------------------


def test_digits_preserved():
    """Digit strings pass through normalize unchanged."""
    assert normalize("1024") == "1024"


def test_ascii_letters_preserved():
    """ASCII letter strings pass through normalize unchanged."""
    assert normalize("hello") == "hello"


def test_cjk_characters_preserved():
    """CJK characters are not removed or altered."""
    assert normalize("今天") == "今天"


def test_semantic_symbols_preserved():
    """Semantic symbols +/-=% are preserved."""
    assert normalize("+/-=%") == "+/-=%"


def test_at_underscore_hash_preserved():
    """@ # _ are preserved."""
    assert normalize("@user #tag _name") == "@user #tag _name"


def test_mixed_content_preserved():
    """Mixed CJK + Latin + digits + symbols round-trips correctly."""
    text = "今天 token 数 1024 个 (LLM)"
    assert normalize(text) == "今天 token 数 1024 个 (LLM)"


# ---------------------------------------------------------------------------
# 7. Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ＡＢＣ１２３",
        "hel​lo",
        "  a  b\t\nc  ",
        "今天，我说。",
        "「你好」",
        "Today we use 1024 tokens. ",
        "第一、第二",
        "  \t\n  ",
        "",
        "hello world",
        "今天 token 数 1024 个 (LLM)",
    ],
)
def test_idempotency(text):
    """normalize(normalize(x)) == normalize(x) for all inputs."""
    assert normalize(normalize(text)) == normalize(text)


# ---------------------------------------------------------------------------
# 8. Empty string
# ---------------------------------------------------------------------------


def test_empty_string():
    """normalize('') returns '' without error."""
    assert normalize("") == ""


# ---------------------------------------------------------------------------
# 9. All-whitespace input
# ---------------------------------------------------------------------------


def test_all_whitespace_returns_empty():
    """normalize('   \\t\\n  ') returns '' after fold+strip."""
    assert normalize("   \t\n  ") == ""


# ---------------------------------------------------------------------------
# 10. Critical equality — the actual validator use case
# ---------------------------------------------------------------------------


def test_validator_use_case_trailing_space():
    """Trailing space difference is normalized away (the T6 validator use case)."""
    assert normalize("Today we use 1024 tokens. ") == normalize(
        "Today we use 1024 tokens."
    )


def test_validator_use_case_cjk_vs_ascii_punct_in_join():
    """Cue text with CJK punctuation equals merged_cn_text with ASCII punctuation."""
    # Simulates: normalize("".join(cue.text for cue in block_cues)) == normalize(block.merged_cn_text)
    cue_texts_joined = "今天，我们先看第一个问题。"
    block_merged_cn_text = "今天,我们先看第一个问题."
    assert normalize(cue_texts_joined) == normalize(block_merged_cn_text)


def test_validator_use_case_bom_in_merged_text():
    """BOM at the start of merged_cn_text (from upstream processing) is stripped."""
    with_bom = "﻿今天,我们先看第一个问题."
    without_bom = "今天,我们先看第一个问题."
    assert normalize(with_bom) == normalize(without_bom)

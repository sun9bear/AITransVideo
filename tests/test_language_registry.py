"""Unit tests for services.language_registry (PR-A, plan 2026-06-13 v3 §2.2).

Pure stdlib — no DB, no network, no paid API. Pins:

* the two-pair registry and its keys;
* the GA-baseline default constants (en / zh-CN / en->zh-CN);
* the canonical-language normalizer (aliases, case, whitespace, unknown);
* the pair resolver (default, reverse, alias-driven, fail-closed on unknown);
* the ``adapted_paid_capabilities`` semantics that drive the §2.4 fail-closed
  gate — en->zh-CN fully adapted, zh-CN->en EMPTY.
"""

from __future__ import annotations

import dataclasses

import pytest

from services.language_registry import (
    ALL_PAID_CAPABILITIES,
    CAPABILITY_POST_EDIT,
    CAPABILITY_PROBE,
    CAPABILITY_S2,
    CAPABILITY_SUGGEST_SPLIT,
    DEFAULT_LANGUAGE_PAIR,
    DEFAULT_LANGUAGE_PAIR_PROFILE,
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    LanguagePairProfile,
    SUPPORTED_LANGUAGE_PAIRS,
    is_supported_language_pair,
    make_pair_key,
    normalize_language,
    resolve_language_pair,
)


# ── Default constants (lockstep with migration 036 + model defaults) ──────


def test_default_constants_are_ga_baseline() -> None:
    assert DEFAULT_SOURCE_LANGUAGE == "en"
    assert DEFAULT_TARGET_LANGUAGE == "zh-CN"
    assert DEFAULT_LANGUAGE_PAIR == "en->zh-CN"


def test_make_pair_key_uses_ascii_arrow() -> None:
    assert make_pair_key("zh-CN", "en") == "zh-CN->en"


# ── Capability set ─────────────────────────────────────────────────────────


def test_all_paid_capabilities_membership() -> None:
    assert ALL_PAID_CAPABILITIES == frozenset(
        {
            CAPABILITY_PROBE,
            CAPABILITY_S2,
            CAPABILITY_SUGGEST_SPLIT,
            CAPABILITY_POST_EDIT,
        }
    )


def test_capability_constant_values() -> None:
    assert CAPABILITY_PROBE == "probe"
    assert CAPABILITY_S2 == "s2"
    assert CAPABILITY_SUGGEST_SPLIT == "suggest_split"
    assert CAPABILITY_POST_EDIT == "post_edit"


# ── Supported pairs ────────────────────────────────────────────────────────


def test_exactly_two_supported_pairs() -> None:
    assert set(SUPPORTED_LANGUAGE_PAIRS) == {"en->zh-CN", "zh-CN->en"}


def test_default_pair_is_fully_adapted() -> None:
    profile = SUPPORTED_LANGUAGE_PAIRS["en->zh-CN"]
    assert profile.is_default is True
    assert profile.adapted_paid_capabilities == ALL_PAID_CAPABILITIES
    assert profile.language_pair == "en->zh-CN"
    assert DEFAULT_LANGUAGE_PAIR_PROFILE is profile


def test_zh_en_pair_has_empty_capabilities() -> None:
    """The §2.4 fail-closed contract: zh-CN->en adapts NO paid capability."""
    profile = SUPPORTED_LANGUAGE_PAIRS["zh-CN->en"]
    assert profile.is_default is False
    assert profile.adapted_paid_capabilities == frozenset()
    assert profile.language_pair == "zh-CN->en"
    for cap in ALL_PAID_CAPABILITIES:
        assert profile.supports_paid_capability(cap) is False


def test_default_pair_supports_every_capability() -> None:
    profile = SUPPORTED_LANGUAGE_PAIRS["en->zh-CN"]
    for cap in ALL_PAID_CAPABILITIES:
        assert profile.supports_paid_capability(cap) is True


def test_profile_language_pair_property_matches_key() -> None:
    for key, profile in SUPPORTED_LANGUAGE_PAIRS.items():
        assert profile.language_pair == key


def test_profile_is_immutable() -> None:
    profile = SUPPORTED_LANGUAGE_PAIRS["en->zh-CN"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.source_language = "fr"  # type: ignore[misc]


# ── normalize_language ─────────────────────────────────────────────────────


def test_normalize_canonical_codes() -> None:
    assert normalize_language("en") == "en"
    assert normalize_language("zh-CN") == "zh-CN"


@pytest.mark.parametrize(
    "raw",
    ["EN", "En", "eng", "English", "english", "en-US", "en_GB", "英文", "英语"],
)
def test_normalize_english_aliases(raw: str) -> None:
    assert normalize_language(raw) == "en"


@pytest.mark.parametrize(
    "raw",
    ["zh", "ZH", "zh-cn", "zh_CN", "ZH-Hans", "cmn", "Chinese", "中文", "普通话", "简体中文"],
)
def test_normalize_chinese_aliases(raw: str) -> None:
    assert normalize_language(raw) == "zh-CN"


def test_normalize_strips_whitespace() -> None:
    assert normalize_language("  en  ") == "en"
    assert normalize_language("\tzh-CN\n") == "zh-CN"


@pytest.mark.parametrize("raw", [None, "", "   ", "fr", "jp", "klingon", "zho-xx"])
def test_normalize_unknown_or_empty_returns_none(raw) -> None:
    assert normalize_language(raw) is None


# ── resolve_language_pair ──────────────────────────────────────────────────


def test_resolve_default_pair() -> None:
    assert resolve_language_pair("en", "zh-CN") is SUPPORTED_LANGUAGE_PAIRS["en->zh-CN"]


def test_resolve_reverse_pair() -> None:
    assert resolve_language_pair("zh-CN", "en") is SUPPORTED_LANGUAGE_PAIRS["zh-CN->en"]


def test_resolve_via_aliases() -> None:
    assert resolve_language_pair("English", "Chinese") is SUPPORTED_LANGUAGE_PAIRS["en->zh-CN"]
    assert resolve_language_pair("中文", "EN") is SUPPORTED_LANGUAGE_PAIRS["zh-CN->en"]


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        ("en", "en"),       # same language — not a supported pair
        ("zh-CN", "zh-CN"),
        ("en", "fr"),       # unknown target
        ("fr", "en"),       # unknown source
        ("en", None),
        (None, "zh-CN"),
        (None, None),
    ],
)
def test_resolve_unsupported_returns_none(src, tgt) -> None:
    assert resolve_language_pair(src, tgt) is None


def test_is_supported_language_pair() -> None:
    assert is_supported_language_pair("en", "zh-CN") is True
    assert is_supported_language_pair("zh-CN", "en") is True
    assert is_supported_language_pair("en", "fr") is False
    assert is_supported_language_pair("en", "en") is False

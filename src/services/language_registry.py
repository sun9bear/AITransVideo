"""Canonical language + language-pair registry (PR-A, plan 2026-06-13 v3 §2.2).

Single source of truth for:

* the **canonical** language codes the platform speaks (``en`` / ``zh-CN``);
* the **supported language pairs** and, per pair, which *paid* capabilities have
  actually been language-adapted (``adapted_paid_capabilities``);
* normalization of free-form language input to a canonical code;
* resolution of a (source, target) request to a :class:`LanguagePairProfile`.

Why ``adapted_paid_capabilities`` (and not ``capabilities``): plan §2.2 (D5)
splits the *internal* paid-gate bitset from the *frontend* display bitset
(``workflow_capabilities``, owned by the Gateway facts endpoint — NOT here).
The two must never share a constant name.

The GA, zero-regression baseline is ``en->zh-CN`` — it is fully adapted, so it
carries the full capability set. The first new pair ``zh-CN->en`` ships with an
**empty** set: no paid capability is language-adapted for it yet, which makes
the §2.4 paid-API gate fail closed (no probe / S2 override / suggest-split /
post-edit on a pair we have not adapted). See plan §2.4 + §6 DoD.

Pure stdlib. No network, no paid API, no DB. Imported by both the Gateway
(``services.language_registry`` — ``src/`` is on the gateway ``sys.path``) and
the Job API / pipeline side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical language codes
# ---------------------------------------------------------------------------

LANG_EN = "en"
LANG_ZH_CN = "zh-CN"

#: The only language codes the rest of the system may persist / compare against.
CANONICAL_LANGUAGES: frozenset[str] = frozenset({LANG_EN, LANG_ZH_CN})

#: GA zero-regression baseline. Kept in lockstep with the alembic 036
#: server_default values and the gateway ``Job`` / Job API ``JobRecord`` field
#: defaults (en / zh-CN / en->zh-CN).
DEFAULT_SOURCE_LANGUAGE = LANG_EN
DEFAULT_TARGET_LANGUAGE = LANG_ZH_CN


# ---------------------------------------------------------------------------
# Paid capability names (drive the §2.4 fail-closed gate)
# ---------------------------------------------------------------------------

CAPABILITY_PROBE = "probe"
CAPABILITY_S2 = "s2"
CAPABILITY_SUGGEST_SPLIT = "suggest_split"
CAPABILITY_POST_EDIT = "post_edit"

#: All paid capabilities that a *fully adapted* pair supports. A pair's
#: ``adapted_paid_capabilities`` is a subset of this set.
ALL_PAID_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_PROBE,
        CAPABILITY_S2,
        CAPABILITY_SUGGEST_SPLIT,
        CAPABILITY_POST_EDIT,
    }
)


# ---------------------------------------------------------------------------
# Language-pair key helpers
# ---------------------------------------------------------------------------

#: Canonical separator for the ``language_pair`` string. ASCII ``->`` so the
#: value is stable across DB / logs / metering and matches the migration
#: ``server_default`` literal exactly.
PAIR_SEPARATOR = "->"


def make_pair_key(source_language: str, target_language: str) -> str:
    """Build the canonical ``"{source}->{target}"`` pair key.

    Inputs are expected to already be canonical (callers normalize first via
    :func:`normalize_language`); this function does not normalize.
    """
    return f"{source_language}{PAIR_SEPARATOR}{target_language}"


DEFAULT_LANGUAGE_PAIR = make_pair_key(DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE)


# ---------------------------------------------------------------------------
# Language normalization
# ---------------------------------------------------------------------------

# Free-form alias -> canonical code. Keys are matched case-insensitively
# (ASCII lower-cased before lookup; CJK keys are unaffected by ``lower()``).
_LANGUAGE_ALIASES: dict[str, str] = {
    # --- English ---
    "en": LANG_EN,
    "eng": LANG_EN,
    "en-us": LANG_EN,
    "en_us": LANG_EN,
    "en-gb": LANG_EN,
    "en_gb": LANG_EN,
    "english": LANG_EN,
    "英文": LANG_EN,
    "英语": LANG_EN,
    # --- Mandarin Chinese (Simplified) — canonical "zh-CN" ---
    "zh": LANG_ZH_CN,
    "zh-cn": LANG_ZH_CN,
    "zh_cn": LANG_ZH_CN,
    "zh-hans": LANG_ZH_CN,
    "zh_hans": LANG_ZH_CN,
    "zh-hans-cn": LANG_ZH_CN,
    "cmn": LANG_ZH_CN,
    "chinese": LANG_ZH_CN,
    "mandarin": LANG_ZH_CN,
    "中文": LANG_ZH_CN,
    "简体中文": LANG_ZH_CN,
    "普通话": LANG_ZH_CN,
}


def normalize_language(value: Optional[str]) -> Optional[str]:
    """Normalize free-form language input to a canonical code.

    Returns a value in :data:`CANONICAL_LANGUAGES`, or ``None`` when the input
    is empty or not a recognized alias. Returning ``None`` (rather than echoing
    the input) keeps callers fail-closed: an unknown language can never silently
    masquerade as a supported one.
    """
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    return _LANGUAGE_ALIASES.get(key.lower())


# ---------------------------------------------------------------------------
# Language-pair profile + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguagePairProfile:
    """Immutable description of one supported source->target language pair.

    ``adapted_paid_capabilities`` is the internal paid-gate bitset (plan §2.2 /
    §2.4): the subset of :data:`ALL_PAID_CAPABILITIES` that has been
    language-adapted for this pair. An empty set means *every* paid capability
    fails closed for this pair.
    """

    source_language: str
    target_language: str
    adapted_paid_capabilities: frozenset[str] = field(default_factory=frozenset)
    is_default: bool = False

    @property
    def language_pair(self) -> str:
        """Canonical ``"{source}->{target}"`` key for this profile."""
        return make_pair_key(self.source_language, self.target_language)

    def supports_paid_capability(self, capability: str) -> bool:
        """True iff ``capability`` has been language-adapted for this pair."""
        return capability in self.adapted_paid_capabilities


#: All supported language pairs, keyed by canonical ``language_pair`` string.
#:
#: * ``en->zh-CN`` — GA baseline, fully adapted (all paid capabilities).
#: * ``zh-CN->en`` — first new pair; EMPTY ``adapted_paid_capabilities`` so the
#:   §2.4 paid-API gate fails closed (no probe / S2 override / suggest-split /
#:   post-edit) until each capability is explicitly adapted. Matches §6 DoD:
#:   first release forbids post-edit / suggest-split for this pair.
SUPPORTED_LANGUAGE_PAIRS: dict[str, LanguagePairProfile] = {
    DEFAULT_LANGUAGE_PAIR: LanguagePairProfile(
        source_language=DEFAULT_SOURCE_LANGUAGE,
        target_language=DEFAULT_TARGET_LANGUAGE,
        adapted_paid_capabilities=ALL_PAID_CAPABILITIES,
        is_default=True,
    ),
    make_pair_key(LANG_ZH_CN, LANG_EN): LanguagePairProfile(
        source_language=LANG_ZH_CN,
        target_language=LANG_EN,
        adapted_paid_capabilities=frozenset(),
        is_default=False,
    ),
}

#: The GA default profile, for callers that need it directly.
DEFAULT_LANGUAGE_PAIR_PROFILE: LanguagePairProfile = SUPPORTED_LANGUAGE_PAIRS[
    DEFAULT_LANGUAGE_PAIR
]


def resolve_language_pair(
    source_language: Optional[str],
    target_language: Optional[str],
) -> Optional[LanguagePairProfile]:
    """Resolve a (source, target) request to a supported pair profile.

    Both inputs are normalized via :func:`normalize_language` first. Returns the
    matching :class:`LanguagePairProfile`, or ``None`` when either language is
    unrecognized or the resulting pair is not in
    :data:`SUPPORTED_LANGUAGE_PAIRS`. Fail-closed by design — the caller decides
    how to handle an unsupported pair (it does NOT silently fall back to the
    default pair).
    """
    src = normalize_language(source_language)
    tgt = normalize_language(target_language)
    if src is None or tgt is None:
        return None
    return SUPPORTED_LANGUAGE_PAIRS.get(make_pair_key(src, tgt))


def is_supported_language_pair(
    source_language: Optional[str],
    target_language: Optional[str],
) -> bool:
    """Convenience boolean wrapper around :func:`resolve_language_pair`."""
    return resolve_language_pair(source_language, target_language) is not None

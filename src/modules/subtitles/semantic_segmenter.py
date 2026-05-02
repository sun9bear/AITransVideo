"""Phase 1a semantic segmenter for subtitle-generation-v2.

Splits a block's merged Chinese text into cue-sized spans using only
CJK strong/medium punctuation — never breaking inside an English word.
Mixed-token spans (URLs, numbers, bracket content) are left whole and
flagged needs_review.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.3, §10 Phase 1a

Invariant (enforced by T6 validator):
    normalize("".join(s.text for s in segment_text(t))) == normalize(t)

Note on whitespace: SegmentSpan preserves the raw chunk text from the
segmenter — no leading/trailing whitespace is stripped.  This means
"".join(s.text for s in spans) strictly equals the segmenter input, and
normalize(join) == normalize(input) holds unconditionally for all inputs,
including those with inter-sentence spaces like "今天。 明天。".
Display layers (e.g. the SRT writer) are responsible for stripping
whitespace before rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SegmentSpan:
    """Immutable span produced by segment_text().

    text:          The raw text of this span.  No leading/trailing whitespace
                   is stripped here — SegmentSpan is a faithful raw-chunk
                   container.  Concatenation of all span.text values exactly
                   equals the segmenter input.  Display layers (e.g. the SRT
                   writer) are responsible for stripping before rendering.
    needs_review:  True when the span contains content that Phase 1a
                   cannot safely split further or verify.
    review_reason: One of "unknown_mixed_token" or "long_unbreakable_text",
                   or None when needs_review is False.
    """

    text: str
    needs_review: bool = False
    review_reason: str | None = None


# ---------------------------------------------------------------------------
# Punctuation character sets
# ---------------------------------------------------------------------------

# Strong boundaries (sentence-final): attach to the *preceding* span.
# Full-width ！(U+FF01) and ？(U+FF1F) included explicitly — we operate on
# raw text before NFKC, so handle both forms.
_STRONG_PUNCT = frozenset("。！？!?")

# Medium boundaries (clause-final): ；;：: and CJK ideographic comma 、
_MEDIUM_PUNCT = frozenset("；;：:、")

# Union — all boundary punctuation handled by the segmenter
_BOUNDARY_PUNCT = _STRONG_PUNCT | _MEDIUM_PUNCT

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# English-word run: starts with a letter, continues with letters/digits/_/'/-.
# Apostrophe included for contractions (don't, it's).
# Hyphen at end of char class to avoid range ambiguity.
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")

# Digit run of length >= 2
_DIGIT_RUN_RE = re.compile(r"\d{2,}")

# URL-like: contains :// or starts with www.  Non-greedy match for the path.
_URL_RE = re.compile(r"(?:https?|ftp)://\S+|www\.\S+")

# Email-like: user@domain.tld pattern
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Bracket/quote pairs checked for balance
_BRACKET_PAIRS: list[tuple[str, str]] = [
    ("(", ")"),
    ("[", "]"),
    ("{", "}"),
    ("'", "'"),  # ' '
    (""", """),  # " "
    ("「", "」"),  # 「 」
    ("『", "』"),  # 『 』
    ("（", "）"),  # （ ）
]


# ---------------------------------------------------------------------------
# CJK character detection (using Unicode code-point ranges)
# ---------------------------------------------------------------------------


def _is_cjk_char(ch: str) -> bool:
    """Return True if *ch* is a CJK ideograph.

    Checks standard CJK Unified Ideograph blocks directly by code-point range,
    avoiding complex regex with non-ASCII literal characters.
    """
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF      # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF   # Extension A
        or 0xF900 <= cp <= 0xFAFF   # CJK Compatibility Ideographs
        or 0x20000 <= cp <= 0x2A6DF # Extension B
        or 0x2A700 <= cp <= 0x2B73F # Extensions C/D
    )


# ---------------------------------------------------------------------------
# CJK-character-equivalent length
# ---------------------------------------------------------------------------


def _cjk_equiv_len(text: str) -> float:
    """Return the CJK-character-equivalent length of *text*.

    CJK char = 1.0; other non-whitespace char = 0.5.
    Used for the 40-unit long-span threshold.
    """
    total = 0.0
    for ch in text:
        if ch.isspace():
            continue
        total += 1.0 if _is_cjk_char(ch) else 0.5
    return total


# ---------------------------------------------------------------------------
# URL / email protection: find "no-split zones"
# ---------------------------------------------------------------------------


def _find_protected_ranges(text: str) -> list[tuple[int, int]]:
    """Return a sorted list of (start, end) ranges that must not be split.

    Protected ranges cover URL and email matches within the text.  Any
    boundary character that falls inside one of these ranges is skipped.
    """
    ranges: list[tuple[int, int]] = []
    for m in _URL_RE.finditer(text):
        ranges.append((m.start(), m.end()))
    for m in _EMAIL_RE.finditer(text):
        # Only add if not already covered by a URL range
        start, end = m.start(), m.end()
        if not any(s <= start and end <= e for s, e in ranges):
            ranges.append((start, end))
    ranges.sort()
    return ranges


def _is_protected(pos: int, protected: list[tuple[int, int]]) -> bool:
    """Return True if character at *pos* is inside a protected range."""
    for s, e in protected:
        if s <= pos < e:
            return True
        if s > pos:
            break  # sorted, no need to continue
    return False


# ---------------------------------------------------------------------------
# ASCII period split predicate
# ---------------------------------------------------------------------------


def _ascii_period_is_boundary(text: str, pos: int) -> bool:
    """Return True if the ASCII '.' at *pos* is a sentence-terminal boundary.

    Phase 1a rule: split on ASCII '.' ONLY when ALL of:
    1. The character immediately before it is NOT a letter or digit
       (rules out abbreviations like 'e.g.' and decimals like '3.14').
    2. The character immediately after it is whitespace, end-of-string, or a
       CJK character (avoids splitting 'hello.world' compound tokens).
    """
    assert text[pos] == "."

    # Rule 1: no split if preceded by a letter or digit
    if pos > 0 and (text[pos - 1].isalpha() or text[pos - 1].isdigit()):
        return False

    # Rule 2: only split when followed by whitespace, EOS, or CJK
    after = text[pos + 1] if pos + 1 < len(text) else ""
    if after == "" or after.isspace() or _is_cjk_char(after):
        return True

    return False


# ---------------------------------------------------------------------------
# Core punctuation split
# ---------------------------------------------------------------------------


def _split_on_boundaries(text: str) -> list[str]:
    """Split *text* at strong/medium punctuation boundaries.

    Returns a list of raw substring chunks (never empty / whitespace-only).

    Rules:
    - Punctuation attaches to the *preceding* chunk (trailing attachment).
    - Consecutive boundary punctuation is treated as a single boundary;
      no empty chunk is emitted between consecutive boundary chars.
    - ASCII '.' only triggers a split when _ascii_period_is_boundary() is True.
    - Characters inside URL / email ranges are never treated as boundaries.
    """
    if not text:
        return []

    protected = _find_protected_ranges(text)

    chunks: list[str] = []
    current_start = 0
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        # Never split inside a protected URL/email range
        if _is_protected(i, protected):
            i += 1
            continue

        is_boundary = False
        if ch in _BOUNDARY_PUNCT:
            is_boundary = True
        elif ch == ".":
            is_boundary = _ascii_period_is_boundary(text, i)

        if is_boundary:
            # Advance past any consecutive boundary punctuation so they are
            # all included in the current chunk (trailing attachment).
            j = i + 1
            while j < n and not _is_protected(j, protected) and (
                text[j] in _BOUNDARY_PUNCT
                or (text[j] == "." and _ascii_period_is_boundary(text, j))
            ):
                j += 1

            chunk = text[current_start:j]
            if chunk.strip():
                chunks.append(chunk)
            current_start = j
            i = j
        else:
            i += 1

    # Remaining text after the last boundary
    remainder = text[current_start:]
    if remainder.strip():
        chunks.append(remainder)

    return chunks


# ---------------------------------------------------------------------------
# English-word boundary safety merge
# ---------------------------------------------------------------------------


def _merge_broken_english_words(chunks: list[str]) -> list[str]:
    """Merge chunks where a split point inadvertently falls inside a word-like token.

    Safety net: if chunk[i] stripped ends with '.' AND the character before
    the dot is a letter or digit, merge chunk[i] with chunk[i+1].
    """
    if len(chunks) <= 1:
        return chunks

    merged: list[str] = []
    i = 0
    while i < len(chunks):
        if i + 1 < len(chunks):
            stripped_current = chunks[i].rstrip()
            if (
                stripped_current.endswith(".")
                and len(stripped_current) >= 2
                and (stripped_current[-2].isalpha() or stripped_current[-2].isdigit())
            ):
                # Merge: fold next chunk into the current position and retry.
                chunks[i + 1] = chunks[i] + chunks[i + 1]
                i += 1
                continue
        merged.append(chunks[i])
        i += 1

    return merged


# ---------------------------------------------------------------------------
# Mixed-token detection
# ---------------------------------------------------------------------------


def _has_unbalanced_brackets(text: str) -> bool:
    """Return True if *text* has any unbalanced bracket/quote pair."""
    for opener, closer in _BRACKET_PAIRS:
        if text.count(opener) != text.count(closer):
            return True
    return False


def _is_mixed_token(text: str) -> bool:
    """Return True if *text* contains any Phase 1a mixed-token pattern.

    Patterns: English word run, digit run >= 2, URL-like, email-like,
    unbalanced brackets/quotes.
    """
    if _URL_RE.search(text):
        return True
    if _EMAIL_RE.search(text):
        return True
    if _ENGLISH_WORD_RE.search(text):
        return True
    if _DIGIT_RUN_RE.search(text):
        return True
    if _has_unbalanced_brackets(text):
        return True
    return False


# ---------------------------------------------------------------------------
# "Single-span exception" helper
# ---------------------------------------------------------------------------


def _is_trivial_single_token(text: str) -> bool:
    """Return True if *text* is a trivially single-token input.

    Per spec rule 4: if the whole text is just one English word OR one
    pure-digit sequence (e.g. 'hello', '24'), do NOT mark needs_review.
    The exception is narrow: it must be the entire span, not a word within
    a longer CJK sentence.
    """
    stripped = text.strip()
    # Pure digit(s) only
    if stripped.isdigit():
        return True
    # Single English word (letters only, possibly with apostrophe/hyphen)
    if _ENGLISH_WORD_RE.fullmatch(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Long-span threshold in CJK-character-equivalents (§5.3 rule 5)
_LONG_SPAN_THRESHOLD = 40.0


def segment_text(text: str) -> list[SegmentSpan]:
    """Segment a block's merged Chinese text into cue-sized spans.

    Phase 1a: split only on Chinese strong/medium punctuation; never
    split inside an English word.  Mixed-token spans (URLs, numbers,
    bracket-enclosed content) stay whole and get needs_review.

    Invariant: normalize("".join(s.text for s in result)) == normalize(text)
    (checked by T6 validator).  Note: inter-sentence whitespace stripped
    from span boundaries will be absorbed by normalize()'s whitespace
    collapsing, so the invariant holds for typical merged_cn_text inputs.

    Returns [] for empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []

    # Steps 1+2: punctuation-based split, with URL/email protection.
    chunks = _split_on_boundaries(text)

    # Step 3: safety merge for inadvertently broken English-word runs.
    chunks = _merge_broken_english_words(chunks)

    # Determine whether the result is a trivially single-token input.
    # The "single-span exception" only suppresses review for inputs that
    # are purely one English word or one digit sequence — not for a Chinese
    # sentence that happens to produce one span.
    is_trivial = len(chunks) == 1 and _is_trivial_single_token(text.strip())

    spans: list[SegmentSpan] = []

    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            continue

        needs_review = False
        review_reason: str | None = None

        # Step 5: long unbreakable text check (takes precedence over mixed-token).
        # Use stripped text for length/content analysis; the raw chunk is what
        # goes into the span so that concatenation exactly reproduces the input.
        if _cjk_equiv_len(stripped) > _LONG_SPAN_THRESHOLD:
            needs_review = True
            review_reason = "long_unbreakable_text"

        # Step 4: mixed-token detection.
        # Suppressed only for trivially single-token inputs (e.g. "hello", "24").
        if not needs_review and not is_trivial and _is_mixed_token(stripped):
            needs_review = True
            review_reason = "unknown_mixed_token"

        spans.append(
            SegmentSpan(
                text=chunk,  # raw chunk: concatenation == input (strip is display-layer concern)
                needs_review=needs_review,
                review_reason=review_reason,
            )
        )

    return spans

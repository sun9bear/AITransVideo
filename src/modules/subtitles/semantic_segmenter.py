"""Semantic segmenter for subtitle-generation-v2.

Splits a block's merged Chinese text into cue-sized spans using CJK
strong/medium punctuation and (Phase 1b pulled forward) weak-boundary
punctuation — never breaking inside an English word.
Mixed-token spans (URLs, numbers, bracket content) are left whole and
flagged needs_review.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.3, §5.3.1, §10

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

# Weak boundaries (cut with condition): CJK comma, ASCII comma, em-dash, ellipsis.
# Note: 、 (ideographic comma) is already in _MEDIUM_PUNCT; ，and , are the weak forms.
# ——: two em-dashes (U+2014 U+2014), common Chinese typographic dash pair.
# ……: two ellipsis chars (U+2026 U+2026), common Chinese typographic ellipsis pair.
WEAK_BOUNDARIES = ("，", ",", "——", "……")

# Minimum CJK-char-equivalent length for each side of a weak-boundary split.
# 6.0 means roughly 6 CJK chars or ~12 ASCII chars per side.
_WEAK_MIN_CHUNK = 6.0

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
# Weak-boundary splitting
# ---------------------------------------------------------------------------


def _find_open_bracket_regions(text: str) -> list[tuple[int, int]]:
    """Return list of (start, end) ranges that are inside a bracket/quote pair.

    Used to prevent weak-boundary splitting inside bracket/quote regions.
    We scan left-to-right tracking depth for each bracket pair independently.
    Each closed region (opener..closer inclusive) is added as a protected range.
    Open (unclosed) regions at end-of-text are also protected to be safe.

    Same-char pairs (e.g. ' … ' or " … "): treated as toggle — each occurrence
    alternates between opening and closing at depth 0/1.

    Uses a local bracket-pair list with explicit Unicode code points to avoid any
    file-encoding ambiguity with the module-level _BRACKET_PAIRS constant.
    """
    # Define bracket pairs via explicit code points to avoid encoding issues.
    # Distinct-char pairs: (opener_codepoint, closer_codepoint)
    # Same-char pairs: (codepoint, codepoint) — handled with toggle logic below.
    _LOCAL_BRACKET_PAIRS: list[tuple[str, str]] = [
        ("(", ")"),          # ASCII parens
        ("[", "]"),          # ASCII brackets
        ("{", "}"),          # ASCII braces
        ("「", "」"),  # 「 」
        ("『", "』"),  # 『 』
        ("（", "）"),  # （ ）
        ("“", "”"),  # " " curly double quotes
    ]
    # Same-char quote pairs (toggle mode): single straight quote and backtick
    _SAME_CHAR_QUOTES: list[str] = [
        "'",    # ASCII straight single quote U+0027
        "‘",  # ' left single quotation mark
        "’",  # ' right single quotation mark (may appear as opener too)
    ]

    protected: list[tuple[int, int]] = []
    n = len(text)

    # Distinct-char pairs: depth-tracking
    for opener, closer in _LOCAL_BRACKET_PAIRS:
        depth = 0
        region_start = -1
        for i, ch in enumerate(text):
            if ch == opener:
                if depth == 0:
                    region_start = i
                depth += 1
            elif ch == closer:
                if depth > 0:
                    depth -= 1
                    if depth == 0:
                        protected.append((region_start, i + 1))
                        region_start = -1
        if depth > 0 and region_start >= 0:
            protected.append((region_start, n))

    # Same-char pairs: toggle mode
    for quote_char in _SAME_CHAR_QUOTES:
        in_region = False
        region_start = -1
        for i, ch in enumerate(text):
            if ch == quote_char:
                if not in_region:
                    region_start = i
                    in_region = True
                else:
                    protected.append((region_start, i + 1))
                    in_region = False
        if in_region and region_start >= 0:
            protected.append((region_start, n))

    # Sort and merge overlapping ranges
    if not protected:
        return []
    protected.sort()
    merged: list[tuple[int, int]] = [protected[0]]
    for s, e in protected[1:]:
        if s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _is_inside_english_or_digit_run(text: str, pos: int) -> bool:
    """Return True if the character at *pos* is immediately adjacent to (or within)
    an English-word run or digit run — i.e. splitting here would break a token.

    Specifically, we look at the characters just before and just after *pos*.
    If the char immediately before is a letter/digit, or immediately after is
    a letter/digit, the split is prohibited (avoids splitting "hello,world" or
    "3,500" where the comma is intra-token).
    """
    if pos > 0 and (text[pos - 1].isalpha() or text[pos - 1].isdigit()):
        return True
    if pos < len(text) and (text[pos].isalpha() or text[pos].isdigit()):
        return True
    return False


def _split_weak_boundaries(
    text: str, *, min_chunk_chars: float = _WEAK_MIN_CHUNK
) -> list[str]:
    """Try to split *text* at each WEAK_BOUNDARIES occurrence, but only
    if both sides meet min_chunk_chars (CJK chars count 1, ASCII letters count 0.5).
    Hard prohibition still applies: never split inside an English word run,
    digit run, URL/email run, or unbalanced bracket region.

    Returns list of substrings in order. If no valid split found, returns
    [text] unchanged.  Concatenation of returned list always equals *text*.
    """
    if not text:
        return [text]

    protected_url = _find_protected_ranges(text)
    protected_brackets = _find_open_bracket_regions(text)

    # Merge all protected ranges into one sorted list
    all_protected: list[tuple[int, int]] = sorted(protected_url + protected_brackets)
    # Merge overlaps
    if all_protected:
        merged_protected: list[tuple[int, int]] = [all_protected[0]]
        for s, e in all_protected[1:]:
            if s < merged_protected[-1][1]:
                merged_protected[-1] = (merged_protected[-1][0], max(merged_protected[-1][1], e))
            else:
                merged_protected.append((s, e))
        all_protected = merged_protected

    def _is_prot(pos: int) -> bool:
        for s, e in all_protected:
            if s <= pos < e:
                return True
            if s > pos:
                break
        return False

    n = len(text)
    # Collect candidate split points (position = start of the weak boundary token,
    # split_end = end; the split inserts a break *after* split_end so the boundary
    # attaches to the preceding chunk — consistent with strong/medium trailing attachment).
    # We scan for each weak-boundary token in order.
    candidate_splits: list[int] = []  # split_end positions where we *could* break

    i = 0
    while i < n:
        matched = False
        for wb in WEAK_BOUNDARIES:
            wlen = len(wb)
            if text[i:i + wlen] == wb:
                split_end = i + wlen  # break point: include boundary in left chunk
                # Check protection
                if not any(_is_prot(j) for j in range(i, split_end)):
                    # Check not inside English/digit run.
                    # Use isascii() to restrict to ASCII letters/digits only — CJK
                    # chars also return True for isalpha() in Python, but splitting
                    # at a comma between two CJK chars is exactly what we want.
                    before_ok = not (
                        i > 0
                        and text[i - 1].isascii()
                        and (text[i - 1].isalpha() or text[i - 1].isdigit())
                    )
                    after_ok = not (
                        split_end < n
                        and text[split_end].isascii()
                        and (text[split_end].isalpha() or text[split_end].isdigit())
                    )
                    if before_ok and after_ok:
                        candidate_splits.append(split_end)
                i = split_end
                matched = True
                break
        if not matched:
            i += 1

    if not candidate_splits:
        return [text]

    # Now greedily select splits where both left and right sides meet min_chunk_chars.
    # Strategy: try to find valid split points.  We scan from left to right,
    # keeping track of the current segment start.  A candidate split is accepted
    # only when (a) the left side from current_start to split_end has >= min_chunk_chars,
    # AND (b) there is enough content remaining on the right side.

    # Pre-compute cumulative CJK-equiv lengths for fast range queries
    cum: list[float] = [0.0] * (n + 1)
    for j, ch in enumerate(text):
        if ch.isspace():
            cum[j + 1] = cum[j]
        else:
            cum[j + 1] = cum[j] + (1.0 if _is_cjk_char(ch) else 0.5)

    def _range_len(start: int, end: int) -> float:
        return cum[end] - cum[start]

    total_len = cum[n]
    if total_len < min_chunk_chars * 2:
        # Not enough content to split at all
        return [text]

    result: list[str] = []
    current_start = 0

    for sp in candidate_splits:
        left_len = _range_len(current_start, sp)
        right_len = _range_len(sp, n)
        if left_len >= min_chunk_chars and right_len >= min_chunk_chars:
            result.append(text[current_start:sp])
            current_start = sp

    # Append remainder (may be the whole text if no split was accepted)
    result.append(text[current_start:])

    # Filter out accidental empty strings while preserving concatenation.
    # (Empty strings can only arise if two splits land at the same position,
    # which shouldn't happen given our scan, but be defensive.)
    result = [s for s in result if s]
    if not result:
        return [text]

    return result


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

    Three-pass split:
    1. Strong/medium punctuation split (。！？!? and ；;：:、).
    2. Safety merge for inadvertently broken English-word runs.
    3. Weak-boundary split (，, , ——, ……) with min-chunk guard.

    Never splits inside an English word, digit run, URL/email, or
    unbalanced bracket region.  Mixed-token spans stay whole and get
    needs_review.

    Invariant: normalize("".join(s.text for s in result)) == normalize(text)
    (checked by T6 validator).  Note: inter-sentence whitespace stripped
    from span boundaries will be absorbed by normalize()'s whitespace
    collapsing, so the invariant holds for typical merged_cn_text inputs.

    Returns [] for empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []

    # Pass 1: strong/medium punctuation-based split, with URL/email protection.
    chunks = _split_on_boundaries(text)

    # Pass 2: safety merge for inadvertently broken English-word runs.
    chunks = _merge_broken_english_words(chunks)

    # Pass 3: weak-boundary split on each surviving chunk.
    expanded_chunks: list[str] = []
    for chunk in chunks:
        sub = _split_weak_boundaries(chunk)
        expanded_chunks.extend(sub)
    chunks = expanded_chunks

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

        # Long unbreakable text check (takes precedence over mixed-token).
        # Use stripped text for length/content analysis; the raw chunk is what
        # goes into the span so that concatenation exactly reproduces the input.
        if _cjk_equiv_len(stripped) > _LONG_SPAN_THRESHOLD:
            needs_review = True
            review_reason = "long_unbreakable_text"

        # Mixed-token detection.
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

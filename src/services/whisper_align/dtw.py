"""Dynamic-time-warping char alignment between cn_text and whisper words.

Phase C of 2026-05-04-subtitle-audio-sync-plan.

Public API: ``align_chars_to_words(cn_text, words) -> list[char_time]``

Each ``char_time`` entry is ``{"start_ms": int, "end_ms": int, "text": str}``
where ``text`` is the corresponding cn_text character (we always serve
OUR text — whisper transcription is a TIMING source, not a TEXT source).

Algorithm:
  1. Flatten whisper words into a single string ``ws_text`` plus a
     parallel ``[(start_ms, end_ms), ...]`` array, with each char's
     time linearly interpolated within its word's [start, end].
  2. Normalize both strings for comparison: digits → Chinese digit chars,
     strip ASCII + CJK punctuation, lowercase.
  3. Run Levenshtein-style DP to find an alignment between the two
     normalized strings. Each cn_text char gets mapped to a whisper
     char position (or "no match").
  4. For each cn_text char:
       - matched / substituted → use that whisper char's time
       - inserted (cn_text has extra char) → interpolate from neighbors
  5. If alignment is too noisy (>50% of cn_text chars insert against
     whisper), return [] — disjoint case, caller falls back.

The cue pipeline only consumes char start/end ms. ``text`` is included
for debug logging and quality-report inspection.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Arabic digits 0-9 → Chinese digit chars. Dual-digit numbers like "20" →
# "二零" (not "二十") — we lose the structural meaning, but that's fine
# because we're just computing an alignment for time, not preserving
# semantic accuracy.
_ARABIC_TO_CHINESE_DIGIT = str.maketrans("0123456789", "零一二三四五六七八九")

# Stripped during normalize: ASCII + common CJK punctuation + whitespace.
# Comparison happens on letters/digits/CJK only.
_PUNCT_RE = re.compile(
    r"["
    r"\s"
    r",.;:!?'\"()\[\]{}<>/\\|`~@#$%^&*+=\-_"
    r"，。；：！？、、‘’“”（）【】「」《》—…·"
    r"]+",
    flags=re.UNICODE,
)


def _normalize_for_compare(s: str) -> str:
    """Lowercase + digits → Chinese chars + strip punctuation/whitespace.

    ONLY used for matching during DP. The output time series uses the
    original cn_text characters verbatim — normalization never leaks
    into the displayed subtitle text.
    """
    if not s:
        return ""
    s = s.translate(_ARABIC_TO_CHINESE_DIGIT)
    s = _PUNCT_RE.sub("", s)
    return s.lower()


# ---------------------------------------------------------------------------
# Whisper word stream → flat (chars, char_times)
# ---------------------------------------------------------------------------


def _flatten_words(words: list[dict]) -> tuple[str, list[tuple[int, int]]]:
    """Concatenate words' text into a single string, build per-char times.

    Each char inside a word gets ``(word_start + i*step, word_start + (i+1)*step)``
    where ``step = (word_end - word_start) / len(word)``. Words with
    zero duration or empty text are skipped — they can't contribute
    timing information.

    Returns ``("", [])`` if every word is malformed.
    """
    flat_chars: list[str] = []
    flat_times: list[tuple[int, int]] = []
    for word in words:
        text = str(word.get("text", "") or "")
        start = int(word.get("start_ms", 0) or 0)
        end = int(word.get("end_ms", 0) or 0)
        if not text or end <= start:
            continue
        n = len(text)
        step = (end - start) / n
        for i, ch in enumerate(text):
            flat_chars.append(ch)
            flat_times.append((
                int(start + i * step),
                int(start + (i + 1) * step),
            ))
    return "".join(flat_chars), flat_times


# ---------------------------------------------------------------------------
# Levenshtein DP — produces alignment trace
# ---------------------------------------------------------------------------


def _alignment_trace(cn_norm: str, ws_norm: str) -> list[int | None]:
    """Compute an alignment from cn_norm chars to ws_norm chars.

    Returns a list ``alignment`` of length ``len(cn_norm)`` where
    ``alignment[i] = j`` means cn_norm[i] aligns to ws_norm[j]
    (match or substitution). ``alignment[i] = None`` means cn_norm[i]
    is an insertion (no whisper char to anchor on).

    Uses standard Levenshtein DP with backtracking (O(N*M) time/space,
    fine for typical block size N,M < 200).
    """
    n, m = len(cn_norm), len(ws_norm)
    if n == 0 or m == 0:
        return [None] * n

    # dp[i][j] = min edits to align cn_norm[:i] vs ws_norm[:j].
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if cn_norm[i - 1] == ws_norm[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # delete from cn_norm
                    dp[i][j - 1],      # insert into cn_norm
                    dp[i - 1][j - 1],  # substitute
                )

    # Backtrack: walk from (n, m) to (0, 0). At each step decide what
    # operation produced the cell, and record per-cn-char alignment.
    alignment: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        if cn_norm[i - 1] == ws_norm[j - 1]:
            alignment[i - 1] = j - 1
            i -= 1
            j -= 1
        else:
            sub = dp[i - 1][j - 1]
            delete = dp[i - 1][j]
            insert = dp[i][j - 1]
            best = min(sub, delete, insert)
            if best == sub:
                # Substitute — still anchors cn[i-1] to ws[j-1] for timing.
                alignment[i - 1] = j - 1
                i -= 1
                j -= 1
            elif best == delete:
                # cn[i-1] has no ws counterpart (will need interpolation).
                alignment[i - 1] = None
                i -= 1
            else:
                # ws[j-1] consumed without a cn char — just advance ws.
                j -= 1
    # Remaining cn chars (i > 0) at the start are insertions.
    while i > 0:
        alignment[i - 1] = None
        i -= 1
    return alignment


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


# Threshold for declaring the alignment "too noisy" → caller falls back.
# We measure by EXACT-MATCH ratio (not by alignment ratio) because Levenshtein
# DP greedily prefers substitutions over delete+insert (sub costs 1 vs 2),
# so a wholly disjoint cn_text vs ws_text would still produce a 100%
# "aligned" trace where every cn char is "aligned" to some random ws char.
# Real alignments share a non-trivial fraction of post-normalize chars.
_MIN_MATCH_RATIO = 0.3


def align_chars_to_words(cn_text: str, words: list[dict]) -> list[dict]:
    """Map each cn_text char to a (start_ms, end_ms) time slice.

    Returns ``[]`` when alignment is impossible / too noisy (caller's
    cue pipeline falls back to existing proportional layout).
    """
    if not cn_text or not words:
        return []

    ws_text, ws_char_times = _flatten_words(words)
    if not ws_text or not ws_char_times:
        return []

    # Build BOTH index direction maps for cn and ws sides:
    # cn side needs orig→norm (to find "given a cn orig char index, where
    # is it in cn norm space?" → look up alignment[norm_idx] to get
    # corresponding ws norm position).
    # ws side needs norm→orig (to find "given an alignment ws norm index,
    # which ws orig char does that point at?" → for time lookup).
    # CodeX P1 (2026-05-04): the previous helper returned only
    # orig_to_norm and the caller treated it as norm_to_orig on the ws
    # side, causing KeyError when a ws orig char normalized to ""
    # (leading whitespace, CJK comma, inter-word space). Returning both
    # directions explicitly removes the foot-gun.
    cn_norm, cn_orig_to_norm, _cn_norm_to_orig = _normalize_with_index_maps(cn_text)
    ws_norm, _ws_orig_to_norm, ws_norm_to_orig = _normalize_with_index_maps(ws_text)

    if not cn_norm or not ws_norm:
        # Whole text was just punctuation / whitespace — no shared
        # signal. Assign proportional times across the whisper span.
        return _proportional_fallback_within_words(cn_text, ws_char_times)

    alignment = _alignment_trace(cn_norm, ws_norm)

    # Disjoint check: count EXACT matches (not substitutions) — only
    # those imply the cn_text and ws_text actually share content.
    exact_match_count = sum(
        1 for cn_idx, ws_idx in enumerate(alignment)
        if ws_idx is not None and cn_norm[cn_idx] == ws_norm[ws_idx]
    )
    if exact_match_count / len(cn_norm) < _MIN_MATCH_RATIO:
        # Too noisy — caller falls back.
        return []

    # For each char in cn_text (original, not normalized), compute its time.
    char_times: list[dict] = []
    cn_orig_len = len(cn_text)
    for orig_idx in range(cn_orig_len):
        # Find which normalized cn-char index this orig_idx maps to (if any).
        cn_norm_idx = cn_orig_to_norm.get(orig_idx)
        if cn_norm_idx is None:
            # This orig char was punctuation/whitespace — interpolate.
            time = _interpolate_neighbor_time(
                char_times, orig_idx, cn_orig_len, ws_char_times,
            )
        else:
            ws_norm_idx = alignment[cn_norm_idx]
            if ws_norm_idx is None:
                # cn char has no ws anchor — interpolate.
                time = _interpolate_neighbor_time(
                    char_times, orig_idx, cn_orig_len, ws_char_times,
                )
            else:
                # ws_norm_idx is a position in ws_norm space; convert back
                # to ws_text orig position to look up the per-char time.
                # Defensive .get() — alignment indices SHOULD be valid by
                # construction, but a malformed alignment trace must not
                # raise; fall back to interpolation instead.
                ws_orig_idx = ws_norm_to_orig.get(ws_norm_idx)
                if ws_orig_idx is None or ws_orig_idx >= len(ws_char_times):
                    time = _interpolate_neighbor_time(
                        char_times, orig_idx, cn_orig_len, ws_char_times,
                    )
                else:
                    time = ws_char_times[ws_orig_idx]
        char_times.append({
            "start_ms": time[0],
            "end_ms": time[1],
            "text": cn_text[orig_idx],
        })

    # Enforce monotonicity: if interpolation produced a non-increasing
    # boundary, snap to the previous char's end.
    for i in range(1, len(char_times)):
        if char_times[i]["start_ms"] < char_times[i - 1]["end_ms"]:
            char_times[i]["start_ms"] = char_times[i - 1]["end_ms"]
        if char_times[i]["end_ms"] < char_times[i]["start_ms"]:
            char_times[i]["end_ms"] = char_times[i]["start_ms"]
    return char_times


def _normalize_with_index_maps(
    s: str,
) -> tuple[str, dict[int, int], dict[int, int]]:
    """Normalize a string and return BOTH index direction maps.

    Returns ``(normalized_string, orig_to_norm, norm_to_orig)``:
    - ``orig_to_norm[orig_idx]`` → first norm position for this orig char.
      Only entries for chars that survived normalization. Used cn-side
      to find "where in norm space did this cn char land?".
    - ``norm_to_orig[norm_idx]`` → orig position this norm char came from.
      Used ws-side to find "given the alignment's norm index, which
      orig ws char is that?".

    For a char that normalizes to multiple norm chars (e.g. Arabic "20"
    → "二零"), ``orig_to_norm`` records the FIRST norm position (so
    callers reading "what time is this orig char at" pick up the start
    of the run). Each of the multiple norm positions maps back to the
    same orig in ``norm_to_orig``.

    Both returns are needed: returning only ``orig_to_norm`` and
    inverting it via ``{v: k}`` is wrong when one orig char normalizes
    to multiple norm chars (the inverse drops all but the last).
    """
    norm_chars: list[str] = []
    orig_to_norm: dict[int, int] = {}
    norm_to_orig: dict[int, int] = {}
    for orig_idx, ch in enumerate(s):
        norm_ch = _normalize_for_compare(ch)
        if norm_ch:
            first_norm_idx = len(norm_chars)
            orig_to_norm[orig_idx] = first_norm_idx
            for nc in norm_ch:
                norm_to_orig[len(norm_chars)] = orig_idx
                norm_chars.append(nc)
    return "".join(norm_chars), orig_to_norm, norm_to_orig


def _interpolate_neighbor_time(
    char_times_so_far: list[dict],
    orig_idx: int,
    total_orig: int,
    ws_char_times: list[tuple[int, int]],
) -> tuple[int, int]:
    """Estimate a time for an unanchored cn char by interpolating between
    its neighbors. Falls back to global proportional if no neighbors exist."""
    if char_times_so_far:
        # Use previous char's end as this char's start — assume small
        # constant char duration (~80ms; matches typical CJK speaking rate).
        prev_end = char_times_so_far[-1]["end_ms"]
        return (prev_end, prev_end + 80)
    # First char with no prior — start at the very first ws char's start.
    if ws_char_times:
        return ws_char_times[0]
    return (0, 0)


def _proportional_fallback_within_words(
    cn_text: str,
    ws_char_times: list[tuple[int, int]],
) -> list[dict]:
    """When normalize produced empty signal but ws span exists, divide
    the ws span proportionally across cn_text chars. Last-resort path
    that still beats showing no times at all."""
    if not cn_text or not ws_char_times:
        return []
    span_start = ws_char_times[0][0]
    span_end = ws_char_times[-1][1]
    if span_end <= span_start:
        return []
    n = len(cn_text)
    step = (span_end - span_start) / n
    return [
        {
            "start_ms": int(span_start + i * step),
            "end_ms": int(span_start + (i + 1) * step),
            "text": cn_text[i],
        }
        for i in range(n)
    ]


__all__ = ["align_chars_to_words"]

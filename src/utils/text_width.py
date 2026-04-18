"""Display-width utilities for CJK-aware string truncation.

Used by the post-edit task naming pipeline to produce ``display_name``
values that fit in a fixed visual width on task cards, regardless of
whether the underlying text is pure ASCII, pure CJK, or mixed.

Width convention:

- ASCII / narrow characters ............ 1 unit
- CJK (Wide + Fullwidth) ............... 2 units
- Emoji / misc symbols (Ambiguous) ..... 2 units (CJK-context default)
- Control / zero-width characters ...... 0 units

This matches the heuristic used by shells, terminals, and Asian CMS systems,
and is what the UI designer implicitly assumes when they say "12 CJK chars".

Parity with the frontend ``frontend-next/src/lib/text/width.ts`` is
verified via shared fixture tests (``tests/test_text_width.py``).
"""

from __future__ import annotations

import unicodedata

__all__ = ["display_width", "truncate_to_width"]


def display_width(s: str) -> int:
    """Return the sum of per-character display widths.

    >>> display_width("hi")
    2
    >>> display_width("你好")
    4
    >>> display_width("hi你好")
    6
    """
    if not s:
        return 0
    total = 0
    for char in s:
        total += _char_width(char)
    return total


def truncate_to_width(s: str, max_width: int) -> str:
    """Return the longest prefix of ``s`` whose display width is <= ``max_width``.

    Never splits mid-character (a CJK char of width 2 is either fully kept or
    fully dropped — never rendered as a half-width artifact).

    >>> truncate_to_width("hello world", 5)
    'hello'
    >>> truncate_to_width("你好世界", 5)
    '你好'
    >>> truncate_to_width("hi你好", 4)
    'hi你'
    >>> truncate_to_width("", 10)
    ''
    >>> truncate_to_width("hi", 0)
    ''
    """
    if max_width <= 0 or not s:
        return ""
    out: list[str] = []
    width = 0
    for char in s:
        cw = _char_width(char)
        if width + cw > max_width:
            break
        out.append(char)
        width += cw
    return "".join(out)


def _char_width(char: str) -> int:
    # Control / zero-width / combining marks contribute nothing.
    category = unicodedata.category(char)
    if category in {"Cc", "Cf", "Mn", "Me"}:
        return 0
    # east_asian_width: 'W' = Wide, 'F' = Fullwidth (CJK ideographs, fullwidth
    # punctuation, etc). 'A' = Ambiguous — in CJK contexts these render as
    # double-width, so we count them as 2 to match what task card designers
    # expect when specifying "12 CJK characters".
    eaw = unicodedata.east_asian_width(char)
    if eaw in {"W", "F", "A"}:
        return 2
    return 1

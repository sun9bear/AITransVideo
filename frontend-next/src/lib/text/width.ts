/**
 * Display-width utilities for CJK-aware string truncation.
 *
 * Mirror of `src/utils/text_width.py` — shared fixture tests in
 * `tests/test_text_width.py` ensure both implementations agree on width
 * and truncation boundaries, so that a ``display_name`` generated on the
 * backend renders exactly as expected on the frontend card.
 *
 * Width convention (same as Python):
 * - ASCII / narrow ............. 1 unit
 * - CJK (Wide + Fullwidth) ..... 2 units
 * - Ambiguous (emoji, etc.) .... 2 units (CJK-context default)
 * - Control / zero-width ....... 0 units
 */

/** Return the sum of per-character display widths. */
export function displayWidth(s: string): number {
  if (!s) return 0
  let total = 0
  // Iterate by code point (not UTF-16 unit) so supplementary plane chars
  // (e.g. some CJK extensions, most emoji) are counted as a single char.
  for (const char of s) {
    total += charWidth(char)
  }
  return total
}

/**
 * Return the longest prefix of `s` whose display width is <= maxWidth.
 * Never splits mid-character.
 */
export function truncateToWidth(s: string, maxWidth: number): string {
  if (maxWidth <= 0 || !s) return ''
  let out = ''
  let width = 0
  for (const char of s) {
    const cw = charWidth(char)
    if (width + cw > maxWidth) break
    out += char
    width += cw
  }
  return out
}

function charWidth(char: string): number {
  // Combining marks, zero-width, directional formatting — all 0.
  // We check a few known ranges; browsers don't expose Unicode category
  // directly, but these ranges cover the common cases.
  const cp = char.codePointAt(0) ?? 0
  if (cp === 0) return 0
  // C0/C1 controls
  if (cp < 0x20 || (cp >= 0x7f && cp <= 0x9f)) return 0
  // Combining diacritics / marks
  if (
    (cp >= 0x0300 && cp <= 0x036f) ||
    (cp >= 0x1ab0 && cp <= 0x1aff) ||
    (cp >= 0x1dc0 && cp <= 0x1dff) ||
    (cp >= 0x20d0 && cp <= 0x20ff) ||
    (cp >= 0xfe20 && cp <= 0xfe2f)
  ) {
    return 0
  }
  // Zero-width / directional
  if (cp === 0x200b || cp === 0x200c || cp === 0x200d || cp === 0xfeff) return 0

  if (isWideOrFullwidth(cp)) return 2
  return 1
}

/**
 * Approximation of `east_asian_width ∈ {W, F, A}` from Unicode.
 * Not every code point, but enough to cover CJK characters, fullwidth forms,
 * Hangul, kana, CJK symbols & punctuation, and most emoji (ambiguous in CJK).
 */
function isWideOrFullwidth(cp: number): boolean {
  return (
    // CJK Unified Ideographs + extensions
    (cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
    (cp >= 0x2e80 && cp <= 0x303e) || // CJK Radicals Supplement / Kangxi / CJK Symbols
    (cp >= 0x3041 && cp <= 0x33ff) || // Hiragana / Katakana / Bopomofo / Hangul Compat / Kanbun / CJK Strokes
    (cp >= 0x3400 && cp <= 0x4dbf) || // CJK Ext A
    (cp >= 0x4e00 && cp <= 0x9fff) || // CJK Unified Ideographs
    (cp >= 0xa000 && cp <= 0xa4cf) || // Yi Syllables / Yi Radicals
    (cp >= 0xac00 && cp <= 0xd7a3) || // Hangul Syllables
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK Compatibility Ideographs
    (cp >= 0xfe30 && cp <= 0xfe4f) || // CJK Compatibility Forms
    (cp >= 0xff00 && cp <= 0xff60) || // Fullwidth Forms
    (cp >= 0xffe0 && cp <= 0xffe6) || // Fullwidth signs
    (cp >= 0x1f300 && cp <= 0x1f64f) || // Misc symbols & pictographs / emoticons
    (cp >= 0x1f680 && cp <= 0x1f6ff) || // Transport & map
    (cp >= 0x1f900 && cp <= 0x1f9ff) || // Supplemental symbols
    (cp >= 0x20000 && cp <= 0x2fffd) || // CJK Ext B..F
    (cp >= 0x30000 && cp <= 0x3fffd)    // CJK Ext G..H
  )
}

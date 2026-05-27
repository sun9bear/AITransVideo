"""Phase 4.2 E.1 — VoiceSelectionPanel + VoiceModifyTab wiring guards.

E.1 wires the D.2 CosyVoice clone modal into the two clone entry points
(`VoiceSelectionPanel.tsx` line ~994 + `VoiceModifyTab.tsx` line ~1276)
via provider-aware onClick dispatch. The MiniMax legacy path remains
untouched.

These guards lock the **mutual exclusion** between the two clone paths
so neither can silently dispatch into the other (the 2026-05-26 incident
shape):

**Forward guards** — new CosyVoice code must not regress to MiniMax:

- **G6.1.4** — `cosyvoiceClone.ts` / `CosyVoiceCloneModal.tsx` /
  `CosyVoiceConsentModal.tsx` must not contain `/jobs/` / `voice-clone` /
  `cloneVoiceForSelection`. Whole-file scan is safe because these are new
  D.2 files that should never reference the legacy path.

**Reverse guards (scoped)** — MiniMax legacy code must stay clean:

- **G6.1.5 / G_MX.2** — `VoiceSelectionPanel.tsx::VoiceCloneModal`
  **function body** must not contain `cosyvoice` / `CosyVoiceCloneModal` /
  `submitCosyvoiceClone` / `/api/voice/cosyvoice/`. **Function-body
  scoped** — file top-level imports + onClick dispatch are explicitly
  allowed to reference CosyVoice (that's how E.1 wiring works).

- **G_MX.3** — `VoiceModifyTab.tsx` MiniMax clone region (around the
  `<VoiceCloneModal>` JSX block + the `cloneModalSpeaker` setState
  branches) must not contain `cosyvoice`. **Scoped to those line
  windows** — the new CosyVoice region in the same file is explicitly
  allowed (and required).

- **G_MX.1** — `voiceSelection.ts::cloneVoiceForSelection` function body
  must still call `/jobs/${jobId}/voice-clone`. Locked at the function-
  body level so unrelated module changes don't affect this guard.

**URL mutual exclusion**:

- **G6.5.3** — Set of fetch URLs from `cosyvoiceClone.ts` is disjoint
  from set of fetch URLs from `voiceSelection.ts::cloneVoiceForSelection`.
  Static analysis — no JS test runner needed.

Per Codex 2026-05-27 review (spec v2): NO JS test runner introduction.
All guards are Python text + simple regex; AST-style scoping handled by
line-window extraction (full TypeScript AST parsing is out of scope and
unnecessary for this guard set).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FE_DIR = REPO_ROOT / "frontend-next" / "src"

# D.2 files — forward guard scope (G6.1.4)
COSYVOICE_FILES = [
    FE_DIR / "lib" / "api" / "cosyvoiceClone.ts",
    FE_DIR / "components" / "voice-clone" / "CosyVoiceCloneModal.tsx",
    FE_DIR / "components" / "voice-clone" / "CosyVoiceConsentModal.tsx",
]

# MiniMax legacy entry points — reverse guard scope (G6.1.5 / G_MX.*)
VOICE_SELECTION_PANEL = (
    FE_DIR / "components" / "workspace" / "VoiceSelectionPanel.tsx"
)
VOICE_MODIFY_TAB = (
    FE_DIR / "app" / "(app)" / "workspace" / "[jobId]" / "edit" / "VoiceModifyTab.tsx"
)
VOICE_SELECTION_API = FE_DIR / "lib" / "api" / "voiceSelection.ts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comments(src: str) -> str:
    """Strip TS/TSX ``//`` line comments + ``/* ... */`` block comments.

    Guards focus on actual code; explanatory comments mentioning forbidden
    patterns ("this file does NOT use cosyvoice") must not false-positive.
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _extract_function_body(src: str, signature_regex: str) -> str:
    """Extract a TS function body starting at the line matching
    ``signature_regex``.

    Procedure (handles TS return-type braces correctly):
    1. Match ``signature_regex`` — must capture up to and including the
       opening ``(`` of the parameter list.
    2. Balance-scan ``(`` / ``)`` to find the closing ``)`` of params.
    3. From there, skip the optional return-type annotation
       (``: Promise<{ ... }>``). Naively: find the next ``{`` that is
       NOT inside a ``<...>`` generic. Implementation: track angle-bracket
       depth alongside paren/brace depth, and only consider a ``{`` as
       the body opener when angle-depth == 0.
    4. Balance-scan ``{`` / ``}`` to find the body's closing brace.

    Returns the slice from the opening body ``{`` to the closing ``}``.
    """
    m = re.search(signature_regex, src)
    if not m:
        raise AssertionError(
            f"Function signature regex did not match: {signature_regex}"
        )
    # Step 2: find the matching `)` of the param list.
    paren_depth = 1  # signature_regex already includes the opening `(`
    j = m.end()
    while j < len(src) and paren_depth > 0:
        if src[j] == "(":
            paren_depth += 1
        elif src[j] == ")":
            paren_depth -= 1
        j += 1
    # j now points just past the closing `)`.
    # Step 3: from j, scan forward to the next `{` that is NOT inside
    # `<...>` generic type. Skip any `: Promise<{ ... }>` return type.
    angle_depth = 0
    body_open = -1
    k = j
    while k < len(src):
        c = src[k]
        if c == "<":
            angle_depth += 1
        elif c == ">":
            if angle_depth > 0:
                angle_depth -= 1
        elif c == "{" and angle_depth == 0:
            body_open = k
            break
        k += 1
    if body_open < 0:
        raise AssertionError(
            f"Couldn't find function-body opening brace for: {signature_regex}"
        )
    # Step 4: balance-scan braces.
    depth = 0
    p = body_open
    while p < len(src):
        c = src[p]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[body_open : p + 1]
        p += 1
    raise AssertionError(
        f"Unbalanced body braces for: {signature_regex}"
    )


def _extract_jsx_region(src: str, anchor_substring: str, before: int = 200, after: int = 600) -> str:
    """Extract a line-window around an anchor substring. Used for JSX-level
    scoping where we want to lock a small region of the file without full
    TSX parsing.
    """
    idx = src.find(anchor_substring)
    assert idx >= 0, f"Anchor not found: {anchor_substring!r}"
    start = max(0, idx - before)
    end = min(len(src), idx + after)
    return src[start:end]


def _http_call_urls_from_code(code: str) -> set[str]:
    """Extract URL literals from HTTP-call sites.

    Two call styles in this codebase:
      - Raw browser API: ``fetch(URL, ...)`` (used by cosyvoiceClone.ts)
      - apiClient wrapper: ``apiClient.post(URL, ...)`` /
        ``apiClient.get(URL, ...)`` (used by voiceSelection.ts)

    Both can pass URL as ``"..."``, ``'...'``, or ```template${expr}```.
    Returns the raw URL string (template literals kept as-is, including
    ``${...}`` interpolation markers — substring checks operate on these).
    """
    urls: set[str] = set()
    # Pattern: <verb>(  <quote-or-backtick> ... ) — we capture the URL.
    # Verbs: fetch / apiClient.<method>
    call_patterns = [
        r"""fetch\s*\(\s*""",
        r"""apiClient\.(?:get|post|put|patch|delete)\s*<[^>]*>?\s*\(\s*""",
        r"""apiClient\.(?:get|post|put|patch|delete)\s*\(\s*""",
    ]
    for verb in call_patterns:
        for m in re.finditer(verb + r"""(['"])([^'"]+)\1""", code):
            urls.add(m.group(2))
        for m in re.finditer(verb + r"""`([^`]+)`""", code):
            urls.add(m.group(1))
    return urls


# ---------------------------------------------------------------------------
# G6.1.4 — CosyVoice modules must not contain MiniMax legacy literals
# ---------------------------------------------------------------------------


def test_g6_1_4_cosyvoice_files_have_no_minimax_legacy_literals():
    """**G6.1.4 hard guard** (plan §6.1 line 563-568):

    The three D.2 CosyVoice files must not contain literal strings
    ``/jobs/`` / ``voice-clone`` / ``cloneVoiceForSelection``. These are
    the MiniMax legacy endpoint markers and importing them would silently
    route CosyVoice clicks to MiniMax dispatch.
    """
    # Use endpoint URL fragments (leading slash) so HTML id substrings like
    # `cosyvoice-clone-speaker-name` don't false-positive on "voice-clone".
    forbidden = ["/jobs/", "/voice-clone", "cloneVoiceForSelection"]
    for path in COSYVOICE_FILES:
        assert path.exists(), f"D.2 file missing: {path}"
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for lit in forbidden:
            assert lit not in code, (
                f"{path.name}: forbidden MiniMax-legacy literal `{lit}` "
                f"found in CosyVoice module code. Allowed only in comments."
            )


# ---------------------------------------------------------------------------
# G6.1.5 / G_MX.2 — VoiceCloneModal function body (MiniMax) must stay clean
# ---------------------------------------------------------------------------

# Pattern for ``export function VoiceCloneModal(...)`` signature.
_VOICE_CLONE_MODAL_SIG = (
    r"export\s+function\s+VoiceCloneModal\s*\("
)


def test_g6_1_5_voice_clone_modal_body_has_no_cosyvoice_literals():
    """**G6.1.5 / G_MX.2 reverse guard (function-body scoped)**:

    Inside ``VoiceCloneModal`` function body (MiniMax legacy), the
    following CosyVoice literals are forbidden:

      - ``cosyvoice`` (lowercase substring)
      - ``CosyVoice`` (camelcase substring)
      - ``/api/voice/cosyvoice/``
      - ``CosyVoiceCloneModal`` / ``CosyVoiceConsentModal``
      - ``submitCosyvoiceClone`` / ``cosyvoiceCloneVoice``
      - ``cosyvoiceClone``

    Whole-file ban is **NOT** acceptable — E.1 wiring adds CosyVoice
    references at file top level (imports) and in the parent component
    (onClick dispatch). Only the legacy modal function body must stay
    clean.
    """
    src = _strip_comments(VOICE_SELECTION_PANEL.read_text(encoding="utf-8"))
    body = _extract_function_body(src, _VOICE_CLONE_MODAL_SIG)
    forbidden = [
        "cosyvoice",
        "CosyVoice",
        "/api/voice/cosyvoice/",
        "CosyVoiceCloneModal",
        "CosyVoiceConsentModal",
        "submitCosyvoiceClone",
        "cosyvoiceCloneVoice",
        "cosyvoiceClone",
    ]
    for lit in forbidden:
        assert lit not in body, (
            f"VoiceSelectionPanel.tsx::VoiceCloneModal function body "
            f"contains forbidden CosyVoice literal `{lit}`. The MiniMax "
            f"legacy modal function must stay clean — CosyVoice routing "
            f"belongs in the parent component's onClick dispatch."
        )


# ---------------------------------------------------------------------------
# G_MX.3 — VoiceModifyTab MiniMax clone region must stay clean (scoped)
# ---------------------------------------------------------------------------


def test_g_mx_3_voice_modify_tab_minimax_clone_region_clean():
    """**G_MX.3 reverse guard (JSX-node scoped)**:

    The MiniMax-clone JSX node in ``VoiceModifyTab.tsx`` —
    ``<VoiceCloneModal ... />`` — must not contain CosyVoice literals
    among its props. Scoping: from the anchor ``<VoiceCloneModal`` to
    the matching ``/>`` (or ``</VoiceCloneModal>``), so the new
    ``<CosyVoiceCloneModal ... />`` block right next to it in the same
    file is NOT scanned (E.1 explicitly places both nodes in this file).
    """
    src = _strip_comments(VOICE_MODIFY_TAB.read_text(encoding="utf-8"))
    anchor = "<VoiceCloneModal"
    start = src.find(anchor)
    assert start >= 0, "VoiceModifyTab.tsx is missing <VoiceCloneModal> JSX"
    # Search for the closing of THIS JSX element.
    # Element either self-closes with `/>` or has children + `</VoiceCloneModal>`.
    self_close = src.find("/>", start)
    open_close = src.find("</VoiceCloneModal>", start)
    candidates = [pos for pos in (self_close, open_close) if pos >= 0]
    assert candidates, "Couldn't find end of <VoiceCloneModal> element"
    end = min(candidates) + 2  # +2 to include the `/>` chars
    region = src[start:end]

    forbidden = [
        "cosyvoice",
        "CosyVoice",
        "CosyVoiceCloneModal",
        "CosyVoiceConsentModal",
        "submitCosyvoiceClone",
    ]
    for lit in forbidden:
        assert lit not in region, (
            f"VoiceModifyTab.tsx MiniMax <VoiceCloneModal>...</> JSX "
            f"region contains CosyVoice literal `{lit}`. The legacy "
            f"JSX node props must stay clean — CosyVoice has its own "
            f"separate <CosyVoiceCloneModal> JSX block elsewhere in "
            f"the file."
        )


# ---------------------------------------------------------------------------
# G_MX.1 — voiceSelection.ts::cloneVoiceForSelection endpoint unchanged
# ---------------------------------------------------------------------------

_CLONE_VOICE_FOR_SEL_SIG = (
    r"export\s+async\s+function\s+cloneVoiceForSelection\s*\("
)


def test_g_mx_1_clone_voice_for_selection_endpoint_unchanged():
    """**G_MX.1 MiniMax-unchanged guard**:

    ``voiceSelection.ts::cloneVoiceForSelection`` must still hit the
    legacy endpoint ``/jobs/${input.jobId}/voice-clone`` (template
    literal containing both ``/jobs/`` and ``voice-clone``). Refactoring
    to a CosyVoice URL would silently break MiniMax clone.
    """
    src = _strip_comments(VOICE_SELECTION_API.read_text(encoding="utf-8"))
    body = _extract_function_body(src, _CLONE_VOICE_FOR_SEL_SIG)
    # Must contain the legacy endpoint
    assert "/jobs/" in body and "voice-clone" in body, (
        "cloneVoiceForSelection no longer references `/jobs/` + "
        "`voice-clone` in its body. Backend MiniMax clone endpoint has "
        "not moved; refactoring this function URL is a regression."
    )
    # Must NOT contain CosyVoice endpoint
    assert "/api/voice/cosyvoice/" not in body, (
        "cloneVoiceForSelection body contains `/api/voice/cosyvoice/` — "
        "MiniMax path must not be re-routed to CosyVoice endpoint."
    )


# ---------------------------------------------------------------------------
# G6.5.3 — URL mutual exclusion between CosyVoice + MiniMax modules
# ---------------------------------------------------------------------------


def test_g6_5_3_cosyvoice_and_minimax_clone_urls_are_mutually_exclusive():
    """**G6.5.3 contract** (plan §6.5 line 605-616, adapted to Python static):

    Extract all ``fetch(...)`` URL literals from:
      - ``cosyvoiceClone.ts`` (entire file)
      - ``voiceSelection.ts::cloneVoiceForSelection`` function body

    Two URL sets must be **fully disjoint**:
      - CosyVoice URLs: all startswith ``/api/voice/cosyvoice/``
      - MiniMax URLs: all contain ``/jobs/`` and ``voice-clone``
      - Intersection must be empty

    This is the spec's "most important regression guard" — directly
    blocks the 2026-05-26 "button shows but dispatch goes wrong" incident
    shape.
    """
    cosy_code = _strip_comments(
        (FE_DIR / "lib" / "api" / "cosyvoiceClone.ts").read_text(encoding="utf-8")
    )
    cosy_urls = _http_call_urls_from_code(cosy_code)
    assert cosy_urls, "Expected at least one fetch() URL in cosyvoiceClone.ts"

    for url in cosy_urls:
        assert url.startswith("/api/voice/cosyvoice/"), (
            f"cosyvoiceClone.ts fetch URL `{url}` is not under "
            f"`/api/voice/cosyvoice/...` namespace."
        )
        assert "/jobs/" not in url, (
            f"cosyvoiceClone.ts fetch URL `{url}` contains forbidden "
            f"MiniMax legacy substring `/jobs/`."
        )
        assert "voice-clone" not in url, (
            f"cosyvoiceClone.ts fetch URL `{url}` contains forbidden "
            f"MiniMax legacy substring `voice-clone`."
        )

    vs_src = _strip_comments(VOICE_SELECTION_API.read_text(encoding="utf-8"))
    vs_body = _extract_function_body(vs_src, _CLONE_VOICE_FOR_SEL_SIG)
    mx_urls = _http_call_urls_from_code(vs_body)
    assert mx_urls, (
        "Expected at least one fetch() URL in cloneVoiceForSelection body"
    )

    for url in mx_urls:
        assert "/jobs/" in url and "voice-clone" in url, (
            f"cloneVoiceForSelection fetch URL `{url}` doesn't match "
            f"the MiniMax legacy `/jobs/.../voice-clone` shape."
        )
        assert "/api/voice/cosyvoice/" not in url, (
            f"cloneVoiceForSelection fetch URL `{url}` contains the "
            f"forbidden CosyVoice substring `/api/voice/cosyvoice/`."
        )

    # Two URL sets must be disjoint (defensive — covered above already,
    # but make the contract explicit and testable as one assertion).
    assert cosy_urls.isdisjoint(mx_urls), (
        f"CosyVoice URL set and MiniMax URL set share an element:\n"
        f"  CosyVoice: {sorted(cosy_urls)}\n"
        f"  MiniMax:   {sorted(mx_urls)}\n"
        f"  shared:    {sorted(cosy_urls & mx_urls)}"
    )


# ---------------------------------------------------------------------------
# Sanity: button onClick dispatch is actually present in E.1 changes
# ---------------------------------------------------------------------------


def test_e1_voice_selection_panel_button_dispatches_by_provider():
    """**E.1 sanity guard**: VoiceSelectionPanel button onClick must
    contain the provider-aware dispatch pattern. Without this, the
    button always opens the MiniMax modal regardless of provider.
    """
    src = _strip_comments(VOICE_SELECTION_PANEL.read_text(encoding="utf-8"))
    # The dispatch pattern: `if (currentProvider === 'cosyvoice') ... setCosyvoiceCloneModalSpeaker`
    assert re.search(
        r"if\s*\(\s*currentProvider\s*===\s*['\"]cosyvoice['\"]\s*\)[\s\S]{0,200}setCosyvoiceCloneModalSpeaker",
        src,
    ), (
        "VoiceSelectionPanel.tsx button onClick is missing the "
        "`if (currentProvider === 'cosyvoice') setCosyvoiceCloneModalSpeaker(...)` "
        "dispatch pattern. Without it, CosyVoice button still triggers "
        "the MiniMax legacy modal."
    )
    # CosyVoiceCloneModal must be rendered in the JSX tree
    assert "<CosyVoiceCloneModal" in src, (
        "VoiceSelectionPanel.tsx is missing <CosyVoiceCloneModal> JSX "
        "render — clicking the button would set state but no modal "
        "would appear."
    )


def test_e1_voice_modify_tab_button_dispatches_by_provider():
    """**E.1 sanity guard**: same dispatch check for VoiceModifyTab."""
    src = _strip_comments(VOICE_MODIFY_TAB.read_text(encoding="utf-8"))
    assert re.search(
        r"if\s*\(\s*currentProvider\s*===\s*['\"]cosyvoice['\"]\s*\)[\s\S]{0,200}setCosyvoiceCloneModalSpeaker",
        src,
    ), (
        "VoiceModifyTab.tsx button onClick is missing the provider-aware "
        "dispatch. CosyVoice clone button would route to MiniMax modal."
    )
    assert "<CosyVoiceCloneModal" in src, (
        "VoiceModifyTab.tsx is missing <CosyVoiceCloneModal> JSX render."
    )


def test_e1_clone_gate_fetched_in_both_components():
    """**E.1 sanity guard**: both components must fetch clone-gate to AND
    with `supportsClone`. Missing fetch → CosyVoice button forever
    hidden (no policy data) or forever shown (treated as granted).
    """
    for path in (VOICE_SELECTION_PANEL, VOICE_MODIFY_TAB):
        src = path.read_text(encoding="utf-8")
        assert "getCosyvoiceCloneGate" in src, (
            f"{path.name} doesn't import/call getCosyvoiceCloneGate — "
            f"button visibility AND with policy gate won't work."
        )
        assert "cosyvoiceCloneGate" in src, (
            f"{path.name} doesn't have a cosyvoiceCloneGate state — "
            f"can_access_clone won't be consulted."
        )

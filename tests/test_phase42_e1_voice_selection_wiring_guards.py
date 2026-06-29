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
    FE_DIR / "app" / "[locale]" / "(app)" / "workspace" / "[jobId]" / "edit" / "VoiceModifyTab.tsx"
)
# VoiceCloneModal (MiniMax legacy modal) was extracted out of
# VoiceSelectionPanel.tsx into its own component file. The body-scoped
# G6.1.5 reverse guard below reads the modal from its current home.
VOICE_CLONE_MODAL = (
    FE_DIR / "components" / "workspace" / "VoiceCloneModal.tsx"
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
    src = _strip_comments(VOICE_CLONE_MODAL.read_text(encoding="utf-8"))
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
            f"VoiceCloneModal.tsx::VoiceCloneModal function body "
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


# ---------------------------------------------------------------------------
# G_E1_P2 — frontend gate must AND runtime_ready (Codex 2026-05-27 PR #15 P2)
# ---------------------------------------------------------------------------


def test_e1_p2_frontend_gate_ands_runtime_ready_via_can_show_clone_button():
    """**E.1 P2 fix guard (Codex 2026-05-27)**: both entry points must read
    ``can_show_clone_button`` (joint policy + runtime field) — NOT just
    ``can_access_clone`` (policy alone).

    Why: ``can_access_clone`` = "admin / allowlist / GA grants visibility"
    but does NOT mean the backend is configured to serve a clone. The
    backend Layers 2-3 (``cosyvoice_clone_worker_enabled`` +
    ``cosyvoice_sample_uploader`` is production-ready + config complete)
    are separate. Frontend must consult both before showing the button.

    The D.1 endpoint was extended in PR #15 to compute
    ``can_show_clone_button = can_access_clone && runtime_ready`` server-
    side; frontend referencing it directly stays in lockstep.

    Detection: in both VoiceSelectionPanel + VoiceModifyTab the
    cosyvoice-branch of ``canSpeakerClone`` must reference
    ``can_show_clone_button``, NOT ``can_access_clone`` alone.
    """
    for path in (VOICE_SELECTION_PANEL, VOICE_MODIFY_TAB):
        src = _strip_comments(path.read_text(encoding="utf-8"))
        # The cosyvoice branch must read `can_show_clone_button` from the gate.
        assert "can_show_clone_button" in src, (
            f"{path.name} must reference `can_show_clone_button` (joint "
            f"policy + runtime field added in D.1 PR #15 P2 fix). Using "
            f"`can_access_clone` alone lets admin/allowlist users see a "
            f"button that 503s on submit."
        )

        # Defensive: the cosyvoice branch in canSpeakerClone should NOT
        # short-circuit on `can_access_clone === true` (policy alone).
        # Locate the cosyvoice branch via the `provider === 'cosyvoice'`
        # or `provider === "cosyvoice"` literal + scan the next ~300 chars
        # for the comparison. Be tolerant of formatting.
        for pat in (
            r"provider\s*===\s*['\"]cosyvoice['\"]",
        ):
            for m in re.finditer(pat, src):
                window = src[m.start(): m.start() + 400]
                # If this window contains a `can_access_clone === true`
                # check, it must ALSO contain `can_show_clone_button` —
                # otherwise the gate skips the runtime layer.
                if (
                    "can_access_clone === true" in window
                    and "can_show_clone_button" not in window
                ):
                    raise AssertionError(
                        f"{path.name} cosyvoice branch of canSpeakerClone "
                        f"reads only `can_access_clone === true` without "
                        f"ANDing `can_show_clone_button`. Window:\n"
                        f"{window}"
                    )


# ---------------------------------------------------------------------------
# G_E1_P2_OPTGROUP — CosyVoice personal-clone optgroup must exist + filter
#                    correctly (Codex 2026-05-27 PR #15 P2² review)
# ---------------------------------------------------------------------------


def test_e1_p2_cosyvoice_personal_optgroup_present_in_both_selectors():
    """**Codex 2026-05-27 P2² fix guard**: after a successful CosyVoice
    clone, the new voice id is stored in ``personalVoices`` but the prior
    code only rendered MiniMax personal optgroups. The <select> ends up
    with a value (the new voice id) but no matching <option> — user sees
    an empty dropdown after closing the modal and can't re-select.

    Lock-in: both VoiceSelectionPanel and VoiceModifyTab must render a
    CosyVoice personal-clone optgroup. Detected via:
      1. An optgroup with the "我的 CosyVoice 克隆音色" label
      2. The gate condition references `currentProvider === 'cosyvoice'`
      3. The filter accepts the canonical backend value
         `provider === 'cosyvoice_voice_clone'` OR the lowercased
         `ttsProvider === 'cosyvoice'` flavour (forward compat).
    """
    for path in (VOICE_SELECTION_PANEL, VOICE_MODIFY_TAB):
        src = _strip_comments(path.read_text(encoding="utf-8"))

        # 1. Label must appear in JSX
        assert "我的 CosyVoice 克隆音色" in src, (
            f"{path.name} is missing the `我的 CosyVoice 克隆音色` optgroup "
            f"label. Without it the new voice id from a successful clone "
            f"won't surface in the dropdown."
        )

        # 2. The optgroup must be inside an IIFE gated on cosyvoice provider
        # Search for the optgroup label region, then check the 600 chars
        # before it for the provider gate.
        idx = src.find("我的 CosyVoice 克隆音色")
        window = src[max(0, idx - 1200): idx]
        assert re.search(
            r"currentProvider\s*!==\s*['\"]cosyvoice['\"]\s*\)\s*return\s+null",
            window,
        ), (
            f"{path.name} CosyVoice optgroup is not gated by "
            f"`currentProvider !== 'cosyvoice'` early-return. Without "
            f"this guard the optgroup leaks into MiniMax / VolcEngine "
            f"selectors."
        )

        # 3. Filter must accept BOTH provider shapes (forward compat)
        assert "cosyvoice_voice_clone" in window, (
            f"{path.name} CosyVoice optgroup filter must check "
            f"`provider === 'cosyvoice_voice_clone'` (canonical backend "
            f"value gateway writes on clone success)."
        )
        assert re.search(
            r"ttsProvider\s*===\s*['\"]cosyvoice['\"]",
            window,
        ), (
            f"{path.name} CosyVoice optgroup filter must also accept "
            f"`ttsProvider === 'cosyvoice'` for backward / forward "
            f"compatibility with row variations."
        )


def test_e1_p2_minimax_optgroups_unchanged_by_cosyvoice_addition():
    """**Reverse guard (Codex Codex P2²)**: adding the CosyVoice
    optgroup must not pollute the MiniMax personal-voice optgroups.
    The existing three "个人音色 · 强匹配" / "个人音色 · 可能匹配" /
    "其他个人音色" labels still need to be MiniMax-gated.
    """
    for path in (VOICE_SELECTION_PANEL, VOICE_MODIFY_TAB):
        src = _strip_comments(path.read_text(encoding="utf-8"))
        # "其他个人音色" must still be gated on MiniMax.
        idx = src.find("其他个人音色")
        assert idx >= 0, f"{path.name} missing legacy MiniMax `其他个人音色` optgroup"
        window = src[max(0, idx - 1200): idx]
        assert re.search(
            r"currentProvider\s*!==\s*['\"]minimax['\"]\s*\)\s*return\s+null",
            window,
        ), (
            f"{path.name} legacy `其他个人音色` optgroup is no longer "
            f"gated by `currentProvider !== 'minimax'`. The MiniMax "
            f"optgroups must remain MiniMax-only after E.1 P2² fix."
        )


# ---------------------------------------------------------------------------
# G_E1_P1²_APPROVAL — approval validator accepts CosyVoice clone voice_id
#                    when speaker carries requires_worker=true
#                    (Codex 2026-05-27 P1 二轮)
# ---------------------------------------------------------------------------


def test_e1_p1_approval_validator_accepts_cosyvoice_clone_when_requires_worker():
    """**Codex 2026-05-27 PR #15 P1 二轮 fix #1**:
    ``_validate_voice_provider_compat`` must accept CosyVoice voice_id
    that doesn't match the builtin catalog when the speaker carries
    ``requires_worker=True`` (gateway-side enriched signal that the
    voice was DB-resolved + ownership-checked).

    Without this, ``approve_voice_selection`` rejects clone voice_ids
    and E.1's file-upload flow can't complete the approval.
    """
    from pathlib import Path as _P
    import sys as _sys

    repo = _P(__file__).resolve().parents[1]
    src = repo / "src"
    if str(src) not in _sys.path:
        _sys.path.insert(0, str(src))

    from services.jobs.review_actions import _validate_voice_provider_compat

    clone_voice = "voice-cosy-v3-flash-deadbeef-1234-5678-uuid"

    # Without requires_worker → must raise (legacy behavior preserved)
    try:
        _validate_voice_provider_compat(
            clone_voice, "cosyvoice", requires_worker=False
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "validator should reject CosyVoice clone voice_id without "
            "requires_worker=True (forward compat)"
        )

    # With requires_worker=True → must accept (clone path)
    _validate_voice_provider_compat(
        clone_voice, "cosyvoice", requires_worker=True
    )

    # Sanity: VolcEngine path still strict
    try:
        _validate_voice_provider_compat(
            clone_voice, "volcengine", requires_worker=True
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "validator should still reject non-volcengine voice_id "
            "with tts_provider=volcengine — requires_worker only affects "
            "CosyVoice clone path."
        )


def test_e1_p1_approval_persists_requires_worker_in_merged_payload():
    """**Codex 2026-05-27 PR #15 P1 二轮**: source-level guard that the
    approval merge loop propagates ``requires_worker`` /
    ``worker_target_model`` from the incoming gateway-enriched speaker
    into the merged_speakers payload (so downstream pipeline +
    voice_map.json see them).

    Static text scan — full behavioral integration test would need
    project_dir fixture + filesystem; this is the cheap structural lock.
    """
    src_path = (
        REPO_ROOT / "src" / "services" / "jobs" / "review_actions.py"
    )
    src = src_path.read_text(encoding="utf-8")
    # The merge loop must touch requires_worker on the merged base.
    assert 'base["requires_worker"]' in src, (
        "approve_voice_selection merge loop missing "
        "`base[\"requires_worker\"] = ...` propagation — downstream "
        "pipeline / voice_map won't know the speaker needs the worker."
    )
    assert 'base["worker_target_model"]' in src, (
        "approve_voice_selection merge loop missing "
        "`base[\"worker_target_model\"] = ...` propagation."
    )
    # And it must CLEAR stale flags when the new sp is not a clone, so
    # re-approving with a builtin voice doesn't leak previous routing.
    assert 'base.pop("requires_worker"' in src, (
        "merge loop must pop requires_worker when the incoming speaker "
        "doesn't carry the flag (re-approve with builtin voice should "
        "clear previous clone routing)."
    )


# ---------------------------------------------------------------------------
# G_E1_P1²_VOICE_MAP — editing voice_map persists CosyVoice clone routing
#                    (Codex 2026-05-27 P1 二轮 #2)
# ---------------------------------------------------------------------------


def test_e1_p1_set_voice_override_accepts_requires_worker_kwarg():
    """**Codex 2026-05-27 PR #15 P1 二轮 fix #2**: edit-flow voice_map
    persistence must accept ``requires_worker`` + ``worker_target_model``.

    Inspects the function signature via inspect — caller has to be able
    to pass these without TypeError, otherwise re-TTS in Studio edit
    won't route to the worker and the clone voice silently doesn't
    take effect.
    """
    import inspect as _inspect

    from services.jobs.editing_voice_map import set_voice_override

    sig = _inspect.signature(set_voice_override)
    assert "requires_worker" in sig.parameters, (
        "set_voice_override missing `requires_worker` kwarg — editing "
        "flow can't persist CosyVoice clone worker routing."
    )
    assert "worker_target_model" in sig.parameters, (
        "set_voice_override missing `worker_target_model` kwarg."
    )


def test_e1_p1_set_voice_override_writes_routing_into_voice_map(tmp_path):
    """**Codex 2026-05-27 PR #15 P1 二轮**: behavioral test —
    set_voice_override with ``requires_worker=True`` persists both flags
    into the voice_map.json entry. Re-TTS readers (services.jobs.copy_service,
    pipeline editor commit) check `entry.get("requires_worker")`."""
    import json as _json
    from services.jobs.editing_voice_map import (
        set_voice_override,
        load_voice_map,
    )
    from services.jobs.editing import EDITING_SUBDIR

    project = tmp_path / "proj"
    editing_dir = project / EDITING_SUBDIR
    editing_dir.mkdir(parents=True)
    # set_voice_override side-effects also call mark_segment_status which
    # requires segment_status.json — pre-seed empty.
    (editing_dir / "segment_status.json").write_text("{}", encoding="utf-8")
    # Pre-seed an empty voice_map.json so load doesn't crash.
    (editing_dir / "voice_map.json").write_text("{}", encoding="utf-8")

    result = set_voice_override(
        project,
        "segment_001",
        provider="cosyvoice",
        voice_id="voice-cosy-v3-flash-uuid-test",
        requires_worker=True,
        worker_target_model="cosyvoice-v3.5-flash",
    )
    assert result["requires_worker"] is True
    assert result["worker_target_model"] == "cosyvoice-v3.5-flash"

    persisted = load_voice_map(project)
    entry = persisted.get("segment_001")
    assert entry is not None, "voice_map.json did not get segment entry"
    assert entry.get("requires_worker") is True, (
        "voice_map.json entry missing requires_worker=True — re-TTS "
        "path will fall back to legacy CosyVoice and clone voice "
        "silently won't take effect."
    )
    assert entry.get("worker_target_model") == "cosyvoice-v3.5-flash"


def test_e1_p1_set_voice_override_omits_routing_when_not_clone(tmp_path):
    """**Codex 2026-05-27 PR #15 P1 二轮**: when caller does NOT pass
    requires_worker (or passes False / None), the persisted entry must
    NOT contain these fields. MiniMax / builtin CosyVoice / VolcEngine
    paths stay unchanged.
    """
    from services.jobs.editing_voice_map import (
        set_voice_override,
        load_voice_map,
    )
    from services.jobs.editing import EDITING_SUBDIR

    project = tmp_path / "proj_nonclone"
    editing_dir = project / EDITING_SUBDIR
    editing_dir.mkdir(parents=True)
    (editing_dir / "segment_status.json").write_text("{}", encoding="utf-8")
    (editing_dir / "voice_map.json").write_text("{}", encoding="utf-8")

    # MiniMax legacy path — no requires_worker kwarg
    set_voice_override(
        project,
        "segment_002",
        provider="minimax",
        voice_id="Chinese (Mandarin)_Wise_Woman",
    )
    entry = load_voice_map(project).get("segment_002") or {}
    assert "requires_worker" not in entry, (
        "voice_map.json entry should NOT carry requires_worker for "
        "MiniMax voice — only CosyVoice clones need worker routing."
    )
    assert "worker_target_model" not in entry

    # Explicit False also stays absent
    set_voice_override(
        project,
        "segment_003",
        provider="cosyvoice",
        voice_id="cosyvoice-v3.5-flash-zh-female-builtin01",
        requires_worker=False,
    )
    entry = load_voice_map(project).get("segment_003") or {}
    assert "requires_worker" not in entry, (
        "voice_map.json entry should NOT carry requires_worker=False — "
        "absence is the canonical 'not a clone' encoding."
    )

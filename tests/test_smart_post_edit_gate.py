"""Smart-mode post-edit entry gate (2026-05-16).

== Background ==

Master plan §6.2 says: "Smart 成功交付后应允许进入 Studio post-edit
二次精修. 产品路径是 '先自动交付, 必要时再人工精修'." The backend
was wired for this at Smart MVP P2 (``EDITABLE_SERVICE_MODES =
{"studio", "smart"}`` in src/services/smart/state.py, and
``enter_editing`` in src/services/jobs/editing.py:128 explicitly
accepts smart jobs with ``smart_state.status in {completed,
downgraded_to_studio}``). But the frontend left the "进入修改" button
gated to ``serviceMode === "studio"`` only, so smart users could
never reach the path. Opened on 2026-05-16 per the user request:

  "智能版视频完成后, 不能像工作台版那样修改吗? 加上修改的功能"

== This test ==

Pins the contract from both sides:

1. Backend constant ``EDITABLE_SERVICE_MODES`` MUST contain "smart" —
   removing it would silently strip the path even if the frontend
   keeps surfacing the button.
2. Frontend ``projects/page.tsx`` MUST use a helper that includes
   "smart" — checks via AST/text grep so any regression to a literal
   ``serviceMode === "studio"`` only gate is caught.

The two halves together = end-to-end guarantee.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class TestBackendSmartEditableContract:
    """Pin EDITABLE_SERVICE_MODES so the backend never silently drops smart."""

    def test_editable_service_modes_includes_smart(self):
        from services.smart.state import EDITABLE_SERVICE_MODES

        assert "smart" in EDITABLE_SERVICE_MODES, (
            "Smart MVP P2 designed for smart-mode post-edit re-entry per "
            "master plan §6.2; removing 'smart' from EDITABLE_SERVICE_MODES "
            "breaks the editing/jianying-draft entry points for every "
            "smart job. If you are intentionally disabling smart post-edit, "
            "do it with an explicit kill-switch flag, not by mutating this "
            "frozen set."
        )

    def test_is_editable_smart_state_accepts_completed_and_downgraded(self):
        from services.smart.state import is_editable_smart_state

        assert is_editable_smart_state({"status": "completed"})
        assert is_editable_smart_state({"status": "downgraded_to_studio"})
        # In-flight smart states MUST NOT enter editing
        for status in (
            "running",
            "clone_blocked_waiting_retry",
            "fail_and_refunded",
            None,
            "",
            "unknown",
        ):
            assert not is_editable_smart_state({"status": status}), (
                f"Smart status {status!r} should NOT permit entering editing"
            )
        # Wrong shape → fail-closed
        assert not is_editable_smart_state(None)
        assert not is_editable_smart_state("completed")  # not a Mapping


class TestFrontendSmartEditButtonGate:
    """Pin the frontend projects page never regresses to studio-only."""

    _PAGE = (
        _REPO
        / "frontend-next"
        / "src"
        / "app"
        / "(app)"
        / "projects"
        / "page.tsx"
    )

    def _source(self) -> str:
        return self._PAGE.read_text(encoding="utf-8")

    def test_editable_service_modes_constant_includes_smart(self):
        """The frontend's mirror of the backend set must include 'smart'.
        Frontend-side filtering happens before any backend call, so a
        missing entry here silently hides the button regardless of
        backend support."""
        source = self._source()
        # Anchor on the constant ASSIGNMENT (skip docstring/comment mentions).
        match = re.search(
            r"const\s+EDITABLE_SERVICE_MODES[^=]*=\s*new\s+Set\(\[([^\]]+)\]\)",
            source,
        )
        assert match is not None, (
            f"Expected `const EDITABLE_SERVICE_MODES = new Set([...])` in "
            f"{self._PAGE}. If you changed the constant shape, update this "
            f"test."
        )
        members = match.group(1)
        assert '"studio"' in members and '"smart"' in members, (
            "EDITABLE_SERVICE_MODES on the frontend must contain both "
            "'studio' and 'smart' to match the backend contract. Members "
            f"section:\n{members}"
        )

    def test_no_studio_only_literal_in_edit_eligibility_helper(self):
        """The eligibility helper must NOT contain a literal
        ``serviceMode === "studio"`` check — that pattern was the original
        bug. Use the set-based check instead so adding new editable modes
        in the future requires a single source change."""
        source = self._source()
        # The original bug pattern (now removed); regression would
        # re-introduce a literal serviceMode === "studio" inside the
        # eligibility check section. Allow the string elsewhere (e.g. tab
        # labels, plan-card highlight styling), but flag it inside the
        # 60-line window after the helper definition.
        helper_idx = source.find("function isJobEditEligible")
        assert helper_idx >= 0, (
            "Expected helper isJobEditEligible() in projects/page.tsx; if "
            "the helper was renamed/moved, update this test."
        )
        # Look at the helper body + the next ~50 lines (covers both
        # collapsed-row + expanded-row consumers).
        body = source[helper_idx : helper_idx + 2000]
        forbidden = re.search(
            r'serviceMode\s*===\s*["\']studio["\']\s*&&\s*job\.status\s*===\s*["\']succeeded["\']',
            body,
        )
        assert forbidden is None, (
            "Found regression: the studio-only eligibility check pattern "
            "is back in the edit-eligibility helper. Smart jobs would be "
            "silently hidden from the 修改 button again.\n"
            f"Match:\n{forbidden.group(0) if forbidden else ''}\n"
            f"Source window:\n{body[:1200]}"
        )

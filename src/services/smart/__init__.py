"""Smart MVP P2 — top-level package.

Skeleton per docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §6.0.
F1-F4 first-pass implementation: state marker channel, handoff helpers,
effective pipeline mode derivation. Auto-decision modules (auto_voice_review /
auto_translation_review / retry_budget) and protocol wiring (smart_wiring.py
sibling module — kept OUTSIDE this package per §6.0 / §8.2 #1) are not in
this skeleton; they will land in subsequent PRs once the state-machine plumbing
is exercised end-to-end.

Public surface:
- state.emit_smart_state_marker(state) — pipeline emits stdout marker
- state.parse_smart_state_marker(line) — process_runner parses
- state.derive_effective_pipeline_mode(record) — handoff-aware mode picker
- state.SMART_STATE_MARKER_PREFIX — runner-side recognition constant
- handoff.emit_handoff_markers(...) — write set_stage + smart_state +
  web_review marker triple in one call
"""
from services.smart.handoff import emit_handoff_markers
from services.smart.state import (
    SMART_STATE_MARKER_PREFIX,
    derive_effective_pipeline_mode,
    emit_smart_state_marker,
    parse_smart_state_marker,
)

__all__ = [
    "SMART_STATE_MARKER_PREFIX",
    "derive_effective_pipeline_mode",
    "emit_handoff_markers",
    "emit_smart_state_marker",
    "parse_smart_state_marker",
]

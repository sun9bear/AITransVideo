"""P1-15a (audit 2026-05-07) regression: S2 Pass 1/2/3 fallback chains
must cap retry attempts at _MAX_FALLBACK_ATTEMPTS_PER_PASS to prevent
runaway paid LLM spend on hard inputs.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        B-HIGH-3 — fallback chains had no max_fallback_attempts cap;
                   a single malformed input cycled through all 4-6
                   candidates, each a paid call.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_transcript_reviewer_exposes_max_fallback_attempts_constant():
    from services.transcript_reviewer import _MAX_FALLBACK_ATTEMPTS_PER_PASS
    assert isinstance(_MAX_FALLBACK_ATTEMPTS_PER_PASS, int)
    assert 1 <= _MAX_FALLBACK_ATTEMPTS_PER_PASS <= 3, (
        f"P1-15a regression: cap is {_MAX_FALLBACK_ATTEMPTS_PER_PASS} — "
        f"audit recommends 2, certainly not >3 (cost guard) and not 0 "
        f"(would disable fallback entirely)"
    )


def test_pass_fallback_loops_reference_the_cap_constant():
    """AST guard: at least 4 occurrences of _MAX_FALLBACK_ATTEMPTS_PER_PASS
    in transcript_reviewer.py — once for definition, plus once per Pass
    that uses it (Pass 1/2/3)."""
    src_path = _REPO_ROOT / "src" / "services" / "transcript_reviewer.py"
    src = src_path.read_text(encoding="utf-8")
    occurrences = src.count("_MAX_FALLBACK_ATTEMPTS_PER_PASS")
    assert occurrences >= 4, (  # 1 def + 3 uses
        f"P1-15a regression: _MAX_FALLBACK_ATTEMPTS_PER_PASS appears "
        f"{occurrences} time(s) in transcript_reviewer.py; expected "
        f">=4 (1 module-level definition + 3 Pass uses)"
    )


def test_fallback_chains_have_explicit_break_or_slicing():
    """Either an explicit `if attempt_idx >= _MAX_FALLBACK_ATTEMPTS_PER_PASS: break`
    or `chain[:_MAX_FALLBACK_ATTEMPTS_PER_PASS]` slicing must appear in
    the source — no implicit reliance on the chain length."""
    src_path = _REPO_ROOT / "src" / "services" / "transcript_reviewer.py"
    src = src_path.read_text(encoding="utf-8")
    # Either pattern is acceptable.
    has_explicit_break = ">= _MAX_FALLBACK_ATTEMPTS_PER_PASS" in src
    has_slicing = "[:_MAX_FALLBACK_ATTEMPTS_PER_PASS]" in src
    assert has_explicit_break or has_slicing, (
        "P1-15a regression: no explicit cap mechanism (break or slicing) "
        "found in transcript_reviewer.py — the constant exists but isn't "
        "actually limiting the fallback loops"
    )

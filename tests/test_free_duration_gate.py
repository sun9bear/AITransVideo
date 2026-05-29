"""Phase 2a Task 7 (gate #7) — free-tier duration cap (FAIL-CLOSED).

Unit-tests the pure ``evaluate_free_duration_cap`` decision + static guards that
``process._check_duration_limit``'s free branch and its run() call site are wired
correctly (process.py is too heavy to import as a unit; the repo guards it with
source scans, see test_phase1_guards / test_free_voiceclone_wiring).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from utils.free_duration_gate import (  # noqa: E402
    FREE_DURATION_CAP_MINUTES,
    REJECT_OVER_CAP,
    REJECT_UNTRUSTED,
    evaluate_free_duration_cap,
)

_MIN = 60_000  # ms per minute


# --- pure decision: over / under / boundary ---

def test_over_cap_rejected():
    assert evaluate_free_duration_cap(11 * _MIN) == REJECT_OVER_CAP


def test_under_cap_ok():
    assert evaluate_free_duration_cap(5 * _MIN) is None


def test_exactly_cap_ok():
    # strict > : exactly 10:00 is allowed
    assert evaluate_free_duration_cap(FREE_DURATION_CAP_MINUTES * _MIN) is None


def test_just_over_cap_rejected():
    assert evaluate_free_duration_cap(FREE_DURATION_CAP_MINUTES * _MIN + 1) == REJECT_OVER_CAP


# --- FAIL-CLOSED: untrusted / unknown duration ---

def test_zero_duration_fail_closed():
    assert evaluate_free_duration_cap(0) == REJECT_UNTRUSTED


def test_negative_duration_fail_closed():
    assert evaluate_free_duration_cap(-1) == REJECT_UNTRUSTED


def test_none_duration_fail_closed():
    assert evaluate_free_duration_cap(None) == REJECT_UNTRUSTED


def test_non_numeric_duration_fail_closed():
    assert evaluate_free_duration_cap("not-a-number") == REJECT_UNTRUSTED


def test_custom_cap_respected():
    assert evaluate_free_duration_cap(12 * _MIN, max_minutes=15) is None
    assert evaluate_free_duration_cap(16 * _MIN, max_minutes=15) == REJECT_OVER_CAP


# --- static wiring guards on process.py ---

def _process_src() -> str:
    return (REPO_ROOT / "src" / "pipeline" / "process.py").read_text(encoding="utf-8")


def test_check_duration_limit_has_free_fail_closed_branch():
    src = _process_src()
    start = src.index("def _check_duration_limit(")
    end = src.index("\ndef ", start + 1)
    fn = src[start:end]
    assert "service_mode_snapshot" in fn, "free branch must key on service_mode_snapshot"
    assert 'service_mode_snapshot == "free"' in fn
    assert "evaluate_free_duration_cap" in fn, "free branch must use the shared gate helper"
    # both rejection reasons must be handled (untrusted/over-cap) -> the free
    # branch raises on each (the legacy plan branch references neither constant).
    assert "REJECT_UNTRUSTED" in fn and "REJECT_OVER_CAP" in fn
    assert fn.count("raise RuntimeError") >= 2, "free branch must raise on both reject reasons"


def test_run_wires_duration_gate_with_service_mode_before_asr():
    src = _process_src()
    run_start = src.index("def run(self, config: ProcessConfig)")
    next_method = src.index("\n    def ", run_start + 1)
    body = src[run_start:next_method]
    assert "service_mode_snapshot=job_service_mode" in body, \
        "run() must pass service_mode_snapshot to _check_duration_limit"
    i_gate = body.index("_check_duration_limit(")
    i_asr = body.index(".transcribe(")
    assert i_gate < i_asr, "the duration gate must run BEFORE ASR transcription (cost gate)"

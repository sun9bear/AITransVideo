"""P1-11b (audit 2026-05-07) regression: payment webhook event
processing must be idempotent under concurrent delivery (provider
retries / race conditions).

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-HIGH-7 — _process_payment_event used SELECT-then-INSERT
                   pattern; concurrent provider callbacks could both
                   pass the SELECT, both add row, fall back to
                   IntegrityError handling instead of clean dedup.
"""
from __future__ import annotations

import sys
import inspect
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


def test_billing_uses_pg_insert_on_conflict():
    """AST guard: gateway/billing.py imports pg_insert from
    sqlalchemy.dialects.postgresql AND _process_payment_event references
    on_conflict_do_nothing — replaces the SELECT-then-INSERT race window
    with a single atomic statement."""
    src_path = _REPO_ROOT / "gateway" / "billing.py"
    src = src_path.read_text(encoding="utf-8")
    assert (
        "from sqlalchemy.dialects.postgresql import insert" in src
        or "sqlalchemy.dialects.postgresql import insert as pg_insert" in src
    ), (
        "P1-11b regression: billing.py no longer imports pg_insert; "
        "the atomic ON CONFLICT replacement is gone."
    )
    assert "on_conflict_do_nothing" in src, (
        "P1-11b regression: billing.py no longer calls "
        "on_conflict_do_nothing; reverted to SELECT-then-INSERT race."
    )


def test_process_payment_event_uses_returning():
    """AST guard: the INSERT call returns id, so caller can detect
    duplicate via None scalar_one_or_none()."""
    from billing import _process_payment_event
    src = inspect.getsource(_process_payment_event)
    assert ".returning(" in src, (
        "P1-11b regression: _process_payment_event no longer uses "
        ".returning(); cannot atomically distinguish 'inserted' from "
        "'duplicate' without a second SELECT round-trip."
    )

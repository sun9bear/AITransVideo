"""APF P0 (AD-6): the R2 artifact sweeper must NOT eager-push anonymous
preview jobs to R2.

Anonymous preview jobs run with ``service_mode="free"`` (so settlement /
gating / watermark contracts stay intact) plus the cross-cutting
``is_anonymous_preview=True`` column. The sweeper keys its eager-push set off
``service_mode`` alone, so without an explicit guard a ``free`` anonymous job
would have its stream-only teaser pushed to R2 — outliving the TTL-delete
promise. ``_classify_candidate`` short-circuits on ``is_anonymous_preview``.

These tests call the pure ``_classify_candidate`` directly — no live R2 / PG.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "gateway"), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub ``database`` so the module-level ``from database import async_session``
# in r2_artifact_sweeper.py doesn't read real env / build an engine.
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

import r2_artifact_sweeper as sweeper  # noqa: E402


def _json_rec(jianying: str | None = None):
    return SimpleNamespace(job_id="job_x", jianying_draft_zip_path=jianying)


def _db_job(*, is_anonymous_preview: bool, r2_artifacts=None, edit_generation=0):
    return SimpleNamespace(
        is_anonymous_preview=is_anonymous_preview,
        r2_artifacts=r2_artifacts,
        edit_generation=edit_generation,
    )


def test_anonymous_free_job_is_not_classified_for_r2_push():
    """service_mode is irrelevant here — an anonymous job (never published,
    r2_artifacts=None) would normally classify as a full push; the
    is_anonymous_preview guard must short-circuit it to (False, None)."""
    should, push_keys = sweeper._classify_candidate(
        _json_rec(), _db_job(is_anonymous_preview=True, r2_artifacts=None)
    )
    assert should is False
    assert push_keys is None


def test_anonymous_guard_overrides_jianying_delta_trigger():
    """Even the secondary jianying-delta trigger must not fire for anonymous
    jobs (anonymous previews never produce editor drafts, but the guard is
    defense-in-depth)."""
    should, _ = sweeper._classify_candidate(
        _json_rec(jianying="/p/draft.zip"),
        _db_job(is_anonymous_preview=True, r2_artifacts=[{"k": "v"}]),
    )
    assert should is False


def test_non_anonymous_free_job_still_classifies_for_push():
    """Regression: a normal free-tier job (is_anonymous_preview=False, never
    published) must STILL be picked up for a full push — the guard only
    isolates anonymous jobs, never the free tier at large."""
    should, push_keys = sweeper._classify_candidate(
        _json_rec(), _db_job(is_anonymous_preview=False, r2_artifacts=None)
    )
    assert should is True
    assert push_keys is None

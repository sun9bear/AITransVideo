"""r2_parity_ok contract tests (Stage B parity gate).

Plan: docs/plans/2026-05-07-disk-relief-via-r2-publisher-and-ttl.md §5.1

Coverage:
- Every expected key has a recognised, non-failed entry + R2 HEAD OK → True
- Missing entry (eager_push set has more than registry) → False
- Entry state="failed" → False
- Entry state="skipped_missing" + others OK → True (skipped doesn't HEAD)
- R2 HEAD miss for an entry claiming "pushed" → False (registry-vs-R2 drift)
- R2 HEAD raises → False (defensive — refuse cleanup)
- r2_artifacts IS NULL → False (not yet swept)
- Older-generation entries don't count toward current gen → False
- Jianying expected when has_jianying_draft=True, otherwise not
- Job row missing → False

No live boto3 / DB. We monkeypatch r2_client.head_artifact and inject a
fake Job-like object directly (avoiding SQLAlchemy / asyncpg setup).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "src"), str(REPO / "gateway")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---- Fakes ------------------------------------------------------------------


class FakeR2:
    def __init__(self) -> None:
        self.head_calls: list[str] = []
        self.head_returns: dict[str, bool] = {}
        self.head_exc: dict[str, Exception] = {}

    def head_artifact(self, key: str) -> bool:
        self.head_calls.append(key)
        if key in self.head_exc:
            raise self.head_exc[key]
        return self.head_returns.get(key, True)  # default exists


class FakeDB:
    """Stub the only SQLAlchemy surface r2_parity_ok touches.

    The function does ``await db.execute(select(Job).where(...))`` and
    then ``result.scalar_one_or_none()``. We give it back a configured
    Job-like SimpleNamespace.
    """

    def __init__(self, job_obj):
        self._job = job_obj
        self._raise: Exception | None = None

    def will_raise(self, exc: Exception):
        self._raise = exc

    async def execute(self, *args, **kwargs):
        if self._raise is not None:
            raise self._raise
        result = SimpleNamespace()
        result.scalar_one_or_none = lambda: self._job
        return result


def _entry(key, gen=0, state="pushed", r2_key=None) -> dict:
    d: dict = {"artifact_key": key, "edit_generation": gen, "state": state}
    if state in ("pushed", "already_present"):
        d["r2_key"] = r2_key or f"jobs/test/g{gen}/{key}.bin"
    return d


def _job(service_mode="studio", gen=0, registry=None):
    return SimpleNamespace(
        job_id="job_test",
        service_mode=service_mode,
        edit_generation=gen,
        r2_artifacts=registry,
    )


@pytest.fixture
def fake_r2(monkeypatch):
    fake = FakeR2()
    import storage.r2_client as r2_client
    monkeypatch.setattr(r2_client, "head_artifact", fake.head_artifact)
    return fake


# ---- Happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_pushed_with_head_ok_returns_true(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is True
    # Every pushable key got HEAD'd exactly once
    assert sorted(fake_r2.head_calls) == sorted(
        f"jobs/test/g0/{k}.bin" for k in EAGER_PUSH_TO_R2_KEYS_STUDIO
    )


@pytest.mark.asyncio
async def test_already_present_with_head_ok_returns_true(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [
        _entry(k, state="already_present") for k in EAGER_PUSH_TO_R2_KEYS_STUDIO
    ]
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is True


# ---- Refuse paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_entry_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    # Drop one expected key
    drop = next(iter(EAGER_PUSH_TO_R2_KEYS_STUDIO))
    registry = [
        _entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO if k != drop
    ]
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is False


@pytest.mark.asyncio
async def test_failed_state_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    # Flip one entry's state to failed
    registry[0]["state"] = "failed"
    registry[0]["error"] = "simulated PUT timeout"
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is False


@pytest.mark.asyncio
async def test_skipped_missing_passes_when_others_ok(fake_r2):
    """skipped_missing means 'on-disk never had this key either' — both
    sides consistent, cleanup is safe for the others."""
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    # Mark one as skipped_missing (no r2_key)
    registry[0]["state"] = "skipped_missing"
    registry[0].pop("r2_key", None)
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is True
    # HEAD only called on the non-skipped entries
    assert len(fake_r2.head_calls) == len(EAGER_PUSH_TO_R2_KEYS_STUDIO) - 1


@pytest.mark.asyncio
async def test_r2_head_miss_returns_false(fake_r2):
    """Registry says pushed but R2 doesn't have the object — cleanup
    refused (lifecycle / manual delete drift)."""
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    # The first entry's R2 key returns False (object missing)
    fake_r2.head_returns[registry[0]["r2_key"]] = False
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is False


@pytest.mark.asyncio
async def test_r2_head_exception_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    fake_r2.head_exc[registry[0]["r2_key"]] = RuntimeError("R2 network down")
    db = FakeDB(_job(registry=registry))
    assert await r2_parity_ok(db, "job_test") is False


@pytest.mark.asyncio
async def test_null_registry_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    db = FakeDB(_job(registry=None))
    assert await r2_parity_ok(db, "job_test") is False


@pytest.mark.asyncio
async def test_old_generation_entries_dont_count(fake_r2):
    """An overwrite-bumped job has entries from old generation
    sitting in the registry as forensic history. They must not
    satisfy the current generation's expected set."""
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    # All entries stamped gen=0, but job is now gen=1
    registry = [_entry(k, gen=0) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    db = FakeDB(_job(gen=1, registry=registry))
    assert await r2_parity_ok(db, "job_test") is False


# ---- Jianying conditional --------------------------------------------------


@pytest.mark.asyncio
async def test_jianying_required_when_caller_flags_it(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    # Registry has eager set but no jianying entry
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    db = FakeDB(_job(registry=registry))
    # has_jianying_draft=True → parity should refuse because jianying
    # entry is missing from registry
    assert (
        await r2_parity_ok(db, "job_test", has_jianying_draft=True)
        is False
    )


@pytest.mark.asyncio
async def test_jianying_not_required_when_caller_flag_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    db = FakeDB(_job(registry=registry))
    # has_jianying_draft default False → no jianying entry needed
    assert await r2_parity_ok(db, "job_test") is True


@pytest.mark.asyncio
async def test_jianying_with_correct_entry_passes(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    from services.r2_publisher_lib.downloadable_keys import (
        EAGER_PUSH_TO_R2_KEYS_STUDIO,
    )
    registry = [_entry(k) for k in EAGER_PUSH_TO_R2_KEYS_STUDIO]
    registry.append(_entry("editor.jianying_draft_zip"))
    db = FakeDB(_job(registry=registry))
    assert (
        await r2_parity_ok(db, "job_test", has_jianying_draft=True)
        is True
    )


# ---- Job row absent ---------------------------------------------------------


@pytest.mark.asyncio
async def test_no_job_row_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    db = FakeDB(None)  # scalar_one_or_none returns None
    assert await r2_parity_ok(db, "job_missing") is False


@pytest.mark.asyncio
async def test_db_execute_exception_returns_false(fake_r2):
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    db = FakeDB(None)
    db.will_raise(RuntimeError("PG connection lost"))
    assert await r2_parity_ok(db, "job_test") is False


# ---- Express service_mode (smaller expected set) ---------------------------


@pytest.mark.asyncio
async def test_express_requires_dubbed_video_and_poster(fake_r2):
    """Stage C: Express EAGER_PUSH now = {dubbed_video, dubbed_video_poster}.
    Parity must require both (plan 2026-05-07 §11.3 C1)."""
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    registry = [
        _entry("publish.dubbed_video"),
        _entry("publish.dubbed_video_poster"),
    ]
    db = FakeDB(_job(service_mode="express", registry=registry))
    assert await r2_parity_ok(db, "job_test") is True


@pytest.mark.asyncio
async def test_express_missing_poster_refuses_parity(fake_r2):
    """Stage C drift guard: Express registry missing poster → parity False.
    Without this, an upgrade from Stage B (poster not in EAGER_PUSH) leaving
    an Express job with only ``publish.dubbed_video`` entry would let
    cleanup delete the local poster while R2 has nothing → /stream/poster
    breaks. Parity must refuse so the sweeper backfills first."""
    from services.r2_publisher_lib.r2_parity import r2_parity_ok
    registry = [_entry("publish.dubbed_video")]  # missing poster
    db = FakeDB(_job(service_mode="express", registry=registry))
    assert await r2_parity_ok(db, "job_test") is False

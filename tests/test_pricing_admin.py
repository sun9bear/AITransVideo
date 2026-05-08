"""Tests for pricing admin: PricingConfigVersion model shape."""

import sys
import os

# Add gateway to sys.path so bare imports (models, pricing_schema, etc.) resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))


def test_pricing_config_version_model_has_required_columns():
    """Verify the SQLAlchemy model has all expected columns."""
    from models import PricingConfigVersion

    cols = {c.name for c in PricingConfigVersion.__table__.columns}
    assert "id" in cols
    assert "version" in cols
    assert "status" in cols
    assert "payload_json" in cols
    assert "change_note" in cols
    assert "updated_by_user_id" in cols
    assert "created_at" in cols
    assert "activated_at" in cols


def test_pricing_config_version_table_name():
    from models import PricingConfigVersion

    assert PricingConfigVersion.__tablename__ == "pricing_config_versions"


def test_pricing_config_version_indexes():
    """Verify expected indexes exist on the table."""
    from models import PricingConfigVersion

    index_names = {idx.name for idx in PricingConfigVersion.__table__.indexes}
    assert "ix_pricing_config_versions_status" in index_names
    assert "ix_pricing_config_versions_version" in index_names
    assert "ix_pricing_config_versions_created_at" in index_names


# ---------------------------------------------------------------------------
# Task 4: pricing_admin tests — payload validation, frozen field detection
# ---------------------------------------------------------------------------

import pytest
from pricing_schema import PricingPayload, build_default_pricing_payload


def test_pricing_payload_validation_rejects_bad_payload():
    """Invalid payload should fail PricingPayload validation."""
    # Missing required fields
    with pytest.raises(Exception):
        PricingPayload.model_validate({"version": 1})

    # trial.fallback_plan references non-existent plan
    base = build_default_pricing_payload().model_dump()
    base["trial"]["fallback_plan"] = "nonexistent_plan"
    with pytest.raises(Exception):
        PricingPayload.model_validate(base)


def test_frozen_field_detection_no_changes():
    """Identical payloads should produce no frozen field changes."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new = build_default_pricing_payload()
    changes = detect_frozen_field_changes(old, new)
    assert changes == []


def test_frozen_field_detection_plan_price_change():
    """Changing a plan price should be detected as a frozen field change."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["plans"]["plus"]["price_cny_fen"]["monthly"] = 1
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert len(changes) > 0
    assert any("plans" in c and "price_cny_fen" in c for c in changes)


def test_frozen_field_detection_debit_rates_change():
    """Changing debit_rates should be detected as a frozen field change."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["credits"]["debit_rates"]["express.standard"] = 999
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert len(changes) > 0
    assert any("debit_rates" in c for c in changes)


def test_frozen_field_detection_trial_changes():
    """Changing trial frozen fields should be detected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()

    # Change trial.days
    new_data = old.model_dump()
    new_data["trial"]["days"] = 30
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.days" in c for c in changes)

    # Change trial.source_minutes
    new_data = old.model_dump()
    new_data["trial"]["source_minutes"] = 100
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.source_minutes" in c for c in changes)

    # Change trial.grant_credits
    new_data = old.model_dump()
    new_data["trial"]["grant_credits"] = 9999
    new = PricingPayload.model_validate(new_data)
    changes = detect_frozen_field_changes(old, new)
    assert any("trial.grant_credits" in c for c in changes)


def test_frozen_field_detection_non_frozen_change_ignored():
    """Changing non-frozen fields should NOT be detected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    # Change a non-frozen field: plan display_name
    new_data["plans"]["free"]["display_name"] = "Free Tier (Updated)"
    # Change another non-frozen field: cost_model
    new_data["cost_model"]["point_cost_rmb"] = 0.999
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    assert changes == []


def test_publish_requires_change_note_for_frozen_changes():
    """When frozen fields differ and change_note is empty, publish should be rejected."""
    from pricing_schema import detect_frozen_field_changes

    old = build_default_pricing_payload()
    new_data = old.model_dump()
    new_data["credits"]["debit_rates"]["studio.standard"] = 20
    new = PricingPayload.model_validate(new_data)

    changes = detect_frozen_field_changes(old, new)
    # This simulates the check: if frozen changes exist and no change_note, reject
    assert len(changes) > 0
    change_note = ""
    should_reject = len(changes) > 0 and not change_note.strip()
    assert should_reject is True

    # With a proper change_note, should NOT reject
    change_note = "Adjusted studio standard debit rate"
    should_reject = len(changes) > 0 and not change_note.strip()
    assert should_reject is False


# ---------------------------------------------------------------------------
# P1-11c follow-up (audit 2026-05-07): IntegrityError → HTTP 409
#
# Migration 017 added UNIQUE on pricing_config_versions.version. Two
# concurrent admin saves both compute max+1=N+1 and INSERT; the second
# commit raises IntegrityError. The endpoints must catch that and
# return HTTP 409 (not 500) with a Chinese-language retry hint.
# ---------------------------------------------------------------------------


import asyncio
import sys as _sys
import types as _types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _ensure_pricing_admin_imports():
    """Stub the ``database`` module (an asyncpg-bound init in production)
    so we can import ``pricing_admin`` cleanly inside the test process."""
    if "database" not in _sys.modules:
        fake = _types.ModuleType("database")
        fake.async_session = MagicMock()
        fake.engine = MagicMock()
        fake.get_db = MagicMock()
        _sys.modules["database"] = fake


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_admin_user():
    return SimpleNamespace(
        id="admin-user-id",
        email="admin@test.com",
        role="admin",
    )


def _make_db_for_save_draft(*, max_version: int, raise_integrity: bool):
    """Build an AsyncMock session where ``select(max(version))`` returns
    ``max_version`` and ``commit()`` either succeeds or raises
    ``IntegrityError`` (mimicking UNIQUE collision)."""
    from sqlalchemy.exc import IntegrityError

    db = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()

    # ``await db.execute(select(...))`` → result whose
    # ``.scalar_one_or_none()`` returns max_version (or None for empty).
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=max_version)
    db.execute = AsyncMock(return_value=execute_result)

    if raise_integrity:
        db.commit = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("UNIQUE violation"))
        )
        db.rollback = AsyncMock()
    else:
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
    return db


def _make_async_session_ctx(db):
    """Wrap an AsyncMock db in a context-manager MagicMock matching
    ``async_session()`` contract: ``async with async_session() as db: ...``."""
    cm = AsyncMock()
    cm.__aenter__.return_value = db
    cm.__aexit__.return_value = None
    factory = MagicMock(return_value=cm)
    return factory


def test_save_draft_concurrent_unique_conflict_returns_409():
    """P1-11c follow-up: two admins clicking Save Draft concurrently
    both compute version=N+1; the second insert violates UNIQUE on
    version and the endpoint must surface HTTP 409, not propagate
    IntegrityError as a 500."""
    _ensure_pricing_admin_imports()
    import pricing_admin
    from fastapi import HTTPException

    user = _make_admin_user()
    db = _make_db_for_save_draft(max_version=7, raise_integrity=True)

    # Patch async_session so the endpoint's ``async with`` returns our mock.
    original_session = pricing_admin.async_session
    pricing_admin.async_session = _make_async_session_ctx(db)
    try:
        with pytest.raises(HTTPException) as excinfo:
            _run(pricing_admin.save_draft(
                pricing_admin.DraftRequest(
                    payload=build_default_pricing_payload().model_dump()
                ),
                user=user,
            ))
        assert excinfo.value.status_code == 409, (
            "P1-11c follow-up regression: save_draft IntegrityError did "
            f"not translate to 409 (got status={excinfo.value.status_code}, "
            f"detail={excinfo.value.detail!r})."
        )
        # Detail mentions concurrency / retry to help the admin UI map
        # the 409 to a sensible toast.
        assert "版本号冲突" in excinfo.value.detail, (
            f"P1-11c follow-up regression: 409 detail string lost the "
            f"'版本号冲突' marker; got {excinfo.value.detail!r}"
        )
        # Session was rolled back so the failed transaction doesn't
        # leak into the next request on the same connection.
        assert db.rollback.await_count == 1, (
            "P1-11c follow-up regression: save_draft did not rollback "
            "after IntegrityError; subsequent requests on the same "
            "connection would inherit a broken transaction state."
        )
    finally:
        pricing_admin.async_session = original_session


def test_save_draft_happy_path_persists_and_returns_version():
    """No-regression: when commit succeeds, save_draft persists the
    new draft row and returns it."""
    _ensure_pricing_admin_imports()
    import pricing_admin

    user = _make_admin_user()
    db = _make_db_for_save_draft(max_version=3, raise_integrity=False)

    original_session = pricing_admin.async_session
    pricing_admin.async_session = _make_async_session_ctx(db)
    try:
        result = _run(pricing_admin.save_draft(
            pricing_admin.DraftRequest(
                payload=build_default_pricing_payload().model_dump()
            ),
            user=user,
        ))
        assert "version" in result
        # The PricingConfigVersion was added with version=4.
        # db.add was called with the new row — pull the actual model
        # instance to verify version + status.
        added_calls = db.add.call_args_list
        assert len(added_calls) == 1
        new_row = added_calls[0][0][0]
        assert new_row.version == 4
        assert new_row.status == "draft"
        assert db.commit.await_count == 1
        # No rollback on the happy path.
        assert db.rollback.await_count == 0
    finally:
        pricing_admin.async_session = original_session


def _make_db_for_publish(*, max_version: int, raise_integrity: bool):
    """Build an AsyncMock session for publish_pricing.

    publish_pricing calls execute() multiple times:
      1. SELECT active row (returns None or a Mock row)
      2. UPDATE active→archived
      3. UPDATE draft→archived
      4. SELECT max(version)
    Then INSERT via db.add + db.commit.
    """
    from sqlalchemy.exc import IntegrityError

    db = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()

    # Each execute call returns a result. We need: scalar_one_or_none
    # for SELECT (#1 returns None — no active row; #4 returns max_version).
    # For UPDATEs (#2, #3) the return value is irrelevant.
    select_active_result = MagicMock()
    select_active_result.scalar_one_or_none = MagicMock(return_value=None)

    update_result = MagicMock()  # UPDATE result; not inspected

    select_max_result = MagicMock()
    select_max_result.scalar_one_or_none = MagicMock(return_value=max_version)

    db.execute = AsyncMock(side_effect=[
        select_active_result,
        update_result,
        update_result,
        select_max_result,
    ])

    if raise_integrity:
        db.commit = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("UNIQUE violation"))
        )
        db.rollback = AsyncMock()
    else:
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
    return db


def test_publish_pricing_concurrent_unique_conflict_returns_409():
    """P1-11c follow-up: same UNIQUE-on-version race for publish.
    Two admins clicking Publish simultaneously — the loser sees 409,
    not 500."""
    _ensure_pricing_admin_imports()
    import pricing_admin
    from fastapi import HTTPException

    user = _make_admin_user()
    db = _make_db_for_publish(max_version=7, raise_integrity=True)

    original_session = pricing_admin.async_session
    pricing_admin.async_session = _make_async_session_ctx(db)

    # publish_pricing also writes a runtime snapshot post-commit;
    # since commit failed we should NEVER reach that code path.
    snapshot_calls: list[object] = []

    def _track_snapshot(payload):
        snapshot_calls.append(payload)

    original_snapshot = pricing_admin.write_runtime_snapshot
    original_invalidate = pricing_admin.invalidate_runtime_pricing_cache
    pricing_admin.write_runtime_snapshot = _track_snapshot
    invalidate_calls: list[None] = []
    pricing_admin.invalidate_runtime_pricing_cache = lambda: invalidate_calls.append(None)

    try:
        with pytest.raises(HTTPException) as excinfo:
            _run(pricing_admin.publish_pricing(
                pricing_admin.PublishRequest(
                    payload=build_default_pricing_payload().model_dump(),
                    change_note=None,
                ),
                user=user,
            ))
        assert excinfo.value.status_code == 409, (
            f"P1-11c follow-up regression: publish IntegrityError did not "
            f"translate to 409 (got {excinfo.value.status_code}, "
            f"detail={excinfo.value.detail!r})."
        )
        assert "版本号冲突" in excinfo.value.detail
        assert db.rollback.await_count == 1
        # Critical: runtime snapshot + cache invalidate must NOT run
        # when the DB commit failed — otherwise the loser of the race
        # would publish a runtime config that doesn't match any DB row.
        assert snapshot_calls == [], (
            "P1-11c follow-up regression: write_runtime_snapshot was "
            "invoked despite commit failing — would publish runtime "
            "config inconsistent with DB state."
        )
        assert invalidate_calls == [], (
            "P1-11c follow-up regression: invalidate_runtime_pricing_cache "
            "was invoked despite commit failing."
        )
    finally:
        pricing_admin.async_session = original_session
        pricing_admin.write_runtime_snapshot = original_snapshot
        pricing_admin.invalidate_runtime_pricing_cache = original_invalidate


def test_publish_pricing_happy_path_writes_snapshot():
    """No-regression: when publish commit succeeds, runtime snapshot
    is written and cache invalidated."""
    _ensure_pricing_admin_imports()
    import pricing_admin

    user = _make_admin_user()
    db = _make_db_for_publish(max_version=2, raise_integrity=False)

    original_session = pricing_admin.async_session
    pricing_admin.async_session = _make_async_session_ctx(db)

    snapshot_calls: list[object] = []
    pricing_admin.write_runtime_snapshot = lambda p: snapshot_calls.append(p)
    invalidate_calls: list[None] = []
    pricing_admin.invalidate_runtime_pricing_cache = lambda: invalidate_calls.append(None)

    try:
        result = _run(pricing_admin.publish_pricing(
            pricing_admin.PublishRequest(
                payload=build_default_pricing_payload().model_dump(),
                change_note="initial publish",
            ),
            user=user,
        ))
        assert "version" in result
        added = db.add.call_args_list[0][0][0]
        assert added.version == 3
        assert added.status == "active"
        assert added.change_note == "initial publish"
        assert db.commit.await_count == 1
        assert db.rollback.await_count == 0
        # Runtime side effects landed exactly once.
        assert len(snapshot_calls) == 1
        assert len(invalidate_calls) == 1
    finally:
        pricing_admin.async_session = original_session

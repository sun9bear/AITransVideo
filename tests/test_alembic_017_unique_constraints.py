"""Audit P1-11a / P1-11c regression: composite + scalar UNIQUE
constraints on the payment-webhook + pricing-version tables.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-CRITICAL-4 — payment_webhook_events.provider_event_id was
                       UNIQUE alone; cross-provider event-id collisions
                       (Stripe vs Alipay) silently dropped the second
                       provider's settlement.
        D-HIGH-3     — pricing_config_versions.version had no UNIQUE,
                       so two concurrent admin saves both inserted
                       version=N+1.

These guards keep three places in lockstep — alembic migration 017,
the SQLAlchemy model __table_args__, and the
``billing.on_conflict_do_nothing(index_elements=...)`` call site. A
drift between any two of them would break the dedup logic at runtime.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


# =====================================================================
# §1 — Migration 017 exists with the expected upgrade/downgrade pairs
# =====================================================================


_MIGRATION_PATH = (
    _REPO_ROOT / "gateway" / "alembic" / "versions"
    / "017_audit_unique_constraints.py"
)


def test_migration_017_file_exists():
    assert _MIGRATION_PATH.is_file(), (
        "P1-11a/c regression: alembic 017 migration is missing. "
        "The model + billing changes assume the schema is migrated."
    )


def test_migration_017_drops_old_and_creates_composite_unique():
    """The 017 upgrade must drop the legacy single-field unique and
    create the composite ``(provider, provider_event_id)`` unique.

    The downgrade must restore the original single-field unique so a
    rollback is well-defined."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")

    # Upgrade: drop old + create new composite
    assert "drop_constraint" in src and (
        "payment_webhook_events_provider_event_id_key" in src
    ), "P1-11a regression: upgrade does not drop the old single-field UNIQUE"
    assert (
        '"provider", "provider_event_id"' in src
    ), "P1-11a regression: composite UNIQUE expression is missing"
    assert "uq_payment_webhook_events_provider_event" in src, (
        "P1-11a regression: composite UNIQUE constraint name drifted"
    )


def test_migration_017_adds_pricing_version_unique():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "uq_pricing_config_versions_version" in src, (
        "P1-11c regression: migration 017 does not add the version "
        "UNIQUE constraint."
    )
    assert "pricing_config_versions" in src
    assert '"version"' in src or "['version']" in src


# =====================================================================
# §2 — models.py __table_args__ matches the migration
# =====================================================================


def test_payment_webhook_event_model_has_composite_unique():
    """models.PaymentWebhookEvent.__table_args__ must declare the
    composite UNIQUE so SQLAlchemy autogenerate stays in sync."""
    import models
    args = models.PaymentWebhookEvent.__table_args__
    # __table_args__ may be a tuple of constraints or a tuple ending
    # in a dict for table-level kwargs. Walk for a UniqueConstraint
    # whose columns are the composite pair.
    found = False
    for arg in args:
        # SQLAlchemy UniqueConstraint exposes .columns (a ColumnCollection)
        # AND a .name; we accept either an exact name match or a column-
        # set match.
        cols = getattr(arg, "columns", None)
        if cols is None:
            continue
        names = sorted(c.name for c in cols)
        if names == sorted(["provider", "provider_event_id"]):
            found = True
            break
    assert found, (
        "P1-11a regression: PaymentWebhookEvent.__table_args__ does not "
        "declare a composite UNIQUE on (provider, provider_event_id). "
        "Schema and ORM metadata are out of sync — migrations would "
        "drift on next autogenerate."
    )


def test_payment_webhook_event_model_no_longer_has_column_level_unique():
    """The old ``unique=True`` at the column level must be gone, otherwise
    autogenerate would try to recreate a single-field UNIQUE alongside
    the composite — duplicate constraint."""
    import models
    col = models.PaymentWebhookEvent.provider_event_id.property.columns[0]
    assert col.unique is None or col.unique is False, (
        "P1-11a regression: provider_event_id column still has "
        "unique=True; this conflicts with the composite UNIQUE in "
        "__table_args__ and would create a duplicate index."
    )


def test_pricing_config_versions_model_has_version_unique():
    import models
    args = models.PricingConfigVersion.__table_args__
    found = False
    for arg in args:
        cols = getattr(arg, "columns", None)
        if cols is None:
            continue
        # Match any UniqueConstraint whose lone column is "version"
        from sqlalchemy import UniqueConstraint  # local import: lightweight
        if not isinstance(arg, UniqueConstraint):
            continue
        names = [c.name for c in cols]
        if names == ["version"]:
            found = True
            break
    assert found, (
        "P1-11c regression: PricingConfigVersion.__table_args__ does "
        "not declare a UNIQUE on `version`. Two concurrent admin "
        "publishes can again produce duplicate version rows."
    )


# =====================================================================
# §3 — billing.py ON CONFLICT target matches the new composite unique
# =====================================================================


def test_billing_on_conflict_uses_composite_index_elements():
    """``_process_payment_event`` calls
    ``insert_stmt.on_conflict_do_nothing(index_elements=...)``. The
    index_elements list MUST be ``["provider", "provider_event_id"]``,
    matching the composite UNIQUE created in migration 017. If it stays
    as ``["provider_event_id"]`` after 017 lands, PostgreSQL will
    reject the statement at runtime because the conflict target no
    longer matches any UNIQUE constraint."""
    from billing import _process_payment_event
    src = inspect.getsource(_process_payment_event)
    tree = ast.parse(src)

    found_correct_target = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match: ...on_conflict_do_nothing(index_elements=[...])
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "on_conflict_do_nothing"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "index_elements":
                continue
            # Expect a List node with two string constants in order.
            if not isinstance(kw.value, ast.List):
                continue
            elements = [
                el.value
                for el in kw.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            if elements == ["provider", "provider_event_id"]:
                found_correct_target = True
                break
        if found_correct_target:
            break

    assert found_correct_target, (
        "P1-11a regression: _process_payment_event's "
        "on_conflict_do_nothing(index_elements=...) is no longer "
        "['provider', 'provider_event_id']. After alembic 017 swapped "
        "the UNIQUE constraint to composite, any other index_elements "
        "value will cause PostgreSQL to error at runtime ('there is no "
        "unique or exclusion constraint matching the ON CONFLICT "
        "specification')."
    )


# =====================================================================
# §4 — Three sources stay in lockstep
# =====================================================================


def test_three_sources_agree_on_composite_dedup_key():
    """End-to-end consistency: migration 017, models.py, and billing.py
    must all reference the same column pair as the dedup key."""
    # Migration mentions the pair as a string
    mig_src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert '"provider", "provider_event_id"' in mig_src

    # Model __table_args__ has it as a UniqueConstraint
    import models
    pwe_unique_cols = None
    for arg in models.PaymentWebhookEvent.__table_args__:
        cols = getattr(arg, "columns", None)
        if cols is None:
            continue
        names = sorted(c.name for c in cols)
        if names == sorted(["provider", "provider_event_id"]):
            pwe_unique_cols = names
            break
    assert pwe_unique_cols == ["provider", "provider_event_id"]

    # billing.py ON CONFLICT target matches
    billing_src = (_REPO_ROOT / "gateway" / "billing.py").read_text(
        encoding="utf-8"
    )
    assert (
        '"provider", "provider_event_id"' in billing_src
        or "'provider', 'provider_event_id'" in billing_src
    ), (
        "P1-11a regression: billing.py ON CONFLICT target diverged "
        "from the migration / model composite key."
    )

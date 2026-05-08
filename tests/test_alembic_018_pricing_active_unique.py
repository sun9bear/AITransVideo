"""Audit P1-11c follow-up² regression: partial UNIQUE on the pricing
``active`` row.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
    Codex review of commit 6019beb (P1-11c follow-up).

The version-only UNIQUE from migration 017 stops two publishers from
inserting the SAME version number, but it doesn't stop two publishers
that pick DIFFERENT version numbers from both ending up at
``status='active'`` due to a READ COMMITTED interleaving on the
archive UPDATE. Migration 018 adds a partial UNIQUE INDEX on
``(status) WHERE status='active'`` that closes that hole at the
schema level.

These guards keep three places in lockstep — alembic migration 018,
the SQLAlchemy model ``__table_args__``, and the ``pricing_admin``
endpoint that relies on the IntegrityError it raises. Drift between
any pair would silently re-open the race.
"""
from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


_MIGRATION_PATH = (
    _REPO_ROOT / "gateway" / "alembic" / "versions"
    / "018_pricing_active_partial_unique.py"
)


# =====================================================================
# §1 — Migration 018 exists with the expected upgrade/downgrade pairs
# =====================================================================


def test_migration_018_file_exists():
    assert _MIGRATION_PATH.is_file(), (
        "P1-11c follow-up² regression: alembic 018 migration is "
        "missing. The model + pricing_admin handler assume the "
        "partial UNIQUE index is present."
    )


def test_migration_018_revision_chain():
    """Migration revision id + down_revision must match the chain
    that the live DB has applied. Drift = upgrade fails on deploy."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "018_pricing_active_unique"' in src, (
        "P1-11c follow-up² regression: migration 018 revision id "
        "drifted from canonical name. Note: shortened to "
        "018_pricing_active_unique (25 chars) on 2026-05-08 — the "
        "original 33-char id overflowed the alembic_version "
        "VARCHAR(32) column on existing DBs."
    )
    assert 'down_revision: Union[str, None] = "017_audit_unique_constraints"' in src, (
        "P1-11c follow-up² regression: migration 018 down_revision is "
        "not 017_audit_unique_constraints — chain is broken."
    )


def test_migration_018_upgrade_creates_partial_unique_index():
    """The upgrade must call ``op.create_index`` with the partial
    predicate ``status = 'active'`` and ``unique=True``. We assert by
    string match because executing alembic against a real engine in a
    unit test would require a live DB."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Index name must match what the model declares.
    assert "uq_pricing_config_versions_active_status" in src, (
        "P1-11c follow-up² regression: index name "
        "'uq_pricing_config_versions_active_status' is missing "
        "from migration 018."
    )
    # Partial predicate (the WHERE clause) — without this the
    # constraint becomes "globally unique on status" which would be
    # nonsense (only one row in the whole table allowed per status).
    assert 'sa.text("status = \'active\'")' in src or "postgresql_where=sa.text(\"status = 'active'\")" in src, (
        "P1-11c follow-up² regression: partial WHERE predicate for "
        "the active-status index is missing or drifted from "
        "\"status = 'active'\". A non-partial unique index would "
        "wrongly forbid more than one row per status value."
    )
    # Index must be unique=True; without it the constraint is just an
    # index, not an enforcement of the single-active invariant.
    assert "unique=True" in src, (
        "P1-11c follow-up² regression: migration 018 omitted "
        "unique=True; the index would not enforce single-active."
    )


def test_migration_018_downgrade_drops_index():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "drop_index" in src, (
        "P1-11c follow-up² regression: migration 018 has no "
        "drop_index in downgrade — rollback would fail."
    )
    assert "uq_pricing_config_versions_active_status" in src


# =====================================================================
# §2 — Model ``__table_args__`` declares the same partial UNIQUE
# =====================================================================


def test_model_declares_partial_unique_index_on_active_status():
    """SQLAlchemy autogenerate compares the model to the live schema;
    if the model omits the index, ``alembic check`` would suggest
    dropping it. The index must be declared with the exact same
    name + predicate the migration creates."""
    from models import PricingConfigVersion

    indexes = list(PricingConfigVersion.__table__.indexes)
    matched = [
        idx for idx in indexes
        if idx.name == "uq_pricing_config_versions_active_status"
    ]
    assert len(matched) == 1, (
        "P1-11c follow-up² regression: PricingConfigVersion.__table_args__ "
        f"is missing the partial UNIQUE index on (status WHERE active). "
        f"Found indexes: {sorted(idx.name for idx in indexes if idx.name)}. "
        "Without the model declaration, alembic autogenerate would "
        "drop the migration's index on next revision."
    )
    idx = matched[0]
    assert idx.unique is True, (
        "P1-11c follow-up² regression: model index "
        "uq_pricing_config_versions_active_status declared but NOT "
        "marked unique=True; would not enforce single-active."
    )
    # postgresql_where dialect option carries the partial predicate.
    pg_where = idx.dialect_options.get("postgresql", {}).get("where")
    assert pg_where is not None, (
        "P1-11c follow-up² regression: model index missing "
        "postgresql_where predicate. A non-partial unique index "
        "would forbid more than one row per status value."
    )
    # Compile the predicate to SQL text and check it mentions
    # status = 'active'. We avoid an exact equality check because
    # SQLAlchemy may render with whitespace variation.
    rendered = str(pg_where)
    assert "status" in rendered and "active" in rendered, (
        f"P1-11c follow-up² regression: partial predicate drifted; "
        f"got {rendered!r} — expected something equivalent to "
        f'"status = \'active\'".'
    )


# =====================================================================
# §3 — pricing_admin handler still maps IntegrityError → 409
#
# Schema enforcement only helps if the endpoint translates the
# violation to a useful HTTP status. The companion test in
# test_pricing_admin.py asserts the BEHAVIOR; this test asserts the
# code structure can't drift away from "catch IntegrityError on commit"
# without breaking a known reference point.
# =====================================================================


def test_pricing_admin_publish_catches_integrity_error():
    """publish_pricing must keep the ``except IntegrityError`` handler
    that translates BOTH constraint violations to 409.

    AST scan: the ``publish_pricing`` function source must contain
    a handler for ``IntegrityError`` with status_code=409. We don't
    pin the exact catch position because future refactors may move it,
    but we DO pin its presence."""
    import ast
    import inspect
    import pricing_admin

    src = inspect.getsource(pricing_admin.publish_pricing)
    tree = ast.parse(src)

    # Find any ExceptHandler that catches IntegrityError.
    integrity_handlers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            exc_type = node.type
            if exc_type is None:
                continue
            if isinstance(exc_type, ast.Name) and exc_type.id == "IntegrityError":
                integrity_handlers.append(node)
            elif isinstance(exc_type, ast.Tuple):
                for sub in exc_type.elts:
                    if isinstance(sub, ast.Name) and sub.id == "IntegrityError":
                        integrity_handlers.append(node)
                        break
    assert integrity_handlers, (
        "P1-11c follow-up² regression: publish_pricing no longer has "
        "an `except IntegrityError` handler. Both the version UNIQUE "
        "(017) and the active-status partial UNIQUE (018) need this "
        "to surface as HTTP 409 — without it, concurrent publish "
        "collisions become 500."
    )

    # And the handler body must raise HTTPException(status_code=409).
    src_handler = ast.unparse(integrity_handlers[0])
    assert "409" in src_handler, (
        "P1-11c follow-up² regression: IntegrityError handler in "
        "publish_pricing no longer raises 409. Got handler body:\n"
        f"{src_handler}"
    )
    assert "HTTPException" in src_handler, (
        "P1-11c follow-up² regression: IntegrityError handler in "
        "publish_pricing no longer raises HTTPException. The 409 "
        "must reach the client as a proper HTTP response, not a "
        "generic exception."
    )

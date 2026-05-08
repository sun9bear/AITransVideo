"""Tests for system_announcements service.

Live audience resolution requires Postgres (UUID + JSONB + EXISTS
sub-queries don't translate to sqlite cleanly), so the unit tests
focus on:

- AUDIENCE_KINDS catalog shape: 14 entries, unique kinds, each carries
  a label / group / params spec the frontend can render.
- ``_validate_params`` parameter normalization, default filling, range
  clamping for each kind that has parameters.
- AST scan of ``_build_audience_filter`` confirming every catalog kind
  has a code branch (so adding a new kind to AUDIENCE_KINDS without
  wiring the SQL filter trips this test).
- ``SEND_COOLDOWN_SECONDS`` is the documented 60 seconds.

DB-backed tests for ``send_announcement`` / ``recall_announcement`` /
``clone_for_resend`` belong to the integration suite (live PG) and are
out of scope here.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def test_audience_kinds_catalog_has_expected_entries():
    from gateway.system_announcements_service import AUDIENCE_KINDS

    expected_kinds = {
        "all",
        "admin_only",
        "registered_within_days",
        "plan_free",
        "plan_plus",
        "plan_pro",
        "plan_paid",
        "trial_active",
        "trial_ending_within_days",
        "trial_ended_within_days",
        "paid_no_jobs",
        "inactive_for_days",
        "active_with_jobs_within_days",
        "had_failures_within_days",
    }
    actual = {entry["kind"] for entry in AUDIENCE_KINDS}
    assert actual == expected_kinds
    # Catalog size matches plan §"7 类 + 8 类 = 15"; we ended up at 14
    # after merging plan_paid (was redundant with plus+pro). If this
    # changes, update the plan doc.
    assert len(AUDIENCE_KINDS) == 14


def test_audience_kinds_have_required_fields():
    from gateway.system_announcements_service import AUDIENCE_KINDS

    for entry in AUDIENCE_KINDS:
        assert "kind" in entry
        assert "label" in entry
        assert "group" in entry
        assert entry["group"] in {"broad", "subscription", "lifecycle", "behavior"}
        assert isinstance(entry.get("params"), list)
        for p in entry["params"]:
            assert "key" in p
            assert "type" in p
            assert "default" in p


def test_audience_kinds_unique():
    from gateway.system_announcements_service import AUDIENCE_KINDS

    kinds = [e["kind"] for e in AUDIENCE_KINDS]
    assert len(kinds) == len(set(kinds)), "duplicate audience kind in catalog"


# ---------------------------------------------------------------------------
# _validate_params
# ---------------------------------------------------------------------------


def test_validate_params_unknown_kind_raises():
    from gateway.system_announcements_service import _validate_params

    with pytest.raises(ValueError):
        _validate_params("not_a_real_kind", None)


def test_validate_params_fills_defaults():
    from gateway.system_announcements_service import _validate_params

    out = _validate_params("registered_within_days", None)
    assert out == {"days": 7}
    out = _validate_params("registered_within_days", {})
    assert out == {"days": 7}


def test_validate_params_clamps_out_of_range():
    from gateway.system_announcements_service import _validate_params

    # min=1, max=365
    out = _validate_params("registered_within_days", {"days": -5})
    assert out["days"] == 1
    out = _validate_params("registered_within_days", {"days": 99999})
    assert out["days"] == 365


def test_validate_params_handles_string_int():
    """Form data sometimes arrives as strings — must coerce safely."""
    from gateway.system_announcements_service import _validate_params

    out = _validate_params("registered_within_days", {"days": "14"})
    assert out["days"] == 14
    # Garbage falls back to default.
    out = _validate_params("registered_within_days", {"days": "abc"})
    assert out["days"] == 7


def test_validate_params_passthrough_for_no_param_kinds():
    from gateway.system_announcements_service import _validate_params

    for kind in ("all", "admin_only", "plan_free", "plan_plus", "plan_paid", "trial_active"):
        out = _validate_params(kind, None)
        assert out == {}


def test_validate_params_active_with_jobs_two_params():
    from gateway.system_announcements_service import _validate_params

    out = _validate_params("active_with_jobs_within_days", None)
    assert out == {"days": 30, "min_jobs": 5}
    out = _validate_params(
        "active_with_jobs_within_days",
        {"days": 60, "min_jobs": 10},
    )
    assert out == {"days": 60, "min_jobs": 10}


# ---------------------------------------------------------------------------
# AST guard: every catalog kind has a SQL filter branch
# ---------------------------------------------------------------------------


def test_build_audience_filter_covers_every_catalog_kind():
    """If someone adds a new entry to AUDIENCE_KINDS without adding the
    matching ``if kind == "...":`` branch in _build_audience_filter,
    the announcement send will raise at runtime. Catch that here.
    """
    from gateway.system_announcements_service import AUDIENCE_KINDS

    src = (
        REPO / "gateway" / "system_announcements_service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find _build_audience_filter, then collect every string literal
    # compared against ``kind`` in its body.
    branch_kinds: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_build_audience_filter"
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Compare)
                    and len(sub.ops) == 1
                    and isinstance(sub.ops[0], ast.Eq)
                    and isinstance(sub.left, ast.Name)
                    and sub.left.id == "kind"
                    and len(sub.comparators) == 1
                    and isinstance(sub.comparators[0], ast.Constant)
                    and isinstance(sub.comparators[0].value, str)
                ):
                    branch_kinds.add(sub.comparators[0].value)
    catalog_kinds = {e["kind"] for e in AUDIENCE_KINDS}
    missing = catalog_kinds - branch_kinds
    assert not missing, (
        f"AUDIENCE_KINDS catalog declares {missing!r} but "
        "_build_audience_filter has no matching branch. Adding a kind "
        "to the catalog requires adding its SQL filter."
    )


# ---------------------------------------------------------------------------
# Send rate limit constant
# ---------------------------------------------------------------------------


def test_send_cooldown_is_60_seconds():
    from gateway.system_announcements_service import SEND_COOLDOWN_SECONDS

    assert SEND_COOLDOWN_SECONDS == 60


# ---------------------------------------------------------------------------
# Pydantic schema sanity
# ---------------------------------------------------------------------------


def test_announcement_input_validates_minimum():
    from gateway.support_models import AnnouncementInput

    # title and body required + min 1 char.
    with pytest.raises(Exception):
        AnnouncementInput(title="", body="x", audience_kind="all")
    with pytest.raises(Exception):
        AnnouncementInput(title="x", body="", audience_kind="all")
    # severity / topic must be from allowed enums.
    with pytest.raises(Exception):
        AnnouncementInput(
            title="x", body="x", audience_kind="all", severity="catastrophic"
        )


def test_announcement_input_carries_audience_params():
    from gateway.support_models import AnnouncementInput

    a = AnnouncementInput(
        title="x",
        body="y",
        audience_kind="registered_within_days",
        audience_params={"days": 14},
    )
    assert a.audience_kind == "registered_within_days"
    assert a.audience_params == {"days": 14}


# ---------------------------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------------------------


def test_migration_023_revision_id_under_32_chars():
    """alembic_version.version_num is VARCHAR(32). Same trap as 020."""
    src = (
        REPO / "gateway" / "alembic" / "versions" / "023_system_announcements.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    revision_value: str | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
            and isinstance(node.value, ast.Constant)
        ):
            revision_value = node.value.value
            break
    assert revision_value is not None
    assert len(revision_value) <= 32, (
        f"Migration 023 revision id {revision_value!r} ({len(revision_value)} chars) "
        "exceeds VARCHAR(32) ceiling — see 069ada3 hotfix"
    )


def test_migration_023_chains_to_022():
    src = (
        REPO / "gateway" / "alembic" / "versions" / "023_system_announcements.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "down_revision"
            and isinstance(node.value, ast.Constant)
        ):
            assert node.value.value == "022_support_admin_presence"
            return
    raise AssertionError("down_revision not found in migration 023")

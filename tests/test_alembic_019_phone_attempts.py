"""Audit P1-10a-2 / S-HIGH-4 regression: phone-challenge attempts column.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
    S-HIGH-4 — pre-019 verify-code endpoint marked the challenge
               consumed on the FIRST wrong guess, which made a
               per-phone DoS attack trivially cheap.

Migration 019 adds the ``attempts`` counter that the new compare-
first / consume-on-limit logic in ``auth_phone`` requires. These
guards keep three places in lockstep — alembic migration 019, the
SQLAlchemy model column, and the endpoint code that actually uses
``attempts``. Drift between any pair would silently re-open the
DoS hole or break the migration.
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
    / "019_add_phone_challenge_attempts.py"
)


# =====================================================================
# §1 — Migration 019 file shape
# =====================================================================


def test_migration_019_file_exists():
    assert _MIGRATION_PATH.is_file(), (
        "P1-10a-2 regression: alembic 019 migration is missing. "
        "auth_phone's new attempts logic assumes the column exists."
    )


def test_migration_019_revision_chain():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "019_add_phone_challenge_attempts"' in src
    assert 'down_revision: Union[str, None] = "018_pricing_active_partial_unique"' in src, (
        "P1-10a-2 regression: migration 019 down_revision is not "
        "018 — chain is broken; deploy will skip 018 or fail."
    )


def test_migration_019_adds_attempts_column():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "add_column" in src and "phone_verification_challenges" in src
    assert '"attempts"' in src
    assert "Integer" in src
    # Server default backfills existing rows; without it the migration
    # fails on tables with existing rows because the column is NOT NULL.
    assert 'server_default="0"' in src, (
        "P1-10a-2 regression: migration 019 missing server_default='0' "
        "for the attempts column. Existing rows would violate NOT NULL "
        "on upgrade."
    )
    assert "nullable=False" in src


def test_migration_019_downgrade_drops_column():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "drop_column" in src
    assert '"attempts"' in src


# =====================================================================
# §2 — Model column declaration
# =====================================================================


def test_model_has_attempts_column():
    from models import PhoneVerificationChallenge

    cols = {c.name: c for c in PhoneVerificationChallenge.__table__.columns}
    assert "attempts" in cols, (
        "P1-10a-2 regression: PhoneVerificationChallenge model is "
        "missing the attempts column. Alembic autogenerate would "
        "suggest dropping it after migration 019 lands."
    )
    col = cols["attempts"]
    assert not col.nullable, (
        "P1-10a-2 regression: PhoneVerificationChallenge.attempts is "
        "nullable=True; the new auth logic assumes a numeric value "
        "and would crash on None."
    )
    # Server default is critical for the upgrade to land cleanly on a
    # populated table.
    assert col.server_default is not None, (
        "P1-10a-2 regression: PhoneVerificationChallenge.attempts has "
        "no server_default — migration would fail on existing rows."
    )


# =====================================================================
# §3 — auth_phone uses the attempts column
# =====================================================================


def test_auth_phone_defines_max_verify_attempts_constant():
    """The MAX_VERIFY_ATTEMPTS constant must exist and be a positive
    int. Drift to 0 / negative would re-introduce the burn-on-first-
    wrong behaviour we just fixed."""
    import auth_phone

    assert hasattr(auth_phone, "MAX_VERIFY_ATTEMPTS"), (
        "P1-10a-2 regression: auth_phone.MAX_VERIFY_ATTEMPTS constant "
        "is missing. The new compare-first logic depends on it."
    )
    assert isinstance(auth_phone.MAX_VERIFY_ATTEMPTS, int)
    assert auth_phone.MAX_VERIFY_ATTEMPTS >= 2, (
        "P1-10a-2 regression: MAX_VERIFY_ATTEMPTS dropped to "
        f"{auth_phone.MAX_VERIFY_ATTEMPTS} — anything < 2 is back to "
        "the old burn-on-first-wrong behavior we just removed."
    )


def test_auth_phone_endpoints_compare_code_before_consuming():
    """AST scan on both endpoints: the comparison ``challenge.code !=
    code`` must appear BEFORE any assignment to ``challenge.consumed_at``
    in the success path. Pre-019 the order was reversed, which was
    the bug."""
    import ast
    import inspect

    import auth_phone

    for endpoint_name in ("verify_code_endpoint", "reset_password_endpoint"):
        fn = getattr(auth_phone, endpoint_name)
        src = inspect.getsource(fn)
        tree = ast.parse(src)

        # Locate the position of the first "challenge.code != code"
        # comparison and the first "challenge.consumed_at = now"
        # assignment. We compare line numbers — the comparison must
        # appear first.
        compare_lineno = None
        consumed_lineno = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "code"
                and isinstance(node.left.value, ast.Name)
                and node.left.value.id == "challenge"
            ):
                if compare_lineno is None:
                    compare_lineno = node.lineno
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute)
                    and t.attr == "consumed_at"
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "challenge"
                    for t in node.targets
                )
            ):
                if consumed_lineno is None:
                    consumed_lineno = node.lineno

        assert compare_lineno is not None, (
            f"P1-10a-2 regression: {endpoint_name} no longer compares "
            "challenge.code anywhere. The endpoint can't enforce "
            "wrong-code attempts if it never compares."
        )
        assert consumed_lineno is not None, (
            f"P1-10a-2 regression: {endpoint_name} no longer assigns "
            "challenge.consumed_at anywhere — the challenge can never "
            "be retired."
        )
        # The comparison must appear before any consumed_at assignment
        # so wrong codes can be detected without burning the challenge.
        # NOTE: we compare the FIRST occurrence of each. The first
        # consumed_at assignment is the wrong-attempt-limit retire path
        # (only fires when attempts >= MAX); the success path also has
        # a consumed_at assignment AFTER the compare. As long as
        # compare appears before the first consumed_at, the contract
        # holds — the limit-retire path is itself inside an if-branch
        # gated on the comparison.
        assert compare_lineno < consumed_lineno, (
            f"P1-10a-2 regression: in {endpoint_name}, "
            f"challenge.consumed_at is assigned at line {consumed_lineno} "
            f"BEFORE challenge.code is compared at line {compare_lineno}. "
            f"This is the exact pre-019 bug: a wrong guess burns the "
            f"challenge before we even check whether it's correct."
        )

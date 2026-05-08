"""Audit P2-22B / D-HIGH-4 regression: alembic 007 downgrade must be
safe in the presence of phone-only users.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-HIGH-4 — pre-fix downgrade re-added NOT NULL on
                   ``users.email`` and ``users.password_hash`` even
                   though phone-only registrations land both columns
                   as NULL. Any DR rollback against a DB where 007
                   has been applied long enough to accumulate
                   phone-only users would fail with
                   ``column "email" contains null values``.

Production has at least 3 confirmed phone-only users as of
2026-05-08 (17612735518 + 18672925519 + 18971025559 + the trial-IP
remediation script's audit), so the original downgrade was
permanently broken on this DB. The fix removes the NOT NULL re-add
on the principle that DR rollbacks should NEVER silently delete
user data.

These guards lock that decision in: any future "tidy up the
asymmetry" attempt that re-adds the NOT NULL would need to be
caught + reviewed BEFORE landing.
"""
from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / "gateway" / "alembic" / "versions"
    / "007_add_phone_and_trial_fields.py"
)


def test_migration_007_file_exists():
    assert _MIGRATION_PATH.is_file()


def test_migration_007_downgrade_does_not_re_add_not_null_on_email():
    """The downgrade must NOT call ``alter_column(..., nullable=False)``
    on ``users.email``. Pre-fix this line existed and crashed any DR
    rollback on a DB with phone-only users."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Look for any alter_column on users.email with nullable=False.
    # Pattern catches both single-line and multi-line forms.
    pattern = re.compile(
        r'alter_column\s*\(\s*"users"\s*,\s*"email"[^)]*nullable\s*=\s*False',
        re.DOTALL,
    )
    match = pattern.search(src)
    assert match is None, (
        "P2-22B regression: migration 007 downgrade re-added NOT NULL "
        "on users.email. Production has phone-only users with "
        "email=NULL — this alter_column would crash with "
        "'column \"email\" contains null values' on any real DR "
        "rollback. See audit D-HIGH-4. Found at:\n"
        f"  {match.group(0) if match else ''!r}\n"
        "If you genuinely need to restore the NOT NULL constraint, "
        "the docstring above the downgrade body documents the manual "
        "DELETE-then-ALTER procedure — do NOT silently drop user data "
        "from the migration."
    )


def test_migration_007_downgrade_does_not_re_add_not_null_on_password_hash():
    """Same guard for users.password_hash — also NULL on phone-only users."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r'alter_column\s*\(\s*"users"\s*,\s*"password_hash"[^)]*nullable\s*=\s*False',
        re.DOTALL,
    )
    match = pattern.search(src)
    assert match is None, (
        "P2-22B regression: migration 007 downgrade re-added NOT NULL "
        "on users.password_hash. Production has phone-only users with "
        "password_hash=NULL (only set after a future bind / reset). "
        "This alter_column would crash on DR rollback. See audit "
        "D-HIGH-4."
    )


def test_migration_007_downgrade_keeps_user_data_drops_only():
    """Defensive: the downgrade must drop SCHEMA elements but NOT
    delete user rows. A migration that silently runs ``DELETE FROM
    users`` to make NOT NULL re-addable is exactly the wrong way to
    fix D-HIGH-4."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Look for any DELETE FROM users in the downgrade body. We allow
    # references INSIDE comments / docstrings (the manual DR procedure
    # is documented there), but NOT in executable code.
    # Strip comments + docstrings via tokenize.
    import io
    import tokenize

    code_only_tokens: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_only_tokens.append(tok.string)
    except tokenize.TokenizeError:
        code_only_tokens = [src]
    code_only = " ".join(code_only_tokens)

    # Look for DELETE FROM users (case-insensitive, with optional
    # whitespace + WHERE clause).
    delete_pattern = re.compile(
        r"DELETE\s+FROM\s+users", re.IGNORECASE
    )
    match = delete_pattern.search(code_only)
    assert match is None, (
        "P2-22B regression: migration 007 downgrade contains "
        "executable DELETE FROM users. A DR rollback should NEVER "
        "silently drop user data — that's the original audit warning. "
        "If clean-up is required, document a manual procedure in the "
        "docstring instead."
    )


def test_migration_007_downgrade_documents_the_asymmetry():
    """The downgrade's docstring / comments must explicitly call out
    the schema asymmetry (post-rollback email + password_hash stay
    nullable). Without this note, future maintainers might 'fix' the
    asymmetry by re-introducing the bug."""
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Look for either of the audit-related references that should be
    # near the explanation. The exact wording can drift; we check for
    # high-signal markers.
    must_contain_one_of = [
        "P2-22B",
        "D-HIGH-4",
        "phone-only",
        "schema asymmetry",
    ]
    found = [marker for marker in must_contain_one_of if marker in src]
    assert found, (
        "P2-22B regression: migration 007 no longer carries any of the "
        "audit-link markers in its source. The asymmetry decision "
        "needs to stay visible to future maintainers — without it, "
        "a 'cleanup' PR could silently re-add the NOT NULL bug. "
        f"Expected one of: {must_contain_one_of}"
    )

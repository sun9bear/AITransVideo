"""Regression guards for email auth migration 026."""

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

_MIGRATION_PATH = (
    _REPO_ROOT / "gateway" / "alembic" / "versions" / "026_email_auth.py"
)


def test_migration_026_file_exists():
    assert _MIGRATION_PATH.is_file()


def test_migration_026_revision_chain():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "026_email_auth"' in src
    assert 'down_revision: Union[str, None] = "025_add_r2_artifacts"' in src


def test_migration_026_adds_user_verified_timestamp_and_challenge_table():
    src = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "email_verified_at" in src
    assert "email_verification_challenges" in src
    assert "code_hash" in src
    assert "password_hash" in src
    assert "attempts" in src
    assert 'server_default="0"' in src


def test_models_have_email_auth_columns():
    from models import EmailVerificationChallenge, User

    user_cols = {c.name: c for c in User.__table__.columns}
    assert "email_verified_at" in user_cols

    challenge_cols = {c.name: c for c in EmailVerificationChallenge.__table__.columns}
    for name in (
        "email",
        "code_hash",
        "purpose",
        "password_hash",
        "display_name",
        "expires_at",
        "consumed_at",
        "attempts",
    ):
        assert name in challenge_cols
    assert not challenge_cols["attempts"].nullable
    assert challenge_cols["attempts"].server_default is not None

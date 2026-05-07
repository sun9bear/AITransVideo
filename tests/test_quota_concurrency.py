"""P0-4 (audit 2026-05-07) regression: free-quota and admin-credits-bucket
reserve/release paths must lock the row to prevent lost-update under concurrent
job creation.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        D-CRITICAL-1 — gateway/quota.py reserve/release without with_for_update()
                       lets two concurrent free-user reserves both observe
                       used=4 and both write 5; real value should be 6.
        D-HIGH-5     — gateway/credits_service.ensure_admin_credits_bucket
                       top-up branch had the same lost-update window.

The default test set runs at AST/source level with no DB dependency, so it
runs in any environment (including the SQLite-only CI sandbox where
`SELECT ... FOR UPDATE` is silently ignored anyway).

The integration test under `test_concurrent_reserve_quota_does_not_lose_update`
requires real PostgreSQL and is skipped unless `AVT_TEST_USE_REAL_PG=1` is set.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)

# Provide a stub `database` module before importing gateway modules — same
# trick used by tests/test_gateway_quota.py to keep imports cheap and avoid
# touching real DB engines during collection.
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)


# =====================================================================
# §1 Source-level guards — these are the contract-level regressions
#    that fire if anyone removes .with_for_update() from the protected
#    code paths.
# =====================================================================


def _src_of(fn) -> str:
    """Return the source body of a callable for substring/AST inspection."""
    return inspect.getsource(fn)


def _has_with_for_update_call(fn) -> bool:
    """AST-level check: does `fn`'s body contain a Call node whose callee
    attribute is `with_for_update`? Catches both
        select(User).where(...).with_for_update()
    and
        stmt = stmt.with_for_update().
    """
    import textwrap

    src = _src_of(fn)
    # textwrap.dedent removes common leading whitespace from EVERY line —
    # safe for both module-level fns (no-op) and methods with class indent.
    # Avoid inspect.cleandoc here: that flattens the function body which
    # raises IndentationError on `def foo():\n"""..."""\n  body`.
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "with_for_update":
                return True
    return False


def test_reserve_quota_uses_with_for_update():
    """reserve_quota must lock the User row before reading
    free_jobs_quota_used so two concurrent reserves cannot both observe
    used=N and both write N+1 (real value should be N+2)."""
    from quota import reserve_quota  # noqa: WPS433

    src = _src_of(reserve_quota)
    assert "with_for_update" in src, (
        "P0-4 regression: gateway/quota.py reserve_quota no longer calls "
        ".with_for_update() — concurrent free-quota reserves can now lose "
        "updates and over-allocate the free tier. See D-CRITICAL-1."
    )
    assert _has_with_for_update_call(reserve_quota), (
        "reserve_quota mentions 'with_for_update' in source but the AST "
        "does not show a call. Was it commented out?"
    )


def test_release_quota_uses_with_for_update():
    """release_quota must lock the User row before reading
    free_jobs_quota_used so a refund concurrent with a reserve cannot
    silently dropped because of a stale read."""
    from quota import release_quota  # noqa: WPS433

    src = _src_of(release_quota)
    assert "with_for_update" in src, (
        "P0-4 regression: gateway/quota.py release_quota no longer calls "
        ".with_for_update() — concurrent reserve+release on the same user "
        "can lose the refund. See D-CRITICAL-1."
    )
    assert _has_with_for_update_call(release_quota)


def test_ensure_admin_credits_bucket_topup_uses_with_for_update():
    """ensure_admin_credits_bucket reads `existing.granted` and then writes
    `existing.granted + delta` and `existing.remaining + delta`. Without a
    row lock, two concurrent admin-grant top-ups can both observe granted=X
    and both write granted=X+delta (real value should be X+2*delta).
    Audit ref: D-HIGH-5."""
    from credits_service import ensure_admin_credits_bucket  # noqa: WPS433

    src = _src_of(ensure_admin_credits_bucket)
    assert "with_for_update" in src, (
        "P0-4 regression: gateway/credits_service.py "
        "ensure_admin_credits_bucket no longer calls .with_for_update() — "
        "concurrent admin top-ups can lose updates. See D-HIGH-5."
    )
    assert _has_with_for_update_call(ensure_admin_credits_bucket)


def test_quota_module_uses_with_for_update_at_least_twice():
    """Belt-and-suspenders: the quota.py module as a whole must contain at
    least two .with_for_update() calls (one in reserve_quota, one in
    release_quota). This catches a partial revert where only one of the
    two paths is restored."""
    quota_src = (_REPO_ROOT / "gateway" / "quota.py").read_text(encoding="utf-8")
    count = quota_src.count("with_for_update")
    assert count >= 2, (
        f"Expected >=2 .with_for_update() occurrences in gateway/quota.py, "
        f"found {count}. Both reserve_quota and release_quota must lock "
        f"the User row."
    )


# =====================================================================
# §2 Integration test — only runs when AVT_TEST_USE_REAL_PG=1 is set.
#    Spawns two asyncio tasks both calling reserve_quota concurrently
#    and asserts the final used counter is 2 (not 1). Skipped in CI by
#    default since SQLite ignores FOR UPDATE.
# =====================================================================


_USE_REAL_PG = os.environ.get("AVT_TEST_USE_REAL_PG") == "1"


@pytest.mark.skipif(
    not _USE_REAL_PG,
    reason=(
        "Integration test for P0-4 row lock requires real PostgreSQL. "
        "Set AVT_TEST_USE_REAL_PG=1 + AVT_TEST_PG_DSN=postgresql+asyncpg://... "
        "to run."
    ),
)
def test_concurrent_reserve_quota_does_not_lose_update():  # pragma: no cover
    """Real-PG end-to-end: two concurrent reserve_quota calls on the same
    free user must end with used=2, not used=1. SQLite would silently let
    this fail — that's why we gate behind a real PG DSN.

    Set the following env vars to run:
        AVT_TEST_USE_REAL_PG=1
        AVT_TEST_PG_DSN=postgresql+asyncpg://user:pass@host/db
    """
    import asyncio
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from models import Base, Job, User  # noqa: WPS433
    from quota import reserve_quota  # noqa: WPS433

    dsn = os.environ.get(
        "AVT_TEST_PG_DSN",
        "postgresql+asyncpg://postgres:postgres@localhost/avt_test",
    )

    async def _scenario() -> int:
        engine = create_async_engine(dsn, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = sessionmaker(  # noqa: N806
            engine, class_=AsyncSession, expire_on_commit=False,
        )

        uid = str(uuid.uuid4())
        # Seed the user
        async with Session() as setup:
            user = User(
                id=uid,
                email=f"{uid}@example.com",
                plan_code="free",
                free_jobs_quota_total=5,
                free_jobs_quota_used=0,
            )
            setup.add(user)
            await setup.commit()

        async def _reserve_one() -> bool:
            async with Session() as s:
                async with s.begin():
                    j = Job(
                        job_id=str(uuid.uuid4()),
                        user_id=uid,
                        status="created",
                        quota_state="none",
                    )
                    s.add(j)
                    await s.flush()
                    return await reserve_quota(s, uid, j)

        results = await asyncio.gather(_reserve_one(), _reserve_one())

        async with Session() as s:
            from sqlalchemy import select  # noqa: WPS433

            row = (
                await s.execute(select(User).where(User.id == uid))
            ).scalar_one()
            return int(row.free_jobs_quota_used)

    final_used = asyncio.run(_scenario())
    assert final_used == 2, (
        f"P0-4 row lock failed: two concurrent reserves left used={final_used}, "
        f"expected 2. Lost update bug has resurfaced."
    )

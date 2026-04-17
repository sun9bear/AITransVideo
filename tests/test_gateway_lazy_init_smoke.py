"""Smoke tests for T3 lazy-init compatibility with external tooling.

After T3 made `gateway/config.py` stop self-populating `settings.database_url`
and `gateway/database.py` require explicit `init_db()`, two independent
entry points broke and needed follow-up fixes (flagged by Codex review):

  1. Alembic env.py read `settings.database_url` directly at module import.
     With the standard compose setup (`AVT_PG_PASSWORD` set,
     `AVT_DATABASE_URL` empty), that URL was now blank → migrations broke.
     Fix: env.py calls resolve_database_url(settings) explicitly.

  2. gateway/migrate_jobs.py used `engine.begin()` / `async_session()` without
     ever calling `init_db()` → RuntimeError. Fix: call init_db() in migrate().

These smoke tests lock down both fixes so the regressions can't silently
reappear.
"""
from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


def test_alembic_env_uses_resolve_database_url(monkeypatch):
    """Alembic's env.py must get a real URL even when AVT_DATABASE_URL is
    empty and only AVT_PG_PASSWORD is set (standard compose layout).

    Previous bug: env.py read settings.database_url (empty by design post-T3)
    and fed that to sqlalchemy.url, breaking migrations.

    We verify by reading env.py source and confirming it calls
    resolve_database_url rather than reading settings.database_url directly.
    """
    env_py = Path(_gateway_dir) / "alembic" / "env.py"
    src = env_py.read_text(encoding="utf-8")
    assert "resolve_database_url(settings)" in src, (
        "Alembic env.py must call resolve_database_url(settings) — "
        "otherwise standard compose (AVT_PG_PASSWORD, empty AVT_DATABASE_URL) "
        "gives it an empty sqlalchemy.url and migrations break."
    )
    # And it must NOT be feeding the empty settings.database_url directly
    # to sqlalchemy.url. (The old broken pattern was:
    # config.set_main_option("sqlalchemy.url", settings.database_url.replace(...)))
    assert "settings.database_url.replace" not in src, (
        "Regression: env.py is back to reading settings.database_url directly. "
        "This breaks when only AVT_PG_PASSWORD is set (standard compose case)."
    )


def test_migrate_jobs_calls_init_db():
    """gateway/migrate_jobs.py is a standalone script — it must explicitly
    call init_db() before using the `engine` / `async_session` proxies.

    Previous bug: the script imported engine/async_session and used them
    directly, but after T3 those are proxies that raise RuntimeError unless
    init_db() has run. Script entry was broken.
    """
    migrate_py = Path(_gateway_dir) / "migrate_jobs.py"
    src = migrate_py.read_text(encoding="utf-8")
    assert "init_db()" in src, (
        "gateway/migrate_jobs.py must call init_db() before using engine/"
        "async_session — T3 made them lazy proxies that require explicit init."
    )
    # Import line should bring in init_db alongside engine/async_session
    assert "init_db" in src.split("from database import")[1].split("\n")[0], (
        "migrate_jobs.py should import init_db from database module."
    )


def test_resolve_database_url_works_with_pg_password_only(monkeypatch):
    """End-to-end check: the standard compose path (AVT_PG_PASSWORD set,
    AVT_DATABASE_URL empty) must produce a valid postgresql+asyncpg URL.

    This is what both the Alembic fix and the init_db() call depend on.
    """
    from config import GatewaySettings, resolve_database_url

    s = GatewaySettings(database_url="", pg_password="somesecret")
    url = resolve_database_url(s)
    assert url.startswith("postgresql+asyncpg://avt:"), (
        f"Expected postgresql+asyncpg URL, got: {url}"
    )
    assert "somesecret" in url
    # sanity: the URL is non-empty (so Alembic / init_db won't fail)
    assert len(url) > len("postgresql+asyncpg://avt:@:5432/aivideotrans")


def test_compose_defaults_gateway_env_to_production():
    """Regression guard for Codex P3 deployment gap:

    The T6 `validate_production_safety` startup check only fires when
    `AVT_ENV == "production"`. If the gateway service in docker-compose.yml
    doesn't set AVT_ENV at all, Pydantic falls back to the class default
    ("dev") and the guard is silently bypassed — defeating the whole
    point of T6.

    The repo-managed compose manifest IS the production deploy source of
    truth (via Deploy-Via-154.cmd). So it must pin AVT_ENV to production
    by default. Local dev overrides via their own .env.
    """
    compose = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    src = compose.read_text(encoding="utf-8")

    # Must set AVT_ENV with a production-default fallback.
    # Accept either `${AVT_ENV:-production}` (preferred, dev-friendly) or a
    # hard-coded `"production"` string. Reject the absence of AVT_ENV entirely.
    assert "AVT_ENV:" in src, (
        "docker-compose.yml gateway service must set AVT_ENV. Without it, "
        "GatewaySettings.env defaults to 'dev' and T6's production-safety "
        "guard is silently disabled."
    )
    # Specifically: find the gateway service block and confirm AVT_ENV there
    # resolves to 'production' by default (either hard-coded or via :-default).
    assert (
        "AVT_ENV: \"${AVT_ENV:-production}\"" in src
        or "AVT_ENV: \"production\"" in src
        or "AVT_ENV=production" in src  # allow shell-form if used elsewhere
    ), (
        "AVT_ENV in docker-compose.yml must default to 'production' "
        "(e.g. ${AVT_ENV:-production}) so forgetting to set it in the "
        "external .env doesn't silently disable the T6 safety guard."
    )

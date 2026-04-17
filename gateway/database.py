"""Database setup and session management — lazy initialization.

Engine and session_maker are created on first access (via explicit init_db()
at app startup), not at import time. This keeps `import gateway.database`
side-effect-free so tests can collect without valid DB credentials.

Backward compatibility: `engine` and `async_session` are exposed as proxy
objects that transparently delegate to the lazily-initialized singletons.
This means existing call sites like `async with async_session() as db:` and
`async with engine.begin() as conn:` keep working without edits, as long as
init_db() has been called before the first real use (which main.py does).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import resolve_database_url, settings

_engine: AsyncEngine | None = None
_async_session: async_sessionmaker | None = None


def init_db(url: str | None = None) -> None:
    """Explicitly initialize engine + session maker. Called from app startup.

    url: optional override (tests may pass a local TEST_DATABASE_URL).
    If None, calls resolve_database_url(settings) — which raises if creds missing.
    """
    global _engine, _async_session
    resolved = url if url is not None else resolve_database_url(settings)
    _engine = create_async_engine(resolved, echo=False, pool_size=5, max_overflow=10)
    _async_session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def _require_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() at app startup before using engine."
        )
    return _engine


def _require_session_maker() -> async_sessionmaker:
    if _async_session is None:
        raise RuntimeError(
            "Database not initialized. Call init_db() at app startup before handling requests."
        )
    return _async_session


class _EngineProxy:
    """Transparent proxy to the lazily-initialized engine.

    Delegates all attribute access to the real engine after init_db() has run.
    Exists solely to preserve the `from database import engine` import pattern
    used across the codebase without requiring every call site to change.
    """

    def __getattr__(self, name: str):
        return getattr(_require_engine(), name)

    def __repr__(self) -> str:
        if _engine is None:
            return "<EngineProxy uninitialized>"
        return repr(_engine)


class _AsyncSessionProxy:
    """Transparent proxy to the lazily-initialized async_sessionmaker.

    Supports the `async_session()` call pattern used across the codebase.
    """

    def __call__(self, *args, **kwargs):
        return _require_session_maker()(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(_require_session_maker(), name)

    def __repr__(self) -> str:
        if _async_session is None:
            return "<AsyncSessionProxy uninitialized>"
        return repr(_async_session)


# Backward-compatible module-level names. Importers may do either
#   from database import engine, async_session
# or
#   import database; database.engine; database.async_session
# Both work — the proxies always resolve to the current singletons at call time.
engine = _EngineProxy()
async_session = _AsyncSessionProxy()


async def get_db() -> AsyncSession:
    maker = _require_session_maker()
    async with maker() as session:
        yield session

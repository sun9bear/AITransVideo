"""APF P0 — PostgreSQL-backed rate-limit counter store for anonymous preview.

Implements the ``CounterStore`` protocol declared in
``src.services.anonymous_preview_backend_adapter`` via an atomic
``INSERT … ON CONFLICT DO UPDATE … WHERE count < cap RETURNING`` pattern
so a single DB round-trip is both the existence check and the conditional
increment.

Design constraints:
* No import of services.jobs or any module under src.pipeline / src.services
  other than the rate-limit contract modules.
* Any SQLAlchemy / connection exception is caught and re-raised as
  ``RateLimitCounterUnavailable`` (chaining the original) so the adapter's
  fail-closed branch activates immediately.
* Day key is computed in Asia/Shanghai (UTC+8) using ``zoneinfo``.
* ``decrement`` is implemented but only intended for the adapter's
  multi-key rollback path; callers outside that path must not invoke it.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone, timedelta
from typing import Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.anonymous_preview_rate_limit import RateLimitCounterUnavailable


__all__ = [
    "hash_scope_key",
    "PgRateLimitCounterStore",
    "shanghai_today",
]

# Asia/Shanghai is fixed UTC+8 (no DST), so a timedelta offset is exact.
# We avoid zoneinfo/tzdata to keep the gateway container dependency-free.
_SHANGHAI = timezone(timedelta(hours=8))


def shanghai_today(now: datetime | None = None) -> str:
    """Return today's date string in Asia/Shanghai time (YYYY-MM-DD).

    Asia/Shanghai is fixed UTC+8 (no DST). If ``now`` is provided it is
    converted to UTC+8; otherwise ``datetime.now(UTC)`` is used. This is
    the canonical day-key function for all PgRateLimitCounterStore
    operations.

    Uses a plain ``timedelta(hours=8)`` offset rather than ``zoneinfo``
    so the gateway container does not require the ``tzdata`` package.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    shanghai_now = now.astimezone(_SHANGHAI)
    return shanghai_now.strftime("%Y-%m-%d")


def hash_scope_key(value: str, *, secret: str) -> str:
    """Return HMAC-SHA256 hex digest of ``value`` keyed with ``secret``.

    Deterministic: same inputs always produce the same output. Different
    secrets produce different outputs so secrets must not be mixed across
    scope types. Raw IP / device / source values must NEVER be persisted;
    callers must hash them via this function before storage.
    """
    return hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class PgRateLimitCounterStore:
    """PostgreSQL-backed implementation of the ``CounterStore`` protocol.

    Uses ``anonymous_preview_daily_usage`` table with an atomic
    ``INSERT … ON CONFLICT DO UPDATE … WHERE count < cap RETURNING``
    pattern so a single statement serves as check + conditional increment
    (no SELECT + UPDATE race).

    Parameters
    ----------
    session:
        A SQLAlchemy ``Session`` already bound to the gateway DB. The
        caller is responsible for commit/rollback; this store only issues
        DML within the provided session.
    scope:
        One of ``'global'``, ``'ip'``, ``'device'``, ``'source'``.
    mode:
        Preview mode key (default ``'free'``).
    now:
        Optional clock override for tests. If ``None``, uses UTC now to
        compute the Shanghai day key.
    """

    def __init__(
        self,
        session: Session,
        *,
        scope: str,
        mode: str = "free",
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._scope = scope
        self._mode = mode
        self._day = shanghai_today(now)

    # ------------------------------------------------------------------
    # CounterStore protocol methods
    # ------------------------------------------------------------------

    def get(self, key: str) -> int:
        """Return the current count for ``key``, or 0 if no row exists."""
        try:
            row = self._session.execute(
                text(
                    "SELECT count FROM anonymous_preview_daily_usage "
                    "WHERE scope = :scope AND scope_key = :key "
                    "  AND mode = :mode AND usage_date = :day"
                ),
                {
                    "scope": self._scope,
                    "key": key,
                    "mode": self._mode,
                    "day": self._day,
                },
            ).fetchone()
            return int(row[0]) if row is not None else 0
        except Exception as exc:
            raise RateLimitCounterUnavailable(
                f"get failed for scope={self._scope!r} key={key!r}"
            ) from exc

    def increment(self, key: str) -> int:
        """Unconditionally increment the counter for ``key`` by 1.

        Uses upsert: inserts a new row with count=1 if absent, otherwise
        adds 1 to the existing count. Returns the new count.
        """
        try:
            row = self._session.execute(
                text(
                    """
                    INSERT INTO anonymous_preview_daily_usage
                        (id, scope, scope_key, mode, usage_date, count,
                         created_at, updated_at)
                    VALUES
                        (gen_random_uuid(), :scope, :key, :mode, :day, 1,
                         now(), now())
                    ON CONFLICT (scope, scope_key, mode, usage_date)
                    DO UPDATE SET
                        count = anonymous_preview_daily_usage.count + 1,
                        updated_at = now()
                    RETURNING count
                    """
                ),
                {
                    "scope": self._scope,
                    "key": key,
                    "mode": self._mode,
                    "day": self._day,
                },
            ).fetchone()
            return int(row[0])
        except Exception as exc:
            raise RateLimitCounterUnavailable(
                f"increment failed for scope={self._scope!r} key={key!r}"
            ) from exc

    def try_acquire(self, key: str, cap: int) -> Tuple[bool, int]:
        """Atomically check-and-increment against ``cap``.

        Issues a single ``INSERT … ON CONFLICT DO UPDATE … WHERE count < cap
        RETURNING`` statement. If the WHERE clause on the UPDATE is not
        satisfied (i.e. count is already at cap) no row is returned,
        meaning the request is denied without mutating state.

        Returns ``(True, new_count)`` on admission, ``(False, current)``
        on denial.
        """
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise RateLimitCounterUnavailable(
                f"cap must be a non-negative int, got {cap!r}"
            )
        try:
            # Attempt atomic conditional upsert.
            # The UPDATE's WHERE filters out rows already at cap, so if
            # the counter is already >= cap the RETURNING clause returns
            # nothing, and we fall through to a plain SELECT to get the
            # current value.
            row = self._session.execute(
                text(
                    """
                    INSERT INTO anonymous_preview_daily_usage
                        (id, scope, scope_key, mode, usage_date, count,
                         created_at, updated_at)
                    VALUES
                        (gen_random_uuid(), :scope, :key, :mode, :day, 1,
                         now(), now())
                    ON CONFLICT (scope, scope_key, mode, usage_date)
                    DO UPDATE SET
                        count = anonymous_preview_daily_usage.count + 1,
                        updated_at = now()
                    WHERE anonymous_preview_daily_usage.count < :cap
                    RETURNING count
                    """
                ),
                {
                    "scope": self._scope,
                    "key": key,
                    "mode": self._mode,
                    "day": self._day,
                    "cap": cap,
                },
            ).fetchone()
            if row is not None:
                return (True, int(row[0]))
            # Row exists but count >= cap — fetch current value for the
            # (False, current) tuple.
            current = self.get(key)
            return (False, current)
        except RateLimitCounterUnavailable:
            raise
        except Exception as exc:
            raise RateLimitCounterUnavailable(
                f"try_acquire failed for scope={self._scope!r} key={key!r}"
            ) from exc

    def decrement(self, key: str) -> int:
        """Best-effort rollback of a prior try_acquire admission.

        Only intended for use by the backend adapter's multi-key rollback
        path (``AnonymousPreviewBackendAdapter._rollback_admitted``).
        Callers outside that path normally have no reason to invoke this.
        Floors at zero so a stray rollback never produces a negative count.
        """
        try:
            row = self._session.execute(
                text(
                    """
                    UPDATE anonymous_preview_daily_usage
                    SET count = GREATEST(count - 1, 0),
                        updated_at = now()
                    WHERE scope = :scope AND scope_key = :key
                      AND mode = :mode AND usage_date = :day
                    RETURNING count
                    """
                ),
                {
                    "scope": self._scope,
                    "key": key,
                    "mode": self._mode,
                    "day": self._day,
                },
            ).fetchone()
            return int(row[0]) if row is not None else 0
        except Exception as exc:
            raise RateLimitCounterUnavailable(
                f"decrement failed for scope={self._scope!r} key={key!r}"
            ) from exc

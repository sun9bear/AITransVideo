"""APF2c-3 anonymous preview rate-limit counter wrapper.

This module is the deliberately minimal landing of the in-memory /
fake rate-limit counter store described in the task instruction at
``docs/ai-workgroup/working/Claude-Code/2026-06-02T224214_from-CodeX_to-Claude-Code_type-instruction_task-APF2c-3-rate-limit-counter-wrapper.md``
and constrained by the phase envelope.

Boundary (intentionally narrow):

* only the standard library is imported;
* no filesystem I/O of any kind;
* no network, no shell, no DB, no key/value store, no third-party
  provider calls, no pricing / payment / points logic;
* no production secret is read;
* no endpoint, gateway route, or worker hook is registered.

What it provides:

* :class:`RateLimitCounterUnavailable` — a readable exception used by
  fail-closed counter stores so the
  :class:`src.services.anonymous_preview_backend_adapter.AnonymousPreviewBackendAdapter`
  can translate any failure into ``PreviewStatus.FAILED`` without a
  silent fallback;
* :class:`InMemoryRateLimitCounterStore` — a process-local counter
  store whose ``get`` returns ``0`` for missing keys, whose
  ``increment`` adds ``1`` per call, and whose keys are isolated from
  each other; an optional ``snapshot()`` is exposed for
  diagnostics/tests only and is not part of the adapter's structural
  protocol;
* :class:`UnavailableRateLimitCounterStore` — a counter store that
  raises :class:`RateLimitCounterUnavailable` on every ``get`` /
  ``increment`` call so the adapter fail-closed branch can be exercised
  without standing up a real backing store.

Both store classes satisfy the structural ``CounterStore`` protocol
declared in
``src.services.anonymous_preview_backend_adapter`` by virtue of having
``get(key: str) -> int`` and ``increment(key: str) -> int`` methods;
this module deliberately does not import the adapter module to keep
the dependency direction pointing the other way.
"""

from __future__ import annotations

import threading
from typing import Mapping, Tuple


__all__ = [
    "RateLimitCounterUnavailable",
    "InMemoryRateLimitCounterStore",
    "UnavailableRateLimitCounterStore",
]


class RateLimitCounterUnavailable(Exception):
    """Raised by a counter store when it cannot serve a request.

    The adapter treats any exception from ``get`` / ``increment`` as a
    fail-closed signal, but using this dedicated subclass makes the
    intent explicit at call sites and in test assertions.
    """


def _require_key(key: object) -> str:
    if not isinstance(key, str):
        raise RateLimitCounterUnavailable(
            f"counter key must be a non-empty str, got {type(key).__name__}"
        )
    if not key:
        raise RateLimitCounterUnavailable("counter key must be a non-empty str")
    return key


class InMemoryRateLimitCounterStore:
    """Process-local fake counter store.

    Missing keys return ``0``. Each ``increment(key)`` adds ``1`` to
    that key's count and returns the new value. Keys are independent
    of each other.

    The store is intentionally process-local: it does not persist to
    disk, does not coordinate across processes, and does not enforce
    expiry. Production rate limiting must be backed by a real shared
    store (Redis / DB) wired in a later phase; this class only exists
    so the backend adapter and its tests can exercise the rate-limit
    decision branch without standing up that infrastructure.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> int:
        valid_key = _require_key(key)
        with self._lock:
            return int(self._counts.get(valid_key, 0))

    def increment(self, key: str) -> int:
        valid_key = _require_key(key)
        with self._lock:
            new_value = int(self._counts.get(valid_key, 0)) + 1
            self._counts[valid_key] = new_value
            return new_value

    def try_acquire(self, key: str, cap: int) -> Tuple[bool, int]:
        """Atomically check-and-increment ``key`` against ``cap``.

        Holds the per-store lock around the read and the write so two
        concurrent callers cannot both observe a value below the cap and
        both increment past it. Returns ``(True, new_value)`` when the
        caller is admitted, ``(False, current)`` when the current count
        is already at or above the cap (no increment performed).
        """

        valid_key = _require_key(key)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise RateLimitCounterUnavailable(
                f"cap must be a non-negative int, got {cap!r}"
            )
        with self._lock:
            current = int(self._counts.get(valid_key, 0))
            if current >= cap:
                return (False, current)
            new_value = current + 1
            self._counts[valid_key] = new_value
            return (True, new_value)

    def decrement(self, key: str) -> int:
        """Best-effort rollback of a prior admission.

        Floors at zero so a stray rollback can never produce a negative
        counter. Used by the adapter's multi-key rollback path; callers
        outside that path normally have no reason to invoke this.
        """

        valid_key = _require_key(key)
        with self._lock:
            current = int(self._counts.get(valid_key, 0))
            new_value = current - 1 if current > 0 else 0
            self._counts[valid_key] = new_value
            return new_value

    def snapshot(self) -> Mapping[str, int]:
        """Return a frozen copy of the current counts.

        Diagnostics/tests only — the adapter never calls this method
        because it is not part of the structural ``CounterStore``
        protocol. Returning a fresh ``dict`` avoids leaking the
        internal mutable state.
        """

        with self._lock:
            return dict(self._counts)


class UnavailableRateLimitCounterStore:
    """Counter store that always reports unavailable.

    Useful for exercising the adapter's fail-closed branch in tests
    without constructing a real backing store that can be coerced into
    failure. The optional ``reason`` lets callers attach a
    human-readable explanation surfaced via the exception message.
    """

    def __init__(self, reason: str = "rate-limit counter store unavailable") -> None:
        self._reason = reason or "rate-limit counter store unavailable"

    def get(self, key: str) -> int:
        raise RateLimitCounterUnavailable(self._reason)

    def increment(self, key: str) -> int:
        raise RateLimitCounterUnavailable(self._reason)

    def try_acquire(self, key: str, cap: int) -> Tuple[bool, int]:
        raise RateLimitCounterUnavailable(self._reason)

    def decrement(self, key: str) -> int:
        raise RateLimitCounterUnavailable(self._reason)

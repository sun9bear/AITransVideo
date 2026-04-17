"""Cross-platform file lock with per-thread reentrancy.

Purpose: protect read-modify-write sequences on shared JSON registries
(e.g. ``voice_registry.json``) against concurrent writers.

Layering:
  - ``threading.RLock``: intra-process reentrancy. If method A holds the
    lock and calls method B which also wraps in ``file_lock``, B's
    acquire is a cheap re-entry instead of a deadlock.
  - ``fcntl.flock`` (POSIX) / ``msvcrt.locking`` (Windows): inter-process
    protection against a second writer (e.g. a CLI script running against
    the same registry while the pipeline is also running).

Thread-local flag ``_lock_depth`` tracks re-entry so we only take the
OS-level lock on the outermost call — that sidesteps the Windows
``LK_LOCK`` limitation where the same handle self-deadlocks.

Atomicity of the file *contents* is already handled elsewhere
(``voice_registry.save()`` uses ``NamedTemporaryFile`` + ``os.replace``).
This module is about serializing the **load → modify → save** sequence
so two writers don't read the same state, diverge, and last-write-wins.

Not needed today for voice_registry.json because the pipeline is single-
threaded and Gateway admin writes are rare, but a cheap guard against
future scenarios where write frequency grows (e.g. bulk import) or
multi-process runs get added (migration scripts + live pipeline).
"""
from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Per-path intra-process reentrant lock. Dict of path → RLock.
_path_locks: dict[str, threading.RLock] = {}
_path_locks_guard = threading.Lock()

# Per-thread re-entry depth counter (keyed by lock path). Used so only the
# outermost acquire takes the OS-level file lock.
_reentry = threading.local()


def _get_rlock(lock_key: str) -> threading.RLock:
    """Return (or lazily create) the singleton RLock for a given lock path."""
    with _path_locks_guard:
        lock = _path_locks.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[lock_key] = lock
        return lock


def _increment_depth(lock_key: str) -> int:
    depths = getattr(_reentry, "depths", None)
    if depths is None:
        depths = {}
        _reentry.depths = depths
    depths[lock_key] = depths.get(lock_key, 0) + 1
    return depths[lock_key]


def _decrement_depth(lock_key: str) -> int:
    depths = getattr(_reentry, "depths", None) or {}
    current = depths.get(lock_key, 0) - 1
    if current <= 0:
        depths.pop(lock_key, None)
    else:
        depths[lock_key] = current
    return current


if sys.platform == "win32":
    import msvcrt

    def _os_lock(fd: int) -> None:
        # 1-byte exclusive advisory lock on the sidecar file. LK_LOCK blocks
        # up to ~10 attempts; we don't retry here because the per-thread
        # depth counter guarantees we never self-contend.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _os_unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive, re-entrant lock covering ``path``.

    Implementation: always takes the process-local ``threading.RLock``;
    takes the OS-level file lock only on the outermost entry (per thread
    per lock_key) to avoid the Windows self-deadlock pitfall with
    ``msvcrt.locking``.

    The lock is keyed by the **resolved absolute path** of the target file
    — two calls with equivalent paths (e.g. ``./foo`` vs ``/abs/foo``)
    share the same RLock.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_key = str(lock_path.resolve())
    rlock = _get_rlock(lock_key)
    depth = 0
    fd: int | None = None

    rlock.acquire()
    try:
        depth = _increment_depth(lock_key)
        if depth == 1:
            # Outermost entry on this thread — take the OS-level file lock.
            # touch(exist_ok=True) is idempotent; races here are harmless
            # because only one thread/process can advance past msvcrt/fcntl.
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.touch(exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                _os_lock(fd)
            except Exception:
                os.close(fd)
                fd = None
                raise
        yield
    finally:
        if depth == 1 and fd is not None:
            try:
                _os_unlock(fd)
            finally:
                os.close(fd)
        _decrement_depth(lock_key)
        rlock.release()

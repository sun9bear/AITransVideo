"""Stable advisory lock key derivation for pan executors.

PostgreSQL's pg_advisory_lock takes a signed bigint key. For the lock to
work as designed — same key across processes, restarts, reaper, and
residue_cleanup — the derivation MUST be deterministic.

Python's builtin hash() is NOT deterministic: PYTHONHASHSEED is
randomized in Python 3.3+ (per process). Two Gateway workers handling
the same (user_id, job_id) would compute different hash() values and
acquire DIFFERENT advisory locks — i.e. the "lock" wouldn't serialize
them at all. This bug bit a previous iteration of the code (CodeX
2026-05-18 P0-1).

Fix: sha256 the canonical "user_id:job_id" string, take the first 8
bytes, reinterpret as a signed 64-bit big-endian integer. Deterministic
across processes / restarts / Python versions.

All pan executors (backup, restore, residue_cleanup) MUST go through
this helper to derive their advisory lock keys.
"""
from __future__ import annotations

import hashlib
import struct
import uuid


def pan_lock_key(user_id: uuid.UUID, job_id: str) -> int:
    """Derive a stable signed int64 lock key for the (user_id, job_id) pair.

    PostgreSQL's pg_advisory_lock(bigint) accepts a signed 8-byte integer.
    We sha256 the canonical "user_id:job_id" string and reinterpret the
    first 8 bytes as a signed big-endian int64.

    Properties:
      - Deterministic: same input → same output across processes and
        Python versions (PYTHONHASHSEED is NOT involved).
      - Well-distributed: sha256 ensures collision probability is
        negligible for any realistic (user_id, job_id) population.
      - Fits in PG bigint: struct '>q' produces a value in
        [-2^63, 2^63 - 1].
    """
    canonical = f"{user_id}:{job_id}".encode('utf-8')
    digest = hashlib.sha256(canonical).digest()
    return struct.unpack('>q', digest[:8])[0]

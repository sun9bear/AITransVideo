"""Contract test: pan.* event vocabulary must stay in sync across the
three modules that own the allow-lists (plan 2026-05-14 §Phase 9 T9.6).

Three independent allow-lists exist for the same vocabulary:

  1. ``src/services/jobs/events.py::SUPPORTED_EVENT_TYPES``
     The canonical source. ``JobEvent.__post_init__`` rejects anything
     not in this set.

  2. ``gateway/storage/event_log.py::_DOWNLOAD_EVENT_TYPES`` (historical
     name retained per plan §3.2 for git-blame continuity)
     The gateway's hand-rolled JSONL writer's allow-list. Anything
     emitted from gateway must be in this set or a WARNING is logged.

  3. ``scripts/r2_observability.py::PAN_EVENT_TYPES``
     The CLI dashboard's inlined vocabulary — covered separately by
     ``tests/test_r2_observability.py::test_script_event_vocab_in_sync_with_jobs_events``.

Why three copies?
-----------------
- (1) lives in ``src/services/`` which the app container imports but
  the gateway container can't (pydub etc. — see CLAUDE.md "Phase 2
  下载后端").
- (2) is in the gateway image, written by hand so emit_download_event
  doesn't need to import from services.jobs.
- (3) is the CLI script, pure stdlib so it can run in either container.

Drift between (1) and (2) creates two failure modes:
  - Type in (1) but not (2): gateway emits → emit_download_event logs
    WARNING and writes anyway (fail-open). Audit data lives but
    surfaces drift signal.
  - Type in (2) but not (1): JobEvent loaded from JSONL by the Job API
    will raise ValueError("Unsupported event_type") in __post_init__.

This test locks the pan-slice of (1) and (2) so neither can drift
without the other.

For the full SUPPORTED_EVENT_TYPES ↔ scripts/r2_observability.py
cross-check (covers download/stream/pan all three prefixes) see
``tests/test_r2_observability.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- sys.path bootstrap for bare imports (mirrors tests/pan_fixtures.py).
# gateway/ uses bare imports because its Dockerfile sets WORKDIR /opt/gateway,
# so production never imports via the ``gateway.`` package prefix.

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


def test_pan_slice_aligned_between_jobs_events_and_event_log() -> None:
    """SUPPORTED_EVENT_TYPES ∩ pan.* == _DOWNLOAD_EVENT_TYPES ∩ pan.*.

    Both allow-lists must include the exact same pan.* members.
    """
    from services.jobs.events import SUPPORTED_EVENT_TYPES
    from storage.event_log import _DOWNLOAD_EVENT_TYPES

    upstream_pan = frozenset(
        t for t in SUPPORTED_EVENT_TYPES if t.startswith("pan.")
    )
    gateway_pan = frozenset(
        t for t in _DOWNLOAD_EVENT_TYPES if t.startswith("pan.")
    )

    missing = upstream_pan - gateway_pan
    extra = gateway_pan - upstream_pan

    assert not missing, (
        f"gateway/storage/event_log.py::_DOWNLOAD_EVENT_TYPES is missing "
        f"pan.* types known to services.jobs.events.SUPPORTED_EVENT_TYPES: "
        f"{sorted(missing)}. Without these the gateway's emit_download_event "
        f"logs WARNING for every emission (still writes — fail-open — but "
        f"the warning is the drift signal)."
    )
    assert not extra, (
        f"gateway/storage/event_log.py::_DOWNLOAD_EVENT_TYPES has unknown "
        f"pan.* types not in services.jobs.events.SUPPORTED_EVENT_TYPES: "
        f"{sorted(extra)}. The Job API will raise ValueError "
        f"'Unsupported event_type' when JobStore.load_events tries to "
        f"materialize these as JobEvent instances."
    )


def test_pan_slice_has_at_least_one_member() -> None:
    """Sanity check: the pan vocabulary is non-empty in both modules.

    If both sets are empty the previous test passes vacuously — this
    test ensures Phase 9 T9.1 + T9.2 actually shipped the constants.
    """
    from services.jobs.events import SUPPORTED_EVENT_TYPES
    from storage.event_log import _DOWNLOAD_EVENT_TYPES

    upstream_pan = frozenset(
        t for t in SUPPORTED_EVENT_TYPES if t.startswith("pan.")
    )
    gateway_pan = frozenset(
        t for t in _DOWNLOAD_EVENT_TYPES if t.startswith("pan.")
    )

    assert upstream_pan, (
        "services.jobs.events.SUPPORTED_EVENT_TYPES has no pan.* members "
        "— Phase 9 T9.1 not done?"
    )
    assert gateway_pan, (
        "gateway/storage/event_log.py::_DOWNLOAD_EVENT_TYPES has no pan.* "
        "members — Phase 9 T9.2 not done?"
    )


def test_pan_failure_recipes_keyed_to_events_in_sync() -> None:
    """The two notification recipes for pan.backup.failed and
    pan.restore.failed (Phase 9 T9.3) must use event_type strings that
    SUPPORTED_EVENT_TYPES accepts. Otherwise the dispatcher silently
    drops the event (CodeX 2026-05-18 P1-2 — same class of bug as the
    pre-fix pan_credentials_revoked dispatch).
    """
    from services.jobs.events import SUPPORTED_EVENT_TYPES
    from notification_dispatch_map import (
        DISPATCH_MAP,
        EVENT_PAN_BACKUP_FAILED,
        EVENT_PAN_RESTORE_FAILED,
        EVENT_PAN_TOKEN_REVOKED,
    )

    assert EVENT_PAN_TOKEN_REVOKED in SUPPORTED_EVENT_TYPES, (
        "pan.token_revoked recipe references an event_type not in "
        "SUPPORTED_EVENT_TYPES — dispatcher would drop the event."
    )
    assert EVENT_PAN_BACKUP_FAILED in SUPPORTED_EVENT_TYPES, (
        "pan.backup.failed recipe references an event_type not in "
        "SUPPORTED_EVENT_TYPES."
    )
    assert EVENT_PAN_RESTORE_FAILED in SUPPORTED_EVENT_TYPES, (
        "pan.restore.failed recipe references an event_type not in "
        "SUPPORTED_EVENT_TYPES."
    )

    # Recipe lookups must succeed for all three.
    assert DISPATCH_MAP.get(EVENT_PAN_TOKEN_REVOKED) is not None
    assert DISPATCH_MAP.get(EVENT_PAN_BACKUP_FAILED) is not None
    assert DISPATCH_MAP.get(EVENT_PAN_RESTORE_FAILED) is not None

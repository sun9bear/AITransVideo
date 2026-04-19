"""Job API runtime wiring helpers.

The Job API has two entry points that both build a ``JobService``:

- ``main.run_job_api_command`` — developer / single-binary path,
  invoked as ``python main.py job-api`` locally.
- ``scripts/run_remote_workbench_service.py _run_job_api`` —
  production container path, invoked from ``linux_app_service.sh``.

Any "inject X callback onto the service before the HTTP server starts"
logic has to live in BOTH entry points, otherwise the container (which
uses the scripts/ path) silently runs with whatever default the service
shipped with — e.g. ``editing_idle_scanner`` never cancels real jobs, or
``regenerate_segment_tts`` returns 501 forever.

This module centralises every such post-build wiring step into one
function that both entry points call. Adding a new injection later is
now a one-line change in this file plus a contract test in
``test_phase1_guards.py`` to ensure both entry points still invoke it.

All wiring steps are individually try/except-guarded: a broken step must
not prevent the Job API from starting — the failure is logged via
``print("[warn] ...")`` (same pattern the entry points used before this
helper existed) and the remaining steps still run.
"""

from __future__ import annotations

from typing import Any

__all__ = ["apply_runtime_wiring"]


def apply_runtime_wiring(service: Any) -> None:
    """Run every post-build wiring step required before the Job API
    starts serving requests.

    Call from BOTH ``main.run_job_api_command`` and
    ``scripts/run_remote_workbench_service.py._run_job_api`` right after
    ``service`` is built and before ``build_job_api_server`` is called.
    Each step is self-guarded so failures are logged but do not block
    the service from coming up.

    Steps:

    1. ``editing_idle_scanner.inject_editing_cancel_callback`` — T1-10.
       Without this, ``cleanup.py`` still calls the module-level
       ``_noop_cancel`` and idle editing jobs are detected but never
       actually cancelled.
    2. ``segment_regenerate.build_real_segment_tts_caller`` + assign to
       ``service._segment_tts_caller`` — A.2. Without this,
       ``regenerate_segment_tts`` falls through to ``_not_wired_tts_caller``
       and returns HTTP 501 on every user click.
    3. ``cleanup.start_cleanup_thread`` — background TTL / idle-scan loop.
       Without this, TTL-expired jobs are not purged and editing idle
       cancel never fires.
    """
    try:
        from services.web_ui.editing_idle_scanner import (
            inject_editing_cancel_callback,
        )

        inject_editing_cancel_callback(service)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[warn] failed to wire editing idle-cancel callback: {exc}")

    try:
        from services.tts.segment_regenerate import (
            build_real_segment_tts_caller,
        )

        service._segment_tts_caller = build_real_segment_tts_caller()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[warn] failed to wire segment TTS caller: {exc}")

    try:
        from services.web_ui.cleanup import start_cleanup_thread

        start_cleanup_thread()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[warn] failed to start cleanup thread: {exc}")

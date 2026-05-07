"""P0-6 (audit 2026-05-07) regression: _cleanup_loop's late import of
editing_idle_scanner must use the production-correct module path. Wrong
prefix (src.services...) silently kills the daemon thread before any
24h-idle editing job is auto-cancelled.

Two guards:

1. ``test_cleanup_loop_late_import_does_not_raise`` — forces a single
   iteration of ``_cleanup_loop`` and verifies the late import resolves
   under the production PYTHONPATH layout (``.../src`` is on sys.path,
   not the project root).
2. ``test_cleanup_module_does_not_use_src_prefix_imports`` — static
   guard against any ``from src.services...`` / ``import src.services``
   appearing in the file again. Cheaper than the runtime test, catches
   regressions even if someone moves the bad import to a top-level place.
"""
from __future__ import annotations

import pytest


class _Stop(BaseException):
    """Sentinel raised by patched time.sleep to interrupt the loop after
    the late import has executed.

    Inherits from ``BaseException`` (not ``Exception``) on purpose:
    ``_cleanup_loop`` wraps each iteration in ``try / except Exception:``
    that logs and continues, so an ``Exception`` subclass would be
    swallowed and the loop would spin forever. ``BaseException`` escapes
    the bare-except and bubbles up to pytest. ``ImportError`` (the actual
    failure mode this test guards against) is a subclass of ``Exception``
    and *would* be caught — except it's raised at the top of the function
    body, *before* the ``while True`` loop, so it bypasses the bare-except
    entirely and surfaces as a real test failure."""


def test_cleanup_loop_late_import_does_not_raise(monkeypatch):
    """Force a single iteration of _cleanup_loop; verify the late import
    of editing_idle_scanner resolves under production PYTHONPATH layout.

    Strategy: patch ``time.sleep`` (which is the very first call in the
    loop body, before any actual cleanup work) to raise ``_Stop``. The
    late ``from services.web_ui import editing_idle_scanner`` runs *before*
    the ``while True`` loop, so by the time sleep raises we've already
    proven the import succeeds. Any ImportError on the bad ``src.``
    prefix would surface here as ImportError, not _Stop.
    """
    import time

    def stop_sleep(_seconds):
        raise _Stop("loop interrupted by test")

    monkeypatch.setattr(time, "sleep", stop_sleep)

    from services.web_ui import cleanup  # noqa: F401

    with pytest.raises(_Stop):
        cleanup._cleanup_loop()


def test_cleanup_module_does_not_use_src_prefix_imports():
    """Static guard: any 'from src.services...' or 'import src.services'
    in cleanup.py would fail in production where PYTHONPATH=.../src ."""
    import inspect

    from services.web_ui import cleanup

    src = inspect.getsource(cleanup)
    assert "from src.services" not in src, (
        "cleanup.py imports under 'src.' prefix; production PYTHONPATH "
        "is .../src so the prefix breaks daemon thread startup."
    )
    assert "import src.services" not in src, (
        "cleanup.py uses 'import src.services...' form; production "
        "PYTHONPATH is .../src so the prefix breaks daemon thread startup."
    )

"""Minimal file-based tracing for DashScope hang diagnosis.

All output goes to /tmp/aivt_exec_trace_<pid>.log and
/tmp/aivt_import_trace_<pid>.log.  Nothing touches stdout/stderr.
"""

from __future__ import annotations

import os
import sys
import threading
import time

_PID = os.getpid()
_EXEC_LOG = f"/tmp/aivt_exec_trace_{_PID}.log"
_IMPORT_LOG = f"/tmp/aivt_import_trace_{_PID}.log"


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def exec_trace(label: str) -> None:
    """Append one line to the execution trace log."""
    line = f"{_ts()} pid={_PID} thread={threading.current_thread().name} {label}\n"
    try:
        with open(_EXEC_LOG, "a") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def dump_dashscope_modules(label: str) -> None:
    """Write all dashscope/websocket modules currently in sys.modules."""
    try:
        with open(_IMPORT_LOG, "a") as f:
            f.write(f"\n--- {_ts()} {label} ---\n")
            for name, mod in sorted(sys.modules.items()):
                if "dashscope" in name or "websocket" in name.lower():
                    fpath = getattr(mod, "__file__", "?")
                    f.write(f"  {name} -> {fpath}\n")
            f.flush()
    except Exception:
        pass


def dump_threads(label: str) -> None:
    """Write all live threads to the execution trace log."""
    try:
        with open(_EXEC_LOG, "a") as f:
            f.write(f"\n--- {_ts()} {label}: thread dump ---\n")
            for t in threading.enumerate():
                f.write(
                    f"  name={t.name} daemon={t.daemon} alive={t.is_alive()} "
                    f"ident={t.ident} class={type(t).__name__}\n"
                )
            non_daemon = [
                t for t in threading.enumerate()
                if t.is_alive() and not t.daemon and t is not threading.main_thread()
            ]
            f.write(f"  non-daemon blockers: {len(non_daemon)}\n")
            f.flush()
    except Exception:
        pass

"""Gateway-local loader for ``src/services/jobs/logs_redactor.py``.

Why this indirection
====================

The natural Python import::

    from services.jobs.logs_redactor import build_default_redactor

does NOT work in the gateway container, because Python eagerly executes
``services/jobs/__init__.py`` before reaching ``logs_redactor``. That
``__init__.py`` does ``from services.jobs.api import ...`` and
``from services.jobs.events import JobEvent``, both of which transitively
import :mod:`pydub`. The gateway container is intentionally minimal and does
not ship pydub (only the worker / app container does — see
``services.jobs.events`` audio-slicing usage). So the package-style import
raises :class:`ImportError`, and any caller that wraps it in
``try/except Exception`` ends up silently fail-open: the redactor is never
built, the message is returned verbatim, and non-admin users see provider
names / file paths / UUIDs that should have been stripped.

Same trap as ``gateway/storage/event_log.py``: the project rule (CLAUDE.md
under "Phase 2 下载后端") is "gateway 侧不 import services.jobs.events;
event_log.py 抽出纯 stdlib 实现". This module follows the same pattern for
``logs_redactor``, but goes one step further: instead of duplicating the
redaction logic, it loads the file directly via
:func:`importlib.util.spec_from_file_location`, which bypasses the package
``__init__`` entirely. ``logs_redactor.py`` itself only needs ``re`` +
``typing`` (pure stdlib), plus a try/except'd lookup of
``services.llm_registry`` that gracefully falls back to brand names if the
registry isn't reachable — both safe in a minimal container.

Consequence: there is exactly **one** source of truth for the redaction
contract (``src/services/jobs/logs_redactor.py``), and gateway uses it
without paying the package-init cost.

Robustness
==========

- The first call locates the file across two candidate paths (local dev:
  ``<repo>/src/services/jobs/logs_redactor.py``; container:
  ``/opt/aivideotrans/app/src/services/jobs/logs_redactor.py``), loads the
  module, and caches the ``build_default_redactor`` callable in a module-
  level slot.
- Subsequent calls reuse the cached callable. Each call still produces a
  fresh :class:`Redactor` instance (the upstream contract — instances are
  immutable and reuse-safe, but we keep the per-call build cheap so callers
  who want a list-wide shared instance build it once explicitly).
- If the file genuinely cannot be located or fails to load, this module
  logs once at WARNING and subsequent calls return ``None`` without
  re-trying (avoids per-request log spam). Callers MUST handle ``None`` as
  fail-open: return the message unchanged. Refusing to serve content just
  because redaction failed would be a worse UX than leaking provider names.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["build_default_redactor"]


_CANDIDATE_PATHS: tuple[Path, ...] = (
    # Local dev: gateway/log_redactor_loader.py → repo root → src/...
    Path(__file__).resolve().parent.parent / "src" / "services" / "jobs" / "logs_redactor.py",
    # Production container: gateway lives at /opt/gateway, src at
    # /opt/aivideotrans/app/src (different mount point).
    Path("/opt/aivideotrans/app/src/services/jobs/logs_redactor.py"),
)


# Cached callable; ``None`` means "not yet attempted".
_cached_builder: Optional[Callable[[], Any]] = None
# Sticky failure flag — once we fail, we don't retry on every request.
_load_failed: bool = False


def _locate_source_file() -> Optional[Path]:
    for path in _CANDIDATE_PATHS:
        if path.is_file():
            return path
    return None


def _load_module() -> Optional[Any]:
    src = _locate_source_file()
    if src is None:
        logger.warning(
            "log_redactor_loader: logs_redactor.py not found in any candidate "
            "path %s; redaction will fail-open (messages returned unchanged)",
            [str(p) for p in _CANDIDATE_PATHS],
        )
        return None
    spec = importlib.util.spec_from_file_location(
        "_avt_gateway_logs_redactor_inline", src
    )
    if spec is None or spec.loader is None:
        logger.warning(
            "log_redactor_loader: spec_from_file_location returned None for %s",
            src,
        )
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_default_redactor() -> Optional[Any]:
    """Return a fresh :class:`Redactor` instance, or ``None`` on failure.

    Caller contract: ``None`` means "redactor unavailable, fail-open" —
    return the message verbatim. Never raise from this function; the
    redaction layer is best-effort.
    """
    global _cached_builder, _load_failed

    if _cached_builder is None and not _load_failed:
        try:
            mod = _load_module()
            if mod is None:
                _load_failed = True
            else:
                builder = getattr(mod, "build_default_redactor", None)
                if not callable(builder):
                    logger.warning(
                        "log_redactor_loader: loaded module has no callable "
                        "build_default_redactor attribute"
                    )
                    _load_failed = True
                else:
                    _cached_builder = builder
        except Exception:
            logger.exception(
                "log_redactor_loader: failed to load logs_redactor.py via "
                "spec_from_file_location"
            )
            _load_failed = True

    if _cached_builder is None:
        return None

    try:
        return _cached_builder()
    except Exception:
        logger.exception(
            "log_redactor_loader: build_default_redactor() call raised; "
            "returning None for fail-open behaviour"
        )
        return None

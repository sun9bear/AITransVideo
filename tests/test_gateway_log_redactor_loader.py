"""Regression guard for the gateway-local log_redactor_loader (D25 / §10.4).

Why this exists
===============

The natural Python import ``from services.jobs.logs_redactor import
build_default_redactor`` does NOT work in the gateway container, because
``services/jobs/__init__.py`` eagerly imports
``services.jobs.api`` / ``services.jobs.events`` / ``services.jobs.process_runner``,
all of which transitively pull :mod:`pydub`. The gateway image is intentionally
minimal and does not ship pydub, so the package-style import raises
:class:`ImportError`. Wrapping the import in ``try/except Exception`` (as
``_serve_redacted_logs`` and ``_redact_job_record_in_place`` originally did)
results in silent fail-open: the redactor is never built, the message is
returned verbatim, and non-admin users see the provider names / file paths /
UUIDs that should have been stripped.

The fix is ``gateway/log_redactor_loader.py``, which loads the file directly
via :func:`importlib.util.spec_from_file_location`, bypassing the package
``__init__`` and pydub entirely.

These tests pin the contract:

1. ``build_default_redactor()`` returns a working :class:`Redactor` (not None)
   even if ``services.jobs`` package import is broken / unavailable.
2. ``job_intercept.py`` does NOT import ``services.jobs.logs_redactor`` at any
   level — the loader is the only allowed entry point. (AST guard.)
3. The loader caches the builder callable so the file isn't re-loaded on every
   request (perf — list endpoints with N rows would otherwise pay N ×
   spec_from_file_location).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_GATEWAY_DIR = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))


@pytest.fixture(autouse=True)
def _reset_loader_state():
    """Clear the loader's module-level cache before AND after every test.

    The loader caches the builder and a sticky ``_load_failed`` flag (so
    repeated requests in production don't keep re-loading after a permanent
    failure). In tests the sticky flag would leak across tests — e.g.
    ``test_loader_returns_none_when_source_missing`` sets
    ``_load_failed = True``, which then makes every subsequent test in the
    process see ``None`` and silently fail-open. Auto-reset on entry/exit
    eliminates that cross-talk."""
    import log_redactor_loader  # type: ignore[import-not-found]

    log_redactor_loader._cached_builder = None
    log_redactor_loader._load_failed = False
    yield
    log_redactor_loader._cached_builder = None
    log_redactor_loader._load_failed = False


# ---------------------------------------------------------------------------
# Functional: loader actually builds a working redactor
# ---------------------------------------------------------------------------


def test_loader_returns_working_redactor() -> None:
    import log_redactor_loader  # type: ignore[import-not-found]

    redactor = log_redactor_loader.build_default_redactor()
    assert redactor is not None, (
        "loader returned None — file location must resolve in dev env"
    )

    redacted = redactor.redact(
        "Gemini failed at job_id=abc 504 https://generativelanguage.googleapis.com"
    )
    # Provider name is the most-important leak vector and must always be gone.
    assert "Gemini" not in redacted, redacted
    # job_id label should also be stripped (UUID + label regex).
    assert "job_id=abc" not in redacted, redacted


def test_loader_caches_builder_across_calls() -> None:
    """Once located, the builder callable is reused — no per-call file IO."""
    import log_redactor_loader  # type: ignore[import-not-found]

    # Warm cache.
    first = log_redactor_loader.build_default_redactor()
    assert first is not None
    cached_builder = log_redactor_loader._cached_builder
    assert cached_builder is not None, "first call should populate cache"

    # Second call must reuse the cached callable (identity check on slot).
    second = log_redactor_loader.build_default_redactor()
    assert second is not None
    assert log_redactor_loader._cached_builder is cached_builder, (
        "cached builder slot must not be re-assigned on the warm path"
    )


def test_loader_returns_none_when_source_missing(monkeypatch) -> None:
    """If the file truly cannot be located, the loader fails-open (returns
    None) rather than raising — callers must handle None as 'no redaction'."""
    import log_redactor_loader  # type: ignore[import-not-found]

    # Point all candidate paths at non-existent files.
    monkeypatch.setattr(
        log_redactor_loader,
        "_CANDIDATE_PATHS",
        (Path("/nonexistent/logs_redactor.py"),),
    )

    result = log_redactor_loader.build_default_redactor()
    assert result is None, "missing source must fail-open with None"
    # Sticky-failure flag should be set so a second call doesn't re-attempt.
    assert log_redactor_loader._load_failed is True


# ---------------------------------------------------------------------------
# AST guard: nobody in gateway/ imports services.jobs.logs_redactor directly
# ---------------------------------------------------------------------------


def _collect_bad_imports(py_path: Path) -> list[str]:
    """Scan a Python file's AST for any ``from services.jobs.logs_redactor
    import ...`` or ``import services.jobs.logs_redactor`` statement.

    Returns a list of human-readable offenders (``"line N: from ..."``) so
    the assertion message points exactly at the regression.
    """
    src = py_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # pragma: no cover — gateway/ is always valid Py
        pytest.fail(f"Cannot parse {py_path}: {exc}")

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "services.jobs.logs_redactor" or mod.startswith(
                "services.jobs.logs_redactor."
            ):
                offenders.append(f"line {node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "services.jobs.logs_redactor" or alias.name.startswith(
                    "services.jobs.logs_redactor."
                ):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    return offenders


def test_gateway_files_do_not_import_services_jobs_logs_redactor_directly() -> None:
    """Architecture invariant: in the gateway container, the only legitimate
    way to reach ``logs_redactor.build_default_redactor`` is via
    ``log_redactor_loader``. Direct package imports trigger
    ``services.jobs.__init__`` → pydub → ImportError → silent fail-open.

    The loader file itself is exempt — it loads the source via
    :func:`importlib.util.spec_from_file_location`, which does NOT count as a
    Python-level import of the package and is what we want.
    """
    gateway_dir = Path(__file__).resolve().parents[1] / "gateway"
    assert gateway_dir.is_dir()

    offenders: dict[str, list[str]] = {}
    for py_file in gateway_dir.rglob("*.py"):
        # The loader module deliberately does NOT use a package-level import
        # (it uses spec_from_file_location). The AST scan would still pass
        # for it, but exclude defensively in case future docstrings mention
        # the bad import string in a way that confuses lint heuristics.
        if py_file.name == "log_redactor_loader.py":
            continue
        bad = _collect_bad_imports(py_file)
        if bad:
            offenders[str(py_file.relative_to(gateway_dir))] = bad

    assert not offenders, (
        "gateway/ must not import services.jobs.logs_redactor directly — "
        "use log_redactor_loader instead. Offenders:\n"
        + "\n".join(
            f"  {f}:\n    " + "\n    ".join(lines)
            for f, lines in offenders.items()
        )
    )

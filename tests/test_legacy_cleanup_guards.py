"""Contract-level regression guards for the 2026-04-17 legacy cleanup.

v1 of these guards used string-grep assertions (ban the literal "web-ui"
from main.py, etc.). Those fire on comments, deprecation messages, help
text — high noise, low signal. v2 replaces them with contract-level
assertions that test observable behavior and structural invariants:

  - `main.py --help` output must not advertise a retired subcommand
    (behavioral contract, not source-string match)
  - AST-level: no .py file imports a deleted module
  - File existence: deleted files must stay gone
  - Narrow business-scoped AST literal scans (not whole-file greps)
  - Caddy @internal_block structural presence (config-level guard)

Implementation notes:
  - Uses subprocess + sys.executable to run `main.py --help`, so tests
    work identically on Windows dev machines (Python from uv) and Linux
    CI. No dependency on GNU grep in PATH.
  - Uses ast.parse + ast.walk for import-graph and literal checks.
  - Skips vendored / build directories during whole-repo scans.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories to skip during whole-repo scans (vendored deps / build artifacts).
# `frontend/` is here as a second layer of protection: if someone accidentally
# recreates it, the dir-existence guard below catches it first, but the scan
# still won't descend into node_modules.
_SKIP_DIRS = {
    "node_modules",
    ".git",
    "build",
    ".venv",
    "venv",
    ".pytest_cache",
    "__pycache__",
    "frontend",  # deleted in T1.1
    "frontend-next",  # not scanned; has its own tooling
}


def _iter_py_files(root: Path):
    """Yield *.py files under ``root``, skipping vendored / build dirs."""
    if not root.exists():
        return
    for p in root.rglob("*.py"):
        rel_parts = p.relative_to(REPO).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        yield p


def _imports_of(py_path: Path) -> set[str]:
    """Return the set of fully-qualified module names this file imports.

    Uses ast — ignores comments, strings, and docstrings. Safer than grep
    because it won't fire on ``# TODO: import services.web_ui.server``.
    """
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # Capture "from X import Y" where Y might itself be a module
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


# ---------------------------------------------------------------------------
# Phase 1 structural invariants: deleted files/directories stay gone
# ---------------------------------------------------------------------------

def test_no_legacy_frontend_dir():
    """T1.1: the Vite frontend/ directory was replaced by frontend-next/ and
    must not be re-created in the repo root."""
    assert not (REPO / "frontend").exists(), \
        "Legacy Vite frontend/ directory was recreated — it was deleted in T1.1 and superseded by frontend-next/."


def test_no_tmp_local_video_repro_dir():
    """T1.2: local debug fixture directory."""
    assert not (REPO / "tmp_local_video_repro").exists(), \
        "tmp_local_video_repro/ came back — it was debug fixtures deleted in T1.2."


def test_no_root_projects_dir():
    """T1.4: the root projects/ directory was empty; real data lives at
    data/projects/. This guard prevents the empty root dir from being
    recreated (e.g. via an errant ``mkdir projects``)."""
    assert not (REPO / "projects").exists(), (
        "Root projects/ directory came back. Job data is at data/projects/; "
        "a stray root dir is confusing and was removed in T1.4."
    )


def test_no_build_dir():
    """T1.3: PyInstaller residue and historical deploy tars were moved out
    of build/ and the directory deleted. The .gitignore has it listed too,
    but a committed recurrence would slip past that."""
    assert not (REPO / "build").exists(), \
        "build/ directory came back — was cleared in T1.3 (deploy tars archived, PyInstaller residue removed)."


def test_no_web_ui_server_file():
    assert not (REPO / "src" / "services" / "web_ui" / "server.py").exists(), \
        "web_ui/server.py was retired in T1.6b (port 8876 Web UI fully downlined)."


def test_no_web_ui_handler_file():
    assert not (REPO / "src" / "services" / "web_ui" / "handler.py").exists(), \
        "web_ui/handler.py was retired in T1.6b (port 8876 Web UI fully downlined)."


# ---------------------------------------------------------------------------
# Contract: main.py --help must not advertise `web-ui`
# ---------------------------------------------------------------------------

def test_main_help_does_not_advertise_web_ui_subcommand():
    """T1.5 behavioral contract: invoking `main.py` must not list `web-ui`
    anywhere in its usage surface.

    Exit code is NOT asserted — main.py's custom argparse-free dispatcher
    returns 1 when called with an unknown command (including ``--help``),
    but still prints its full usage text. That's intentional legacy
    behavior. This guard is a CLI-surface assertion about the OUTPUT,
    not the exit code.

    The output must:
      - not be empty (usage text should appear)
      - not contain the retired ``web-ui`` subcommand anywhere
    """
    # Try both "--help" (some CLIs accept it) and no-args (always prints
    # usage). Whichever path actually produces usage is the one we check.
    for argv in ([sys.executable, str(REPO / "main.py"), "--help"],
                 [sys.executable, str(REPO / "main.py")]):
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, cwd=str(REPO),
        )
        combined_output = (result.stdout + result.stderr).lower()
        if "usage" in combined_output or "python main.py" in combined_output:
            # We got usage text; now assert web-ui is not advertised.
            assert "web-ui" not in combined_output, (
                "main.py usage output still advertises the retired web-ui "
                "subcommand (retired in T1.5). Check that the function, "
                "dispatch table entry, and usage line are all gone.\n\n"
                f"Full output:\n{result.stdout}\n{result.stderr}"
            )
            return
    # If neither invocation yielded usage text, something is deeply wrong.
    raise AssertionError(
        "Could not get usage text from main.py via --help or no-args. "
        "The CLI surface may be broken independent of this guard."
    )


# ---------------------------------------------------------------------------
# Contract: AST-level imports of deleted modules
# ---------------------------------------------------------------------------

_DELETED_IMPORT_TARGETS = {
    # Module paths removed in T1.6b. No .py file anywhere in the repo may
    # reference these; an import would fail at runtime.
    "services.web_ui.server",
    "services.web_ui.handler",
}


def test_no_imports_of_deleted_web_ui_modules():
    """T1.6b structural invariant: after the deletion, no .py file in src/,
    gateway/, or tests/ may import services.web_ui.server or .handler
    (or any submember thereof). AST-level — immune to comments/docstrings."""
    offenders: list[str] = []
    for scan_root in (REPO / "src", REPO / "gateway", REPO / "tests"):
        if not scan_root.exists():
            continue
        for py in _iter_py_files(scan_root):
            imports = _imports_of(py)
            for bad in _DELETED_IMPORT_TARGETS:
                if any(i == bad or i.startswith(bad + ".") for i in imports):
                    offenders.append(f"{py.relative_to(REPO)} imports {bad}")
    assert offenders == [], (
        "Deleted module(s) are still imported somewhere:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Contract: narrow AST literal scan for hardcoded Job API URL in gateway
# ---------------------------------------------------------------------------

# Only these files may legitimately contain the literal Job API URL. The list
# stays intentionally tiny; add to it only if a second legitimate site emerges
# and is reviewed.
_JOB_API_URL_ALLOWLIST_RELATIVE = {
    ("gateway", "config.py"),  # default value of settings.job_api_upstream
}

_JOB_API_URL_LITERALS = ("http://localhost:8877", "http://127.0.0.1:8877")


def test_gateway_business_modules_no_hardcoded_job_api_url():
    """T2.1 behavioral contract: no gateway *business* .py file may contain
    the Job API URL as a **string literal** (ast.Constant). Comments and
    docstrings (which the AST doesn't see as Constants unless they're
    assigned to a name) are fine.
    """
    gateway = REPO / "gateway"
    if not gateway.exists():
        return
    offenders: list[str] = []
    for py in _iter_py_files(gateway):
        rel = py.relative_to(REPO).parts
        # Allowlist: config.py defines the default; fine there.
        if rel in _JOB_API_URL_ALLOWLIST_RELATIVE:
            continue
        # Tests under gateway/tests or named test_* get skipped — guard is
        # for business modules only.
        if "test" in py.stem or any("tests" in p for p in rel):
            continue
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in _JOB_API_URL_LITERALS:
                    offenders.append(
                        f"{py.relative_to(REPO)}:{node.lineno}: {node.value}"
                    )
    assert offenders == [], (
        "Hardcoded Job API upstream URL reappeared in gateway business "
        "module(s). Use settings.job_api_upstream instead:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Contract: Caddy @internal_block must stay in place (T4 defense-in-depth)
# ---------------------------------------------------------------------------

def test_caddyfile_has_internal_block_rule():
    """Production defense-in-depth: Caddy must block /api/internal/* at
    the public edge so the internal API can never reach the open
    internet even if the gateway's X-Internal-Key check is misconfigured.

    Added by the prior migration-debt batch (T4); this guard keeps it.
    """
    caddy = REPO / "Caddyfile"
    if not caddy.exists():
        # Local dev checkouts without a Caddyfile are acceptable; the
        # guard is meant to fire on regressions in the committed file,
        # not on absence.
        return
    src = caddy.read_text(encoding="utf-8")
    assert "@internal_block" in src, (
        "Caddyfile lost its @internal_block matcher. /api/internal/* must "
        "stay gated at the Caddy layer even if the gateway's in-process "
        "X-Internal-Key validator is bypassed."
    )
    assert "/api/internal/*" in src, (
        "Caddyfile has @internal_block defined but no longer references "
        "/api/internal/*. That path is the whole point of the rule."
    )


# ---------------------------------------------------------------------------
# Cleanup summary
# ---------------------------------------------------------------------------
#
# This file is intentionally concise; each test is a single structural or
# behavioral contract. If you're considering adding a string-grep guard
# here, stop — that was v1's anti-pattern. Instead:
#
#   1. Identify the observable behavior the cleanup must preserve (e.g. a
#      CLI command disappears, a module can't be imported, a file doesn't
#      exist, a config rule is present).
#   2. Encode that behavior as a contract (subprocess, ast, Path.exists, etc).
#
# See docs/plans/2026-04-17-legacy-migration-cleanup.md §4 (Task 4.1) for
# the design rationale.

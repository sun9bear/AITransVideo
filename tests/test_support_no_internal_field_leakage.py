"""Contract guard: support modules must not embed internal-only paths/fields
into AI prompt construction or external adapter payloads.

Plan §10.4 — the AI / template / handoff layer is allowlist-only. Adding
``project_dir`` / ``workspace_dir`` / ``manifest_path`` / absolute path
prefixes / stacktrace strings into a prompt or external payload would
leak internal information to a third-party provider's logs.

This guard is AST-level — it scans every ``support*.py`` file and the
``support_adapters/`` subpackage for offending string literals. Comments
and docstrings are EXEMPT (we want to be able to talk about why those
fields are forbidden inside the source).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Forbidden string fragments. Listed as substrings so e.g.
# `Job.workspace_dir` triggers via the `workspace_dir` literal.
FORBIDDEN_FRAGMENTS = (
    "project_dir",
    "workspace_dir",
    "manifest_path",
    "/opt/aivideotrans",
)

# Forbidden path-prefix patterns. Matched as `string.startswith(pattern)`.
FORBIDDEN_PREFIXES = (
    "D:\\",
    "/opt/",
    "/home/",
    "/root/",
    "/var/",
    "/mnt/",
)

# Files this guard scans are exactly those that construct AI prompts or
# external-recipient payloads. Pure DB / HTTP / config layers are out of
# scope — they legitimately need to know about /opt/aivideotrans paths,
# Path(...) construction, etc. Plan §10.4 — the leak risk is the AI
# prompt and external adapter content, not the storage location of a
# config file.
SCAN_FILES = [
    REPO / "gateway" / "support_ai.py",
    REPO / "gateway" / "support_service.py",
    REPO / "gateway" / "support_handoff.py",
    REPO / "gateway" / "support_templates.py",
    REPO / "gateway" / "support_policy.py",
    REPO / "gateway" / "support_budget.py",
    REPO / "gateway" / "support_knowledge.py",
    REPO / "gateway" / "support_models.py",
    REPO / "gateway" / "notifications_service.py",
    REPO / "gateway" / "notifications_helpers.py",
    REPO / "gateway" / "notification_dispatch_map.py",
    REPO / "gateway" / "support_adapters" / "email.py",
    REPO / "gateway" / "support_adapters" / "chatwoot.py",
    REPO / "gateway" / "support_adapters" / "wechat_kf.py",
]


def _docstring_offsets(tree: ast.AST) -> set[int]:
    """Return line numbers where docstring literals live, so we can skip them."""
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                lit = body[0].value
                start = getattr(lit, "lineno", 0)
                end = getattr(lit, "end_lineno", start) or start
                for ln in range(start, end + 1):
                    docstring_lines.add(ln)
    return docstring_lines


def _env_fallback_constants(tree: ast.AST) -> set[int]:
    """Lines where the literal is config-layer infrastructure, not prompt content.

    Three exemption patterns, all match against AST shape (not regex):

    1. ``os.environ.get(KEY, "/opt/aivideotrans/config")`` — fallback default.
    2. ``os.getenv(KEY, "/opt/...")`` — same idea.
    3. ``Path("/opt/aivideotrans/app/src")`` / ``pathlib.Path(...)`` —
       sys.path setup or local path constants.

    Each exemption marks ONLY the line number of the literal, so an
    unrelated literal on the same line still trips the guard.

    Plan §10.4 leak risk is the AI prompt and external adapter content,
    not the storage location of a config file. These exemptions reflect
    that distinction explicitly.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        is_env_get = False
        if isinstance(func, ast.Attribute) and func.attr == "get":
            value = func.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
                and value.attr == "environ"
            ):
                is_env_get = True
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr == "getenv"
        ):
            is_env_get = True
        if is_env_get:
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                lit = node.args[1]
                ln = getattr(lit, "lineno", 0)
                if ln:
                    exempt.add(ln)
            continue

        # Path("/opt/...") or pathlib.Path("/opt/...")
        is_path_call = False
        if isinstance(func, ast.Name) and func.id == "Path":
            is_path_call = True
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "Path"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pathlib"
        ):
            is_path_call = True
        if is_path_call:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    ln = getattr(arg, "lineno", 0)
                    if ln:
                        exempt.add(ln)
    return exempt


def _iter_support_files() -> list[Path]:
    return [p for p in SCAN_FILES if p.exists()]


def _scan(py_path: Path) -> list[str]:
    """Return a list of human-readable violations for this file."""
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    docstring_lines = _docstring_offsets(tree)
    env_fallback_lines = _env_fallback_constants(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        line = getattr(node, "lineno", 0)
        if line in docstring_lines:
            continue
        if line in env_fallback_lines:
            # Env-var fallback default — config layer, not AI prompt.
            continue
        text = node.value
        for frag in FORBIDDEN_FRAGMENTS:
            if frag in text:
                violations.append(
                    f"{py_path.name}:{line} contains forbidden fragment {frag!r} in literal {text[:80]!r}"
                )
        for prefix in FORBIDDEN_PREFIXES:
            if text.startswith(prefix):
                violations.append(
                    f"{py_path.name}:{line} starts with forbidden prefix {prefix!r}: {text[:80]!r}"
                )
    return violations


def test_support_modules_have_no_internal_field_leakage():
    files = _iter_support_files()
    assert files, "expected to find at least one support_*.py to scan"
    all_violations: list[str] = []
    for path in files:
        # Allow the dedicated sanitizer to *list* forbidden field names as
        # part of its own logic. The sanitizer file documents which fields
        # are stripped and uses the literal strings for that purpose.
        if path.name == "support_knowledge.py":
            continue
        all_violations.extend(_scan(path))
    assert all_violations == [], (
        "Support modules contain forbidden internal field references:\n  "
        + "\n  ".join(all_violations)
    )


def test_sanitize_job_context_dataclass_has_no_internal_fields():
    """The JobContextForAI dataclass (plan §10.4) declares the allowlist of
    fields legal to send to AI. Adding any of the forbidden fragments
    here would silently reopen the leak — fail loudly.
    """
    src = (REPO / "gateway" / "support_knowledge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_dataclass = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "JobContextForAI":
            found_dataclass = True
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name
                ):
                    name = item.target.id
                    for frag in FORBIDDEN_FRAGMENTS:
                        assert frag not in name, (
                            f"JobContextForAI declares forbidden field {name!r}"
                        )
            break
    assert found_dataclass, "JobContextForAI dataclass not found in support_knowledge.py"

"""AST import guard: collector must only import stdlib."""
import ast
from pathlib import Path

COLLECTOR_PATH = (Path(__file__).resolve().parent.parent
                  / "scripts" / "smart_shadow_eval_collector.py")

STDLIB_WHITELIST = frozenset({
    "argparse", "json", "pathlib", "datetime", "hashlib",
    "sys", "os", "signal", "traceback", "socket", "subprocess",
    "logging", "collections", "typing", "dataclasses", "re", "time",
    "__future__",
})

FORBIDDEN_PREFIXES = ("src.", "gateway.")
FORBIDDEN_NAMES = frozenset({
    "anthropic", "google", "boto3", "openai", "httpx",
    "pydantic", "faster_whisper", "ctranslate2", "torch",
})


def _imported_top_modules(tree: ast.AST) -> set[str]:
    """Yield top-level module names from import statements."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def test_collector_only_imports_stdlib():
    src = COLLECTOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = _imported_top_modules(tree)
    for mod in imported:
        assert mod in STDLIB_WHITELIST, (
            f"forbidden import: {mod!r} not in stdlib whitelist"
        )
        assert not any(mod.startswith(p) for p in FORBIDDEN_PREFIXES), (
            f"forbidden import: {mod!r} starts with project prefix"
        )
        assert mod not in FORBIDDEN_NAMES, (
            f"forbidden external SDK import: {mod!r}"
        )

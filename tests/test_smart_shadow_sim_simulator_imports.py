"""AST import guards for P1 simulator + aggregator: stdlib-only.

Mirrors P0 collector's guard pattern (see test_smart_shadow_eval_collector_imports.py).
"""
import ast
from pathlib import Path

SIMULATOR_PATH = (Path(__file__).resolve().parent.parent
                  / "scripts" / "smart_shadow_sim_simulator.py")
AGGREGATOR_PATH = (Path(__file__).resolve().parent.parent
                   / "scripts" / "smart_shadow_sim_aggregator.py")

STDLIB_WHITELIST = frozenset({
    "argparse", "json", "pathlib", "datetime", "hashlib",
    "sys", "os", "signal", "traceback", "socket", "subprocess",
    "logging", "collections", "typing", "dataclasses", "re", "time",
    "__future__",
})

FORBIDDEN_PREFIXES = ("src.", "gateway.", "services.", "modules.")
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


def _check_imports(script_path: Path):
    src = script_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = _imported_top_modules(tree)
    for mod in imported:
        assert mod in STDLIB_WHITELIST, (
            f"forbidden import in {script_path.name}: {mod!r} not in stdlib whitelist"
        )
        assert not any(mod.startswith(p) for p in FORBIDDEN_PREFIXES), (
            f"forbidden import in {script_path.name}: {mod!r} starts with project prefix"
        )
        assert mod not in FORBIDDEN_NAMES, (
            f"forbidden external SDK import in {script_path.name}: {mod!r}"
        )


def test_simulator_only_imports_stdlib():
    _check_imports(SIMULATOR_PATH)


def test_aggregator_only_imports_stdlib():
    _check_imports(AGGREGATOR_PATH)

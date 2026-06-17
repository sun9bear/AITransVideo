"""P3b register+bill capacity guard source scan."""
from __future__ import annotations

import ast
from pathlib import Path


_API = Path(__file__).resolve().parents[1] / "gateway" / "user_voice_api.py"


def _func_src(name: str) -> str:
    src = _API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def test_register_billed_passes_admin_library_cap_to_service():
    body = _func_src("internal_smart_clone_register_billed")

    assert "smart_user_voice_clone_cap" in body
    assert "library_cap=library_cap" in body
    assert body.index("smart_user_voice_clone_cap") < body.index(
        "await register_smart_clone_with_billing("
    )
    assert "voice_library_full" in body

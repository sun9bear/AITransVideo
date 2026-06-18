"""Guard smart clone billed registration idempotency conflicts."""
from __future__ import annotations

import ast
import re
from pathlib import Path


_API = Path(__file__).resolve().parents[1] / "gateway" / "user_voice_api.py"


def _func_src(name: str) -> str:
    src = _API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == name
        ):
            return ast.get_source_segment(src, node) or ""
    return ""


def test_register_billed_maps_idempotency_conflict_to_409():
    body = _func_src("internal_smart_clone_register_billed")
    assert "idempotency_conflict" in body
    assert re.search(
        r'idempotency_conflict.*?_json\(\s*409',
        body,
        re.S,
    ) or re.search(
        r'_json\(\s*409[^)]*idempotency_conflict',
        body,
        re.S,
    )

"""Contract guard: support_ai's default provider literal must be ``"fake"``.

Plan §11 — accidental promotion of a real provider as the default would
trigger paid LLM calls on every visitor message. The literal value of
``DEFAULT_PROVIDER`` is enforced here at the AST level so future edits
that try to make the default dynamic / env-derived / any-non-literal
fail this test.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPPORT_AI = REPO / "gateway" / "support_ai.py"


def _find_default_provider_literal() -> str | None:
    src = SUPPORT_AI.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "DEFAULT_PROVIDER"
            and isinstance(node.value, ast.Constant)
        ):
            value = node.value.value
            if isinstance(value, str):
                return value
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "DEFAULT_PROVIDER"
            and isinstance(node.value, ast.Constant)
        ):
            value = node.value.value
            if isinstance(value, str):
                return value
    return None


def test_default_provider_literal_is_fake():
    literal = _find_default_provider_literal()
    assert literal == "fake", (
        f"DEFAULT_PROVIDER must literally equal \"fake\"; got {literal!r}. "
        "Plan §11 requires the default to be the deterministic fake provider "
        "so missing env config never silently activates a paid LLM."
    )


def test_resolve_provider_falls_back_to_fake_when_env_missing():
    """Reachable via the existing test runner because conftest puts src on
    sys.path. We rely on no-real-key-set state; this is the same condition
    every test environment is in (fake-default contract)."""
    # Make sure we are in the unset-env state before resolving.
    env_keys = (
        "AVT_SUPPORT_AI_PROVIDER",
        "DEEPSEEK_API_KEY",
    )
    saved = {k: os.environ.pop(k, None) for k in env_keys}
    try:
        # Importing this way avoids a hard dependency on `gateway.` being a
        # package — gateway has no __init__.py and is importable as a
        # namespace package.
        from gateway import support_ai  # type: ignore

        provider = support_ai.resolve_provider()
        assert provider.name == "fake"
        # Even if AVT_SUPPORT_AI_PROVIDER points to an unknown name, we
        # still fall back to fake.
        os.environ["AVT_SUPPORT_AI_PROVIDER"] = "nonexistent-provider"
        provider2 = support_ai.resolve_provider()
        assert provider2.name == "fake"
        # Asking explicitly for "deepseek" without DEEPSEEK_API_KEY makes
        # is_real_provider_ready return False — the calling service must
        # use that to keep the fake fallback active.
        assert support_ai.is_real_provider_ready("deepseek") is False
        # The fake provider always reports "ready=False" via the helper
        # because it is not a real provider; the helper's job is to flag
        # only real providers with usable credentials.
        assert support_ai.is_real_provider_ready("fake") is False
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

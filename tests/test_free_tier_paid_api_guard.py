"""Phase 2a Task 6 (gate #6) part (c) — paid-API guard for the FREE TTS path.

CLAUDE.md hard constraint: the free tier must NEVER auto-call a paid clone API
(MiniMax voice cloning). The free TTS path is exclusively MiMo:
  - voiceclone success    -> mimo_tts_provider.synthesize_voiceclone (free promo)
  - voiceclone failure     -> base MiMo preset (force_mimo_preset, SAME provider)
  - admin kill-switch off  -> CosyVoice preset (compute_job_policy, Task 6a)

These AST / behavioral guards (mirroring test_phase1_guards §1/§6) pin that the
free fallback can never reach a paid clone.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# Paid clone / paid TTS provider module tokens the free MiMo path must not import.
# (mimo_tts_provider's OWN zero-shot voiceclone is the FREE promo, not a paid clone.)
_FREE_PATH_FORBIDDEN_IMPORTS = (
    "voice_clone",
    "minimax_clone",
    "voice_clone_router",
    "minimax_tts",
    "minimax_voice",
    "express_clone",
    "express_reservation",
)


def test_mimo_provider_imports_no_paid_clone_module():
    """The free path's sole TTS provider (mimo_tts_provider) must not import any
    paid clone / paid TTS module — proves a free job can never reach MiniMax
    cloning through the provider it uses for BOTH voiceclone and the preset
    fallback."""
    src = _read("src/services/tts/mimo_tts_provider.py")
    tree = ast.parse(src)
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            for alias in getattr(node, "names", []):
                full = f"{mod}.{alias.name}".strip(".")
                for forbidden in _FREE_PATH_FORBIDDEN_IMPORTS:
                    if forbidden in full:
                        offending.append(f"{full} (matched {forbidden!r})")
    assert not offending, (
        "mimo_tts_provider.py imports a paid clone/TTS module — the free path "
        "could reach a paid API (CLAUDE.md):\n" + "\n".join(f"  {o}" for o in offending)
    )


def test_get_fallback_provider_mimo_is_none():
    """The generic provider-fallback chain must never switch a free MiMo job to a
    paid provider. get_fallback_provider('mimo', *) is None either way, so the
    free-voiceclone failure path can ONLY degrade within MiMo."""
    from services.tts.tts_strategy import get_fallback_provider

    assert get_fallback_provider("mimo", True) is None
    assert get_fallback_provider("mimo", False) is None


def test_free_voiceclone_fallback_routes_to_mimo_only():
    """The Task 6 free_voiceclone fallback in _generate_one_with_backoff must route
    to provider='mimo' + force_mimo_preset=True (base MiMo preset) — never a paid
    provider. AST-scans the _generate_one(...) call inside the free_voiceclone
    fallback branch."""
    tree = ast.parse(_read("src/services/tts/tts_generator.py"))
    backoff = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_generate_one_with_backoff"),
        None,
    )
    assert backoff is not None, "_generate_one_with_backoff not found"

    fallback_calls: list[dict] = []
    for node in ast.walk(backoff):
        if not isinstance(node, ast.If):
            continue
        if "free_voiceclone" not in ast.unparse(node.test):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "_generate_one"
            ):
                fallback_calls.append({k.arg: k.value for k in inner.keywords})

    assert fallback_calls, (
        "no self._generate_one(...) call found inside a free_voiceclone fallback "
        "branch of _generate_one_with_backoff"
    )
    for kw in fallback_calls:
        prov = kw.get("provider")
        assert isinstance(prov, ast.Constant) and prov.value == "mimo", (
            "free_voiceclone fallback must call _generate_one(provider='mimo') — "
            "never a paid provider"
        )
        forced = kw.get("force_mimo_preset")
        assert isinstance(forced, ast.Constant) and forced.value is True, (
            "free_voiceclone fallback must pass force_mimo_preset=True (base MiMo preset)"
        )

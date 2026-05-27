"""Phase 4.2 E.1 — A0b unit tests: `is_worker_enabled_in_env` + supports_clone
provider-branch logic.

A0b restored two files from `stash@{N}` (referenced by message
``pre-D.2-non-D.2-changes-2026-05-27``):

1. ``src/services/mainland_worker/client_factory.py``:
   New ``is_worker_enabled_in_env()`` lightweight env probe — checks
   ``AVT_MAINLAND_VOICE_WORKER_ENABLED`` only. Does NOT build a client,
   does NOT read URL / key_id / secret, does NOT do I/O. Used by
   ``_build_voice_selection_review`` to decide whether the CosyVoice
   "克隆音色" button is rendered to the frontend.

2. ``src/pipeline/process.py::_build_voice_selection_review``:
   New ``supports_clone`` provider branch:

     - ``minimax``    → True (always; legacy MiniMax clone)
     - ``cosyvoice``  → ``is_worker_enabled_in_env()`` (runtime mainland gate)
     - ``volcengine`` → False (no clone implementation)

   Previously hard-coded ``prov == "minimax"``. E.1 makes CosyVoice respect
   the env-driven runtime availability AND lets VolcEngine stay False.

These tests cover only the A0b unit semantics. The full wiring (button
visibility AND-ing supports_clone with /clone-gate's can_access_clone)
is verified by the E.1 guard tests in
``tests/test_phase42_e1_voice_selection_wiring_guards.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Section A — `is_worker_enabled_in_env()` env probe
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_env(monkeypatch):
    """Strip ``AVT_MAINLAND_VOICE_WORKER_ENABLED`` so each test starts clean.

    The env var leaks across tests otherwise (e.g. CI sets it to "1"). We use
    ``monkeypatch.delenv(..., raising=False)`` so the fixture also works on
    machines where the var isn't set at all.
    """
    monkeypatch.delenv("AVT_MAINLAND_VOICE_WORKER_ENABLED", raising=False)
    return monkeypatch


def _get_probe():
    """Import lazily so the import doesn't crash collection when src/ is
    unavailable (e.g. tests/ run from a worktree without src). The A0b
    file MUST exist in working tree for E.1 PR to compile."""
    from services.mainland_worker.client_factory import is_worker_enabled_in_env

    return is_worker_enabled_in_env


def test_a0b_probe_is_false_when_env_absent(_isolated_env):
    """**A0b probe — case 1**: env var absent → False (fail-safe-off)."""
    assert _get_probe()() is False


@pytest.mark.parametrize(
    "value",
    [
        "true",
        "True",
        "TRUE",
        "1",
        "yes",
        "YES",
        "on",
        "On",
    ],
)
def test_a0b_probe_is_true_for_truthy_values(_isolated_env, value):
    """**A0b probe — case 2**: standard truthy literals → True.

    Matches the same ``_TRUTHY_LITERALS`` set used by
    ``build_client_from_env``; mismatch would create a "build refuses but
    probe says enabled" inconsistency where frontend shows clone button
    while backend can't actually serve.
    """
    _isolated_env.setenv("AVT_MAINLAND_VOICE_WORKER_ENABLED", value)
    assert _get_probe()() is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "false",
        "False",
        "0",
        "no",
        "off",
        "disabled",
        "  ",  # whitespace-only
        "maybe",
        "nullable",  # contains "null" — must still be False
    ],
)
def test_a0b_probe_is_false_for_falsy_or_garbage(_isolated_env, value):
    """**A0b probe — case 3**: non-truthy / garbage → False (fail-safe-off).

    Any unrecognized string MUST yield False — never let an unknown value
    accidentally enable a paid API gateway. Standard ``_env_truthy`` rule.
    """
    _isolated_env.setenv("AVT_MAINLAND_VOICE_WORKER_ENABLED", value)
    assert _get_probe()() is False


def test_a0b_probe_does_not_touch_other_env_vars(_isolated_env):
    """**A0b probe — case 4**: probe is single-key. Setting URL / key_id /
    secret without ENABLED → still False. Probe must NOT short-circuit
    based on "looks configured"; it MUST be driven by the explicit gate.
    """
    _isolated_env.setenv("AVT_MAINLAND_VOICE_WORKER_URL", "https://nope.example")
    _isolated_env.setenv("AVT_MAINLAND_VOICE_WORKER_KEY_ID", "any")
    _isolated_env.setenv("AVT_MAINLAND_VOICE_WORKER_SECRET", "any")
    # ENABLED still absent → probe must report False
    assert _get_probe()() is False


def test_a0b_probe_does_no_io():
    """**A0b probe — case 5 (defense-in-depth)**: source code does not
    call requests / httpx / open / subprocess.

    AST scan the probe function for forbidden I/O imports / calls. If
    a future refactor accidentally turns probe into a "build_client + ping"
    side-effect, this guards fails immediately.
    """
    import ast
    import inspect

    from services.mainland_worker import client_factory

    src = inspect.getsource(client_factory.is_worker_enabled_in_env)
    tree = ast.parse(src)

    # NB: do NOT include "get" here — `os.environ.get()` is the legitimate
    # env-read pattern this probe uses. ``forbidden_module_prefixes`` below
    # still catches `requests.get` / `httpx.get` / `urllib.request.urlopen`
    # via the attribute-chain check.
    forbidden_names = {
        "open",
        "Path",
        "post",
        "request",
        "urlopen",
        "Popen",
        "run",
        "check_output",
    }
    forbidden_module_prefixes = {
        "httpx",
        "requests",
        "urllib",
        "subprocess",
        "socket",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            assert name not in forbidden_names, (
                f"is_worker_enabled_in_env() must NOT call I/O primitive "
                f"`{name}` — probe must be env-only."
            )
        if isinstance(node, ast.Attribute):
            mod = (
                node.value.id if isinstance(node.value, ast.Name) else None
            )
            if mod in forbidden_module_prefixes:
                raise AssertionError(
                    f"is_worker_enabled_in_env() must NOT touch I/O module "
                    f"`{mod}`. probe is env-read only."
                )


# ---------------------------------------------------------------------------
# Section B — supports_clone provider-branch source-level check (AST)
# ---------------------------------------------------------------------------
#
# We can't easily call ``_build_voice_selection_review`` directly without
# constructing a full ProcessPipeline instance + speakers / providers
# fixtures. Instead, we AST-scan the function body to verify the three
# provider branches yield the right boolean values. Behavioral tests of
# the full _build_voice_selection_review live in existing pipeline tests;
# this section locks the **shape** of the A0b change.
# ---------------------------------------------------------------------------


def _read_process_py() -> str:
    process_py = REPO_ROOT / "src" / "pipeline" / "process.py"
    assert process_py.exists()
    return process_py.read_text(encoding="utf-8")


def test_a0b_process_imports_is_worker_enabled_probe():
    """**A0b shape — case 1**: process.py imports
    ``is_worker_enabled_in_env`` from the client_factory module.

    Locked because:
    1. CLAUDE.md F.6 guard requires env-read centralization in
       client_factory; process.py importing it preserves that.
    2. A direct ``os.environ.get("AVT_MAINLAND_...")`` call inside
       process.py would break the F.6 boundary.
    """
    src = _read_process_py()
    assert (
        "from services.mainland_worker.client_factory import is_worker_enabled_in_env"
        in src
    ), (
        "process.py must import is_worker_enabled_in_env from "
        "services.mainland_worker.client_factory (centralized env read; "
        "F.6 guard requirement)."
    )


def test_a0b_process_supports_clone_three_branches_correct():
    """**A0b shape — case 2**: ``_build_voice_selection_review`` contains
    the three-branch supports_clone block with the right boolean shape:

      - ``minimax``    → ``True``
      - ``cosyvoice``  → ``_cosyvoice_clone_enabled`` (probe result, runtime)
      - else (volcengine) → ``False``

    Plain text scan of the block — sufficient to detect regression where
    someone reverts to ``prov == "minimax"`` single-line literal.
    """
    src = _read_process_py()
    # MiniMax branch
    assert 'if prov == "minimax":\n                supports_clone = True' in src, (
        "missing `if prov == 'minimax': supports_clone = True` branch"
    )
    # CosyVoice branch — must use probe result variable, NOT literal True/False
    assert (
        'elif prov == "cosyvoice":\n                supports_clone = _cosyvoice_clone_enabled'
        in src
    ), (
        "CosyVoice branch must read supports_clone from "
        "_cosyvoice_clone_enabled (probe result). Literal True would let "
        "the button render even when the env is off → 503 cascade."
    )
    # default else branch — must be False
    assert "else:\n                supports_clone = False" in src, (
        "missing else: supports_clone = False (volcengine has no clone)"
    )
    # The probe must be called and assigned to a local
    assert "_cosyvoice_clone_enabled = is_worker_enabled_in_env()" in src, (
        "process.py must call is_worker_enabled_in_env() and store in "
        "_cosyvoice_clone_enabled. Re-calling per-provider is fine but "
        "wasteful; single call before the loop is the canonical shape."
    )
    # Defensive: the legacy single-line shape MUST NOT coexist (no dead code path)
    assert '"supports_clone": prov == "minimax"' not in src, (
        "legacy hard-coded `\"supports_clone\": prov == \"minimax\"` still "
        "present — A0b refactor incomplete."
    )

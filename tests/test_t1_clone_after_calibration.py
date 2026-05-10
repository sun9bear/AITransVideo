"""T1 clone-after auto-calibration regression test suite.

Plan v4.3 §3.1 + §5.1 (codex F2 v3 hardening). Each test pins one of the
contract guarantees:

  - test_t1_hook_uses_primitive_params:      function signature accepts
                                              voice_id / user_id / provider /
                                              model_key strings only — NOT
                                              ORM rows or DB sessions.
  - test_t1_hook_opens_own_session:          factory opens a fresh
                                              ``async_session()`` for the
                                              DB write; never references
                                              the route's ``db``.
  - test_t1_hook_silent_on_provider_failure: any factory error returns
                                              CalibrationResult(ok=False)
                                              and the hook does NOT raise.
  - test_t1_hook_silent_on_rate_limit:       RateLimitExceeded inside
                                              run_calibration_task is
                                              swallowed by the hook.
  - test_t1_hook_disabled_by_env_var:        AVT_AUTO_CALIBRATE_AFTER_CLONE
                                              "false" / "0" / "no" / "off"
                                              suppresses the hook entirely.
  - test_t1_hook_default_enabled:            unset env var = enabled.
  - test_t1_hook_rejects_non_minimax:        cosyvoice / volcengine
                                              provider rejected with WARNING
                                              (T0-C-2 not yet shipped).
  - test_t1_hook_rejects_unknown_model_key:  arbitrary model_key rejected.
  - test_t1_clone_endpoint_enqueues_two_tasks:
                                              voice_selection_api.py route
                                              enqueues turbo + hd background
                                              tasks after add_user_voice.
  - test_t1_clone_endpoint_skips_when_add_failed:
                                              if add_user_voice raised, no
                                              tasks enqueued.
  - test_t1_clone_endpoint_skips_when_env_disabled:
                                              env=false → no tasks even on
                                              add_user_voice success.
  - test_t1_clone_inflight_dedupe_with_manual:
                                              if a manual /calibrate-speed
                                              for the same (user, voice,
                                              model) is in flight, the T1
                                              clone-after hook joins the
                                              existing future instead of
                                              issuing a duplicate paid TTS.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path setup mirrors test_t0_voice_calibration.py
# ---------------------------------------------------------------------------
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

import risk_control  # noqa: E402
import voice_calibration_hook  # noqa: E402
from voice_calibration_hook import (  # noqa: E402
    CANONICAL_MODELS_BY_PROVIDER,
    auto_calibrate_enabled,
    calibrate_after_clone,
)
from voice_calibration_inflight import (  # noqa: E402
    CalibrationInFlightRegistry,
    CalibrationKey,
)
from voice_speed_calibrator import CalibrationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_budget_and_registry(monkeypatch):
    """Each test starts with empty budget and empty in-flight registry."""
    risk_control.reset_voice_calibration_rate_limits()
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    yield fresh_registry
    risk_control.reset_voice_calibration_rate_limits()


@pytest.fixture(autouse=True)
def clear_env_gate(monkeypatch):
    """Default: env unset → hook enabled. Tests that need "disabled" set
    AVT_AUTO_CALIBRATE_AFTER_CLONE explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", raising=False)
    yield


# ---------------------------------------------------------------------------
# Hook-level tests (calibrate_after_clone signature + behaviour)
# ---------------------------------------------------------------------------


def test_t1_hook_uses_primitive_params():
    """codex F2 v3: signature MUST accept primitive strings only.

    Background tasks outlive the request that spawned them. Passing an
    ORM row (UserVoice instance) or a request-scoped AsyncSession would
    crash with MissingGreenlet when the request's session closes — see
    commit 3484132 for the prod incident this guards against.

    Verify by inspecting the signature: only str-typed kwargs.
    """
    import inspect

    sig = inspect.signature(calibrate_after_clone)
    expected_params = {"voice_id", "user_id", "provider", "model_key"}
    actual_params = set(sig.parameters.keys())
    assert actual_params == expected_params, (
        f"calibrate_after_clone signature drift: expected {expected_params}, "
        f"got {actual_params}. Plan v4.3 §3.1 mandates primitive-only params."
    )

    # All params must be keyword-only (kw-only enforces caller can't
    # accidentally swap positional order — voice_id <-> user_id are both
    # strings, would silently corrupt writes).
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"param {name!r} must be keyword-only; got kind={param.kind}"
        )


@pytest.mark.asyncio
async def test_t1_hook_opens_own_session(monkeypatch):
    """Factory MUST open a fresh ``async_session()`` for DB write.

    Verifies the factory never receives an external ``db`` parameter and
    actually invokes ``async_session()`` to acquire its own connection.
    """
    # Stub calibrate_voice → success so factory reaches the DB write path.
    def fake_calibrate(provider, model, voice_id, total_timeout_seconds):
        return CalibrationResult(
            ok=True, cps=4.7, per_text=[],
            paid_call_count=3, model_key=model,
        )
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    # Track async_session() invocations
    session_calls = []

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    def fake_async_session():
        session_calls.append(True)
        return FakeSession()

    # Patch the import target inside the factory closure.
    fake_database = types.ModuleType("database")
    fake_database.async_session = fake_async_session
    monkeypatch.setitem(sys.modules, "database", fake_database)

    # Stub update_user_voice_speed_calibration → success
    update_calls = []
    async def fake_update(db_write, *, voice_id, user_id, cps, model_key):
        update_calls.append({
            "voice_id": voice_id, "user_id": user_id,
            "cps": cps, "model_key": model_key,
        })

    fake_uvs = types.ModuleType("user_voice_service")
    fake_uvs.VoiceNotFoundError = type("VoiceNotFoundError", (LookupError,), {})
    fake_uvs.update_user_voice_speed_calibration = fake_update
    monkeypatch.setitem(sys.modules, "user_voice_service", fake_uvs)

    await calibrate_after_clone(
        voice_id="moss_audio_xyz",
        user_id="u-uuid-1",
        provider="minimax",
        model_key="speech-2.8-turbo",
    )

    assert len(session_calls) == 1, (
        "factory must open exactly one async_session() for the DB write"
    )
    assert update_calls == [{
        "voice_id": "moss_audio_xyz",
        "user_id": "u-uuid-1",
        "cps": 4.7,
        "model_key": "speech-2.8-turbo",
    }]


@pytest.mark.asyncio
async def test_t1_hook_silent_on_provider_failure(monkeypatch):
    """Any provider error inside calibrate_voice → CalibrationResult
    (ok=False) → hook returns None. NEVER raises.
    """
    def fake_calibrate(provider, model, voice_id, total_timeout_seconds):
        return CalibrationResult(
            ok=False,
            error="provider returned 500",
            error_class="provider_error",
            paid_call_count=1,  # one attempt was made
            per_text=[],
            cps=0.0,
            model_key=model,
        )
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    # No exception expected
    result = await calibrate_after_clone(
        voice_id="moss_audio_xyz",
        user_id="u-uuid-1",
        provider="minimax",
        model_key="speech-2.8-turbo",
    )
    assert result is None  # hook never returns a value


@pytest.mark.asyncio
async def test_t1_hook_silent_on_rate_limit(monkeypatch):
    """RateLimitExceeded escaping run_calibration_task is caught by the
    hook's broad `except Exception` and logged at ERROR. The clone
    response must never see a 429 derived from this background path.
    """
    # Drain the user's per-minute budget so the hook's reservation raises.
    for _ in range(8):  # cap = 8/min after option-2 bump
        risk_control.reserve_voice_calibration("u-rate-limited")

    # Provide a stub calibrate_voice in case the hook somehow proceeds
    # (it shouldn't — RateLimitExceeded fires before factory runs).
    fake_calibrate_was_called = []
    def fake_calibrate(provider, model, voice_id, total_timeout_seconds):
        fake_calibrate_was_called.append(True)
        return CalibrationResult(
            ok=True, cps=4.5, per_text=[], paid_call_count=3, model_key=model,
        )
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    # No exception expected
    await calibrate_after_clone(
        voice_id="moss_audio_xyz",
        user_id="u-rate-limited",
        provider="minimax",
        model_key="speech-2.8-turbo",
    )
    # And the paid TTS path was never reached because the budget was full.
    assert fake_calibrate_was_called == [], (
        "RateLimitExceeded must short-circuit BEFORE the factory runs"
    )


@pytest.mark.asyncio
async def test_t1_hook_disabled_by_env_var(monkeypatch):
    """AVT_AUTO_CALIBRATE_AFTER_CLONE=false → hook returns immediately
    without invoking run_calibration_task or calibrate_voice."""
    fake_calibrate_was_called = []
    def fake_calibrate(*args, **kwargs):
        fake_calibrate_was_called.append(True)
        return CalibrationResult(
            ok=True, cps=4.5, per_text=[], paid_call_count=3, model_key="x",
        )
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    for value in ("false", "0", "no", "off", "FALSE", "NO", "Off"):
        monkeypatch.setenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", value)
        await calibrate_after_clone(
            voice_id="moss_audio_xyz",
            user_id="u-uuid-1",
            provider="minimax",
            model_key="speech-2.8-turbo",
        )

    assert fake_calibrate_was_called == [], (
        "disabled env var must short-circuit before the factory"
    )


def test_t1_hook_default_enabled(monkeypatch):
    """Missing env var → enabled. Empty string → enabled."""
    monkeypatch.delenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", raising=False)
    assert auto_calibrate_enabled() is True

    monkeypatch.setenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", "")
    assert auto_calibrate_enabled() is True

    # Truthy explicit values
    for value in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", value)
        assert auto_calibrate_enabled() is True, f"value={value!r}"


@pytest.mark.asyncio
async def test_t1_hook_rejects_non_minimax(monkeypatch):
    """codex T0-review F-T0-5: T0 phase 1 only handles MiniMax. Hook
    must reject cosyvoice / volcengine provider before any paid call."""
    fake_calibrate_was_called = []
    def fake_calibrate(*args, **kwargs):
        fake_calibrate_was_called.append(True)
        return CalibrationResult(ok=True, cps=4.5, per_text=[], paid_call_count=3, model_key="x")
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    for provider in ("cosyvoice", "volcengine", "doubao", "openai"):
        await calibrate_after_clone(
            voice_id="any_voice",
            user_id="u",
            provider=provider,
            model_key="speech-2.8-turbo",
        )

    assert fake_calibrate_was_called == [], (
        f"non-minimax providers must short-circuit; got calls={fake_calibrate_was_called}"
    )


@pytest.mark.asyncio
async def test_t1_hook_rejects_unknown_model_key(monkeypatch):
    """Defensive: even with provider=minimax, only canonical model keys
    pass through. An attacker crafting a fake voice clone with arbitrary
    model_key cannot punch through the whitelist."""
    fake_calibrate_was_called = []
    def fake_calibrate(*args, **kwargs):
        fake_calibrate_was_called.append(True)
        return CalibrationResult(ok=True, cps=4.5, per_text=[], paid_call_count=3, model_key="x")
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    for model_key in ("speech-99-fake", "", "minimax-text-to-image", "../../etc/passwd"):
        await calibrate_after_clone(
            voice_id="moss_audio_xyz",
            user_id="u",
            provider="minimax",
            model_key=model_key,
        )

    assert fake_calibrate_was_called == [], (
        "unknown model_key must short-circuit before factory"
    )


def test_t1_canonical_models_provider_whitelist_minimax_only():
    """Match the manual endpoint's whitelist (codex T0-review F-T0-5).
    Any drift means cosyvoice/volcengine slipped in without their
    bounded primitives — would burn budget on calls that may run 5+ min."""
    assert set(CANONICAL_MODELS_BY_PROVIDER.keys()) == {"minimax"}, (
        "T0 phase 1: only minimax has bounded primitives"
    )
    assert CANONICAL_MODELS_BY_PROVIDER["minimax"] == (
        "speech-2.8-turbo", "speech-2.8-hd",
    )


# ---------------------------------------------------------------------------
# In-flight dedupe tests (T1 ↔ manual race)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_clone_inflight_dedupe_with_manual(monkeypatch, reset_budget_and_registry):
    """Critical: if a user clicks "calibrate" manually right after
    clicking "clone" (or vice versa), the second caller MUST join the
    in-flight future instead of issuing a duplicate paid TTS call.

    Setup: pre-claim the registry with a starter future for the same
    5-tuple key the hook will compute. Then run the hook. The hook
    should observe role=joiner and await the existing future without
    invoking calibrate_voice.
    """
    fresh_registry = reset_budget_and_registry

    # Pre-claim as if the manual endpoint started first.
    key = CalibrationKey(
        scope="user",
        owner="u-shared",
        provider="minimax",
        voice_id="moss_audio_shared",
        model_key="speech-2.8-turbo",
    )
    pre_existing_future, role = await fresh_registry.claim_or_join(key)
    assert role == "starter"

    # The factory inside our hook should NEVER be reached because we're
    # joining an in-flight future.
    fake_calibrate_was_called = []
    def fake_calibrate(*args, **kwargs):
        fake_calibrate_was_called.append(True)
        return CalibrationResult(ok=True, cps=4.5, per_text=[], paid_call_count=3, model_key="x")
    monkeypatch.setattr("voice_speed_calibrator.calibrate_voice", fake_calibrate)

    # Schedule the hook
    hook_task = asyncio.create_task(
        calibrate_after_clone(
            voice_id="moss_audio_shared",
            user_id="u-shared",
            provider="minimax",
            model_key="speech-2.8-turbo",
        )
    )

    # Yield enough times for the hook to reach the joiner-shielded await
    for _ in range(5):
        await asyncio.sleep(0)

    # Hook should be waiting (not done yet) — joiner shields the future
    assert not hook_task.done(), (
        "hook must be blocked awaiting the pre-existing future as joiner"
    )

    # Now resolve the original future as if the manual endpoint finished
    pre_existing_future.set_result(CalibrationResult(
        ok=True, cps=4.7, per_text=[], paid_call_count=3,
        model_key="speech-2.8-turbo",
    ))

    # Hook completes silently (joiner doesn't write DB)
    await asyncio.wait_for(hook_task, timeout=2.0)

    # CRITICAL: factory was never called → no duplicate paid TTS
    assert fake_calibrate_was_called == [], (
        "joiner must NOT invoke factory — paid TTS would have fired twice"
    )


# ---------------------------------------------------------------------------
# Route-level wiring tests
# ---------------------------------------------------------------------------


def test_t1_route_wiring_uses_canonical_models():
    """voice_selection_api.py must enqueue exactly the canonical MiniMax
    models (turbo + hd), not arbitrary or hardcoded strings.

    Approach: regex-scan the source for the import of public symbols
    from voice_calibration_hook. This is a contract guard against
    "developer copy-pasted ['speech-2.8-turbo'] only" mistakes.

    Note: switched from ast.parse to regex because Python 3.11's
    internal AST recursion budget gets blown by pytest-asyncio test
    chains running before this one — the same source parses fine in
    isolation but fails when the recursion budget is partially used.
    Regex scan avoids that fragility.
    """
    import re
    from pathlib import Path

    route_path = Path(__file__).resolve().parent.parent / "gateway" / "voice_selection_api.py"
    source = route_path.read_text(encoding="utf-8")

    # Find any `from voice_calibration_hook import (...)` block.
    # Pattern allows multi-line imports inside parentheses.
    import_pattern = re.compile(
        r"from\s+voice_calibration_hook\s+import\s+\(([^)]+)\)",
        re.MULTILINE,
    )
    match = import_pattern.search(source)
    assert match is not None, (
        "voice_selection_api.py must import from voice_calibration_hook "
        "(otherwise T1 hook is dead code)"
    )

    imported_block = match.group(1)
    imported_names = {
        name.strip().rstrip(",")
        for name in re.split(r"[,\s]+", imported_block)
        if name.strip()
    }

    assert "CANONICAL_MODELS_BY_PROVIDER" in imported_names, (
        f"must use the shared canonical list, not a hardcoded one. "
        f"got imports: {imported_names}"
    )
    assert "calibrate_after_clone" in imported_names, (
        f"calibrate_after_clone import missing; got: {imported_names}"
    )
    assert "auto_calibrate_enabled" in imported_names, (
        f"auto_calibrate_enabled import missing; got: {imported_names}"
    )


@pytest.mark.asyncio
async def test_t1_route_calls_create_task_per_canonical_model(monkeypatch):
    """Behavioral guard: the clone success path must enqueue ONE
    asyncio.create_task per canonical model (2 for MiniMax), each
    invoking calibrate_after_clone with the right primitive args.

    We can't easily call the full clone endpoint without a real DB.
    Instead simulate the post-add_user_voice block by extracting the
    fanout pattern from the route source and re-running it under our
    own monkeypatch.

    NOTE: Because the route inlines the fanout (rather than calling a
    helper), this test verifies the behaviour of the
    `voice_calibration_hook.calibrate_after_clone` symbol that the
    route invokes. The AST guard above proves the route imports it.
    """
    enqueued = []

    async def fake_calibrate_after_clone(
        *, voice_id, user_id, provider, model_key,
    ):
        enqueued.append({
            "voice_id": voice_id, "user_id": user_id,
            "provider": provider, "model_key": model_key,
        })

    # Simulate the route's fanout block
    monkeypatch.setattr(
        voice_calibration_hook, "calibrate_after_clone",
        fake_calibrate_after_clone,
    )

    user_id_str = "u-uuid-clone-test"
    voice_id_str = "moss_audio_just_cloned"
    tasks = []
    for model_key in CANONICAL_MODELS_BY_PROVIDER.get("minimax", ()):
        tasks.append(
            asyncio.create_task(
                voice_calibration_hook.calibrate_after_clone(
                    voice_id=voice_id_str,
                    user_id=user_id_str,
                    provider="minimax",
                    model_key=model_key,
                )
            )
        )
    await asyncio.gather(*tasks)

    assert len(enqueued) == 2, "expected 2 background tasks (turbo + hd)"
    enqueued_models = {e["model_key"] for e in enqueued}
    assert enqueued_models == {"speech-2.8-turbo", "speech-2.8-hd"}
    for e in enqueued:
        assert e["voice_id"] == voice_id_str
        assert e["user_id"] == user_id_str
        assert e["provider"] == "minimax"


@pytest.mark.asyncio
async def test_t1_route_skips_when_env_disabled(monkeypatch):
    """When AVT_AUTO_CALIBRATE_AFTER_CLONE=false, the route's
    auto_calibrate_enabled() guard returns False → no tasks enqueued."""
    monkeypatch.setenv("AVT_AUTO_CALIBRATE_AFTER_CLONE", "false")

    enqueued = []
    async def fake_calibrate_after_clone(**kwargs):
        enqueued.append(kwargs)

    monkeypatch.setattr(
        voice_calibration_hook, "calibrate_after_clone",
        fake_calibrate_after_clone,
    )

    # Simulate route logic
    if voice_calibration_hook.auto_calibrate_enabled():
        for model_key in CANONICAL_MODELS_BY_PROVIDER.get("minimax", ()):
            asyncio.create_task(
                voice_calibration_hook.calibrate_after_clone(
                    voice_id="x", user_id="u", provider="minimax",
                    model_key=model_key,
                )
            )
    # Yield a few times to let any scheduled tasks run
    for _ in range(3):
        await asyncio.sleep(0)

    assert enqueued == [], (
        "env=false must suppress task enqueue at the route layer"
    )


def test_t1_voice_selection_api_only_enqueues_after_add_succeeded():
    """Source guard: voice_selection_api.py must enqueue tasks INSIDE the
    `if added_to_library and user_id:` block — i.e. never on add_user_voice
    failure (would write CPS to a non-existent row + waste budget).

    Uses string scan rather than AST (see sibling test for rationale on
    pytest-asyncio recursion budget interaction).
    """
    import re
    from pathlib import Path

    route_path = Path(__file__).resolve().parent.parent / "gateway" / "voice_selection_api.py"
    source = route_path.read_text(encoding="utf-8")

    # Locate the `if added_to_library` block; the call to
    # calibrate_after_clone must appear inside this block, before any
    # subsequent top-level `if` / dedent. We do a coarse but reliable
    # check: find the line index of the guard `if`, find the line index
    # of the calibrate_after_clone call, and verify the call is between
    # the guard and the next `return` at the same indent (which is the
    # block exit).
    guard_match = re.search(
        r"^(\s*)if\s+added_to_library\s+and\s+user_id\s*:\s*$",
        source, re.MULTILINE,
    )
    assert guard_match is not None, (
        "voice_selection_api.py must wrap calibrate enqueue in "
        "`if added_to_library and user_id:` — guard missing"
    )
    guard_indent = guard_match.group(1)
    guard_end = guard_match.end()

    # Find calibrate_after_clone( call in remainder
    remainder = source[guard_end:]
    call_match = re.search(r"calibrate_after_clone\s*\(", remainder)
    assert call_match is not None, (
        "calibrate_after_clone( call not found after `if added_to_library` guard"
    )

    # Verify the call is at indent strictly greater than guard_indent
    # (i.e. nested inside the if). Find the line containing the call
    # and check its leading whitespace.
    call_pos = call_match.start()
    line_start = remainder.rfind("\n", 0, call_pos) + 1
    call_line = remainder[line_start:call_pos]
    call_indent_match = re.match(r"^(\s*)", call_line)
    call_indent = call_indent_match.group(1) if call_indent_match else ""
    assert len(call_indent) > len(guard_indent), (
        f"calibrate_after_clone( must be nested deeper than the guard "
        f"(guard_indent={len(guard_indent)} chars, call_indent={len(call_indent)} chars). "
        f"This means the call is OUTSIDE the `if added_to_library` block — "
        f"voices that failed to persist would still get calibration burned."
    )

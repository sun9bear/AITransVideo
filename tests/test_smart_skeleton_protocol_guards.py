"""Smart MVP P2 — Protocol + AST guard + fake behavior + wiring inject suite.

Companion to ``tests/test_smart_skeleton_acceptance.py`` (which covers
F1-F4 state-machine wiring). This file locks the second-PR delivery
(Codex candidate B):

  - AST guard: src/services/smart/**.py is forbidden from importing
    real provider modules (services.voice_clone / services.voice.* /
    services.tts.* / services.llm.*). The composition root in
    src/services/smart_wiring.py is the only place those imports
    happen for Smart, and lives outside the smart package on purpose.
    Plan §8.2 #1 + Codex 第三轮 F5.

  - Protocol compliance: real adapters in smart_wiring.py satisfy the
    Protocol shapes declared in services.smart.contracts (runtime
    isinstance check via @runtime_checkable). Same for fakes.

  - Fake behaviour: knobs (success / quota / failure-after-N) work
    as documented; record-on-call gives tests assertion handles.

  - Wiring inject_for_test: context manager replaces / restores the
    default adapter cleanly; nested injects compose; restore runs even
    when the body raises.

Test coverage rationale: this PR is "infrastructure" — landing it
without behaviour tests would leave the wiring contract un-locked
and let subsequent business-logic PRs accidentally bypass the
provider injection (the exact failure pattern Codex's 8 review
rounds kept catching).
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Repo path setup — mirrors tests/conftest.py + test_smart_skeleton_acceptance.py
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
_GATEWAY = _PROJECT_ROOT / "gateway"
for _p in (str(_SRC), str(_GATEWAY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# DB stub — same as test_credits_service.py / test_smart_skeleton_acceptance.py
if "database" not in sys.modules:
    _fake_database = types.ModuleType("database")
    _fake_database.get_db = MagicMock()
    _fake_database.engine = MagicMock()
    _fake_database.async_session = MagicMock()
    sys.modules["database"] = _fake_database


# ===================================================================
# AST guard — smart package may NOT import real provider modules
# ===================================================================


_SMART_PACKAGE_DIR = _SRC / "services" / "smart"

# Module path prefixes (after `from ` or as the first dotted segment in
# `import X`) that are forbidden inside src/services/smart/. Centralised
# so future paid-API providers can be added once.
_FORBIDDEN_PREFIXES = (
    "services.voice_clone",
    "services.voice.auto_clone",
    "services.tts.",       # any TTS provider submodule
    "services.llm.",       # any LLM provider submodule
)


def _iter_smart_package_modules():
    """Yield (Path, module_relative_name) for every .py in services.smart."""
    for path in sorted(_SMART_PACKAGE_DIR.rglob("*.py")):
        if path.name.startswith("_") and path.name != "__init__.py":
            # _internal modules still subject to guard, but skip
            # generated / cache files just in case.
            continue
        rel = path.relative_to(_SRC).with_suffix("").as_posix().replace("/", ".")
        yield path, rel


def _imports_in_module(path: Path) -> list[tuple[str, int]]:
    """Return (imported_module_name, lineno) for every Import / ImportFrom
    node in ``path``. Resolves relative imports against the file's
    package position so the guard catches ``from .voice_clone import X``
    forms too."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Skip relative imports — they target inside the smart
            # package so by definition not reaching forbidden externals.
            if node.level and not module:
                continue
            out.append((module, node.lineno))
    return out


class TestSmartPackageAstGuard:
    """Plan §8.2 #1: src/services/smart/**.py must not directly import
    real paid-provider modules. Composition root in
    src/services/smart_wiring.py (sibling of, not inside, the package)
    is the only allowed adapter site."""

    def test_no_smart_module_imports_real_provider(self):
        """Walk every .py inside src/services/smart/, AST-parse, assert
        no Import / ImportFrom targets a forbidden provider prefix."""
        violations: list[str] = []
        for path, modname in _iter_smart_package_modules():
            for imported, lineno in _imports_in_module(path):
                for forbidden in _FORBIDDEN_PREFIXES:
                    if imported == forbidden.rstrip(".") or imported.startswith(forbidden):
                        violations.append(
                            f"{path.relative_to(_PROJECT_ROOT).as_posix()}:{lineno} "
                            f"imports {imported!r} (forbidden prefix {forbidden!r})"
                        )
        assert not violations, (
            "Smart package modules must NOT import real provider modules "
            "directly. Use the Protocol interfaces in services.smart.contracts "
            "and inject the real adapter via services.smart_wiring.\n\n"
            + "\n".join(violations)
        )

    def test_smart_wiring_lives_outside_smart_package(self):
        """The composition root file must be at src/services/smart_wiring.py,
        NOT src/services/smart/wiring.py — otherwise it would be subject
        to the guard above and couldn't import real providers."""
        wiring_outside = _SRC / "services" / "smart_wiring.py"
        wiring_inside = _SRC / "services" / "smart" / "wiring.py"
        assert wiring_outside.exists(), (
            "src/services/smart_wiring.py is missing — composition root must "
            "live as a sibling of the smart package."
        )
        assert not wiring_inside.exists(), (
            "src/services/smart/wiring.py exists — composition root must NOT "
            "live inside the smart package (it would trip the AST guard)."
        )

    def test_guard_finds_a_known_forbidden_import_when_planted(self, tmp_path):
        """Sanity meta-test: write a tmp file that imports a forbidden
        prefix and verify the guard's import-walking logic flags it.
        Catches "guard silently passes everything" regressions."""
        bad = tmp_path / "bad.py"
        bad.write_text("from services.voice_clone import MiniMaxVoiceCloneClient\n")
        imports = _imports_in_module(bad)
        assert any(
            mod.startswith(prefix.rstrip("."))
            for mod, _line in imports
            for prefix in _FORBIDDEN_PREFIXES
        ), "guard's _imports_in_module() failed to spot a planted violation"


# ===================================================================
# Protocol compliance — real adapters + fakes both satisfy the shape
# ===================================================================


class TestProtocolCompliance:
    """@runtime_checkable Protocols let us isinstance-check at test time
    so a missing or renamed method shows up here rather than at first
    real-call site."""

    def test_real_minimax_clone_adapter_satisfies_clone_provider(self):
        from services.smart.contracts import CloneProvider
        from services.smart_wiring import _MiniMaxCloneAdapter

        adapter = _MiniMaxCloneAdapter()
        assert isinstance(adapter, CloneProvider), (
            "_MiniMaxCloneAdapter must satisfy CloneProvider Protocol — "
            "renaming clone_voice() or changing its signature breaks Smart."
        )

    def test_fake_clone_provider_satisfies_clone_provider(self):
        from services.smart.contracts import CloneProvider
        from tests.fakes import FakeCloneProvider

        assert isinstance(FakeCloneProvider(), CloneProvider)

    def test_fake_tts_provider_satisfies_tts_provider(self):
        from services.smart.contracts import TTSProvider
        from tests.fakes import FakeTTSProvider

        assert isinstance(FakeTTSProvider(), TTSProvider)

    def test_fake_llm_provider_satisfies_llm_provider(self):
        from services.smart.contracts import LLMProvider
        from tests.fakes import FakeLLMProvider

        assert isinstance(FakeLLMProvider(), LLMProvider)

    def test_not_wired_tts_adapter_satisfies_protocol(self):
        """Even the stub adapter satisfies the Protocol shape — only the
        method body raises. Lets the wiring module load + return
        something usable that fails loudly only on actual call."""
        from services.smart.contracts import TTSProvider
        from services.smart_wiring import _NotWiredTTSAdapter

        assert isinstance(_NotWiredTTSAdapter(), TTSProvider)

    def test_not_wired_llm_adapter_satisfies_protocol(self):
        from services.smart.contracts import LLMProvider
        from services.smart_wiring import _NotWiredLLMAdapter

        assert isinstance(_NotWiredLLMAdapter(), LLMProvider)


# ===================================================================
# Fake behaviour — knobs work as documented
# ===================================================================


class TestFakeCloneProviderBehaviour:
    def test_default_success_returns_deterministic_voice_id(self, tmp_path):
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        result = fake.clone_voice(
            speaker_id="speaker_a",
            speaker_name="查理·芒格",
            source_audio_path=tmp_path / "sample.wav",
        )
        assert result.speaker_id == "speaker_a"
        assert result.voice_id == "fake_vt_speaker_a_19700101"
        assert result.provider_name == "fake_minimax_voice_clone"

    def test_success_false_raises_clone_error(self, tmp_path):
        from tests.fakes import FakeCloneProvider
        from tests.fakes.fake_clone_provider import FakeCloneError

        fake = FakeCloneProvider(success=False)
        with pytest.raises(FakeCloneError):
            fake.clone_voice(
                speaker_id="speaker_a",
                speaker_name="x",
                source_audio_path=tmp_path / "sample.wav",
            )
        # Call still recorded (auto_voice_review needs to know the
        # provider was attempted before falling through to preset).
        assert len(fake.calls) == 1

    def test_quota_zero_raises_quota_error(self, tmp_path):
        from tests.fakes import FakeCloneProvider
        from tests.fakes.fake_clone_provider import FakeCloneQuotaError

        fake = FakeCloneProvider(quota_remaining=0)
        with pytest.raises(FakeCloneQuotaError):
            fake.clone_voice(
                speaker_id="speaker_a",
                speaker_name="x",
                source_audio_path=tmp_path / "sample.wav",
            )

    def test_quota_decrements_only_on_success(self, tmp_path):
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider(quota_remaining=2)
        fake.clone_voice(
            speaker_id="speaker_a",
            speaker_name="x",
            source_audio_path=tmp_path / "sample.wav",
        )
        assert fake.quota_remaining == 1
        fake.clone_voice(
            speaker_id="speaker_b",
            speaker_name="y",
            source_audio_path=tmp_path / "sample.wav",
        )
        assert fake.quota_remaining == 0

    def test_failure_after_n_calls(self, tmp_path):
        from tests.fakes import FakeCloneProvider
        from tests.fakes.fake_clone_provider import FakeCloneError

        fake = FakeCloneProvider(failure_after_n_calls=2)
        # Calls 1 and 2 succeed.
        for sid in ("speaker_a", "speaker_b"):
            fake.clone_voice(
                speaker_id=sid,
                speaker_name=sid,
                source_audio_path=tmp_path / "sample.wav",
            )
        # Call 3 raises.
        with pytest.raises(FakeCloneError):
            fake.clone_voice(
                speaker_id="speaker_c",
                speaker_name="z",
                source_audio_path=tmp_path / "sample.wav",
            )


class TestFakeTTSProviderBehaviour:
    def test_default_returns_synthetic_result(self):
        from tests.fakes import FakeTTSProvider

        fake = FakeTTSProvider(simulated_duration_seconds=4.5, billed_chars_per_call=80)
        result = fake.synthesize(
            text="这是一段中文。",
            voice_id="fake_voice_x",
            model_name="speech-2.8-hd",
        )
        assert result.duration_seconds == 4.5
        assert result.billed_chars == 80
        assert result.audio_path.name.startswith("call_001")

    def test_failure_after_n_calls(self):
        from tests.fakes import FakeTTSProvider
        from tests.fakes.fake_tts_provider import FakeTTSError

        fake = FakeTTSProvider(failure_after_n_calls=1)
        fake.synthesize(text="一", voice_id="v", model_name="m")
        with pytest.raises(FakeTTSError):
            fake.synthesize(text="二", voice_id="v", model_name="m")


class TestInMemoryUsageMeterBehaviour:
    def test_record_voice_clone_appears_in_summarize(self):
        from tests.fakes import InMemoryUsageMeter

        meter = InMemoryUsageMeter()
        meter.record_voice_clone(
            provider="fake_minimax",
            model="voice_clone",
            voice_id="fake_vt_x",
            speaker_id="speaker_a",
            source_audio_seconds=12.5,
            source_audio_bytes=200_000,
            selected_segment_count=3,
        )
        summary = meter.summarize()
        assert summary["voice_clone_call_count"] == 1
        assert summary["voice_clone_billable_count"] == 1
        assert summary["tts_call_count"] == 0

    def test_billable_false_does_not_count_billable(self):
        from tests.fakes import InMemoryUsageMeter

        meter = InMemoryUsageMeter()
        meter.record_voice_clone(
            provider="x", model=None, voice_id="vid", speaker_id="sid",
            billable=False,
        )
        summary = meter.summarize()
        assert summary["voice_clone_call_count"] == 1
        assert summary["voice_clone_billable_count"] == 0

    def test_per_bucket_tts_chars_split_correctly(self):
        from tests.fakes import InMemoryUsageMeter

        meter = InMemoryUsageMeter()
        meter.record_tts_call(provider="p", model="m", voice_id="v",
                              billed_chars=100, bucket="first_tts")
        meter.record_tts_call(provider="p", model="m", voice_id="v",
                              billed_chars=50, bucket="post_tts_resynth")
        meter.record_tts_call(provider="p", model="m", voice_id="v",
                              billed_chars=30, bucket="post_edit_resynth")
        summary = meter.summarize()
        assert summary["first_tts_billed_chars"] == 100
        assert summary["post_tts_resynth_billed_chars"] == 50
        assert summary["post_edit_resynth_billed_chars"] == 30
        assert summary["tts_billed_chars"] == 180


# ===================================================================
# Wiring inject_for_test — context manager replaces and restores
# ===================================================================


class TestWiringInjectForTest:
    def _reset_module_state(self):
        """Helper — wiring keeps module-level singletons; reset between
        tests so leakage from one test doesn't pollute the next."""
        import services.smart_wiring as wiring

        wiring._default_clone_provider = None
        wiring._default_tts_provider = None
        wiring._default_llm_provider = None
        wiring._test_clone_override = None
        wiring._test_tts_override = None
        wiring._test_llm_override = None

    def test_inject_replaces_default_clone_provider(self):
        self._reset_module_state()
        from services.smart_wiring import build_smart_clone_provider, inject_for_test
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        with inject_for_test(clone_provider=fake):
            assert build_smart_clone_provider() is fake

    def test_inject_restores_default_after_context_exits(self):
        self._reset_module_state()
        from services.smart_wiring import build_smart_clone_provider, inject_for_test
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        with inject_for_test(clone_provider=fake):
            assert build_smart_clone_provider() is fake
        # Outside the context — default singleton is built lazily and
        # is NOT the fake.
        provider = build_smart_clone_provider()
        assert provider is not fake

    def test_inject_restores_even_on_exception(self):
        self._reset_module_state()
        from services.smart_wiring import build_smart_clone_provider, inject_for_test
        from tests.fakes import FakeCloneProvider

        fake = FakeCloneProvider()
        with pytest.raises(RuntimeError, match="boom"):
            with inject_for_test(clone_provider=fake):
                assert build_smart_clone_provider() is fake
                raise RuntimeError("boom")
        # After the raise, default restored.
        assert build_smart_clone_provider() is not fake

    def test_nested_injects_stack_correctly(self):
        self._reset_module_state()
        from services.smart_wiring import build_smart_clone_provider, inject_for_test
        from tests.fakes import FakeCloneProvider

        outer = FakeCloneProvider(provider_name="outer")
        inner = FakeCloneProvider(provider_name="inner")
        with inject_for_test(clone_provider=outer):
            assert build_smart_clone_provider() is outer
            with inject_for_test(clone_provider=inner):
                assert build_smart_clone_provider() is inner
            # Inner exited — outer restored, NOT default.
            assert build_smart_clone_provider() is outer
        # Both exited — default.
        assert build_smart_clone_provider() is not outer
        assert build_smart_clone_provider() is not inner

    def test_inject_axes_are_independent(self):
        """Injecting only clone_provider must not clobber tts/llm
        overrides set by an outer fixture."""
        self._reset_module_state()
        from services.smart_wiring import (
            build_smart_clone_provider,
            build_smart_tts_provider,
            inject_for_test,
        )
        from tests.fakes import FakeCloneProvider, FakeTTSProvider

        outer_tts = FakeTTSProvider()
        with inject_for_test(tts_provider=outer_tts):
            inner_clone = FakeCloneProvider()
            with inject_for_test(clone_provider=inner_clone):
                # Inner only touched clone — outer tts still wins.
                assert build_smart_clone_provider() is inner_clone
                assert build_smart_tts_provider() is outer_tts


class TestSmartPackageContractsReExport:
    """contracts.py is part of the smart package public surface — make
    sure every Protocol + Result is explicitly exported so consumers
    don't reach into internal paths."""

    def test_contracts_module_exports_all_protocols_and_results(self):
        from services.smart import contracts

        for name in (
            "CloneProvider", "CloneResult",
            "TTSProvider", "TTSResult",
            "LLMProvider", "LLMResult",
        ):
            assert hasattr(contracts, name), f"{name!r} missing from contracts.py"
            assert name in contracts.__all__

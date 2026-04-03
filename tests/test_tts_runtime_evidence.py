"""TTS runtime evidence tests — three-layer proof structure.

Layer 1: Provider decision unit tests
    Verify _resolve_provider_decision() returns correct provider + source.

Layer 2: Mocked pipeline runtime evidence
    Verify [S4] TTS provider log line is emitted during pipeline run.

Layer 3: Runner / job events log capture
    Verify [S4] provider log lines flow into stored job events.

IMPORTANT — patch targets:
    tts_generator.py uses ``from services.tts.tts_strategy import
    get_tts_provider, get_tts_provider_for_job``.  This copies the function
    objects into the ``services.tts.tts_generator`` namespace.  To control
    what ``_resolve_provider_decision()`` actually calls at runtime, patches
    MUST target ``services.tts.tts_generator.get_tts_provider`` (and
    ``...get_tts_provider_for_job``), NOT the original module
    ``services.tts.tts_strategy.*``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.tts.tts_generator import TTSConfig, TTSGenerator

# The module whose local bindings we need to patch.
_GEN_MOD = "services.tts.tts_generator"


# ===================================================================
# Layer 1: Provider decision unit tests
# ===================================================================


class TestProviderDecision:
    """_resolve_provider_decision returns {provider, source}."""

    def _make_generator(self, *, job_record=None) -> TTSGenerator:
        return TTSGenerator(TTSConfig(api_key="test-key"), job_record=job_record)

    def test_job_record_provider_takes_priority(self, monkeypatch):
        """INV-3: per-job tts_provider wins over global default.

        Patch get_tts_provider_for_job so it returns exactly what the
        job_record says.  Patch get_tts_provider to a *different* value
        to prove it is NOT used when a job_record is present.
        """
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider_for_job",
            lambda job: job["tts_provider"],
        )
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "SHOULD_NOT_BE_USED",
        )
        gen = self._make_generator()
        decision = gen._resolve_provider_decision(
            job_record={"tts_provider": "cosyvoice"}
        )
        assert decision["provider"] == "cosyvoice"
        assert decision["source"] == "job_record"

    def test_global_default_when_no_job_record(self, monkeypatch):
        """When job_record is None, get_tts_provider() is called.

        Patch get_tts_provider to a sentinel to prove the path is taken.
        """
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "minimax",
        )
        gen = self._make_generator()
        decision = gen._resolve_provider_decision(job_record=None)
        assert decision["provider"] == "minimax"
        assert decision["source"] == "global_default"

    def test_patch_actually_controls_global_default_path(self, monkeypatch):
        """Verify the patch target is correct: changing the patched value
        changes the decision result.  If the patch targeted the wrong
        module, this test would still see the real legacy resolution."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "SENTINEL_PROVIDER",
        )
        gen = self._make_generator()
        decision = gen._resolve_provider_decision(job_record=None)
        # If patch is effective, provider must be exactly the sentinel.
        # If it's NOT effective, we'd get the real legacy value (minimax/cosyvoice/…).
        assert decision["provider"] == "SENTINEL_PROVIDER"

    def test_constructor_job_record_used_as_fallback(self, monkeypatch):
        """When generate_all passes no job_record, constructor default is used."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider_for_job",
            lambda job: job["tts_provider"],
        )
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "SHOULD_NOT_BE_USED",
        )
        gen = self._make_generator(job_record={"tts_provider": "cosyvoice"})
        decision = gen._resolve_provider_decision(job_record=gen._default_job_record)
        assert decision["provider"] == "cosyvoice"
        assert decision["source"] == "job_record"

    def test_object_style_job_record(self, monkeypatch):
        """Job record as SimpleNamespace (attribute access) works too."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider_for_job",
            lambda job: getattr(job, "tts_provider", None) or "fallback",
        )
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "SHOULD_NOT_BE_USED",
        )
        gen = self._make_generator()
        record = SimpleNamespace(tts_provider="volcengine")
        decision = gen._resolve_provider_decision(job_record=record)
        assert decision["provider"] == "volcengine"
        assert decision["source"] == "job_record"

    # --- paid / studio provider decision ---

    def test_paid_studio_job_record_selects_minimax(self, monkeypatch):
        """INV-2 at decision level: paid/studio job_record → minimax."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider_for_job",
            lambda job: job["tts_provider"],
        )
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "SHOULD_NOT_BE_USED",
        )
        gen = self._make_generator()
        decision = gen._resolve_provider_decision(
            job_record={"tts_provider": "minimax", "service_mode": "studio"}
        )
        assert decision["provider"] == "minimax"
        assert decision["source"] == "job_record"


# ===================================================================
# Layer 2: generate_all emits [S4] TTS provider log with source
# ===================================================================


class TestGenerateAllProviderLog:
    """generate_all() prints stable [S4] TTS provider evidence."""

    def _run_generate_all(self, monkeypatch, capsys, *, job_record):
        """Helper: run generate_all with empty segments, return captured stdout."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider_for_job",
            lambda job: (job["tts_provider"] if isinstance(job, dict)
                         else getattr(job, "tts_provider")),
        )
        gen = TTSGenerator(TTSConfig(api_key="test-key"))
        tmp_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "tts_evidence_test")
        os.makedirs(tmp_dir, exist_ok=True)
        gen.generate_all(segments=[], output_dir=tmp_dir, job_record=job_record)
        return capsys.readouterr()

    def test_free_express_runtime_log(self, capsys, monkeypatch):
        """[S4] log shows cosyvoice with source=job_record for free/express."""
        captured = self._run_generate_all(
            monkeypatch, capsys,
            job_record={"tts_provider": "cosyvoice", "service_mode": "express"},
        )
        assert "[S4] TTS provider: cosyvoice" in captured.out
        assert "(source: job_record)" in captured.out

    def test_paid_studio_runtime_log(self, capsys, monkeypatch):
        """[S4] log shows minimax with source=job_record for paid/studio."""
        captured = self._run_generate_all(
            monkeypatch, capsys,
            job_record={"tts_provider": "minimax", "service_mode": "studio"},
        )
        assert "[S4] TTS provider: minimax" in captured.out
        assert "(source: job_record)" in captured.out

    def test_no_job_record_runtime_log(self, capsys, monkeypatch):
        """[S4] log shows global_default when no job_record provided."""
        monkeypatch.setattr(
            f"{_GEN_MOD}.get_tts_provider",
            lambda *a, **kw: "minimax",
        )
        gen = TTSGenerator(TTSConfig(api_key="test-key"))
        tmp_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "tts_evidence_test")
        os.makedirs(tmp_dir, exist_ok=True)
        gen.generate_all(segments=[], output_dir=tmp_dir)
        captured = capsys.readouterr()
        assert "[S4] TTS provider: minimax" in captured.out
        assert "(source: global_default)" in captured.out


# ===================================================================
# Layer 3: Runner log capture — [S4] lines enter job events
# ===================================================================


class TestRunnerLogCapture:
    """process_runner._record_line persists [S4] lines as job events."""

    def test_s4_provider_log_stored_in_events(self, tmp_path):
        from services.jobs.store import JobStore
        from services.jobs.models import JobRecord

        store = JobStore(tmp_path / "jobs")
        job = JobRecord.from_dict({
            "job_id": "job_evidence_001",
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://youtube.example/watch?v=evidence",
            "output_target": "editor",
            "speakers": "auto",
            "status": "running",
            "current_stage": "draft",
            "created_at": "2026-04-03T00:00:00Z",
            "updated_at": "2026-04-03T00:00:00Z",
        })
        store.save_job(job)

        from services.jobs.process_runner import ProcessJobRunner

        runner = ProcessJobRunner(
            store=store,
            project_root=tmp_path,
            python_executable="python",
            popen_factory=MagicMock(),
            run_timeout_seconds=5,
        )

        # Simulate pipeline emitting [S4] TTS provider line
        runner._record_line("job_evidence_001", "[S4] TTS provider: cosyvoice (source: job_record)")

        events = store.load_events("job_evidence_001")
        s4_events = [e for e in events if "[S4] TTS provider:" in (e.message or "")]
        assert len(s4_events) >= 1, "Expected [S4] TTS provider line in job events"
        assert "cosyvoice" in s4_events[0].message

    def test_s4_paid_provider_log_stored_in_events(self, tmp_path):
        """Paid/studio [S4] minimax line also enters job events."""
        from services.jobs.store import JobStore
        from services.jobs.models import JobRecord

        store = JobStore(tmp_path / "jobs")
        job = JobRecord.from_dict({
            "job_id": "job_evidence_002",
            "job_type": "localize_video",
            "source_type": "youtube_url",
            "source_ref": "https://youtube.example/watch?v=evidence-paid",
            "output_target": "editor",
            "speakers": "auto",
            "status": "running",
            "current_stage": "draft",
            "created_at": "2026-04-03T00:00:00Z",
            "updated_at": "2026-04-03T00:00:00Z",
        })
        store.save_job(job)

        from services.jobs.process_runner import ProcessJobRunner

        runner = ProcessJobRunner(
            store=store,
            project_root=tmp_path,
            python_executable="python",
            popen_factory=MagicMock(),
            run_timeout_seconds=5,
        )

        runner._record_line("job_evidence_002", "[S4] TTS provider: minimax (source: job_record)")

        events = store.load_events("job_evidence_002")
        s4_events = [e for e in events if "[S4] TTS provider:" in (e.message or "")]
        assert len(s4_events) >= 1
        assert "minimax" in s4_events[0].message

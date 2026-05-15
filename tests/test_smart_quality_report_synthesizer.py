"""Codex 第三十八轮 P1: ``synthesize_quality_report_from_jsonl`` tests.

Decision log §P3-a scope-down says smart handoff jobs don't write
``smart_quality_report.json`` (handoff events are in
``smart_decisions.jsonl`` instead). The original P3-c renderer mapped
``quality_report_not_written`` 404 to "正在处理中" — misleading for
handoff jobs, which are actually waiting for the user to take over
via Studio.

Fix: ``services.smart.quality_report_synthesizer`` reads JSONL and
synthesizes a minimal-but-valid v1 quality_report payload when
``downgrade_handoff`` events exist. The Job API endpoint serves this
synthesized payload so the renderer shows ``status=downgraded_to_studio``
+ populated ``handoff_history``.

Pure-function tests below — no I/O beyond reading a fixture file the
test sets up.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _write_jsonl_events(audit_dir: Path, events: list[dict]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    target = audit_dir / "smart_decisions.jsonl"
    with target.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ===========================================================================
# Cycle 1 — returns None when no JSONL or no handoff events
# ===========================================================================


class TestSynthesizerNoHandoffEvents:

    def test_returns_none_when_jsonl_missing(self, tmp_path):
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        # don't create the directory at all
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is None

    def test_returns_none_when_jsonl_empty(self, tmp_path):
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        (audit_dir / "smart_decisions.jsonl").write_text("", encoding="utf-8")
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is None

    def test_returns_none_when_only_speaker_gate_no_handoff(self, tmp_path):
        """Pure speaker_gate without downgrade_handoff = job is still
        in-flight, not yet at terminal or handoff. Return None so the
        endpoint returns 404 (frontend shows "处理中")."""
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        _write_jsonl_events(audit_dir, [
            {
                "decision_type": "speaker_gate",
                "decision": "approved",
                "evidence": {"main_speaker_count": 1, "main_speaker_ids": ["a"]},
                "created_at": "2026-05-15T11:00:00+00:00",
            }
        ])
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is None

    def test_returns_none_when_jsonl_malformed(self, tmp_path):
        """Defensive — malformed JSONL must not crash the helper."""
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        (audit_dir / "smart_decisions.jsonl").write_text(
            "not json\n{also not\n", encoding="utf-8",
        )
        # Malformed line skipped; if no handoff event found, return None.
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is None


# ===========================================================================
# Cycle 2 — synthesizes valid v1 payload with handoff_history populated
# ===========================================================================


class TestSynthesizerHandoffPath:

    def test_single_handoff_event_synthesizes_minimal_payload(self, tmp_path):
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        _write_jsonl_events(audit_dir, [
            {
                "decision_type": "downgrade_handoff",
                "decision": "rejected",
                "reason_code": "voice_library_quota_unavailable",
                "evidence": {"foo": "bar"},
                "extra": {
                    "job_id": "job_handoff",
                    "user_id": "user_owner",
                    "handoff_stage": "voice_selection_review",
                },
                "created_at": "2026-05-15T11:30:00+00:00",
            }
        ])
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_handoff", user_id="user_owner",
        )
        assert result is not None
        assert result["schema_version"] == 1
        assert result["job_id"] == "job_handoff"
        assert result["user_id"] == "user_owner"
        assert result["service_mode"] == "smart"
        # Smart state reflects handoff.
        ss = result["smart_state_final"]
        assert ss["status"] == "downgraded_to_studio"
        assert ss["credits_policy"] == "pending_settle"
        assert ss["reason"] == "voice_library_quota_unavailable"
        # handoff_history has 1 entry.
        hh = result["handoff_history"]
        assert len(hh) == 1
        assert hh[0]["stage"] == "voice_selection_review"
        assert hh[0]["reason"] == "voice_library_quota_unavailable"
        assert hh[0]["occurred_at"] == "2026-05-15T11:30:00+00:00"
        # Other sections empty / null.
        assert result["voice_decisions"] == []
        assert result["translation_review"] is None
        assert result["retry_summary"]["rewrite_attempts_used"] == 0
        assert result["retry_summary"]["retts_attempts_used"] == 0

    def test_multiple_handoff_events_all_in_history(self, tmp_path):
        """When pipeline emits multiple downgrade_handoff events (rare
        but possible — e.g. fallback chain), all appear in
        handoff_history; smart_state_final.reason uses the LAST one."""
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        _write_jsonl_events(audit_dir, [
            {
                "decision_type": "downgrade_handoff",
                "decision": "rejected",
                "reason_code": "clone_sample_extraction_failed",
                "extra": {"handoff_stage": "voice_selection_review"},
                "created_at": "2026-05-15T11:30:00+00:00",
            },
            {
                "decision_type": "downgrade_handoff",
                "decision": "rejected",
                "reason_code": "clone_library_register_failed",
                "extra": {"handoff_stage": "voice_selection_review"},
                "created_at": "2026-05-15T11:31:00+00:00",
            }
        ])
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_multi", user_id="user_x",
        )
        assert result is not None
        assert len(result["handoff_history"]) == 2
        assert result["handoff_history"][0]["reason"] == "clone_sample_extraction_failed"
        assert result["handoff_history"][1]["reason"] == "clone_library_register_failed"
        # smart_state_final.reason is the LAST handoff.
        assert (
            result["smart_state_final"]["reason"]
            == "clone_library_register_failed"
        )

    def test_speaker_gate_event_populates_speaker_summary(self, tmp_path):
        """If a speaker_gate event preceded the handoff (typical for
        eligibility-pass → later handoff), use its evidence for
        speaker_summary so the renderer shows what was detected."""
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        _write_jsonl_events(audit_dir, [
            {
                "decision_type": "speaker_gate",
                "decision": "approved",
                "evidence": {
                    "main_speaker_count": 2,
                    "main_speaker_ids": ["speaker_a", "speaker_b"],
                    "excluded_speakers": [
                        {"speaker_id": "c", "reason": "non_speech"}
                    ],
                },
                "created_at": "2026-05-15T11:25:00+00:00",
            },
            {
                "decision_type": "downgrade_handoff",
                "decision": "rejected",
                "reason_code": "voice_library_quota_unavailable",
                "extra": {"handoff_stage": "voice_selection_review"},
                "created_at": "2026-05-15T11:30:00+00:00",
            }
        ])
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is not None
        sm = result["speaker_summary"]
        assert sm["main_speaker_count"] == 2
        assert sm["main_speaker_ids"] == ["speaker_a", "speaker_b"]
        assert len(sm["excluded_speakers"]) == 1

    def test_handoff_with_missing_handoff_stage_falls_back_to_unknown(
        self, tmp_path,
    ):
        """Defensive — older audit events may lack ``handoff_stage`` in
        extra. Synthesize ``"unknown"`` rather than crash."""
        from services.smart.quality_report_synthesizer import (
            synthesize_quality_report_from_jsonl,
        )
        audit_dir = tmp_path / "audit"
        _write_jsonl_events(audit_dir, [
            {
                "decision_type": "downgrade_handoff",
                "decision": "rejected",
                "reason_code": "some_reason",
                # no extra.handoff_stage
                "created_at": "2026-05-15T11:30:00+00:00",
            }
        ])
        result = synthesize_quality_report_from_jsonl(
            audit_dir, job_id="job_x", user_id="user_x",
        )
        assert result is not None
        assert result["handoff_history"][0]["stage"] == "unknown"


# ===========================================================================
# Cycle 3 — Job API endpoint serves synthesized payload for handoff jobs
# ===========================================================================


import threading
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _request_json(method: str, url: str):
    request = Request(url, method=method)
    request.add_header("Accept", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _spin_up_job_api(tmp_path: Path):
    from services.jobs.api import build_job_api_server
    from services.jobs.process_runner import ProcessJobRunner
    from services.jobs.service import JobService
    from services.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        popen_factory=lambda *_a, **_kw: None,
        run_timeout_seconds=5,
    )
    service = JobService(store=store, runner=runner)
    server = build_job_api_server(service=service, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    return service, base_url, server, thread


def _make_smart_handoff_job(
    tmp_path: Path, store, *, job_id: str, project_name: str,
    handoff_reason: str = "voice_library_quota_unavailable",
    handoff_stage: str = "voice_selection_review",
) -> Path:
    """Create a smart JobRecord + write a downgrade_handoff JSONL event
    but NO smart_quality_report.json (real handoff scenario)."""
    from services.jobs.models import JobRecord

    project_dir = tmp_path / "projects" / project_name
    audit = project_dir / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    _write_jsonl_events(audit, [
        {
            "decision_type": "downgrade_handoff",
            "decision": "rejected",
            "reason_code": handoff_reason,
            "extra": {"handoff_stage": handoff_stage},
            "created_at": "2026-05-15T11:30:00+00:00",
        }
    ])
    record = JobRecord(
        job_id=job_id,
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://yt.example/" + project_name,
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="waiting_for_review",
        current_stage="voice_selection_review",
        progress_message="handoff",
        created_at="2026-05-15T11:00:00+00:00",
        updated_at="2026-05-15T11:31:00+00:00",
        service_mode="smart",
        project_dir=str(project_dir.resolve(strict=False)),
    )
    store.save_job(record)
    return project_dir


class TestEndpointServesSynthesizedHandoffPayload:

    def test_handoff_job_returns_200_with_handoff_history(self, tmp_path):
        """The endpoint MUST return 200 + synthesized payload for
        smart jobs that hit handoff before terminal — NOT 404. This is
        the user-actionable signal that the job needs manual takeover.
        """
        service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            _make_smart_handoff_job(
                tmp_path, service.store,
                job_id="job_handoff_endpoint",
                project_name="job_handoff_endpoint",
                handoff_reason="clone_sample_extraction_failed",
                handoff_stage="voice_selection_review",
            )
            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_handoff_endpoint/smart-quality-report",
            )
            assert status == HTTPStatus.OK, (
                f"Expected 200 for handoff job; got {status}: {body}"
            )
            assert body["schema_version"] == 1
            assert body["smart_state_final"]["status"] == "downgraded_to_studio"
            assert (
                body["smart_state_final"]["reason"]
                == "clone_sample_extraction_failed"
            )
            assert len(body["handoff_history"]) == 1
            assert body["handoff_history"][0]["stage"] == "voice_selection_review"
            assert (
                body["handoff_history"][0]["reason"]
                == "clone_sample_extraction_failed"
            )
        finally:
            server.shutdown()

    def test_smart_job_with_no_audit_returns_404_in_flight(self, tmp_path):
        """Truly in-flight smart job — no quality_report.json and no
        JSONL events — still returns 404 quality_report_not_written so
        frontend shows the "处理中" hint."""
        from services.jobs.models import JobRecord

        service, base_url, server, _thread = _spin_up_job_api(tmp_path)
        try:
            project_dir = tmp_path / "projects" / "in_flight"
            (project_dir / "audit").mkdir(parents=True, exist_ok=True)
            # NO quality_report.json, NO smart_decisions.jsonl
            record = JobRecord(
                job_id="job_in_flight",
                job_type="localize_video",
                source_type="youtube_url",
                source_ref="https://yt.example/inflight",
                output_target="editor",
                speakers="auto",
                voice_a=None,
                voice_b=None,
                status="running",
                current_stage="speaker_review",
                progress_message="running",
                created_at="2026-05-15T11:00:00+00:00",
                updated_at="2026-05-15T11:05:00+00:00",
                service_mode="smart",
                project_dir=str(project_dir.resolve(strict=False)),
            )
            service.store.save_job(record)

            status, body = _request_json(
                "GET",
                f"{base_url}/jobs/job_in_flight/smart-quality-report",
            )
            assert status == HTTPStatus.NOT_FOUND
            assert body.get("error") == "quality_report_not_written"
        finally:
            server.shutdown()

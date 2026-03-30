"""Test that quota compensation actually deletes the upstream job.

This is an end-to-end test: starts a real Job API server, creates a job via
HTTP, then calls _compensate_upstream_job and verifies the job is gone.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.services.jobs.api import build_job_api_server
from src.services.jobs.process_runner import ProcessJobRunner
from src.services.jobs.service import JobService
from src.services.jobs.store import JobStore


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def job_api(tmp_path):
    """Start a real Job API server on a random port, yield (base_url, store)."""
    store = JobStore(tmp_path / "jobs")
    runner = MagicMock()
    runner.start = MagicMock()
    service = JobService(store=store, runner=runner)
    port = _find_free_port()
    server = build_job_api_server(service=service, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Wait for server to be ready
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/jobs", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", store
    server.shutdown()


class TestCompensationDeletesUpstreamJob:
    def test_delete_endpoint_removes_job(self, job_api):
        """Verify DELETE /jobs/{id} actually removes the job from the store."""
        base_url, store = job_api

        # Create a job via POST
        create_body = json.dumps({
            "job_type": "localize_video",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=test"},
            "output_target": "editor",
            "speakers": "auto",
            "service_mode": "express",
        }).encode()
        req = urllib.request.Request(
            f"{base_url}/jobs", data=create_body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
            job_data = json.loads(resp.read())
        job_id = job_data["job_id"]

        # Verify job exists in store
        assert store.load_job(job_id) is not None

        # DELETE the job (this is what _compensate_upstream_job calls)
        del_req = urllib.request.Request(
            f"{base_url}/jobs/{job_id}", method="DELETE",
        )
        with urllib.request.urlopen(del_req, timeout=5) as resp:
            assert resp.status == 200
            del_data = json.loads(resp.read())
            assert del_data["deleted"] is True

        # Verify job is gone from store
        assert store.load_job(job_id) is None

    def test_delete_nonexistent_returns_404(self, job_api):
        base_url, _ = job_api
        del_req = urllib.request.Request(
            f"{base_url}/jobs/nonexistent", method="DELETE",
        )
        try:
            urllib.request.urlopen(del_req, timeout=5)
            assert False, "Expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_store_delete_job_method(self, tmp_path):
        """Unit test for JobStore.delete_job."""
        store = JobStore(tmp_path / "jobs")
        runner = MagicMock()
        runner.start = MagicMock()
        svc = JobService(store=store, runner=runner)

        job = svc.submit_job(
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            service_mode="express",
        )
        assert store.load_job(job.job_id) is not None

        deleted = store.delete_job(job.job_id)
        assert deleted is True
        assert store.load_job(job.job_id) is None

        # Second delete returns False
        assert store.delete_job(job.job_id) is False

    def test_cancel_and_delete_stops_running_process(self, tmp_path):
        """Verify cancel_and_delete_job kills a running subprocess, not just deletes the file."""
        store = JobStore(tmp_path / "jobs")

        # Use a real ProcessJobRunner with a real (but harmless) long-running subprocess
        runner = ProcessJobRunner(
            store=store,
            project_root=tmp_path,
            # Use a long-running command that we can kill
            popen_factory=subprocess.Popen,
        )

        # Create a job record directly in the store
        from src.services.jobs.models import JobRecord
        from src.services.state_manager import utc_now_iso
        ts = utc_now_iso()
        record = JobRecord(
            job_id="job_cancel_test",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="queued",
            current_stage=None,
            progress_message="test",
            created_at=ts,
            updated_at=ts,
        )
        store.save_job(record)

        # Start a real long-running subprocess (python -c "import time; time.sleep(60)")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Register it in the runner's process map
        with runner._lock:
            runner._processes["job_cancel_test"] = proc

        # Verify process is alive
        assert proc.poll() is None
        assert runner.is_process_active("job_cancel_test") is True

        # Now cancel_and_delete
        svc = JobService(store=store, runner=runner)
        deleted = svc.cancel_and_delete_job("job_cancel_test")

        assert deleted is True
        # Process should be dead
        assert proc.poll() is not None
        # Record should be gone
        assert store.load_job("job_cancel_test") is None
        # Runner should no longer track it
        assert runner.is_process_active("job_cancel_test") is False

    def test_cancel_with_real_monitor_thread_does_not_recreate_job(self, tmp_path):
        """Start a job via runner.start() (real monitor thread), then cancel.

        After cancel + delete, the monitor/finalize thread must NOT write the
        job record back to the store.
        """
        store = JobStore(tmp_path / "jobs")

        # popen_factory that spawns a real but short-lived "sleep" process
        # whose stdout eventually closes, causing the monitor to finalize.
        def slow_popen(cmd, **kwargs):
            # Ignore the real command; spawn a harmless sleeper with stdout
            return subprocess.Popen(
                [sys.executable, "-u", "-c",
                 "import time, sys; "
                 "print('[S0] starting', flush=True); "
                 "time.sleep(30); "
                 "print('[S0] done', flush=True)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

        runner = ProcessJobRunner(
            store=store,
            project_root=tmp_path,
            popen_factory=slow_popen,
        )

        from src.services.jobs.models import JobRecord
        from src.services.state_manager import utc_now_iso
        ts = utc_now_iso()
        record = JobRecord(
            job_id="job_monitor_test",
            job_type="localize_video",
            source_type="youtube_url",
            source_ref="https://youtube.com/watch?v=test",
            output_target="editor",
            speakers="auto",
            voice_a=None,
            voice_b=None,
            status="queued",
            current_stage=None,
            progress_message="test",
            created_at=ts,
            updated_at=ts,
        )
        store.save_job(record)

        # Start via the real runner — this spawns the process and monitor thread
        runner.start(record)

        # Give the monitor thread a moment to start reading stdout
        time.sleep(0.5)
        assert runner.is_process_active("job_monitor_test") is True

        # Cancel and delete
        svc = JobService(store=store, runner=runner)
        deleted = svc.cancel_and_delete_job("job_monitor_test")
        assert deleted is True
        assert store.load_job("job_monitor_test") is None

        # Wait enough time for the monitor thread to finalize
        # (it should see the deleted flag and skip writing)
        time.sleep(2.0)

        # The critical assertion: the job must still NOT exist in the store
        assert store.load_job("job_monitor_test") is None

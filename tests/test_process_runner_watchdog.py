"""tests/test_process_runner_watchdog.py

watchdog wall-clock 超时测试。

覆盖三个场景：
1. watchdog 触发：子进程静默挂死（不输出）→ deadline 到期 kill → 任务 failed。
2. 正常完成：快速子进程正常退出 → timer.cancel() 被调用，不泄漏 timer。
3. _compute_deadline_seconds 接线：有源时长用分层表，无源时长回退 run_timeout_seconds。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from services.jobs.models import JobRecord
from services.jobs.process_runner import ProcessJobRunner, get_timeout_for_duration
from services.jobs.store import JobStore
from tests.job_test_helpers import FakePopenFactory, wait_for


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _make_job(**overrides) -> JobRecord:
    base = {
        "job_id": "job_wdog001",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.example/watch?v=wdog",
        "output_target": "editor",
        "speakers": "auto",
        "status": "queued",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return JobRecord.from_dict(base)


def _make_runner(
    tmp_path: Path,
    *,
    popen_factory=None,
    run_timeout_seconds: int = 30,
) -> ProcessJobRunner:
    store = JobStore(tmp_path / "jobs")
    return ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable=sys.executable,
        popen_factory=popen_factory or MagicMock(),
        run_timeout_seconds=run_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# 真实子进程辅助：用来测 watchdog 触发
# ---------------------------------------------------------------------------

class _BlockingPopen:
    """模拟一个保持 stdout 打开但不输出、长 sleep 的子进程（不依赖 POSIX API）。

    在 watchdog kill 时将 returncode 设为非 0 以触发 failed 路径。
    """

    def __init__(self) -> None:
        self._r, self._w = None, None
        self._returncode: int | None = None
        self._killed = threading.Event()
        # 用 os.pipe 模拟 stdout pipe（跨平台）
        import os
        self._r_fd, self._w_fd = os.pipe()
        import io
        self.stdout = io.open(self._r_fd, "r", encoding="utf-8", closefd=True)

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        # 等待 kill 事件（最多 timeout 秒）
        self._killed.wait(timeout=timeout)
        return self._returncode if self._returncode is not None else -9

    def kill(self) -> None:
        import os
        self._returncode = -9
        self._killed.set()
        try:
            os.close(self._w_fd)  # 关写端 → 读端 EOF → for-loop 退出
        except OSError:
            pass

    def terminate(self) -> None:
        self.kill()


class _BlockingPopenFactory:
    """返回 _BlockingPopen 的工厂，供 ProcessJobRunner 注入。"""

    def __init__(self) -> None:
        self.instance: _BlockingPopen | None = None

    def __call__(self, command, **kwargs) -> _BlockingPopen:
        self.instance = _BlockingPopen()
        return self.instance


# ---------------------------------------------------------------------------
# 场景 1：watchdog 触发
# ---------------------------------------------------------------------------

@pytest.mark.timeout(15)
def test_watchdog_kills_hanging_process_and_marks_failed(tmp_path: Path) -> None:
    """子进程保持 stdout 打开不输出（静默挂死），
    watchdog 1~2 秒后 kill，任务最终被标为 failed，测试不卡住。
    """
    factory = _BlockingPopenFactory()
    runner = _make_runner(tmp_path, popen_factory=factory, run_timeout_seconds=2)

    job = _make_job()
    runner.store.save_job(job)

    # 把 _compute_deadline_seconds monkeypatch 成 1 秒，加速测试
    runner._compute_deadline_seconds = lambda j: 1  # type: ignore[method-assign]

    started = runner.start(job)
    assert started.status == "running"

    # 等待任务被标为 failed（最多 8 秒）
    def _is_failed() -> bool:
        try:
            j = runner.store.require_job(job.job_id)
            return j.status == "failed"
        except Exception:
            return False

    wait_for(_is_failed, timeout_seconds=8, interval_seconds=0.1)

    final = runner.store.require_job(job.job_id)
    assert final.status == "failed", f"expected failed, got {final.status}"


# ---------------------------------------------------------------------------
# 场景 2：正常完成不受影响，timer.cancel() 被调用
# ---------------------------------------------------------------------------

def test_normal_completion_cancels_watchdog_timer(tmp_path: Path) -> None:
    """快速正常退出的子进程：确认 timer.cancel() 被调用，成功路径不变。"""
    factory = FakePopenFactory([{"lines": ["[S0] Ingesting", "[S6] Done"], "returncode": 0}])
    runner = _make_runner(tmp_path, popen_factory=factory, run_timeout_seconds=30)

    job = _make_job()
    runner.store.save_job(job)

    cancelled_timers: list[bool] = []

    original_timer_init = threading.Timer.__init__

    class _SpyTimer(threading.Timer):
        def cancel(self) -> None:
            cancelled_timers.append(True)
            super().cancel()

    with patch("services.jobs.process_runner.threading") as mock_threading:
        # 保持 threading.Event / threading.Lock 等正常工作
        mock_threading.Lock = threading.Lock
        mock_threading.Event = threading.Event
        mock_threading.Thread = threading.Thread
        mock_threading.Timer = _SpyTimer

        started = runner.start(job)
        assert started.status == "running"

        # 等待任务完成
        def _is_done() -> bool:
            try:
                j = runner.store.require_job(job.job_id)
                return j.status in ("succeeded", "failed")
            except Exception:
                return False

        wait_for(_is_done, timeout_seconds=5, interval_seconds=0.05)

    final = runner.store.require_job(job.job_id)
    assert final.status == "succeeded", f"expected succeeded, got {final.status}"
    assert len(cancelled_timers) >= 1, "watchdog timer.cancel() was never called"


# ---------------------------------------------------------------------------
# 场景 3：_compute_deadline_seconds 接线
# ---------------------------------------------------------------------------

def test_compute_deadline_uses_duration_tier_when_available(tmp_path: Path) -> None:
    """有 source_duration_seconds 时，deadline 来自分层表。"""
    runner = _make_runner(tmp_path, run_timeout_seconds=9999)

    # 10 分钟视频 → tier1 (≤30 min) = 2*3600
    job_short = _make_job(source_duration_seconds=600.0)  # 10 min
    assert runner._compute_deadline_seconds(job_short) == get_timeout_for_duration(10.0)
    assert runner._compute_deadline_seconds(job_short) == 2 * 3600

    # 90 分钟视频 → tier2 (30-120 min) = 6*3600
    job_mid = _make_job(source_duration_seconds=5400.0)  # 90 min
    assert runner._compute_deadline_seconds(job_mid) == get_timeout_for_duration(90.0)
    assert runner._compute_deadline_seconds(job_mid) == 6 * 3600

    # 150 分钟视频 → tier3 (>120 min) = 8*3600
    job_long = _make_job(source_duration_seconds=9000.0)  # 150 min
    assert runner._compute_deadline_seconds(job_long) == get_timeout_for_duration(150.0)
    assert runner._compute_deadline_seconds(job_long) == 8 * 3600


def test_compute_deadline_falls_back_to_run_timeout_when_no_duration(tmp_path: Path) -> None:
    """无 source_duration_seconds 或为 None 时，回退到 run_timeout_seconds。"""
    runner = _make_runner(tmp_path, run_timeout_seconds=1234)

    job_no_dur = _make_job()  # source_duration_seconds 默认 None
    assert runner._compute_deadline_seconds(job_no_dur) == 1234

    job_zero = _make_job(source_duration_seconds=0)
    assert runner._compute_deadline_seconds(job_zero) == 1234

    assert runner._compute_deadline_seconds(None) == 1234

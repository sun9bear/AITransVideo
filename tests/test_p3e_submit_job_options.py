"""P3e §2 (Option C) — submit_job 接受 gateway 预供 job_id + smart_state.

plan 2026-06-14-p3e2-preview-lane-design.md §2。smart 预览克隆需在 forward 前
用 job_id 做 reserve(task_id=job_id) + 把 reservation marker 塞 smart_state 一并
forward → pipeline _snap/finalizer 读得到（避免 job_id 由本服务 mint 导致的
"reservation 需 job_id 但 marker 需 forward 前"鸡蛋问题）。

本测试钉死 Job API 侧契约：job_id 严格校验形状（合法→用、非空非法→拒绝）、
smart_state dict 透传进 JobRecord。默认（不传）→ 既有行为字节级不变。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.job_test_helpers import FakePopenFactory
from services.jobs import ProcessJobRunner
from services.jobs.service import JobService, UnsupportedJobRequestError
from services.jobs.store import JobStore


def _build_service(tmp_path: Path) -> JobService:
    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner(
        store=store,
        project_root=tmp_path,
        python_executable="python",
        # submit_job 启动一个 subprocess/job；本套测试有的用例 loop 多次提交，
        # 备足 fake plans（每个 plan = 一个 no-op job run）。
        popen_factory=FakePopenFactory([{"lines": [], "returncode": 0} for _ in range(12)]),
        run_timeout_seconds=5,
    )
    return JobService(store=store, runner=runner)


_VALID = "job_" + "a" * 32  # 形状合法（job_ + 32 hex）


def test_supplied_valid_job_id_is_used(tmp_path: Path) -> None:
    """🔥 Option C：gateway 预供合法 job_id → submit_job 用它（reservation linkage
    成立——gateway 已用此 id reserve）。"""
    service = _build_service(tmp_path)
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=x",
        user_id="7",
        job_id=_VALID,
    )
    assert created.job_id == _VALID


def test_supplied_invalid_job_id_is_rejected(tmp_path: Path) -> None:
    """🔥 防注入：非空非法 job_id（含路径注入尝试）→ 拒绝，不静默换 id。"""
    service = _build_service(tmp_path)
    for bad in ("../../etc/passwd", "job_xyz", "job_" + "a" * 31, "JOB_" + "a" * 32):
        with pytest.raises(UnsupportedJobRequestError, match="invalid job_id"):
            service.submit_job(
                source_type="youtube_url",
                source_ref="https://youtube.example/watch?v=x",
                user_id="7",
                job_id=bad,
            )


def test_blank_supplied_job_id_mints_as_absent(tmp_path: Path) -> None:
    """空字符串/空白 job_id 等价于未传，保持默认 mint 行为。"""
    service = _build_service(tmp_path)
    for blank in ("", "  "):
        created = service.submit_job(
            source_type="youtube_url",
            source_ref="https://youtube.example/watch?v=x",
            user_id="7",
            job_id=blank,
        )
        assert created.job_id.startswith("job_") and len(created.job_id) == 4 + 32


def test_absent_job_id_mints_as_before(tmp_path: Path) -> None:
    """默认（不传 job_id）→ 本服务 mint（既有行为字节级不变）。"""
    service = _build_service(tmp_path)
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=x",
        user_id="7",
    )
    assert created.job_id.startswith("job_") and len(created.job_id) == 4 + 32


def test_supplied_smart_state_persisted(tmp_path: Path) -> None:
    """🔥 smart_state（reservation marker）透传进 JobRecord → pipeline _snap /
    mirror→finalizer 读得到。"""
    service = _build_service(tmp_path)
    marker = {"smart_clone_reservation_id": "res-123", "smart_clone_credit_reserved": True}
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=x",
        user_id="7",
        job_id=_VALID,
        smart_state=marker,
    )
    assert created.smart_state is not None
    assert created.smart_state.get("smart_clone_reservation_id") == "res-123"
    assert created.smart_state.get("smart_clone_credit_reserved") is True
    # 落库往返保真
    reloaded = service.require_job(created.job_id)
    assert reloaded.smart_state.get("smart_clone_reservation_id") == "res-123"


def test_absent_smart_state_is_none(tmp_path: Path) -> None:
    """默认（不传 smart_state）→ None（既有非 smart-preview job 行为不变）。"""
    service = _build_service(tmp_path)
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=x",
        user_id="7",
    )
    assert created.smart_state is None


def test_non_dict_smart_state_coerced_to_none(tmp_path: Path) -> None:
    """smart_state 非 dict（防御）→ None，不污染 JobRecord。"""
    service = _build_service(tmp_path)
    created = service.submit_job(
        source_type="youtube_url",
        source_ref="https://youtube.example/watch?v=x",
        user_id="7",
        smart_state="not-a-dict",  # type: ignore[arg-type]
    )
    assert created.smart_state is None

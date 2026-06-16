"""分片上传 sweeper loop 层测试 — plan 2026-06-11 §3.8（sweep_once 行为见 store 测试）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import chunked_upload_api as api
import chunked_upload_store as store
import chunked_upload_sweeper as sweeper
from chunked_upload_store import ChunkedLimits


@pytest.fixture()
def uploads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path / "runtime_logs"))
    monkeypatch.setattr(store, "HARD_MIN_CHUNK_BYTES", 1)
    return tmp_path


def _limits(**overrides) -> ChunkedLimits:
    base = dict(
        enabled=False,  # sweeper 不看 enabled——kill-switch 关闭也要清扫
        max_file_mb=2048, chunk_mb=64, per_user_active=5,
        per_user_inflight_gb=4, global_inflight_gb=20, daily_per_user_gb=8,
        disk_floor_gb=0, ttl_hours=24, ready_ttl_hours=6,
    )
    base.update(overrides)
    return ChunkedLimits(**base)


def test_sweep_once_runs_with_disabled_flag(uploads_env, monkeypatch):
    """kill-switch 关闭时清扫照常执行（磁盘残留回收不受开关影响）。"""
    monkeypatch.setattr(api, "resolve_chunked_limits", _limits)
    data = b"0123456789"
    st = store.init_upload(
        user_id="user-a", declared_size=len(data),
        declared_sha256=hashlib.sha256(data).hexdigest(),
        chunk_size=4, file_name="v.mp4",
        limits=_limits(enabled=True),
    )
    # 老化到超 TTL
    raw = store.load_state("user-a", st["upload_id"])
    raw["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    store._write_state(store._state_path("user-a", st["upload_id"]), raw)

    stats = sweeper.sweep_chunked_uploads_once()
    assert stats["expired_purged"] == 1
    assert store.load_state("user-a", st["upload_id"]) is None


def test_sweeper_loop_stop_event_and_audit_jsonl(uploads_env, monkeypatch):
    monkeypatch.setattr(sweeper, "INITIAL_DELAY_S", 0)
    monkeypatch.setattr(sweeper, "SWEEP_INTERVAL_S", 0)
    calls = {"n": 0}

    def fake_sweep():
        calls["n"] += 1
        return {"expired_purged": 1, "orphan_purged": 0}

    monkeypatch.setattr(sweeper, "sweep_chunked_uploads_once", fake_sweep)

    async def run():
        stop = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(sweeper.sweeper_loop(stop_event=stop), stopper())

    asyncio.run(run())
    assert calls["n"] >= 1
    audit = uploads_env / "runtime_logs" / "chunked_upload_sweeper.jsonl"
    assert audit.exists()
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "chunked_upload_sweeper"
    assert row["expired_purged"] == 1


def test_sweeper_tick_failure_does_not_crash_loop(uploads_env, monkeypatch):
    monkeypatch.setattr(sweeper, "INITIAL_DELAY_S", 0)
    monkeypatch.setattr(sweeper, "SWEEP_INTERVAL_S", 0)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("tick failed")

    monkeypatch.setattr(sweeper, "sweep_chunked_uploads_once", boom)

    async def run():
        stop = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(sweeper.sweeper_loop(stop_event=stop), stopper())

    asyncio.run(run())
    assert calls["n"] >= 2, "tick 异常后 loop 必须继续"


def test_main_wires_sweeper_and_router():
    """main.py 必须 include chunked_upload_router 并启动/关闭 sweeper。"""
    from pathlib import Path

    main_src = (
        Path(__file__).resolve().parents[1] / "gateway" / "main.py"
    ).read_text(encoding="utf-8")
    assert "from chunked_upload_api import router as chunked_upload_router" in main_src
    assert "app.include_router(chunked_upload_router)" in main_src
    assert "chunked_upload_sweeper" in main_src
    assert '"chunked_upload_sweeper_task"' in main_src, "shutdown cancel 列表必须含 sweeper task"

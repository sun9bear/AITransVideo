"""分片上传 store 层测试 — plan 2026-06-11 §5 测试矩阵（状态机/并发/限额/完整性/claim/续传）。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import chunked_upload_store as store
from chunked_upload_store import ChunkedLimits, ChunkedUploadError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def uploads_env(tmp_path, monkeypatch):
    """隔离 uploads 根目录 + 放开多片小文件测试用的最小片限制。"""
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(store, "HARD_MIN_CHUNK_BYTES", 1)
    return tmp_path


def make_limits(**overrides) -> ChunkedLimits:
    base = dict(
        enabled=True,
        max_file_mb=2048,
        chunk_mb=64,
        per_user_active=2,
        per_user_inflight_gb=4,
        global_inflight_gb=20,
        daily_per_user_gb=8,
        disk_floor_gb=0,
        ttl_hours=24,
        ready_ttl_hours=6,
    )
    base.update(overrides)
    return ChunkedLimits(**base)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def init_for(data: bytes, *, user_id="user-a", chunk_size=4, limits=None, name="v.mp4"):
    return store.init_upload(
        user_id=user_id,
        declared_size=len(data),
        declared_sha256=sha256_hex(data),
        chunk_size=chunk_size,
        file_name=name,
        limits=limits or make_limits(),
    )


def put_part(user_id: str, upload_id: str, state: dict, data: bytes, n: int) -> dict:
    """模拟路由层完成流式落盘后的 commit（store 层契约）。"""
    chunk_size = int(state["chunk_size"])
    piece = data[n * chunk_size:(n + 1) * chunk_size]
    updir = store.upload_dir(user_id, upload_id)
    updir.mkdir(parents=True, exist_ok=True)
    tmp = updir / f"part_{n:05d}.test.tmp"
    tmp.write_bytes(piece)
    return store.commit_part(
        user_id=user_id,
        upload_id=upload_id,
        part_index=n,
        tmp_path=tmp,
        actual_size=len(piece),
        actual_sha256=sha256_hex(piece),
    )


def upload_all(data: bytes, *, user_id="user-a", chunk_size=4, limits=None) -> dict:
    state = init_for(data, user_id=user_id, chunk_size=chunk_size, limits=limits)
    uid = state["upload_id"]
    for n in range(state["total_parts"]):
        state = put_part(user_id, uid, state, data, n)
    return store.complete_upload(user_id=user_id, upload_id=uid, limits=limits or make_limits())


# ---------------------------------------------------------------------------
# init 校验 / 限额
# ---------------------------------------------------------------------------


def test_init_creates_receiving_state(uploads_env):
    data = b"0123456789"
    state = init_for(data)
    assert store.UPLOAD_ID_RE.match(state["upload_id"])
    assert state["state"] == store.STATE_RECEIVING
    assert state["total_parts"] == 3
    assert state["resumed"] is False
    on_disk = store.load_state("user-a", state["upload_id"])
    assert on_disk is not None and on_disk["declared_size"] == 10


def test_init_rejects_bad_sha256_and_size(uploads_env):
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="u", declared_size=10, declared_sha256="xyz",
            chunk_size=4, file_name="f", limits=make_limits(),
        )
    assert e.value.code == "invalid_sha256"
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="u", declared_size=0, declared_sha256="a" * 64,
            chunk_size=4, file_name="f", limits=make_limits(),
        )
    assert e.value.code == "invalid_size"


def test_init_rejects_over_max_file(uploads_env):
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="u", declared_size=2 * 1024 * 1024,
            declared_sha256="a" * 64, chunk_size=1024 * 1024,
            file_name="f", limits=make_limits(max_file_mb=1),
        )
    assert e.value.status_code == 413 and e.value.code == "over_limit"


def test_init_rejects_chunk_over_80mb(uploads_env):
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="u", declared_size=100, declared_sha256="a" * 64,
            chunk_size=81 * 1024 * 1024, file_name="f", limits=make_limits(),
        )
    assert e.value.code == "invalid_chunk_size"


def test_init_rejects_too_many_parts(uploads_env, monkeypatch):
    monkeypatch.setattr(store, "HARD_MAX_TOTAL_PARTS", 2)
    with pytest.raises(ChunkedUploadError) as e:
        init_for(b"0123456789", chunk_size=2)
    assert e.value.code == "invalid_chunk_size"


def test_per_user_active_limit(uploads_env):
    limits = make_limits(per_user_active=2)
    init_for(b"aaaa", limits=limits)
    init_for(b"bbbb", limits=limits)
    with pytest.raises(ChunkedUploadError) as e:
        init_for(b"cccc", limits=limits)
    assert e.value.status_code == 429 and e.value.code == "too_many_active"
    # 不影响其他用户
    init_for(b"dddd", user_id="user-b", limits=limits)


def test_inflight_gb_limits(uploads_env, monkeypatch):
    # 把 GB 单位换算后用小数字验证逻辑：1GB=2^30，难直接造；
    # 改为 monkeypatch 声明大小逼近——直接注入 in-flight state 文件。
    limits = make_limits(per_user_inflight_gb=1, per_user_active=10)
    big = 800 * 1024 * 1024  # 0.78 GB
    store.init_upload(
        user_id="user-a", declared_size=big, declared_sha256="a" * 64,
        chunk_size=64 * 1024 * 1024, file_name="f1", limits=limits,
    )
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="user-a", declared_size=big, declared_sha256="b" * 64,
            chunk_size=64 * 1024 * 1024, file_name="f2", limits=limits,
        )
    assert e.value.code == "user_inflight_exceeded"
    # 全局：另一个用户也会被全局闸拦住
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="user-b", declared_size=big, declared_sha256="c" * 64,
            chunk_size=64 * 1024 * 1024, file_name="f3", limits=make_limits(
                global_inflight_gb=1, per_user_active=10,
            ),
        )
    assert e.value.code == "global_inflight_exceeded"


def test_daily_quota_counts_even_after_abort(uploads_env):
    limits = make_limits(daily_per_user_gb=1, per_user_active=10)
    big = 700 * 1024 * 1024
    st = store.init_upload(
        user_id="user-a", declared_size=big, declared_sha256="a" * 64,
        chunk_size=64 * 1024 * 1024, file_name="f1", limits=limits,
    )
    store.abort_upload(user_id="user-a", upload_id=st["upload_id"])
    # 声明即计："曾经发生过即算"，放弃不退配额
    with pytest.raises(ChunkedUploadError) as e:
        store.init_upload(
            user_id="user-a", declared_size=big, declared_sha256="b" * 64,
            chunk_size=64 * 1024 * 1024, file_name="f2", limits=limits,
        )
    assert e.value.code == "daily_quota_exceeded"


def test_disk_reserve_507_and_inflight_accounting(uploads_env, monkeypatch):
    # 固定可用空间：只够一个 2S reserve
    size = 100
    monkeypatch.setattr(store, "_disk_free_bytes", lambda _p: 2 * size + 50)
    limits = make_limits(disk_floor_gb=0, per_user_active=10)
    init_for(b"x" * size, limits=limits)  # need 200 ≤ 250 → 过
    with pytest.raises(ChunkedUploadError) as e:
        # 第二个 init 看到第一个的 in-flight 余量 200 → need 400 > 250 → 507
        store.init_upload(
            user_id="user-a", declared_size=size, declared_sha256="b" * 64,
            chunk_size=size, file_name="f2", limits=limits,
        )
    assert e.value.status_code == 507


def test_concurrent_double_init_serialized_by_reserve_lock(uploads_env, monkeypatch):
    size = 100
    monkeypatch.setattr(store, "_disk_free_bytes", lambda _p: 2 * size + 50)
    limits = make_limits(disk_floor_gb=0, per_user_active=10)
    results: list = [None, None]

    def run(i, sha):
        try:
            results[i] = store.init_upload(
                user_id="user-a", declared_size=size, declared_sha256=sha,
                chunk_size=size, file_name=f"f{i}", limits=limits,
            )
        except ChunkedUploadError as exc:
            results[i] = exc

    t1 = threading.Thread(target=run, args=(0, "a" * 64))
    t2 = threading.Thread(target=run, args=(1, "b" * 64))
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, ChunkedUploadError)]
    assert len(oks) == 1 and len(errs) == 1, "并发双 init 必须恰好一个通过"
    assert errs[0].status_code == 507


# ---------------------------------------------------------------------------
# 续传复用（§3.5）
# ---------------------------------------------------------------------------


def test_resume_same_tuple_returns_same_upload(uploads_env):
    data = b"0123456789"
    s1 = init_for(data)
    put_part("user-a", s1["upload_id"], s1, data, 0)
    s2 = init_for(data)
    assert s2["upload_id"] == s1["upload_id"]
    assert s2["resumed"] is True
    assert store.received_part_indices(s2) == [0]


def test_resume_does_not_cross_users(uploads_env):
    data = b"0123456789"
    s1 = init_for(data, user_id="user-a")
    s2 = init_for(data, user_id="user-b")
    assert s1["upload_id"] != s2["upload_id"]


def test_resume_requires_exact_tuple(uploads_env):
    data = b"0123456789"
    s1 = init_for(data, chunk_size=4)
    s2 = init_for(data, chunk_size=5)  # chunk_size 不同 → 新 upload
    assert s1["upload_id"] != s2["upload_id"]


# ---------------------------------------------------------------------------
# part 提交 / 状态机
# ---------------------------------------------------------------------------


def test_commit_part_rejects_size_mismatch_and_deletes_tmp(uploads_env):
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    updir = store.upload_dir("user-a", uid)
    tmp = updir / "part_00000.x.tmp"
    tmp.write_bytes(b"123")  # 期望 4 字节
    with pytest.raises(ChunkedUploadError) as e:
        store.commit_part(
            user_id="user-a", upload_id=uid, part_index=0,
            tmp_path=tmp, actual_size=3, actual_sha256=sha256_hex(b"123"),
        )
    assert e.value.code == "part_size_mismatch"
    assert not tmp.exists()


def test_tmp_residue_not_counted_as_valid_part(uploads_env):
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    updir = store.upload_dir("user-a", uid)
    (updir / "part_00001.dead.tmp").write_bytes(b"zzzz")
    fresh = store.load_state("user-a", uid)
    assert store.received_part_indices(fresh) == []
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(user_id="user-a", upload_id=uid, limits=make_limits())
    assert e.value.code == "missing_parts"


def test_part_reupload_overwrites_in_receiving(uploads_env):
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    put_part("user-a", uid, st, data, 0)
    st2 = put_part("user-a", uid, st, data, 0)  # 重传同片 = 覆盖
    assert store.received_part_indices(st2) == [0]


def test_complete_full_flow_and_idempotency(uploads_env, tmp_path):
    data = b"hello world, chunked!"
    final = upload_all(data, chunk_size=5)
    assert final["state"] == store.STATE_READY
    final_path = Path(final["final_path"])
    assert final_path.is_file()
    assert final_path.read_bytes() == data
    assert sha256_hex(final_path.read_bytes()) == sha256_hex(data)
    # 分片已删
    updir = store.upload_dir("user-a", final["upload_id"])
    assert not list(updir.glob("part_*"))
    # final 落在 uploads/{user}/ 下
    assert final_path.parent == store.uploads_root() / "user-a"
    # 幂等：再次 complete 返回同一 ref
    again = store.complete_upload(
        user_id="user-a", upload_id=final["upload_id"], limits=make_limits(),
    )
    assert again["final_path"] == final["final_path"]
    assert again["state"] == store.STATE_READY


def test_complete_missing_parts_409(uploads_env):
    data = b"0123456789"
    st = init_for(data)
    put_part("user-a", st["upload_id"], st, data, 0)
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(user_id="user-a", upload_id=st["upload_id"], limits=make_limits())
    assert e.value.status_code == 409 and e.value.code == "missing_parts"


def test_complete_sha256_mismatch_goes_failed_integrity_and_clears_parts(uploads_env):
    data = b"0123456789"
    st = store.init_upload(
        user_id="user-a", declared_size=len(data),
        declared_sha256="f" * 64,  # 故意声明错哈希
        chunk_size=4, file_name="v.mp4", limits=make_limits(),
    )
    uid = st["upload_id"]
    for n in range(st["total_parts"]):
        st = put_part("user-a", uid, st, data, n)
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(user_id="user-a", upload_id=uid, limits=make_limits())
    assert e.value.status_code == 422 and e.value.code == "sha256_mismatch"
    after = store.load_state("user-a", uid)
    assert after["state"] == store.STATE_FAILED_INTEGRITY
    # 分片清空——不留满位图死循环
    assert after["parts"] == {}
    assert not list(store.upload_dir("user-a", uid).glob("part_*"))
    # failed_integrity 拒绝继续收片
    tmp = store.upload_dir("user-a", uid) / "part_00000.y.tmp"
    tmp.write_bytes(data[:4])
    with pytest.raises(ChunkedUploadError) as e:
        store.commit_part(
            user_id="user-a", upload_id=uid, part_index=0,
            tmp_path=tmp, actual_size=4, actual_sha256=sha256_hex(data[:4]),
        )
    assert e.value.status_code == 409
    # 但允许用户 DELETE 清盘
    store.abort_upload(user_id="user-a", upload_id=uid)
    assert store.load_state("user-a", uid) is None


def test_completing_state_blocks_part_and_reports_in_progress(uploads_env):
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    for n in range(st["total_parts"]):
        st = put_part("user-a", uid, st, data, n)
    # 人为置 completing（模拟另一进程合并中）
    raw = store.load_state("user-a", uid)
    raw["state"] = store.STATE_COMPLETING
    store._save_state("user-a", uid, raw)
    tmp = store.upload_dir("user-a", uid) / "part_00000.z.tmp"
    tmp.write_bytes(data[:4])
    with pytest.raises(ChunkedUploadError) as e:
        store.commit_part(
            user_id="user-a", upload_id=uid, part_index=0,
            tmp_path=tmp, actual_size=4, actual_sha256=sha256_hex(data[:4]),
        )
    assert e.value.status_code == 409
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(user_id="user-a", upload_id=uid, limits=make_limits())
    assert e.value.status_code == 202 and e.value.code == "in_progress"


def test_complete_second_disk_precheck_507_reverts_nothing(uploads_env, monkeypatch):
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    for n in range(st["total_parts"]):
        st = put_part("user-a", uid, st, data, n)
    monkeypatch.setattr(store, "_disk_free_bytes", lambda _p: 1)
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(
            user_id="user-a", upload_id=uid,
            limits=make_limits(disk_floor_gb=1),
        )
    assert e.value.status_code == 507
    after = store.load_state("user-a", uid)
    assert after["state"] == store.STATE_RECEIVING  # 分片保留可重试


def test_complete_finalize_failure_reverts_to_receiving_and_is_retryable(
    uploads_env, monkeypatch,
):
    """Codex review 2026-06-11 P2 回归：completing 落盘后 finalization
    （mkdir/改名/状态写盘）任何意外异常必须回 receiving + 分片完整保留，
    不许卡死在 completing（202 死循环）也不许留孤儿终文件。"""
    data = b"0123456789"
    st = init_for(data)
    uid = st["upload_id"]
    for n in range(st["total_parts"]):
        st = put_part("user-a", uid, st, data, n)

    original_finalize = store._finalize_move

    def boom(_tmp, _final):
        raise RuntimeError("disk glitch during finalize")

    monkeypatch.setattr(store, "_finalize_move", boom)
    with pytest.raises(ChunkedUploadError) as e:
        store.complete_upload(user_id="user-a", upload_id=uid, limits=make_limits())
    assert e.value.status_code == 500 and e.value.code == "merge_failed"

    after = store.load_state("user-a", uid)
    assert after["state"] == store.STATE_RECEIVING, "不许卡死在 completing"
    assert "finalize_error" in (after["failure_reason"] or "")
    # 分片完整保留 → 故障恢复后可直接重试 complete
    assert store.received_part_indices(after) == list(range(after["total_parts"]))
    # 无孤儿终文件 / merged.tmp 残留
    assert not (store.uploads_root() / "user-a").exists() or not list(
        (store.uploads_root() / "user-a").glob(f"{uid[:12]}_*")
    )
    assert not (store.upload_dir("user-a", uid) / "merged.tmp").exists()

    # 故障消除后重试成功
    monkeypatch.setattr(store, "_finalize_move", original_finalize)
    final = store.complete_upload(user_id="user-a", upload_id=uid, limits=make_limits())
    assert final["state"] == store.STATE_READY
    assert Path(final["final_path"]).read_bytes() == data


def test_concurrent_double_complete_single_merge(uploads_env):
    data = b"0123456789" * 100
    st = init_for(data, chunk_size=256)
    uid = st["upload_id"]
    for n in range(st["total_parts"]):
        st = put_part("user-a", uid, st, data, n)
    results: list = [None, None]

    def run(i):
        try:
            results[i] = store.complete_upload(
                user_id="user-a", upload_id=uid, limits=make_limits(),
            )
        except ChunkedUploadError as exc:
            results[i] = exc

    t1 = threading.Thread(target=run, args=(0,))
    t2 = threading.Thread(target=run, args=(1,))
    t1.start(); t2.start(); t1.join(); t2.join()
    # 锁串行化：两个都最终拿到 ready（后到者吃幂等分支）或一个 202
    finals = [r for r in results if isinstance(r, dict)]
    assert len(finals) >= 1
    paths = {r["final_path"] for r in finals}
    assert len(paths) == 1
    assert Path(next(iter(paths))).read_bytes() == data


# ---------------------------------------------------------------------------
# abort（R5）
# ---------------------------------------------------------------------------


def test_abort_only_from_receiving_or_failed(uploads_env):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    with pytest.raises(ChunkedUploadError) as e:
        store.abort_upload(user_id="user-a", upload_id=final["upload_id"])
    assert e.value.status_code == 409
    st = init_for(b"zzzz")
    store.abort_upload(user_id="user-a", upload_id=st["upload_id"])
    assert store.load_state("user-a", st["upload_id"]) is None


# ---------------------------------------------------------------------------
# opaque ref / claim（§3.10 / §3.8）
# ---------------------------------------------------------------------------


def test_parse_chunked_source_value():
    assert store.parse_chunked_source_value("https://x") is None
    assert store.parse_chunked_source_value("/abs/path.mp4") is None
    assert store.parse_chunked_source_value("chunked:zzz") == ""
    assert store.parse_chunked_source_value("chunked:" + "a" * 32) == "a" * 32
    # 路径穿越尝试在 regex 处归零
    assert store.parse_chunked_source_value("chunked:../etc/passwd") == ""


def test_resolve_ready_upload_ownership_and_state(uploads_env):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    assert store.resolve_ready_upload(user_id="user-a", upload_id=uid) == final["final_path"]
    # 非本人 → None
    assert store.resolve_ready_upload(user_id="user-b", upload_id=uid) is None
    # 非 ready → None
    st = init_for(b"zzzz")
    assert store.resolve_ready_upload(user_id="user-a", upload_id=st["upload_id"]) is None


def test_resolve_ready_upload_refuses_path_outside_uploads(uploads_env, tmp_path):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    # 篡改 final_path 指向 uploads 根外（深度防御）
    raw = store.load_state("user-a", uid)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    raw["final_path"] = str(outside)
    store._save_state("user-a", uid, raw)
    assert store.resolve_ready_upload(user_id="user-a", upload_id=uid) is None


def test_claim_lifecycle(uploads_env):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    assert store.claim_upload(user_id="user-a", upload_id=uid, job_id="job-1") is True
    # 幂等：同 job 重复 claim
    assert store.claim_upload(user_id="user-a", upload_id=uid, job_id="job-1") is True
    # 不同 job → False
    assert store.claim_upload(user_id="user-a", upload_id=uid, job_id="job-2") is False
    # receiving 态不能 claim
    st = init_for(b"zzzz")
    assert store.claim_upload(user_id="user-a", upload_id=st["upload_id"], job_id="j") is False


# ---------------------------------------------------------------------------
# sweep_once（§3.8）
# ---------------------------------------------------------------------------


def _age_state(user_id: str, upload_id: str, hours: float) -> None:
    raw = store.load_state(user_id, upload_id)
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    raw["updated_at"] = old.isoformat()
    store._write_state(store._state_path(user_id, upload_id), raw)


def test_sweep_expires_stale_receiving(uploads_env):
    st = init_for(b"0123456789")
    _age_state("user-a", st["upload_id"], 25)
    stats = store.sweep_once(make_limits(ttl_hours=24))
    assert stats["expired_purged"] == 1
    assert store.load_state("user-a", st["upload_id"]) is None


def test_sweep_keeps_fresh_receiving(uploads_env):
    st = init_for(b"0123456789")
    stats = store.sweep_once(make_limits(ttl_hours=24))
    assert stats["expired_purged"] == 0
    assert store.load_state("user-a", st["upload_id"]) is not None


def test_sweep_removes_orphan_dir(uploads_env):
    orphan = store.chunked_root() / "user-a" / ("c" * 32)
    orphan.mkdir(parents=True)
    (orphan / "part_00000").write_bytes(b"junk")
    stats = store.sweep_once(make_limits())
    assert stats["orphan_purged"] == 1
    assert not orphan.exists()


def test_sweep_deletes_unclaimed_ready_after_ttl(uploads_env):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    _age_state("user-a", uid, 7)
    stats = store.sweep_once(make_limits(ready_ttl_hours=6))
    assert stats["ready_unclaimed_purged"] == 1
    assert not Path(final["final_path"]).exists()
    assert store.load_state("user-a", uid) is None


def test_sweep_keeps_claimed_ready_final_file(uploads_env):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    store.claim_upload(user_id="user-a", upload_id=uid, job_id="job-9")
    _age_state("user-a", uid, 7)
    stats = store.sweep_once(make_limits(ready_ttl_hours=6))
    assert stats["ready_claimed_state_cleaned"] == 1
    # 终文件归 uploads 生命周期管理，不删
    assert Path(final["final_path"]).exists()
    assert store.load_state("user-a", uid) is None


def test_sweep_guard_refuses_final_path_outside_uploads(uploads_env, tmp_path):
    data = b"0123456789"
    final = upload_all(data, chunk_size=4)
    uid = final["upload_id"]
    # uploads 根 = {tmp_path}/uploads；tmp_path 直下不在根内 → 守卫生效
    outside = tmp_path / "sweeper_guard_outside.bin"
    outside.write_bytes(b"precious")
    raw = store.load_state("user-a", uid)
    raw["final_path"] = str(outside)
    store._write_state(store._state_path("user-a", uid), raw)
    _age_state("user-a", uid, 7)
    store.sweep_once(make_limits(ready_ttl_hours=6))
    assert outside.exists(), "uploads 根外的路径绝不能被 sweeper 删除"


def test_sweep_purges_old_usage_days(uploads_env):
    old_day = store.chunked_root() / "_usage" / "2020-01-01"
    old_day.mkdir(parents=True)
    (old_day / "user-a.json").write_text('{"bytes": 1}', encoding="utf-8")
    stats = store.sweep_once(make_limits())
    assert stats["usage_days_purged"] == 1
    assert not old_day.exists()

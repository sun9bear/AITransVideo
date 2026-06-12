"""APF 匿名 Express T2 — 配额三层计数守卫.

plan docs/plans/2026-06-12-anonymous-express-preview-plan.md §B（v4）：

1. **入口总闸（不动）**：既有 global/per-scope 计数器 key 形状、写入路径
   逐字节不变——每次 intake 无论 lane 照常累加（天然跨 lane 总闸）。
2. **per-scope per-mode（新增）**：scope_key 带 ``:mode:{lane}`` 后缀 +
   mode 列落 lane；ip/device/source × {free,express} 各 1 次/日（D3）。
3. **express 全局子闸（新增）**：``anonymous_express_daily_global_cap``
   = express 独立 global 计数行，复用 increment-and-check 原子路径。

判定顺序：总闸 → lane 子闸 → per-scope per-mode；任一拒即拒；
拒绝不落计数（denial 时回滚本次已 acquire 的行）。零 SUM。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import anonymous_preview_intake_wiring as wiring  # noqa: E402
from anonymous_preview_intake_wiring import (  # noqa: E402
    ANON_PREVIEW_COUNTER_SCOPE,
    PER_SCOPE_PER_MODE_DAILY_CAP,
    LaneAwareCounterStore,
    express_subgate_key,
    mode_scope_key,
    peek_counter_keys,
    peek_mode_counter_keys,
)


class _MemStore:
    """内存版 CounterStore（协议同 PgRateLimitCounterStore）。"""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.acquired: list[tuple[str, int]] = []
        self.decremented: list[str] = []

    def get(self, key: str) -> int:
        return self.counts.get(key, 0)

    def increment(self, key: str) -> int:
        self.counts[key] = self.get(key) + 1
        return self.counts[key]

    def try_acquire(self, key: str, cap: int):
        self.acquired.append((key, cap))
        cur = self.get(key)
        if cur >= cap:
            return (False, cur)
        self.counts[key] = cur + 1
        return (True, cur + 1)

    def decrement(self, key: str) -> int:
        self.decremented.append(key)
        self.counts[key] = max(0, self.get(key) - 1)
        return self.counts[key]


def _wrapper(store: _MemStore, lane: str, express_cap: int = 50):
    return LaneAwareCounterStore(
        store, store, lane=lane, express_global_cap=express_cap
    )


DAY = "2026-06-12"
G_KEY = f"global:{DAY}"
IP_KEY = f"ip:abcd1234:{DAY}"


# ---------------------------------------------------------------------------
# A. key 形状（单一推导落点——AD-8 bug ⑤ 教训）
# ---------------------------------------------------------------------------


class TestKeyShapes:
    def test_mode_scope_key_shape(self):
        assert mode_scope_key(IP_KEY, "express") == f"{IP_KEY}:mode:express"
        assert mode_scope_key(IP_KEY, "free") == f"{IP_KEY}:mode:free"

    def test_express_subgate_key_shape(self):
        assert express_subgate_key(DAY) == f"global:{DAY}:mode:express"

    def test_peek_mode_counter_keys_match_wrapper_derivation(self):
        """peek 侧推导必须与权威 wrapper 侧逐字节一致。"""
        secret = "s" * 32
        _g, ip_legacy = peek_counter_keys("203.0.113.7", DAY, secret=secret)
        exp_subgate, ip_mode = peek_mode_counter_keys(
            "203.0.113.7", DAY, "express", secret=secret
        )
        assert exp_subgate == express_subgate_key(DAY)
        assert ip_mode == mode_scope_key(ip_legacy, "express")

    def test_per_scope_per_mode_cap_is_one(self):
        """D3：per-scope×mode = 1 次/日（plan §B 第 2 层）。"""
        assert PER_SCOPE_PER_MODE_DAILY_CAP == 1


# ---------------------------------------------------------------------------
# B. 既有计数器逐字节不变（总闸语义）
# ---------------------------------------------------------------------------


class TestLegacyLayerUntouched:
    def test_legacy_keys_passed_verbatim_free(self):
        store = _MemStore()
        w = _wrapper(store, "free")
        w.try_acquire(G_KEY, 500)
        w.try_acquire(IP_KEY, 3)
        legacy_calls = [k for k, _ in store.acquired if ":mode:" not in k]
        assert legacy_calls == [G_KEY, IP_KEY]
        # caps 原样透传
        assert (G_KEY, 500) in store.acquired
        assert (IP_KEY, 3) in store.acquired

    def test_legacy_keys_passed_verbatim_express(self):
        """express intake 照常累加既有计数器（天然跨 lane 总闸）。"""
        store = _MemStore()
        w = _wrapper(store, "express")
        w.try_acquire(G_KEY, 500)
        assert store.counts[G_KEY] == 1

    def test_free_global_has_no_subgate(self):
        """free lane 的 global key 无子闸——只有 express 有。"""
        store = _MemStore()
        w = _wrapper(store, "free")
        w.try_acquire(G_KEY, 500)
        assert all(":mode:" not in k for k, _ in store.acquired)


# ---------------------------------------------------------------------------
# C. express 全局子闸
# ---------------------------------------------------------------------------


class TestExpressSubgate:
    def test_express_global_acquires_subgate(self):
        store = _MemStore()
        w = _wrapper(store, "express", express_cap=50)
        ok, _ = w.try_acquire(G_KEY, 500)
        assert ok
        assert store.counts[G_KEY] == 1
        assert store.counts[express_subgate_key(DAY)] == 1
        assert (express_subgate_key(DAY), 50) in store.acquired

    def test_subgate_denial_rolls_back_legacy(self):
        """子闸拒 → 总闸行回滚（拒绝不落计数）。"""
        store = _MemStore()
        store.counts[express_subgate_key(DAY)] = 50  # 子闸打满
        w = _wrapper(store, "express", express_cap=50)
        ok, _ = w.try_acquire(G_KEY, 500)
        assert not ok
        assert store.counts[G_KEY] == 0, "总闸行必须被回滚"

    def test_express_cap_zero_fail_closed(self):
        """admin cap 读取失败 fail-closed 0 → express 子闸恒拒。"""
        store = _MemStore()
        w = _wrapper(store, "express", express_cap=0)
        ok, _ = w.try_acquire(G_KEY, 500)
        assert not ok
        assert store.counts[G_KEY] == 0


# ---------------------------------------------------------------------------
# D. per-scope per-mode（1 次/日，free/express 互不挤占）
# ---------------------------------------------------------------------------


class TestPerScopePerMode:
    def test_per_scope_acquires_mode_row(self):
        store = _MemStore()
        w = _wrapper(store, "express")
        ok, _ = w.try_acquire(IP_KEY, 3)
        assert ok
        assert store.counts[IP_KEY] == 1
        assert store.counts[mode_scope_key(IP_KEY, "express")] == 1
        assert (
            mode_scope_key(IP_KEY, "express"),
            PER_SCOPE_PER_MODE_DAILY_CAP,
        ) in store.acquired

    def test_second_same_mode_denied_and_legacy_rolled_back(self):
        store = _MemStore()
        w = _wrapper(store, "express")
        assert w.try_acquire(IP_KEY, 3)[0]
        ok, _ = w.try_acquire(IP_KEY, 3)
        assert not ok
        # 拒绝不落计数：legacy 回到 1（第一次的），mode 行也是 1
        assert store.counts[IP_KEY] == 1
        assert store.counts[mode_scope_key(IP_KEY, "express")] == 1

    def test_free_and_express_do_not_crowd_each_other(self):
        """同 IP：1 次 free + 1 次 express 都可过（per-mode 行独立），
        legacy 总行计 2（既有 per-ip cap=3 的总闸语义不变）。"""
        store = _MemStore()
        assert _wrapper(store, "free").try_acquire(IP_KEY, 3)[0]
        assert _wrapper(store, "express").try_acquire(IP_KEY, 3)[0]
        assert store.counts[IP_KEY] == 2
        assert store.counts[mode_scope_key(IP_KEY, "free")] == 1
        assert store.counts[mode_scope_key(IP_KEY, "express")] == 1

    def test_decrement_rolls_back_both_rows(self):
        """adapter 多 key 回滚路径：decrement 双行同步回退（T5 配额退还
        也走这条：per-scope per-mode 行回退）。"""
        store = _MemStore()
        w = _wrapper(store, "express")
        w.try_acquire(IP_KEY, 3)
        w.decrement(IP_KEY)
        assert store.counts[IP_KEY] == 0
        assert store.counts[mode_scope_key(IP_KEY, "express")] == 0

    def test_decrement_global_rolls_back_subgate(self):
        store = _MemStore()
        w = _wrapper(store, "express", express_cap=50)
        w.try_acquire(G_KEY, 500)
        w.decrement(G_KEY)
        assert store.counts[G_KEY] == 0
        assert store.counts[express_subgate_key(DAY)] == 0


# ---------------------------------------------------------------------------
# E. wiring 集成：adapter 真实路径下的 acquire 序列
# ---------------------------------------------------------------------------


class TestWiringIntegration:
    def _run(self, monkeypatch, tmp_path, mode: str, store: _MemStore):
        from services.anonymous_preview_backend_adapter import (
            RequestFacts,
            UploadFacts,
        )
        from services.anonymous_preview_intake import SourceType
        from anonymous_preview_record_store import RecordStoreError

        monkeypatch.setattr(wiring, "resolve_express_global_cap", lambda: 50)
        stored = tmp_path / "v.mp4"
        stored.write_bytes(b"x" * 64)
        request_facts = RequestFacts(
            raw_session_id="sess_t2",
            raw_ip="203.0.113.9",
            raw_device_cookie="dev_t2",
            source_type=SourceType.LOCAL_UPLOAD,
            is_free_user=True,
            day_key=DAY,
        )
        upload_facts = UploadFacts(
            file_name="v.mp4",
            byte_length=64,
            duration_seconds=0.0,
            source_hash="cd" * 32,
            stored_path=stored,
        )
        factory_calls: list = []

        def _factory(scope: str):
            factory_calls.append(scope)
            return store

        try:
            wiring.run_intake_and_save(
                db_session=MagicMock(),
                request_facts=request_facts,
                upload_facts=upload_facts,
                counter_store_factory=_factory,
                upload_root=tmp_path,
                mode=mode,
            )
        except RecordStoreError:
            pass
        return factory_calls

    def test_factory_called_once_with_same_scope(self, monkeypatch, tmp_path):
        """注入 factory 只调一次（同 store 承载双行）——保住既有
        TestPeekKeyDerivationConsistency 的 scope 断言。"""
        store = _MemStore()
        calls = self._run(monkeypatch, tmp_path, "express", store)
        assert calls == [ANON_PREVIEW_COUNTER_SCOPE]

    def test_express_acquire_sequence(self, monkeypatch, tmp_path):
        """判定顺序：总闸 → express 子闸 → per-scope（legacy → per-mode）。"""
        store = _MemStore()
        self._run(monkeypatch, tmp_path, "express", store)
        keys = [k for k, _ in store.acquired]
        assert keys[0] == G_KEY.replace(DAY, DAY)  # global:{day}
        assert keys[0].startswith("global:")
        assert keys[1] == mode_scope_key(keys[0], "express")
        # 其后每个 per-scope legacy key 紧跟其 :mode:express companion
        rest = keys[2:]
        assert len(rest) % 2 == 0
        for i in range(0, len(rest), 2):
            assert rest[i + 1] == mode_scope_key(rest[i], "express")
        # legacy 序列与 free 现状完全一致（global, ip, device, source）
        legacy = [k for k in keys if ":mode:" not in k]
        assert [k.split(":")[0] for k in legacy] == [
            "global", "ip", "device", "source",
        ]

    def test_free_acquire_sequence_adds_mode_rows_only(self, monkeypatch, tmp_path):
        store = _MemStore()
        self._run(monkeypatch, tmp_path, "free", store)
        keys = [k for k, _ in store.acquired]
        # global 无子闸；ip/device/source 各带 :mode:free companion
        assert keys[0].startswith("global:")
        assert ":mode:" not in keys[0]
        assert keys[1].startswith("ip:")
        assert keys[2] == mode_scope_key(keys[1], "free")


# ---------------------------------------------------------------------------
# F. AD-8 peek 按 lane 查（plan §A，T1 验收项落在 T2 一并钉死）
# ---------------------------------------------------------------------------


class _RecordingDb:
    """记录 (sql_text, params) 并按序返回计数。"""

    def __init__(self, counts: list[int]):
        self.calls: list[tuple[str, dict]] = []
        self._counts = list(counts)

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params or {})))
        n = self._counts.pop(0) if self._counts else 0
        row = MagicMock()
        row.fetchone = MagicMock(return_value=[n])
        return row


class TestPeekPerLane:
    def _limits(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            anonymous_preview_cap_global_per_day=500,
            anonymous_preview_cap_per_ip=3,
        )

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        import anonymous_preview_api as api

        monkeypatch.setattr(
            api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
        )
        monkeypatch.setattr(api, "resolve_express_global_cap", lambda: 50)
        self.api = api

    @pytest.mark.asyncio
    async def test_express_peek_queries_subgate_and_mode_rows(self):
        db = _RecordingDb([0, 0, 0, 0])
        out = await self.api.ad8_peek_precheck(
            db, MagicMock(client=None, headers={}), self._limits(), lane="express"
        )
        assert out is None
        keys = [p["key"] for _s, p in db.calls]
        modes = [p.get("mode", "free") for _s, p in db.calls]
        # 顺序：总闸（legacy global）→ express 子闸 → legacy ip → per-mode ip
        assert keys[0].startswith("global:") and ":mode:" not in keys[0]
        assert keys[1].endswith(":mode:express") and keys[1].startswith("global:")
        assert keys[2].startswith("ip:") and ":mode:" not in keys[2]
        assert keys[3] == mode_scope_key(keys[2], "express")
        assert modes[1] == "express" and modes[3] == "express"

    @pytest.mark.asyncio
    async def test_express_subgate_at_cap_429(self):
        db = _RecordingDb([0, 50])
        out = await self.api.ad8_peek_precheck(
            db, MagicMock(client=None, headers={}), self._limits(), lane="express"
        )
        assert out is not None and out.status_code == 429

    @pytest.mark.asyncio
    async def test_per_mode_ip_at_cap_429(self):
        db = _RecordingDb([0, 0, 0, PER_SCOPE_PER_MODE_DAILY_CAP])
        out = await self.api.ad8_peek_precheck(
            db, MagicMock(client=None, headers={}), self._limits(), lane="express"
        )
        assert out is not None and out.status_code == 429

    @pytest.mark.asyncio
    async def test_free_peek_keeps_legacy_queries_and_adds_mode_row(self):
        """free lane：总闸/per-ip 查询形状不变 + 增 per-mode(free) 行。"""
        db = _RecordingDb([0, 0, 0])
        out = await self.api.ad8_peek_precheck(
            db, MagicMock(client=None, headers={}), self._limits(), lane="free"
        )
        assert out is None
        keys = [p["key"] for _s, p in db.calls]
        assert keys[0].startswith("global:") and ":mode:" not in keys[0]
        assert keys[1].startswith("ip:") and ":mode:" not in keys[1]
        assert keys[2] == mode_scope_key(keys[1], "free")

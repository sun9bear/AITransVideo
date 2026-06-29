"""APF per-mode 三维度配额旋钮（2026-06-13 项目主裁定，原硬编码常量改 admin 旋钮）。

把 ``PER_SCOPE_PER_MODE_DAILY_CAP`` 常量替换为三个 admin 字段
``anonymous_preview_cap_per_{ip,device,source}_per_mode``（默认 1，热可调）。
覆盖：后端字段/默认/边界、resolver、scope_key→cap 映射、LaneAwareCounterStore
按维度取 cap、前端 full-body POST 同步（interface/DEFAULT/reset 三处静态扫描）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ADMIN_SETTINGS_PAGE = (
    _REPO / "frontend-next" / "src" / "app" / "[locale]" / "(app)" / "admin"
    / "settings" / "page.tsx"
)

_FIELDS = (
    "anonymous_preview_cap_per_ip_per_mode",
    "anonymous_preview_cap_per_device_per_mode",
    "anonymous_preview_cap_per_source_per_mode",
)


# ---------------------------------------------------------------------------
# 1. 后端 AdminSettings 字段 / 默认 / 边界
# ---------------------------------------------------------------------------


def test_backend_fields_default_one():
    from admin_settings import AdminSettings

    d = AdminSettings()
    for f in _FIELDS:
        assert f in AdminSettings.model_fields, f"缺字段 {f}"
        assert getattr(d, f) == 1, f"{f} 默认应为 1（保持 T2 行为）"


def test_backend_bounds_reject_zero_and_over_cap():
    from admin_settings import AdminSettings

    for f in _FIELDS:
        with pytest.raises(Exception):
            AdminSettings(**{f: 0})       # 下界：0 = 该维度拒死所有 intake
        with pytest.raises(Exception):
            AdminSettings(**{f: 1001})    # 上界
        assert getattr(AdminSettings(**{f: 1000}), f) == 1000
        assert getattr(AdminSettings(**{f: 3}), f) == 3


def test_per_mode_caps_not_in_apf_limit_bounds():
    """不得污染 _APF_LIMIT_BOUNDS（被 limits_knobs 整表相等钉死）。"""
    import admin_settings as adm

    for f in _FIELDS:
        assert f not in adm._APF_LIMIT_BOUNDS


# ---------------------------------------------------------------------------
# 2. resolver + scope_key→cap 映射
# ---------------------------------------------------------------------------


def test_resolve_per_mode_caps_reads_admin(monkeypatch):
    import anonymous_preview_intake_wiring as wiring
    from types import SimpleNamespace

    monkeypatch.setattr(
        wiring, "load_settings" if hasattr(wiring, "load_settings") else "__noop__",
        lambda: None, raising=False,
    )
    # resolve_per_mode_caps lazy-imports admin_settings.load_settings；改 stub
    import admin_settings as adm
    monkeypatch.setattr(
        adm, "load_settings",
        lambda: SimpleNamespace(
            anonymous_preview_cap_per_ip_per_mode=3,
            anonymous_preview_cap_per_device_per_mode=2,
            anonymous_preview_cap_per_source_per_mode=1,
        ),
    )
    caps = wiring.resolve_per_mode_caps()
    assert caps == {"ip": 3, "device": 2, "source": 1}


def test_resolve_per_mode_caps_fail_safe(monkeypatch):
    """读取异常 → fail-safe 默认 1（非 fail-closed 0，否则拒死所有 intake）。"""
    import anonymous_preview_intake_wiring as wiring
    import admin_settings as adm

    def _boom():
        raise RuntimeError("admin unavailable")

    monkeypatch.setattr(adm, "load_settings", _boom)
    caps = wiring.resolve_per_mode_caps()
    assert caps == {"ip": 1, "device": 1, "source": 1}


def test_per_mode_cap_for_scope_key_by_prefix():
    from anonymous_preview_intake_wiring import per_mode_cap_for_scope_key

    caps = {"ip": 3, "device": 2, "source": 1}
    assert per_mode_cap_for_scope_key("ip:hash:2026-06-13:mode:free", caps) == 3
    assert per_mode_cap_for_scope_key("device:hash:2026-06-13:mode:free", caps) == 2
    assert per_mode_cap_for_scope_key("source:hash:2026-06-13:mode:express", caps) == 1
    # 未知前缀回落默认常量
    assert per_mode_cap_for_scope_key("weird:x", caps) == 1


# ---------------------------------------------------------------------------
# 3. LaneAwareCounterStore 按维度取 cap
# ---------------------------------------------------------------------------


class _MemStore:
    def __init__(self):
        self.counts: dict[str, int] = {}

    def get(self, key):
        return self.counts.get(key, 0)

    def try_acquire(self, key, cap):
        cur = self.get(key)
        if cur >= cap:
            return (False, cur)
        self.counts[key] = cur + 1
        return (True, cur + 1)

    def decrement(self, key):
        self.counts[key] = max(0, self.get(key) - 1)
        return self.counts[key]

    def increment(self, key):
        self.counts[key] = self.get(key) + 1
        return self.counts[key]


def test_lane_store_uses_per_dimension_caps():
    """per_ip_per_mode=3 → 同 IP 同 mode 可过 3 次；per_device=1 → 设备第 2 次拒。"""
    from anonymous_preview_intake_wiring import LaneAwareCounterStore, mode_scope_key

    store = _MemStore()
    caps = {"ip": 3, "device": 1, "source": 1}

    def fresh():
        return LaneAwareCounterStore(
            store, store, lane="free", express_global_cap=0, per_mode_caps=caps
        )

    IP = "ip:h:2026-06-13"
    # 三次 ip per-mode 取额都应过（cap=3）
    assert fresh().try_acquire(IP, 100)[0] is True
    assert fresh().try_acquire(IP, 100)[0] is True
    assert fresh().try_acquire(IP, 100)[0] is True
    assert store.counts[mode_scope_key(IP, "free")] == 3
    # 第四次被 per-mode ip cap=3 拒（legacy cap=100 仍宽）
    ok, _ = fresh().try_acquire(IP, 100)
    assert ok is False

    DEV = "device:d:2026-06-13"
    assert fresh().try_acquire(DEV, 100)[0] is True
    ok, _ = fresh().try_acquire(DEV, 100)  # cap=1，第二次拒
    assert ok is False


def test_lane_store_default_caps_when_none():
    from anonymous_preview_intake_wiring import LaneAwareCounterStore

    store = _MemStore()
    w = LaneAwareCounterStore(store, store, lane="free", express_global_cap=0)
    IP = "ip:h:2026-06-13"
    assert w.try_acquire(IP, 100)[0] is True
    # 默认 cap=1 → 第二次拒
    w2 = LaneAwareCounterStore(store, store, lane="free", express_global_cap=0)
    assert w2.try_acquire(IP, 100)[0] is False


# ---------------------------------------------------------------------------
# 4. 前端 full-body POST 同步（interface / DEFAULT_SETTINGS / reset）
# ---------------------------------------------------------------------------


def _page() -> str:
    return ADMIN_SETTINGS_PAGE.read_text(encoding="utf-8")


def test_frontend_interface_has_fields():
    src = _page()
    m = re.search(r"interface\s+AdminSettings\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m
    body = m.group("body")
    for f in _FIELDS:
        assert re.search(rf"{re.escape(f)}\s*:\s*number", body), f"interface 缺 {f}"


def test_frontend_defaults_are_one():
    src = _page()
    m = re.search(r"DEFAULT_SETTINGS[^=]*=\s*\{(?P<body>[\s\S]*?)\n\}", src)
    assert m
    body = m.group("body")
    for f in _FIELDS:
        assert re.search(rf"{re.escape(f)}\s*:\s*1\s*,", body), f"DEFAULT_SETTINGS 缺 {f}: 1"


def test_frontend_reset_restores_fields():
    src = _page()
    for f in _FIELDS:
        assert re.search(
            rf"{re.escape(f)}\s*:\s*\n?\s*DEFAULT_SETTINGS\.{re.escape(f)}", src
        ), f"reset 缺 {f}"

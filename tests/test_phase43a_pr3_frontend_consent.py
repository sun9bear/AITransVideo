"""Phase 4.3a PR3 — 前端 Express consent UI 静态守卫（守卫先行）。

本项目前端无 Vitest/RTL/jsdom，沿用 ``test_phase2_download_backend.py`` 的
**Python 静态扫** 模式锁死 PR3 spec v0.2 的契约：

- express 提交**无条件**带 ``express_consent``；未勾也发 ``auto_voice_clone:false``
- ``express_consent`` 只在 express 分支构造（不污染 studio/smart）
- checkbox 默认未勾选；submit 取值 ``serviceMode==='express' ? state : false``
  （切 mode / availability false 不残留 true）
- availability fetch 非 2xx → fail-closed ``available:false``
- 前端**绝不**构造 / 发送 ``server_confirmed_at``
- 文案**不**承诺"N 天后删除/失效"（4.3b sweeper 未上线）
- PR3 改动面只在 frontend-next / tests（不碰后端）

**守卫先行**：PR3-A 提交时部分 presence 守卫为 red，PR3-B/C 实施后转 green。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_FE = _REPO / "frontend-next" / "src"
_JOBS_TS = _FE / "lib" / "api" / "jobs.ts"
_ENT_TS = _FE / "lib" / "api" / "entitlements.ts"
_TYPES_TS = _FE / "types" / "jobs.ts"
_FORM_TSX = _FE / "components" / "workspace" / "TranslationForm.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# submit payload（jobs.ts）
# ---------------------------------------------------------------------------


def test_express_submission_always_sends_consent():
    """jobs.ts：service_mode==='express' 分支构造 express_consent + auto_voice_clone
    （spec §2.1 / DoD #4，锁"未勾选也发 false"）。"""
    src = _read(_JOBS_TS)
    assert "express_consent" in src, "submitTranslationJob 必须构造 express_consent"
    assert "auto_voice_clone" in src, "express_consent 必须含 auto_voice_clone 字段"
    # express_consent 的赋值必须出现在 express 分支语境（同文件内有 'express' 判断）
    assert re.search(r"service_mode\s*===\s*['\"]express['\"]", src), (
        "express_consent 必须 gated 在 service_mode==='express' 分支"
    )


def test_consent_payload_only_for_express():
    """express_consent 只在 express 分支；不在 smart 分支误发（smart 走 smart_consent）。"""
    src = _read(_JOBS_TS)
    # smart 分支构造 smart_consent，不构造 express_consent —— 粗扫：express_consent
    # 行不应紧贴 smart 判断。用结构性断言：smart_consent 与 express_consent 都存在，
    # 且 express_consent 不出现在 "=== 'smart'" 同一赋值块里（弱断言：分别独立）。
    assert "smart_consent" in src and "express_consent" in src
    # express_consent 赋值右侧不应引用 smart
    for m in re.finditer(r"express_consent\s*=\s*\{", src):
        window = src[m.start(): m.start() + 200]
        assert "smart" not in window.lower(), "express_consent 块不应混入 smart 语义"


def test_frontend_never_sends_server_confirmed_at():
    """spec §3 / DoD #10：前端绝不构造 server_confirmed_at（后端单一来源）。
    递归扫 frontend-next/src 所有 ts/tsx。"""
    offenders = []
    for p in _FE.rglob("*.ts*"):
        if "server_confirmed_at" in _read(p):
            offenders.append(str(p.relative_to(_REPO)))
    assert not offenders, (
        f"前端不得出现 server_confirmed_at（后端生成）：{offenders}"
    )


# ---------------------------------------------------------------------------
# availability client（entitlements.ts）fail-closed
# ---------------------------------------------------------------------------


def test_availability_client_exists_and_fail_closed():
    """entitlements.ts：getExpressAutoCloneAvailability 调对 endpoint 且非 2xx
    返回 available:false（spec §4.1 fail-closed）。"""
    src = _read(_ENT_TS)
    assert "express-auto-clone-availability" in src, "必须调 availability endpoint"
    assert "getExpressAutoCloneAvailability" in src
    # fail-closed：函数体内出现 available: false（非 2xx 兜底）
    assert re.search(r"available:\s*false", src), (
        "availability fetch 非 2xx 必须 fail-closed 返回 available:false"
    )


# ---------------------------------------------------------------------------
# TranslationForm checkbox：默认 false + availability gating + state reset
# ---------------------------------------------------------------------------


def test_checkbox_default_unchecked():
    """TranslationForm：consent state 初始为 false（opt-in 硬约束）。"""
    src = _read(_FORM_TSX)
    # const [expressAutoVoiceClone, setExpressAutoVoiceClone] = useState(false)
    assert re.search(
        r"\[\s*expressAutoVoiceClone\s*,[^\]]*\]\s*=\s*useState\s*(<[^>]*>)?\s*\(\s*false\s*\)",
        src,
    ), "consent checkbox state 必须默认 useState(false)"


def test_checkbox_gated_by_express_and_availability():
    """TranslationForm：checkbox 渲染受 service_mode==='express' 且 availability
    可用双门控（spec §2.2 / §4.2）。"""
    src = _read(_FORM_TSX)
    assert "expressAutoCloneAvailable" in src, "必须有 availability state"
    assert re.search(r"serviceMode\s*===\s*['\"]express['\"]", src)
    # 渲染条件里同时出现 express 判断与 availability flag
    assert re.search(
        r"serviceMode\s*===\s*['\"]express['\"]\s*&&\s*expressAutoCloneAvailable", src
    ), "checkbox 渲染必须 express && expressAutoCloneAvailable 双门控"


def test_consent_state_resets_on_mode_switch():
    """spec §2.6 / DoD #8：submit 取值 serviceMode==='express' ? state : false，
    非 express 恒 false（不残留 true 触发付费 clone）。"""
    src = _read(_FORM_TSX)
    assert re.search(
        r"serviceMode\s*===\s*['\"]express['\"]\s*\?\s*expressAutoVoiceClone\s*:\s*false",
        src,
    ), "submit 必须用 serviceMode==='express' ? expressAutoVoiceClone : false 取值"


def test_consent_copy_no_deletion_deadline():
    """spec §5 / DoD #9：consent 文案不得承诺"N 天后删除/失效"（4.3b 未上线）。"""
    src = _read(_FORM_TSX)
    # 禁止"数字+天 ... 删除/失效/清除/移除"这类删除时限承诺
    bad = re.search(r"\d+\s*天[^。\n]{0,12}(删除|失效|清除|移除|过期)", src)
    assert not bad, (
        f"consent 文案不得承诺删除时限（命中：{bad.group(0) if bad else ''}）"
    )


# ---------------------------------------------------------------------------
# PR3 纯前端（不碰后端）
# ---------------------------------------------------------------------------


def test_pr3_changes_are_frontend_or_meta_only():
    """spec §2.5 / §8 / DoD #7：PR3 改动只在 frontend-next / tests / docs，
    不碰 gateway / src / migration / *.py（后端零改动）。

    用 git diff against main 检查；git 不可用时 skip（CI 有独立 diff 审查）。
    """
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "main...HEAD"],
            cwd=str(_REPO), capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pytest.skip("git unavailable")
    if out.returncode != 0:
        pytest.skip(f"git diff failed: {out.stderr[:200]}")
    changed = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not changed:
        pytest.skip("no diff vs main (branch not ahead)")
    forbidden = []
    for path in changed:
        # 允许：frontend-next/** + tests/** + docs/**
        if path.startswith(("frontend-next/", "tests/", "docs/")):
            continue
        forbidden.append(path)
    assert not forbidden, (
        f"PR3 必须纯前端，不应改这些后端 / 其它文件：{forbidden}"
    )

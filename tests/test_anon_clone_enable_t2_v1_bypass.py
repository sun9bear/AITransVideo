"""P2 V1 回归 — 方案 A 旁路不变量：preset_mapping 不阻断 cloned-voice→worker.

plan 2026-06-14 §3.2 / Q1（CodeX 拍板方案 A）。

方案 A：防线② 保留 voice_strategy=preset_mapping 不动，匿名/快捷 CosyVoice 克隆
经 speaker routing 旁路注入（auto_clone 成功写 _speaker_voices + requires_worker
routing → process 强制 tts_provider="cosyvoice" → tts_generator 见 requires_worker
强制走武汉 worker）。

V1 锁定的核心不变量：**``voice_strategy=preset_mapping`` 绝不会把一个
``requires_worker=True`` 的克隆段 diverts 到 MiMo 预设或 MiniMax**。
``force_mimo_preset=True`` 只在 ``_voice_strategy == "free_voiceclone"`` 的免费档
fallback 路径设置（与匿名 express 的 preset_mapping 无关）——这是旁路成立的关键。

requires_worker → worker 的运行时 dispatch 由既有 test_phase4_1_d_worker_routing /
test_phase4_1_f_lockdown_guards 覆盖；本文件 source-invariant 锁方案 A 特有边界。
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TTS_GEN = _REPO / "src" / "services" / "tts" / "tts_generator.py"
_AUTO_CLONE = _REPO / "src" / "services" / "express" / "auto_clone.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_force_mimo_preset_only_under_free_voiceclone_guard():
    """``force_mimo_preset=True`` 只出现在 free_voiceclone fallback 上下文，
    绝不绑 preset_mapping / anonymous / express。

    AST：找所有 ``force_mimo_preset=True`` 关键字实参，确认其所在 _generate_one
    调用的最近 if 守卫含 ``free_voiceclone``（而非 voice_strategy=preset_mapping
    或 anonymous）。源码漂移（有人给 preset_mapping/anon 路径加 force_mimo_preset）
    会在此 red，保护方案 A 旁路。
    """
    src = _read(_TTS_GEN)
    # 出现次数：当前实现只应有 1 处 True（free_voiceclone fallback）+ 1 处签名默认 False。
    true_count = src.count("force_mimo_preset=True")
    assert true_count == 1, (
        f"force_mimo_preset=True 出现 {true_count} 次；新增设置点必须复核是否会"
        f"误伤方案 A 旁路（preset_mapping 的 requires_worker 段应走 worker 不走 mimo）"
    )
    # 该 True 的上下文必须是 free_voiceclone（取其前 800 字符窗口）。
    idx = src.index("force_mimo_preset=True")
    window = src[max(0, idx - 800):idx]
    assert "free_voiceclone" in window, (
        "force_mimo_preset=True 必须在 free_voiceclone fallback 上下文；"
        "不得绑定 preset_mapping / anonymous（否则克隆段被错误 diverts 到 MiMo）"
    )


def test_requires_worker_forces_cosyvoice_provider_seam_present():
    """旁路 dispatch seam 仍在：``if not force_mimo_preset and ... requires_worker``
    → 强制 provider='cosyvoice'。这是 preset_mapping 下克隆段路由到 worker 的关键，
    删/改此行须同步评审方案 A。"""
    src = _read(_TTS_GEN)
    # TU-07: getattr(segment,"requires_worker",False) → segment.requires_worker（字节等价，
    # slots dataclass 字段恒存在）。seam 语义不变，仅更新被 pin 的实现文本。
    assert "if not force_mimo_preset and bool(segment.requires_worker):" in src
    # 强制 cosyvoice（不允许 mismatched 付费 provider）
    assert "Refusing to call paid" in src  # mismatch → 抛错而非静默调付费


def test_auto_clone_success_injects_requires_worker_routing():
    """auto_clone 成功路径**原地注入** requires_worker routing（旁路入口）。

    source-pin：run_express_auto_clone 成功分支写
    ``speaker_routing[main_speaker_id] = {"requires_worker": True, ...}`` +
    ``speaker_voices[main_speaker_id] = clone_res.voice_id``。运行时编排成功路径
    由既有 express auto_clone 测试覆盖；此处锁注入契约不被悄悄改掉。
    """
    src = _read(_AUTO_CLONE)
    assert 'speaker_voices[main_speaker_id] = clone_res.voice_id' in src
    assert '"requires_worker": True' in src


def test_auto_clone_never_imports_minimax():
    """🔥 红线 source-pin：auto_clone 编排器绝不 import MiniMax 克隆模块。
    任何失败路径只回预设（speaker_voices 不变）。

    只扫 import 节点（docstring/注释里"绝不调 MiniMax"的说明文字不算违规——
    那正是文档化红线本身）。"""
    tree = ast.parse(_read(_AUTO_CLONE))
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for m in mods:
            assert "minimax" not in m.lower(), f"auto_clone 不得 import MiniMax: {m}"
        # 函数调用 attr/name 也不得命中 minimax provider
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name) else ""
            )
            assert "minimax" not in str(name).lower(), (
                f"auto_clone 不得调用 MiniMax: {name}"
            )

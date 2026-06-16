"""匿名预览源时长超限 → 友好提示（项目主裁定 2026-06-16）.

需求：匿名免费用户上传时长超过匿名**源上传**上限（``anonymous_preview_max_source_seconds``，
admin 热配置；与预览长度 ``anonymous_preview_max_seconds`` 已于 2026-06-16 解耦）的视频时，
在预览流程开始前就拦住，并给"视频时长超限，请更换视频再上传"的友好提示——而不是把
生硬的 rejected/英文原因抛给用户。

实现（保持现有上限不放宽，只细分客户端可见 code + 前端文案）：
- gateway ``_redact_reason`` 把 intake/adapter 的 "... exceeds intake cap" 拒绝
  细分成可区分的 client code ``duration_exceeded``（持久 status_reason 文本**不变**
  =审计契约，多处测试 pin "exceeds intake cap"）。
- 前端 ``mapStatusReason`` 把 ``duration_exceeded`` 映射成中文友好提示。

本文件锁：(1) _redact_reason 行为（细分 + 不回归既有 code）；(2) 前端映射存在
（项目无 JS test runner → Python 静态扫描，沿用 test_d7_frontend_convert_guard.py 约定）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import anonymous_preview_api as api  # noqa: E402

_ANON_PREVIEW_TS = (
    _REPO / "frontend-next" / "src" / "lib" / "api" / "anonymousPreview.ts"
)


# ---------------------------------------------------------------------------
# 1. gateway _redact_reason — 时长超限细分出可区分 client code
# ---------------------------------------------------------------------------


def test_redact_reason_duration_exceeded_from_intake_reason():
    # intake.validate_upload 的越限文案（持久审计契约，含真实时长数）。
    assert (
        api._redact_reason("upload duration 11905.71 exceeds intake cap")
        == "duration_exceeded"
    )


def test_redact_reason_duration_exceeded_from_adapter_reason():
    # backend_adapter 探测期越限文案。
    assert (
        api._redact_reason("probed duration 11905.71 exceeds intake cap")
        == "duration_exceeded"
    )


def test_redact_reason_duration_exceeded_case_insensitive():
    assert (
        api._redact_reason("Probed Duration 999 EXCEEDS INTAKE CAP")
        == "duration_exceeded"
    )


def test_redact_reason_preserves_existing_codes():
    # 细分分支在 _safe_codes 之前，但不得吞掉既有 code（无 "exceeds intake cap" 子串）。
    assert api._redact_reason("rate_limited: too many") == "rate_limited"
    assert api._redact_reason("content_blocked by policy") == "content_blocked"
    assert api._redact_reason("needs_review manual") == "needs_review"
    assert api._redact_reason("storage_unavailable now") == "storage_unavailable"
    # 其它非时长拒绝（如越限字节/格式，本会经上传预检走 mapUploadError）落通用 rejected。
    assert api._redact_reason("upload bytes 999 exceed cap") == "rejected"
    assert api._redact_reason("upload extension 'avi' is not allowed") == "rejected"
    assert api._redact_reason(None) is None
    assert api._redact_reason("") is None


# ---------------------------------------------------------------------------
# 2. 前端 mapStatusReason 映射友好文案（静态扫描）
# ---------------------------------------------------------------------------


def test_frontend_maps_duration_exceeded_to_friendly_text():
    assert _ANON_PREVIEW_TS.exists(), f"前端文件不存在: {_ANON_PREVIEW_TS}"
    src = _ANON_PREVIEW_TS.read_text(encoding="utf-8")
    # mapStatusReason 须把 duration_exceeded 映射为项目主裁定的友好提示。
    assert "duration_exceeded:" in src, "mapStatusReason 缺 duration_exceeded 映射"
    assert "视频时长超限，请更换视频再上传" in src, "缺友好提示文案（项目主裁定）"

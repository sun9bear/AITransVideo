"""P3b — smart 克隆 register+bill 内部 endpoint 守卫（source-scan）.

plan v3。pipeline(独立进程)经 ``/api/internal/smart-clone/register-billed`` 调
register+bill 单一事务。endpoint 逻辑薄(auth+校验+dispatch)，业务在
register_smart_clone_with_billing(test_p3a_smart_clone_reserve 已厚测)。

本守卫用 source-scan(不 import gateway 模块，避 database-stub 污染，见 memory
feedback_test_database_stub_convention)，锁 endpoint 契约：路由/内部 auth/输入
校验/dispatch 到 service/no_active_reservation→409。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "gateway" / "user_voice_api.py"


def _read() -> str:
    return _API.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    tree = ast.parse(_read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(_read(), node) or ""
    return ""


def test_register_billed_route_registered():
    src = _read()
    assert '@internal_router.post("/smart-clone/register-billed")' in src
    assert "async def internal_smart_clone_register_billed(" in src


def test_reservation_active_check_route_registered():
    src = _read()
    assert '@internal_router.post("/smart-clone/reservations/check-active")' in src
    assert "async def internal_smart_clone_reservation_check_active(" in src


def test_register_billed_has_internal_auth():
    body = _func_src("internal_smart_clone_register_billed")
    assert body, "endpoint 函数未找到"
    # 内部 auth 在最前（未过 auth 不得 dispatch）——比的是实际调用位置，
    # 不是 docstring 里的服务名提及。
    assert "_internal_access_error(request)" in body
    assert body.index("_internal_access_error(request)") < body.index("await register_smart_clone_with_billing(")


def test_register_billed_validates_inputs():
    body = _func_src("internal_smart_clone_register_billed")
    for err in ("invalid_user_id", "invalid_reservation_id", "invalid_task_id", "invalid_voice_id"):
        assert err in body, f"缺输入校验 {err}"
    # user_id / reservation_id 都做 UUID 解析
    assert body.count("uuid.UUID(str(") >= 2


def test_register_billed_dispatches_to_service():
    body = _func_src("internal_smart_clone_register_billed")
    assert "from smart_clone_reservation_service import register_smart_clone_with_billing" in body
    assert re.search(r"await register_smart_clone_with_billing\(", body)


def test_register_billed_forwards_source_metadata_to_service():
    body = _func_src("internal_smart_clone_register_billed")
    for field in (
        "source_type",
        "source_ref",
        "source_content_hash",
        "source_video_title",
        "source_speaker_name",
        "source_speaker_name_key",
        "source_content_summary",
        "source_content_era",
        "source_content_tags",
        "clone_sample_seconds",
        "clone_sample_segment_ids",
        "notes",
    ):
        assert f"{field}=body.get(\"{field}\")" in body
    assert 'source_published_at=_parse_optional_datetime(body.get("source_published_at"))' in body


def test_register_billed_maps_no_active_reservation_to_409():
    body = _func_src("internal_smart_clone_register_billed")
    assert "no_active_reservation" in body
    m = re.search(r'no_active_reservation.*?_json\(\s*409', body, re.S) or re.search(
        r'_json\(\s*409[^)]*no_active_reservation', body, re.S
    )
    assert m, "no_active_reservation 必须映射到 409"

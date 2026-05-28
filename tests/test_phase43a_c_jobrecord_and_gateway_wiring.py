"""Phase 4.3a PR1-C — JobRecord schema + Gateway server_confirmed_at 注入测试。

锁定两条 spec 关键合约：

1. **JobRecord roundtrip**（spec §3.2）：``express_consent`` +
   ``express_consent_parse_error`` 两个新字段必须 round-trip
   ``to_dict()`` ↔ ``from_dict()`` 字节级完整，与现有 ``smart_consent``
   field 共存不互相干扰。

2. **Gateway server_confirmed_at 注入**（spec §3.1.a P1-5）：
   ``gateway/job_intercept.py`` 在 ``service_mode=="express"`` +
   ``auto_voice_clone is True`` 时**必须**调用
   ``datetime.now(timezone.utc).isoformat()`` 生成
   ``server_confirmed_at`` 字段。任何漏掉 / 用 client 时间替代 / 漏
   timezone-aware 的写法 → 测试 fail。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from services.jobs.models import JobRecord  # noqa: E402


# ---------------------------------------------------------------------------
# JobRecord roundtrip (spec §3.2)
# ---------------------------------------------------------------------------


def _build_minimal_express_record(**overrides) -> JobRecord:
    """构造最小合法 JobRecord 用于 schema 测试。"""
    base = dict(
        job_id="job_phase43a_test_1",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/video",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status="queued",
        current_stage=None,
        progress_message="queued",
        created_at="2026-05-28T03:00:00Z",
        updated_at="2026-05-28T03:00:00Z",
    )
    base.update(overrides)
    return JobRecord(**base)


def test_jobrecord_express_consent_field_defaults_to_none():
    """新建 JobRecord 不传 express_consent → 默认 None（与现有字段一致）。"""
    record = _build_minimal_express_record()
    assert record.express_consent is None
    assert record.express_consent_parse_error is None


def test_jobrecord_express_consent_roundtrip_to_dict_from_dict():
    """express_consent dict 必须在 to_dict ↔ from_dict 之间字节级保真。"""
    consent = {
        "auto_voice_clone": True,
        "client_confirmed_at": "2026-05-28T03:45:21Z",
        "server_confirmed_at": "2026-05-28T03:45:22.123Z",
    }
    record = _build_minimal_express_record(
        express_consent=dict(consent),  # deep-copy via dict()
        express_consent_parse_error=None,
    )

    serialized = record.to_dict()
    assert serialized["express_consent"] == consent
    assert serialized["express_consent_parse_error"] is None

    restored = JobRecord.from_dict(serialized)
    assert restored.express_consent == consent
    assert restored.express_consent_parse_error is None


def test_jobrecord_express_consent_parse_error_roundtrip():
    """parse_error 字段独立 roundtrip。"""
    record = _build_minimal_express_record(
        express_consent=None,
        express_consent_parse_error="auto_voice_clone_not_bool",
    )

    serialized = record.to_dict()
    assert serialized["express_consent"] is None
    assert serialized["express_consent_parse_error"] == "auto_voice_clone_not_bool"

    restored = JobRecord.from_dict(serialized)
    assert restored.express_consent is None
    assert restored.express_consent_parse_error == "auto_voice_clone_not_bool"


def test_jobrecord_smart_consent_unchanged_by_phase43a_addition():
    """守卫：Phase 4.3a 不动 smart_consent 字段的 round-trip 行为。"""
    smart_consent = {
        "auto_voice_clone": True,
        "auto_retranslate": True,
        "auto_retts": True,
        "auto_multimodal_verification": True,
        "no_extra_charge_without_confirmation": True,
        "on_budget_exhausted": "degraded_delivery_with_report",
    }
    record = _build_minimal_express_record(
        smart_consent=dict(smart_consent),
    )
    serialized = record.to_dict()
    assert serialized["smart_consent"] == smart_consent
    restored = JobRecord.from_dict(serialized)
    assert restored.smart_consent == smart_consent


def test_jobrecord_express_and_smart_consent_coexist():
    """混合 case：JobRecord 同时持有两套 consent dict 互不干扰
    （理论上 service_mode 同一时刻只取一条路径，但 JobRecord schema
    必须允许两字段共存）。
    """
    record = _build_minimal_express_record(
        smart_consent={"auto_voice_clone": True},
        express_consent={"auto_voice_clone": False, "client_confirmed_at": None},
    )
    serialized = record.to_dict()
    assert serialized["smart_consent"] == {"auto_voice_clone": True}
    assert serialized["express_consent"] == {
        "auto_voice_clone": False,
        "client_confirmed_at": None,
    }


def test_jobrecord_post_init_deep_copies_express_consent():
    """守卫：__post_init__ 必须 deep-copy 输入 dict（避免外部 mutation
    污染 record 内部状态，与现有 smart_consent 同模式）。
    """
    consent = {"auto_voice_clone": True, "client_confirmed_at": "2026-05-28"}
    record = _build_minimal_express_record(express_consent=consent)
    # mutate 外部 dict
    consent["auto_voice_clone"] = False
    consent["client_confirmed_at"] = "FORGED"
    # record 内部 dict 不应受影响
    assert record.express_consent["auto_voice_clone"] is True
    assert record.express_consent["client_confirmed_at"] == "2026-05-28"


# ---------------------------------------------------------------------------
# Gateway server_confirmed_at 注入（spec §3.1.a 关键不变量）
# ---------------------------------------------------------------------------


def _read_job_intercept_source() -> str:
    return (REPO_ROOT / "gateway" / "job_intercept.py").read_text(encoding="utf-8")


def test_gateway_calls_validate_express_consent_on_express_mode():
    """守卫：``gateway/job_intercept.py`` 必须在 ``service_mode=="express"``
    分支里调用 ``validate_express_consent``。
    """
    src = _read_job_intercept_source()
    assert 'service_mode == "express"' in src, (
        "job_intercept.py 缺少 service_mode==express 分支（Phase 4.3a 必备）"
    )
    assert "validate_express_consent" in src, (
        "job_intercept.py 不调 validate_express_consent —— Phase 4.3a §3.1.a 必须"
    )
    assert "from express_consent import validate_express_consent" in src, (
        "job_intercept.py 必须显式 import validate_express_consent —— "
        "lazy import 与 smart_consent 同模式"
    )


def test_gateway_generates_server_confirmed_at_with_timezone_utc():
    """守卫：``gateway/job_intercept.py`` 必须用
    ``datetime.now(timezone.utc).isoformat()`` 生成 server_confirmed_at。

    spec §3.1.a：永远是后端生成，timezone-aware UTC，不读 client_confirmed_at。
    """
    src = _read_job_intercept_source()
    assert 'server_confirmed_at' in src, (
        "job_intercept.py 缺少 server_confirmed_at 注入 —— spec §3.1.a 关键不变量"
    )
    # 必须用 timezone-aware datetime
    assert "datetime.now(timezone.utc).isoformat()" in src, (
        "server_confirmed_at 必须用 datetime.now(timezone.utc).isoformat() 生成 —— "
        "naive datetime / 用 time.time() 之类都不允许（spec §3.1.a）"
    )


def test_gateway_server_confirmed_at_gated_on_auto_voice_clone_true():
    """守卫：server_confirmed_at 必须用 ``is True``（strict identity）门控，
    而**不是** truthy 检查（``if x``）—— 后者会让 int 1 / str "true"
    意外解锁，与 spec §3.1 + smart_consent strict bool 一致。
    """
    src = _read_job_intercept_source()
    # 找到 server_confirmed_at 注入块的上下文：必须含 "is True"
    # 简单 textual heuristic：含 ``auto_voice_clone") is True`` 字面量
    assert 'auto_voice_clone") is True' in src, (
        "server_confirmed_at 必须用 `auto_voice_clone is True`（strict identity）门控 —— "
        "防止 truthy non-True 值绕过 strict bool check"
    )


def test_gateway_forwards_express_consent_in_request_data():
    """守卫：``gateway/job_intercept.py`` 必须把 ``express_consent`` 写入
    ``request_data`` 透传给 Job API（与 ``smart_consent`` 同模式）。
    """
    src = _read_job_intercept_source()
    assert 'request_data["express_consent"]' in src, (
        "job_intercept.py 不在 request_data 注入 express_consent —— "
        "Job API 拿不到 consent payload"
    )
    assert 'request_data["express_consent_parse_error"]' in src, (
        "job_intercept.py 不在 request_data 注入 express_consent_parse_error —— "
        "Job API 拿不到 parse_error reason"
    )


def test_gateway_does_not_validate_smart_consent_on_express_mode():
    """守卫：service_mode==express 时不应触发 validate_smart_consent。

    AST 扫：``validate_smart_consent`` 的调用必须在 ``service_mode==smart``
    条件分支内（同已有 smart 路径）；不允许出现在 service_mode==express 分支。
    """
    src = _read_job_intercept_source()
    # 用文本检查更简单：smart 分支的 validation 行必须含 "smart" 上下文
    # 查找 validate_smart_consent 调用点上下文
    idx = src.find("from smart_consent import validate_smart_consent")
    assert idx > 0, "validate_smart_consent import 不存在"
    # 找上 30 行（gateway/job_intercept.py:1046 上下文有 service_mode == "smart"）
    preamble = src[max(0, idx - 800):idx]
    assert 'service_mode == "smart"' in preamble, (
        "validate_smart_consent 调用上下文不在 service_mode==smart 分支内"
    )


def test_gateway_passthrough_in_express_payload_does_not_leak_smart_fields():
    """守卫：service_mode==express 不把 smart_consent_payload 推 request_data。

    虽然现有代码已经满足（``if smart_consent_payload is not None``），但
    Phase 4.3a 实施时新加的 express 注入块不能意外解锁 smart_consent
    分支。验证 if 语句仍在。
    """
    src = _read_job_intercept_source()
    assert "if smart_consent_payload is not None:" in src, (
        "smart_consent_payload 注入应保留 ``is not None`` 门控 —— "
        "Phase 4.3a 不应误删此 guard"
    )
    assert "if express_consent_payload is not None:" in src, (
        "express_consent_payload 注入需要相同 ``is not None`` 门控 —— "
        "防止 express_consent==None 时把 None 注入 request_data"
    )


# ---------------------------------------------------------------------------
# AST-level: express_consent.py 文件本身存在并 expose 正确 API
# ---------------------------------------------------------------------------


def test_express_consent_module_exposes_validator_only():
    """守卫：``gateway/express_consent.py`` ``__all__`` 只 export
    ``validate_express_consent``。

    防止 PR 未来 leak 内部 helper（如 ``_REQUIRED_BOOL_FIELDS`` 之类）。
    """
    src_path = REPO_ROOT / "gateway" / "express_consent.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    # 找 __all__ 赋值
    found_all = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id == "__all__":
                found_all = True
                # value 应该是 List of Str
                assert isinstance(node.value, ast.List), (
                    "__all__ 应该是 list literal"
                )
                names = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                assert names == ["validate_express_consent"], (
                    f"__all__ 应该只 export ['validate_express_consent']，实际 {names}"
                )
                break
    assert found_all, "express_consent.py 必须显式 __all__"

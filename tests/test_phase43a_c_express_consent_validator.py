"""Phase 4.3a PR1-C — Express consent validator unit tests.

锁定 ``gateway/express_consent.py::validate_express_consent`` 的契约：
soft-skip 语义 + 2 字段 schema + 与 ``validate_smart_consent`` 边界
完全独立。

测试覆盖（覆盖 spec v0.3 §3.1 + §3.1.a + §10.2 consent 单元测试）：

1. 输入为 None → 返 `(None, "express_consent_missing_or_invalid_type")`
2. 输入为非 dict（str / int / list）→ 同上 reason
3. 缺 ``auto_voice_clone`` 字段 → 返 `({"auto_voice_clone": False, ...}, None)`
4. ``auto_voice_clone`` 为非 bool（int 0 / int 1 / str "true"）→ 拒收
5. ``auto_voice_clone=True`` + 无 client_confirmed_at → 成功，
   ``client_confirmed_at=None``
6. ``auto_voice_clone=True`` + 含 client_confirmed_at（str）→ 成功
7. ``client_confirmed_at`` 非 str → 拒收
8. validate 返回的 dict **绝不** 含 ``server_confirmed_at``
   （那是 caller 后加的）
9. validate 永不 raise（任意输入都要返 tuple）
10. validator 输出对 input dict 没副作用（无 mutation）

注意：本测试**不**测 Gateway 加 server_confirmed_at 的逻辑，那在
``test_phase43a_c_gateway_express_consent_wiring.py``（gateway 集成）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# gateway/ 不在 sys.path 默认根上（Phase 4.3a 实现是 gateway-local 模块）
REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = REPO_ROOT / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from express_consent import validate_express_consent  # noqa: E402


# ---------------------------------------------------------------------------
# Soft-skip on missing / invalid type
# ---------------------------------------------------------------------------


def test_none_input_returns_invalid_type_reason():
    parsed, reason = validate_express_consent(None)
    assert parsed is None
    assert reason == "express_consent_missing_or_invalid_type"


def test_string_input_returns_invalid_type_reason():
    parsed, reason = validate_express_consent("auto_voice_clone=true")
    assert parsed is None
    assert reason == "express_consent_missing_or_invalid_type"


def test_int_input_returns_invalid_type_reason():
    parsed, reason = validate_express_consent(42)
    assert parsed is None
    assert reason == "express_consent_missing_or_invalid_type"


def test_list_input_returns_invalid_type_reason():
    parsed, reason = validate_express_consent([{"auto_voice_clone": True}])
    assert parsed is None
    assert reason == "express_consent_missing_or_invalid_type"


# ---------------------------------------------------------------------------
# auto_voice_clone field handling
# ---------------------------------------------------------------------------


def test_missing_auto_voice_clone_treated_as_false():
    """缺字段 = 未勾选，不算错误（与 spec §3.1 soft-skip 一致）。"""
    parsed, reason = validate_express_consent({})
    assert reason is None
    assert parsed == {
        "auto_voice_clone": False,
        "client_confirmed_at": None,
    }


def test_auto_voice_clone_false_is_valid():
    parsed, reason = validate_express_consent({"auto_voice_clone": False})
    assert reason is None
    assert parsed == {
        "auto_voice_clone": False,
        "client_confirmed_at": None,
    }


def test_auto_voice_clone_true_is_valid():
    parsed, reason = validate_express_consent({"auto_voice_clone": True})
    assert reason is None
    assert parsed == {
        "auto_voice_clone": True,
        "client_confirmed_at": None,
    }


def test_auto_voice_clone_int_1_rejected_strict_bool():
    """严格 bool 校验：int 1 不是 True（防 ``1 == True`` 滑过）。"""
    parsed, reason = validate_express_consent({"auto_voice_clone": 1})
    assert parsed is None
    assert reason == "auto_voice_clone_not_bool"


def test_auto_voice_clone_int_0_rejected_strict_bool():
    parsed, reason = validate_express_consent({"auto_voice_clone": 0})
    assert parsed is None
    assert reason == "auto_voice_clone_not_bool"


def test_auto_voice_clone_string_true_rejected_strict_bool():
    parsed, reason = validate_express_consent({"auto_voice_clone": "true"})
    assert parsed is None
    assert reason == "auto_voice_clone_not_bool"


def test_auto_voice_clone_none_value_rejected():
    parsed, reason = validate_express_consent({"auto_voice_clone": None})
    assert parsed is None
    assert reason == "auto_voice_clone_not_bool"


# ---------------------------------------------------------------------------
# client_confirmed_at field handling
# ---------------------------------------------------------------------------


def test_client_confirmed_at_present_and_string():
    parsed, reason = validate_express_consent({
        "auto_voice_clone": True,
        "client_confirmed_at": "2026-05-28T03:45:21.123Z",
    })
    assert reason is None
    assert parsed == {
        "auto_voice_clone": True,
        "client_confirmed_at": "2026-05-28T03:45:21.123Z",
    }


def test_client_confirmed_at_none_keeps_normalized_none():
    parsed, reason = validate_express_consent({
        "auto_voice_clone": True,
        "client_confirmed_at": None,
    })
    assert reason is None
    assert parsed == {
        "auto_voice_clone": True,
        "client_confirmed_at": None,
    }


def test_client_confirmed_at_empty_string_normalized_to_none():
    parsed, reason = validate_express_consent({
        "auto_voice_clone": True,
        "client_confirmed_at": "   ",
    })
    assert reason is None
    assert parsed == {
        "auto_voice_clone": True,
        "client_confirmed_at": None,
    }


def test_client_confirmed_at_int_rejected():
    parsed, reason = validate_express_consent({
        "auto_voice_clone": True,
        "client_confirmed_at": 1234567890,
    })
    assert parsed is None
    assert reason == "client_confirmed_at_not_string"


def test_client_confirmed_at_dict_rejected():
    parsed, reason = validate_express_consent({
        "auto_voice_clone": True,
        "client_confirmed_at": {"iso": "2026-05-28"},
    })
    assert parsed is None
    assert reason == "client_confirmed_at_not_string"


# ---------------------------------------------------------------------------
# Output schema lockdown: NEVER contain server_confirmed_at
# ---------------------------------------------------------------------------


def test_validator_never_adds_server_confirmed_at():
    """守卫：validator 输出**绝不**含 server_confirmed_at。

    server_confirmed_at 是 caller（``gateway/job_intercept.py``）在 ``auto_voice_clone=True``
    时用 ``datetime.now(timezone.utc).isoformat()`` 后追加的，由后端
    单一来源生成。validator 永远不读 / 不写该字段。
    spec v0.3 §3.1.a。
    """
    for raw in [
        {"auto_voice_clone": True},
        {"auto_voice_clone": True, "client_confirmed_at": "2026-05-28T00:00:00Z"},
        {"auto_voice_clone": False},
        # 即便输入里含 server_confirmed_at（前端 bug / 攻击），也不该流到输出
        {"auto_voice_clone": True, "server_confirmed_at": "2020-01-01T00:00:00Z"},
    ]:
        parsed, reason = validate_express_consent(raw)
        if parsed is not None:
            assert "server_confirmed_at" not in parsed, (
                f"validator 漏字段：server_confirmed_at 不应出现在输出 dict: {parsed!r}"
            )


# ---------------------------------------------------------------------------
# Idempotency / no-mutation
# ---------------------------------------------------------------------------


def test_validator_does_not_mutate_input_dict():
    original = {
        "auto_voice_clone": True,
        "client_confirmed_at": "2026-05-28T03:45:21.123Z",
    }
    snapshot = dict(original)
    validate_express_consent(original)
    assert original == snapshot, "validator 改了输入 dict（不允许有副作用）"


# ---------------------------------------------------------------------------
# Validator never raises (even on weird input)
# ---------------------------------------------------------------------------


def test_validator_handles_weird_inputs_without_raising():
    """soft skip 语义：任何 input 都返 tuple，不抛异常。"""
    weird_inputs = [
        None,
        True,
        False,
        b"bytes",
        3.14,
        object(),
        [],
        {},
    ]
    for raw in weird_inputs:
        # 不应 raise
        parsed, reason = validate_express_consent(raw)
        # 至少有一个不是 None（要么 parsed 要么 reason）
        # （我们对 {} 的设计：返成功，parsed={"auto_voice_clone": False, ...}）
        assert isinstance(parsed, dict) or isinstance(reason, str)


# ---------------------------------------------------------------------------
# 边界：validator 与 smart_consent 严格分离
# ---------------------------------------------------------------------------


def test_validator_does_not_import_smart_consent():
    """守卫：``gateway/express_consent.py`` 源码不 import smart_consent。

    Phase 4.3a NG1 + Codex P1×4：Express consent 路径与 Smart consent 路径
    完全独立的实现。

    用 AST 精准扫 import 语句，不是文本 substring（避免 docstring 里出现
    "smart_consent" 字面量被误判）。
    """
    import ast as _ast
    src_path = REPO_ROOT / "gateway" / "express_consent.py"
    tree = _ast.parse(src_path.read_text(encoding="utf-8"))

    forbidden = {"smart_consent", "gateway.smart_consent"}
    forbidden_names = {"SmartConsent", "validate_smart_consent"}

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"express_consent.py 不应 import {alias.name} —— "
                    "Express consent 路径必须独立于 Smart consent"
                )
        elif isinstance(node, _ast.ImportFrom):
            assert node.module not in forbidden, (
                f"express_consent.py 不应 from {node.module} import ... —— "
                "Express consent 路径必须独立于 Smart consent"
            )
        elif isinstance(node, _ast.Name):
            assert node.id not in forbidden_names, (
                f"express_consent.py 引用了 SmartConsent 类名/函数名 {node.id} —— "
                "两条路径必须实现独立"
            )

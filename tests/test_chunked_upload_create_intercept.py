"""job create opaque ref 接线守卫 — plan 2026-06-11 §3.10.

``intercept_create_job`` 体量大（DB + upstream 全链），功能行为由
store 层测试覆盖（resolve_ready_upload / claim_upload ownership 矩阵）；
本文件按仓库 AST 守卫先例锁**接线契约**：

1. local_video + ``chunked:`` 前缀必须经 ``parse_chunked_source_value`` +
   ``resolve_ready_upload`` 解析，替换 ``source["value"]`` 为服务端 final_path。
2. upstream 成功后必须调 ``claim_upload`` 回写 job_id（claim 闭环）。
3. 解析失败必须 return 同一错误响应（无存在性侧信道）。
"""
from __future__ import annotations

import ast
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"


def _create_job_func() -> ast.AsyncFunctionDef:
    tree = ast.parse((GATEWAY / "job_intercept.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "intercept_create_job":
            return node
    raise AssertionError("找不到 intercept_create_job")


def _called_names(func: ast.AsyncFunctionDef) -> set[str]:
    """直接调用名 + ``asyncio.to_thread(fn, ...)`` 形态的间接调用名。"""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
                if f.attr == "to_thread":
                    for arg in node.args:
                        if isinstance(arg, ast.Name):
                            names.add(arg.id)
    return names


def test_create_job_resolves_chunked_ref():
    func = _create_job_func()
    called = _called_names(func)
    assert "parse_chunked_source_value" in called, (
        "intercept_create_job 必须经 parse_chunked_source_value 解析 chunked: ref"
    )
    assert "resolve_ready_upload" in called, (
        "intercept_create_job 必须经 resolve_ready_upload 校验 ownership+state 并取 final_path"
    )


def test_create_job_claims_upload_after_success():
    func = _create_job_func()
    called = _called_names(func)
    assert "claim_upload" in called, (
        "intercept_create_job 必须在 upstream 成功后 claim_upload 回写 job_id（§3.8 claim 闭环）"
    )


def test_create_job_replaces_source_value_with_resolved_path():
    """source["value"] 必须被服务端 final_path 覆盖（路径不作能力凭证）。"""
    func = _create_job_func()
    src = ast.unparse(func)
    assert "source['value'] = _resolved_path" in src or (
        'source["value"] = _resolved_path' in src
    ), "解析成功后必须把 source.value 替换为服务端记录的 final_path"


def test_resolver_rejects_forged_path_source_value():
    """伪造路径型 source.value（非 chunked: 前缀）不会触发 resolver——
    它走存量 local_video 路径（H1 现网加固单独任务），而 chunked: 前缀
    + 非法 id 必须被同形拒绝。"""
    import chunked_upload_store as store

    assert store.parse_chunked_source_value("/opt/aivideotrans/app/uploads/u/x.mp4") is None
    assert store.parse_chunked_source_value("chunked:/etc/passwd") == ""
    assert store.parse_chunked_source_value("chunked:" + "A" * 32) == ""  # 大写非法

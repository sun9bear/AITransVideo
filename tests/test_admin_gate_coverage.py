"""Audit appendix §10: every ``/api/admin/...`` route must enforce
``_require_admin`` (or for the voice-catalog internal endpoints,
``_require_internal_access``).

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md §10
        admin 页面后端 gate 检查清单 — 9 个前端 admin 页面的
        backend ``_require_admin`` 挂载情况"待人工核（⏳）"。
        审计当时无法直接验证。

This guard closes that hole. It walks every gateway/*.py module that
registers an ``APIRouter`` under the ``/api/admin/...`` prefix, finds
every ``@router.{method}(...)`` decorated function, and asserts the
function body (or signature) contains one of the recognised admin /
internal gate markers.

Initial sweep on 2026-05-08 found 57 admin routes across 9 files,
all gated. This test locks that in.

Adding a new admin endpoint without the gate causes this test to
fail in CI — the only way past it is to either add the gate or
explicitly extend the marker list (which itself triggers a code
review). That's the intended trade-off.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that register one or more APIRouters under ``/api/admin/...``.
# Each file must have its own ``_require_admin`` (currently 9 distinct
# copies — P2-19 will抽 to a shared helper later, but for now this
# guard is content with the per-file pattern).
#
# IMPORTANT: only include files that are TRACKED in the git index. A
# WIP-only file (e.g. user's untracked AI customer support work at the
# 2026-05-08 audit checkpoint) would make this test pass locally but
# fail on a clean CI checkout at ``assert path.is_file()``. When the
# WIP lands in committed form, add the file here and bump the baseline.
_ADMIN_FILES = (
    "gateway/admin_settings.py",
    "gateway/admin_disk_api.py",
    "gateway/admin_cosyvoice_control_api.py",
    "gateway/admin_job_monitor_api.py",
    "gateway/admin_smart_analytics_api.py",
    "gateway/cost_management.py",
    "gateway/credits_observability.py",
    "gateway/pricing_admin.py",
    "gateway/s2_monitor_api.py",
    "gateway/traffic_analytics.py",
    "gateway/voice_catalog_api.py",
)

# Recognised gate-call markers. Any of these in the function source
# (signature or body) counts as gated:
#
#   * ``_require_admin(user)`` — the most common pattern across
#     admin_settings, pricing_admin, cost_management, etc.
#   * ``Depends(_require_admin)`` — alternative form via FastAPI
#     dependency, currently unused but allowed for future migration.
#   * ``_require_internal_access`` — voice_catalog_api's internal
#     endpoints (used by gateway-internal callers, NOT user-facing
#     admin) live in the same router file but use a different gate.
_GATE_MARKERS = (
    "_require_admin(",
    "Depends(_require_admin)",
    "_require_internal_access",
)


def _collect_routes(src: str, file_label: str) -> list[tuple[int, str, str | None, str, bool]]:
    """For each function decorated with ``@router.{method}(...)`` (or
    ``@internal_router.{method}(...)``) in ``src``, return a tuple of
    ``(lineno, http_method, url_pattern, fn_name, has_gate)``."""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = False
        method = ""
        url: str | None = None
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not isinstance(dec.func, ast.Attribute):
                continue
            if (
                isinstance(dec.func.value, ast.Name)
                and dec.func.value.id in ("router", "internal_router")
            ):
                method = dec.func.attr
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    url = dec.args[0].value
                is_route = True
                break
        if not is_route:
            continue
        body_src = ast.unparse(node)
        sig_src = ast.unparse(node.args)
        has_gate = any(
            marker in body_src or marker in sig_src
            for marker in _GATE_MARKERS
        )
        out.append((node.lineno, method, url, node.name, has_gate))
    return out


def test_every_admin_route_has_gate_call():
    """The core guard: scan every admin file's @router.* decorators
    and assert each route enforces an admin / internal gate.
    """
    gaps: list[str] = []
    total = 0
    for rel in _ADMIN_FILES:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"admin file missing: {rel}"
        src = path.read_text(encoding="utf-8")
        for lineno, method, url, fn_name, has_gate in _collect_routes(src, rel):
            total += 1
            if not has_gate:
                gaps.append(
                    f"  {rel}:{lineno}  "
                    f"{method.upper():<6} {url or '?':<35}  {fn_name}"
                )
    assert total > 0, (
        "Audit §10 regression: AST scan found 0 admin routes. The "
        "_ADMIN_FILES list is stale or the gateway router registration "
        "pattern changed; this guard would silently pass without "
        "checking anything."
    )
    assert gaps == [], (
        "Audit §10 regression: the following admin route(s) do NOT "
        "enforce ``_require_admin`` (or ``_require_internal_access`` "
        "for voice-catalog internal endpoints). Without the gate, "
        "any logged-in non-admin user can call the endpoint and get "
        "back data they shouldn't see / mutate state they shouldn't "
        "control. Fix by adding the gate as the first action of the "
        "route function:\n\n    _require_admin(user)\n\n"
        "Then re-run the test.\n\n"
        "Gaps:\n" + "\n".join(gaps)
    )


def test_admin_route_count_baseline():
    """Sanity: the 2026-05-08 audit baseline counted 51 admin routes
    across the original 8 tracked admin files (the user's WIP
    ``admin_support_api.py`` adds 6 more but is intentionally
    excluded — see _ADMIN_FILES note). If the count drops sharply,
    the _ADMIN_FILES list may be missing a new admin file. If it
    grows, that's fine — new admin endpoints landed.

    This test is intentionally a soft floor (≥ baseline) rather than
    an exact match — additions are normal, but a sudden drop would
    indicate the scan stopped finding routes (e.g. router rename).
    """
    total = 0
    for rel in _ADMIN_FILES:
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        total += len(_collect_routes(src, rel))
    baseline = 51
    assert total >= baseline, (
        f"Audit §10 regression: admin route count dropped to {total}, "
        f"below the 2026-05-08 baseline of {baseline}. Either a "
        "router was renamed (this scan still keys on ``router`` / "
        "``internal_router`` identifiers) or a whole admin file was "
        "removed without updating _ADMIN_FILES."
    )

"""Contract-level regression guards for the Phase 0 post-edit scaffolding.

These tests do not exercise the running system — they encode the small set of
invariants that were easy to break during T0-3 → T0-6 implementation, so that
any regression (accidentally removing an ``editing`` branch, re-hardcoding a
status list, dropping a migration safety net) fails CI before merge.

Structure:

- **§1 Status enum invariants** — Python-side set membership.
- **§2 Python backend touch points** — AST / text scans of the files listed
  in ``docs/internal/status-touchpoints-2026-04-18.md``.
- **§3 Migration 015 contract** — upgrade / downgrade symmetry +
  ``context.autocommit_block`` for the backfill (CodeX P2 fix).
- **§4 Frontend contract parity** — scans TypeScript source files to confirm
  the same invariants are expressed on the frontend (no JS test runner set
  up in ``frontend-next``).

Pure file-reads; no DB, no network, no fixtures.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from textwrap import dedent

import pytest

from src.services.jobs.models import (
    ACTIVE_JOB_STATUSES,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_EDITING,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_WAITING_FOR_REVIEW,
    SUPPORTED_JOB_STATUSES,
    WORKER_ACTIVE_STATUSES,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    path = REPO_ROOT / rel_path
    return path.read_text(encoding="utf-8")


def _parse(rel_path: str) -> ast.AST:
    return ast.parse(_read(rel_path), filename=str(REPO_ROOT / rel_path))


# =====================================================================
# §1. Status enum invariants — "editing" has the right membership.
# =====================================================================


def test_editing_is_supported() -> None:
    assert JOB_STATUS_EDITING in SUPPORTED_JOB_STATUSES


def test_editing_is_active() -> None:
    """List polling / concurrency / cleanup-skip all depend on this."""
    assert JOB_STATUS_EDITING in ACTIVE_JOB_STATUSES


def test_editing_is_NOT_worker_active() -> None:
    """The critical fix from CodeX T0-3 round 1: editing has no worker process
    and must never be touched by the reap-stale path. If this ever flips, the
    next cleanup tick will silently mark every editing job as failed."""
    assert JOB_STATUS_EDITING not in WORKER_ACTIVE_STATUSES


def test_worker_active_is_exactly_queued_and_running() -> None:
    """Defense in depth — if someone adds waiting_for_review to
    WORKER_ACTIVE_STATUSES they'd break the existing review gate too.
    Pin the set down to the two statuses that genuinely require a live worker."""
    assert WORKER_ACTIVE_STATUSES == {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}


def test_worker_active_is_subset_of_active() -> None:
    assert WORKER_ACTIVE_STATUSES.issubset(ACTIVE_JOB_STATUSES)


def test_terminal_statuses_unchanged() -> None:
    """editing is not terminal — quota settlement must not trigger."""
    terminal = {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
    assert JOB_STATUS_EDITING not in terminal
    assert JOB_STATUS_WAITING_FOR_REVIEW not in terminal


# =====================================================================
# §2. Python backend touch points — service.py / job_intercept.py / cleanup.py
# =====================================================================


def test_service_py_reap_stale_uses_worker_active_statuses() -> None:
    """``_reap_stale_jobs`` must filter on WORKER_ACTIVE_STATUSES, not the
    broader ACTIVE_JOB_STATUSES. Otherwise editing jobs without a worker
    get reaped as stale. See touchpoints doc §0."""
    src = _read("src/services/jobs/service.py")
    # Locate the function body
    match = re.search(
        r"def _reap_stale_jobs\(self\)[^:]*:\s*(?:\"\"\"[\s\S]*?\"\"\"\s*)?"
        r"(.*?)(?=\n    def |\nclass )",
        src,
        flags=re.DOTALL,
    )
    assert match, "_reap_stale_jobs not found in service.py"
    body = match.group(1)
    assert "WORKER_ACTIVE_STATUSES" in body, (
        "_reap_stale_jobs must reference WORKER_ACTIVE_STATUSES "
        "(not hardcoded {QUEUED, RUNNING} or ACTIVE_JOB_STATUSES)"
    )


def test_service_py_is_stale_process_backed_uses_worker_active_statuses() -> None:
    src = _read("src/services/jobs/service.py")
    match = re.search(
        r"def _is_stale_process_backed_active_job\([^)]*\)[^:]*:\s*"
        r"(.*?)(?=\n    def |\nclass )",
        src,
        flags=re.DOTALL,
    )
    assert match, "_is_stale_process_backed_active_job not found"
    body = match.group(1)
    assert "WORKER_ACTIVE_STATUSES" in body, (
        "_is_stale_process_backed_active_job must filter via "
        "WORKER_ACTIVE_STATUSES; raw-set literals re-introduce the bug."
    )


def test_gateway_job_intercept_concurrency_includes_editing() -> None:
    """The per-user active-job concurrency limit must count ``editing`` so a
    user cannot sidestep the cap by entering editing mode repeatedly."""
    src = _read("gateway/job_intercept.py")
    # Find the Job.status.in_([...]) literal used in the concurrency SQL.
    # If someone regresses it to the 3-entry list, this test fails.
    matches = re.findall(r"Job\.status\.in_\(\[(.*?)\]\)", src)
    assert matches, "Gateway concurrency query not found in job_intercept.py"
    for literal in matches:
        # This literal is the concurrency guard (there is only one .in_ call
        # against Job.status in this file at the time of writing).
        entries = [s.strip().strip('"').strip("'") for s in literal.split(",")]
        assert "editing" in entries, (
            f"Concurrency guard must include 'editing' — found {entries}"
        )


def test_cleanup_py_protects_editing_and_waiting() -> None:
    """cleanup must skip editing (handled by idle_scanner) and waiting_for_review
    (user-owned). If these are removed from the protected set, editing jobs
    would start getting deleted by TTL based on updated_at."""
    src = _read("src/services/web_ui/cleanup.py")
    # The protected set is declared as a frozenset literal; grep its contents.
    match = re.search(
        r"_CLEANUP_PROTECTED_STATUSES\s*=\s*frozenset\(\s*\{([^}]+)\}",
        src,
        flags=re.DOTALL,
    )
    assert match, "_CLEANUP_PROTECTED_STATUSES not found in cleanup.py"
    literal = match.group(1)
    entries = {s.strip().strip('"').strip("'") for s in literal.split(",") if s.strip()}
    assert {"queued", "running", "waiting_for_review", "editing"}.issubset(entries), (
        f"cleanup must protect all four statuses; got {entries}"
    )


def test_cleanup_py_prefers_explicit_expires_at() -> None:
    """The resolver must read expires_at before the legacy updated_at + 7d
    fallback. If the priority flips, new expires_at values are ignored and
    every job falls back to the legacy rule."""
    src = _read("src/services/web_ui/cleanup.py")
    # _resolve_expires_at reads data.get("expires_at") first
    match = re.search(
        r"def _resolve_expires_at\([^)]*\)[^:]*:\s*"
        r"(?:\"\"\"[\s\S]*?\"\"\"\s*)?(.*?)(?=\ndef |\Z)",
        src,
        flags=re.DOTALL,
    )
    assert match, "_resolve_expires_at not found in cleanup.py"
    body = match.group(1)
    explicit_pos = body.find('"expires_at"')
    fallback_pos = body.find('"updated_at"')
    assert explicit_pos > -1, "expires_at lookup missing from _resolve_expires_at"
    assert fallback_pos > -1, "updated_at fallback missing from _resolve_expires_at"
    assert explicit_pos < fallback_pos, (
        "_resolve_expires_at must read expires_at BEFORE falling back to "
        "updated_at; the reverse order silently ignores new TTL values."
    )


# =====================================================================
# §3. Migration 015 contract — symmetric downgrade + autocommit_block
# =====================================================================


def _migration_015_tree() -> ast.Module:
    src = _read("gateway/alembic/versions/015_add_post_edit_fields.py")
    return ast.parse(src)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_migration_015_has_upgrade_and_downgrade() -> None:
    tree = _migration_015_tree()
    assert _find_function(tree, "upgrade") is not None
    assert _find_function(tree, "downgrade") is not None


def test_migration_015_downgrade_drops_every_new_column() -> None:
    """If upgrade() adds columns that downgrade() forgets to drop, migration
    is non-reversible. Compare the two op.add_column / op.drop_column sets."""
    tree = _migration_015_tree()
    upgrade = _find_function(tree, "upgrade")
    downgrade = _find_function(tree, "downgrade")
    assert upgrade and downgrade

    # op.add_column("jobs", sa.Column("display_name", ...)) — column name is
    # inside the sa.Column(...) call at arg_index=1 of op.add_column.
    added = _collect_op_names(upgrade, "add_column", arg_index=1)
    # op.drop_column("jobs", "display_name") — column name is at arg_index=1
    # of op.drop_column itself (the bare string, no nested Column() wrapper).
    dropped = _collect_op_names(downgrade, "drop_column", arg_index=1)
    assert added, "upgrade() produced no add_column calls — regression?"
    assert dropped, "downgrade() produced no drop_column calls — regression?"
    assert added == dropped, (
        f"Migration 015 upgrade/downgrade asymmetry — "
        f"added={sorted(added)} dropped={sorted(dropped)}"
    )


def test_migration_015_downgrade_drops_every_new_index() -> None:
    tree = _migration_015_tree()
    upgrade = _find_function(tree, "upgrade")
    downgrade = _find_function(tree, "downgrade")
    assert upgrade and downgrade
    added = _collect_op_names(upgrade, "create_index", arg_index=0)
    dropped = _collect_op_names(downgrade, "drop_index", arg_index=0)
    assert added, "upgrade() produced no create_index calls"
    assert dropped, "downgrade() produced no drop_index calls"
    assert added == dropped, (
        f"Migration 015 index asymmetry — "
        f"added={sorted(added)} dropped={sorted(dropped)}"
    )


def test_migration_015_backfill_wrapped_in_autocommit_block() -> None:
    """Without autocommit_block, the per-batch UPDATE loops run in a single
    Alembic transaction and the sleeps do not release row locks (CodeX P2
    T0-2 round 2).

    API note: ``op.get_context().autocommit_block()`` is the correct call.
    ``from alembic import context; context.autocommit_block()`` compiles but
    raises AttributeError at upgrade() time — the class-level ``context``
    proxy does not expose this method. An earlier version of this migration
    used the wrong form; dry-run (``alembic upgrade head --sql``) caught it
    before any production apply. This guard pins the corrected form.
    """
    tree = _migration_015_tree()
    upgrade = _find_function(tree, "upgrade")
    assert upgrade is not None

    # Match: with op.get_context().autocommit_block():
    # AST shape — With(items=[withitem(context_expr=Call(
    #   func=Attribute(attr='autocommit_block',
    #                  value=Call(func=Attribute(attr='get_context',
    #                                            value=Name('op'))))))])
    with_blocks = [n for n in ast.walk(upgrade) if isinstance(n, ast.With)]
    matched = False
    for w in with_blocks:
        for item in w.items:
            call = item.context_expr
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "autocommit_block"
            ):
                continue
            inner = call.func.value
            # Must be op.get_context(), not bare `context`.
            if not (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get_context"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "op"
            ):
                # Explicitly fail loudly if someone regresses to the buggy
                # `context.autocommit_block()` form — that was the T0-2 round 2
                # bug the dry-run caught.
                continue
            matched = True
            # Also assert the backfill UPDATEs live inside this block.
            inner_updates = [
                node
                for node in ast.walk(w)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "UPDATE jobs" in node.value
            ]
            assert inner_updates, (
                "op.get_context().autocommit_block found but no UPDATE jobs "
                "SQL inside — backfill may have been moved out."
            )
    assert matched, (
        "Migration 015 must wrap its backfill in "
        "`with op.get_context().autocommit_block():`. The short form "
        "`context.autocommit_block()` raises AttributeError at apply time."
    )


def _collect_op_names(func: ast.FunctionDef, op_name: str, arg_index: int = 0) -> set[str]:
    """Return the set of string literals passed as the ``arg_index``-th argument
    to ``op.<op_name>(...)`` inside the given function.

    For ``op.add_column(table, sa.Column("foo", ...))`` we want the inner
    Column name, so we descend one level when we detect the pattern.
    """
    out: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == op_name):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "op"):
            continue
        if arg_index >= len(node.args):
            continue
        arg = node.args[arg_index]
        # add_column passes sa.Column(name, type_) at arg_index=1 — name is
        # INSIDE the Column call, not the arg itself.
        if (
            op_name == "add_column"
            and isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "Column"
        ):
            if arg.args and isinstance(arg.args[0], ast.Constant):
                out.add(str(arg.args[0].value))
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.add(arg.value)
    return out


# =====================================================================
# §4. Frontend contract parity — scan TypeScript source files.
# =====================================================================
#
# The frontend has no JS test runner configured (no vitest / jest), so we
# encode the contracts here via regex/string scans. Noisier than a real test
# runner but sufficient for "did someone delete the editing branch?" checks.


def test_frontend_job_status_labels_includes_editing() -> None:
    src = _read("frontend-next/src/types/jobs.ts")
    assert re.search(r"editing\s*:\s*['\"]修改中['\"]", src), (
        "JOB_STATUS_LABELS must include editing: '修改中'"
    )


def test_frontend_active_job_statuses_includes_editing() -> None:
    src = _read("frontend-next/src/types/jobs.ts")
    match = re.search(
        r"ACTIVE_JOB_STATUSES\s*:\s*readonly\s+JobStatus\[\]\s*=\s*\[(.*?)\]",
        src,
        flags=re.DOTALL,
    )
    assert match, "ACTIVE_JOB_STATUSES declaration not found in types/jobs.ts"
    entries = {s.strip().strip("'").strip('"') for s in match.group(1).split(",") if s.strip()}
    assert "editing" in entries, (
        f"ACTIVE_JOB_STATUSES must contain 'editing' — found {entries}"
    )


def test_frontend_worker_active_does_not_include_editing() -> None:
    """Matches Python WORKER_ACTIVE_STATUSES contract."""
    src = _read("frontend-next/src/types/jobs.ts")
    match = re.search(
        r"WORKER_ACTIVE_JOB_STATUSES\s*:\s*readonly\s+JobStatus\[\]\s*=\s*\[(.*?)\]",
        src,
        flags=re.DOTALL,
    )
    assert match, "WORKER_ACTIVE_JOB_STATUSES declaration not found in types/jobs.ts"
    entries = {s.strip().strip("'").strip('"') for s in match.group(1).split(",") if s.strip()}
    assert "editing" not in entries, (
        f"WORKER_ACTIVE_JOB_STATUSES must NOT contain 'editing' — found {entries}"
    )
    assert entries == {"queued", "running"}, (
        f"WORKER_ACTIVE_JOB_STATUSES must match backend: exactly "
        f"{{queued, running}} — found {entries}"
    )


def test_frontend_status_badge_handles_edit_generation() -> None:
    """Regression guard for D33: running + editGeneration > 0 → "重合成中 · 第 N 次修改"."""
    src = _read("frontend-next/src/components/status-badge.tsx")
    assert "editGeneration" in src, (
        "StatusBadge must accept editGeneration prop (plan D33)"
    )
    assert "重合成中" in src, (
        "StatusBadge must produce the \"重合成中\" label for running + editGeneration > 0"
    )
    # The special-case branch must gate on both status === 'running' and
    # editGeneration > 0; a regression that widens the check (e.g. applies
    # to every status) would silently relabel succeeded/failed jobs.
    assert re.search(r"status\s*===\s*[\"']running[\"']", src), (
        "重合成中 branch must be gated on status === 'running'"
    )


def test_frontend_expiry_prefers_explicit_expires_at() -> None:
    src = _read("frontend-next/src/features/jobs/expiry.ts")
    # The TS helper reads `job.expiresAt` before falling back to
    # `job.updatedAt`; pin the ordering.
    explicit = src.find("job.expiresAt")
    fallback = src.find("job.updatedAt")
    assert explicit > -1 and fallback > -1, (
        "expiry.ts must reference both job.expiresAt and job.updatedAt"
    )
    assert explicit < fallback, (
        "expiry.ts must check job.expiresAt BEFORE falling back to "
        "job.updatedAt, matching backend _resolve_expires_at priority."
    )
    # Legacy fallback uses 7-day retention.
    assert "LEGACY_RETENTION_DAYS" in src
    assert "7" in src, "expiry.ts must still encode the 7-day legacy fallback"


def test_frontend_projects_page_has_editing_case() -> None:
    """The list page's ExpandedContent switch must have a dedicated editing
    branch so the card renders the "继续修改" CTA instead of falling through
    to ``default: return null``."""
    src = _read("frontend-next/src/app/(app)/projects/page.tsx")
    assert re.search(r"case\s+[\"']editing[\"']", src), (
        "projects/page.tsx switch must include case 'editing'"
    )
    assert "继续修改" in src, (
        "projects/page.tsx must render a \"继续修改\" CTA for editing jobs"
    )


def test_frontend_projects_page_edit_button_is_feature_flag_gated() -> None:
    """D43 direct-access button must be gated so Phase 0 never exposes the
    edit route to users. Scan for both the flag and the three-way condition."""
    src = _read("frontend-next/src/app/(app)/projects/page.tsx")
    assert "NEXT_PUBLIC_ENABLE_POST_EDIT" in src, (
        "feature flag env var must be referenced"
    )
    assert "POST_EDIT_ENABLED" in src
    # Scan for the three gate conditions: feature flag + studio + succeeded.
    assert re.search(
        r"POST_EDIT_ENABLED[^\n]*serviceMode[^\n]*[\"']studio[\"'][^\n]*[\"']succeeded[\"']",
        src,
    ) or re.search(
        r"serviceMode[^\n]*[\"']studio[\"'][^\n]*[\"']succeeded[\"'][^\n]*POST_EDIT_ENABLED",
        src,
    ), (
        "D43 button gate must combine POST_EDIT_ENABLED + serviceMode === 'studio' "
        "+ status === 'succeeded' on a single expression"
    )


def test_frontend_workspace_page_editing_does_not_trigger_generic_cancel() -> None:
    """Editing sessions must NOT surface the generic "取消任务" button on the
    workspace header — cancelling an editing session drops the user's draft
    and must route through the edit page's own二次确认 flow (plan §7.6)."""
    src = _read("frontend-next/src/app/(app)/workspace/[jobId]/page.tsx")
    assert "isEditing" in src, "workspace page must derive an isEditing flag"
    # Find the cancel button block. Its render guard is an expression using
    # `isWaitingForReview` / `isProcessing` — confirm `isEditing` is NOT in
    # the condition list.
    match = re.search(
        r"\{[^{}]*?(?:isWaitingForReview|isProcessing)[^{}]*?\?\s*\(\s*<button[\s\S]*?"
        r"(?:取消任务|取消中)[\s\S]*?\)\s*:\s*null[^{}]*?\}",
        src,
    )
    assert match is not None, (
        "Expected the header cancel-button conditional block to be present"
    )
    cancel_guard = match.group(0)
    assert "isEditing" not in cancel_guard, (
        "The generic cancel-task button must NOT be rendered for editing "
        "jobs. Route editing cancel through /workspace/{id}/edit instead."
    )


def test_frontend_workspace_page_has_editing_section() -> None:
    src = _read("frontend-next/src/app/(app)/workspace/[jobId]/page.tsx")
    assert re.search(r"\{isEditing\s*\?", src), (
        "workspace page must render a dedicated {isEditing ? ...} section"
    )
    assert "继续修改" in src, (
        "workspace page editing section must offer 继续修改 CTA"
    )


def test_touchpoints_document_exists() -> None:
    """Phase 0 T0-1 strong-dependency artifact — T0-3…T0-6 were built against it."""
    path = REPO_ROOT / "docs/internal/status-touchpoints-2026-04-18.md"
    assert path.is_file(), (
        "T0-1 touchpoints clipboard is missing — it's the canonical reference "
        "for every state-branch edit in Phase 0. See plan §4.3."
    )


# Kept out of the paragraph above for clarity: pytest will show docstrings
# in -v mode; keeping them short here.
del dedent  # unused helper, drop reference

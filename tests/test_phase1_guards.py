"""Contract-level guards for Phase 1 post-edit surface (T1-12).

These tests do not exercise the running system — they pin invariants
that the Phase 1 implementation depends on, so that a future refactor
or a silent upstream change cannot break the editing flow undetected.

§1 Paid-API guard — the single most important invariant (plan D26):
    The commit pipeline (alignment → publish) must NEVER call the
    tts_generator. If a regression makes commit silently re-invoke TTS,
    users who just hit "commit" would be charged again for a segment
    they already paid to synthesize. This AST scan raises if any module
    under src/modules/alignment/ or src/modules/output/ references
    tts_generator at function-call depth.

§2 Editing module structure — the files documented in plan §13.4 must
    exist and expose the expected public API names. Renaming without
    updating the API client would silently break the frontend.

§3 Gateway feature flag coverage — every editing mutation subpath must
    be recognised by _is_post_edit_mutation_subpath(). Missing an entry
    means that endpoint stays accessible even when the flag is off.

§4 Segment / commit strategy whitelists — frozen so frontend contracts
    (D34 / D36 / T1-5) stay in sync.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# =====================================================================
# §1 Paid-API guard — commit pipeline must NOT re-invoke tts_generator
# =====================================================================


_FORBIDDEN_TTS_FUNCS = ("generate", "generate_audio", "synthesize", "synthesise")
_FORBIDDEN_TTS_MODULE_TOKENS = ("tts_generator", "segment_regenerate")


def _module_paths_under(rel_dir: str) -> list[Path]:
    root = REPO_ROOT / rel_dir
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*.py") if "__pycache__" not in str(p)]


def _scan_tts_calls(module_paths: list[Path]) -> list[tuple[str, str]]:
    """Return list of (file_path, offending_snippet) tuples where a
    forbidden tts_generator call is found. Snippet is the AST node source
    line for debugging."""
    offenders: list[tuple[str, str]] = []
    for path in module_paths:
        src = path.read_text(encoding="utf-8", errors="replace")
        if not any(tok in src for tok in _FORBIDDEN_TTS_MODULE_TOKENS):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func
                # Match tts_generator.generate_*() / segment_regenerate.*()
                if isinstance(callee, ast.Attribute):
                    root_name = _attr_root(callee)
                    if root_name in _FORBIDDEN_TTS_MODULE_TOKENS:
                        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else "<?>"
                        offenders.append((str(path.relative_to(REPO_ROOT)), line.strip()))
                elif isinstance(callee, ast.Name) and callee.id in _FORBIDDEN_TTS_MODULE_TOKENS:
                    line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else "<?>"
                    offenders.append((str(path.relative_to(REPO_ROOT)), line.strip()))
    return offenders


def _attr_root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def test_alignment_modules_do_not_call_tts_generator() -> None:
    """D26 — alignment stage MUST NOT re-synthesize TTS during commit.
    If this test fails, the commit pipeline would silently double-charge
    users after every overwrite."""
    offenders = _scan_tts_calls(_module_paths_under("src/modules/alignment"))
    assert not offenders, (
        "commit alignment path contains tts_generator call(s):\n"
        + "\n".join(f"  {p}: {line}" for p, line in offenders)
    )


def test_publish_modules_do_not_call_tts_generator() -> None:
    offenders = _scan_tts_calls(_module_paths_under("src/modules/output"))
    assert not offenders, (
        "commit publish path contains tts_generator call(s):\n"
        + "\n".join(f"  {p}: {line}" for p, line in offenders)
    )


def test_editing_commit_pipeline_does_not_call_tts_generator() -> None:
    """Direct scan of the module that commit_editing_pipeline lives in."""
    path = REPO_ROOT / "src" / "services" / "jobs" / "editing_commit.py"
    assert path.is_file()
    src = path.read_text(encoding="utf-8")
    assert "tts_generator" not in src, (
        "editing_commit.py imports / references tts_generator; commit "
        "must NEVER regenerate TTS (D26 invariant). Draft TTS comes only "
        "from user-initiated endpoints."
    )
    # Also ensure NotImplementedError-guarded segment_regenerate is not called.
    assert "regenerate_segment_tts" not in src, (
        "editing_commit.py calls regenerate_segment_tts; commit must use "
        "existing drafts only."
    )


def test_both_job_api_entry_points_apply_runtime_wiring() -> None:
    """The Job API has two entry points — ``main.run_job_api_command``
    (developer / single-binary) and
    ``scripts/run_remote_workbench_service.py._run_job_api``
    (container / linux_app_service.sh). Both MUST call
    ``apply_runtime_wiring`` before starting the HTTP server, otherwise
    one entry silently drops T1-10 idle-cancel callback wiring and the
    A.2 segment TTS caller wiring (regression found 2026-04-19: the
    container path had never installed the post-edit TTS caller —
    regenerate-tts returned 501 forever despite main.py doing it).

    The check is intentionally a plain substring scan — an AST-level
    import-graph check would also catch it, but a typo-level substring
    assert keeps the contract trivially readable and makes the failure
    message obvious when someone adds a third entry point and forgets.
    """
    checks = [
        ("main.py", "apply_runtime_wiring"),
        ("scripts/run_remote_workbench_service.py", "apply_runtime_wiring"),
    ]
    missing: list[str] = []
    for rel, needle in checks:
        src = _read(rel)
        if needle not in src:
            missing.append(f"{rel}: missing {needle!r}")
    assert not missing, (
        "Job API entry point(s) do not install runtime wiring:\n"
        + "\n".join(f"  {m}" for m in missing)
    )


def test_paid_api_surface_isolated_from_commit_alignment_publish() -> None:
    """segment_regenerate is the SOLE production entry point for paid TTS
    calls in the post-edit flow (wired into JobService at Job API boot
    from main.py). Any other production module importing it would create
    a code path that could touch paid providers outside of a user-initiated
    click — violating the CLAUDE.md paid-API policy.

    Scope: alignment + output pipeline modules + editing_commit. Tests are
    allowed to import freely."""
    offenders: list[str] = []
    for rel_dir in ("src/modules/alignment", "src/modules/output"):
        for path in _module_paths_under(rel_dir):
            src = path.read_text(encoding="utf-8", errors="replace")
            if "segment_regenerate" in src:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    commit_path = REPO_ROOT / "src" / "services" / "jobs" / "editing_commit.py"
    if commit_path.is_file():
        commit_src = commit_path.read_text(encoding="utf-8")
        if "segment_regenerate" in commit_src:
            offenders.append(str(commit_path.relative_to(REPO_ROOT)))

    assert not offenders, (
        "segment_regenerate leaked into commit/alignment/publish:\n"
        + "\n".join(f"  {p}" for p in offenders)
    )


# =====================================================================
# §2 Editing module structure
# =====================================================================


_EXPECTED_MODULES = {
    "src/services/jobs/editing.py": [
        "enter_editing", "cancel_editing", "commit_editing",
        "touch_editing", "EditingConflictError", "EDITING_SUBDIR",
    ],
    "src/services/jobs/editing_segments.py": [
        "load_editing_segments", "patch_editing_segment",
        "mark_segment_status", "editing_payload",
        "SEGMENT_STATUS_TEXT_DIRTY", "SEGMENT_STATUS_TTS_DIRTY",
        "PATCHABLE_SEGMENT_FIELDS",
    ],
    "src/services/jobs/editing_tts.py": [
        "regenerate_segment_tts", "accept_draft_tts", "discard_draft_tts",
        "SegmentTTSCaller", "TtsNotWiredError", "DRAFT_TTS_SUBDIR",
    ],
    "src/services/jobs/editing_voice_map.py": [
        "load_voice_map", "set_voice_override", "clear_voice_override",
        "VOICE_MAP_FILE",
    ],
    "src/services/jobs/editing_batch.py": [
        "regenerate_all_dirty_segments",
        "BATCH_REGENERATE_TRIGGER_STATUSES",
    ],
    "src/services/jobs/editing_commit.py": [
        "commit_editing_pipeline", "CommitPipelineError",
    ],
    "src/services/jobs/copy_service.py": [
        "prepare_copy_project_dir", "hardlink_baseline_audio",
        "apply_draft_segment", "write_audio_safely",
        "rollback_prepared_target",
    ],
    "src/services/jobs/runner_extensions.py": [
        "submit_job_from_existing_project_dir", "SUPPORTED_START_STAGES",
    ],
    "src/services/jobs/input_validators.py": [
        "validate_segment_id", "SEGMENT_ID_RE", "validate_commit_strategy",
    ],
    "src/services/web_ui/editing_idle_scanner.py": [
        "scan_editing_idle", "find_idle_editing_jobs",
        "inject_editing_cancel_callback", "reset_editing_cancel_callback",
        "IDLE_THRESHOLD_HOURS", "REASON_IDLE_AUTO",
    ],
    "src/services/jobs/logs_redactor.py": [
        "build_default_redactor", "Redactor", "REDACTED_PLACEHOLDER",
    ],
}


@pytest.mark.parametrize("module_rel,symbols", list(_EXPECTED_MODULES.items()))
def test_module_exposes_expected_api(module_rel: str, symbols: list[str]) -> None:
    path = REPO_ROOT / module_rel
    assert path.is_file(), f"missing module: {module_rel}"
    src = path.read_text(encoding="utf-8")
    for name in symbols:
        # Substring scan is sufficient — all these symbols are declared at
        # module top-level with "def name" / "class name" / "NAME = ...".
        # Catches accidental renames without pulling in AST.
        if not re.search(rf"(?:^|\n)(?:def |class |async def |{re.escape(name)}\s*[:=])", src):
            pytest.fail(f"module {module_rel} missing expected symbol: {name}")


# =====================================================================
# §3 Gateway feature flag coverage
# =====================================================================


def test_gateway_knows_every_post_edit_endpoint() -> None:
    """Every editing mutation subpath the Job API exposes must be listed
    in ``_is_post_edit_mutation_subpath`` so the feature flag (D29) gates
    it. A missing entry would leave the endpoint accessible when the flag
    is off — silent feature leak."""
    gateway_src = _read("gateway/job_intercept.py")

    # State transitions
    for subpath in ("enter-edit", "editing/cancel", "editing/commit"):
        assert (
            f'"{subpath}"' in gateway_src or f"'{subpath}'" in gateway_src
        ), f"gateway does not recognise transition subpath: {subpath}"

    # Simple mutations (T1-6)
    for subpath in ("regenerate-all-tts", "editing/voice-map"):
        assert (
            f'"{subpath}"' in gateway_src or f"'{subpath}'" in gateway_src
        ), f"gateway does not recognise simple mutation subpath: {subpath}"

    # Segment-scoped actions: must appear in the segments action whitelist
    for action in ("update", "status", "regenerate-tts", "accept-draft", "discard-draft"):
        assert (
            f'"{action}"' in gateway_src or f"'{action}'" in gateway_src
        ), f"gateway segments action allowlist missing: {action}"


def test_gateway_logs_redaction_gated_on_role() -> None:
    """GET /logs must be intercepted for role-based redaction (D25).
    If the interception branch is removed, non-admins leak provider names."""
    src = _read("gateway/job_intercept.py")
    assert "_serve_redacted_logs" in src, (
        "gateway must wire _serve_redacted_logs into intercept_job_subresource"
    )
    assert 'subpath == "logs"' in src, (
        "gateway must dispatch /logs through the redactor interceptor"
    )


# =====================================================================
# §4 Whitelists
# =====================================================================


def test_supported_commit_strategies_locked() -> None:
    """Contract with the frontend CommitStrategy type union."""
    from services.jobs.editing_commit import commit_editing_pipeline  # noqa: F401
    from services.jobs.input_validators import validate_commit_strategy

    assert validate_commit_strategy("overwrite") == "overwrite"
    assert validate_commit_strategy("copy_as_new") == "copy_as_new"
    with pytest.raises(ValueError):
        validate_commit_strategy("force_push")


def test_supported_segment_statuses_frontend_parity() -> None:
    """Contract: the SegmentStatus TS union in lib/api/editing.ts must
    be a subset of the Python SUPPORTED_SEGMENT_STATUSES."""
    from services.jobs.editing_segments import SUPPORTED_SEGMENT_STATUSES

    ts_src = _read("frontend-next/src/lib/api/editing.ts")
    # Look at the SegmentStatus union
    match = re.search(r"type SegmentStatus\s*=\s*(.+?)(?:\n\n|\nexport)", ts_src, re.DOTALL)
    assert match, "SegmentStatus type union missing from editing.ts"
    literal_block = match.group(1)
    frontend_literals = set(re.findall(r'"([a-z_]+)"', literal_block))
    # Every TS literal must exist in Python set
    missing = frontend_literals - SUPPORTED_SEGMENT_STATUSES
    assert not missing, (
        f"frontend SegmentStatus has literals not in Python SUPPORTED_SEGMENT_STATUSES: {missing}"
    )


def test_commit_strategy_frontend_parity() -> None:
    """Frontend CommitStrategy union ≡ backend SUPPORTED_COMMIT_STRATEGIES."""
    from services.jobs.editing import SUPPORTED_COMMIT_STRATEGIES

    ts_src = _read("frontend-next/src/lib/api/editing.ts")
    match = re.search(r"CommitStrategy\s*=\s*(.+?)(?:\n\n|\nexport)", ts_src, re.DOTALL)
    assert match, "CommitStrategy union missing from editing.ts"
    frontend = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert frontend == set(SUPPORTED_COMMIT_STRATEGIES), (
        f"frontend CommitStrategy {frontend} != backend {set(SUPPORTED_COMMIT_STRATEGIES)}"
    )


def test_stage_alignment_in_supported_public_stages() -> None:
    """T1-8: runner_extensions requires STAGE_ALIGNMENT to be a recognised
    public stage or JobRecord.__post_init__ will reject records that set it."""
    from services.jobs.models import STAGE_ALIGNMENT, SUPPORTED_PUBLIC_STAGES
    assert STAGE_ALIGNMENT in SUPPORTED_PUBLIC_STAGES


# =====================================================================
# §4b editor/segments.json baseline wiring
# =====================================================================


def test_pipeline_publish_writes_editor_segments_baseline() -> None:
    """Pipeline S6 publish stage must invoke ``write_editor_segments_from_translation``
    so newly completed Studio tasks have an editor/segments.json baseline on
    disk without relying on editing.enter_editing's lazy fallback. The lazy
    fallback is safety-net only — for new tasks it must never fire because
    every first-time click-修改 then pays the translation → editor copy cost
    and loses the "publish shipped a baseline" invariant.

    This scan looks for the import AND the call, since an import without a
    call would be a silent regression (someone refactoring in pieces)."""
    src = _read("src/pipeline/process.py")
    tree = ast.parse(src)

    import_found = False
    call_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.endswith("editor_baseline"):
                names = {alias.name for alias in node.names}
                if "write_editor_segments_from_translation" in names:
                    import_found = True
        if isinstance(node, ast.Call):
            func = node.func
            # Direct-name call (from ... import write_editor_segments_from_translation)
            if isinstance(func, ast.Name) and func.id == "write_editor_segments_from_translation":
                call_found = True
            # Attribute call (editor_baseline.write_editor_segments_from_translation)
            elif isinstance(func, ast.Attribute) and func.attr == "write_editor_segments_from_translation":
                call_found = True

    assert import_found, (
        "src/pipeline/process.py must import "
        "write_editor_segments_from_translation from services.jobs.editor_baseline"
    )
    assert call_found, (
        "src/pipeline/process.py must call write_editor_segments_from_translation "
        "(plan follow-up to T1-3: pipeline publish owns the baseline; "
        "editing.enter_editing fallback is safety-net only)"
    )


def test_editing_enter_delegates_baseline_seed_to_shared_helper() -> None:
    """editing.py must NOT duplicate the seed logic — both the pipeline
    writer and the legacy fallback need to behave identically (same
    normalisation, same error shape) so a task whose baseline came from
    path 1 is indistinguishable from one whose baseline came from path 2.

    Duplicated implementations drift. If a future edit adds a second
    json.dumps of a segments list inside editing.py it is almost certainly
    a regression — route it through editor_baseline instead."""
    src = _read("src/services/jobs/editing.py")
    tree = ast.parse(src)

    # Shared helper must be imported.
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.endswith("editor_baseline"):
                names = {alias.name for alias in node.names}
                if "write_editor_segments_from_translation" in names:
                    imported = True
                    break
    assert imported, (
        "editing.py must import write_editor_segments_from_translation "
        "from services.jobs.editor_baseline (seed logic is shared with "
        "the pipeline publish path)"
    )

    # No local json.dumps of a "segments" list — that would indicate
    # someone reinlined the seed logic.
    assert "json.dumps" not in src, (
        "editing.py must NOT json.dumps anything on its own; any segment "
        "serialisation belongs in editor_baseline or the store layer"
    )


# =====================================================================
# §5 Frontend ↔ backend endpoint path parity
# =====================================================================


@pytest.mark.parametrize(
    "frontend_call,expected_backend_path",
    [
        ("enterEditing", "enter-edit"),
        ("cancelEditing", "editing/cancel"),
        ("commitEditing", "editing/commit"),
        ("getEditingSegments", "editing/segments"),
        ("patchSegmentText", "segments/${segmentId}/update"),
        ("markSegmentStatus", "segments/${segmentId}/status"),
        ("regenerateSegmentTts", "segments/${segmentId}/regenerate-tts"),
        ("acceptSegmentDraft", "segments/${segmentId}/accept-draft"),
        ("discardSegmentDraft", "segments/${segmentId}/discard-draft"),
        ("regenerateAllDirtyTts", "regenerate-all-tts"),
        ("getRegenerateAllStatus", "regenerate-all-tts/status"),
        ("getVoiceMap", "editing/voice-map"),
        ("setVoiceOverride", "editing/voice-map"),
        ("clearVoiceOverride", "editing/voice-map"),
    ],
)
def test_frontend_api_client_hits_backend_path(
    frontend_call: str,
    expected_backend_path: str,
) -> None:
    """Lightweight path contract: editing.ts references the matching
    backend path near the client function declaration. Scoped by locating
    the ``function {name}`` declaration and scanning the next ~30 lines —
    sufficient granularity since each path is unique per call site."""
    src = _read("frontend-next/src/lib/api/editing.ts")
    marker = f"function {frontend_call}"
    idx = src.find(marker)
    assert idx >= 0, f"function {frontend_call} not found in editing.ts"
    # Scope: 30 lines after the declaration (generous enough for all
    # current call shapes).
    window = "\n".join(src[idx:].splitlines()[:30])
    assert expected_backend_path in window, (
        f"function {frontend_call} body does not reference backend path "
        f"{expected_backend_path!r}"
    )

# REFACTOR_PHASE1_SUMMARY

Date: 2026-03-18

## Scope

This summary closes the first major refactor pass driven by `AIVideoTrans_Codex_执行版总文档_最终版.md`, and updates that closure to match the code now present in this workspace.

Goal of Phase 1:

- establish one canonical workflow build layer
- split editor and publish output boundaries
- add a shared output dispatch contract
- keep legacy paths compatible while the new architecture stabilizes

## Completed Outcomes

### 1. Canonical build layer exists

Added and wired:

- `src/core/artifact_index.py`
- `src/core/project_model.py`
- `src/modules/workflow/workflow_result.py`
- `src/modules/workflow/project_builder.py`
- `ProjectWorkflow.run_build()`

Result:

- workflow now produces a canonical `WorkflowBuildResult`
- `LocalizedProject` and `ArtifactIndex` are real first-class objects
- legacy `run()` still works through compatibility conversion

### 2. Audio preparation is no longer purely legacy-owned

Added and wired:

- `src/services/audio/source_audio_preparation.py`
- workflow-level `audio_preparation` handling

Result:

- `source.original_audio`
- `working.speech_for_asr`
- `working.ambient_audio`

are now part of the shared artifact vocabulary and workflow state.

### 3. Editor output was split out

Added and wired:

- `src/modules/output/editor/editor_package_backend.py`
- `src/modules/output/editor/editor_package_writer.py`
- `src/modules/output/editor/draft_backend.py`

Result:

- editor output is now a formal backend family
- `draft` is explicitly treated as an editor sub-capability
- `src/modules/output/project_output.py` remains only as a compatibility shim

### 4. Minimal publish output exists

Added and wired:

- `src/modules/output/publish/publish_backend.py`
- `src/modules/output/publish/video_renderer.py`

Result:

- the repository now has a minimal publish path for `dubbed_video.mp4`
- publish currently requires a source video artifact
- this is intentionally thin and does not yet claim full publish completeness

### 5. Unified output dispatch exists

Added and wired:

- `OutputTarget`
- `OutputRequest`
- `OutputBundleResult`
- `OutputDispatcher`

Result:

- the same canonical build can now route to:
  - `editor`
  - `publish`
  - `both`

### 6. Manifest output exists

Added and wired:

- `src/modules/output/manifest_writer.py`

Result:

- workflow-driven output dispatch writes `manifest.json`
- `manifest_path` is part of `OutputBundleResult`
- manifest output is no longer just planned; it is implemented

### 7. Workflow CLI path now reaches dispatcher

Current state:

- workflow-driven demo commands use `run_build() -> OutputDispatcher`
- `local-audio-demo` and `local-video-demo` accept `--output editor|publish|both`
- `local-audio-demo` still rejects publish in practice because there is no source video to render from

## Compatibility Decisions Kept On Purpose

The following were intentionally *not* retired in this pass:

- `src/pipeline/process.py`
- the `process` command in `main.py`
- `src/modules/output/project_output.py`
- legacy-compatible `run()` result shape

Reason:

- the execution doc favored phased migration over early physical cleanup
- these paths still protect practical end-to-end usage while the shared build layer stabilizes
- `process` still remains the most complete YouTube-oriented runtime entry today

## Current Boundary After Phase 1

This is the most important architectural truth to preserve:

- workflow is the intended future mainline
- `process` is still the most complete compatibility shell
- these two facts coexist intentionally right now

In other words:

- Phase 1 did **not** finish the migration of legacy runtime behavior into the new mainline
- it finished the shared build/output foundation needed for that later convergence

## Validation

Latest verification in this workspace:

- `pytest -q` -> `474 passed, 2 warnings`
- `python main.py --help` prints the CLI usage text

Important note:

- the current `--help` path still exits via the usage/SystemExit branch, so the help text prints successfully but the process exits with code `1`

## Known Remaining Work

### 1. Controlled convergence

The highest-value remaining architectural task is:

- decide and execute the gradual path for `process` to consume `run_build()` and/or `OutputDispatcher`

Current working decision:

- `PROCESS_WORKFLOW_CONVERGENCE.md`

This matters more than adding another isolated export surface.

### 2. Publish remains intentionally minimal

Publish should currently be described as:

- implemented
- minimal
- video-backed
- not yet feature-complete

Remaining publish work stays outside Phase 1 closure:

- subtitle burn-in
- original-audio mixing
- richer publish controls

### 3. Environment and runtime documentation

The project now also needs stronger run-environment documentation:

- dependency baseline
- `yt-dlp` expectations
- minimal runnable setup
- Windows / Unix differences

## Post-Closure Convergence Progress

After the Phase 1 closure, the repository has continued a controlled convergence path without reopening scope.

Important progress now true in the workspace:

- legacy `process` output dispatches through `OutputDispatcher`
- legacy output stage payload now trusts `OutputBundleResult` as the manifest/output truth source
- canonical build-shape assembly is now more shared between workflow and process via:
  - `ProjectBuilder`
  - `project_shape_helpers`

Important boundary that still remains:

- `process` still interprets YouTube/process-specific runtime facts
- `process` still owns review gates and process-only compatibility state
- `process` still does not consume `ProjectWorkflow.run_build()`

So the project is past the point where `process` owns its own canonical source/artifact shape,
but it is not yet at the point where `process` is only a thin wrapper over the full workflow build path.

## Bottom Line

Phase 1 is complete in the sense that the repository now has:

- a canonical workflow build layer
- an explicit editor backend family
- a minimal publish backend family
- a shared dispatcher
- manifest output
- a still-working compatibility bridge for legacy runtime paths

What remains is **controlled convergence and scope clarification**, not another ground-up rewrite.

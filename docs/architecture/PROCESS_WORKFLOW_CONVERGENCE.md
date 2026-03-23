# PROCESS_WORKFLOW_CONVERGENCE

Date: 2026-03-18

## Decision

The project should take **Option B**:

- keep `src/pipeline/process.py` as the practical compatibility shell for now
- gradually move it to consume `ProjectWorkflow.run_build()` and `OutputDispatcher`
- avoid a hard cut that would break current YouTube-oriented end-to-end behavior

This means `process` should stop evolving as an independent architecture center.

Its job is to become a compatibility entry that reuses the newer canonical build and output path wherever that reuse is safe.

## Why This Is The Right Next Step

Current reality:

- the canonical workflow/build layer already exists
- workflow demos already use `run_build() -> OutputDispatcher`
- `process` still owns the most complete practical runtime behavior
- Web UI and operator flows still depend on that practical behavior

If the project keeps expanding Web UI and legacy runtime behavior before convergence, it will keep paying the cost of two truth sources:

- one truth source for workflow-driven build/output
- another truth source for legacy end-to-end execution

That is now the highest-value architecture risk to reduce.

## What This Decision Does Not Mean

This decision does **not** mean:

- deleting `process` immediately
- rewriting the whole runtime stack in one pass
- blocking bug fixes or small Web UI stability work
- claiming publish is fully complete

The project should still preserve:

- existing YouTube-oriented runs
- review gates that are already working in practice
- current operator entrypoints in `main.py`

## Desired End State

The target shape is:

- one canonical pre-output build path
- one shared output dispatch path
- one practical compatibility shell for legacy entrypoints

In that end state:

- `process` still exists as a command surface if it is useful
- but it no longer owns a separate build/output truth
- editor/publish artifacts come from the same canonical build vocabulary

## Progress So Far

Recent controlled convergence slices have already landed:

- `process` output now dispatches through `OutputDispatcher`
- legacy output stage state now trusts `OutputBundleResult` for manifest/output truth
- `process` no longer hand-assembles canonical source/artifact shape directly
- shared canonical shape rules now live in:
  - `ProjectBuilder`
  - `project_shape_helpers`

Current boundary after those slices:

- `ProjectBuilder` owns canonical object assembly:
  - `ArtifactIndex`
  - `LocalizedProject`
  - `WorkflowBuildResult`
- `project_shape_helpers` owns canonical normalization/grouping rules for:
  - `source_info`
  - shared artifact-entry families
- `process` still owns legacy runtime fact interpretation and process-only compatibility state

This is still a bridge state, but it is a smaller bridge than before.

## Convergence Order

The work should be done in this order.

### Step 1. Output convergence first

Highest-value first move:

- make `process` route more output handling through `OutputDispatcher`
- reduce direct legacy output branching where practical
- keep current visible behavior stable

Why first:

- output is already the cleanest shared boundary
- this gives immediate reduction in duplicated output logic
- it avoids mixing review-gate migration with output migration too early

### Step 2. Asset/build convergence second

After output convergence is stable:

- move more `process` asset preparation to shared workflow vocabulary
- prefer canonical artifact names and locations
- reduce special-case output assumptions owned only by `process`
- move canonical shape rules out of `process` before moving review/runtime behavior

Why second:

- this is where the project stops maintaining two build mental models
- it prepares the path for broader reuse by Web UI and future operator tools

### Step 3. Review-gate convergence third

Only after the shared build/output path is trusted:

- pull transcript/speaker review and translation review boundaries closer to workflow-owned state
- keep current `review_state.json` behavior stable while refactoring
- avoid breaking the current operator loop

Why third:

- review gates are user-visible and easy to regress
- they should move after the lower-level output/build path is less risky

### Step 4. Physical retirement last

Only after the above steps are proven:

- shrink compatibility shims
- retire dead branches
- simplify `main.py` command internals where safe

Do not start with this step.

## Immediate Next Execution Slice

The next concrete work should be a small, controlled slice:

1. document which parts of `process` still own legacy runtime fact interpretation
2. keep canonical shape rules in shared helpers rather than growing new `process` bridge code
3. identify the next smallest process-only artifact/source boundary that can be normalized safely
4. add regression coverage around each migrated boundary

This should be treated as the next architecture task before returning to larger Web UI expansion.

## What Still Clearly Stays In `process`

For the current phase, the following still belong to `process.py`:

- YouTube/process-specific source fact interpretation
- download/cache reuse decisions
- file existence checks and path selection
- process-only artifact entries such as:
  - `source.download_metadata`
  - `state.review`
  - `state.project`
- review gate behavior, including:
  - `speaker_review`
  - `translation_review`
  - `voice_review`
- TTS/alignment/runtime recovery behavior

These are not the right next extraction targets until lower-risk canonical boundaries are exhausted.

## Web UI Implication

Web UI should continue to receive:

- bug fixes
- usability fixes
- stability fixes

But the next major product push should wait until this convergence moves forward.

In practical terms:

- do not treat Web UI as the next main expansion front
- finish the convergence decision first
- then return to `音频试听与对齐`, partial reruns, and richer review loops on a more stable backend boundary

## Short Project Rule

For the next phase, prefer this rule:

`process` may stay as the operator shell, but it should progressively stop being a second architecture.

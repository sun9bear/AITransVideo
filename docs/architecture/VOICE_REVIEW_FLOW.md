# VOICE_REVIEW_FLOW

Last updated: 2026-03-18

## Purpose

`voice_review` exists to prevent an otherwise-recoverable auto-clone failure from killing the whole run.

It is the human-review checkpoint used when automatic sample extraction cannot produce a MiniMax-compatible voice-clone sample.

## Current Requirement Boundary

The current runtime is aligned to the MiniMax clone-duration boundary:

- minimum sample duration: `10s`
- maximum sample duration: `300s`

Current local extraction behavior:

- try to keep collecting same-speaker speech until the sample is close to `300s`
- never exceed `300s`
- join nearby same-speaker clips when practical
- skip obviously unusable low-volume clips

## Trigger Condition

`voice_review` is entered only when all of the following are true:

1. the run is using the review-capable path (`--wait-for-review`, which the Web UI uses)
2. the speaker does not already resolve to a usable voice from the local voice registry / defaults
3. automatic sample extraction finishes with a sample shorter than `10s`

If the run is not review-capable, the same condition still raises a clear failure instead of pausing.

## End-to-End Flow

### 1. Voice resolution starts

For each speaker that still has no usable `voice_id`, `process` tries:

- speaker-specific voice registry hit
- project default fallback if applicable
- automatic sample extraction
- automatic MiniMax clone

### 2. Sample extraction tries to maximize useful audio

The extractor now prefers:

- longer same-speaker material
- adjacent clips that can be merged cleanly
- total sample duration closer to `300s`

It no longer stops as soon as a minimal local threshold is reached.

### 3. Short sample pauses into review

If the extracted sample is still shorter than `10s`, `process` writes a pending review stage:

- stage name: `voice_review`
- tab mapping: `voice-library`
- persisted file: `review_state.json`

The payload includes:

- `speaker_id`
- `speaker_label`
- `speaker_name`
- `voice_arg_name`
- `sample_path`
- `sample_duration_s`
- `silence_ratio`
- human-readable message

`process` then returns `waiting_for_review` instead of proceeding to TTS.

## Web UI Operator Flow

When `voice_review` is active, Web UI opens the `voice-library` tab and shows the pending speaker(s).

The operator currently has three choices:

1. choose an existing registered voice for that speaker
2. manually enter a `Voice ID` and bind it as the speaker default
3. cancel the waiting task

After every pending speaker resolves to a usable voice, the operator can click `Confirm and continue`.

## What "Confirm and continue" Does

The approve action:

- re-checks that each pending speaker now resolves to a usable voice
- writes `voice_review` to `approved` in `review_state.json`
- stores the resolved voice summary in the stage payload
- restarts the paused `process` run through the existing Web UI continuation path

This keeps the outer operator workflow stable:

`run -> wait_for_review -> fix voice binding -> continue`

## What "Cancel task" Does

The cancel action:

- marks the pending `voice_review` stage as `skipped`
- clears the current waiting review gate in Web UI
- changes the in-memory job status to `cancelled`

It does not silently resume the run.

## Current Scope

This flow currently supports:

- short-sample recovery for auto-clone
- existing voice selection from the local registry
- manual `Voice ID` entry from Web UI
- explicit continue / cancel handling

## Current Non-Goals

This flow does not yet provide:

- uploading a new replacement sample from Web UI
- retrying extraction with custom time ranges
- previewing the extracted sample in the review card
- a separate clone-specific wizard outside the existing `voice-library` tab

## Key Files

- `src/services/voice/sample_extractor.py`
- `src/pipeline/process.py`
- `src/services/review_state.py`
- `src/services/web_ui.py`
- `tests/test_sample_extractor.py`
- `tests/test_process_pipeline.py`
- `tests/test_web_ui.py`

## Why This Does Not Conflict With The Main Convergence Work

`voice_review` is not a new parallel architecture.

It extends the same review-gate pattern already used for:

- `speaker_review`
- `translation_review`

So this change is best understood as:

- a runtime robustness improvement
- a better operator recovery path
- a compatible addition to the existing `process -> review_state -> Web UI` loop

It does not change the current convergence priority of:

`process -> shared build / shared output / shared state semantics`

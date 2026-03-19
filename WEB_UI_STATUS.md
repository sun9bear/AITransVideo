# WEB_UI_STATUS

Last updated: 2026-03-19

## Current Position

The Web UI is no longer just a task launcher.

It has entered the "local review workbench" stage, but it is not finished.

Current practical status:

- The tab structure is in place.
- The main review flow for transcript/speaker and translation/rewrite is usable.
- Queue navigation and batch review actions exist.
- When a job enters a pending review stage, the Web UI now auto-opens the matching tab once and highlights the matching review block with a short pulse cue.
- Part of the review state is already persisted into project state.
- Audio alignment and richer rerun workflows are still incomplete.

## What Is Already Done

### 1. Base tab structure exists

The following tabs are implemented:

- `运行`
- `设置`
- `结果`
- `转录与发言人`
- `翻译与重写`
- `音色与语音库`
- `音频试听与对齐`

This means the Web UI has already moved beyond a single-page launcher.

### 2. Run and settings flow exists

Implemented:

- start / stop run
- current job status
- stage display
- runtime log display
- provider/model/settings editing

This is already enough for practical local runs and troubleshooting.

### 3. Results page is usable

Implemented:

- recent project detection
- project path / manifest display
- editor outputs display
- publish outputs display
- `needs_review` centralized list
- pagination / keyword / speaker filters
- direct jump to a segment

### 4. Transcript and speaker review page is usable

Implemented:

- speaker display-name editing
- per-segment speaker reassignment
- per-segment `speaker` confirmation
- per-segment transcript confirmation
- batch confirm speaker
- batch confirm transcript
- batch reset confirmation state
- previous / next / next pending / next needs-review navigation
- save-state feedback
- polling protection while editing

Current persistence status:

- segment confirmation state now syncs into `review_state.json`
- speaker-name text edits still use explicit save/approve flow

### 5. Translation and rewrite review page is usable

Implemented:

- edit `cn_text`
- edit `tts_cn_text`
- per-segment translation confirmation
- per-segment rewrite-needed mark
- batch confirm translation
- batch mark rewrite-needed
- batch reset state
- previous / next / next pending / next needs-review navigation
- save-state feedback
- gate-aware approve-and-continue flow

Current persistence status:

- translation confirmation and rewrite-needed state sync into `review_state.json`
- text edits still use explicit save/approve flow

### 6. Results-to-review queue navigation exists

Implemented:

- open a `needs_review` item from the results page
- jump directly into:
  - transcript review
  - translation review
  - audio alignment
- auto-position to the target segment

This closes the basic:

`结果 -> 待处理段落 -> 对应审校页 -> 继续下一条`

workflow.

### 7. Web UI snapshot now exposes more real review state

The snapshot already includes:

- transcript review counts
- translation review counts
- rewrite-request counts
- speaker display-name overrides
- review flow stage state

This makes refresh/restart behavior much more stable than before.

## What Is Only Partially Done

### 1. Voice library page

Current state:

- page exists
- speaker/voice browsing exists
- project default / speaker default binding exists

Still missing:

- richer voice management workflow
- clearer clone/listen/verify loop inside Web UI
- stronger review-to-voice assignment linkage

### 2. Audio alignment page

Current state:

- page exists
- list/filter/pagination exists
- segment targeting from results exists
- local listened-confirmation UI exists

Still missing:

- persistence into project review state
- stronger preview/listen workflow
- single-segment rerun entry
- clearer abnormal-alignment handling

## What Is Not Done Yet

These are still outside the current completed scope:

- single-segment rerun for `translation / rewrite / TTS / alignment`
- batch rerun
- version compare
- run history review
- full review-status persistence for audio alignment
- richer voice-library management inside Web UI
- more complete manifest audit view
- time-axis / waveform style advanced review UI

## Recommended Next Step

If Web UI work resumes later, the best next step is:

### Priority 1

Finish `音频试听与对齐` in the same style as the transcript/translation pages:

- persist review state into `review_state.json`
- make counts come from server snapshot
- stabilize listen/confirm/reset behavior

### Priority 2

Add partial rerun entrypoints:

- rerun translation
- rerun rewrite
- rerun TTS
- rerun alignment

### Priority 3

Then improve voice-library and history/version tooling.

## Recommendation For Now

Yes: it is reasonable to pause Web UI feature expansion here for a moment.

Current recommendation:

- keep this Web UI state as the recorded baseline
- first finish Phase 1 documentation and architecture alignment
- then come back to Web UI for audio-alignment persistence and partial rerun

That order is safer than continuing to expand the UI while the larger process/workflow boundary is still being clarified.

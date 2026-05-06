# Subtitle ↔ Audio Sync (P0 + P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make subtitle timing match the actual dubbed audio waveform (剪映 ASR-style accuracy), while preserving the invariant that subtitle text equals the text actually fed to TTS — including pre-TTS rewrites, post-TTS rewrites, and user edits.

**Architecture:** Two-phase landing.
- **P0** (Phase A + B): Add `tts_input_cn_text` snapshot field to `DubbingSegment` so we always know what text the current audio was synthesized from. Wire it through editor/segments.json, the publish-resume loader, the SemanticBlock builder, and the cue pipeline. Cue pipeline gains a per-block sync flag that gates fancier alignment downstream.
- **P1** (Phase C): Plug in local `faster-whisper` (small / INT8 / subprocess-isolated) on synced blocks only. Whisper transcribes each block's `aligned_audio_path`, returns word timestamps, then a DTW aligns those timestamps onto our `merged_cn_text` characters. Drift blocks fall through to the current proportional-distribution path.

**Tech Stack:** Python 3.12, faster-whisper (CTranslate2 backend, INT8 quantization), pydub for WAV duration, jieba/zhon for CJK char tokenization, existing pytest suite.

**Hard constraints (don't break):**
- Don't auto-call paid TTS / LLM APIs in any code path (CLAUDE.md root rule).
- `editor/segments.json` schema is append-only — never rename or repurpose existing fields.
- Whisper runs in a subprocess that exits after each cue regeneration so the 1.5GB model doesn't pin RAM in long-lived processes.
- Cap whisper to 2 CPU cores via `OMP_NUM_THREADS=2` so other pipelines don't starve on the 4-core box.
- Drift blocks (text changed without TTS re-gen) MUST fall back to the existing proportional path — never silently produce timestamps from mismatched audio.

---

## File Structure (what changes, what stays)

| File | Role | Change |
|---|---|---|
| `src/services/gemini/translator.py` | DubbingSegment dataclass | Add `tts_input_cn_text: str = ""` field |
| `src/services/alignment/aligner.py` | first-pass duration capture | Mirror existing `first_pass_cn_text` pattern: write `tts_input_cn_text` after every TTS pass |
| `src/pipeline/process.py` | Pipeline driver | Persist new field in segments-dict → editor/segments.json. Re-load on publish-resume. Pass through to SemanticBlock builder. |
| `src/services/jobs/editing_tts.py` | Per-segment regen-tts | Stamp `tts_input_cn_text = cn_text` on draft accept |
| `src/services/jobs/editing_segments.py` | Segment update + split | Existing `text_dirty` flow already covers drift detection — verify, don't change |
| `src/core/models.py` | SemanticBlock dataclass | Add `tts_input_cn_text: str = ""` field |
| `src/modules/subtitles/cue_pipeline.py` | Cue builder driver | Per-block sync check; expose `text_audio_drift` flag in BlockSpec / quality report |
| `src/modules/subtitles/cue_validator.py` | Validation report | Add new issue code `text_audio_drift` (severity: review) |
| `src/services/whisper_align/__init__.py` | NEW - whisper alignment service | Subprocess-isolated faster-whisper runner + DTW char alignment |
| `src/services/whisper_align/runner.py` | NEW - subprocess entry | Loads model, transcribes one WAV, prints JSON to stdout |
| `src/modules/subtitles/cue_pipeline.py` (P1) | Cue builder | When block is sync, call whisper_align to get word timestamps, override proportional layout |
| `tests/test_dubbing_segment_tts_input.py` | NEW | Field round-trip + write-site coverage |
| `tests/test_cue_pipeline_sync_flag.py` | NEW | Sync flag from drift detection |
| `tests/test_whisper_align_dtw.py` | NEW | DTW alignment correctness, char-edit tolerance |
| `tests/test_whisper_align_subprocess.py` | NEW | Subprocess isolation (mock whisper, verify env vars + cleanup) |
| `requirements.txt` | Dep manifest | Add `faster-whisper==1.0.3` |

---

## Phase A — P0a: `tts_input_cn_text` snapshot field

Goal: every successful TTS synthesis records the exact text that was sent to the TTS engine. The field travels with the segment all the way to `editor/segments.json` and back on publish-resume.

### Task A1: Add field to DubbingSegment dataclass

**Files:**
- Modify: `src/services/gemini/translator.py` (DubbingSegment, around line 284 next to `first_pass_cn_text`)
- Test: `tests/test_dubbing_segment_tts_input.py` (new)

- [ ] **A1.1: Write failing test for field default**

```python
# tests/test_dubbing_segment_tts_input.py
from services.gemini.translator import DubbingSegment

def test_dubbing_segment_has_tts_input_cn_text_default_empty():
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello", cn_text="你好",
    )
    assert seg.tts_input_cn_text == ""
```

- [ ] **A1.2: Run test → fails with AttributeError**

```bash
python -m pytest tests/test_dubbing_segment_tts_input.py::test_dubbing_segment_has_tts_input_cn_text_default_empty -v
```

Expected: `AttributeError: 'DubbingSegment' object has no attribute 'tts_input_cn_text'`

- [ ] **A1.3: Add field**

In `src/services/gemini/translator.py` after `first_pass_cn_text: str = ""` (line 284):

```python
    # 2026-05-04 P0a — exact text that was fed to TTS for the CURRENT
    # aligned_audio_path. Mutated on every successful TTS synthesis (initial
    # pipeline, pre-TTS rewrite, post-TTS rewrite, single-segment regen-tts,
    # batch regen-all-dirty). Never mutated by user text edits — that's how
    # we detect "text changed but audio still old" drift downstream.
    tts_input_cn_text: str = ""
```

- [ ] **A1.4: Re-run test → passes**

- [ ] **A1.5: Commit**

```bash
git add src/services/gemini/translator.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(segments): add tts_input_cn_text field to DubbingSegment"
```

### Task A2: Capture at first TTS in alignment

**Files:**
- Modify: `src/services/alignment/aligner.py` near line 244

- [ ] **A2.1: Write failing test**

```python
# tests/test_dubbing_segment_tts_input.py
def test_aligner_stamps_tts_input_cn_text_on_first_pass():
    """When alignment captures first_pass_cn_text, it MUST also capture
    tts_input_cn_text — they're snapshotted from the same value."""
    from services.gemini.translator import DubbingSegment
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello",
        cn_text="你好世界", actual_duration_ms=1000,
    )
    # Simulate the snapshot path used by aligner._snapshot_first_pass
    from services.alignment.aligner import _snapshot_first_pass_text  # may need to extract
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "你好世界"
    assert seg.tts_input_cn_text == "你好世界"
```

- [ ] **A2.2: Run test → fails (function doesn't exist or doesn't set new field)**

- [ ] **A2.3: Extract snapshot logic + capture both fields**

In `src/services/alignment/aligner.py`, replace the inline block at lines 243-245 with a helper:

```python
def _snapshot_first_pass_text(segment: "DubbingSegment") -> None:
    """Snapshot the text used for the FIRST TTS pass (before any rewrite/DSP).

    Two fields, snapshotted at the same time:
    - first_pass_cn_text: only set the first time; preserved across post-TTS
      rewrites for downstream voice-speed-profile guardrails.
    - tts_input_cn_text: ALWAYS overwritten with current cn_text. This tracks
      "the text that produced the audio currently on disk" — post-TTS rewrites
      will re-stamp it; user text-edit-without-regen-TTS will NOT (because
      this helper only runs at TTS/alignment time, not at edit time).
    """
    current = segment.cn_text.strip()
    if not getattr(segment, "first_pass_cn_text", ""):
        segment.first_pass_cn_text = current
    segment.tts_input_cn_text = current
```

Then call it at the existing site:

```python
# Replace lines 243-245:
_snapshot_first_pass_text(segment)
```

- [ ] **A2.4: Run test → passes**

- [ ] **A2.5: Commit**

```bash
git add src/services/alignment/aligner.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(alignment): stamp tts_input_cn_text alongside first_pass_cn_text"
```

### Task A3: Capture after post-TTS rewrite

When `_apply_post_tts_rewrite` updates `segment.cn_text` and re-synthesizes, the new audio reflects the new text. We need to re-stamp.

**Files:**
- Modify: `src/services/alignment/aligner.py` — find every site that writes a new TTS audio (post-rewrite path)

- [ ] **A3.1: Locate the post-rewrite TTS write site**

```bash
grep -n "tts_audio_path\s*=" src/services/alignment/aligner.py
```

- [ ] **A3.2: Write failing test for re-stamp on rewrite**

```python
def test_post_tts_rewrite_restamps_tts_input_cn_text():
    """When a segment is rewritten and re-synthesized post-TTS, both
    cn_text AND tts_input_cn_text must reflect the new text. (first_pass_cn_text
    stays as the original first attempt — that's its contract.)"""
    from services.gemini.translator import DubbingSegment
    from services.alignment.aligner import _snapshot_first_pass_text
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello",
        cn_text="原版", actual_duration_ms=1000,
    )
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "原版"
    assert seg.tts_input_cn_text == "原版"

    # Simulate post-TTS rewrite path
    seg.cn_text = "重写版"
    seg.rewrite_count = 1
    _snapshot_first_pass_text(seg)  # called again after re-synthesis
    assert seg.first_pass_cn_text == "原版"        # unchanged
    assert seg.tts_input_cn_text == "重写版"      # restamped
```

- [ ] **A3.3: Run test → should already pass given A2 logic, otherwise fix the helper**

- [ ] **A3.4: Verify the post-TTS rewrite path actually calls `_snapshot_first_pass_text`**

Read aligner.py around any `apply_rewrite` / `_resynthesize` site. If the helper isn't called, add the call. If the path doesn't go through aligner at all (rewrite happens in pipeline.process), find that site and add the call there too.

- [ ] **A3.5: Commit**

```bash
git add src/services/alignment/aligner.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(alignment): re-stamp tts_input_cn_text on post-TTS rewrite"
```

### Task A4: Persist field in editor/segments.json

**Files:**
- Modify: `src/pipeline/process.py` near line 7106 (segments → JSON dict)
- Modify: `src/pipeline/process.py` near line 7931 (JSON dict → segments)
- Modify: `src/pipeline/process.py` near line 4689 (default backfill on load)

- [ ] **A4.1: Write failing test for JSON round-trip**

```python
# tests/test_dubbing_segment_tts_input.py
def test_editor_segments_json_round_trip_preserves_field(tmp_path):
    from pipeline.process import _serialize_segment, _deserialize_segment  # may need extraction
    from services.gemini.translator import DubbingSegment
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello",
        cn_text="新文本", tts_input_cn_text="原合成文本",
    )
    payload = _serialize_segment(seg)
    assert payload["tts_input_cn_text"] == "原合成文本"
    seg2 = _deserialize_segment(payload)
    assert seg2.tts_input_cn_text == "原合成文本"
```

- [ ] **A4.2: Run test → fails (helpers don't exist OR don't include the field)**

- [ ] **A4.3: Add field to serialization at line 7106 area**

Search for `"first_pass_cn_text": segment.first_pass_cn_text,` and add right after:

```python
"tts_input_cn_text": segment.tts_input_cn_text,
```

- [ ] **A4.4: Add field to deserialization at line 7931 area**

Search for `cn_text=s.get("cn_text", "")` and add `tts_input_cn_text` to the `DubbingSegment(...)` kwargs.

- [ ] **A4.5: Backfill default for legacy jobs**

In `_load_segments_for_publish_resume` (line 3047) or wherever segments are loaded from old JSON, ensure `tts_input_cn_text` defaults to `cn_text` (assume in-sync) if the field is missing in the JSON. Document this in a comment as a one-time legacy-job migration.

```python
# Legacy editor/segments.json files written before 2026-05-04 don't have
# tts_input_cn_text. Conservative default: assume the audio matches the
# current cn_text (i.e. user hasn't done text-edit-without-regen-tts on
# this old job). If they had, the segment would already be marked text_dirty
# in segment_status.json and the dirty path takes over downstream.
if not getattr(segment, "tts_input_cn_text", ""):
    segment.tts_input_cn_text = segment.cn_text
```

- [ ] **A4.6: Run test → passes**

- [ ] **A4.7: Commit**

```bash
git add src/pipeline/process.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(segments): persist tts_input_cn_text in editor/segments.json"
```

### Task A5: Stamp on draft accept (single regen-tts)

**Files:**
- Modify: `src/services/jobs/editing_tts.py` — find the accept-draft logic that promotes a draft TTS to baseline

- [ ] **A5.1: Locate accept-draft path**

```bash
grep -n "accept_draft\|tts_segments_draft\|promote.*draft" src/services/jobs/editing_tts.py
```

- [ ] **A5.2: Write failing test**

```python
# tests/test_dubbing_segment_tts_input.py
def test_accept_draft_stamps_tts_input_cn_text(tmp_path):
    """When user clicks 'accept draft TTS' for a single segment, the audio
    on disk now reflects the segment's CURRENT cn_text — so tts_input_cn_text
    must be re-stamped to match."""
    # ... build minimal project_dir with editor/segments.json (cn_text='X',
    # tts_input_cn_text='OLD'), editor/editing/tts_segments_draft/seg_1.wav
    # call services.jobs.editing_tts.accept_draft_tts(project_dir, '1')
    # assert editor/segments.json now has tts_input_cn_text='X'
```

- [ ] **A5.3: Run → fails**

- [ ] **A5.4: Implement re-stamp at accept site**

Inside `accept_draft_tts()` (or wherever the draft → baseline promotion happens), after the audio file is moved/copied:

```python
segment["tts_input_cn_text"] = segment["cn_text"]
```

- [ ] **A5.5: Run test → passes**

- [ ] **A5.6: Commit**

```bash
git add src/services/jobs/editing_tts.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(editing): stamp tts_input_cn_text on draft TTS accept"
```

### Task A6: Stamp on batch regen-all-dirty

**Files:**
- Modify: `src/services/jobs/editing_batch.py` (or wherever batch re-TTS finishes)

- [ ] **A6.1: Locate batch finalize site**

```bash
grep -n "regenerate_all_dirty\|batch.*tts" src/services/jobs/editing_batch.py
```

- [ ] **A6.2: Write failing test**

```python
def test_batch_regen_stamps_tts_input_for_all_resynthesized_segments(tmp_path):
    """After batch regen-all-dirty completes, every segment that had its
    audio re-synthesized has tts_input_cn_text == cn_text."""
    # ... seed three segments: seg_1 text_dirty, seg_2 voice_dirty, seg_3 accepted
    # ... seg_1.cn_text='A_NEW', seg_1.tts_input_cn_text='A_OLD'
    # ... seg_2.cn_text='B', seg_2.tts_input_cn_text='B'
    # ... seg_3 is already in sync; should not be touched
    # call regenerate_all_dirty_segments(...)
    # after: seg_1.tts_input_cn_text=='A_NEW', seg_2.tts_input_cn_text=='B',
    #        seg_3 unchanged
```

- [ ] **A6.3: Run → fails**

- [ ] **A6.4: Implement at the per-segment finalize step inside the batch loop**

Same pattern as A5.4 — after each segment's audio is finalized, stamp `tts_input_cn_text = cn_text`.

- [ ] **A6.5: Run → passes**

- [ ] **A6.6: Commit**

```bash
git add src/services/jobs/editing_batch.py tests/test_dubbing_segment_tts_input.py
git commit -m "feat(editing): stamp tts_input_cn_text on batch regen-all-dirty"
```

---

## Phase B — P0b: Cue pipeline sync check

Goal: cue pipeline knows per-block whether `merged_cn_text` matches what produced the audio. Drift blocks are flagged in the validation report so downstream consumers (P1 whisper alignment, future UI badges) can react.

### Task B1: Add field to SemanticBlock

**Files:**
- Modify: `src/core/models.py` (SemanticBlock dataclass)

- [ ] **B1.1: Write failing test**

```python
# tests/test_cue_pipeline_sync_flag.py
def test_semantic_block_has_tts_input_cn_text_default_empty():
    from core.models import SemanticBlock
    b = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=1000,
        target_duration_ms=1000, merged_cn_text="hi",
    )
    assert b.tts_input_cn_text == ""
```

- [ ] **B1.2: Run → fails**

- [ ] **B1.3: Add field**

In `src/core/models.py` SemanticBlock dataclass:

```python
    tts_input_cn_text: str = ""  # 2026-05-04: text used for TTS that produced
                                  # the current aligned_audio_path. Joined from
                                  # merged segments' tts_input_cn_text fields
                                  # at block-build time.
```

- [ ] **B1.4: Run → passes**

- [ ] **B1.5: Commit**

```bash
git add src/core/models.py tests/test_cue_pipeline_sync_flag.py
git commit -m "feat(models): add tts_input_cn_text to SemanticBlock"
```

### Task B2: Pipeline _build_blocks populates the field

**Files:**
- Modify: `src/pipeline/process.py` near line 6724 (`SemanticBlock(...)` constructor)
- Modify: `src/pipeline/process.py` `_join_short_merge_texts` analog for tts_input

- [ ] **B2.1: Write failing test for single-segment block**

```python
def test_block_inherits_tts_input_cn_text_from_segment(tmp_path):
    """A 1-segment block's tts_input_cn_text equals the segment's."""
    from pipeline.process import ProcessPipeline
    from services.gemini.translator import DubbingSegment
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello",
        cn_text="你好", tts_input_cn_text="你好",
    )
    blocks = ProcessPipeline._build_blocks_from_segments([seg])
    assert blocks[0].tts_input_cn_text == "你好"
```

- [ ] **B2.2: Run → fails (field defaults to "")**

- [ ] **B2.3: Add to constructor at line 6724**

```python
SemanticBlock(
    block_id=...,
    ...,
    merged_cn_text=segment.cn_text,
    tts_input_cn_text=segment.tts_input_cn_text or segment.cn_text,
    # ... other fields
)
```

(`or segment.cn_text` is the legacy-backfill safety net — same rationale as A4.5.)

- [ ] **B2.4: Add merged-block test for short_merge case**

```python
def test_block_merges_tts_input_for_short_merge_chain(tmp_path):
    """When short_merge merges seg_2 + seg_3 into seg_1's block, the merged
    tts_input_cn_text is joined the same way as merged_cn_text."""
    # build 3 segments: seg_1 (target), seg_2 + seg_3 short-merged into seg_1
    # seg_1.cn_text='A', seg_1.tts_input_cn_text='A'
    # seg_2.cn_text='B', seg_2.tts_input_cn_text='B'
    # seg_3.cn_text='C', seg_3.tts_input_cn_text='C'
    # ... apply short_merge
    # blocks = ProcessPipeline._build_blocks_from_segments([merged_seg_1])
    # assert blocks[0].merged_cn_text == 'A B C' (or whatever join semantics)
    # assert blocks[0].tts_input_cn_text matches the same join shape
```

- [ ] **B2.5: Run → fails (joining not done for tts_input)**

- [ ] **B2.6: Implement matching join logic**

Find `_join_short_merge_texts` (line 6410) and either generalize to take the field name, or add a parallel call for `tts_input_cn_text`. Apply at block-build time.

- [ ] **B2.7: Run → passes**

- [ ] **B2.8: Commit**

```bash
git add src/pipeline/process.py tests/test_cue_pipeline_sync_flag.py
git commit -m "feat(blocks): populate SemanticBlock.tts_input_cn_text from segments"
```

### Task B3: Sync flag in cue pipeline + validation report

**Files:**
- Modify: `src/modules/subtitles/cue_validator.py` — add issue code
- Modify: `src/modules/subtitles/cue_pipeline.py` — emit sync flag

- [ ] **B3.1: Write failing test for drift detection**

```python
def test_cue_pipeline_flags_drift_block_in_quality_report():
    """A block whose merged_cn_text != tts_input_cn_text emits a
    text_audio_drift issue in the validation report."""
    from core.models import SemanticBlock, SubtitleLine
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    drift_block = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=2000,
        target_duration_ms=2000,
        merged_cn_text="新版文字",        # user edited, no regen-tts
        tts_input_cn_text="原版文字",     # what audio was synthesized from
    )
    sync_block = SemanticBlock(
        block_id="b2", speaker_id="A", speaker_name="A",
        original_srt_indices=[2], first_start_ms=2000, last_end_ms=4000,
        target_duration_ms=2000,
        merged_cn_text="同步的文字",
        tts_input_cn_text="同步的文字",
    )
    lines = [
        SubtitleLine(index=1, start_ms=0, end_ms=2000, speaker_id="A",
                     speaker_name="A", en_text="x", cn_text="新版文字"),
        SubtitleLine(index=2, start_ms=2000, end_ms=4000, speaker_id="A",
                     speaker_name="A", en_text="y", cn_text="同步的文字"),
    ]
    result = build_subtitle_cues_for_blocks([drift_block, sync_block], lines)

    drift_issues = [i for i in result.report.issues if i.code == "text_audio_drift"]
    assert len(drift_issues) == 1
    assert drift_issues[0].block_id == "b1"
    # And the sync block has no drift issue
    sync_issues = [i for i in result.report.issues
                   if i.code == "text_audio_drift" and i.block_id == "b2"]
    assert sync_issues == []
```

- [ ] **B3.2: Run → fails (no such issue code)**

- [ ] **B3.3: Add issue code to validator**

In `src/modules/subtitles/cue_validator.py`, add to the issue-code constants and the severity map. Severity = `"review"` (informational, doesn't block validation status).

- [ ] **B3.4: Emit issue in cue_pipeline**

In `cue_pipeline.build_subtitle_cues_for_blocks` per-block loop, after building cues for a block:

```python
# 2026-05-04: detect text↔audio drift for downstream consumers (P1 whisper
# alignment will skip drift blocks; UI may surface a "audio out of date" badge).
if (block.tts_input_cn_text
    and block.tts_input_cn_text.strip() != block.merged_cn_text.strip()):
    block_specs[-1].text_audio_drift = True   # add field to BlockSpec
```

- [ ] **B3.5: Add `text_audio_drift: bool = False` to `BlockSpec` and the validator emits a `text_audio_drift` issue when True**

- [ ] **B3.6: Run → passes**

- [ ] **B3.7: Add a sync-block negative test (already in B3.1's test); confirm pass**

- [ ] **B3.8: Commit**

```bash
git add src/modules/subtitles/cue_pipeline.py src/modules/subtitles/cue_validator.py tests/test_cue_pipeline_sync_flag.py
git commit -m "feat(cue): detect text↔audio drift per block, emit validation issue"
```

### Task B4: Wire sync flag into subtitle_quality_report.json

**Files:**
- Modify: `src/modules/output/output_dispatcher.py` — quality report serializer

- [ ] **B4.1: Write failing test that the report JSON exposes drift count**

- [ ] **B4.2: Add `text_audio_drift_count` to BlockSummary**

- [ ] **B4.3: Run → passes**

- [ ] **B4.4: Commit**

```bash
git add src/modules/output/output_dispatcher.py src/modules/subtitles/cue_validator.py tests/test_cue_pipeline_sync_flag.py
git commit -m "feat(quality): expose text_audio_drift count per block in quality report"
```

### Task B5: Deploy P0 to US server, smoke test

- [ ] **B5.1: Deploy modified files to container**

```bash
# upload the four modified src files via SCP-US-Via-154.cmd
# docker cp into aivideotrans-app:/opt/aivideotrans/app/src/...
# docker restart aivideotrans-app
```

- [ ] **B5.2: Verify import works**

```bash
docker exec aivideotrans-app python -c "
from services.gemini.translator import DubbingSegment
from core.models import SemanticBlock
seg = DubbingSegment(segment_id=1, speaker_id='A', display_name='A',
                     voice_id='v', start_ms=0, end_ms=1, target_duration_ms=1,
                     source_text='', cn_text='', tts_input_cn_text='X')
print('seg.tts_input_cn_text =', repr(seg.tts_input_cn_text))
"
```

- [ ] **B5.3: Pick a recent succeeded job, regenerate cues with the new code**

(Reuse the regen script from earlier — but now blocks should populate `tts_input_cn_text` from segments' fields. Backfill rule means existing jobs default to "in sync".)

- [ ] **B5.4: Smoke test — open the regen'd subtitle_quality_report.json**

Confirm it now has `text_audio_drift_count` per block, all 0 for an unedited job.

- [ ] **B5.5: Commit deployment marker (just a comment, optional)**

---

## Phase C — P1: faster-whisper alignment for sync blocks

Goal: per-block, when `text_audio_drift == False`, run faster-whisper on the block's audio range to get word-level timestamps, then DTW-align our `merged_cn_text` characters onto whisper's character positions and use those for cue boundaries.

### Task C1: Add faster-whisper dependency + smoke test

**Files:**
- Modify: `requirements.txt`
- Test: `tests/test_whisper_align_subprocess.py` (new)

- [ ] **C1.1: Add to requirements.txt**

```
faster-whisper==1.0.3
```

- [ ] **C1.2: Smoke test — model loads, transcribes a tiny WAV**

```python
# tests/test_whisper_align_subprocess.py
import pytest
faster_whisper = pytest.importorskip("faster_whisper")

def test_faster_whisper_loads_small_int8_model_and_transcribes(tmp_path):
    """Sanity: faster-whisper imports, small/int8 model loads,
    transcribes a synthetic CJK WAV."""
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    # Generate a 1-second test WAV — silence is fine for the test, we
    # just want to verify the inference path runs end-to-end.
    import wave, struct
    p = tmp_path / "silence.wav"
    with wave.open(str(p), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 16000)
    segments, info = model.transcribe(str(p), language="zh")
    list(segments)  # consume
    assert info.duration > 0.5
```

- [ ] **C1.3: Run → installs faster-whisper, downloads small model (one-time ~466MB), passes**

- [ ] **C1.4: Commit**

```bash
git add requirements.txt tests/test_whisper_align_subprocess.py
git commit -m "feat(deps): add faster-whisper for subtitle audio alignment"
```

### Task C2: Subprocess-isolated whisper runner

**Files:**
- Create: `src/services/whisper_align/__init__.py`
- Create: `src/services/whisper_align/runner.py`

- [ ] **C2.1: Write failing test**

```python
def test_run_whisper_subprocess_returns_word_timestamps(tmp_path):
    """run_whisper(wav_path) returns a list of (start_ms, end_ms, text)
    tuples with word-level granularity. Subprocess must exit cleanly so
    no zombie processes pile up."""
    from services.whisper_align import run_whisper_subprocess
    # Use a real test WAV with known CJK speech ('你好世界' 1.5s)
    test_wav = "tests/fixtures/whisper/ni_hao_shi_jie.wav"
    words = run_whisper_subprocess(test_wav, language="zh")
    assert len(words) > 0
    assert all(isinstance(w["start_ms"], int) for w in words)
    assert all(isinstance(w["end_ms"], int) for w in words)
    assert all(isinstance(w["text"], str) for w in words)
    # Roughly correct timing
    assert words[0]["start_ms"] < 500   # first word starts in first half-sec
    assert words[-1]["end_ms"] < 2000   # all done within 2s
```

- [ ] **C2.2: Run → fails (module doesn't exist)**

- [ ] **C2.3: Implement subprocess runner**

`src/services/whisper_align/runner.py` — entry that the subprocess actually executes:

```python
"""Subprocess entry: load faster-whisper, transcribe one WAV, dump JSON.

Invoked by run_whisper_subprocess() via:
    python -m services.whisper_align.runner --wav <path> --language zh

Stdout: a single JSON line with { "words": [{start_ms, end_ms, text}, ...] }.
Stderr: any progress/log lines (caller may swallow).

Lives in its own subprocess so the 1.5GB model footprint exits with this
process — never pinning the long-lived Job-API or runner process RAM.
"""
import argparse
import json
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--language", default="zh")
    ap.add_argument("--model", default="small")
    args = ap.parse_args()

    # Cap CPU usage so we don't starve other pipelines on the 4-core host.
    os.environ.setdefault("OMP_NUM_THREADS", "2")

    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        args.wav,
        language=args.language,
        word_timestamps=True,
        # Ensure deterministic enough for tests
        beam_size=1,
        vad_filter=False,
    )

    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({
                "start_ms": int(w.start * 1000),
                "end_ms": int(w.end * 1000),
                "text": w.word,
            })

    json.dump({"words": words, "duration_ms": int(info.duration * 1000)},
              sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
```

`src/services/whisper_align/__init__.py`:

```python
"""Subprocess wrapper for faster-whisper word-timestamp transcription."""
import json
import subprocess
import sys
from pathlib import Path


def run_whisper_subprocess(
    wav_path: str,
    *,
    language: str = "zh",
    model: str = "small",
    timeout_sec: int = 600,
) -> list[dict]:
    """Run faster-whisper in a fresh subprocess; return list of word dicts.

    Subprocess isolation: the model (~1.5GB RAM peak) is loaded and unloaded
    within the child process, so the parent (Job-API / runner) never accumulates
    the footprint. CPU is capped to 2 threads via OMP_NUM_THREADS in runner.py.
    """
    cmd = [
        sys.executable, "-m", "services.whisper_align.runner",
        "--wav", str(wav_path),
        "--language", language,
        "--model", model,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_sec,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"whisper subprocess failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )
    payload = json.loads(proc.stdout)
    return payload["words"]
```

- [ ] **C2.4: Generate the test fixture WAV (synthesize a known utterance ahead of time, commit it)**

Use any local TTS engine the dev has, or a public CC0 sample. Cap at < 50KB so it's checked in safely.

- [ ] **C2.5: Run → passes**

- [ ] **C2.6: Commit**

```bash
git add src/services/whisper_align/ tests/test_whisper_align_subprocess.py tests/fixtures/whisper/
git commit -m "feat(whisper): subprocess-isolated faster-whisper runner"
```

### Task C3: DTW char alignment

**Files:**
- Create: `src/services/whisper_align/dtw.py`
- Test: `tests/test_whisper_align_dtw.py`

- [ ] **C3.1: Write failing tests for DTW happy path + edit tolerance**

```python
def test_dtw_aligns_identical_text():
    """When whisper transcript == cn_text exactly, every char gets the
    whisper word's proportional time slice."""
    from services.whisper_align.dtw import align_chars_to_words

    cn_text = "你好世界"
    words = [
        {"start_ms": 100, "end_ms": 500, "text": "你好"},
        {"start_ms": 500, "end_ms": 900, "text": "世界"},
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert len(char_times) == 4
    assert char_times[0]["start_ms"] == 100  # 你
    assert char_times[1]["end_ms"] == 500     # 好
    assert char_times[2]["start_ms"] == 500   # 世
    assert char_times[3]["end_ms"] == 900     # 界


def test_dtw_tolerates_number_normalization():
    """ASR normalizes '二十多岁' → '20多岁'. Our cn_text is '二十多岁'.
    DTW should still align — char count differs but the alignment finds
    correspondence by content."""
    from services.whisper_align.dtw import align_chars_to_words
    cn_text = "二十多岁"  # 4 CJK chars
    words = [
        {"start_ms": 0, "end_ms": 800, "text": "20多岁"},  # 4 chars (2 ascii + 2 cjk)
    ]
    char_times = align_chars_to_words(cn_text, words)
    assert len(char_times) == 4
    # All 4 of our chars get a time within [0, 800]
    for ct in char_times:
        assert 0 <= ct["start_ms"] < ct["end_ms"] <= 800


def test_dtw_returns_empty_on_disjoint_text():
    """If whisper transcript and cn_text share no characters (>80% diff),
    DTW returns empty so caller can fall back to proportional layout."""
    from services.whisper_align.dtw import align_chars_to_words
    cn_text = "完全不同的内容"
    words = [{"start_ms": 0, "end_ms": 1000, "text": "totally different"}]
    char_times = align_chars_to_words(cn_text, words)
    assert char_times == []
```

- [ ] **C3.2: Run → fails (module doesn't exist)**

- [ ] **C3.3: Implement DTW**

Use `python-Levenshtein` editops or write a small DP. Match whisper's transcript chars (with ASCII/CJK normalization: convert digits to Chinese and vice versa for comparison) to cn_text chars. Compute alignment, then map cn_text char index → whisper char index → whisper word index → time slice.

Simple algorithm:
1. Concatenate all whisper words into one string with a per-char timestamp array (each char's time = linear interp inside its word).
2. Run Levenshtein editops between cn_text (after normalization) and whisper string.
3. For each cn_text char, find the corresponding whisper char position from editops; if matched/replaced, take the whisper char time; if inserted, interpolate from neighbors.

- [ ] **C3.4: Run all 3 tests → all pass**

- [ ] **C3.5: Commit**

```bash
git add src/services/whisper_align/dtw.py tests/test_whisper_align_dtw.py
git commit -m "feat(whisper): DTW char alignment between cn_text and whisper transcript"
```

### Task C4: Integrate into cue_pipeline

**Files:**
- Modify: `src/modules/subtitles/cue_pipeline.py`
- Modify: `src/modules/subtitles/cue_builder.py` (or add an alt path)

- [ ] **C4.1: Write integration test**

```python
def test_sync_block_uses_whisper_aligned_cue_boundaries(tmp_path, monkeypatch):
    """When a block is sync (no drift), cue_pipeline calls whisper_align
    and uses the returned char timestamps for cue start/end."""
    fake_words = [
        {"start_ms": 300, "end_ms": 1400, "text": "在某个阶段"},
        {"start_ms": 1400, "end_ms": 3600, "text": "通常是在你20多岁时"},
    ]
    def fake_run_whisper(*a, **kw): return fake_words
    monkeypatch.setattr(
        "services.whisper_align.run_whisper_subprocess", fake_run_whisper
    )
    # build a sync block whose merged_cn_text is the joined transcript
    # call build_subtitle_cues_for_blocks
    # assert cue 1 starts at 300 (not 192 from the SRT window),
    # cue 2 starts at 1400, etc.
```

- [ ] **C4.2: Run → fails**

- [ ] **C4.3: Implement integration**

In `cue_pipeline.build_subtitle_cues_for_blocks`, per-block:

```python
if (block.tts_input_cn_text
    and block.tts_input_cn_text.strip() == block.merged_cn_text.strip()
    and block.aligned_audio_path
    and Path(block.aligned_audio_path).is_file()
    and _whisper_align_enabled()):
    try:
        words = run_whisper_subprocess(
            block.aligned_audio_path, language="zh", model="small",
        )
        char_times = align_chars_to_words(block.merged_cn_text, words)
        if char_times:
            cues = build_cues_with_char_times(block, char_times, ...)
        else:
            cues = build_cues_for_block(...)  # fallback
    except Exception as exc:
        logger.warning("whisper align failed for %s: %s; falling back",
                       block.block_id, exc)
        cues = build_cues_for_block(...)
else:
    cues = build_cues_for_block(...)  # legacy path for drift / disabled / no audio
```

- [ ] **C4.4: Add feature flag `AVT_WHISPER_ALIGN_ENABLED=1` (default off for safe rollout)**

Reading via `os.environ.get("AVT_WHISPER_ALIGN_ENABLED") == "1"`.

- [ ] **C4.5: Run → passes**

- [ ] **C4.6: Commit**

```bash
git add src/modules/subtitles/cue_pipeline.py tests/test_cue_pipeline_sync_flag.py
git commit -m "feat(cue): integrate whisper alignment for sync blocks (flag-gated)"
```

### Task C5: Resource limits + per-block caching

**Files:**
- Modify: `src/services/whisper_align/__init__.py`

- [ ] **C5.1: Write test for content-hash caching**

A repeat call with the same WAV + same model returns cached result without spawning a subprocess.

- [ ] **C5.2: Implement cache keyed on (wav_sha256, model)**

Cache file: `{project_dir}/.cache/whisper_align/{sha256}.json`. Cleanup not in-scope; OS-level disk pressure handles it.

- [ ] **C5.3: Test passes**

- [ ] **C5.4: Commit**

```bash
git add src/services/whisper_align/ tests/test_whisper_align_subprocess.py
git commit -m "feat(whisper): per-block transcript cache keyed on content hash"
```

### Task C6: Deploy P1 to US server, end-to-end smoke test

- [ ] **C6.1: Install faster-whisper in container**

```bash
docker exec aivideotrans-app pip install 'faster-whisper==1.0.3'
```

- [ ] **C6.2: Pre-download small model into container's model cache**

```bash
docker exec aivideotrans-app python -c "
from faster_whisper import WhisperModel
m = WhisperModel('small', device='cpu', compute_type='int8')
print('cached at', m.model_path if hasattr(m, 'model_path') else 'OK')
"
```

- [ ] **C6.3: Deploy modified src files**

- [ ] **C6.4: Restart container**

- [ ] **C6.5: Set feature flag in docker-compose env, recreate**

```yaml
environment:
  AVT_WHISPER_ALIGN_ENABLED: "1"
```

- [ ] **C6.6: Re-trigger cue regeneration on the reshape job (or any sync job)**

Use the same `regen_cues_reshape.py` style script, but now whisper alignment kicks in.

- [ ] **C6.7: Compare new server SRT vs Jianying's SRT**

```bash
# pull subtitles_zh.srt from container, run diff_srts.py
# expect: server avg cue duration drops to ~2000ms (from 2367), boundary
# locations shift toward speech pauses, no overlap pairs.
```

- [ ] **C6.8: User regenerates Jianying draft, compares subtitle vs audio sync subjectively**

- [ ] **C6.9: If acceptable, commit a deployment marker / update CLAUDE.md memory file with rollout notes**

---

## Risks & rollback

- **Whisper subprocess hangs / OOMs.** Mitigated by `timeout_sec=600`, `OMP_NUM_THREADS=2`, model="small" (1.5GB RAM peak vs 5.8GB available). On failure, per-block try/except falls back to proportional layout — user gets the same SRT they have today, no worse.
- **DTW edge cases (very long blocks, noisy whisper transcript).** Drift detection covers most; even on sync blocks, DTW has the disjoint-text bailout.
- **Disk pressure from cached transcripts.** Each cache file is ~5KB JSON; for a typical 85-segment job that's ~425KB. Negligible compared to existing project disk usage.
- **Rollback** at any phase: revert the feature flag (P1) or revert the commits (P0 phases). Field is additive, never removed.

## Out of scope (future)

- Commit-time dirty warning UI (P0c) — depends on this plan but doesn't block subtitle accuracy.
- TTS provider word-timestamp capture (preferred long-term replacement for whisper) — separate plan when V3 TTS framework lands.
- Whisper model size auto-tuning per audio duration / content type.

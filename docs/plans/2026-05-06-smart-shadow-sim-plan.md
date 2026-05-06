# Smart Shadow Simulator Implementation Plan (P1)

> **For agentic workers:** Use TDD per task with small commits. Steps use checkbox (`- [ ]`) syntax. **3 explicit pause gates between phases for human review** — do not auto-continue past gate.

**Goal:** 实现 P1 离线 shadow simulator + aggregator，对历史 Studio 任务模拟"如果是智能版会怎么决策"，对比 user_edit_events / metering / subtitle drift，生成 per-job sidecars + 跨 job aggregate report。**绝不**挂生产 lifecycle hook、不动用户交付、不调付费 API。

**Architecture:** 双 stdlib-only Python 脚本，沿用 P0 evaluator 模式（fact-sheet driven + AST guard + PII guard + fixture-driven tests）。

**Spec doc:** [`2026-05-06-smart-shadow-sim-design.md`](2026-05-06-smart-shadow-sim-design.md)

**Output dir 硬约束:** `D:\Claude\temp\smart_shadow_sim\<run_id>\` (本地) 或 `/tmp/smart_shadow_sim/<run_id>/` (远端)。**绝不**写 production `{project_dir}/audit/`。

---

## File Structure

```
scripts/
├── smart_shadow_sim_simulator.py         # NEW — per-job decisions + report
└── smart_shadow_sim_aggregator.py        # NEW — cross-job aggregate

tests/
├── test_smart_shadow_sim_simulator.py
├── test_smart_shadow_sim_simulator_imports.py    # AST guard
├── test_smart_shadow_sim_simulator_pii_guard.py  # PII guard, reuses P0 PII_LITERALS
├── test_smart_shadow_sim_aggregator.py
└── fixtures/smart_shadow_sim/
    ├── full_post_phase/                  # post-Phase-D, 0 user edits
    │   ├── facts.jsonl
    │   └── projects/<pid>/<jid>/
    │       ├── audit/user_edit_events.jsonl
    │       ├── editor/segments.json
    │       ├── metering/usage_events.jsonl
    │       └── output/{subtitle_quality_report,subtitle_cues}.json
    ├── full_with_user_edits/             # post-Phase-D + speaker correction
    ├── pre_phase/                         # missing metering/whisper
    └── corrupted/                         # missing key facts fields
```

不修改 `src/` / `gateway/` / `services/`。

---

## Phase A: Skeleton + Schemas + Hardening Guards

目标：simulator + aggregator 能跑（empty input），三套 guard 全绿。

### Task A1: simulator skeleton (--help works)
- [ ] **A1.1** test_simulator_help_works (subprocess --help, asserts --facts in stdout)
- [ ] **A1.2** Run → fail
- [ ] **A1.3** Create `scripts/smart_shadow_sim_simulator.py`:
  - `from __future__ import annotations` + stdlib imports only
  - `SCHEMA_VERSION = 1`
  - `build_arg_parser()` with all §6.1 args
  - `main()` returns 0
- [ ] **A1.4** Run → pass
- [ ] **A1.5** Commit: `feat: add smart_shadow_sim_simulator skeleton with arg parsing`

### Task A2: aggregator skeleton (--help works)
- [ ] **A2.1-A2.5** Same pattern, file = `smart_shadow_sim_aggregator.py`, args per §6.2
- [ ] Commit: `feat: add smart_shadow_sim_aggregator skeleton with arg parsing`

### Task A3: simulator empty run + sidecar schema scaffold
- [ ] **A3.1** test: empty facts.jsonl → simulator exit 1 (no jobs simulated, per §8) + writes `summary.json` with `is_complete_run=true`
- [ ] **A3.2** test: 1-fact run produces `<out_dir>/<job_id>/smart_shadow_decisions.jsonl` (empty placeholder ok at A3) + `smart_shadow_report.json` with required v1 fields
- [ ] **A3.3** Implement: facts loader, run_id generator (reuse P0 pattern), per-job out dir creation, empty decisions/report writers
- [ ] **A3.4** Run → 2 tests pass
- [ ] Commit: `feat: simulator facts loader + per-job sidecar scaffolding`

### Task A4: aggregator empty run
- [ ] **A4.1** test: empty simulator-out-dir → aggregator writes empty aggregate_report.json with `jobs_simulated=0` + warnings
- [ ] **A4.2** Implement: simulator-out-dir scanner (look for `<job_id>/smart_shadow_report.json` files), aggregate writer
- [ ] **A4.3** Run → pass
- [ ] Commit: `feat: aggregator dir scan + empty aggregate writer`

### Task A5: AST import guards (simulator + aggregator)
- [ ] **A5.1** Create `tests/test_smart_shadow_sim_simulator_imports.py`:
  - Reuse exact STDLIB_WHITELIST + FORBIDDEN_PREFIXES + FORBIDDEN_NAMES from P0
  - Assert simulator file's imports all whitelisted
- [ ] **A5.2** Create same guard for aggregator (can be same test file with 2 functions)
- [ ] **A5.3** Run → pass (both scripts already stdlib-only)
- [ ] Commit: `test: AST import guards for simulator + aggregator stdlib-only requirement`

### Task A6: PII injection guard (reuse P0 PII_LITERALS)
- [ ] **A6.1** Create `tests/test_smart_shadow_sim_simulator_pii_guard.py`:
  - `from tests.test_smart_shadow_eval_collector_pii_guard import PII_LITERALS`
  - test creates fixture with PII strings in user_edit_events.jsonl + editor/segments.json + transcript
  - asserts simulator output (decisions.jsonl + report.json) contains 0 of the literals
- [ ] **A6.2** May need fixture project_dir minimal; can write a tiny PII-injected fact + project_dir
- [ ] **A6.3** Run → pass (simulator should not echo any segment content)
- [ ] Commit: `test: PII injection guard reusing P0 PII_LITERALS`

### ⏸ **GATE 1: Phase A complete — pause for human review**

Show user:
- file tree (2 scripts + 4 tests added)
- test count (target: ~6-8 tests passing)
- empty-run smoke output sample
- AST guard + PII guard verified

User approves → Phase B. User objects → fix.

---

## Phase B: Smart Decision Logic + studio_actual Extraction + Per-Job Sidecars

目标：在 1 个 fixture job 上能产出真实 decisions + report（包含 stage diff 和 per-segment）。

### Task B1: editor/segments.json reader (inline, simulator-only)
- [ ] **B1.1** test_simulator_reads_editor_segments fixture: minimal editor/segments.json with 3 segments → simulator reads + caches
- [ ] **B1.2** Add `_load_editor_segments(project_dir: Path) -> list[dict]` helper:
  - Try editor/segments.json first
  - Fallback translation/segments.json
  - Return [] on missing/unreadable
- [ ] **B1.3** Run → pass
- [ ] Commit: `feat: simulator inline editor/segments.json reader`

### Task B2: stage decisions — eligibility_gate + voice_sample_selection + clone_policy
- [ ] **B2.1** test fixture `full_post_phase`: 1 fact (main=2 speakers, both have ≥8s samples) → expect:
  - eligibility_gate.smart_decision = "pass"
  - voice_sample_selection.smart_decision = both speakers "clone"
  - clone_policy.smart_decision = list with 2 speaker_ids
- [ ] **B2.2** Implement helpers:
  - `_decide_eligibility_gate(fact, main_threshold)` → `{decision, reason}`
  - `_decide_voice_sample_selection(fact, soft_seconds, preferred_seconds)` → list of per-speaker dict
  - `_decide_clone_policy(voice_selection_decision)` → list of speaker_ids
  - Each handles `unevaluable` per §3.1 fallback rules
- [ ] **B2.3** Wire into simulator main loop, write to decisions.jsonl
- [ ] **B2.4** Run → pass
- [ ] Commit: `feat: simulator stage decisions — eligibility/voice_selection/clone_policy`

### Task B3: stage decisions — translation_review_auto_approval (event-time signals only)
- [ ] **B3.1** test fixture: low uncertain_speaker_share + clone_eligible_ratio=1.0 → smart_decision = "auto_approve"
- [ ] **B3.2** test fixture: high uncertain_speaker_share OR clone_eligible_ratio < 0.5 → smart_decision = "manual_review_required: <reason>"
- [ ] **B3.3** Implement `_decide_translation_review(fact)` using §3.1 event-time signals (NOT drift, NOT retry — those are post-hoc)
- [ ] **B3.4** Run → pass
- [ ] Commit: `feat: simulator translation_review_auto_approval (event-time signals)`

### Task B4: tts_duration_repair_policy with §3.5 retry formula
- [ ] **B4.1** test fixture: 5 segments, 1 with cn_text length > k_chars × duration × 1.05 → expected_retts_count = 1
- [ ] **B4.2** test: segments with rewrite_count > 0 in editor.segments → expected_retts_count includes those
- [ ] **B4.3** test: total expected_retts × avg_segment > 1.5 × source → would_hit_budget_cap = true
- [ ] **B4.4** Implement `_estimate_retry(segments, source_duration)` per §3.5 formula
- [ ] **B4.5** Run → pass
- [ ] Commit: `feat: simulator tts_duration_repair_policy with retry estimation v1`

### Task B5: subtitle_sync_policy (post-Phase-D only)
- [ ] **B5.1** test: post-Phase-D fact (whisper.alignment_model = "small") → smart_decision = `whisper_align_recommended: true`
- [ ] **B5.2** test: pre-Phase-D fact (whisper data null) → smart_decision = `unevaluable: pre_phase_d_job`
- [ ] **B5.3** Implement `_decide_subtitle_sync(fact)`
- [ ] **B5.4** Run → pass
- [ ] Commit: `feat: simulator subtitle_sync_policy with pre-Phase-D fallback`

### Task B6: studio_actual extraction (§3.4 6-stage table)
- [ ] **B6.1** test for each of 6 stages: feed fixture → assert correct studio_actual extracted:
  - eligibility_gate: always "pass"
  - voice_sample_selection: from `actual_clone_stats.voice_ids_by_speaker`
  - clone_policy: cloned subset of voice_ids
  - translation_review: from `user_edits.text_changes_effective`
  - tts_duration_repair: from `retry_stats.retts_count` (or fallback fact)
  - subtitle_sync: from `whisper.alignment_model + subtitle_sync.text_audio_drift_count`
- [ ] **B6.2** Implement `_extract_studio_actual(fact, stage)` for each
- [ ] **B6.3** Run → pass
- [ ] Commit: `feat: studio_actual extraction for all 6 stages`

### Task B7: diff_kind classification + match field
- [ ] **B7.1** test: smart="pass" + studio="pass" → match=true, diff_kind="match"
- [ ] **B7.2** test: smart="reject" + studio="pass" → match=false, diff_kind="smart_more_aggressive"
- [ ] **B7.3** test: studio="unknown" → match=null, diff_kind="no_studio_signal"
- [ ] **B7.4** Implement `_classify_diff(smart, studio)` with 5 enum
- [ ] **B7.5** Run → pass
- [ ] Commit: `feat: simulator diff_kind classification + match field`

### Task B8: per-segment decisions (only "interesting" segments)
- [ ] **B8.1** test fixture with 5 segments where:
  - seg 1: long cn_text → expected_retts=true → recorded
  - seg 2: rewrite_count > 0 in editor → recorded
  - seg 3: in user_edit_events as speaker_changed → recorded
  - seg 4 + 5: nothing interesting → NOT recorded
- [ ] **B8.2** Implement `_per_segment_decisions(fact, segments, user_edits)` filtering on §3.2 6 conditions
- [ ] **B8.3** Run → pass (3 segments recorded out of 5)
- [ ] Commit: `feat: simulator per-segment decisions for "interesting" segments only`

### Task B9: report.json complete schema (§4.2)
- [ ] **B9.1** test: full fixture run → report.json contains all required v1 keys (smart_eligibility, stage_decisions_count, stage_decisions_match, stages_unevaluable, etc.)
- [ ] **B9.2** Implement `_build_per_job_report(decisions_list, fact, args)` aggregating stage stats
- [ ] **B9.3** Run → pass
- [ ] Commit: `feat: simulator per-job report.json complete schema`

### ⏸ **GATE 2: Phase B complete — code review pause**

Show user:
- 1 fixture full pass with realistic decisions output
- diff_kind distribution
- decisions.jsonl + report.json sample (1 job)
- All Phase A guards still green
- Test count (~25+ tests passing)

User approves → Phase C. User objects → fix.

---

## Phase C: Aggregator + Real Smoke

目标：跨 job 聚合 + 在 3-5 真实历史 job 上跑通。

### Task C1: aggregator scans simulator output + builds aggregate
- [ ] **C1.1** test: 3 mini job sidecars in simulator-out-dir → aggregate_report.json with jobs_simulated=3
- [ ] **C1.2** Implement aggregator main loop: glob `*/smart_shadow_report.json`, accumulate
- [ ] **C1.3** Run → pass
- [ ] Commit: `feat: aggregator dir scan + jobs_simulated count`

### Task C2: cross-job stats — eligibility breakdown + stage match rate
- [ ] **C2.1** test: 5 mock job reports → aggregate `smart_eligibility_breakdown` correct counts
- [ ] **C2.2** test: stage_decision_match_rate per-stage ratio correct
- [ ] **C2.3** Implement `_aggregate_stage_match_rate(per_job_reports)`
- [ ] **C2.4** Run → pass
- [ ] Commit: `feat: aggregator eligibility breakdown + stage match rate`

### Task C3: cross-job stats — voice/translation/drift diff (per §4.3)
- [ ] **C3.1** test: each diff group (`voice_selection_diff`, `translation_review_diff`, `subtitle_drift_observations`) has correct counts
- [ ] **C3.2** Implement aggregations
- [ ] **C3.3** Run → pass
- [ ] Commit: `feat: aggregator voice/translation/subtitle diff aggregations`

### Task C4: retry_estimation_vs_actual + p2_readiness_signals
- [ ] **C4.1** test: aggregate retry estimation error
- [ ] **C4.2** test: p2_readiness_signals correct (post_phase_metered_jobs count, ready_for_p2_rerun bool)
- [ ] **C4.3** Implement
- [ ] **C4.4** Run → pass
- [ ] Commit: `feat: aggregator retry estimation accuracy + p2 readiness gates`

### Task C5: warnings list + edge cases
- [ ] **C5.1** test: jobs with stages_unevaluable > 0 → warnings include corresponding entries
- [ ] **C5.2** test: 0 jobs in metering subset → warning about "smoke needs more post-Phase-D data"
- [ ] **C5.3** Implement warnings collection
- [ ] **C5.4** Run → pass
- [ ] Commit: `feat: aggregator warnings + edge cases`

### Task C6: Local smoke — 3 fixtures + 1 PII fixture
- [ ] **C6.1** Build 4 fixtures (`full_post_phase`, `full_with_user_edits`, `pre_phase`, `corrupted`):
  - facts.jsonl (1 fact each, copy structure from P0 fixtures)
  - project_dir mini (audit/user_edit_events.jsonl + editor/segments.json + metering/usage_events.jsonl + output/* per scenario)
- [ ] **C6.2** test: simulator + aggregator end-to-end on 4 fixtures
- [ ] **C6.3** Run → pass; verify aggregate_report.json has expected structure
- [ ] Commit: `test: e2e fixture suite for simulator + aggregator`

### Task C7: Real-data smoke — 3-5 jobs from .codex_tmp + prod_full
- [ ] **C7.1** Run simulator locally on `D:/Claude/temp/smart_shadow_eval/prod_full/facts.jsonl --limit 5` → outputs to `D:/Claude/temp/smart_shadow_sim/local_smoke/`
- [ ] **C7.2** Run aggregator on local_smoke → aggregate_report.json
- [ ] **C7.3** Manual inspection: check that
  - decisions.jsonl per job is sensible
  - aggregate eligibility breakdown matches P0 results §3
  - voice_selection_diff makes sense
  - PII not leaked

### ⏸ **GATE 3: Phase C complete — show user real aggregate**

Show user:
- aggregate_report.json from 3-5 real jobs
- per-job decisions.jsonl sample
- voice_selection_diff / translation_review_diff actual numbers
- p2_readiness_signals.ready_for_p2_rerun (still false; n=3-5 < 10)
- Total test count (~40+ tests passing)
- Total commits (~14-16)

User approves → P1 done, await more post-Phase-D data accumulation. User objects → fix.

---

## Done Criteria

- [ ] All Phase A/B/C tasks committed
- [ ] All 3 hardening guards green (AST + PII + paths-in-sync if applicable)
- [ ] Local smoke produces sensible aggregate on 3-5 real jobs
- [ ] No production code modified (`src/` / `gateway/` / `services/` untouched)
- [ ] No production audit/ writes
- [ ] No paid API calls
- [ ] No git push without user explicit approval
- [ ] Spec doc cross-link updated in smart-auto-pipeline-plan.md §15 P1 (post-completion)

## Out of Scope (P2/P3/P4)

- Real `service_mode=smart` job creation
- Frontend Smart entry
- Real billing / clone / TTS / verifier API calls
- Lifecycle hooks
- Writing to production `{project_dir}/audit/`
- Multimodal verifier

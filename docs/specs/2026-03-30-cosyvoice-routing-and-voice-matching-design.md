# CosyVoice Routing And Voice Matching Design

**Date:** 2026-03-30

**Status:** Superseded

> **Note (2026-04-08):** CosyVoice 的硬编码映射表（`_BASE_MAP` + `_STYLE_OVERRIDES`）已被统一音色匹配模块（`voice_reranker.py`）替代。CosyVoice 现在与 VolcEngine、MiniMax 共用 `combined_rerank` 9 维评分。Studio 模式支持三引擎自由切换（per-speaker）。详见：
> - `docs/plans/2026-04-08-cosyvoice-unified-matcher-handoff.md` — 统一匹配模块交接文档
> - `docs/plans/2026-04-08-three-engine-voice-selection-plan.md` — 三引擎选择方案
>
> INV-2 不再成立：Studio 模式不再限定 minimax，可选 cosyvoice / volcengine。

**Related Docs:**
- `docs/FREE_VS_PAID_TTS_ROUTING_STATUS_AND_RECOVERY_PLAN.md`
- `docs/POST_MEMBERSHIP_MINIMAL_REPAIR_CHECKLIST.md`
- `docs/specs/2026-03-26-tts-provider-final-architecture.md`

## 1. Goal

Restore the free-vs-paid TTS routing contract end-to-end, then improve the free-tier CosyVoice path so the system can automatically map reviewed speaker traits to suitable official CosyVoice system voices for international deployment.

This design intentionally splits the work into two phases:

1. Restore `--job-id` propagation so per-job `tts_provider` reaches the pipeline.
2. Upgrade CosyVoice voice selection from coarse rule-based mapping to official-doc-informed automatic matching with optional Instruct support where available.

The routing fix is a hard prerequisite for validating any real CosyVoice behavior in the pipeline.

## 2. Product Invariants

The following routing invariants remain unchanged:

- `INV-1`: free user + `service_mode=express` must use `cosyvoice`
- `INV-2`: paid user + `service_mode=studio` must use `minimax`
- `INV-3`: per-job `tts_provider` snapshot must take priority over `admin_settings.json`
- `INV-4`: missing job identity must not silently downgrade to a global default provider

Voice-matching improvements must not alter these invariants. They only refine how the CosyVoice branch chooses a system voice and optional instruction once `tts_provider=cosyvoice` has already been selected.

## 3. Official Aliyun Findings

### 3.1 Recommended API integration mode

For this project, the most suitable CosyVoice integration mode is:

- server-side batch TTS
- Python SDK / DashScope-based synthesis
- subprocess isolation per synthesis call

Reasons:

- The current workflow is offline pipeline generation, not live streaming conversation.
- Existing engineering work has already isolated the DashScope SDK thread-leak risk with `scripts/cosyvoice_tts_helper.py`.
- This keeps the implementation lightweight, testable, and replaceable, which matches project architecture rules.

The design does not recommend switching the pipeline to a browser-side or real-time streaming TTS architecture.

### 3.2 International deployment constraint

Based on the official Aliyun Model Studio CosyVoice docs reviewed on 2026-03-30:

- international deployment supports `cosyvoice-v3-plus`
- international deployment supports `cosyvoice-v3-flash`
- among these, `cosyvoice-v3-flash` is the practical target for system-voice routing with Instruct support

For this project, `cosyvoice-v3-flash` should remain the default international CosyVoice model.

### 3.3 System voice catalog implications

The official system voice list already exists in the repo as:

- `src/services/tts/cosyvoice_voice_catalog.py`

This file records many `cosyvoice-v3-flash` system voices that are available for international use, including a broad set of Mandarin-and-English compatible voices.

Important implication:

- automatic matching should not be limited to only a few demo voices
- the selector should consider the broader international-capable catalog for normal routing
- Instruct must be treated as an optional enhancement layer, not the baseline selection mechanism

For the first matching release, the active candidate set may intentionally exclude:

- dialect voices
- overseas-marketing / export-marketing voices

These categories are still useful catalog assets for future multi-language or region-specific releases, but they do not need to be part of the initial general-purpose matching set.

### 3.4 Instruct support constraint

From the reviewed official voice list, only a subset of system voices explicitly support Instruct.

The strongest currently confirmed Instruct-capable voices are:

- `longanyang`
- `longanhuan`
- `longhuhu_v3`

The official page also shows fixed supported instruction patterns per voice, such as combinations of:

- emotion
- scene + emotion
- role + emotion
- identity + emotion

This means the project should not assume arbitrary free-form prompting works equally across all system voices.

Instead, the system should:

1. choose a voice from the full international-capable catalog
2. attach `instruction` only when the selected voice is known to support it
3. generate instructions from constrained enums aligned with the official supported patterns

## 4. Current Project State

### 4.1 Routing chain status

The original free-tier routing failure root cause has already been identified and functionally repaired in the current working branch:

- Gateway writes per-job `tts_provider=cosyvoice`
- `src/services/jobs/process_runner.py` now passes `--job-id`
- `main.py` now parses `--job-id`
- pipeline can now load the job snapshot
- `tts_strategy` can resolve the per-job provider from the snapshot before global fallback
- remote validation has already proven the `express -> cosyvoice` path end-to-end

The historical failure mode was:

- free `express` jobs incorrectly run through `minimax`

Current implication:

- Phase A root-cause repair is no longer the open design problem
- the remaining design work is Phase B quality improvement inside the already-restored CosyVoice branch
- routing invariants should now be treated as regression requirements, not speculative work

### 4.2 Pipeline readiness after routing restoration

The downstream pieces are already mostly in place:

- `src/pipeline/process.py` already includes `ProcessConfig.job_id`
- pipeline already knows how to load a job snapshot by `job_id`
- `src/services/tts/tts_generator.py` already resolves provider from `job_record`
- `src/services/tts/tts_strategy.py` already prioritizes per-job `tts_provider`

This confirms the issue is the missing identity propagation link, not the provider selection core.

### 4.3 Current speaker-trait extraction

The current S2 review flow already extracts meaningful voice-related metadata.

`src/services/transcript_reviewer.py` currently asks the reviewer to output:

- speaker name
- `gender`
- `age_group`
- `role`
- `style`
- `voice_description`

`src/pipeline/process.py` then injects these into segments and derives:

- `persona_style`
- `energy_level`

This means the system already has a usable speaker profile. The main gaps are:

- the extracted data is still too loosely structured for deterministic matching
- the selector does not explicitly model official CosyVoice voice categories and Instruct templates
- there is no path yet for passing `instruction` into CosyVoice synthesis

### 4.4 Current CosyVoice selector limitations

`src/services/tts/cosyvoice_voice_selector.py` currently:

- infers `persona_style` from keyword rules
- infers `energy_level` from keyword rules
- selects a voice mainly from `gender + age_group + persona_style`

Current limitations:

- no category-aware ranking across the broader official catalog
- no mapping from reviewed speaker role into official voice categories
- no explicit support for childlike or strongly stylized voices
- no optional Instruct generation
- no separation between "best voice match" and "best Instruct-capable voice"

## 5. Design Principles

The implementation should follow these principles:

- preserve current routing architecture
- avoid redesigning TTS provider selection
- keep deterministic matching ahead of any LLM-driven choice
- use official voice metadata as the source of truth
- keep Instruct generation constrained and auditable
- keep the CosyVoice path replaceable and testable

## 6. Proposed Design

### 6.1 Phase separation

The work is split into two independent but ordered deliverables.

#### Phase A: routing recovery

Restore `--job-id` propagation so per-job provider routing works again.

Scope:

- `src/services/jobs/process_runner.py`
- `main.py`
- `tests/test_tts_routing_invariants.py`

Output:

- free `express` route reaches real CosyVoice branch
- paid `studio` route remains on Minimax

Phase A is not considered complete when only local tests pass.

**Phase A Done requires all of the following in the remote container environment:**

- an `express` job runs with the deployed container code
- pipeline logs show the job snapshot was loaded by `job_id`
- pipeline logs show `TTS provider: cosyvoice`
- logs or runtime evidence confirm the CosyVoice helper path was actually invoked
- the job reaches `succeeded`
- output artifacts are complete
- CosyVoice-generated WAV output is accepted by the downstream alignment pipeline without format-related failure

Phase B must not begin until this definition is met.

#### Phase B: CosyVoice automatic voice matching

Improve only the CosyVoice branch after routing is proven healthy.

Scope:

- reviewer output schema
- speaker-profile normalization
- CosyVoice voice selection
- optional Instruct generation
- offline voice-profile enrichment for reranking
- tests

Output:

- Phase B1: better automatic system-voice assignment for free-tier jobs using official metadata as the primary source of truth
- Phase B1: optional official-template-compatible `instruction` for supported voices
- Phase B2: improved reranking precision from an offline voice-profile library built from uniform calibration samples

Phase B is split into two implementation layers:

- **Phase B1**: production-safe baseline matching
  - uses official voice metadata plus normalized speaker traits
  - does not depend on Gemini audio labeling to function
  - must be shippable on its own
- **Phase B2**: offline profile enhancement
  - generates uniform calibration samples for candidate voices
  - uses Gemini multimodal analysis to create structured voice-profile labels
  - only improves reranking / tie-break behavior
  - must not become a runtime hard dependency for core routing

### 6.2 Speaker profile model

The project should normalize reviewed speaker metadata into a small deterministic profile structure.

Proposed normalized fields:

- `gender`
- `age_group`
- `role_archetype`
- `delivery_scene`
- `emotion_default`
- `persona_style`
- `energy_level`
- `voice_texture`
- `is_childlike`

Field intent:

- `role_archetype`: host, narrator, customer_service, assistant, storyteller, commentator, child_character, robotic_character, generic
- `delivery_scene`: chat, news, ad, promo, navigation, podcast, drama, poetry, education, customer_service, audiobook, short_video, generic
- `emotion_default`: neutral, warm, cheerful, serious, calm, lively, empathetic, melancholy
- `voice_texture`: bright, clear, low, husky, soft, magnetic, crisp, gentle, mature
- `is_childlike`: boolean shortcut for child/young-character routing

Important rule:

- these values should be constrained enums or controlled vocabularies
- free-form `voice_description` remains available for traceability and fallback inference
- deterministic fields must drive routing
- `voice_texture` should be treated as a secondary tie-breaker in Phase B v1, not a primary routing dimension

Not included in the Phase B v1 speaker profile:

- `language_variant`

Reason:

- for this product, language or dialect preference is more naturally a job-level or user-level preference than a speaker trait inferred from source transcript review
- if needed later, language preference can be introduced as a higher-level routing constraint instead of a speaker-level field

### 6.3 Review-stage changes

The transcript reviewer prompt should be upgraded to request structured speaker output that is closer to official CosyVoice routing needs.

Current state:

- reviewer returns `role`, `style`, `voice_description`
- pipeline heuristically infers `persona_style` and `energy_level`

Proposed state:

- reviewer keeps the current fields for backward compatibility
- reviewer additionally returns:
  - `role_archetype`
  - `delivery_scene`
  - `emotion_default`
  - `voice_texture`
  - `is_childlike`

These new fields should be optional at first, with deterministic local fallback inference when absent.

Phase B v1 guidance:

- `voice_texture` is useful but noisy, so it should remain optional and low-weight
- `role_archetype`, `delivery_scene`, and `is_childlike` should carry more routing weight than `voice_texture`

### 6.4 CosyVoice routing layers

CosyVoice selection should become a two-layer decision.

#### Layer 1: base voice match

Select the best matching system voice from the international-capable official catalog using:

- gender
- age group
- role archetype
- scene
- persona
- energy
- texture as a tie-breaker only
- childlike flag

This layer should work for all supported voices and produce:

- `voice_id`
- `match_reason`
- `match_score`
- `match_confidence`
- optional `backup_voices`

#### Layer 2: optional Instruct enhancement

If the selected voice is one of the known Instruct-capable voices, generate a constrained instruction string using the official supported template families for that voice.

If the selected voice does not support Instruct:

- do not generate instruction
- continue synthesis with voice-only routing

This avoids overfitting the whole catalog around Instruct.

### 6.4B Offline voice-profile reranking

Phase B2 should add an offline enhancement layer for higher-precision reranking.

Design rules:

- do not rely on official preview audio as the primary profiling source
- instead, synthesize a **uniform calibration script** across candidate voices
- use these same-text audio samples to build a comparable voice-profile library
- run Gemini multimodal analysis offline to generate structured tags
- keep a human-review step before these tags are promoted into the production catalog

Why a uniform calibration script is preferred:

- official preview clips often use different text
- different text introduces performance-style variance that can be mistaken for timbre variance
- same-text samples make cross-voice comparison much more stable

The offline profile library should be used only for:

- rerank
- tie-break
- confidence refinement

It should not replace the primary metadata-based selector.

Important distinction:

- B1 speaker-profile fields describe the **source speaker**
- B2 offline profile labels describe the **target CosyVoice voice**

Because they describe different entities, some similarly named dimensions are acceptable. However, B2 should avoid overweighting duplicated semantics during rerank.

Calibration-script design constraints:

- start with one required neutral calibration script shared across all candidate voices
- the script should be 2-3 sentences, roughly 50-80 Chinese characters total
- wording should be emotionally neutral, non-technical, and phonetically varied enough to expose stable timbre differences
- if one neutral script proves too weak for reliable differentiation, add at most one secondary light-conversational sample instead of expanding into many styles
- B2 should not depend on a large prompt set; keep the offline profiling dataset compact and auditable

Suggested structured offline labels for the **voice-side** profile catalog:

Primary rerank labels:

- `pitch_level`: low / mid / high
- `warmth`: low / medium / high
- `authority`: low / medium / high
- `intimacy`: low / medium / high

Secondary consistency labels:

- `energy_level`: low / medium / high
- `brightness`: low / medium / high
- `maturity`: child / young / adult / elder
- `delivery_style`: narration / assistant / customer_service / companion / explainer / storyteller
- `texture_tags`: soft / crisp / magnetic / husky / airy / steady
- `childlike`: true / false

Scoring guidance:

- primary rerank labels should contribute most of the B2 incremental signal
- secondary labels should be used as consistency checks, tie-breakers, or confidence refiners
- B2 must not double-count B1 semantics so heavily that it overrides a strong primary match for the wrong reasons

These labels are intended to improve precision among already-plausible candidates, especially where official traits are semantically close.

### 6.4A Module responsibility model

Phase B should not introduce two unrelated modules that both own `speaker profile -> voice_id`.

Recommended model:

- keep `src/services/tts/cosyvoice_voice_selector.py` as the base deterministic selector
- add a new wrapper module that enhances the selector result
- the wrapper may:
  - keep the selector's chosen voice
  - promote the result to a better Instruct-capable anchor voice when the profile strongly matches an official template family
  - attach an optional `instruction`

Recommended new module name:

- `src/services/tts/cosyvoice_instruction_enhancer.py`

This name is preferred over `router` because the module should wrap and enhance the existing selector, not replace provider routing.

Responsibility split:

- `cosyvoice_voice_selector.py`
  - base deterministic voice selection
  - backward-compatible fallback behavior

- `cosyvoice_instruction_enhancer.py`
  - inspect normalized speaker profile
  - call the selector for the baseline result
  - decide whether to keep the selected voice or promote to an Instruct-capable anchor voice
  - generate optional `instruction`
  - return reasoning metadata

- `cosyvoice_provider.py`
  - transport boundary only
  - accepts `voice`, `model`, optional `instruction`
  - no voice-routing logic

- `scripts/cosyvoice_tts_helper.py`
  - SDK execution only
  - no routing logic
  - no catalog logic
  - no reviewer logic

### 6.5 Voice routing strategy

The routing strategy should distinguish three categories.

#### Category A: Instruct-capable anchor voices

Use when the speaker profile strongly matches the official templates.

- `longanyang`
  - adult male narration
  - news
  - ad / promo
  - commentary
  - navigation
  - children content explanation

- `longanhuan`
  - energetic or warm female
  - customer service
  - radio / podcast
  - drama explanation
  - poetry
  - education / popular science
  - product promotion

- `longhuhu_v3`
  - child voice
  - toy / story machine
  - strong character voice
  - robotic or IP-like young-character scenarios

#### Category B: broad international system voices

Use for normal matching when Instruct is unnecessary or unsupported.

Examples already in the initial active candidate set:

- assistant-like voices
- customer-service voices
- audiobook voices
- social companion voices
- news / broadcast voices

Explicitly deferred from the first release candidate pool:

- dialect voices
- overseas-marketing voices

Catalog requirement:

- `src/services/tts/cosyvoice_voice_catalog.py` should distinguish between "voice exists in catalog" and "voice is active in the current matching pool"
- add a `matchable` flag or equivalent filtering mechanism
- deferred voices stay in the catalog for future releases but default to non-matchable in Phase B1

#### Category C: deterministic fallback

If structured matching confidence is low:

- fall back to a stable age-and-gender-based voice
- never fail the whole job only because a fine-grained match is ambiguous

### 6.6 Instruction generation rules

Instruction generation must be deterministic and template-driven.

It should not generate arbitrary prose directly from `voice_description`.

Instead:

1. choose an official template family supported by the selected voice
2. fill slots from normalized profile enums
3. validate the final instruction against a per-voice allowlist

Example shape:

- `scene + emotion`
- `role + emotion`
- `identity + emotion`

The implementation should store per-voice supported template definitions locally so the behavior is stable even if the web docs change later.

### 6.7 SDK/helper boundary

The existing helper isolation pattern should be preserved.

Current helper inputs:

- `text`
- `voice`
- `model`
- `output_path`

Planned extension:

- optional `instruction`

The helper remains responsible only for:

- loading env
- calling DashScope SDK
- writing audio
- exiting cleanly

It should not contain routing logic.

Routing and instruction generation should stay in Python service-layer code before the helper call.

Before implementing this extension, a short SDK spike is required to confirm the exact Python API shape for `instruction`.

## 7. Data Flow

### 7.1 Routing recovery path

1. Gateway computes policy and writes per-job snapshot
2. Job runner passes `--job-id`
3. `main.py` parses `--job-id`
4. pipeline loads job snapshot
5. TTS generator resolves provider from job record
6. express job enters CosyVoice branch
7. studio job enters Minimax branch

### 7.2 CosyVoice matching path

1. S2 review produces speaker metadata
2. pipeline normalizes metadata into speaker profile
3. CosyVoice matcher selects best `voice_id`
4. matcher optionally generates `instruction`
5. TTS generator passes voice and optional instruction into CosyVoice helper
6. helper synthesizes and exits

## 8. File-Level Design

### 8.1 Files to modify for routing recovery

- `src/services/jobs/process_runner.py`
- `main.py`
- `tests/test_tts_routing_invariants.py`

### 8.2 Files to modify for voice matching

- `src/services/transcript_reviewer.py`
- `src/pipeline/process.py`
- `src/services/tts/tts_generator.py`
- `src/services/tts/cosyvoice_provider.py`
- `scripts/cosyvoice_tts_helper.py`
- `src/services/tts/cosyvoice_voice_selector.py`
- `src/services/tts/cosyvoice_voice_catalog.py`

Expected catalog change:

- `src/services/tts/cosyvoice_voice_catalog.py` should expose whether a voice is present in the full catalog and whether it is currently eligible for matching
- dialect and overseas-marketing voices remain cataloged but default to `matchable=False` for the first release candidate set

### 8.3 Suggested new files

- `src/services/tts/cosyvoice_instruction_enhancer.py`
  - call the existing selector
  - optionally promote to an Instruct-capable anchor voice
  - return `voice_id`, optional `instruction`, and reasoning metadata

- `tests/test_cosyvoice_instruction_enhancer.py`
  - cover deterministic enhancement and instruction generation

- `scripts/cosyvoice_calibration_sample_builder.py`
  - generate uniform calibration text samples across candidate voices
  - export local audio assets for offline profiling

- `src/services/tts/cosyvoice_voice_profile_catalog.py`
  - store reviewed offline voice-profile labels used for rerank / tie-break

- `tests/test_cosyvoice_voice_profile_catalog.py`
  - validate schema integrity and fallback behavior when no offline labels exist

- optional static metadata file or module for per-voice template capabilities
  - keep official template constraints local and testable

## 9. Testing Strategy

### 9.1 Routing tests

Routing-recovery implementation is already complete in the current working branch. The test requirement now becomes regression protection.

Required regression coverage:

- keep the former `xfail` cases in `tests/test_tts_routing_invariants.py` passing as hard assertions
- ensure `--job-id` appears in runner command construction
- ensure `main.py` accepts `--job-id`
- ensure per-job `tts_provider` still outranks global fallback behavior

End-to-end verification:

- `express` job logs `TTS provider: cosyvoice`
- `studio` job logs `TTS provider: minimax`
- pipeline logs snapshot load via `job_id`
- the remote `express` job reaches `succeeded`
- CosyVoice helper is actually invoked during the remote `express` run
- CosyVoice-generated WAV output is consumed successfully by downstream alignment

### 9.2 Matcher unit tests

Add deterministic tests for:

- adult male narrator -> `longanyang`
- warm energetic female support/promo voice -> `longanhuan`
- childlike speaker -> `longhuhu_v3`
- elderly male -> stable fallback voice
- unsupported scene/persona combination -> non-Instruct voice-only routing
- low-confidence profile -> stable selector fallback without instruction

### 9.3 Instruction tests

Add tests for:

- Instruct generated only for supported voices
- instruction template family chosen from allowlist
- invalid combination falls back to no instruction
- non-Instruct voice never receives instruction

### 9.4 Integration tests

Add integration coverage to confirm:

- segment metadata survives S2 -> S4 handoff
- matcher output is wired into TTS generation
- helper request payload includes `instruction` only when expected

### 9.5 Offline profile enhancement tests

Add tests for the Phase B2 enhancement layer:

- calibration sample generation uses the same text across candidate voices
- offline profile labels load correctly from the local catalog
- rerank improves tie-break decisions without overriding strong primary matches
- absence of offline profile labels falls back cleanly to the Phase B1 selector
- Gemini-derived labels are optional, not required for runtime routing

### 9.6 Helper compatibility smoke validation

Phase A and Phase B both require a lightweight smoke validation outside pure unit tests:

- run the CosyVoice helper on a representative sample text
- verify the output file is a valid WAV
- verify the downstream alignment pipeline can read the generated WAV
- verify no sample-rate or format mismatch breaks the job after synthesis

## 10. Deployment And Validation

Deployment method must follow the active environment topology.

Current remote environment:

- `app` uses bind mounts for `src/`, `main.py`, and `scripts/`
- code deployment is: sync files to the remote host bind-mount source, then restart `app`
- runtime fingerprint validation is still required after restart

If a future environment does not use bind mounts, the equivalent fallback is:

- copy the required files into the container image filesystem
- restart the container
- verify runtime fingerprint inside the container

Validation order:

1. deploy routing fix only
2. verify express/studio provider routing
3. deploy voice-matching changes
4. verify representative CosyVoice scenarios

Emergency rollback path:

- if the routing recovery causes free-tier jobs to fail unexpectedly in production, the fastest rollback is to restore the previous `process_runner.py` behavior that does not pass `--job-id`, then restart the container
- this is acceptable as an emergency rollback only
- this rollback reverts free-tier jobs to the previous global-default behavior and is not a valid steady state

## 11. Risks

### 11.1 Highest risk

The highest-risk unknown is the exact Python SDK call shape for `instruction`.

Official docs clearly expose instruction capability, but the currently reviewed Python quick-start examples do not fully show the same request surface as the Java parameter examples.

Mitigation:

- do a small spike before full implementation
- inspect both `SpeechSynthesizer.__init__` and `SpeechSynthesizer.call`
- validate the exact SDK parameter shape in isolation
- attempt a minimal live synthesis using a known Instruct-capable voice and a trivial instruction
- if the Python SDK surface is insufficient, keep helper architecture and swap only the helper transport layer

### 11.2 Medium risk

Reviewer outputs may remain noisy or inconsistent.

Mitigation:

- add normalized constrained fields
- keep deterministic fallback inference
- make voice matching robust to partial data

### 11.3 Medium risk

The broader voice catalog may tempt over-complex ranking logic.

Mitigation:

- start with a small weighted scoring model
- keep rules transparent and test-backed
- avoid LLM-in-the-loop runtime matching

### 11.4 Medium risk

Gemini multimodal labeling may overfit to performance differences if source clips do not share the same text.

Mitigation:

- use a uniform calibration script for candidate voices
- keep Gemini analysis offline only
- require human review before promoting labels into the production profile catalog
- use the resulting labels only for rerank / tie-break, not first-pass routing

## 12. Recommended Execution Order

### Milestone 1: restore routing

- implement `--job-id` propagation
- remove `xfail`
- run invariant tests
- deploy and verify express/studio paths
- complete the remote-container Phase A Done checklist

### Milestone 2: Phase B1 design lock

- run the SDK `instruction` spike first
- lock normalized speaker profile schema
- lock per-voice Instruct capability metadata
- lock candidate-routing table

### Milestone 3: implement Phase B1 baseline matcher

- add matcher module
- wire profile normalization
- pass optional instruction to helper
- add tests

### Milestone 4: validate Phase B1 baseline matching

B1 Done requires all of the following:

- Phase A routing regression checks remain green
- the active candidate pool is explicitly defined and excludes deferred voices by default
- the selector returns `voice_id`, `match_reason`, and `match_confidence`
- the enhancer adds `instruction` only for officially supported voices
- the system still degrades cleanly to voice-only routing when instruction is unsupported or ambiguous
- representative express jobs produce expected voices without breaking output artifacts
- the matching result is inspectable in logs or structured metadata so product surfaces can present a confidence hint

- free express adult male narration sample
- free express warm female customer-service / explainer sample
- free express childlike sample
- verify no hang and correct output artifacts

### Milestone 5: Phase B2 offline profile enhancement

- define the calibration script
- generate same-text calibration samples for the active candidate set
- run Gemini multimodal analysis offline
- review and normalize the generated labels
- store reviewed labels in the local voice-profile catalog
- add rerank and fallback tests

## 13. Recommendation

Proceed immediately with the routing recovery as the smallest safe change set.

Do not implement voice-matching changes until the free-tier CosyVoice branch is demonstrably live in the full pipeline.

After routing is fixed, implement the CosyVoice matching improvement as a deterministic, test-first enhancement centered on:

- normalized S2 speaker traits
- catalog-aware system voice scoring
- optional Instruct only for officially supported voices
- offline Gemini-assisted voice-profile reranking as a non-blocking enhancement layer

## 14. References

- Aliyun Model Studio CosyVoice overview: `https://help.aliyun.com/zh/model-studio/text-to-speech`
- Aliyun CosyVoice voice list: `https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list`
- Aliyun CosyVoice SDK reference used during review: `https://help.aliyun.com/zh/model-studio/developer-reference/quick-start-cosyvoice`
- Aliyun Java SDK reference reviewed for instruction-related parameters: `https://help.aliyun.com/zh/model-studio/cosyvoice-java-sdk`

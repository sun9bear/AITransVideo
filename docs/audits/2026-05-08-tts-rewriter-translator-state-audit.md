# TTS / Rewriter / Translator 共享状态审计

**Plan ref**: `docs/plans/2026-05-08-p2-17-pipeline-parallelization-plan.md` §3.1 + §5 step 2
**目的**: 列出 `TTSGenerator` / `GeminiRewriter` / `GeminiTranslator` 三个类所有 `self.X = ` 赋值，标明 read/write 时机；为每条 mutable 字段决定一种并发处理方式（① 改 local return / ② 加锁 / ③ 接受 `_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1` 串行作为唯一保障）。
**结论先行**: 三个对象都是 stateful 且整个 pipeline 复用同一实例。`SegmentAligner` 进入 thread pool 后，rewriter + TTS fallback 的 critical section 必须由 `_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1`（17a-1 引入）完整覆盖才能保正确性。**这条不是成本保守值，是正确性约束**。
**未来调高 paid_fallback 到 ≥2 的前置**: 必须先把所有 ③ 标注的字段升级到 ① 或 ②。

---

## 1. `TTSGenerator`（[tts_generator.py:173](src/services/tts/tts_generator.py:173)）

`SegmentAligner` 的 fallback 路径在 [aligner.py:489](src/services/alignment/aligner.py:489) 直接调用 `tts_generator._generate_one(segment, ...)`，跳过 `generate_all`。Pipeline 在 [process.py:2807-2814](src/pipeline/process.py:2807) 把同一个 `TTSGenerator` 实例传给 `SegmentAligner`。

| 字段 | 写入时机 | 读取时机 | 并发风险 | 处理 |
|---|---|---|---|---|
| `self.config` ([tts_generator.py:179](src/services/tts/tts_generator.py:179)) | `__init__` 一次 | 各处 read | 无（init-only） | 不动 |
| `self._default_job_record` ([:190](src/services/tts/tts_generator.py:190)) | `__init__` 一次 | `_resolve_active_job_record` ([:662](src/services/tts/tts_generator.py:662)) | 无（init-only） | 不动 |
| `self._speaker_voice_cache` ([:197](src/services/tts/tts_generator.py:197)) | `__init__` 设空 dict；`generate_all` 入口 `clear()` ([:257](src/services/tts/tts_generator.py:257))；`_generate_one_*` 内部三处 setitem ([:577, :765, :1077](src/services/tts/tts_generator.py:577)) | 三个 provider 路径的 `if speaker_id in cache` 分支 ([:536, :735, :1046](src/services/tts/tts_generator.py:536)) | **高**: alignment fallback 直接调 `_generate_one`，并发触发 → cache `__setitem__` race；CPython dict ops 单写不撕裂但 `if x in cache: ...; cache[x] = ...` 这种 read-modify-write 可能让两个 worker 同时 auto-match 同一 speaker，浪费一次 LLM/probe 调用 | ③ paid_fallback=1 串行 — 加锁会拖慢 sequential generate_all 的内部并行（segment > 100 时启 3-worker），不值。同时 alignment fallback 发生概率本就低，串行可接受 |
| `self._chars_per_second_by_speaker` ([:203, :223](src/services/tts/tts_generator.py:203)) | `__init__` 设空；`set_speaker_chars_per_second` 外部 setter ([:223](src/services/tts/tts_generator.py:223)) | `speed_decision` 路径读 ([:594, :785, :1099](src/services/tts/tts_generator.py:594)) | 中：pipeline 在调 `generate_all` 之前一次性 set；alignment 阶段不会再调用 setter，所以并发读时不会被改写 | 不动（read-mostly during align_all） |
| `self._global_chars_per_second` ([:204, :224](src/services/tts/tts_generator.py:204)) | 同上 | 同上 | 同上 | 不动 |
| `self._usage_meter` ([:205, :208](src/services/tts/tts_generator.py:205)) | `__init__` None；`set_usage_meter` ([:207](src/services/tts/tts_generator.py:207)) 外部 setter | `_record_tts_usage` 读 | 低：pipeline 启动期 set 一次，alignment 阶段不再变 | 不动 |
| `self._active_job_record` ([:264](src/services/tts/tts_generator.py:264)) | `generate_all` 入口写当前 job record | `_resolve_active_job_record` ([:662](src/services/tts/tts_generator.py:662)) → `_generate_one_volcengine` 等读 | **高**: alignment fallback 走 `_generate_one` 但**不经过 `generate_all`**，意味着这字段反映的是上一次 `generate_all` 调用的 job record；在多 job 共用一个 TTSGenerator 的情况下并发可能读到错的 job 字段（但本项目当前是 per-job 实例化，单进程多 job 不共享，影响有限） | ③ paid_fallback=1 串行；并把"alignment fallback 期间不再次调 `generate_all`"作为隐式契约写进 commit message |
| `self._job_provider` ([:266](src/services/tts/tts_generator.py:266)) | `generate_all` 入口写 | `print` + 后续 dispatch | 同上 | 同上 ③ |
| `self._OUTER_BACKOFF_SCHEDULE` / `self._OUTER_PAUSE_SECONDS` / `self._PARALLEL_THRESHOLD` / `self._PARALLEL_WORKERS` | class-level 常量 | read-only | 无 | 不动 |

**`TTSGenerator.generate_all` 内部并发**: [tts_generator.py:277-284](src/services/tts/tts_generator.py:277) 在 segment > 100 时已启 3-worker 内部并行 (`_generate_all_parallel`)。alignment fallback 不走 `generate_all`，所以两层并发不会嵌套触发——但需要 17a-1 的实现注释里写明这点，未来若改成走 `generate_all` 会出现 N×3 嵌套。

---

## 2. `GeminiRewriter`（[rewriter.py:26](src/services/gemini/rewriter.py:26)）

| 字段 | 写入时机 | 读取时机 | 并发风险 | 处理 |
|---|---|---|---|---|
| `self.translator` ([rewriter.py:35](src/services/gemini/rewriter.py:35)) | `__init__` 一次 | `_call_task_with_usage_phase` 委托 ([:168](src/services/gemini/rewriter.py:168)) | 无（init-only），但**指向的对象自身有共享状态**——见 §3 | 不动 rewriter，但要看 §3 |
| `self.chars_per_second` ([:36](src/services/gemini/rewriter.py:36)) | `__init__` 一次 | `rewrite_for_duration_with_profile` ([:86](src/services/gemini/rewriter.py:86)) read | 无 | 不动 |
| `self.chars_per_second_by_speaker` ([:37](src/services/gemini/rewriter.py:37)) | `__init__` 一次（构造一个新 dict，不共享外部对象） | read 同上 | 无 | 不动 |
| `self.rewrite_prompt_template` ([:41](src/services/gemini/rewriter.py:41)) | `__init__` 一次 | read | 无 | 不动 |
| `self.usage_phase` ([:44](src/services/gemini/rewriter.py:44)) | `__init__` 一次 | `_call_task_with_usage_phase` read ([:165, :174](src/services/gemini/rewriter.py:165)) | 无 | 不动 |

**`GeminiRewriter` 自身是无状态的**（init 后所有 `self.X` 不再 mutate）。**问题完全在它对 `self.translator` 的副作用**——见下条。

---

## 3. `GeminiTranslator`（[translator.py:387](src/services/gemini/translator.py:387)）

`GeminiRewriter` 的所有付费 LLM 调用都通过 `self.translator._call_task_with_fallback` 走，且会在前后**临时改写 `translator._metering_usage_context`**（[rewriter.py:164-175](src/services/gemini/rewriter.py:164)）。同一个 `GeminiTranslator` 实例在 pipeline 里既被 `GeminiRewriter` 用，也被翻译主路径用。

| 字段 | 写入时机 | 读取时机 | 并发风险 | 处理 |
|---|---|---|---|---|
| `self.api_key` / `self.model_name` / `self.temperature` / `self.max_output_tokens` / `self.sdk_backend` / `self.llm_router` ([translator.py:405-410](src/services/gemini/translator.py:405)) | `__init__` 一次 | read | 无（init-only） | 不动 |
| `self.speaker_infer_prompt_template` / `self.translation_prompt_template` ([:411, :414](src/services/gemini/translator.py:411)) | `__init__` 一次 | read | 无 | 不动 |
| `self._usage_meter` ([:422, :440](src/services/gemini/translator.py:422)) | `__init__` None；`set_usage_meter` 外部 setter ([:440](src/services/gemini/translator.py:440)) | `_call_task_with_fallback` 内 `meter.record_llm` ([:471](src/services/gemini/translator.py:471)) | 低：pipeline 启动期 set 一次，alignment 阶段不再变 | 不动 |
| `self._metering_usage_context` ([:423](src/services/gemini/translator.py:423)) | `__init__` 设 ""；`GeminiRewriter._call_task_with_usage_phase` try/finally 改写 ([rewriter.py:166, :175](src/services/gemini/rewriter.py:166)) | `_call_task_with_fallback` → `meter.record_llm(phase=getattr(self, "_metering_usage_context", "") or "", ...)` ([translator.py:473](src/services/gemini/translator.py:473)) | **高**: alignment 并发触发多路 rewrite → 多线程交错 setattr/restore，B 线程 capture 到 A 的值作为 previous_phase，A finally restore 把字段拨回空串，B 的 `record_llm` 读到错误 phase。**后果**：admin 后台基于 phase 的成本归因失真（不是用户 visible，但影响内部成本面板与 quota 统计） | ③ paid_fallback=1 串行 — 这是 §3.1 audit 段强调的真实证据。修复 ① 需要把 phase 改成 thread-local 或 per-call 参数（重写 rewriter ↔ translator 的 contract），太大；② 加锁会把整段 LLM 调用串行化，与 paid_fallback=1 等价。两者都不优于 ③ |
| `self._legacy_sdk` ([:431](src/services/gemini/translator.py:431)) | 仅 legacy SDK 分支 init | 各处 model 调 | 无（init-only）；legacy 分支本就是 process-wide 单点 | 不动 |
| `self.model` ([:432](src/services/gemini/translator.py:432)) | legacy 分支 init | model 调 | 同上 | 不动 |
| `self.client` ([:436](src/services/gemini/translator.py:436)) | google-genai 分支 init | model 调 | 无（init-only） | 不动 |
| `self._types_module` ([:437](src/services/gemini/translator.py:437)) | `__init__` 一次 | read | 无 | 不动 |
| `self._service_mode`（外部设置，[process.py:1565](src/pipeline/process.py:1565)） | pipeline 启动 stage 写一次 | `_call_task_with_fallback` ([:1216](src/services/gemini/translator.py:1216)) read | 低：pipeline 单线程 stage 启动时 set，alignment 阶段不再改 | 不动 |

**关键泄漏点**: `_metering_usage_context` 的 try/finally 模式不是 thread-safe。这是 §3.1 audit "GeminiRewriter 通过 GeminiTranslator 维护临时共享状态" 的具体来源，**和 §3.4 17d 里的 `OpenAICompatibleTranslationProvider._retry_report` 是两条独立泄漏**，不要混淆。

---

## 4. 处理方式分布

| 处理 | 字段数 | 说明 |
|---|---|---|
| ① local return | 0 | 本审计未提议任何字段改 local return（成本/收益不划算） |
| ② 加锁 | 0 | 同上——加锁实质等于 paid_fallback=1，不如直接走 ③ |
| ③ paid_fallback=1 串行 | 4 | `TTSGenerator._speaker_voice_cache` / `_active_job_record` / `_job_provider`、`GeminiTranslator._metering_usage_context` |
| 不动（init-only / read-mostly during align） | 大多数 | 见各表 |

---

## 5. 在 `_attempt_rewrite_loop` semaphore acquire 处必须出现的注释（17a-1 commit 时贴）

```python
# Paid-fallback semaphore. Default _ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1
# is a CORRECTNESS constraint, not a cost-conservatism value.
#
# The rewrite + TTS fallback path mutates 4 shared fields on instances
# that are reused across the whole pipeline (see audit:
# docs/audits/2026-05-08-tts-rewriter-translator-state-audit.md):
#
#   - GeminiTranslator._metering_usage_context (rewriter try/finally
#     setattr; concurrent rewrite races corrupt usage phase attribution).
#   - TTSGenerator._speaker_voice_cache (per-speaker auto-match cache;
#     concurrent _generate_one writes can duplicate match work).
#   - TTSGenerator._active_job_record / _job_provider (set in
#     generate_all but read by the direct _generate_one path; concurrent
#     calls may read stale state).
#
# Raising max concurrency above 1 requires upgrading each of these to
# either ① local-return semantics or ② an explicit lock; do NOT just
# bump the env.
```

---

## 6. References

- 方案: [`docs/plans/2026-05-08-p2-17-pipeline-parallelization-plan.md`](../plans/2026-05-08-p2-17-pipeline-parallelization-plan.md) §3.1 audit 子节
- TTSGenerator: [`src/services/tts/tts_generator.py:173`](../../src/services/tts/tts_generator.py)
- GeminiRewriter: [`src/services/gemini/rewriter.py:26`](../../src/services/gemini/rewriter.py)
- GeminiTranslator: [`src/services/gemini/translator.py:387`](../../src/services/gemini/translator.py)
- 与 17d 区分的另一条泄漏（不在本 audit 范围）: `OpenAICompatibleTranslationProvider._retry_report` ([`src/modules/translation/providers.py:300`](../../src/modules/translation/providers.py))

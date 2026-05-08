# P2-17 流水线串行 → 并行化方案

**Audit ref**: `docs/audits/2026-05-07-comprehensive-codebase-audit.md` §9 P2-17（"性能瓶颈中只有 IO 类属 P1；pipeline 串行属吞吐优化，归 P2"）
**状态**: 设计待审 v2 — 不要在审核通过前动代码
**Drafted**: 2026-05-08
**Last updated**: 2026-05-08（v2 — 整合二轮代码审查反馈）
**Author**: Claude (cross-session handoff)

**Codex review update (2026-05-08, v1)**:
- 方案方向保留：4 个并行点必须拆开做，不能一个 commit 合并。
- 但原稿低估了 17a 的线程安全风险：`SegmentAligner` 当前有 `self._last_dsp_fit_result` 和共享 `PostTTSBudgetTracker`，不能直接把同一个 `self._align_one` 丢进线程池。
- 17b / 17c 的“互不依赖”假设与当前代码不符：默认 AssemblyAI 转录依赖 `speech_audio_path`；S2 Pass 2 依赖 Pass 1 输出的 `pass1_lines` / `speakers`。
- 17d 需要先处理 provider 实例状态（例如 `_retry_report`）和全局付费 LLM 并发阀，再考虑 chunk 并行。
- 审核结论：**只允许把 17a 作为下一步候选；17b / 17c / 17d 暂不进入实施。17a 也必须先完成线程安全改造。**
- 17a 实施必须拆成两个可独立 review / rollback 的 commit：**17a-0 线程安全 refactor（无并行行为变化）**，再做 **17a-1 thread-pool rollout（env kill switch + 行为测试）**。
- 17a 的 rollback 语义必须明确：`AVT_ALIGN_MAX_WORKERS=1` 应直接走旧串行实现，而不是“单 worker 但先 pre-classify”的近似语义。

**Codex review update (2026-05-08, v2 — 整合反馈)**:
- **paid fallback semaphore 必须罩住 rewriter 调用 + TTS `_generate_one` 整段**（不能只罩 TTS）。Rewriter 走 `GeminiTranslator`，并发同实例同样撞付费 LLM 限流和 retry audit race。详见 §3.1 修订段。
- **17a-0 必须包含 `TTSGenerator` / `GeminiRewriter` / `GeminiTranslator` 的 mutable `self.X` audit**。`paid_fallback_max_concurrency=1` 由"成本保守值"升级为**正确性约束**——`TTSGenerator._speaker_voice_cache` / `_active_job_record` / `_job_provider` 是 stateful 且实例复用，未审计前并发调高即数据撕裂。详见 §3.1 新增"TTS / Rewriter 共享状态 audit"子节。
- **`_align_all_serial()` 必须从当前 `align_all` 体逐字搬出**，保留 `[S5] 对齐进度: i/N` / `[S5] 跳过已完成的对齐段 i/N` / `[S5] 对齐缓存已过期，重新处理段 i/N` 三条日志的字符串和触发节奏。`AVT_ALIGN_MAX_WORKERS=1` 只在走旧路径时才是真回滚。
- **现有测试 `tests/test_aligner.py:352` 直接读 `aligner._last_dsp_fit_result`**，17a-0 改 `_dsp_stretch` 返回 `(path, FitResult | None)` 后必须同步改测试。grep 全 repo 确认还有没有其它直接访问点。
- **17b / 17c / 17d 改成"已排除子项"显式标注**（§3.2-§3.4），避免后续 session 误读成排队待做。
- **17d 风险措辞**：从 "cost spike (4× 账单)" 改为 "rate-limit burst + retry 风暴"——chunk 总数不变、账单不变，问题是 burst rate 撞 Gemini 60/min。
- **ROI 数字加前提**：30 分钟视频省 ~5 分钟仅在"首次运行 + cache/keep_original 命中率低"时成立；resume / 大量 keep_original 段时收益接近 0。上线后必须按 `alignment_method` 分布重新算实际收益。
- **docker-compose.yml 必须显式列出 `AVT_ALIGN_MAX_WORKERS` / `AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY`**（即使是 default value），避免 "env 没注入 → 走代码默认 2" 和 "env 写错 → clamp 到 1" 两种情况肉眼分不清。运维需要单一真源。
- **CLAUDE.md 更新延后到 17a 真落地**（不在 plan-only 阶段先改）。

---

## 0. 背景

P2-17 是审计 §9 列出的最后一项 P2，工程量最大、风险最分散。审计推荐了 4 个并行化点：

> Pipeline / alignment 串行 → 并行（吞吐优化，非 P1 安全）：alignment 加 ThreadPoolExecutor；Pipeline audio_separation + 转录并行；S2 Pass 1/2 并行；翻译 chunk 并行

本文档把 4 个并行化点拆分成独立阶段（17a-17d），分别评估 ROI、风险、实施成本，给出推荐执行顺序与每个阶段的详细方案。**单一 commit 完成 4 个子项**会让 review/rollback 变得无法操作 — 拆开是硬性要求。

---

## 1. 现状（concrete file references）

| 子项 | 关键文件 | 当前形态 | 实测耗时（30 分钟视频） |
|---|---|---|---|
| 17a Alignment | `src/services/alignment/aligner.py:168-220` `align_all()` | for-loop 顺序调用 `_align_one`；但 `SegmentAligner` 有共享状态 `_last_dsp_fit_result` / `post_tts_budget_tracker` | ~400s（需用真实样本重测） |
| 17b Pipeline 阶段并行 | `src/pipeline/process.py` | 默认路径是 `audio_preparation` 产出 `speech_audio_path` 后再 AssemblyAI 转录；不是独立输入 | 暂不计 ROI，不能直接并行 |
| 17c S2 Pass 1/2 并行 | `src/services/transcript_reviewer.py` | Pass 2 使用 Pass 1 的 `pass1_lines` 和 `pass1_result["speakers"]` | 暂不计 ROI，不能直接并行 |
| 17d 翻译 chunk 并行 | `src/modules/translation/translator.py:43-52` `translate_lines` | chunk router 当前无 prev/next 依赖；但 provider 实例有 `_retry_report` 等共享状态 | ROI 可能高，但需专项设计 |

整流水线 30 分钟视频耗时（pre-fix）≈ 25-30 分钟。原估算“17a+b+c+d 全做完可省 ~12 分钟”只能作为方向性目标，不能作为当前实施承诺；每个子项上线前必须补真实样本、机器配置、缓存状态和计时口径。

---

## 2. 风险维度通用清单

每个阶段都要按这些维度评估：

1. **付费 API rate-limit burst** — Gemini / MiniMax / CosyVoice / VolcEngine / AssemblyAI 按调用次数计费，但**并行不天然增加调用次数也不天然增加账单**——chunk / segment / pass 总数固定。真实风险是：① burst rate 在 provider 限流窗口内倍增 → 撞 429 / quota exhaust；② provider 失败后 retry 风暴把"省下来的并发时间"全吃回去甚至倒贴；③ 限流触发后 fallback provider 接力会改变成本归属。对 17a 而言这点尤其要关注付费 fallback 路径（rewrite + TTS），所以 paid fallback semaphore 默认 1。
2. **JobRecord race** — `progress_message` / `current_stage` / `error_summary` 同时改 → 即使有 `update_job(mutator)` 也会出现 "B 的 stage 改写盖掉 A 的 stage 改写" 的语义冲突。
3. **共享对象 mutation** — pipeline 内有多个 `DubbingSegment` / `SubtitleLine` 列表，如果一个 worker 修改 segment.X 而另一 worker 读 segment.X，CPython GIL 保字段不撕裂但**逻辑顺序**没法保。
4. **磁盘 IO 上限** — 同时写 N 个 ffmpeg 子进程的 wav 输出，磁盘吞吐瓶颈在 SSD 上是 800 MB/s，HDD 上 ~120 MB/s。4 worker 在 SSD 上不撞瓶颈，HDD 上要测。
5. **错误传播** — 串行 for-loop 里抛 exception 立即停；ThreadPoolExecutor 里某个 future 抛 exception 默认会被 `.result()` re-raise，但其他正在跑的 future 不会被 cancel。需要明确 cancel-on-first-error 的策略。
6. **实例内部状态** — 不只看输入列表是否独立，也要检查 service/provider 实例本身是否有跨调用状态。已知风险：`SegmentAligner._last_dsp_fit_result`、`PostTTSBudgetTracker._usage_by_root`、`TTSGenerator._speaker_voice_cache` / `_active_job_record` / `_job_provider`、`GeminiTranslator._metering_usage_context`（rewrite phase 归因，rewriter try/finally 临时挂在 translator 上）、`OpenAICompatibleTranslationProvider._retry_report`（17d）。
7. **观测与回滚** — 每个阶段必须同时带 metrics 和 kill switch。没有线上耗时、失败率、fallback 付费调用并发峰值、429 / retry 计数，就不能继续放大 worker 数。

---

## 3. 子项详细分析

### 3.1 子项 17a — Alignment 并行（推荐先做）

**位置**: `src/services/alignment/aligner.py:168-220`

**当前代码骨架**:
```python
def align_all(self, segments, output_dir):
    for index, segment in enumerate(segments, start=1):
        if is_keep_original_dubbing_mode(...):  # branch 1: cheap
            results.append(self._keep_original_result(segment))
            continue
        if cache_hit(segment):  # branch 2: cheap
            results.append(...)
            continue
        results.append(self._align_one(segment, output_root))  # branch 3: expensive
```

**`_align_one` 内部**:
- Happy path（~95%）: 纯 ffmpeg DSP 子进程 — 无付费 API。
- 失败回退（~5%）: 调 `rewriter` (Gemini) + `tts_generator._generate_one` (MiniMax/CosyVoice/VolcEngine)，付费 API。
- 当前实现还有两个不能忽略的共享状态：
  - `self._last_dsp_fit_result` 在 `_dsp_stretch()` 写入，在 `_apply_dsp_fit_audit()` / `_last_dsp_fit_was_capped_underflow()` 读取。多个线程共用同一个 `SegmentAligner` 会把 A 段的 fit result 串到 B 段。
  - `post_tts_budget_tracker` 内部是普通 dict，`try_consume_for_segment()` 是 read-modify-write。并行 fallback 时可能超额消费 TTS 预算。

**风险评估**:

| 维度 | 评估 |
|---|---|
| 付费 API 并发 | 仅 fallback 路径触发，但最坏会同时触发多路 Gemini + TTS。必须加独立 semaphore，P1 默认只允许 1 路 paid fallback |
| JobRecord race | 不写 JobRecord — `align_all` 只输出 `AlignedSegment` 列表，由调用方写 |
| 共享对象 mutation | 当前不能判定为低风险。`segment_X` 本身独立，但 `SegmentAligner` 实例状态和 `PostTTSBudgetTracker` 共享 |
| 磁盘 IO | 4 路并行 ffmpeg，每路 ~5-15 MB 输出；SSD 不饱和，HDD 上需测 |
| 错误传播 | `as_completed` 可 re-raise，但 running ffmpeg/provider call 不能真正 cancel。必须接受“本批剩余 running task 可能继续消耗资源”的事实，并用小 worker 数降低影响 |

**推荐方案 17a-v1（修订版）**:

先做线程安全前置改造，再做 thread pool。不要按原稿直接 `pool.submit(self._align_one, ...)`。

必须满足以下条件之一：

1. **首选：消除 `SegmentAligner` 的跨调用状态**
   - 把 `_last_dsp_fit_result` 从 `self` 字段改成 `_dsp_stretch(...) -> tuple[path, FitResult | None]` 或等价局部返回值。
   - `_apply_dsp_fit_audit(segment, fit_result)` 和 `_last_dsp_fit_was_capped_underflow(fit_result)` 显式接收本段 fit result。
   - 这样每个 `_align_one` 的 DSP audit 完全由本段局部变量驱动。

2. **备选：每个 worker 使用独立 `SegmentAligner` 实例**
   - 子实例复制阈值、rewriter、tts_generator 配置。
   - 但 `post_tts_budget_tracker` 仍然需要线程安全或 paid fallback 串行化；否则不能解决预算超额。

同时必须处理 `PostTTSBudgetTracker`：

- 给 `PostTTSBudgetTracker` 加 `threading.Lock`，包住 `root_id_for_segment()` / `register_child_segments()` / `remaining_for_segment()` / `try_consume_for_segment()` 读写路径；或
- 不让 rewrite/TTS fallback 进入并行区：DSP/direct 可并行，任何需要 post-TTS rewrite 的段进入单独串行队列。

如果选择加锁，锁粒度必须写清楚：
- 使用 `threading.RLock`，避免 `register_child_segments()` 内部调用 `root_id_for_segment()` 时自锁。
- `try_consume_for_segment()` 的 read-modify-write 必须在**同一个 critical section** 内完成，不能只给单独 dict 读写加锁。
- 不允许在持有 budget lock 时调用 rewriter、TTS provider、ffmpeg 或任何慢 IO；锁只保护内存账本。

付费 fallback 还要加独立并发阀，且必须罩住 **rewriter 调用 + TTS `_generate_one` 整段**（不能只罩 TTS）：

```python
_ALIGN_MAX_WORKERS = int(os.environ.get("AVT_ALIGN_MAX_WORKERS", "2"))
_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY = int(
    os.environ.get("AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY", "1")
)
```

`_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1` 是**正确性约束**，不只是成本保守值（详见下文 "TTS / Rewriter 共享状态 audit"）。落点必须是 `_attempt_rewrite_loop` 进入循环前 `acquire`、循环退出后 `release`，覆盖：

- [aligner.py:472-479](src/services/alignment/aligner.py:472) `_rewrite_segment_with_constraints`（付费 LLM — Gemini rewrite）
- [aligner.py:489-500](src/services/alignment/aligner.py:489) `tts_generator._generate_one`（付费 TTS — MiniMax / CosyVoice / VolcEngine）
- [aligner.py:545-549](src/services/alignment/aligner.py:545) best-candidate 收尾的 `_dsp_stretch`（不付费但读 `_last_dsp_fit_result`，且与上面两步在 _last_dsp_fit_result 的语义流上耦合）

如果只罩 TTS 一段，rewriter 仍然会被 N 路并发触发——既撞 Gemini 限流又撞 `GeminiTranslator` 的共享状态。两半必须在同一个 critical section 内。

**TTS / Rewriter 共享状态 audit（17a-0 必须做完）**:

paid_fallback semaphore 默认 1 是**正确性约束**，理由：

- **`TTSGenerator` 是 stateful 的**，且整个 pipeline 复用同一个实例（[process.py:2807-2814](src/pipeline/process.py:2807) 构造 `SegmentAligner(tts_generator=tts_generator, ...)`）：
  - `self._speaker_voice_cache` ([tts_generator.py:197](src/services/tts/tts_generator.py:197))：speaker_id → (voice_id, confidence) 缓存，每次 `_generate_one` 内部 auto-match 时读写。alignment 并发触发 N 路 `_generate_one` → cache 写入 race。
  - `self._active_job_record` / `self._job_provider` ([tts_generator.py:264-266](src/services/tts/tts_generator.py:264))：在 `generate_all` 入口设置；但 `_align_one` rewrite 路径**直接调** `_generate_one`（[aligner.py:489](src/services/alignment/aligner.py:489)），跳过 `generate_all`，并发下这两个字段反映的是最后一次 `generate_all` 调用的状态，不能保证和当前 segment 匹配。
  - `TTSGenerator.generate_all` 自身在 segment 数 > 100 时已经启用 3-worker 内部并行（[tts_generator.py:277-284](src/services/tts/tts_generator.py:277)）；alignment 再叠一层会出现"alignment N worker × TTS 3 worker"嵌套并发，触发概率低但出现就难复现。
- **`GeminiRewriter` 本身只读**（[rewriter.py:35-44](src/services/gemini/rewriter.py:35) 的 `self.X` 全部 init 后不再 mutate），但它通过 `self.translator`（`GeminiTranslator`）维护一段**临时共享状态**——`_metering_usage_context`：
  - [rewriter.py:164-175](src/services/gemini/rewriter.py:164) `_call_task_with_usage_phase` 用 try/finally 在 `translator._metering_usage_context` 上做 setattr → 调 `_call_task_with_fallback` → finally 恢复 previous_phase。
  - [translator.py:471-483](src/services/gemini/translator.py:471) `meter.record_llm(..., phase=getattr(self, "_metering_usage_context", "") or "", ...)` 在响应回来时读这个字段写 usage phase。
  - 并发场景：Thread A setattr 进入 rewrite，Thread B 紧接着 setattr 后 capture 到 A 的值作为 previous_phase，A finally restore 把字段拨回空串，B 的 `record_llm` 读到空串——B 的 rewrite 调用被错误归到默认 phase。Phase 归因被打串后，usage_meter 的成本/调用归属表会失真，admin 后台基于 phase 的成本面板会读到错误数据。
  - 结论：`GeminiRewriter` 不是无状态——它把状态借存在 `GeminiTranslator` 实例上；并发同 translator 实例 = phase 归因 race。这条与 17d 里讨论的 `OpenAICompatibleTranslationProvider._retry_report` 是**两条独立的状态泄漏**，不要混淆。

17a-0 commit 必须做的 audit：
1. grep `class TTSGenerator` / `class GeminiRewriter` / `class GeminiTranslator` 所有 `self.X = ` 赋值，列出每条字段的 read/write 时机（init / 每次调用 / 外部 setter）。
2. 对每条 mutable 字段决定一种处理方式：① 改 local return；② 加锁；③ 接受"必须靠 paid_fallback=1 串行"作为唯一保障——并把这条标注在 commit message 里。
3. 把 audit 结论以注释形式写在 `_attempt_rewrite_loop` 的 semaphore acquire 上方，让 review 能直接看到为什么 1 是正确性而非保守。

未来若要把 paid fallback 调到 2+，必须先把上面三条 audit 升级到 ① 或 ② — 不能只调 env。

**并行框架参考（只在完成上述前置后使用）**:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

_ALIGN_MAX_WORKERS = int(os.environ.get("AVT_ALIGN_MAX_WORKERS", "2"))


def align_all(self, segments, output_dir):
    output_root = Path(output_dir).resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True)
    total = len(segments)
    workers = _read_align_max_workers()

    # Hard rollback/debug path. _align_all_serial() MUST be the current
    # align_all body copied verbatim — same `[S5] 对齐进度: i/N` log cadence
    # (every 15 segments + final), same "[S5] 跳过已完成的对齐段 i/N" /
    # "[S5] 对齐缓存已过期，重新处理段 i/N" log strings, same i counter
    # semantics. Any divergence breaks ops dashboards and bisect of the
    # parallel rollout. workers=1 is the only true rollback path.
    if workers <= 1:
        return self._align_all_serial(segments, output_dir)

    # Pre-classify so cheap branches (keep_original / cache_hit) stay
    # synchronous and only the expensive `_align_one` calls go through
    # the thread pool. Classification is itself O(N) but each step is
    # in-memory or a single stat() so it's negligible vs the ffmpeg
    # work it gates.
    cheap_results: dict[int, AlignedSegment] = {}
    needs_align: list[tuple[int, DubbingSegment]] = []
    for idx, segment in enumerate(segments):
        if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
            cheap_results[idx] = self._keep_original_result(segment)
            continue
        output_path = output_root / f"segment_{segment.segment_id:03d}_aligned.wav"
        if is_valid_output(str(output_path)) and self._aligned_cache_is_fresh(segment, output_path):
            cheap_results[idx] = self._build_cached_result(segment, output_path)
            continue
        needs_align.append((idx, segment))

    # Parallel writes make duplicate output paths nondeterministic. The old
    # serial loop would effectively "last write wins"; the parallel rollout
    # must fail fast instead.
    output_paths = [
        output_root / f"segment_{seg.segment_id:03d}_aligned.wav"
        for _idx, seg in needs_align
    ]
    if len({str(path) for path in output_paths}) != len(output_paths):
        raise AlignmentError("duplicate alignment output path; segment_id values must be unique")

    # Parallel DSP/direct work for non-cached segments.
    # Paid rewrite/TTS fallback must be guarded by a separate semaphore
    # inside the worker path, or routed through a serialized fallback path.
    parallel_results: dict[int, AlignedSegment] = {}
    completed = len(cheap_results)
    progress_lock = threading.Lock()
    if needs_align:
        workers = min(workers, len(needs_align))
        stop_event = threading.Event()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="align") as pool:
            future_to_idx = {
                pool.submit(self._align_one_guarded, seg, str(output_root), stop_event): idx
                for idx, seg in needs_align
            }
            try:
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    parallel_results[idx] = future.result()  # re-raises on error
                    with progress_lock:
                        completed += 1
                        if completed % 15 == 0 or completed == total:
                            print(f"[S5] 对齐进度: {completed}/{total} 段")
            except BaseException:
                stop_event.set()
                for pending in future_to_idx:
                    pending.cancel()
                raise

    # Re-assemble in the original segment order so downstream callers
    # can rely on positional alignment with the input list.
    return [
        cheap_results.get(idx) or parallel_results[idx]
        for idx in range(total)
    ]
```

**关键设计点**:
- `AVT_ALIGN_MAX_WORKERS` env 控制并发度，**首次上线默认 2**，观察后再考虑 4；设为 1 作为紧急回滚开关
- `AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY` 独立控制 rewrite/TTS fallback，默认 1
- env 解析必须 fail-safe：非法值、0、负数都 clamp 到 `1`；`AVT_ALIGN_MAX_WORKERS` 初版建议 cap 到 `4`，避免误配 `32` 直接打爆磁盘/CPU/provider。
- `AVT_ALIGN_MAX_WORKERS=1` 必须直接走旧串行实现（建议抽 `_align_all_serial()`），不能只用单 worker pool 模拟。
- 输入 segment 按位置 idx 跟踪 → 输出按 idx 重组，保持顺序。下游对位置 / in-place mutation 的实际依赖（grep `align_all` callers in `src/pipeline/process.py`）：
  - [process.py:2807-2814](src/pipeline/process.py:2807) 主路径接 `align_all` 返回值；line 2854 `sum(... for segment in aligned_segments if segment.needs_review)` 不依赖顺序，但 line 2823/2831 在 repair 后 `_build_aligned_segments(translation_result.segments)` **从输入列表重建** AlignedSegment（[process.py:6588-6609](src/pipeline/process.py:6588)），依赖 `_align_one` 在每个 segment 上的 in-place mutation（[aligner.py:406-411](src/services/alignment/aligner.py:406) 写 `aligned_audio_path` / `actual_duration_ms` / `alignment_method` / `needs_review`）。
  - [process.py:6064](src/pipeline/process.py:6064) 与 [process.py:6224](src/pipeline/process.py:6224)（semantic split repair / retry 路径）**直接丢弃返回值**，完全依赖 `child_segments` 的 in-place mutation 状态。
  - 结论：每个 segment 必须 exactly 一个 worker 处理（plan 已通过 idx 分区保证），返回列表按 input idx 重组以满足主路径"len + needs_review 计数 + 后续 build_result"语义。worker 之间不共享 segment 对象——CPython 写一个 attribute 是原子的，跨段不会撕裂。
- 进入 thread pool 前必须校验待写 output path 唯一；重复 `segment_id` / 重复路径直接 fail fast，避免并行下 nondeterministic 覆盖。
- 进度日志走 `progress_lock` 保线程安全；改成"完成数/总数"语义而非"i/N"（i 在并行里没意义）
- `as_completed` 的 `future.result()` 会 re-raise 首个失败；实现必须设置 `stop_event`、cancel pending futures，并在 worker 进入 paid fallback 前检查 `stop_event`。已经运行中的 ffmpeg/provider call 不能真正取消，所以默认 worker 数和 paid fallback semaphore 必须保守。
- 不承诺并发日志顺序等同旧串行；只承诺输出列表顺序、alignment metadata、费用护栏和 `max_workers=1` 的旧串行路径。

**测试策略**:
1. **现有测试需要同步改的点**：
   - [tests/test_aligner.py:352](tests/test_aligner.py:352) 直接读 `aligner._last_dsp_fit_result`（`test_aligner_caps_extreme_underflow_dsp_and_pads_silence`）。17a-0 把 `_dsp_stretch` 改返回 `(path, FitResult | None)` 后必须把这行改成读返回值。
   - 17a-0 commit 前 `grep -rn "_last_dsp_fit_result" tests/ src/` 全 repo 确认没有别处直接访问该字段。
   - 其余 `tests/test_aligner.py` / `tests/test_alignment.py` 用 mock，不依赖并发结构；17a-0 跑全测试确认无回归。
2. **新增行为测试，不建议用 AST guard 强绑 `ThreadPoolExecutor`**:
   - `test_align_all_uses_thread_pool_for_needs_align`: spy `_align_one` 记录线程名，验证 ≥2 thread 用到（mock 4 段 cache miss）
   - `test_align_all_preserves_order_under_parallel`: 4 段 cache miss + mock `_align_one` 故意延迟（最后一段最先返回）→ 输出顺序仍按 segment_id
   - `test_align_all_avt_max_workers_one_dispatches_to_serial`: env=1 时 `_align_all_serial()` 被调用（spy / mock 断言），且**不创建 ThreadPoolExecutor / 不出现 `align_*` thread name**——避免实现者写成单 worker pool 来近似旧行为。建议同时断言三条 `[S5]` 日志字符串和触发节奏与未引入并行前一致（`capsys`/`capfd`）。
   - `test_align_all_dsp_audit_does_not_cross_segments`: 两个段 mock 不同 `FitResult`，并行完成顺序反转，最终每段 audit 字段必须匹配本段
   - `test_post_tts_budget_tracker_thread_safe`: 多线程同时 `try_consume_for_segment`，最终不能超过 `max_extra_tts_per_root`
   - `test_paid_fallback_concurrency_is_limited`: mock rewrite/TTS fallback 记录并发峰值，默认必须 ≤1
   - `test_align_all_duplicate_output_paths_fail_fast`: 重复 `segment_id` / 重复 output path 在进入 thread pool 前抛 `AlignmentError`
   - `test_align_all_cancel_pending_on_first_error`: 一个 worker 失败后设置 stop flag，pending paid fallback 不再进入 provider
   - `test_align_all_invalid_env_values_fall_back_to_serial`: env 非法、0、负数都走安全串行/保守值
3. **集成 smoke**（手工，部署前）:
   - 实际 30 段视频 alignment，比对 v1 vs v0 的 alignment metadata 和可感知输出；wav SHA256 可作为参考，但 ffmpeg 并发下时间戳/metadata 可能导致 byte-level 不完全稳定，不能只用 SHA256 判定正确性。
   - 记录总耗时、direct/dsp/force_dsp/rewrite 数量、paid fallback 并发峰值、needs_review 数量。

**Rollback 策略**:
- 设 `AVT_ALIGN_MAX_WORKERS=1` → 退化为顺序行为
- 设 `AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1` → 付费 fallback 保持串行
- 主回滚路径依赖 git commit + env kill switch；“备份单文件”只作为运维习惯，不作为正式方案要求

**估算**:
- 实施时间: 2-4 小时（拆成 17a-0 线程安全 refactor + 17a-1 并行 rollout 两个 commit；包含 `_last_dsp_fit_result` 局部化 / budget tracker lock / 新测试）
- Codex review: 至少 1 轮，重点看线程安全和费用护栏
- 部署: src/ bind mount，docker restart aivideotrans-app

**ROI**: 中-高，**有前提**——30 分钟视频在**首次运行 + cache/keep_original 命中率低**时省约 5 分钟；resume 重跑或大量 keep_original 段时收益接近 0，因为 cheap 分支已经被现有路径同步处理，ThreadPool 只覆盖 expensive 分支（`force_dsp` / `dsp` / `rewrite_*`）。上线后必须按 `alignment_method` 分布的 needs_align 段数 × 平均单段耗时重新算实际收益，再决定是否值得把 worker 从 2 调到 4。
**风险**: 中（修完共享状态 + paid fallback semaphore 后可接受；未修前不能实施）

---

### 3.2 已排除：子项 17b — Pipeline audio_separation ‖ transcription

> **状态：🚫 已排除（不进入 P2 排期）**
> **排除原因**：默认 AssemblyAI 路径依赖 `speech_audio_path`（[process.py:1602-1603](src/pipeline/process.py:1602)），两 stage 不独立。
> **重启条件**（两条都满足才能重新评估，缺一不可）：
> 1. 完成 "原始音频 ASR vs speech_audio_path ASR" 的离线质量实验（diarization 准确率、英文字面准确率、后续 S2 修正量），证明用原始音频转录不显著回退。
> 2. Pipeline 状态机重构成 `ingestion` 父 stage + sub-stage 列表，避免 `current_stage` / `progress_message` 单字段被两个并行任务互相覆盖；同时与前端协调（`F-HIGH-1` workspace 轮询逻辑依赖该字段）。
>
> 不要靠"看起来该并行就先做"。下面分析保留作为重启时的起点。

**位置**: `src/pipeline/process.py`（8325 行，需要找具体 stage 入口）

**当前形态（修订）**: pipeline 顺序执行 audio preparation / separation → transcription。原稿写“两个 stage 的输入都是同一个 source video，互不依赖”，这不符合当前默认路径。

当前 AssemblyAI 路径调用的是 `speech_audio_path`：

```python
transcript_result = transcriber.transcribe(
    str(speech_audio_path),
    str(final_project_dir / "transcript"),
    ...
)
```

`speech_audio_path` 来自 `_ensure_separated_audio_assets(...)` / `SourceAudioPreparationService.prepare(...)`。因此默认路径是**先得到 speech audio，再转录**。只有另行证明“用原始音频直接转录质量不回退”之后，才存在并行空间。

**风险评估**:

| 维度 | 评估 |
|---|---|
| 付费 API 并发 | transcription 调 AssemblyAI / Gemini（付费）；若为了并行改成原始音频转录，可能改变 ASR 质量和成本口径 |
| JobRecord race | **高风险**: 两个 stage 都改 `current_stage` 和 `progress_message`。即使用 `update_job(mutator)`，也会出现"audio_sep 把 stage 改成 ingestion，刚 commit；transcription 也想改成 ingestion 但已经是了，没事；但 progress_message 同一时刻被两个 stage 改写"的不一致 |
| 共享对象 mutation | manifest.json 是两 stage 都写的 artifact registry — 如果两 stage 并发 add_artifact，需要 file_lock 保护（已经有，但要测） |
| 磁盘 IO | demucs 模型加载 + ASR 上传同时进行，瓶颈在网络上行（ASR 是云调用） |
| 错误传播 | 一个 stage 失败应当 cancel 另一个？还是等另一个完成再报错？需要 product 决策 |

**推荐方案 17b-v1（修订）**:

**暂不实施。** 不能直接 `gather(audio_separation, transcription)`。

如果未来重新评估，必须先拆成两个子问题：

1. **质量实验**：比较“原始音频 ASR” vs “speech_audio_path ASR”的 speaker diarization、英文转录准确率、后续 S2 修正量。没有 smart_shadow_eval 样本前不改主路。
2. **状态机重构**：引入上层 `ingestion` stage + sub-stage 列表，避免 `current_stage` / `progress_message` 被两个并行任务互相覆盖。

只有当质量实验通过，且前端能正确展示 sub-stage，才考虑并行。

**重构成本**: 高 — pipeline `current_stage` 是 UI 显示的关键字段，改语义会影响前端（`F-HIGH-1` workspace 轮询逻辑就依赖这个字段）。需要先和前端确认。

**ROI**: 未确认（原始估算不成立，因默认转录输入依赖 speech audio）
**风险**: 高（ASR 输入语义变化 + pipeline 状态机重构 + 前端协调）

**推荐**: **暂不做**。先保持串行，最多作为 P3+ 研究项。

---

### 3.3 已排除：子项 17c — S2 Pass 1/2 并行

> **状态：🚫 已排除（不进入 P2 排期）**
> **排除原因**：Pass 2 实参 `lines=pass1_lines` + `speakers=pass1_result["speakers"]`（[transcript_reviewer.py:1004-1006](src/services/transcript_reviewer.py:1004)）来自 Pass 1 应用 corrections + audience guard + speaker verifier 之后的输出，是真实数据依赖，不是顺序惯性。
> **重启条件**（两条都满足才能重新评估）：
> 1. S2 经实测确认是 pipeline 总耗时的关键瓶颈（当前不在 §5 性能详表的 P1 项里）。
> 2. 设计 speculative Pass 2（基于原始 transcript + ASR speaker map 跑），并实现冲突检测——Pass 1 改了 speaker 或 split 后能可靠回退串行 Pass 2，进度展示能区分 "speculative text review / speaker review / merge validation" 三个子状态。
>
> 当前不值得做。下面分析保留作为重启时的起点。

**位置**: `src/services/transcript_reviewer.py:1362-1375` (Pass 1) + `:1655-1668` (Pass 2)

**当前形态**: 三轮拆分（CLAUDE.md 已记录）：
- Pass 1（speaker）：音频+文本，纠 speaker label
- Pass 2（text）：纯文本，纠错字 + glossary
- Pass 3（voice profile）：per-speaker 音频片段 → 音色画像（在翻译审核后才跑）

原稿写“Pass 1 和 Pass 2 互不依赖”，这不符合当前代码。

当前 `_orchestrate_three_pass()` 中：

- Pass 1 输出 `pass1_lines` 和 `pass1_result["speakers"]`。
- Pass 2 调 `_review_pass2_text(lines=pass1_lines, speakers=pass1_result["speakers"], ...)`。

也就是说 Pass 2 的输入依赖 Pass 1 的 speaker 修正结果和 speaker map。直接并行会改变 Pass 2 prompt 上下文。

**风险评估**:

| 维度 | 评估 |
|---|---|
| 付费 API 并发 | Pass 1 + 2 都调 Gemini，并行 = 2× 同时调用，比顺序大略 1× 总成本（每个 Pass 调用次数不变）。Gemini 限流 60/min 的边界要核对 |
| JobRecord race | progress_message "S2 Pass X..." 两个同时改，UI 显示乱跳 |
| 共享对象 mutation | 文件输出隔离不是主要问题；主要问题是 Pass 2 语义依赖 Pass 1 输出 |
| 磁盘 IO | 可忽略（Gemini 是网络调用） |
| 错误传播 | Pass 1 fail → fallback；Pass 2 fail → fallback。两路都有 fallback 链 — 并行后要保证 fallback 仍按"立即降级 + 记日志"语义 |

**推荐方案 17c-v1（修订）**: **暂不实施直接并行**。

如果未来要做，只能走 speculative 方案：

1. Pass 2 先基于原始 transcript + ASR speaker map 做 speculative text review。
2. Pass 1 完成后，合并 speaker corrections 与 Pass 2 text corrections。
3. 增加冲突检测：如果 Pass 1 改了 speaker 或 split 结构影响 Pass 2 correction index，则丢弃 speculative Pass 2，回退串行 Pass 2。

这已经不是 80 行优化，而是新的合并语义和质量风险。当前不值得做。

**前置条件**:
- 必须先确认 Gemini 项目的实际并发限制（admin_settings 里有 quota 配置吗？）
- 如果未来做 speculative 并行，progress_message 不能再写单一路径的 "S2 Pass X..."，需要表达“speculative text review / speaker review / merge validation”三个子状态。

**ROI**: 低-中（S2 占总耗时低；speculative 丢弃会进一步降低收益）
**风险**: 中-高（prompt 语义变化 + correction merge 风险 + LLM 并发）

**推荐**: 暂不做。除非后续数据证明 S2 是主要瓶颈，否则不要动。

---

### 3.4 已排除：子项 17d — 翻译 chunk 并行

> **状态：🚫 已排除（不进入 P2 排期，但保留为最有可能重启的项）**
> **排除原因**：`OpenAICompatibleTranslationProvider._retry_report` ([providers.py:300, :315-321](src/modules/translation/providers.py:300)) 是 stateful，并发同实例会撞 retry audit race；fallback_translator 也走同一限流通道才安全。
> **重启条件**（缺一不可）：
> 1. 17a 上线 1-2 周观察后，确认 alignment 并行没有副作用（线程模型、付费 fallback 触发率、运维体感）。
> 2. 完成 provider 状态隔离专项：`_retry_report` 改为每次调用局部返回值 / provider factory per chunk；`fallback_translator` 走同一 semaphore。
> 3. 落地全局 LLM concurrency semaphore（`AVT_TRANSLATE_MAX_CONCURRENCY`，默认保守 1 或 2），primary + fallback 共用。
>
> 下面分析保留作为重启时的起点。

**位置**: `src/modules/translation/translator.py:43-52` `translate_lines`

**当前代码**:
```python
def translate_lines(self, lines):
    validate_source_lines(lines)
    translated_lines = []
    for chunk in self.router.route(lines):
        raw = self.translator.translate_batch(chunk)
        processed = self.process_batch_output(chunk, raw, sanitize=True)
        translated_lines.extend(self.merge_batch(chunk, processed.texts))
    return translated_lines
```

**风险评估**:

| 维度 | 评估 |
|---|---|
| 付费 API 并发 | **rate-limit burst + retry 风暴风险**：`translator.translate_batch` 是付费 LLM。chunk 总数固定，并行**不增加调用次数也不增加账单**，但 burst rate 上升——Gemini 60/min 看似够，但 4 路并行的 burst window 内会撞 429 → retry 风暴 → 实际耗时反而劣化。问题是限流和 retry 风暴，不是账单 4× |
| JobRecord race | 不直接写 JobRecord（pipeline 上层写）|
| 共享对象 mutation | 每个 chunk 是 `list[SubtitleLine]` 切片，router 当前无 prev/next 依赖；但 provider 实例有共享 `_retry_report` 等状态，不能并发复用同一个 provider 实例 |
| 磁盘 IO | 不写磁盘 |
| 错误传播 | 一个 chunk fail 时是否影响其他 chunk？fallback_translator 的语义需要重新审视 |

**额外难点 — chunk 间上下文**:
- 翻译 chunking 通常会带 `previous_chunk_summary` 之类的上下文跨 chunk 传递（避免相邻 chunk 翻译风格漂移）
- 并行后这个上下文链就断了 — 需要看 TranslationChunkRouter 是否真的有这个依赖
- 当前 `TranslationChunkRouter` 只是按行数/字符数切 batch，没有显式 prev/next 依赖；但 `OpenAICompatibleTranslationProvider` 会维护 `_retry_report`，并行调用同一 provider 实例会导致 retry audit 串扰。

**推荐方案 17d-v1（修订）**:
1. 先核 chunk 之间是否有 prev/next 依赖（grep `previous` / `context` / `prior_chunk` in router）
2. 再核 provider 是否 stateless；若不是，必须改成 **provider factory per chunk** 或把 `_retry_report` 改成每次调用局部返回值。
3. 加全局 LLM semaphore，例如 `AVT_TRANSLATE_MAX_CONCURRENCY=2`，并对 real provider 默认保守值 1 或 2。
4. fallback translator 也必须走同一个 semaphore，不能 primary 限流、fallback 放飞。
5. 如果有 chunk 间上下文依赖：放弃 17d，或改用 provider 原生 batch API。

**ROI**: 高（5 分钟省到 1 分钟，长视频用户最有感）
**风险**: 高（付费 API 并发 + 可能的上下文依赖 + 限流）

**推荐**: 延后，需要单独的“翻译流水线 cost spike 防御 + provider state isolation”专项。

---

## 4. 推荐执行顺序

```
17a safety refactor  ──→  17a parallel rollout  ──→  观察 1-2 周  ──→  重新评估 17d
```

**理由**:
1. 17a 仍然是唯一适合先做的候选，但必须拆成“线程安全前置改造”和“并行 rollout”两步。
2. 17a 上线后观察实际效果：alignment 时间下降多少、fallback 路径是否触发、付费 fallback 并发峰值是多少。
3. 17d ROI 可能高，但必须单独做 provider 状态隔离和 LLM 并发阀。
4. 17b / 17c 当前独立性假设不成立，暂不排期。

---

## 5. 17a 详细实施 checklist（执行前必看）

如果决定做 17a，按这个顺序：

**17a-0 — 线程安全 refactor + 状态 audit（无并行行为变化）**

- [ ] 1. 先写 characterization tests：当前串行输出顺序、DSP audit 字段、post-TTS budget 消费上限、rewrite/TTS 调用顺序与次数。
- [ ] 2. **`TTSGenerator` / `GeminiRewriter` / `GeminiTranslator` 状态 audit**：grep 三个类所有 `self.X = ` 赋值，列出每条字段的 read/write 时机；产出审计表（建议存 `docs/audits/`）。对每条 mutable 字段决定 ① local return / ② 加锁 / ③ 接受 paid_fallback=1 串行作为唯一保障。结果以注释形式贴在 §3.1 semaphore acquire 处。
- [ ] 3. 把 `_last_dsp_fit_result` 从 `self` 状态改为 per-segment 局部值（首选）或改为 worker-local aligner；`_dsp_stretch` 改返回 `(path, FitResult | None)`，`_apply_dsp_fit_audit(segment, fit_result)` / `_last_dsp_fit_was_capped_underflow(fit_result)` 显式接收。
- [ ] 4. **同步改测试访问点**：`grep -rn "_last_dsp_fit_result" tests/ src/`，已知 [tests/test_aligner.py:352](tests/test_aligner.py:352) 必须改成读 `_dsp_stretch` 返回值；其他若有命中按相同方式改。
- [ ] 5. 让 `PostTTSBudgetTracker` 线程安全：`threading.RLock` 包 `root_id_for_segment` / `register_child_segments` / `remaining_for_segment` / `try_consume_for_segment`；`try_consume_for_segment()` 的 read-modify-write 必须同锁保护；持锁期间不调用慢 IO / rewriter / TTS provider。
- [ ] 6. 跑 `tests/test_aligner.py` + `tests/test_alignment.py` + `tests/test_pipeline_resume_from_alignment.py` 全过；行为零变化。commit 17a-0；Codex review 一轮，重点看 audit 完整性 + 线程安全 refactor 是否有语义漂移。

**17a-1 — thread-pool rollout（env kill switch + 行为测试）**

- [ ] 7. 抽 `_align_all_serial()`：把当前 `align_all` 体逐字搬出，保留 `[S5] 对齐进度: i/N` / `[S5] 跳过已完成的对齐段 i/N` / `[S5] 对齐缓存已过期，重新处理段 i/N` 三条日志的字符串和触发节奏。`AVT_ALIGN_MAX_WORKERS=1` 直接走这个函数。
- [ ] 8. 添加 env：`AVT_ALIGN_MAX_WORKERS` 默认 `2`、非法值/0/负数 fallback 到 `1`、初版 cap 到 `4`；`AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY` 默认 `1`（正确性约束）。**两个 env 必须在 docker-compose.yml `app` service 显式列出**（即便是 default value），运维需要单一真源。
- [ ] 9. 改 `align_all` 实现（按 §3.1 修订方案）：cheap / cache 分支同步，expensive 分支进入线程池；进入线程池前校验 output path 唯一（duplicate `segment_id` / 重复路径 fail fast）。
- [ ] 10. 实现 paid fallback semaphore：罩住 `_attempt_rewrite_loop` 的 rewriter 调用 + `tts_generator._generate_one` + best-candidate 收尾的 `_dsp_stretch` 整段，**不能只罩 TTS**。worker 进入 critical section 前检查 stop_event；首错 cancel pending future。
- [ ] 11. 写行为测试（不强绑 ThreadPoolExecutor 的 AST guard）：
    - 线程实际使用 / 段顺序保持 / `AVT_ALIGN_MAX_WORKERS=1` 走 `_align_all_serial`
    - DSP audit 不串段 / `PostTTSBudgetTracker` 不超额
    - paid fallback 并发受限（rewrite + TTS 整段）/ 重复 output path fail-fast / 首错 cancel pending / env 非法值保守 fallback
    - 串行旧路径的三条 `[S5]` 日志字符串和触发频率与 17a-0 之前一致（用 capsys / capfd 断言）
- [ ] 12. 跑 `tests/test_aligner.py` + `tests/test_alignment.py` + `tests/test_pipeline_resume_from_alignment.py` + `tests/test_process_pipeline.py` 全过。
- [ ] 13. commit 17a-1；Codex review 一轮，重点看：semaphore 覆盖范围、`_align_all_serial` 与原 `align_all` 行为等价、max_workers=1 旧串行回滚语义、重复 output path、首错取消策略。
- [ ] 14. 部署：`docker cp` 更新代码 + 改 env 后用 `docker compose up -d --force-recreate aivideotrans-app`（`docker restart` 不重读 env_file）。
- [ ] 15. 部署后 smoke：选真实首次运行任务跑 alignment，记录 `[S5] 对齐进度`、总耗时、`alignment_method` 分布（needs_align 段数 vs cheap 段数）、paid fallback 并发峰值。按实际分布重新计算 ROI。

---

## 6. 通用回滚预案

**任何阶段（17a-d）都必须有 env-var kill switch**：

| 阶段 | env var | 设 = 1 时退化为 |
|---|---|---|
| 17a | `AVT_ALIGN_MAX_WORKERS` | `1` 即顺序 |
| 17a paid fallback | `AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY` | `1` 付费 fallback 串行 |
| 17b | `AVT_PIPELINE_PARALLEL_INGESTION` | `0` 关并行 |
| 17c | `AVT_S2_PARALLEL_PASSES` | `0` 关并行 |
| 17d | `AVT_TRANSLATE_MAX_CONCURRENCY` | `1` 即顺序 |

**应急流程**：
1. SSH 到 US，`docker exec aivideotrans-app env | grep AVT_` 看当前值
2. 改 docker-compose.yml 把 env 加进去
3. `docker compose up -d --force-recreate aivideotrans-app`（注意：`docker restart` 不重读 env_file，必须 force-recreate）

---

## 7. 决策点（v2 已确认）

| # | 决策 | 状态 |
|---|---|---|
| 1 | 现在只做 17a，17b/17c/17d 标"已排除子项" | ✅ 已确认（v2） |
| 2 | `AVT_ALIGN_MAX_WORKERS` 默认 `2`，clamp 到 `[1, 4]`，1-2 周后再评估 4 | ✅ 已确认（v2） |
| 3 | 用 per-segment local fit result（`_dsp_stretch` 改返回 tuple），不要靠 `self._last_dsp_fit_result` 加锁 | ✅ 已确认（v2） |
| 4 | `AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY` 默认 `1`，且这是**正确性约束**（依赖 §3.1 audit），不是成本保守值 | ✅ 已确认（v2） |
| 5 | 17a-0 包含 `TTSGenerator` / `GeminiRewriter` / `GeminiTranslator` mutable `self.X` audit | ✅ 已确认（v2） |
| 6 | docker-compose.yml `app` service 必须显式列出两个 alignment env（即便是 default） | ✅ 已确认（v2） |
| 7 | paid fallback semaphore 必须覆盖 rewriter + TTS `_generate_one` 整段，不能只罩 TTS | ✅ 已确认（v2） |
| 8 | `_align_all_serial()` 必须从当前 `align_all` 体逐字搬出（含三条 `[S5]` 日志） | ✅ 已确认（v2） |
| 9 | CLAUDE.md 更新延后到 17a 真落地（不在 plan-only 阶段先改） | ✅ 已确认（v2） |
| 10 | 17d 是否开专项 | ⏸ 延后——见 §3.4 重启条件；当前不排期 |

执行前最后一步：审核人确认 §3.1 修订方案 + §5 checklist 内容，然后即可进入 17a-0 实施。

---

## 8. References

- 审计报告: `docs/audits/2026-05-07-comprehensive-codebase-audit.md` §9 P2-17、§5 性能详表
- Alignment 源: `src/services/alignment/aligner.py:168-220` (align_all) + 269-940 (_align_one + helpers)
- Pipeline 源: `src/pipeline/process.py` (8325 行)
- S2 Reviewer: `src/services/transcript_reviewer.py` `_orchestrate_three_pass()` 中 Pass 1 → Pass 2 依赖链
- 翻译: `src/modules/translation/translator.py:43-52`，provider 状态见 `src/modules/translation/providers.py`
- 已落地的相关并发原语: `src/services/_file_lock.py` (file_lock for cross-process), `gateway/risk_control.py:reserve_voice_probe` (P2-23 reserve/refund pattern)

---

**End of plan.** 不要在审核通过前动代码。

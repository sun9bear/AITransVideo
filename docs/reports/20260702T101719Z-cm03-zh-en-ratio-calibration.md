# CM-03 zh->en `natural_length_ratio` calibration report

- Generated: 20260702T101719Z
- Corpus: `/tmp/cm03_corpus`
- Effective translate route (same llm_registry routing as production Studio zh->en, admin overrides included): `gemini_31_flash_lite` (api_model_id=`gemini-3.1-flash-lite`, provider=`gemini`, fallbacks=['mimo_omni'])
- Measurement entry: `GeminiTranslator.translate_probe` — **constraint-neutralized**: the probe prompt carries no target_chars/min_chars/max_chars (which in the regular translate() path are derived from the very 0.55 prior under calibration and injected as hard constraints). The measured ratio below is the NATURAL unconstrained length ratio (v3 plan semantics); production output is additionally clamped to +/-15% of the ratio by design.
- Clips: 3 (0 failed)

## Pooled ratio distribution (target word count / source CJK char count)

| n | p10 | p25 | p50 | p75 | p90 | mean |
|---|---|---|---|---|---|---|
| 3 | 0.364 | 0.461 | 0.622 | 0.659 | 0.681 | 0.539 |

## Per-clip

| clip | segments | source CJK chars | target words | ratio | error |
|---|---|---|---|---|---|
| clip1_caodewang.txt | 1 | 127 | 79 | 0.622 |  |
| clip2_daihuo.txt | 2 | 866 | 259 | 0.299 |  |
| clip3_tugaiqi.txt | 1 | 69 | 48 | 0.696 |  |

## Conclusion

**update_ratio**

Pooled p50 (0.62) deviates from provisional 0.55 by 13% (> 10% threshold) -- recommend UPDATING natural_length_ratio to 0.62. This affects two downstream consumers: (1) translator.py length budget (_estimate_dynamic_target_chars / _count_cn_chars retry gate) and (2) process.py voice-speed cps metadata (target_chars_per_second).

### Impact on downstream consumers if the ratio changes

1. Length budget: `services/gemini/translator.py` `_estimate_dynamic_target_chars` (5 call sites) and the `_count_cn_chars` retry gate consume `natural_length_ratio` to size the translation length budget per segment.
2. Voice-speed cps: `src/pipeline/process.py` derives `target_chars_per_second` (DubbingSegment) from the source words/second times the ratio; zh->en currently ships with the speed dimension explicitly DISABLED (plan Phase 4 point 2), so this consumer is dormant until that is revisited.


---

## Phase B 决策附录（主模型合成，推翻上方脚本机械结论；owner 授权跑批 2026-07-02）

**决策：维持 natural_length_ratio = 0.55（有数据支撑的维持结论）。**

上方 `update_ratio → 0.62` 是脚本按「无权 p50 与先验偏差 >10%」的机械规则得出，
在 n=3、语域高度分化的语料上不成立：

| clip | 源语速(字/秒) | probe 无约束 ratio | 生产实际 ratio | 首轮 TTS 时长偏差 |
|---|---|---|---|---|
| 曹德旺（慢/演讲） | 3.7 | 0.622 | 0.630 | ≈0% |
| 带货（快/口播） | 6.0 | 0.299 | 0.437 | **+26%（超时）** |
| 涂改器（短/干脆） | 5.2 | 0.696 | 0.580 | +5.7% |

1. **加权口径矛盾**：按源字数加权的 pooled ratio = 386/1062 ≈ **0.36**（clip2 占语料
   82% 质量）；无权中位数 0.62 由两个 <130 字的小 clip 主导。两个口径打架 → 单点更新
   缺乏统计基础。
2. **语域依赖是主效应**：慢语速内容自然 ratio ≈0.62-0.63，快语速口播即便压到
   0.44 词/字、TTS 仍首轮超时 26%——真实约束是**源语速**而非统一字数比。
   0.55 恰好落在观测区间（0.30-0.70）的质量加权中带，是跨语域折中先验的合理取值。
3. **上调 0.62 的实害**：会放宽快语速内容的长度预算 → 超时/DSP 提速压力加剧
   （clip2 类内容首当其冲）；下调 0.36 则压短慢语速内容。维持 0.55 + 既有
   rewrite/DSP 弹性链路是当前最优。
4. **后续方向（backlog，非本单元）**：用管线已有的源语速（chars/sec，来自
   transcript 时间戳）动态调制每段长度预算，替代静态常数；语料扩充（≥10 clip、
   覆盖语域×语速矩阵）后可重跑本脚本复核。

对应代码动作：`language_registry.py` zh-CN->en 注释由 provisional 改为
measured-and-kept（引用本报告）；`tests/test_language_registry.py` 断言改名去
provisional 语义。零行为变更。

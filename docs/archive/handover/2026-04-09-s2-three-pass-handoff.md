# Session Handoff: S2 三轮拆分实施

> 写给下一个 Claude Code 会话的交接文档
> 日期：2026-04-09

---

## 1. 任务

按 `docs/plans/2026-04-08-s2-three-pass-split-plan.md` 方案实施 S2 审校三轮拆分。

## 2. 核心要求

- `review_transcript()` 对外签名和 `ReviewResult` 输出不变
- 拆分只在内部编排层
- 严格 Contract：每轮只允许特定字段，越界丢弃
- Pass 1/2 失败 → fallback 到 legacy 单次路径
- Pass 3 失败 → 不回滚 Pass 1/2

## 3. 必读文件

### 方案文档
| 文件 | 读什么 |
|------|--------|
| `docs/plans/2026-04-08-s2-three-pass-split-plan.md` | **完整方案**：3 轮定义、contract、fallback、产物、prompt 草案、测试要求 |
| `docs/analysis/2026-04-08-s2-review-data-chain-analysis.md` | 当前 S2 数据链全景：prompt 内容、JSON 处理链、数据下游影响 |
| `docs/issues/2026-04-08-s2-speaker-misassignment-investigation.md` | speaker 误分配 bug 排查，说明为什么要拆分 |

### 核心代码
| 文件 | 读什么 |
|------|--------|
| `src/services/transcript_reviewer.py` | **重点**：当前 `review_transcript()` 全流程、`_call_review()`、`_apply_corrections()`、`_apply_interview_sanity_check()`、prompt 模板、debug artifact 写入 |
| `src/pipeline/process.py` 710-810 行 | S2 调用入口、reviewer 结果消费（speaker names、glossary、styles 注入） |
| `src/pipeline/process.py` 1260-1280 行 | Pass 3 插入点：翻译审核后、音色选择前，voice_description 注入 segments |

### 依赖
| 文件 | 读什么 |
|------|--------|
| `src/services/assemblyai/transcriber.py` | TranscriptLine dataclass、ASR speaker mapping |
| `src/services/gemini/translator.py` | DubbingSegment dataclass（Pass 3 输出写入这里） |
| `src/services/gemini/client_factory.py` | Gemini API 调用方式 |

## 4. 实施步骤

| 步骤 | 内容 | 测试 |
|------|------|------|
| 1 | 将当前 `review_transcript()` 核心逻辑收为 `legacy_review_transcript_single_pass()` | 确保 legacy 路径独立可调用 |
| 2 | 写 Pass 1 prompt + `_review_pass1_speakers()` + contract 过滤（只保留 correct_speaker） | Pass 1 不产出 fix_text/merge/split |
| 3 | 写 Pass 2 prompt + `_review_pass2_text()` + contract 过滤（只保留 fix_text/split/glossary） | Pass 2 不产出 correct_speaker/merge |
| 4 | 新的 `review_transcript()` 编排 Pass1→Pass2 + fallback 到 legacy | 失败时 fallback 正常 |
| 5 | 写 Pass 3 prompt + `review_pass3_voice_profiles()` + 音频提取逻辑 | Pass 3 只产出 profiles |
| 6 | `process.py` 在翻译审核后插入 Pass 3 调用 | 整流程跑通 |
| 7 | 产物写入（4 份 JSON：pass1/pass2/pass3/聚合 review_result） | 产物完整 |
| 8 | 测试验证 | 见方案第 13 节 |

## 5. 关键注意事项

1. **Prompt 草案**在方案文档附录里，直接用
2. **merge 操作已被禁止**（跨 speaker 兜底 + Pass 2 prompt 不输出 merge）
3. **`_apply_interview_sanity_check` 保留**不删除，作为 safety net
4. **Pass 3 位置**在翻译审核之后、音色确认之前（`process.py` 1260 行附近）
5. **音频提取**：每个 speaker 取最长连续 utterance 15-30s，ffmpeg 提取+压缩
6. **不需要改前端**
7. **不需要改 Gateway**
8. **不需要改 review_state 阶段定义**

## 6. 当前代码状态

以下改动已在本轮完成，是前置依赖：

- `s2_review_result.json` + `s2_review_audit.json` 已落地
- merge 跨 speaker 兜底已加
- speaker_c+ 姓名全链保留已修
- Prompt "不确定时不要改 speaker" 约束已加
- `_apply_interview_sanity_check` 已改为检查 transcript 实际 speaker 数量

## 7. 测试命令

```bash
# 基础回归
pytest tests/test_pipeline_speaker_fallback.py -q
pytest tests/test_voice_selection_payload.py -q

# S2 相关
pytest tests/test_transcript_reviewer.py -q  # 如果存在

# Pipeline 回归（部分用例慢）
pytest tests/test_process_pipeline.py -q
```

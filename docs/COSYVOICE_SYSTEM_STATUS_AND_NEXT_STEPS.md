# CosyVoice 系统状态总览与下一步建议

> 截至 2026-03-31，Phase A + Phase B 任务线完成后的系统快照。

## 一、已完成能力

### Phase A：`--job-id` 传播链修复

| 能力 | 状态 | 审核节点 |
|------|------|---------|
| Gateway → Pipeline job snapshot 传播 | ✓ 生产验证 | 节点 2 |
| INV-1: free/express → cosyvoice | ✓ | 节点 2 |
| INV-2: paid/studio → minimax | ✓ | 节点 2 |
| INV-3: per-job snapshot 优先 | ✓ | 节点 2 |

### Phase B1：Baseline Voice Matcher

| 能力 | 状态 | 审核节点 |
|------|------|---------|
| Voice catalog: gender + matchable 字段（68 voices） | ✓ | 节点 4 |
| `select_voice_match()` → VoiceMatchResult（score/confidence/backup） | ✓ | 节点 4 |
| Childlike 推导（`infer_is_childlike` heuristic） | ✓ | 节点 4 |
| `selected_voice` / `match_confidence` 持久化到 segments.json | ✓ | 节点 4 |
| `cosyvoice_instruction_enhancer.py` wrapper（instruction gated off） | ✓ | 节点 4 |
| Endpoint-safe voice pool（intl 模式排除 418 voice） | ✓ | 节点 11 |
| 远程验证：adult male / female / childlike 三类样例 | ✓ | 节点 5 |

### Phase B2：Offline Voice Profiling

| 能力 | 状态 | 审核节点 |
|------|------|---------|
| Voice profile schema（primary 4 维 + secondary 6 维） | ✓ | 节点 6 |
| Calibration sample builder | ✓ | 节点 6 |
| Gemini multimodal profiler（双样本输入） | ✓ | 节点 7 |
| 5-voice pilot profiling（gemini-3.1-pro-preview） | ✓ | 节点 7 |
| A/B model 对照（2.5-flash vs 3.1-pro） | ✓ | 节点 8 |
| 10-voice 全量 profiling（intl 可用子集） | ✓ | 节点 9 |
| 27-voice 双端点可用性审计 | ✓ | 节点 10 |

### 端点切换设置

| 能力 | 状态 | 审核节点 |
|------|------|---------|
| `cosyvoice_endpoint_config.py`（runtime/offline 分离） | ✓ | 节点 11 |
| Provider → helper `endpoint_mode` 透传 | ✓ | 节点 11 |
| 双 key 自动选择（intl/mainland/fallback） | ✓ | 节点 13 |
| Gateway AdminSettings 扩展 + Pydantic 验证 | ✓ | 节点 12 |
| Next 管理后台 CosyVoice 端点配置区块 | ✓ | 节点 12 |
| 远程 API 保存/读取/落盘验证 | ✓ | 节点 14 |
| 上线与回滚文档 | ✓ | — |

## 二、当前系统数据

| 指标 | 值 |
|------|-----|
| Catalog 总 voice 数 | 68 |
| Matchable voice 数 | 59 |
| Intl 端点可用 voice 数 | 10 |
| Mainland 端点可用 voice 数 | 59 |
| B1 硬编码 map 覆盖 voice 数 | 12 |
| B2 已 profiled voice 数 | 10（intl 子集） |
| B2 高可用维度 | pitch_level, childlike, maturity, texture_tags |
| B2 低权重维度（趋同） | warmth, authority, intimacy, delivery_style |
| Instruction 状态 | gated off（`INSTRUCT_ENABLED=False`） |
| 默认 runtime endpoint | international |
| 默认 offline endpoint | mainland |

## 三、未完成 / 未接入生产的能力

| 能力 | 当前状态 | 差什么 |
|------|---------|--------|
| B2 profile rerank | 离线数据已有，代码未接入 | `_rerank_with_profiles()` 未写 |
| 27-voice 全量 profiling | 只完成 10 个（intl 可用子集） | 需要在 mainland 端点补剩余 17 个 |
| Instruction 增强 | gated off | DashScope v3-flash 不支持，等端点/模型更新 |
| Warm female 远程真实样例 | 未完成（Gemini 503） | 等 Gemini reviewer 稳定后补一次 |
| 自然 childlike 远程样例 | 受控验证通过，未有自然输入 | 等有真实儿童节目视频输入时自然验证 |

---

## 四、下一阶段最值得做的 3 件事

### 1. 在 mainland 端点完成剩余 17 voice 的 B2 profiling

**价值**：当前只 profile 了 intl 可用的 10 个 voice。剩余 17 个包含客服女（longyingjing_v3）、新闻播报（longshuo_v3/loongbella_v3）、有声书（longsanshu_v3/longmiao_v3）等高价值音色。完成后 B2 rerank 才有足够数据覆盖面。

**成本**：极低。工具链已就绪，offline=mainland + dual key 已验证。预计 1 次 sample builder 运行 + 1 次 Gemini profiler 运行（~30 min）。

**前置条件**：无。

**建议执行方式**：
```bash
docker exec aivideotrans-app python3 scripts/b2_calibration_sample_builder.py \
  --output-dir /opt/aivideotrans/data/b2_calibration_samples \
  --endpoint-mode mainland
# 然后运行 profiler
```

### 2. 实现 `_rerank_with_profiles()` 并接入生产 selector

**价值**：这是 B2 的最终产出——利用离线 profile 数据在 B1 匹配 confidence 为 low/medium 时做 tie-break rerank。当前 B1 对同 gender+age 的多个候选 voice 无法区分，rerank 可以用 pitch_level + texture_tags + maturity 做更精细的匹配。

**成本**：中等。需要在 `cosyvoice_voice_selector.py` 中新增 ~50 行 rerank 函数 + 加载 profile JSON + 测试。

**前置条件**：第 1 项（全量 profiling）完成后效果更好，但仅用当前 10 voice 数据也可以先上线一个 MVP。

**建议限定范围**：
- 只使用高可用维度（pitch_level, texture_tags, maturity, childlike）
- warmth/authority/intimacy/delivery_style 暂不纳入权重
- 对无 profile 的 voice 自动跳过 rerank，不降级

### 3. 评估是否将 runtime 默认切换为 mainland

**价值**：当前 runtime=international 只能用 10 个 voice，selector 对 `longyingjing_v3`（female_middle 默认）等高频 voice 必须做 endpoint fallback。如果切到 mainland，59 个 voice 全部可用，B1 map 和 B2 rerank 可以发挥完整能力。

**成本/风险**：
- 延迟从 1-2s 增至 3-7s（SG→大陆）
- 数据路径变更可能需要合规评估
- 需要验证长视频场景下累计延迟是否可接受

**建议评估方式**：
1. 用一个 10+ 段的 express job 做 mainland runtime 对照，测量总 TTS 时间差
2. 如果延迟差在可接受范围（<2x），考虑切默认
3. 如果不可接受，保持 intl 并观察 DashScope 是否后续在 intl 上线更多 voice

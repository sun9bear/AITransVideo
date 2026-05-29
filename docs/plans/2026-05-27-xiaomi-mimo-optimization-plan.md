# 小米 MiMo V2.5 调价后的项目优化方案

**日期**：2026-05-27  
**状态**：`IN_PROGRESS` —— Phase 0a（spike）✅ / Phase 0b（`mimo_omni` 强制迁移）✅ / Phase 1（MiMo 成本目录）✅ **均已部署美国主机并验证**；Phase 2（usage 采集）已被 spike 解锁，待实现；Phase 3+ 待做  
**范围**：MiMo LLM / MiMo TTS / OpenAI-compatible usage 计量 / `mimo_omni` 强制迁移（deadline 2026-06-30）/ Smart、Studio、Express 模型选择策略  
**原则**：先做 response spike 和成本观测，再做 shadow / 试点；不因为价格下降直接替换跨 provider 主链路默认模型。`mimo-v2-omni` 已确认 2026-06-30 下线，属可用性问题须强制迁移；`mimo-v2-tts` **无**官方下线公告，按主动升级处理，不混为同一事实。

## 0. TL;DR

小米 MiMo-V2.5 系列在 2026-05-27 00:00 起大幅降价，按量 API 价格已经进入可以认真评估的区间。**同时官方已公告 `mimo-v2-pro` / `mimo-v2-omni` 退役**：2026-06-01 00:00 自动转发到 V2.5，2026-06-30 00:00 原模型名正式下线。本项目的 `mimo_omni` 逻辑模型据此必须强制迁移。

⚠️ **三件事必须分清，不能混成同一个“V2 停用”事实**：

- **`mimo-v2-omni` 强制迁移（availability-critical，有官方 deadline）**：2026-06-30 后 `mimo-v2-omni` 名失效，项目内 `mimo_omni` 路径必须迁到 `mimo-v2.5`。这是“不迁就会坏”。详见 Phase 0b。
- **`mimo-v2-tts` 主动升级（无官方下线公告）**：截至目前官方 TTS 文档仍只支持 `mimo-v2-tts`，**没有**宣布 TTS V2 停用。因此升级到 `mimo-v2.5-tts` 是“主动升级 / 风险前置”，不是强制迁移，没有 deadline，且可回退 `mimo-v2-tts`。详见 Phase 3。
- **是否让 MiMo 成为跨 provider 默认（替代 Gemini / MiniMax）**：结论**不变**，仍走谨慎 shadow / 试点。停用 omni ≠ 必须把 MiMo 抬成主模型。

价格利好层面，对本项目的主要价值不是“立刻全量替换 Gemini / MiniMax”，而是：

1. **低成本文本与多模态审核**：`mimo-v2.5` 可作为 S2 Pass1/Pass3、翻译、rewrite、智能切点的低成本候选，但要先 shadow。
2. **成本目录需要补齐**：当前 `gateway/cost_management.py` 没有 `mimo:*` 费率，admin 成本页不能正确估算 MiMo 成本。
3. **usage 采集要按 OpenAI-compatible 统一处理**：MiMo、DeepSeek、OpenAI HTTP 路径都有“只返回正文、丢弃 usage”的问题；MiMo 降价是触发点，但实现不应只修 MiMo。
4. **Phase 1 不动 `cost_rank`**：`cost_rank` 会改变 fallback 链，不能混在“价格目录更新”里；rank 调整必须进入试点 PR，并先看 fallback diff。
5. **TTS 是主动升级不是强制迁移**：官方未宣布 `mimo-v2-tts` 停用，升级到 `mimo-v2.5-tts` 是主动升级（Phase 3），smoke 不过可回退 v2-tts；限时免费不能当作长期商业事实。
6. **Smart 不直接切默认**：Smart 当前产品合同硬依赖 MiniMax clone/quota 边界；MiMo 可以先做 LLM shadow / 可选模型，不直接改 Smart 的 TTS 主链路。

## 1. 外部事实

### 1.1 官方调价

官方公告显示，MiMo-V2.5 系列从北京时间 2026-05-27 00:00 起更新 API 价格，核心变化：

- 最高降幅约 99%。
- 不再按上下文窗口长度分档。
- 按量 API 与 Token Plan 都有调整。
- `mimo-v2.5-tts`、`mimo-v2.5-voiceclone`、`mimo-v2.5-voicedesign` 当前仍显示为限时免费。

按量 API 新价格：

| 模型 | 缓存命中输入 | 缓存未命中输入 | 输出 |
| --- | ---: | ---: | ---: |
| `mimo-v2.5` | RMB 0.02 / 1M tokens | RMB 1.00 / 1M tokens | RMB 2.00 / 1M tokens |
| `mimo-v2.5-pro` | RMB 0.025 / 1M tokens | RMB 3.00 / 1M tokens | RMB 6.00 / 1M tokens |

参考：

- https://platform.xiaomimimo.com/docs/zh-CN/news/v2.5-price-update
- https://platform.xiaomimimo.com/docs/zh-CN/price/pay-as-you-go
- https://platform.xiaomimimo.com/docs/zh-CN/quick-start/model

### 1.2 Token Plan 边界

本项目是面向用户的视频翻译后端工作流，不应把 Token Plan 当作生产成本真源。除非后续明确确认官方条款允许这类服务端转售/代调用场景，否则：

- 成本目录以按量 API 价格为准。
- Token Plan 只能作为人工研发、benchmark 或开发者工具侧参考。
- admin 成本页、毛利分析、商业化定价不能引用 Token Plan 折算价作为主事实。

合规确认落点：

- owner：产品/商务负责人，不由工程默认判断。
- 输出文件：`docs/legal/mimo-tokenplan-eligibility.md`
- 在该文件确认前，Token Plan 不得进入 Gateway pricing、entitlements、credits 或 admin cost catalog 主路径。

### 1.3 V2 停用 / 升级（区分 omni 与 tts，不可混为一谈）

**已确认停用（强制迁移）—— `mimo-v2-pro` / `mimo-v2-omni`：**

- 来源：官方[模型发布页](https://platform.xiaomimimo.com/docs/zh-CN/updates/model)。
- 时间线：**2026-06-01 00:00（北京时间）自动转发到 V2.5；2026-06-30 00:00 原模型名正式下线。**
- 影响本项目：`src/services/llm_registry.py` 的 `mimo_omni -> mimo-v2-omni` 在 6-30 后失效。受影响的运行时硬编码点：
  - `src/services/llm_registry.py` 中 `mimo_omni -> mimo-v2-omni`（选中即坏）。
  - `transcript_reviewer._call_mimo_omni_raw` 内部默认 `_resolve_model_id("mimo_omni")`（line 2566；仅 `model_id` 传空时触发，但默认值是 V2）。
  - `transcript_reviewer._call_review_mimo_omni` metering 回退到 `"mimo_omni"`（line 2644）。
- 结论：omni 迁移是 availability 问题，deadline = 2026-06-30，优先级高于成本目录 / 试点。详见 Phase 0b。

**未确认停用（主动升级，非强制）—— `mimo-v2-tts`：**

- 来源：官方[TTS V2 文档](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis)今天仍写“当前仅支持 `mimo-v2-tts`”，并有完整调用示例；模型发布页的退役公告**未提 TTS**。
- 因此 `src/services/tts/mimo_tts_provider.py::DEFAULT_MIMO_MODEL = "mimo-v2-tts"` **没有**官方 deadline。升级到 `mimo-v2.5-tts` 是主动升级（风险前置），不是 availability 迁移。
- 除非后续拿到 TTS V2 单独下线公告，否则不要把它写成“已确认停用”。详见 Phase 3。

## 2. 本项目现状

### 2.1 MiMo LLM 已接入 registry

`src/services/llm_registry.py` 已有三个 MiMo logical model：

- `mimo_v25` -> `mimo-v2.5`，`supports_audio=True`
- `mimo_v25_pro` -> `mimo-v2.5-pro`，`supports_audio=False`
- `mimo_omni` -> `mimo-v2-omni`，legacy 兼容

当前问题：

- `cost_hint` 仍写 Token Plan 1x/2x，不适合作为生产提示。
- `cost_rank` 会影响 `get_fallback_candidates()` 的全局 fallback 排序，不能在价格目录 PR 里顺手修改。
- Smart mode 默认仍全部指向 `gemini_pro`，符合当前“高质量优先”的产品定位。

相关文件：

- `src/services/llm_registry.py`
- `gateway/admin_settings.py`
- `frontend-next/src/app/(app)/admin/prompts/page.tsx`

### 2.2 OpenAI-compatible 文本路径都丢弃 usage

`src/services/gemini/translator.py::_call_mimo_text(...)` 直接调用：

```text
https://api.xiaomimimo.com/v1/chat/completions
```

当前实现只返回：

```python
body["choices"][0]["message"]["content"]
```

同类问题也存在于 DeepSeek / OpenAI compatible HTTP path：它们也只取正文，不把 provider usage 传给 `UsageMeter.record_llm(...)`。因此 admin cost page 中这些模型的 token 成本目前都是本地估算。

当前问题：

- 没有返回完整 response。
- 没有提取真实 input/output/cached/audio token。
- `UsageMeter.record_llm(...)` 因此通常写入 `token_count_source = "estimated_text_length"`。
- HTTP 错误没有记录 body 摘要，不利于区分 auth、quota、rate-limit、invalid-output。

相关文件：

- `src/services/gemini/translator.py`
- `src/services/usage_meter.py`
- `gateway/cost_management.py`

### 2.3 S2 transcript reviewer 已支持 MiMo 多模态

`src/services/transcript_reviewer.py::_call_mimo_omni_raw(...)` 支持 text-only 与 `input_audio` payload，可服务 Pass1 / Pass3 这类审核阶段。

当前问题：

- 同样只返回 response text。
- usage 没有进入 metering。
- 仍以 `mimo_omni` 命名保留 legacy wrapper，实际也会接收 `mimo-v2.5` 的 model id；命名与行为容易误导。
- Pass1/Pass3 的质量指标与翻译 benchmark 不同，不能用纯文本翻译分数推断音频审核可默认切换。

相关文件：

- `src/services/transcript_reviewer.py`
- `tests/test_transcript_reviewer.py`

### 2.4 MiMo TTS 已接入但仍是旧默认模型

`src/services/tts/mimo_tts_provider.py` 当前默认：

```python
DEFAULT_MIMO_MODEL = "mimo-v2-tts"
```

当前问题：

- 官方**未**宣布 `mimo-v2-tts` 停用（TTS 文档今天仍只支持 v2-tts）。升级到 `mimo-v2.5-tts` 是主动升级，不是强制迁移，无 deadline，可回退 v2-tts。见 Phase 3。
- TTS response 的 usage 没有采集。
- `src/services/tts/tts_generator.py` 明确把 MiMo 的 `billed_chars` 保持为 0，因为项目此前认为其 token-based billing 无法准确映射到 billed chars。
- 前端 settings 文案写“MiMo-V2-TTS（小米）”，可后续同步为 V2.5。
- 限时免费如果只写成 `cost=0`，会误导 admin 毛利分析；需要显式 promotional 状态或失效日期。

相关文件：

- `src/services/tts/mimo_tts_provider.py`
- `src/services/tts/tts_generator.py`
- `src/services/tts/tts_strategy.py`
- `frontend-next/src/app/(app)/admin/settings/page.tsx`
- `tests/test_job_metering_writeback.py`
- `gateway/credits_observability.py`

### 2.5 Smart TTS 当前硬锁 MiniMax

`gateway/job_intercept.py::compute_job_policy(...)` 对 `service_mode == "smart"` 返回：

- `tts_provider = "minimax"`
- `tts_model = "speech-2.8-hd"`
- `voice_clone_enabled = True`
- `voice_strategy = "smart_auto"`

这是合理的当前边界：Smart 的 clone API、quota、UserVoice mirror、provider exhaustion 处理都围绕 MiniMax 建模。

结论：

- MiMo 降价不应直接改变 Smart TTS 主链路。
- MiMo 可以先进入 Smart 的 LLM prompt stages 试点，不触碰 Smart voice clone/TTS contract。
- `mimo-v2.5-voiceclone` / `mimo-v2.5-voicedesign` 不应进入任何自动路径。

### 2.6 已有 benchmark 结果不能支持直接全量切换

已有 `reports/benchmark/translation_quality_benchmark_main_20260426.md` 中，24 个样本的排序为：

| 模型 | Overall | Quality | Constraints | Avg latency |
| --- | ---: | ---: | ---: | ---: |
| `gemini_31_flash_lite` | 86.81 | 83.50 | 83.58 | 2903 ms |
| `gemini_pro` | 83.81 | 86.00 | 92.74 | 32387 ms |
| `deepseek` | 82.54 | 79.67 | 76.47 | 6411 ms |
| `mimo_v25` | 82.48 | 79.58 | 81.13 | 9867 ms |
| `mimo_v25_pro` | 80.03 | 80.00 | 79.25 | 21861 ms |

结论：

- `mimo_v25` 可用，但还不是当前 benchmark 第一。
- `mimo_v25_pro` 在这个 benchmark 里更慢，不适合因为“Pro”命名就默认采用。
- 降价后的正确动作是重新跑 benchmark + shadow eval，而不是按旧分数直接定默认。
- 该 benchmark 只覆盖文本翻译，不覆盖 Pass1/Pass3 音频审核。

## 3. 优化目标

### 3.1 成本目标

- MiMo LLM 成本在 admin cost page 中可见、可解释、可回放。
- DeepSeek / OpenAI / MiMo compatible path 尽量使用 provider 返回的真实 usage。
- cached input token 单独计价，不能混入普通 input token。
- audio token 能被单独采集；在字段形状未确认前，audio rate 明确保持 missing/unknown，不伪造。
- promotional / limited-free TTS 价格必须显式可见，避免过期后继续按 0 成本分析。

### 3.2 质量目标

- 保持当前项目不变量：
  - TTS unit 是 `SemanticBlock`，不是 subtitle line。
  - Alignment DSP-first，rewrite loop 只是 fallback。
  - Subtitle retiming 仍是数学/确定性逻辑。
  - 主交付目标仍是 Jianying draft output。
- MiMo 只进入翻译、审核、rewrite、suggest-split 等 LLM 决策点，不替代 deterministic retiming / alignment 逻辑。

### 3.3 产品目标

- Express / Studio 可通过 admin model settings 做 shadow 或试点。
- Smart 保持高质量默认，不因为价格降价自动改变用户承诺。
- 用户侧不暴露内部成本字段；成本仍只在 admin-only surface。
- Smart `_MODE_DEFAULTS` 的任何调整都属于产品承诺变更，需要产品/商务 sign-off；不得由工程 PR 单方决定。

### 3.4 可用性目标（本次新增）

- 2026-06-30（omni 下线）前，项目内不再有任何运行时路径解析到 `mimo-v2-omni`。
- 迁移过程对历史 admin 设置零破坏：引用 `mimo_omni` 的设置仍可解析、不报错。
- TTS 是主动升级：若 `mimo-v2.5-tts` 验证不通过，可回退 `mimo-v2-tts`（仍受官方支持）或其它 provider；不必赶 deadline。

## 4. 分阶段方案

### Phase 0：事实固化与文档

本文件即 Phase 0 产物。

验收：

- 有明确外部价格事实。
- 有本项目代码现状映射。
- 有后续实施顺序和不做事项。

### Phase 0a：provider response spike

在改成本目录和 parser 前，先用最小真实调用确认字段形状。

触发方式：

- 由开发者手动一次性运行并提交脱敏 fixture。
- 不得放进 pipeline、scheduled task、heartbeat automation、CI 或默认测试路径。
- 不得在无明确人工确认的情况下自动调用付费 provider API。

范围：

1. 调一次 MiMo text chat completion。
2. 调一次 MiMo 多模态音频 chat completion。
3. 如条件允许，调一次 `mimo-v2.5-tts`。
4. 调一次 DeepSeek text completion，确认 compatible path 的 usage 字段形状。
5. 将脱敏后的 response 保存为 fixture 或审计样例，建议路径：
   - `tests/fixtures/provider_responses/mimo_v25_text_response.json`
   - `tests/fixtures/provider_responses/mimo_v25_audio_response.json`
   - `tests/fixtures/provider_responses/deepseek_chat_response.json`

要求：

- fixture 必须脱敏，不包含 API key、长原文、用户私有音频。
- 如果不能提交真实 fixture，至少在本方案或 follow-up doc 中记录字段结构。
- parser 实现不得依赖猜测字段名。

验收：

- 明确 MiMo 是否返回 `usage.prompt_tokens` / `completion_tokens`。
- 明确 cached token 字段实际位置。
- 明确 audio token 字段实际位置。
- 明确 TTS 是否返回 usage；如果不返回，TTS 成本保持 unknown/promotional，不伪造 token。
- 复核官方下线时间线（omni 已知：2026-06-01 转发 / 2026-06-30 下线；TTS 暂无下线公告，需复查是否新增）。

实施状态（2026-05-29 在美国主机 `docker exec` 跑，非侵入，未 restart）：

- ✅ 字段形状已确认并落 fixture：`tests/fixtures/provider_responses/{mimo_v25_text_usage,mimo_v25_audio_usage,deepseek_chat_usage}.json` + `README.md`。
- ✅ MiMo / DeepSeek 均返回 `usage.prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`；MiMo 音频调用额外返回 `prompt_tokens_details.audio_tokens`。
- ⚠️ **PR 2 关键约束（spike 实测）**：`prompt_tokens` 是**含 cached + audio 的总输入**。成本引擎加法计费，PR 2 必须拆：`input = prompt - audio - cached`、`cached_input = cached_tokens`、`audio_input = audio_tokens`，否则缓存/音频部分双重计费。详见 fixtures README。
- ⚠️ MiMo TTS 朴素 chat payload 返回 HTTP 400，需 `mimo_tts_provider` 专用格式（PR 3 处理）；TTS 限免，成本不依赖此。
- ⚠️ TTS 仍无官方下线公告（与 §1.3 一致，未变）。

### Phase 0b：`mimo_omni` 强制迁移（availability-critical，deadline 2026-06-30）

> 触发原因：官方已公告 `mimo-v2-pro` / `mimo-v2-omni` 退役（2026-06-01 自动转发，2026-06-30 原名下线），不是价格优化。因此**不以 benchmark / shadow 为门槛**，但要 smoke 验证 `mimo-v2.5` 多模态接口可用。依赖 Phase 0a 已确认 V2.5 text / audio 接口形状。**本阶段仅处理 omni，TTS 不在内（见 Phase 3）。**

改动：

1. **`mimo_omni` logical model**：`mimo-v2-omni` 在 6-30 后失效。本 PR **锁定方案 A**（规避 P2 漏改风险）：
   - 把 `mimo_omni.api_model_id` 从 `mimo-v2-omni` 重指向 `mimo-v2.5`，label 标 deprecated，保留逻辑名不破坏历史 admin settings。
   - 本质是“同名重定向到 V2.5”，但**必须有 admin 可见性 / 日志**——这点取代旧方案“绝不静默重定向”：停用让重定向不可避免，但不能无声。
   - （若坚持方案 B 彻底移除 `mimo_omni`，必须同时清理 `transcript_reviewer._call_mimo_omni_raw` 默认 `_resolve_model_id("mimo_omni")`（line 2566）与 `_call_review_mimo_omni` 的 `"mimo_omni"` 回退（line 2644），并加测试——否则只迁 admin settings 会漏掉这两条路径。)
2. **内部默认值验证**：方案 A 下 `_call_mimo_omni_raw` / `_call_review_mimo_omni` 对 `mimo_omni` 的引用自然解析到 `mimo-v2.5`，无需逐处改；但要加测试断言解析结果不再是 `mimo-v2-omni`。

不做：

- 不借迁移之机把 MiMo 抬成跨 provider 默认（那是 Phase 4 / 5）。
- 不在本阶段动 TTS（`DEFAULT_MIMO_MODEL`）—— TTS 无官方 deadline，放 Phase 3。

验收：

- 全仓搜索无残留 `mimo-v2-omni` 运行时默认值（历史 fixture / 注释除外），含 `transcript_reviewer.py` line 2566 / 2644。
- `resolve_model_id("mimo_omni") == "mimo-v2.5"`；选 `mimo_omni` 的历史 admin 设置不报错、有日志 / UI 提示。
- Pass1 / Pass3 音频审核选 MiMo 时走 `mimo-v2.5`，smoke 出正常结果。
- 无 `MIMO_API_KEY` 环境下 `main.py` import 与 `pytest` 仍可跑。

回滚：

- 2026-06-30 前 `mimo-v2-omni` 仍可用，可临时切回；6-30 后不可逆，必须确保迁移在 deadline 前完成。

实施状态（2026-05-29 落地，方案 A）：

- ✅ `mimo_omni.api_model_id` 重指向 `mimo-v2.5`，标 `deprecated=True`，label/cost_hint 更新（`src/services/llm_registry.py`）。
- ✅ `get_prompt_model` 选中 deprecated 模型时去重 WARNING；`get_all_models_with_status` / `get_available_models_for_prompt` 暴露 `deprecated` 字段供 admin UI。
- ✅ 回归测试 `tests/test_mimo_omni_migration.py`（6 条）+ `tests/test_transcript_reviewer.py`（62 条）全绿。
- ✅ 已验证全仓无 `api_model_id → 逻辑名` 反向查找，方案 A 重指向不破坏唯一性假设；测试无断言旧值 `mimo-v2-omni`。
- ✅ **已部署美国主机（2026-05-29）**：等当时一个在跑的 Express job 结束后，上传 `llm_registry.py` 到 bind-mount + `docker restart aivideotrans-app`；运行态验证 `resolve_model_id('mimo_omni')=="mimo-v2.5"`、`deprecated=True`，容器 healthy，公网 200；旧文件备份 `.bak-20260529`。
- ⏳ 完整 Pass1/Pass3 选 MiMo 跑真实 job 的 smoke 未单独做（解析已在运行态验证，spike 也证明 `mimo-v2.5` 多模态接口可用，风险低）。

### Phase 1：成本目录与 UI 提示更新

改动：

1. 在 `gateway/cost_management.py::DEFAULT_PRICE_CATALOG["llm"]` 增加：
   - `mimo:mimo-v2.5`
   - `mimo:mimo-v2.5-pro`
2. 价格字段使用 RMB-direct：
   - `input_per_million_rmb`
   - `cached_input_per_million_rmb`
   - `output_per_million_rmb`
3. **audio 费率处理（2026-05-29 查官网确认）**：官方 pay-as-you-go 文档**未单列**音频输入计费——多模态 audio 按通用 input token 计价（与文本同价）。因此成本引擎 `apply_costs`（`gateway/cost_management.py:429`）对 audio 的 `or input_price` 回退对 MiMo 是**有据的**，不是伪造。已显式写 `audio_input_per_million_rmb = input`（自文档化，不依赖隐式回退）。
   - 音频 token **数量** 仍用引擎默认 25 tokens/s 估算，待 PR 2 采集真实 usage 后转为精确值；费率本身已准确。
   - 历史顾虑（已解除）：曾担心"省略 audio 费率→引擎按 input 价伪造 audio 成本"违反"不伪造"。查官网确认 MiMo 本就 audio=input 价后，该回退成为正确行为。若官方将来改为单列 audio 计费，需同步更新本条 + 评估引擎回退是否仍合理。

实施状态（2026-05-29 落地）：

- ✅ `gateway/cost_management.py` 增 `mimo:mimo-v2.5` / `mimo:mimo-v2.5-pro`（RMB-direct input/cached/output + audio_input=input），catalog version → `2026-05-29-mimo-v2.5-llm`。
- ✅ `src/services/llm_registry.py` 的 `mimo_v25` / `mimo_v25_pro` cost_hint 改为 RMB-direct（去掉 Token Plan 字样）。
- ✅ 未动 `cost_rank` / `_MODE_DEFAULTS` / fallback。回归测试 `test_mimo_omni_migration::test_mimo_cost_ranks_unchanged` 守卫 cost_rank 不变。
- ✅ 成本测试 `tests/test_cost_management.py` 新增 3 条（mimo-v2.5 文本+缓存=¥3.02、pro 配置、omni 别名命中 v2.5 catalog），与现有 cost 套件全绿（8 条）。
- ⏳ admin model UI 已通过 cost_hint + `deprecated` 字段获得数据；前端是否加 deprecated 徽章为可选增强，未做。
- ✅ **已部署美国主机（2026-05-29）**：gateway 是镜像构建（非 bind-mount），上传 `cost_management.py` 到构建上下文（基线与运行镜像一致，无漂移）→ 后台 `docker compose build gateway` → 在飞 job guard=0 后 `up -d --no-deps --force-recreate gateway`（不碰 app）；运行态验证 catalog version=`2026-05-29-mimo-v2.5-llm` + `mimo:mimo-v2.5` 条目，gateway healthy，公网 200；旧文件备份 `.bak-20260529`。
4. 更新 `src/services/llm_registry.py`：
   - `mimo_v25.cost_hint`
   - `mimo_v25_pro.cost_hint`
5. 更新 admin model UI 显示，避免 Token Plan 字样误导生产成本。

明确不做：

- 不修改 `cost_rank`。
- 不修改 `_MODE_DEFAULTS`。
- 不修改 fallback candidates。
- 不把 MiMo 加进更多运行时默认链路。

建议费率：

```json
{
  "mimo:mimo-v2.5": {
    "input_per_million_rmb": 1.0,
    "cached_input_per_million_rmb": 0.02,
    "output_per_million_rmb": 2.0,
    "source": "xiaomi_mimo_pay_as_you_go_2026-05-27"
  },
  "mimo:mimo-v2.5-pro": {
    "input_per_million_rmb": 3.0,
    "cached_input_per_million_rmb": 0.025,
    "output_per_million_rmb": 6.0,
    "source": "xiaomi_mimo_pay_as_you_go_2026-05-27"
  }
}
```

测试：

- cost tests 增加 MiMo LLM row 成本计算。
- 验证 cached token 被按缓存价计费。
- 验证 MiMo audio usage 未配置费率时显示 `missing_rate` 或明确 warning。
- 增加 fallback-chain snapshot 测试或文档输出，证明 Phase 1 没有改变 fallback 链。

风险：

- 如果 provider usage 字段名与 OpenAI 兼容字段不同，Phase 1 只能解决估算价，不能解决真实 token。

### Phase 2：OpenAI-compatible 真实 usage 统一采集

MiMo 是触发点，但实现应覆盖 shared compatible path，避免只解决一半。

改动：

1. 将 MiMo / DeepSeek / OpenAI compatible HTTP call 拆成“拿完整 response”和“提取正文”两个层次。
2. 保持外部调用方仍拿正文，避免大范围改签名；可用内部 helper：
   - `_call_openai_compatible_response(...) -> dict`
   - `_extract_openai_compatible_text(response) -> str`
   - `_extract_openai_compatible_usage(response) -> dict`
3. 对 `UsageMeter.record_llm(...)` 传入真实 token：
   - `input_tokens`
   - `output_tokens`
   - `cached_input_tokens`
   - `audio_input_tokens` / `input_audio_tokens`
4. 对 transcript reviewer 的 MiMo path 做同样处理。
5. 错误路径记录 provider body 摘要，但必须经 redaction，不能泄露 API key 或用户长文本。

字段命名：

- `audio_input_tokens` / `input_audio_tokens` 等字段名以 Phase 0a fixture 实测为准。
- plan 阶段不冻结 provider response 字段名，只冻结“真实 usage 优先、失败回退估算”的行为。

验收：

- MiMo、DeepSeek 成功调用的 `usage_events.jsonl` 中 `token_count_source != estimated_text_length`。
- admin cost page 中 MiMo row 有 configured rate 与非空 text cost。
- cached token 能进入 `cached_input_tokens` 字段。
- 音频审核调用能记录 `audio_input_seconds`，并尽量记录 audio token。
- 无 usage 字段时 fallback 到估算 token，且主流程不失败。

测试：

- fake MiMo response 使用 Phase 0a fixture 字段。
- fake DeepSeek response 使用 Phase 0a fixture 字段。
- 单测验证 `UsageMeter` event 字段落盘。
- 单测验证无 usage 字段时 fallback 到估算 token，且带 warning/extra 标记。
- 单测验证 parsing 失败不影响正文返回。

风险：

- 修改 translator 调用路径容易影响 DeepSeek/OpenAI/Gemini。实现时只改 direct HTTP compatible path，不碰 Gemini SDK path。
- usage parsing 失败不能变成主流程失败；应 best-effort。

实施状态（2026-05-29，PR 2 第一部分 — 仅本地仓库 + 测试，**未部署**）：

- ✅ **translator 路径已落地**：`_normalize_openai_usage(body)` best-effort 解析 + 拆分（`input = prompt − cached − audio`，避免双重计费）；`_call_mimo_text` / `_call_openai_compatible` 返回 `(text, usage)`；`_call_by_model` 暂存 `self._last_call_usage`（仍返回 str，不破坏 stub 测试）；`_record_llm_usage` / `record_llm` 透传真实 token，`token_count_source` 在有真实 usage 时写 `provider_usage`。覆盖 translate / rewrite / probe / content_compliance（mimo + deepseek + openai HTTP path）。
- ✅ **usage_meter 向后兼容**：新参数默认 None；无真实 usage 时事件 payload 形状与历史一致（不加 cached/audio 键、`token_count_source` 仍 estimated）。
- ✅ 测试 `tests/test_pr2_usage_capture.py`（normalizer × fixtures + record_llm provider/estimate 两路）；回归 137 条全绿（usage_meter / gemini_translator / content_compliance / job_metering_writeback / transcript_reviewer / cost / credits_observability）。
- ✅ **PR 2 第二部分已落地**：transcript_reviewer 的 MiMo 音频审核路径——`_call_mimo_omni_raw` 加可选 `usage_sink` 出参（保持返回 str，不破坏 monkeypatch 测试）填充 usage；pass1 / pass3 / legacy `_call_review_mimo_omni` 三个计量点捕获并透传到 reviewer `_record_llm_usage`（低置信说话人 verifier 本就不计量，未动）。
- ✅ **DRY**：normalizer 抽到 `llm_registry.normalize_openai_usage` 单一真源，translator 的 staticmethod 改为委托，reviewer import 复用。
- ✅ 测试：`test_pr2_usage_capture.py` 加 reviewer 两条（mock urlopen 验 usage_sink + `_record_llm_usage` 透传）；146 + 60 条回归全绿。
- ⏳ 待部署美国主机（app 容器 bind-mount，需在飞检查 + restart）。

### Phase 3：MiMo TTS V2.5 主动升级 + 计量 + 定位

> TTS V2 **无官方下线公告**，所以这是主动升级不是强制迁移。先 smoke，再决定是否把默认从 `mimo-v2-tts` 升到 `mimo-v2.5-tts`；smoke 不过可保持 v2-tts。

改动：

1. **默认模型升级（主动，可回退）**：smoke 通过后把 `mimo_tts_provider.py::DEFAULT_MIMO_MODEL` 升到 `mimo-v2.5-tts`，并同步 docstring / class 注释 / `admin/settings` 里的 “MiMo-V2-TTS” 文案。保留 env/admin override（`MIMO_TTS_MODEL`）。
   - 确认 V2.5 的可用音色 id（`mimo_default` / `default_zh` / `default_en` 是否仍有效或更名）；音色不一致要在 smoke 阶段先发现。
   - 仍**不**碰 Smart TTS（Smart 仍 MiniMax）；只影响 Express / Studio 选 MiMo TTS 的路径。
2. 运行 TTS smoke：中文长句 / 中英混合 / 多说话人 style / 极短句 / 情绪口语风格。
3. 如果 response 提供 usage，则采集 token usage（沿用 Phase 0a 字段）。
4. 成本目录中对 MiMo TTS 显式表达限时免费，不只靠 `source` 字符串：
   - `rate_status = "promotional"`，或 rate 加 `pricing_until` / `promotional_until`，或 UI 强制显示 “限时免费，失效日期未知/待确认”。

不建议做的事：

- 不把 Smart TTS 从 MiniMax 改成 MiMo。
- 不把 MiMo TTS `billed_chars` 伪造为 `len(text)` 或 `len(text)*2`。
- 不用限时免费价格重新设计用户套餐。
- 不因为“怕停用”就跳过 smoke 强行切默认——TTS V2 没有 deadline。

验收：

- Express/Studio 可在 admin settings 中选择 MiMo TTS 并正常完成任务。
- 生成音频 duration 可被现有 DSP alignment 处理。
- `main.py` 与 `pytest` 在无真实 API key 环境仍可跑。
- admin cost UI 不会把 promotional TTS 当成长期 0 成本事实。

回滚：

- `mimo-v2-tts` 仍受官方支持，可直接把 `DEFAULT_MIMO_MODEL` / admin setting 切回 v2-tts；或回落 MiniMax / CosyVoice / VolcEngine。
- 若真要从 Express / Studio 摘掉 MiMo TTS 选项，**必须同步改 gateway allowlist**：`gateway/job_intercept.py::_VALID_EXPRESS_PROVIDERS` / `_VALID_STUDIO_PROVIDERS`（line 348-349）目前都含 `"mimo"`，并接受 admin 配置；只改前端会漏，gateway 仍放行。

### Phase 4：模型选择试点

建议策略：

| 场景 | 建议 |
| --- | --- |
| Express 翻译 | 先 shadow `mimo_v25`，与 `deepseek` / `gemini_31_flash_lite` 对照；不直接给真实用户默认开启 |
| Studio Pass1/Pass3 | 先 shadow `mimo_v25` 音频审核；需要专门音频审核 benchmark |
| Studio translate/rewrite | 先 shadow `mimo_v25`；JSON 成功率与 glossary 保留率达标后再考虑 admin 小流量试点 |
| Smart 默认 | 暂不切；先做 admin override / shadow |
| `mimo_v25_pro` | 不因 Pro 命名默认采用；只在复杂 agent/text benchmark 证明收益后使用 |

试点开关：

- 使用现有 `prompt_models[mode][prompt_key]` admin override。
- 必要时增加 “MiMo shadow only” runtime flag，而不是直接改 `_MODE_DEFAULTS`。
- 试点结果进入 Smart analytics / report analysis，不进入用户解释面。
- `cost_rank` 如需调整，只能在本阶段做，并附 fallback chain diff 与回归测试。

验收指标：

- JSON parse success rate。
- provider error / timeout / 429 rate。
- translation quality benchmark overall。
- glossary preservation。
- wrong-script / Latin-dominant 风险。
- S2 speaker correction precision。
- Smart handoff rate。
- rewrite retry count。
- LLM cost per source minute。
- latency p50 / p95。

### Phase 5：是否调整默认值的决策门槛

默认值切换分成两类，不能混用同一套 benchmark。

文本翻译 / rewrite 默认切换门槛：

1. 近 24 个以上 translation benchmark 样本整体分不低于当前默认候选，且没有明显 category regression。
2. 至少一批真实任务 shadow 中：
   - JSON 成功率 >= 99%
   - provider error rate 不高于当前默认
   - glossary / subtitle quality report 不退化
3. admin cost page 能准确显示 MiMo 成本，不再大量 `missing_rate`。
4. rollback 只需要改 admin settings 或小范围 registry 默认，不需要迁移数据。

Pass1 / Pass3 音频审核默认切换门槛：

1. 至少 10 个带音频样本的专门审核 benchmark。
2. speaker 纠错 precision / recall 不低于当前默认。
3. glossary 抽取与 corrections 质量不退化。
4. audio usage 与 cost coverage 明确；未知则不得默认切换。
5. Smart 的用户承诺、clone/quota、review gate 都不受影响。

## 5. Legacy 与兼容策略

### 5.1 `mimo_omni`（omni 停用后必须迁移，deadline 2026-06-30）

- 原则更新：旧方案写“不静默重定向 `mimo_omni`→`mimo_v25`”，是在 omni 仍可用、迁移属可选优化的前提下成立。**官方公告 omni 6-30 下线后该前提失效**——`mimo-v2-omni` 会直接坏，迁移不再可选。
- 按 Phase 0b **锁定方案 A**（重指向 + deprecated label），带 admin 可见性 / 迁移日志，不能无声重定向；底线是运行时不再解析出 `mimo-v2-omni`。
- 若选方案 B（移除逻辑名），必须连带清理 `transcript_reviewer.py` 的两处 `mimo_omni` 默认 / 回退（line 2566 / 2644），否则会漏迁。
- 注意：本节只约束 omni；TTS（`mimo-v2-tts`）无官方 deadline，按 Phase 3 主动升级处理。

### 5.2 MiMo voiceclone / voicedesign

- 不引入 `mimo-v2.5-voiceclone` / `mimo-v2.5-voicedesign` 到任何自动路径。
- 不把它们作为 MiniMax clone 失败后的 fallback。
- 即使限时免费，也不改变 Smart auto-clone / quota / UserVoice mirror 合同。
- 手动研究可以另开方案，但不能进入默认 path 或测试 path。

## 6. 明确不做

- 不把 subtitle retiming 改成 LLM-driven。
- 不把 DSP-first alignment 改成模型猜测。
- 不把 TTS unit 从 `SemanticBlock` 改回 subtitle line。
- 不把 Smart TTS 主链路从 MiniMax 改成 MiMo。
- 不把 MiMo 限时免费 TTS 当作长期商业定价依据。
- 不把 Token Plan 折算价写进 Gateway pricing / entitlements / credits truth。
- 不“无声”迁移 `mimo_omni`（迁移本身因 omni 6-30 下线是必须的，但必须带 admin 可见性 / 日志——见 §5.1 / Phase 0b）。
- 不把 `mimo-v2-tts` 写成“已确认停用”——官方未公告 TTS 下线，它是主动升级（Phase 3）。
- 不借 omni 迁移之机把 MiMo 抬成跨 provider 默认（Gemini / MiniMax 主链路不变）。
- 不把 MiMo voiceclone / voicedesign 放进自动 fallback。
- 不把成本字段暴露到用户 Workspace。
- 不引入真实外部 API 作为默认测试依赖。

## 7. 推荐实施顺序

1. **Phase 0a**：用真实请求确认 MiMo text、MiMo audio、MiMo TTS、DeepSeek response 的 usage 字段形状；复核官方下线时间线（omni 2026-06-30）。
2. **Phase 0b（优先，availability-critical，deadline 2026-06-30）**：把 `mimo_omni` 重指向 `mimo-v2.5`（方案 A），带迁移日志 / UI 提示。**仅 omni，不含 TTS。**
3. **Phase 1**：补 MiMo LLM RMB-direct cost catalog 与 registry cost hint，不动 `cost_rank`。
4. **Phase 2**：补 OpenAI-compatible response usage parsing 与 UsageMeter 写入。
5. **重新跑 benchmark**：至少复跑 `scripts/benchmark/translation_quality_benchmark.py` 的主样本集。
6. **补音频审核 benchmark**：为 Pass1/Pass3 单独准备样本和指标。
7. **Phase 3**：MiMo TTS V2.5 主动升级（smoke 后切默认，可回退 v2-tts）+ 计量 + 限免成本可见性。
8. **Phase 4**：Express / Studio 做 shadow；达标后再做 admin override 小流量试点。
9. **Phase 5**：根据 Smart analytics / cost page / benchmark 决定是否调整默认。

## 8. 测试清单

### 8.1 单元测试

- MiMo LLM rate lookup：
  - `mimo:mimo-v2.5`
  - `mimo:mimo-v2.5-pro`
- cached token cost：
  - cached input 按缓存命中价计费。
  - uncached input 按普通输入价计费。
- OpenAI-compatible usage parser：
  - MiMo text fixture。
  - MiMo audio fixture。
  - DeepSeek fixture。
  - no-usage fallback。
  - malformed-usage fallback。
- fallback chain guard：
  - 对 `MODEL_REGISTRY` 所有 logical model 与 `requires_audio={True,False}` 组合 dump `get_fallback_candidates()` 结果。
  - baseline 建议路径：`tests/snapshots/fallback_chain.json`。
  - PR 1 前后 diff 必须为空；如果不为空，该改动必须移到 Phase 4 / PR 5。
- `mimo_omni` 强制迁移（Phase 0b）：
  - `resolve_model_id("mimo_omni") == "mimo-v2.5"`（方案 A），或引用被迁移到 `mimo_v25`（方案 B）。
  - 历史 admin 设置引用 `mimo_omni` 不报错。
  - 回归守卫：源码无残留 `mimo-v2-omni` 运行时默认值（含 `transcript_reviewer.py` line 2566 / 2644）。
- MiMo TTS V2.5 主动升级（Phase 3）：
  - smoke 后 `DEFAULT_MIMO_MODEL == "mimo-v2.5-tts"`。
  - env/admin override 仍可覆盖；可回退 `mimo-v2-tts`。
- no-key path：
  - 无 `MIMO_API_KEY` 时不影响 `main.py` import。

### 8.2 集成/回归测试

- `pytest -q tests/test_transcript_reviewer.py`
- `pytest -q tests/test_job_metering_writeback.py`
- `pytest -q tests/test_credits_observability.py`
- cost management 相关测试（新增或挂到现有 suite）。

### 8.3 手工 smoke

- Express + MiMo LLM translate。
- Studio + MiMo Pass1 音频审核。
- Studio + MiMo TTS 生成短视频任务。
- Admin cost page 查看 MiMo LLM row。
- Admin model page 禁用 MiMo 后，下拉列表与 fallback 行为正确。

## 9. Rollback

每个阶段都必须支持低成本回滚：

- Phase 0a：只产生 fixture / 记录，不影响 runtime。
- Phase 0b：2026-06-30 前 `mimo-v2-omni` 仍可用，可临时切回；6-30 后不可逆，必须在 deadline 前完成迁移。
- Phase 1：删除/覆盖 cost catalog entry 即可；因为不动 `cost_rank`，不会影响 fallback 行为。
- Phase 2：usage parsing 失败不阻断主流程；可以只退回估算 token。
- Phase 3：`mimo-v2-tts` 仍受支持，可直接把 `DEFAULT_MIMO_MODEL` / admin setting 切回 v2-tts，或回落其它 provider。若要摘掉 MiMo TTS 选项，须同步改 gateway allowlist（`_VALID_EXPRESS_PROVIDERS` / `_VALID_STUDIO_PROVIDERS`），不能只改前端。
- Phase 4：移除 admin prompt model override 或 shadow flag。
- Phase 5：如默认切换后退化，只改 registry/default settings，不迁移历史任务。

## 10. 后续 PR 建议

### PR 0：Provider response spike

范围：

- 最小真实请求脚本或一次性手工记录。
- 脱敏 fixture / 字段结构记录。
- `docs/legal/mimo-tokenplan-eligibility.md` 占位或合规状态记录。

不碰：

- runtime 默认行为。
- cost catalog。
- Gateway pricing。

### PR 0b：`mimo_omni` 强制迁移（availability-critical，deadline 2026-06-30）

范围：

- `src/services/llm_registry.py`（`mimo_omni.api_model_id` 重指向 `mimo-v2.5` + deprecated label，方案 A）
- `src/services/transcript_reviewer.py`（仅当选方案 B 时清理 line 2566 / 2644 的 `mimo_omni` 默认与回退；方案 A 不必改）
- migration 日志 / admin 可见性
- 回归守卫：`resolve_model_id("mimo_omni") == "mimo-v2.5"`，源码无残留 `mimo-v2-omni` 运行时默认值

不碰：

- TTS（`DEFAULT_MIMO_MODEL`）—— 无官方 deadline，放 PR 3
- Smart MiniMax hard lock
- `cost_rank` / `_MODE_DEFAULTS`
- MiMo voiceclone / voicedesign 自动路径
- 跨 provider 默认（不把 MiMo 抬成主模型）

依赖：Phase 0a 已确认 V2.5 多模态接口形状。deadline = 2026-06-30。

### PR 1：MiMo 价格目录更新

范围：

- `gateway/cost_management.py`
- `src/services/llm_registry.py`
- 相关成本测试

不碰：

- `cost_rank`
- translator 调用签名
- Smart defaults
- TTS provider

### PR 2：OpenAI-compatible usage 真实采集

范围：

- `src/services/gemini/translator.py`
- `src/services/transcript_reviewer.py`
- `src/services/usage_meter.py`（如需要扩字段）
- tests

不碰：

- 成本价格事实之外的商业定价
- Smart TTS policy
- Gemini SDK path

### PR 3：MiMo TTS V2.5 主动升级 + 计量

> TTS 无官方 deadline；本 PR 在 smoke 通过后把默认升到 v2.5-tts（可回退），并做计量 + promotional pricing。

范围：

- `src/services/tts/mimo_tts_provider.py`（smoke 后 `DEFAULT_MIMO_MODEL` → `mimo-v2.5-tts` + docstring / class 文案）
- `frontend-next/src/app/(app)/admin/settings/page.tsx`（“MiMo-V2-TTS” 文案）
- `src/services/tts/tts_generator.py`（如 Phase 0a 确认 TTS 有 usage 则采集；否则 billed_chars=0 + unknown）
- `gateway/cost_management.py`（MiMo TTS promotional / `pricing_until` 表达）
- admin cost UI 的限免提示
- tests（含 `DEFAULT_MIMO_MODEL == "mimo-v2.5-tts"`）

不碰：

- Smart MiniMax hard lock
- MiMo voiceclone / voicedesign 自动路径
- credits package pricing

### PR 4：Benchmark + Shadow report

范围：

- translation benchmark 结果文件
- Pass1/Pass3 音频审核 benchmark
- Smart/report analysis 文档或 dashboard 解释

不碰：

- 默认模型切换

### PR 5：默认值与 fallback rank 决策

范围：

- 仅在 PR 4 数据达标后考虑。
- 仅调整 studio / express defaults 和 `cost_rank`。
- Smart `_MODE_DEFAULTS` 在本 PR 不动；如需调整，必须另开产品签核后的方案。
- 必须附 fallback chain diff。

不碰：

- Smart TTS / clone policy。
- Token Plan 商业化事实。

## 11. 决策记录

- **2026-05-27**：确认 MiMo 降价是重要机会，但当前仓库缺少 MiMo cost catalog 与真实 usage 采集；先进入方案阶段，不直接改默认模型。
- **2026-05-27 Claude Code review 后更新**：Phase 2 扩展为 OpenAI-compatible usage 统一采集；Phase 1 明确不动 `cost_rank`；新增 Phase 0a response spike、TTS promotional pricing、Pass1/Pass3 独立 benchmark、MiMo voiceclone/voicedesign 禁止自动接入、Token Plan 合规落点。（注：当时写的 “`mimo_omni` 不静默迁移” 已被下一条 V2 停用决策部分推翻——见下。）
- **2026-05-27 V2 停用决策（初版，部分被 Codex 修正）**：据用户“V2 即将停用”提示，初版把 `mimo-v2-omni` 与 `mimo-v2-tts` 都当成强制迁移。保留的关键决策：(1) “强制迁移 MiMo 自身路径” 与 “把 MiMo 抬成跨 provider 默认” 是两件事；(2) 旧 “不静默重定向 `mimo_omni`” 原则被 omni 停用推翻，改为 “必须迁移但带 admin 可见性 / 日志”。
- **2026-05-27 Codex 复核修正（P1，关键）**：官方公告只确认 `mimo-v2-pro` / `mimo-v2-omni` 退役（2026-06-01 自动转发 / 2026-06-30 下线），**未提 `mimo-v2-tts`**；官方 TTS 文档至今仍只支持 `mimo-v2-tts`。据此修正：Phase 0b 收窄为 **omni-only 强制迁移**（deadline 2026-06-30）；TTS 升级降级为 **Phase 3 主动升级**（无 deadline、可回退 v2-tts）。另吸收：(P2) omni 迁移锁定方案 A，若选 B 须连带清理 `transcript_reviewer.py` line 2566/2644；(P3) 回滚摘除 MiMo TTS 须同步改 gateway allowlist `_VALID_EXPRESS_PROVIDERS` / `_VALID_STUDIO_PROVIDERS`（job_intercept.py:348-349）；(P4) omni 日期填实。
- **2026-05-29 执行前复核 + Phase 0b/Phase 1 落地**：
  - (1) **Phase 0b 已实现并通过测试**（`mimo_omni`→`mimo-v2.5` 方案 A，`tests/test_mimo_omni_migration.py` 7 条 + `tests/test_transcript_reviewer.py` 62 条全绿）。
  - (2) 执行前发现 PR 1 潜在问题——`apply_costs` 对缺失 audio 费率 `or input_price` 回退（25 tokens/s），曾担心"省略 audio 费率"会伪造 audio 成本。**查官网确认 MiMo 未单列音频计费、audio 按 input token 计价**，该回退实为正确行为，PR 1 解除阻塞。
  - (3) **Phase 1 已实现并通过测试**（catalog 增 mimo-v2.5/pro + cost_hint RMB 化，未动 cost_rank，`tests/test_cost_management.py` +3 条全绿）。
  - (4) 官网确认 TTS（含 v2.5-tts / voiceclone / voicedesign / v2-tts）全部"限时免费"、无失效日期 → Phase 3 promotional 处理不变。
  - (5) 旁注：`tests/test_smart_cost_summary_writer.py::test_quota_brake_handoff_writes_cost_summary` 预存在失败（扫 `process.py` 找 `smart_handoff_quota_unavailable` marker，该 marker 已不在源码），与本次改动无关，已开独立任务排查。
- **2026-05-29 spike + 部署美国主机**：
  - Phase 0a spike 在美国主机 `docker exec` 跑（非侵入），确认 usage 字段形状并落 fixtures（见 `tests/fixtures/provider_responses/`）；关键约束：`prompt_tokens` 含 cached+audio，PR 2 须拆分避免双重计费。
  - Phase 0b + Phase 1 **已部署美国主机并运行态验证**（详见各自实施状态块）。部署纪律：先等在飞 Express job 结束才 restart app；gateway 用 `--no-deps --force-recreate` 不波及 app；两文件均留 `.bak-20260529` 回滚点。
  - ⚠️ **仓库 drift 提醒**：生产已是新代码，但本地改动 **尚未 commit**（用户未授权 commit）。本地仓库与生产一致、与 git HEAD 不一致；需尽快 commit 以消除 drift（`llm_registry.py` / `cost_management.py` / 两个测试文件 / fixtures / 本 plan）。

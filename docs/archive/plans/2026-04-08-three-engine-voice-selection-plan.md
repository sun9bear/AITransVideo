# 三引擎音色选择 + 自由切换实施方案

> 日期：2026-04-08
> 前置：CosyVoice 统一音色匹配模块已完成，MiniMax 604 音色已导入 DB

---

## 1. 背景与目标

Studio 模式的音色确认阶段（`voice_selection_review`）当前只展示管理后台设置的那一个 TTS 引擎的音色。需要改为：
- 管理后台加 CosyVoice 选项
- 音色确认阶段可在 MiniMax / CosyVoice / 豆包 三个引擎之间自由切换
- 每个说话人可独立选择不同引擎的音色（混用）
- 所有音色可试听

## 2. 当前真实代码基线

| 组件 | 现状 |
|------|------|
| 管理后台 TTS 引擎选项 | minimax / volcengine（无 cosyvoice） |
| `_VALID_STUDIO_PROVIDERS` | `{"minimax", "mimo", "volcengine"}`（`gateway/job_intercept.py:64`） |
| payload builder | 只构建单 provider 的 `available_voices`（`process.py:1705-1762`） |
| 试听 | 支持 VolcEngine + MiniMax clone（`review_actions.py:255`），不支持 CosyVoice / MiniMax 官方音色 |
| 审批 | per-speaker `voice_id` + `voice_source`，无 `tts_provider`（`review_actions.py:318`） |
| TTS dispatch | job 全局 provider（`tts_generator.py:731`），不支持 segment 级 |
| 前端面板 | 全局 `ttsProvider` 状态（`VoiceSelectionPanel.tsx:61`），无 Tab 切换 |
| `presentation.ts` | `buildNativeReviewRoute()` 需确认 `voice_selection_review` 已纳入路由 |

## 3. 范围与非目标

### 范围
- 三引擎 payload 预计算 + 前端 Tab 切换
- per-speaker provider 选择 + 审批 + 运行时 override
- 三引擎试听（CosyVoice 新增 + MiniMax 官方音色新增）
- provider 维度 metering / observability 可见

### 非目标
- **不改 credits 真值** — 不改 `credits_service.py` 的 `DEBIT_RATES`，不新增 per-provider 扣点规则
- **不改用户侧 estimate** — 前端不显示 per-provider 成本差异
- **不改 `voice_review` 旧阶段** — `voice_review` 保留为历史恢复/fallback 阶段
- **不做 MiniMax turbo vs HD 切换** — 本轮 MiniMax 统一用 job 级 `tts_model`

## 4. 关键定义

| 术语 | 职责 |
|------|------|
| `voice_selection_review` | Studio 常规音色选择阶段。负责多 speaker 选音、试听、可选 clone、per-speaker provider 选择。**本轮三引擎主路径。** |
| `voice_review` | 旧 recovery/fallback 阶段。只处理历史短样本/恢复性场景，不承载新三引擎路径。 |

## 5. 向后兼容约定

- payload 保留旧字段 `tts_provider` + `available_voices`，新增 `all_providers` + `auto_matched_by_provider`
- 前端检测：有 `all_providers` → 三 Tab 模式；无 → 单 provider 模式（旧 payload 正常渲染）
- 审批 payload：`tts_provider` per-speaker 可选。缺失时回退 job 全局 `tts_provider`
- `segment.tts_provider` 可选。缺失时回退 `_generate_one()` 的 `provider` 参数 → `get_tts_provider()`

---

## 6. 分阶段实施方案

### Phase 0: 协议收口与兼容

**目标：** 确认 stage 路由、前端 CTA 路径、旧 payload 兼容。

| 改动 | 文件 | 行号 |
|------|------|------|
| 确认 `voice_selection_review` 在 `buildNativeReviewRoute()` 中有 CTA 路由 | `frontend-next/src/features/jobs/presentation.ts` | ~218 |
| 在代码注释中明确 `voice_review` vs `voice_selection_review` 职责 | `src/services/review_state.py` | stage 常量附近 |

**验收：** 现有 Studio 流程不回归，`voice_selection_review` CTA 能正常跳转。

---

### Phase 1: 管理后台加 CosyVoice

**目标：** 允许 cosyvoice 作为 Studio 默认 provider。

| 改动 | 文件 |
|------|------|
| `_VALID_STUDIO_PROVIDERS` 加 `"cosyvoice"` | `gateway/job_intercept.py:64` |
| Studio TTS 引擎列表加 CosyVoice 选项 | `frontend-next/src/app/(app)/admin/settings/page.tsx:47-48` |

注：Provider 文案只作为管理配置说明，不作为 credits / pricing 真值。

**验收：** 管理后台可选 CosyVoice，保存后 `admin_settings.json` 写入 `studio_tts_provider: "cosyvoice"`。

---

### Phase 2: Payload 构建三引擎数据

**目标：** `_build_voice_selection_review_payload()` 同时产出三引擎的 voices + auto_match。

**Payload 结构：**
```python
{
    # --- 旧字段（向后兼容，默认引擎） ---
    "tts_provider": "minimax",
    "available_voices": [...],

    # --- 新字段（三引擎） ---
    "all_providers": {
        "minimax": {
            "label": "MiniMax Speech 2.8",
            "available_voices": [...],
            "supports_clone": True,
        },
        "cosyvoice": {
            "label": "CosyVoice（阿里百炼）",
            "available_voices": [...],
            "supports_clone": False,
        },
        "volcengine": {
            "label": "豆包 2.0",
            "available_voices": [...],
            "supports_clone": False,
        },
    },
    "speakers": [
        {
            "speaker_id": "speaker_a",
            "auto_matched_voice": {...},           # 旧字段（默认引擎匹配）
            "auto_matched_by_provider": {          # 新字段
                "minimax": {"voice_id": ..., "label": ..., "match_confidence": ...},
                "cosyvoice": {"voice_id": ..., "label": ..., "match_confidence": ...},
                "volcengine": {"voice_id": ..., "label": ..., "match_confidence": ...},
            },
            ...
        }
    ],
}
```

**实现：** 抽取 `_build_provider_voices(provider, service_mode)` 工具函数，循环三引擎调用。auto_match 同理。

**验收：**
- `VoiceSelectionPanel.tsx:116` 现有 `available_voices` 读取逻辑不失效
- 新 payload 包含 `all_providers` + `auto_matched_by_provider`

---

### Phase 3: 试听三引擎

**目标：** 准确试听三引擎的音色。先跑通试听再做审批/运行时。

| 改动 | 文件 |
|------|------|
| `preview_voice()` 加显式 `tts_provider` 路由 | `review_actions.py:255` |
| 新增 `_preview_cosyvoice_voice()` | `review_actions.py` |
| 前端 `previewVoice()` 调用带 `tts_provider` | `voiceSelection.ts:72` + `VoiceSelectionPanel.tsx:195` |

```python
def preview_voice(*, voice_id, config_path, tts_provider=None):
    # 显式 provider 优先，不只靠 voice_id 猜
    if tts_provider == "volcengine" or _is_volcengine_voice(voice_id):
        return _preview_volcengine_voice(voice_id)
    if tts_provider == "cosyvoice" or is_cosyvoice_v3_flash_builtin_voice(voice_id):
        return _preview_cosyvoice_voice(voice_id)
    # MiniMax (clone + official catalog)
    return _preview_minimax_voice(voice_id, config_path)
```

**验收：** 三个引擎的音色都能准确试听，不混路由。

---

### Phase 4: 审批与运行时 override

**目标：** 前端提交 per-speaker provider，pipeline 恢复后按 speaker 选择的 provider dispatch TTS。

#### 4.1 审批 payload 扩展

```json
{
    "speakers": [
        {"speaker_id": "speaker_a", "voice_id": "longanwen_v3", "voice_source": "catalog", "tts_provider": "cosyvoice"},
        {"speaker_id": "speaker_b", "voice_id": "English_radiant_girl", "voice_source": "catalog", "tts_provider": "minimax"}
    ]
}
```

#### 4.2 后端保存 + 恢复

| 改动 | 文件 |
|------|------|
| `approve_voice_selection()` 保存 per-speaker `tts_provider` | `review_actions.py:318` |
| `_apply_runtime_voice_overrides()` 同时写 `segment.tts_provider` | `process.py:2094` |
| `_generate_one()` 优先读 `segment.tts_provider` | `tts_generator.py:731` |

```python
# tts_generator._generate_one() provider 解析
provider = getattr(segment, "tts_provider", None) or provider or get_tts_provider()
```

**验收：**
- Speaker A 选 CosyVoice、Speaker B 选 MiniMax → 审批 → TTS 分别 dispatch 正确
- 缺失 `tts_provider` 时回退 job 全局 provider（旧 payload 兼容）

---

### Phase 5: 前端三 Tab 与 speaker 级 provider

**目标：** 最小可用 UI — 三 Tab、下拉切换、审批提交。

#### 5.1 Provider Tab

每个 speaker 卡片顶部显示引擎 Tab：
```
┌─────────────┬─────────────────┬──────────┐
│ MiniMax 2.8 │ CosyVoice 百炼  │  豆包 2.0 │
└─────────────┴─────────────────┴──────────┘
```

- 默认选中 `payload.tts_provider`
- 切换 Tab：音色下拉切换到对应引擎的 `all_providers[prov].available_voices`
- 自动匹配从 `auto_matched_by_provider[prov]` 预填

#### 5.2 状态模型

```typescript
interface SpeakerVoiceState {
  voiceId: string
  voiceSource: 'catalog' | 'cloned' | 'auto_matched'
  selectedProvider: string  // 新增：per-speaker
  isCloning: boolean
  cloneError: string | null
}
```

全局 `ttsProvider` 状态 → 改为 per-speaker `selectedProvider`。

#### 5.3 MiniMax Tab 特有

MiniMax Tab 下额外显示：
- 「我的音色」分组（个人音色库 user_voices）
- 「音色克隆」按钮（现有 VoiceCloneModal 不变）

CosyVoice / 豆包 Tab 不显示克隆功能。

#### 5.4 旧 payload 回退

```typescript
const hasMultiProvider = !!payload.all_providers
if (!hasMultiProvider) {
  // 单 provider 模式：原有逻辑不变
}
```

**验收：**
- 旧单 provider payload 仍能正常渲染
- 新 payload 显示三 Tab
- Speaker 级独立选择，审批提交带 `tts_provider`

---

### Phase 6: 观测补齐（计费后置）

**目标：** provider 维度 metering 可见，不改 credits 真值。

| 改动 | 说明 |
|------|------|
| `metering_snapshot` 记录实际使用 provider | `tts_generator.py` TTSResult 已有 `selected_voice`，确保 provider 可回溯 |
| admin summary 可查 provider 分布 | 后续 follow-up，不阻塞本轮 |

**非目标再次确认：**
- 不改 `credits_service.py` 的 `DEBIT_RATES`
- 不新增前端硬编码计费规则
- 不做 per-provider 扣点规则切换

**后续迭代命名：** `voice-selection-provider-pricing-followup`（不叫 V3-2，避免时序混乱）

---

## 7. 涉及文件清单

### 后端
| 文件 | 改动 |
|------|------|
| `gateway/job_intercept.py` | `_VALID_STUDIO_PROVIDERS` 加 `"cosyvoice"` |
| `src/pipeline/process.py` | payload 三引擎 + `_apply_runtime_voice_overrides` per-speaker provider |
| `src/services/jobs/review_actions.py` | `preview_voice()` 三路 + `approve_voice_selection()` per-speaker |
| `src/services/tts/tts_generator.py` | `_generate_one()` 优先 `segment.tts_provider` |
| `src/services/review_state.py` | stage 职责注释 |

### 前端
| 文件 | 改动 |
|------|------|
| `frontend-next/src/app/(app)/admin/settings/page.tsx` | 加 CosyVoice 选项 |
| `frontend-next/src/features/jobs/presentation.ts` | 确认 `voice_selection_review` 路由 |
| `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx` | 三 Tab + per-speaker provider |
| `frontend-next/src/lib/api/voiceSelection.ts` | `previewVoice` 加 `tts_provider`；审批加 per-speaker `tts_provider` |

---

## 8. 验收标准

1. 管理后台可选 cosyvoice 作为 Studio 默认 provider
2. `voice_selection_review` 默认展示所配 provider，可切到另外两个 provider
3. Speaker A / B 可提交不同 `tts_provider`
4. Pipeline 恢复后，TTS dispatch 按 speaker 选择的 provider 执行
5. 三个 provider 都能试听
6. 旧单 provider payload 仍能在工作台正常渲染
7. 不修改用户侧 credits estimate 真值，不新增前端硬编码计费规则

## 9. 测试组

| 测试 | 覆盖 |
|------|------|
| payload builder | 旧字段兼容 + `all_providers` / `auto_matched_by_provider` 正确 |
| preview_voice | `tts_provider=cosyvoice/volcengine/minimax` 三路分发 |
| approve | 保存 per-speaker `tts_provider` |
| pipeline/process | 恢复并写入 `segment.tts_provider` |
| tts_generator | 优先 `segment.tts_provider`，回退 job 全局 |
| frontend panel | 旧 payload 回退、新 payload 三 Tab、speaker 级 provider 提交 |

## 10. 风险与回退

| 风险 | 缓解 |
|------|------|
| 三引擎 payload 过大 | 三引擎 voices 总量 ~600 条 JSON，<100KB，可接受 |
| CosyVoice international 端点只有 10 个音色 | endpoint 过滤后音色少，但功能不受影响 |
| 混用 provider 的 segment 计费不准 | 本轮只补观测，不改扣点真值 |
| 旧 payload 在进行中的 job | 前端 `hasMultiProvider` 检测回退单 provider 模式 |

---

## 11. 前置完成清单

以下改动已在本轮完成，是本方案的前置依赖：

### 统一音色匹配模块
- `src/services/tts/voice_reranker.py` — provider-agnostic 9 维评分
- `src/services/tts/volcengine_voice_selector.py` — 使用共享 reranker
- `src/services/tts/cosyvoice_voice_selector.py` — `select_cosyvoice_voice_match()`
- `src/services/tts/minimax_voice_selector.py` — 语言预过滤 + combined_rerank
- `src/services/tts/voice_match_resolver.py` — 三 provider dispatch
- `src/services/tts/voice_match_types.py` — 加 `target_language`

### MiniMax 音色库导入
- `src/services/tts/minimax_voice_catalog_604.json` — 604 音色原始数据
- `gateway/scripts/seed_voice_catalog.py` — MiniMax 导入 + 950 traits 关键词映射
- DB: voice_catalog 604 条 + voice_labels 602 条 final 标签（已在 US 主机执行）

### TTS Generator 接入
- `tts_generator._generate_one_cosyvoice()` → 走 `resolve_voice_match()`
- `tts_generator` MiniMax 默认路径 → 走 `resolve_voice_match()`
- `process.py` payload builder → 三 provider 分支

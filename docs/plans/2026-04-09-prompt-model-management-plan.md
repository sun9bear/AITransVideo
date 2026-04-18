# 提示词 & 模型管理方案（v2）

> **Status:** partially-implemented (with gaps)  
> **Last updated:** 2026-04-17（metadata 规整 + 代码审计评估）  
> **Version:** v2（整合讨论反馈）  
> **Implementation:** 核心架构 ~70% 落地，4 条显著 gap 待补（见下方 §0 实施状态评估）

---

## 0. 实施状态评估（2026-04-17 代码审计）

### 已落地（按方案 §9 的 9 步对照）

| 步骤 | 方案要求 | 实际状态 | 证据 |
|------|---------|---------|------|
| 1 | 新建 `llm_registry.py`（MODEL_REGISTRY + 4 函数） | ✅ 完成 | `src/services/llm_registry.py` 279 行；`get_prompt_model` / `get_api_key` / `resolve_model_id` / `get_fallback_candidates` 齐全；多出 `get_available_models_for_prompt` / `get_all_models_with_status` 供前端数据源 |
| 2 | reviewer provider dispatch + 删 `_MODEL_MAP` | ✅ 完成 | `transcript_reviewer.py:1269` 有 `if provider == "mimo" / deepseek / openai` 分发；`_MODEL_MAP` 已删 |
| 3 | reviewer 支持 `skip_pass1` + `mode` 参数 | ✅ 签名已加 | `_orchestrate_three_pass(mode, skip_pass1=False)` 存在 |
| 4 | translator `_call_by_provider()` dispatch | 🟡 部分 | translator.py:868 新路径已建；**但 1028 行保留 legacy LLMRouter fallback chain**，方案 §5.4 要求路由决策层被取代 |
| 5 | process.py 传 mode（原方案还要求 Express 传 `skip_pass1=True`，**已废止**） | ✅ 完成 | 后期决策推翻"Express 跳 Pass 1"，两模式都跑 Pass 1 以保留 ASR 标注纠正。`process.py:964 skip_pass1=False` 是有意硬编码，方案 §4 的"快捷版跳 Pass 1"章节已过时，保留是为了记录决策演化 |
| 6 | Gateway API 扩展 | ✅ 完成 | `gateway/admin_settings.py` 返回 `prompt_models` / `provider_api_keys` / `api_key_status` / `available_models`；key 写入协议（脱敏值拒绝 / 空串清空）到位 |
| 7 | 前端 prompts 页面（Tab + 模型下拉 + Key 管理） | ✅ 完成 | `frontend-next/src/app/(app)/admin/prompts/page.tsx` 420+ 行，studio/express Tab、provider_api_keys 编辑齐全 |
| 8 | settings 页面移除旧 `review_model` / `translation_model` 下拉 | 🟡 部分 | 下拉 UI 已移除（description 引导去 prompts 页），但 interface 的 `review_model` / `translation_model` 字段 + 默认值（line 8-9, 35-36）**残留**；line 236 仍提"使用上面设置的默认翻译模型" |
| 9 | 旧读取点清理（§8.1 共 6 条） | ❌ **大部分未清理** | 见下方"Gap 2" |

### Gap 明细（方案宣传价值未兑现的点）

**Gap 1 已作废**（2026-04-17 用户澄清）  
方案 §4 "快捷版跳过 Pass 1" 是方案草稿阶段的设想，后期决策推翻 — 两模式都跑 Pass 1 以保留 ASR 说话人标注纠正。`process.py:964 skip_pass1=False` 是有意为之，不是实施缺失。方案 §4 章节因而过时，应该删掉或标为"已被后期决策推翻"，保留只做演化记录。

**Gap 2（架构清理未竟）— §8.1 旧读取点仅清一半**

| §8.1 清单项 | 方案要求 | 实际 |
|-------------|---------|------|
| `process.py:122 _get_default_translation_model()` | 改调 `get_prompt_model(mode, "translate")` | 函数仍存在（line 136），line 1139 仍在调 |
| `transcript_reviewer.py:226 _get_review_model()` | 删除 | 仍存在（line 238） |
| `admin_settings.py AdminSettings.review_model/translation_model` 字段 | 保留但不读写 | 前端 settings interface 仍保留字段+默认值 |
| `transcript_reviewer.py _MODEL_MAP` | 删除 | ✅ 已删 |
| `llm/router.py DEFAULT_LLM_MODELS` | 不再被使用 | ❌ LLMRouter 仍被 translator `_call_by_translate_or_rewrite()` legacy 路径调用 |
| 前端 settings 审校/翻译模型下拉 | 移除 | 🟡 UI 下拉已撤，但底层 interface 字段残留 |

- 影响：新旧逻辑并存 = 后台配置和运行时行为"哪个真值生效"不透明
- 风险：如果某天 `admin_settings.json` 保留的旧 `review_model` 和新 `prompt_models` 冲突，行为不可预测

**Gap 3（语义错误）— 自动降级顺序与方案完全相反**
- 方案 §3.3 原话：
  > 3. 按 cost_rank **降序**排列（优先选质量最接近的）  
  > 4. 逐个尝试，直到成功或全部失败
- 实际实现（`transcript_reviewer.py:1017-1019, 1325-1327, 1668-1670`）：
  ```python
  _fallback_models = get_fallback_candidates(review_model, requires_audio=...)
  _cheapest = _fallback_models[-1] if _fallback_models else None       # 最便宜
  _second_cheapest = _fallback_models[-2] if len(...) >= 2 else None   # 第二便宜
  # 后续只尝试这 2 个
  ```
- 问题 1：**只尝试 2 个候选**，不是方案说的"逐个尝试直到成功或全部失败"
- 问题 2：**从最便宜开始尝试**（`[-1]` 是 candidates 降序列表的末尾 = 最便宜），与方案"优先质量最接近"正相反
- 实际后果：Gemini Pro 失败 → 直接用 MiMo（免费）或 DeepSeek，跳过了 Gemini Flash Lite 这种合理的降级目标
- 用户视角：高价模型失败时，产出质量陡降而不是 graceful degradation

**Gap 4（废弃不彻底）— LLMRouter 仍在翻译主路径中**
- 方案 §5.4 原话："路由决策层被 llm_registry 取代；Provider 调用层保留复用"
- 实际 `translator.py:1028` 注释："Legacy path: LLMRouter fallback chain (for unmapped tasks)"
- 新旧路径共存，"未被 prompt_key 映射"时走老路——这个条件判断本身说明方案的"完全取代"在实现时打了折扣

### 修复优先级（Gap 1 已作废后）

| # | Gap | 修复成本 | 影响面 |
|---|-----|---------|-------|
| 3 | fallback 顺序 + 只取 2 个 | ~15 行，改 3 处 fallback 逻辑 | 降级行为的用户体验正确性 |
| 2 | 清理旧读取点 | ~30 行分散在 3 文件 | 真值一致性 + 长期可维护性 |
| 4 | LLMRouter 路由决策层真正下线 | 翻译流程 audit + 清理 router.py | 与方案 §5.4 一致性（不致命，见单独评估） |

建议按 3 → 2 → 4 顺序修。Gap 3 决定降级体验对错；Gap 2/4 是架构清理，可归入下一轮 cleanup。

**另需**：方案 §4（快捷版跳过 Pass 1）在下一次维护时整段删除或归档到"演化历史"小节，避免误导未来会话以为这是实施缺失。

---

---

## 1. 目标

在管理员后台的「提示词管理」页面中：
1. **工作台版 / 快捷版分别设置模型**（每个提示词独立选模型）
2. **每个 Provider 独立配 API Key**（可选覆盖，留空用全局环境变量）
3. **快捷版优化流程**：跳过 Pass 1（说话人识别），降低成本和延迟
4. **自动降级**：指定模型失败时，自动选择更便宜的同能力模型重试
5. 所有配置实时生效，不需要重启容器
6. **完全取代**旧的 `review_model` / `translation_model` 全局字段

---

## 2. 当前状态

### 2.1 模型使用现状

| 提示词 | 当前模型决定方式 | 可选模型 | API Key |
|-------|--------------|---------|---------|
| Pass 1 说话人识别 | `admin_settings.review_model` 全局共享 | gemini_pro / gemini / mimo_omni | GEMINI_API_KEY 或 MIMO_API_KEY |
| Pass 2 文本修正 | 同 Pass 1 | 同上（纯文本即可） | 同上 |
| Pass 3 音色画像 | 同 Pass 1 | 同上（需要音频） | 同上 |
| 翻译 | LLMRouter fallback chain | gemini / deepseek / openai | 各 provider 的 env var |
| 重写 | 同翻译（复用 GeminiTranslator） | 同翻译 | 同翻译 |

### 2.2 系统已接入的 LLM Provider

| Provider | 模型 | API Key 环境变量 | 支持音频 | 用途 |
|----------|------|----------------|:-------:|------|
| **Gemini** | gemini-3.1-pro-preview | GEMINI_API_KEY | ✅ | 审校/翻译/重写 |
| **Gemini** | gemini-2.5-flash-lite | GEMINI_API_KEY | ✅ | 审校/翻译（低成本） |
| **DeepSeek** | deepseek-chat | DEEPSEEK_API_KEY | ❌ | 翻译/重写 |
| **OpenAI** | gpt-4.1 | OPENAI_API_KEY | ❌ | 翻译/重写 |
| **OpenAI** | gpt-5.4 | OPENAI_API_KEY | ❌ | 翻译/重写（高质量） |
| **MiMo Omni** | mimo-v2-omni | MIMO_API_KEY | ❌ | 纯文本审校/翻译（免费） |

> **变更**：移除 Anthropic（claude-sonnet-4-6）和 GPT-4o。
> **GPT-4o 为何移除**：OpenAI 官方模型页标注 `Audio: Not supported`，旧的 `gpt-4o-audio-preview` 将于 2026-05-07 下线。且当前 `OpenAIProvider` 是纯文本架构，接入音频需全新调用路径，不在本次范围内。
> **多模态（音频）模型**：仅 Gemini 两个型号可用（gemini_pro / gemini），Pass 1/3 无 OpenAI 备选。

### 2.3 模型约束

| 提示词 | 约束 | 可选模型 | 原因 |
|-------|------|---------|------|
| Pass 1 | **必须支持音频输入** | 仅 Gemini（gemini_pro / gemini） | 需要听音频判断说话人，当前仅 Gemini SDK 支持音频上传 |
| Pass 2 | 纯文本即可 | 全部 6 个模型 | 只处理文本 |
| Pass 3 | **必须支持音频输入** | 仅 Gemini（gemini_pro / gemini） | 需要听音频片段做音色画像 |
| 翻译 | 纯文本即可 | 全部 6 个模型 | 只处理文本 |
| 重写 | 纯文本即可 | 全部 6 个模型 | 只处理文本 |

> **为什么没有 OpenAI 音频选项**：GPT-4o 官方标注 `Audio: Not supported`，旧 `gpt-4o-audio-preview` 将于 2026-05-07 下线。且当前 `OpenAIProvider` 是纯文本架构（`prompt: str`），与 Gemini 的音频上传（`client.files.upload` + `Part.from_bytes`）完全不兼容，接入需全新调用路径，不在本次范围。

### 2.4 废弃项

| 组件 | 处理方式 |
|------|---------|
| `admin_settings.review_model` | 被 `prompt_models` 取代，字段保留但不再读取 |
| `admin_settings.translation_model` | 被 `prompt_models` 取代，字段保留但不再读取 |
| `LLMRouter` fallback chain（翻译路由） | 被 per-prompt 模型选择 + 自动降级取代 |
| 前端设置页的审校模型/翻译模型下拉 | 移除，统一到提示词管理页面 |

---

## 3. 数据结构设计

### 3.1 admin_settings.json 扩展

在现有 `review_prompts` 同级新增 `prompt_models` 和 `provider_api_keys`：

```json
{
  "...": "...现有字段保持不变...",

  "review_prompts": {
    "pass1": "自定义提示词...",
    "pass2": "",
    "pass3": "",
    "translate": "",
    "rewrite": ""
  },

  "prompt_models": {
    "studio": {
      "pass1": "gemini_pro",
      "pass2": "gemini",
      "pass3": "gemini_pro",
      "translate": "deepseek",
      "rewrite": "deepseek"
    },
    "express": {
      "pass2": "gemini",
      "pass3": "gemini",
      "translate": "deepseek",
      "rewrite": "deepseek"
    }
  },

  "provider_api_keys": {
    "deepseek": "",
    "openai": "",
    "mimo": ""
  }
}
```

说明：
- **`prompt_models`**：工作台版 / 快捷版分别设置。快捷版没有 `pass1`（跳过说话人识别）
- **`provider_api_keys`**：按 provider 维度覆盖，不按 prompt 维度（避免重复配置同一个 key）。留空 = 使用环境变量。**Gemini 不在此列**——后台不管理 Gemini 凭据，Gemini 继续走现有 `client_factory.py` 的凭据优先级（`GOOGLE_APPLICATION_CREDENTIALS` → `VERTEX_AI_EXPRESS_KEY` → `GEMINI_API_KEY`）
- 不再读取旧的 `review_model` / `translation_model` 字段

### 3.2 模型注册表（新文件：`src/services/llm_registry.py`）

```python
MODEL_REGISTRY: dict[str, dict] = {
    # Gemini 系列（支持音频）
    # 认证方式：走 client_factory.create_gemini_client() 统一处理
    # 支持三种凭据：GOOGLE_APPLICATION_CREDENTIALS → VERTEX_AI_EXPRESS_KEY → GEMINI_API_KEY
    # 不走 provider_api_keys，后台不管理 Gemini 凭据
    "gemini_pro": {
        "api_model_id": "gemini-3.1-pro-preview",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "vertex_ai",  # 标记：走 service account，不走 api_key
        "cost_rank": 5,
        "label": "Gemini 3.1 Pro（高质量）",
        "cost_hint": "¥2.4/h 音频",
    },
    "gemini": {
        "api_model_id": "gemini-2.5-flash-lite",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "vertex_ai",
        "cost_rank": 2,
        "label": "Gemini 2.5 Flash Lite（低成本）",
        "cost_hint": "¥0.27/h 音频",
    },
    # DeepSeek（纯文本）
    "deepseek": {
        "api_model_id": "deepseek-chat",
        "provider": "deepseek",
        "supports_audio": False,
        "api_key_env": "DEEPSEEK_API_KEY",
        "cost_rank": 3,
        "label": "DeepSeek Chat",
        "cost_hint": "¥1/百万 token",
    },
    # OpenAI 纯文本系列
    "openai": {
        "api_model_id": "gpt-4.1",
        "provider": "openai",
        "supports_audio": False,
        "api_key_env": "OPENAI_API_KEY",
        "cost_rank": 4,
        "label": "GPT-4.1",
        "cost_hint": "¥0.15/千 token",
    },
    "gpt54": {
        "api_model_id": "gpt-5.4",
        "provider": "openai",
        "supports_audio": False,
        "api_key_env": "OPENAI_API_KEY",
        "cost_rank": 6,
        "label": "GPT-5.4（高质量）",
        "cost_hint": "约 ¥0.5/千 token",
    },
    # MiMo（纯文本，免费）
    "mimo_omni": {
        "api_model_id": "mimo-v2-omni",
        "provider": "mimo",
        "supports_audio": False,
        "api_key_env": "MIMO_API_KEY",
        "cost_rank": 1,
        "label": "MiMo-V2-Omni（免费）",
        "cost_hint": "免费",
    },
}
```

### 3.3 自动降级策略

指定模型调用失败时，按 `cost_rank` 自动选择更便宜的模型重试：

1. 获取当前模型的 `cost_rank` 和 `supports_audio`
2. 从注册表中筛选：`cost_rank < 当前` 且能力匹配（音频 prompt 只选 `supports_audio=True`）
3. 按 `cost_rank` **降序**排列（优先选质量最接近的）
4. 逐个尝试，直到成功或全部失败
5. 全部失败 → 报错给用户，不静默吞掉

**示例**：
- Pass 1 用 `gemini_pro`(rank=5) 失败 → 降级到 `gemini`(rank=2, 支持音频) → 再失败 → 报错
- 翻译用 `deepseek`(rank=3) 失败 → 降级到 `gemini`(rank=2) → 降级到 `mimo_omni`(rank=1) → 再失败 → 报错

**原则**：降级链中不会出现比管理员选定模型更贵的模型，符合成本可控要求。

**实现预留**：`get_fallback_candidates()` 支持可选的 `allowed_models: set[str]` 参数，按 prompt 限定兼容降级集合。初期不启用（全局 cost_rank 排序已够用），后续如发现某些模型不适合特定任务（如 MiMo 翻译质量不达标），可按 prompt 缩小降级范围而不改架构。

---

## 4. 快捷版流程优化

### 4.1 跳过 Pass 1（说话人识别）

快捷版 pipeline 直接跳过 Pass 1，不发送音频给 LLM 做说话人分析：

| 步骤 | 工作台版 | 快捷版 |
|------|:------:|:-----:|
| Pass 1 说话人识别 | ✅ 听音频 + 文本分析 | ❌ 跳过 |
| Pass 2 文本修正 | ✅ | ✅ |
| Pass 3 音色画像 | ✅ | ✅ |
| 翻译 | ✅ | ✅ |
| 重写 | ✅ | ✅ |

**显式 Tradeoff（跳过 Pass 1 的代价）**：

| 维度 | 工作台版（有 Pass 1） | 快捷版（跳过 Pass 1） | 影响程度 |
|------|:---------------:|:---------------:|:-------:|
| 说话人姓名 | 从音频推断真实名字 | 停留在 "Speaker A/B" 占位名 | 低（展示用） |
| 说话人标注纠正 | Pass 1 听音频纠正 ASR 错误 | 保留 ASR 原始标注（可能有错） | 中 |
| 翻译质量 | 不受影响（翻译不依赖说话人姓名） | 不受影响 | 无 |
| TTS 音色匹配 | Pass 1 → 注入 segment → voice matcher | **Pass 3 补上真实音色画像** → voice matcher | 低（Pass 3 缓解） |
| 翻译/音色审核 UI | 显示真实名字 | N/A（Express 不显示审核 UI） | 无 |

**缓解措施**：Pass 3 仍然运行，独立听音频片段生成 gender/age/voice_description/persona_style/energy_level。Pipeline 中 Pass 3 在翻译后、TTS 前执行，voice matcher 最终用的是 Pass 3 的真实画像，而非 Pass 1 fallback 的粗糙默认值。

**可接受的原因**：快捷版面向"快速出结果"场景，用户不会看到审核 UI，不关心说话人姓名。ASR 标注纠正的缺失是唯一实质退化，但对大多数视频（1-2 个说话人）影响有限。

**改动点**：
- `_orchestrate_three_pass()` 新增 `skip_pass1: bool` 参数
- 跳过时用 ASR 原始 speaker label 构造最小 speakers dict：`{"speaker_a": {"name": "Speaker A"}, ...}`
- `process.py` 中 Express 模式传 `skip_pass1=True`

**成本节约**：每次快捷版任务省去 1 次音频 LLM 调用（gemini_pro ≈ ¥0.05/10min 视频）。

---

## 5. 后端改动

### 5.1 新增：共享模块 `src/services/llm_registry.py`

**为什么不放在 `transcript_reviewer.py`**：reviewer、translator 都需要用，放 reviewer 里会造成 translator 反向依赖 reviewer 的耦合。

```python
"""LLM model registry + per-prompt model/key resolution."""

import json, os, time
from pathlib import Path

MODEL_REGISTRY = { ... }  # 见 3.2

_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")
_cache: dict | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0  # 5 秒失效，避免单次 pipeline 重复读文件

def _load_settings() -> dict:
    """带 TTL 缓存的 settings 读取。"""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache
    try:
        if _SETTINGS_PATH.exists():
            _cache = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            _cache_ts = now
            return _cache
    except Exception:
        pass
    return {}

def get_prompt_model(mode: str, prompt_key: str) -> str:
    """获取指定模式 + 提示词的模型。
    
    mode: "studio" | "express"
    prompt_key: "pass1" | "pass2" | "pass3" | "translate" | "rewrite"
    """
    settings = _load_settings()
    models = settings.get("prompt_models", {}).get(mode, {})
    model = models.get(prompt_key, "")
    if model and model in MODEL_REGISTRY:
        return model
    # 默认值
    defaults = {
        "pass1": "gemini_pro", "pass3": "gemini_pro",
        "pass2": "gemini", "translate": "deepseek", "rewrite": "deepseek",
    }
    return defaults.get(prompt_key, "gemini")

def get_api_key(model_name: str) -> str | None:
    """获取模型对应的 API Key。
    
    Gemini 返回 None（走 Vertex AI service account，由 client_factory 处理）。
    其他 provider：优先 provider_api_keys 覆盖，否则环境变量。
    """
    model_info = MODEL_REGISTRY.get(model_name, {})
    # Gemini 走 Vertex AI service account，不需要 API key
    if model_info.get("auth") == "vertex_ai":
        return None
    provider = model_info.get("provider", "")
    # 先查 per-provider 覆盖
    settings = _load_settings()
    provider_keys = settings.get("provider_api_keys", {})
    override = provider_keys.get(provider, "")
    if override:
        return override
    # 回退到环境变量
    env_var = model_info.get("api_key_env", "")
    return os.environ.get(env_var, "").strip() if env_var else ""

def resolve_model_id(logical_name: str) -> str:
    """逻辑名 → API model ID。"""
    return MODEL_REGISTRY.get(logical_name, {}).get("api_model_id", logical_name)

def get_fallback_candidates(model_name: str, requires_audio: bool) -> list[str]:
    """获取降级候选列表（cost_rank < 当前，能力匹配，按 rank 降序）。"""
    current = MODEL_REGISTRY.get(model_name, {})
    current_rank = current.get("cost_rank", 99)
    candidates = []
    for name, info in MODEL_REGISTRY.items():
        if name == model_name:
            continue
        if info.get("cost_rank", 99) >= current_rank:
            continue
        if requires_audio and not info.get("supports_audio"):
            continue
        candidates.append(name)
    candidates.sort(key=lambda n: MODEL_REGISTRY[n]["cost_rank"], reverse=True)
    return candidates
```

### 5.2 修改：Pass 1/2/3 使用 llm_registry + provider dispatch

文件：`src/services/transcript_reviewer.py`

**关键改造**：当前 `_review_pass1_speakers()`、`_review_pass2_text()`、`review_pass3_voice_profiles()` 都直接调 Gemini SDK（`_create_review_client()` → `genai.Client`）。要让 Pass 2 能用 DeepSeek/OpenAI/MiMo，需要在 reviewer 里也加 provider dispatch。

新增 `_call_review_llm()` 统一分发函数（与 translator 的 `_call_by_provider()` 同构）：

```python
from src.services.llm_registry import get_api_key, resolve_model_id, MODEL_REGISTRY

def _call_review_llm(
    *, model_name: str, prompt: str, audio_parts: list | None = None,
    json_mode: bool = True,
) -> str:
    """审校 LLM 统一调用入口，按 provider dispatch。"""
    info = MODEL_REGISTRY[model_name]
    provider = info["provider"]
    api_model_id = info["api_model_id"]

    if provider == "gemini":
        # 走现有 Gemini SDK 路径（支持 audio_parts）
        client = _create_review_client()  # Vertex AI service account
        return _call_gemini_review(client, api_model_id, prompt, audio_parts, json_mode)
    elif provider == "mimo":
        api_key = get_api_key(model_name)
        return _call_review_mimo_omni(api_key=api_key, prompt=prompt, model_id=api_model_id)
    else:
        # DeepSeek / OpenAI — 纯文本，不支持 audio_parts
        if audio_parts:
            raise ValueError(f"{model_name} 不支持音频输入，不能用于 Pass 1/3")
        api_key = get_api_key(model_name)
        return _call_openai_compatible_review(
            provider=provider, api_key=api_key,
            model_id=api_model_id, prompt=prompt, json_mode=json_mode,
        )
```

orchestrator 改为：

```python
def _orchestrate_three_pass(..., mode: str = "studio", skip_pass1: bool = False):
    if skip_pass1:
        # 快捷版：用 ASR 原始 speaker label，不做音频分析
        pass1_result = _build_minimal_speakers(lines)
        pass1_lines = lines
    else:
        pass1_model = get_prompt_model(mode, "pass1")
        pass1_result = _review_pass1_speakers(..., review_model=pass1_model)
        pass1_lines = _apply_corrections(...)

    pass2_model = get_prompt_model(mode, "pass2")
    pass2_result = _review_pass2_text(..., review_model=pass2_model)
    # Pass 2 内部调 _call_review_llm()，如果是 DeepSeek/OpenAI 走纯文本路径
```

**旧代码清理**：reviewer 中的 `_MODEL_MAP` 删除，统一从 `llm_registry.MODEL_REGISTRY` 导入。`_create_review_client()` 改为不接收 `api_key` 参数（Gemini 走 service account）。

### 5.3 修改：翻译/重写使用 llm_registry

文件：`src/services/gemini/translator.py`

新增 `_call_by_provider()` 统一分发方法，按 provider 类型 dispatch：

```python
from src.services.llm_registry import get_prompt_model, get_api_key, resolve_model_id, MODEL_REGISTRY

def _call_by_provider(self, model_name: str, prompt: str, json_mode: bool = False) -> str:
    """按 provider 类型 dispatch 到对应的调用方式。"""
    info = MODEL_REGISTRY[model_name]
    provider = info["provider"]
    api_model_id = info["api_model_id"]
    
    if provider == "gemini":
        # Vertex AI service account，走现有 _call_gemini_with_retry()
        return self._call_gemini_with_retry(prompt, json_mode=json_mode, model_name=api_model_id)
    else:
        # DeepSeek / OpenAI / MiMo — 复用现有 LLMRouter provider 层
        api_key = get_api_key(model_name)
        return self._call_openai_compatible(
            provider=provider, api_key=api_key,
            model_id=api_model_id, prompt=prompt, json_mode=json_mode,
        )
```

- **Gemini**：走现有 `_call_gemini_with_retry()` → `create_gemini_client()`（Vertex AI service account），不需要传 API key
- **DeepSeek / OpenAI**：复用现有 `OpenAIProvider` / `DeepSeekProvider` 的调用逻辑
- **MiMo**：走现有 `_call_review_mimo_omni()` 的 HTTP 调用逻辑
- **失败时**：从 `get_fallback_candidates()` 取下一个模型，递归调用

翻译不再走 LLMRouter fallback chain，改为 llm_registry 决策 + `_call_by_provider()` dispatch。

### 5.4 废弃 LLMRouter 的路由决策层

`src/services/llm/router.py` 的**路由决策层**（`get_route()`、fallback chain）被 `llm_registry` 取代。
**Provider 调用层**（`OpenAIProvider`、`DeepSeekProvider`）保留复用。
- `process.py` 不再构造 `LLMRouter` 实例传给 `GeminiTranslator`
- router.py 文件暂时保留，后续清理

### 5.5 API Key 安全

- **存储**：`provider_api_keys` 存在 `admin_settings.json`，文件权限 600
- **传输**：Gateway API 返回时做脱敏（只返回后 4 位）
- **历史隔离**：提示词版本历史（`review_prompt_history.json`）**不包含 key 字段**，只记录 prompts + models 变更
- **前端**：显示 `****xxxx` 格式，编辑时覆盖写入

---

## 6. Gateway API 改动

文件：`gateway/admin_settings.py`

### 6.1 扩展 GET /api/admin/review-prompts

响应增加 `models`、`provider_api_keys`、`available_models`、`api_key_status` 字段：

```json
{
  "prompts": {"pass1": "", "pass2": "", "pass3": "", "translate": "", "rewrite": ""},
  "defaults": {"pass1": "...", "pass2": "...", ...},

  "models": {
    "studio": {"pass1": "gemini_pro", "pass2": "gemini", "pass3": "gemini_pro", "translate": "deepseek", "rewrite": "deepseek"},
    "express": {"pass2": "gemini", "pass3": "gemini", "translate": "deepseek", "rewrite": "deepseek"}
  },
  "default_models": {
    "studio": {"pass1": "gemini_pro", "pass2": "gemini", "pass3": "gemini_pro", "translate": "deepseek", "rewrite": "deepseek"},
    "express": {"pass2": "gemini", "pass3": "gemini", "translate": "deepseek", "rewrite": "deepseek"}
  },

  "provider_api_keys": {
    "deepseek": "",
    "openai": "",
    "mimo": ""
  },
  "api_key_status": {
    "GOOGLE_APPLICATION_CREDENTIALS": true,
    "DEEPSEEK_API_KEY": true,
    "OPENAI_API_KEY": false,
    "MIMO_API_KEY": true
  },

  "available_models": {
    "pass1": [
      {"value": "gemini_pro", "label": "Gemini 3.1 Pro（高质量）", "cost_hint": "¥2.4/h 音频", "cost_rank": 5},
      {"value": "gemini", "label": "Gemini 2.5 Flash Lite（低成本）", "cost_hint": "¥0.27/h 音频", "cost_rank": 2}
    ],
    "pass2": [
      {"value": "gemini_pro", "label": "Gemini 3.1 Pro（高质量）", "cost_hint": "¥2.4/h 音频", "cost_rank": 5},
      {"value": "gemini", "label": "Gemini 2.5 Flash Lite（低成本）", "cost_hint": "¥0.27/h 音频", "cost_rank": 2},
      {"value": "deepseek", "label": "DeepSeek Chat", "cost_hint": "¥1/百万 token", "cost_rank": 3},
      {"value": "openai", "label": "GPT-4.1", "cost_hint": "¥0.15/千 token", "cost_rank": 4},
      {"value": "gpt54", "label": "GPT-5.4（高质量）", "cost_hint": "约 ¥0.5/千 token", "cost_rank": 6},
      {"value": "mimo_omni", "label": "MiMo-V2-Omni（免费）", "cost_hint": "免费", "cost_rank": 1}
    ],
    "pass3": ["...同 pass1，仅支持音频的模型..."],
    "translate": ["...同 pass2，所有纯文本 + 音频模型..."],
    "rewrite": ["...同 pass2..."]
  },

  "history": [...]
}
```

- `available_models`：Pass 1/3 自动过滤掉不支持音频的模型
- `provider_api_keys`：脱敏后的 key（`****xxxx` 或空）
- `api_key_status`：各环境变量是否已配置（前端显示 ✅/❌）
- `default_models`：前端用于"恢复默认"操作

### 6.2 扩展 POST /api/admin/review-prompts

请求 body 增加 `models` 和 `provider_api_keys`：

```json
{
  "prompts": {"pass1": "...", ...},
  "models": {
    "studio": {"pass1": "gemini_pro", "pass2": "deepseek", ...},
    "express": {"pass2": "deepseek", ...}
  },
  "provider_api_keys": {
    "deepseek": "sk-new-key-xxx",
    "openai": "",
    "mimo": ""
  },
  "label": "版本标签"
}
```

写入 `admin_settings.json` 的 `prompt_models` 和 `provider_api_keys` 字段。

**API Key 写入协议**（`provider_api_keys` 字段）：

| POST 中的值 | 含义 | 行为 |
|------------|------|------|
| 字段不存在 / `null` | 保持原值 | 不修改该 provider 的 key |
| `""` 空字符串 | 清空覆盖 | 删除 admin 覆盖，回退到环境变量 |
| `"sk-xxx..."` 非空字符串 | 覆盖新值 | 写入新 key |
| `"****abcd"` 脱敏格式 | **拒绝** | Gateway 检测到脱敏格式，返回 400 错误 |

前端保存时：未修改的 key 不发送（保持原值），用户点「清除」发空字符串，用户输入新值发原文。**绝不回传脱敏值。**

### 6.3 版本历史：prompts + models 联合回滚

版本历史快照结构扩展为同时记录 prompts 和 models（**不记录 key**）：

```json
{
  "saved_at": "2026-04-09T12:00:00Z",
  "label": "版本 3",
  "prompts": {"pass1": "...", "pass2": "...", ...},
  "models": {
    "studio": {"pass1": "gemini_pro", ...},
    "express": {"pass2": "gemini", ...}
  }
}
```

**restore 行为**：恢复版本时同时恢复 prompts 和 models，不恢复 key。
**delete 行为**：删除历史条目，不影响当前配置。
**向前兼容**：旧历史条目没有 `models` 字段时，restore 只恢复 prompts，models 保持当前值。

---

## 7. 前端改动

文件：`frontend-next/src/app/(app)/admin/prompts/page.tsx`

### 7.1 页面结构：工作台版 / 快捷版 Tab

```
┌─ [工作台版] [快捷版] ─────────────────────────────────┐
│                                                       │
│ ── 工作台版模型配置 ──                                  │
│                                                       │
│ ┌── Pass 1 - 说话人识别 ─────────────────────────────┐ │
│ │ 模型: [Gemini 3.1 Pro ▾]  ¥2.4/h 音频             │ │
│ │ ┌─────────────────────────────────────────────┐   │ │
│ │ │ 提示词内容...                                │   │ │
│ │ └─────────────────────────────────────────────┘   │ │
│ │ [恢复为系统默认]                                   │ │
│ └────────────────────────────────────────────────────┘ │
│                                                       │
│ ┌── Pass 2 - 文本修正 ───────────────────────────────┐ │
│ │ 模型: [DeepSeek Chat ▾]  ¥1/百万 token             │ │
│ │ ...                                                │ │
│ └────────────────────────────────────────────────────┘ │
│                                                       │
│ ┌── Pass 3 / 翻译 / 重写 ... ────────────────────────┐ │
│ └────────────────────────────────────────────────────┘ │
│                                                       │
│ ── API Key 管理 ──                                     │
│                                                       │
│ Gemini:   由 client_factory 管理     ✅ 已配置          │
│ DeepSeek: [未设置]   [修改]         ✅ 环境变量已配置   │
│ OpenAI:   [未设置]   [修改]         ❌ 环境变量未配置   │
│ MiMo:     [****efgh] [修改] [清除]  ✅ 环境变量已配置   │
│                                                       │
└───────────────────────────────────────────────────────┘
```

### 7.2 快捷版 Tab

- 不显示 Pass 1 卡片（快捷版跳过说话人识别）
- 显示 Pass 2 / Pass 3 / 翻译 / 重写，各有独立的模型下拉
- 提示词与工作台版共享（同一个 prompt 模板，不分 mode）
- API Key 管理区与工作台版共享（同一个 provider_api_keys）

### 7.3 模型下拉列表

- Pass 1/3：只显示支持音频的模型（Gemini 系列）
- Pass 2 / 翻译 / 重写：显示所有模型
- 每个选项附带成本提示（`cost_hint`）
- MiMo 在纯文本 prompt 的下拉列表中可选（免费选项）

### 7.4 API Key 编辑交互

- 按 **Provider 维度**管理，不按 prompt 维度
- 默认显示 `****xxxx`（脱敏）或 "未设置"
- 点「修改」→ 弹出输入框，输入新 key
- 点「清除」→ 恢复使用全局环境变量
- ✅/❌ 标识各 provider 的环境变量是否已配置
- 保存时 key 随 prompts / models 一起提交

---

## 8. 迁移策略

首次加载时，如果 `prompt_models` 不存在：
1. 从旧 `review_model` 字段读取值，填入 `prompt_models.studio.pass1/2/3` + `prompt_models.express.pass2/3`
2. 从旧 `translation_model` 字段读取值，填入 `prompt_models.studio.translate/rewrite` + `prompt_models.express.translate/rewrite`
3. 旧字段保留但不再读取（向前兼容旧版 Gateway 回滚）

### 8.1 旧读取点清理清单（防真值残留）

实施时**必须**清除以下旧入口，否则会出现后台新配置和运行时行为不一致：

| 文件 | 旧读取点 | 处理方式 |
|------|---------|---------|
| `src/pipeline/process.py:122` | `_get_default_translation_model()` 读旧 `translation_model` | 改为调 `llm_registry.get_prompt_model(mode, "translate")` |
| `src/services/transcript_reviewer.py:226` | `_get_review_model()` 读旧 `review_model` | 删除，改为 orchestrator 传入 `get_prompt_model(mode, "pass1/2/3")` |
| `gateway/admin_settings.py:35` | `AdminSettings.review_model` / `translation_model` 字段 | 字段保留（兼容回滚），但新的 GET/POST 逻辑不再读写这两个字段 |
| `frontend-next/src/app/(app)/admin/settings/page.tsx` | 审校模型 / 翻译模型下拉 | 移除这两个下拉，统一到提示词管理页面 |
| `src/services/transcript_reviewer.py:31` | `_MODEL_MAP` | 删除，统一从 `llm_registry.MODEL_REGISTRY` 导入 |
| `src/services/llm/router.py:34` | `DEFAULT_LLM_MODELS` | 不再被翻译流程使用，文件保留但标记废弃 |

---

## 9. 实施步骤

| 步骤 | 内容 | 改动文件 |
|------|------|---------|
| 1 | 新建 `llm_registry.py`：MODEL_REGISTRY + get_prompt_model + get_api_key + fallback + resolve_model_id | `src/services/llm_registry.py`（新增） |
| 2 | reviewer 加 `_call_review_llm()` provider dispatch，删除 `_MODEL_MAP`，改用 llm_registry | `src/services/transcript_reviewer.py` |
| 3 | reviewer 支持 skip_pass1 + mode 参数 | `src/services/transcript_reviewer.py` |
| 4 | translator 加 `_call_by_provider()` dispatch，移除 LLMRouter 路由依赖 | `src/services/gemini/translator.py` |
| 5 | process.py 传入 mode + skip_pass1，Express 跳过 Pass 1 | `src/pipeline/process.py` |
| 6 | Gateway API 扩展：models/keys/available_models + key 写入协议 + history 联合回滚 | `gateway/admin_settings.py` |
| 7 | 前端：工作台/快捷 Tab + 模型下拉 + Provider Key 管理 | `frontend-next/src/app/(app)/admin/prompts/page.tsx` |
| 8 | 前端设置页：移除旧的 review_model / translation_model 下拉 | `frontend-next/src/app/(app)/admin/settings/page.tsx` |
| 9 | 测试验证 + 部署 | - |

---

## 10. 不改的

- 不改 TTS 引擎选择（已有独立配置）
- 不改 ASR（AssemblyAI）配置
- 不改环境变量体系（admin override 是叠加层，不替代 .env）
- `LLMRouter` 文件保留但不再被翻译流程使用，后续清理

---

## 11. 安全考虑

- API Key 在 `admin_settings.json` 中明文存储，文件权限 600
- Gateway API 返回时脱敏（只返回后 4 位）
- **版本历史不记录 key**（history 快照只含 prompts + models）
- 前端不在 URL 参数或 localStorage 中存储 key
- 审计日志记录谁修改了 key（已有 admin audit log 基础设施）

---

## 12. 成本对比参考

### 12.1 单次调用成本（10 分钟视频）

| 模型 | cost_rank | Pass 1 | Pass 2 | Pass 3 | 翻译 (50 seg) | 重写 (10 seg) |
|------|:---------:|:---:|:---:|:---:|:---:|:---:|
| MiMo-V2-Omni | 1 | N/A | 免费 | N/A | 免费 | 免费 |
| Gemini 2.5 Flash Lite | 2 | ¥0.007 | ¥0.003 | ¥0.005 | ¥0.005 | ¥0.002 |
| DeepSeek Chat | 3 | N/A | ¥0.001 | N/A | ¥0.002 | ¥0.001 |
| GPT-4.1 | 4 | N/A | ¥0.010 | N/A | ¥0.020 | ¥0.008 |
| Gemini 3.1 Pro | 5 | ¥0.050 | ¥0.015 | ¥0.030 | ¥0.030 | ¥0.010 |
| GPT-5.4 | 6 | N/A | ¥0.025 | N/A | ¥0.050 | ¥0.020 |

### 12.2 推荐组合

**工作台版 — 高质量**：
- Pass1=gemini_pro, Pass2=gemini_pro, Pass3=gemini_pro, 翻译=gemini_pro, 重写=gemini_pro
- 总计 ≈ ¥0.135 / 10min 视频

**工作台版 — 性价比**：
- Pass1=gemini, Pass2=deepseek, Pass3=gemini, 翻译=deepseek, 重写=deepseek
- 总计 ≈ ¥0.015 / 10min 视频

**快捷版 — 最低成本**（跳过 Pass 1）：
- Pass2=deepseek, Pass3=gemini, 翻译=deepseek, 重写=deepseek
- 总计 ≈ ¥0.008 / 10min 视频（比工作台性价比方案再省 ~47%）

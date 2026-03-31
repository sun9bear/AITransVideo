# TTS 供应商最终架构建议（人民币版）

更新时间：2026-03-27

## 目标

为 AIVideoTrans 的两类任务方案选择更合适的 TTS 供应商：

- 快捷版：低成本、自动化、稳定、适合免费与轻量付费用户
- 工作台版：更高质量、可审校、可克隆音色、适合 Plus / Pro / Admin

同时综合考虑：

- 单位成本
- 中文场景效果
- 男/女声控制稳定性
- 音色克隆能力
- 输出速率与接口限流
- 当前项目的分段 TTS 执行方式

## 结论

推荐采用以下最终架构：

- 快捷版：阿里云 CosyVoice `v3-flash`
- 工作台版（Plus）：MiniMax `speech-2.8-turbo`
- 工作台版（Pro / Admin）：MiniMax `speech-2.8-hd`
- MiMo：降级为内部实验 / 备用 provider，不再作为生产默认

## 推荐映射

| 用户等级 | 可用方案 | 推荐 TTS 与流程 | 限制 |
|---|---|---|---|
| Free | 仅快捷版 | CosyVoice `v3-flash` + 全自动流程 + 无审核 | 10 分钟/条，5 条总免费额度，同时进行 1 条 |
| Plus | 快捷版 + 工作台版 | 快捷版 = CosyVoice `v3-flash`；工作台版 = MiniMax `speech-2.8-turbo` + 审核流程 + 可克隆 | 60 分钟/条，同时进行 3 条 |
| Pro | 快捷版 + 工作台版 | 快捷版 = CosyVoice `v3-flash`；工作台版 = MiniMax `speech-2.8-hd` + 审核流程 + 可克隆 | 180 分钟/条，同时进行 10 条 |
| Admin | 快捷版 + 工作台版 | 默认同 Pro；后台可切 Turbo / HD；可管理后台设置 | 无时长、无并发、无额度限制 |

## 为什么这样选

### 1. MiMo 不适合继续做生产默认

当前实测现象已经说明：

- `<style>...</style>` 风格控制有效
- 语速、语气、整体风格会变化
- 但男/女声身份不稳定

结合官方文档：

- 可选内置 voice 只有 `mimo_default`、`default_zh`、`default_en`
- 其中 `default_zh`、`default_en` 明确是 female voice
- 官方公开文档没有提供“稳定男声切换”的明确控制入口
- 官方明确写明当前不支持 voice cloning

因此 MiMo 更适合：

- 风格化实验
- 备用 provider
- 非关键链路

不适合：

- 免费主链路
- 对男女声稳定性有要求的商业产品

### 2. CosyVoice 最适合快捷版

它更适合承担“低成本、稳定男女声、中文友好”的角色：

- 成本低
- 中文场景成熟
- 支持流式
- 支持音色复刻
- 作为自动化链路更稳

### 3. MiniMax 最适合工作台版

它更适合承担“高质量、审校、克隆音色、精细化”的角色：

- 男/女声与音色控制更成熟
- 音色克隆能力更适合工作台方案
- 支持同步、异步长文本、WebSocket 等多种模式
- 现有项目里也已经有 MiniMax 路径基础

## 成本对比（人民币）

### 计费估算前提

- 10 分钟中文配音稿常见约 1500 到 2500 个汉字
- 粗略按 3000 到 5000 计费字符估算
- 这里只计算 TTS 本身，不含：
  - ASR
  - 翻译
  - 存储/CDN
  - 下载
  - 转码
  - 数据库/任务调度

## 供应商成本表

| 供应商 | 官方计费 | 10 分钟中文配音粗估 | 说明 |
|---|---:|---:|---|
| MiMo | 当前官方文档显示“限时免费” | 约 0 元 | 不适合作为长期预算依据 |
| CosyVoice `v3-flash` | 1 元 / 万字符 | 0.3 到 0.5 元 | 适合快捷版 |
| CosyVoice `v3.5-flash` | 0.8 元 / 万字符 | 0.24 到 0.4 元 | 如果可用，性价比更高 |
| CosyVoice `v3-plus` / `v2` | 2 元 / 万字符 | 0.6 到 1.0 元 | 更高质量，但快捷版通常没必要 |
| MiniMax Turbo（国内按量） | 2 元 / 万字符 | 0.6 到 1.0 元 | 适合 Plus 工作台版 |
| MiniMax HD（国内按量） | 3.5 元 / 万字符 | 1.05 到 1.75 元 | 适合 Pro / Admin 工作台版 |

## 长视频成本粗估

### 快捷版（CosyVoice `v3-flash`）

| 时长 | 字符估算 | 单条成本 |
|---|---:|---:|
| 10 分钟 | 3000 到 5000 | 0.3 到 0.5 元 |
| 60 分钟 | 18000 到 30000 | 1.8 到 3.0 元 |
| 180 分钟 | 54000 到 90000 | 5.4 到 9.0 元 |

### 工作台版（MiniMax Turbo）

| 时长 | 字符估算 | 单条成本 |
|---|---:|---:|
| 10 分钟 | 3000 到 5000 | 0.6 到 1.0 元 |
| 60 分钟 | 18000 到 30000 | 3.6 到 6.0 元 |
| 180 分钟 | 54000 到 90000 | 10.8 到 18.0 元 |

### 工作台版（MiniMax HD）

| 时长 | 字符估算 | 单条成本 |
|---|---:|---:|
| 10 分钟 | 3000 到 5000 | 1.05 到 1.75 元 |
| 60 分钟 | 18000 到 30000 | 6.3 到 10.5 元 |
| 180 分钟 | 54000 到 90000 | 18.9 到 31.5 元 |

## 音色克隆成本

### CosyVoice

- 官方支持声音复刻
- 声音复刻服务本身可免费使用（以官方当前说明为准）
- 后续使用复刻音色进行 TTS 时，仍按 CosyVoice 模型字符数计费

### MiniMax（国内）

- 快速复刻：9.9 元 / 音色
- 音色设计：9.9 元 / 音色
- 真正计费时点：首次正式使用该音色进行语音合成

因此工作台版如果启用克隆音色：

- 首次使用成本 = 语音合成成本 + 9.9 元
- 后续复用同一音色 = 仅语音合成成本

## 输出速率与限流对比

当前项目不是“一次输入整篇长文”，而是按 segment / semantic block 分段调用 TTS。

因此需要同时看：

- 请求提交速率
- provider 限流
- 模型输出模式（同步 / 流式 / 异步）

## 供应商速率对比

| 供应商 | 输出模式 | 官方限流信息 | 适配判断 |
|---|---|---|---|
| MiMo | 非流式 + 流式 | FAQ 当前写明 RPM 100 | 提交速率不差，但声音控制不够稳 |
| CosyVoice（百炼/ISI） | 流式能力明确 | 百炼部分接口 3 RPS；CosyVoice 流式接口文档给出并发限制 | 适合批量快速提交与自动化链路 |
| MiniMax | 同步、异步长文本、WebSocket | 国内语音资源包中给出 RPM 60 / 200 / 500 等档位 | 默认速率一般，但资源包和异步接口很适合工作台版 |

## 120 段视频的理论提交下限（仅按限流估算）

假设一个 10 分钟视频切成 120 段，每段一次 TTS 请求，仅按接口限流估算最短提交时间：

| 供应商 | 限流假设 | 120 段最短提交时间 |
|---|---:|---:|
| MiMo | 100 RPM ≈ 1.67 req/s | 约 72 秒 |
| CosyVoice | 3 RPS | 约 40 秒 |
| MiniMax 默认 | 60 RPM = 1 req/s | 约 120 秒 |
| MiniMax 资源包高档 | 200 RPM ≈ 3.33 req/s | 约 36 秒 |

注意：

- 这只是“可提交速度”，不等于最终音频生成完成时间
- 真正耗时还包括模型出音频的时间与网络开销

## 最终执行策略建议

### 快捷版

- provider: `cosyvoice`
- model: `cosyvoice-v3-flash`
- review: `false`
- voice_clone_enabled: `false`
- voice_strategy: `preset_mapping`
- 目标：便宜、快、男女声稳定、自动化

### 工作台版（Plus）

- provider: `minimax`
- model: `speech-2.8-turbo`
- review: `true`
- voice_clone_enabled: `true`
- 目标：兼顾成本与质量

### 工作台版（Pro / Admin）

- provider: `minimax`
- model: `speech-2.8-hd`
- review: `true`
- voice_clone_enabled: `true`
- 目标：更高质量与更强控制力

## 当前项目的工程建议

### 数据流与职责分层

任务创建时的策略快照数据流：

```
frontend (service_mode) -> Gateway compute_job_policy() -> PostgreSQL + Job API record/store -> process.py consume snapshot
```

各层职责：

1. **前端** (`frontend-next/src/lib/api/jobs.ts`)
   - 提交 `service_mode`（express / studio），不决定具体 provider
   - `submitTranslationJob` 请求体增加 `service_mode` 字段

2. **Gateway** (`gateway/job_intercept.py`)
   - `compute_job_policy(user, service_mode)` 计算完整策略快照
   - 将快照写入 PostgreSQL（Job 表）和上游 Job API 请求

3. **Job API** (`src/services/jobs/api.py`)
   - 接收 Gateway 传入的 snapshot 字段
   - 传递给 store 层持久化

4. **Job Store** (`src/services/jobs/models.py` + `src/services/jobs/store.py`)
   - `JobRecord` 扩展 snapshot 字段
   - store 持久化 snapshot 到 JSON 文件

5. **Pipeline** (`src/pipeline/process.py`)
   - 仅消费快照字段，**绝不自行推断** free/paid/admin 身份

任务创建时固化以下字段：

- `service_mode`
- `tts_provider`
- `tts_model`
- `requires_review`
- `voice_clone_enabled`
- `voice_strategy`
- `plan_code_snapshot`
- `role_snapshot`
- `source_duration_seconds`
- `quota_cost`

### 数据模型扩展

#### Gateway ORM (`gateway/models.py`)

**User 表新增字段：**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `role` | VARCHAR | `"user"` | 取值: `"user"` \| `"admin"` |
| `plan_code` | VARCHAR | `"free"` | 取值: `"free"` \| `"plus"` \| `"pro"` |
| `free_jobs_quota_total` | INTEGER | `5` | 免费额度总量 |
| `free_jobs_quota_used` | INTEGER | `0` | 已使用免费额度 |

**Job 表新增字段：**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `service_mode` | VARCHAR | — | 取值: `"express"` \| `"studio"` |
| `tts_provider` | VARCHAR | — | 取值: `"cosyvoice"` \| `"minimax"` \| `"mimo"` |
| `tts_model` | VARCHAR | — | 具体模型标识 |
| `requires_review` | BOOLEAN | — | 是否需要审核 |
| `voice_clone_enabled` | BOOLEAN | — | 是否启用音色克隆 |
| `voice_strategy` | VARCHAR | — | 取值: `"preset_mapping"` \| `"user_selected"` |
| `plan_code_snapshot` | VARCHAR | — | 创建时用户 plan 快照 |
| `role_snapshot` | VARCHAR | — | 创建时用户 role 快照 |
| `source_duration_seconds` | FLOAT | nullable | 源视频时长（秒） |
| `quota_cost` | INTEGER | `1` | 消耗的额度数 |

#### Job API store (`src/services/jobs/`)

- `src/services/jobs/models.py` 的 `JobRecord` 需要添加与上述 Gateway Job 表一致的字段
- `src/services/jobs/store.py` 需要支持新字段的读写

#### 数据库迁移

- 必须通过 **Alembic migration** 添加新字段，不能仅修改 ORM 定义
- Gateway 和 Job API 各自的存储层都需要同步更新

### 队列拆分

建议拆成两条 provider 队列：

- `express_tts_queue`
  - 默认走 CosyVoice
- `studio_tts_queue`
  - 默认走 MiniMax

分别配置独立限流器，避免互相拖累。

### 前端映射

前端仍只暴露两个方案：

- `快捷版`
- `工作台版`

不要在前端再暴露 provider 选择，避免增加用户决策负担。

后端根据用户等级映射实际模型：

- Free / Plus / Pro / Admin + `express`
  - → CosyVoice `v3-flash`
- Plus + `studio`
  - → MiniMax Turbo
- Pro / Admin + `studio`
  - → MiniMax HD

### MiMo 的定位

MiMo 不建议删除，但建议改成：

- 内部实验 provider
- 备用 provider
- 后台灰度开关

不要继续让它承担默认生产链路。

## 快捷版音色映射（CosyVoice v3）

快捷版使用 CosyVoice v3-flash 预设音色，通过 gender/age_group 做有限确定性映射。

这是一个独立的选择器模块 `src/services/tts/cosyvoice_voice_selector.py`，不散落在业务代码中。

```python
COSYVOICE_V3_VOICE_MAP = {
    "male":           "longanyang",       # 龙安洋，阳光大男孩，20-30岁
    "female":         "longanhuan",       # 龙安欢，元气女，20-30岁
    "male_elderly":   "longhua",          # 龙华，成熟男性
    "female_young":   "longanhuan",       # 复用龙安欢
}
FALLBACK_VOICE = "longanyang"
```

注意：这里使用的是 **v3-flash 真实音色 ID**，不是 v2 的音色。

## 能力感知 Fallback

### 快捷版（无克隆、无自定义音色）

```
CosyVoice v3-flash → MiMo（备用）→ fail
降级时标记 provider_downgraded=true
```

### 工作台版（未启用克隆 / 未指定自定义音色）

```
MiniMax → CosyVoice 预设音色降级 → fail
降级时标记 provider_downgraded=true
```

### 工作台版（已启用克隆 / 已指定自定义 voice_id）

```
MiniMax → fail（不降级）
原因：克隆音色能力不等价，静默降级语义不一致
```

## Speaker Profile 数据链路

完整的 speaker profile 数据链路：

```
src/services/transcript_reviewer.py (outputs gender/age_group)
  → DubbingSegment (needs new gender/age_group fields)
  → src/services/gemini/translator.py (passes through)
  → src/services/tts/cosyvoice_voice_selector.py (consumes gender/age_group)
```

实现方式二选一：

1. 在 `DubbingSegment` 上直接添加 `gender` 和 `age_group` 字段
2. 添加统一的 speaker profile normalization 层

无论哪种方式，都必须保证 `transcript_reviewer` 输出的结构化 gender/age_group 能沿链路传递到 `cosyvoice_voice_selector`。

## 架构约束（不可变）

- 快捷版**不做音色复刻**，只用系统预设音色
- MiMo **仅作为快捷版备用**，不用于工作台版
- 工作台版启用克隆时**不静默降级**（克隆音色能力不等价）
- Admin 是 **role** 不是 plan（Admin 可以是任意 plan_code）
- 快捷版音色选择先做**有限确定性映射**，不做复杂智能匹配

## 改动目标文件清单

### Backend

| 操作 | 文件路径 |
|------|----------|
| NEW | `src/services/tts/cosyvoice_provider.py` |
| NEW | `src/services/tts/cosyvoice_voice_selector.py` |
| MODIFY | `src/services/tts/tts_strategy.py` |
| MODIFY | `src/services/tts/tts_generator.py` |
| MODIFY | `src/pipeline/process.py`（仅消费快照，不推断身份） |
| MODIFY | `src/services/transcript_reviewer.py`（添加结构化 gender/age_group 输出） |
| MODIFY | `src/services/gemini/translator.py`（DubbingSegment 添加 gender/age_group） |
| MODIFY | `gateway/job_intercept.py`（compute_job_policy + 写入快照） |
| MODIFY | `gateway/models.py`（User + Job 新增字段） |
| MODIFY | `gateway/admin_settings.py`（express/studio provider 配置） |
| MODIFY | `src/services/jobs/api.py`（Job API 接收 snapshot 字段） |
| MODIFY | `src/services/jobs/models.py`（JobRecord 扩展 snapshot 字段） |
| MODIFY | `src/services/jobs/store.py`（持久化 snapshot 字段） |
| NEW | Alembic migration |

### Frontend

| 操作 | 文件路径 |
|------|----------|
| MODIFY | `frontend-next/src/app/translations/new/page.tsx` |
| MODIFY | `frontend-next/src/app/admin/settings/page.tsx` |
| MODIFY | `frontend-next/src/lib/api/jobs.ts`（提交 job 时传 service_mode） |

## 最终推荐

### 推荐正式生产组合

- 免费 / 快捷版：CosyVoice `v3-flash`
- Plus 工作台版：MiniMax `speech-2.8-turbo`
- Pro / Admin 工作台版：MiniMax `speech-2.8-hd`
- MiMo：仅保留观察和内部实验

### 这套架构的优点

- 免费版成本非常低
- 快捷版走 CosyVoice v3-flash + 全自动流程 + 预设音色映射，男女声稳定性更好
- 工作台版保留高质量和克隆能力
- Admin 可以直接使用最高能力链路
- 与当前会员和任务方案分层设计天然兼容

## 官方参考

- MiMo Speech Synthesis
  - https://platform.xiaomimimo.com/#/docs/usage-guide/speech-synthesis
- MiMo FAQ
  - https://platform.xiaomimimo.com/#/docs/faq
- 阿里云百炼模型规格与价格
  - https://help.aliyun.com/zh/model-studio/models
- 阿里云文本转语音
  - https://help.aliyun.com/zh/model-studio/text-to-speech
- MiniMax 国内按量计费
  - https://platform.minimaxi.com/docs/guides/pricing-paygo
- MiniMax 国内语音资源包
  - https://platform.minimaxi.com/docs/guides/pricing-speech

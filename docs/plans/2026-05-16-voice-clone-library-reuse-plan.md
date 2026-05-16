# 个人音色库来源追踪与复用方案

Date: 2026-05-16

Status: Draft

## 背景

当前项目已经具备 MiniMax 音色克隆能力，并会在部分流程中把克隆结果登记到个人音色库。但个人音色库目前更像 `voice_id` 列表，缺少克隆来源、来源视频、说话人匹配信息和复用决策。

这会带来两个问题：

1. 同一用户再次处理同一个视频、同一个说话人时，系统难以判断已有音色是否可复用。
2. 工作台手动克隆和智能版自动克隆可能重复调用克隆 provider，造成重复扣点、重复消耗 provider 克隆额度，以及个人音色库中出现多个难以区分的 `{speaker} Clone`。

本方案的目标是把克隆音色沉淀为可解释、可匹配、可复用的用户资产。

## 相关项目约束

- Gateway 是套餐、试用、价格、权益和扣点规则的事实来源。
- 前端只展示 Gateway 返回的商业和音色事实，不应自行决定是否扣点。
- 智能版流程应继续保持增量迁移，不做大规模重写。
- 测试、本地开发和默认路径不得引入真实外部服务依赖。
- 个人音色复用只在同一用户范围内发生，不做跨用户共享音色库。

## 前置条件

个人音色复用依赖稳定的“同一视频”标识。当前项目已有 `Job.source_content_hash` 字段，但创建任务主路径并未稳定填充该字段。因此在实现个人音色复用前，必须先完成一个独立前置任务：创建任务时生成并保存 `source_content_hash`。

完成判据：

- YouTube 任务在创建时保存规范化 `source_content_hash`，格式为 `youtube:{video_id}`。
- 上传视频任务在 Gateway 侧保存内容 hash，推荐 streaming SHA-256，不依赖前端浏览器计算大文件 hash。
- `editing/commit copy_as_new` 继续继承源任务的 `source_content_hash`。
- 强匹配逻辑必须要求 `source_content_hash` 非空，禁止把 `NULL == NULL` 视为同一视频。
- Phase 0 不回填历史 jobs。Phase 0 之前创建的任务及其衍生克隆不参与强匹配，只能通过 Phase 1 之后新克隆的音色逐步建立匹配关系。
- 增加回归守卫：`test_create_job_populates_source_content_hash_youtube_and_upload`。

智能版自动复用还需要一个独立前置调整：匹配检查必须插在 consent 检查之后、样本时长和 quota 水位线检查之前。样本时长和 quota 只约束“是否可以新克隆”，不应阻止“复用已有个人音色”。

## 目标

- 克隆音色保存到个人音色库时，记录来源视频和来源说话人信息。
- 用户可见音色名从 `{speaker} Clone` 改为 `{speaker_name} · {clone_time}`。
- 智能版自动克隆前先从个人音色库匹配可复用音色。
- 工作台和后期编辑中，用户点击克隆前先查询个人音色库，命中后提示是否复用。
- 复用已有个人音色时，不调用克隆 provider，不消耗克隆音色点数。
- 保留用户主动重新克隆同一视频同一说话人的能力。

## 非目标

- 不做跨用户音色共享。
- 不做全局声音指纹检索。
- 不引入新的实时外部识别服务。
- 不改变主交付目标，pipeline 仍以 Jianying draft 输出为主。
- 不把 plan、price、trial、entitlement 事实下沉到前端。

## 当前实现梳理

### 克隆 provider

`src/services/voice_clone.py` 封装 MiniMax 音色克隆客户端。

当前 provider 侧 `voice_id` 由安全机器 ID 组成，形如：

```text
vt_{speaker_id}_{timestamp_ms}
```

该 ID 适合作为 provider voice id，不建议改成中文名或人类可读时间。

### 工作台手动克隆

`gateway/voice_selection_api.py` 提供：

```text
POST /job-api/jobs/{job_id}/voice-clone
```

当前行为：

- 根据说话人选择的音频片段生成克隆样本。
- 克隆前 shadow reserve 克隆点数。
- 克隆成功后 capture。
- 克隆失败后 release。
- 成功后调用 `add_user_voice()` 写入个人音色库。

当前个人音色库 label 大致为：

```text
{display_speaker_name} Clone
```

### 智能版自动克隆

智能版自动克隆相关路径：

- `src/services/smart/auto_voice_review.py`
- `src/services/smart_wiring.py`
- `src/pipeline/process.py`

当前行为：

- 仅当 `smart_consent.auto_voice_clone is True` 时自动克隆。
- 根据主说话人和样本时长做克隆决策。
- 克隆成功后，pipeline 调用 Gateway 内部接口登记个人音色。
- 登记失败时进入 fail-closed handoff，避免克隆音色丢失在 provider 侧但用户库不可见。

当前智能版登记 label 也倾向于：

```text
{speaker_name} Clone
```

### 个人音色库

`gateway/models.py` 中的 `UserVoice` 当前主要字段包括：

- `user_id`
- `voice_id`
- `voice_type`
- `provider`
- `tts_provider`
- `platform`
- `label`
- `source_speaker_id`
- `notes`
- `expired_at`
- `chars_per_second`
- `chars_per_second_by_model`
- `speed_calibrated_at`
- `created_at`
- `updated_at`

当前缺少可用于复用匹配的来源字段，例如来源视频 hash、YouTube video id、上传视频 hash、来源视频标题、来源说话人名、内容摘要和年代信息。

`gateway/user_voice_service.py` 的 `add_user_voice()` 目前按 `(user_id, voice_id)` upsert。这个约束可以保留，但不应作为复用匹配的唯一依据。

### source_content_hash 现状

`Job.source_content_hash` 已经存在，但创建任务主路径需要补齐稳定写入逻辑。否则方案中的强匹配只能覆盖后续新任务，无法覆盖大多数生产任务。

首期实现前必须明确：

- YouTube 来源由 Gateway 规范化 URL，抽取 video id 后写为 `youtube:{video_id}`。
- 上传视频由 Gateway 对落盘文件做 streaming SHA-256，写入 `source_content_hash`。
- 不用前端 Web Crypto 计算大文件 hash，避免大视频卡住浏览器。
- 复制编辑任务继续继承源任务 hash。
- `source_content_hash` 为空时不参与强匹配。

## 模块化设计

本方案要求把“音色克隆业务能力”收敛成可复用模块，供智能版、工作台、后期编辑和后续流程调用。模块化目标不是把所有代码搬到一个文件，而是明确分层边界，让各流程不再各自拼接克隆、扣点、登记和命名逻辑。

### 分层边界

建议分为三层：

1. Provider client 层
2. 克隆业务编排层
3. 个人音色库匹配和登记层

### Provider client 层

Provider client 只负责调用外部克隆 provider，例如 MiniMax：

- 上传克隆样本。
- 创建 provider voice id。
- 返回 provider 克隆结果。
- 不负责扣点。
- 不负责个人音色库命名。
- 不负责判断是否复用已有音色。

当前 `src/services/voice_clone.py` 已经接近这一层，应继续保持为低层 provider adapter。

### 克隆业务编排层

新增或整理一个统一的业务编排服务模块，建议命名为：

```text
VoiceCloneOrchestrator
```

或：

```text
UserVoiceCloneService
```

该模块负责共享克隆业务能力，但不应把所有调用方的计费语义合并成一个策略。工作台手动克隆和智能版自动克隆的商业契约不同：

- 工作台手动克隆：调用 clone provider 才进入 shadow reserve / capture / release，并消耗克隆点数。
- 智能版自动克隆：在 smart 套餐 envelope 内执行，`no_extra_charge_without_confirmation` 是用户契约，不应因为模块化而改为单次 clone 扣点。
- 复用已有个人音色：不调用 clone provider，不消耗克隆点数。

因此建议同一模块内提供按调用方区分的 entrypoint：

```text
studio_clone_with_capture()
smart_clone_within_envelope()
reuse_existing_user_voice()
match_existing_user_voice()
register_cloned_user_voice()
```

共享逻辑包括：

- 接收 `user_id`、`job_id`、`speaker_id`、`speaker_name`、克隆样本和来源信息。
- 克隆前调用个人音色匹配服务。
- 根据调用方策略决定强匹配时自动复用还是提示用户。
- 调用 provider client。
- 生成统一 label：`{speaker_name} · {clone_time}`。
- 写入个人音色库来源字段。
- 返回统一结果对象。

工作台 entrypoint 负责 reserve / capture / release。智能版 entrypoint 不得调用工作台克隆扣点路径，但可以记录非计费复用或克隆审计。

复用审计建议提供一层语义明确的薄包装：

```text
record_voice_reuse(source_voice_id=...)
```

该包装内部可以复用现有 usage meter 存储结构，但 callsite 不应直接调用名称容易误导的 `record_voice_clone(..., billable=False, reuse=True)`。

统一结果对象建议包含：

```json
{
  "action": "reused_user_voice | cloned_new_voice | needs_user_confirmation | failed",
  "voice_id": "vt_speaker_a_...",
  "user_voice_id": 42,
  "label": "芒格 · 2026-05-16 14:32",
  "match_confidence": "strong",
  "match_reason": "same_source_content_hash_and_speaker_id",
  "billing_mode": "studio_clone_points | smart_envelope | none"
}
```

这样各流程只关心结果，不需要重复理解克隆、匹配、扣点和保存细节。

### 个人音色库匹配和登记层

个人音色库服务负责：

- 根据来源视频和说话人查找候选音色。
- 判断强匹配、中等匹配和弱匹配。
- 过滤过期、删除、provider 不兼容的音色。
- 保存新克隆音色。
- 保存来源字段。
- 提供统一的音色 label 构造函数。

该层应由 Gateway 持有，因为个人音色库、扣点和商业权益都属于 Gateway 事实来源。

### 各流程调用方式

智能版自动克隆：

```text
pipeline -> Gateway/internal clone-or-reuse service -> match -> reuse or provider clone -> user_voices
```

工作台手动克隆：

```text
frontend -> Gateway clone preview/match -> user confirms reuse or clone -> clone-or-reuse service
```

后期编辑手动克隆：

```text
frontend -> same Gateway clone preview/match -> same clone-or-reuse service
```

CLI 或本地工具：

```text
CLI -> provider client
```

CLI 可以继续直接使用 provider client，或后续接入 Gateway 模块。SaaS 主流程不应依赖 CLI 的本地 registry 逻辑。

### 依赖方向

智能版的纯决策代码应继续保持轻量和可测试，不直接 import provider client，也不直接发 Gateway HTTP。

推荐依赖方向：

```text
frontend
  -> Gateway voice selection API
    -> UserVoiceCloneService
      -> UserVoiceMatcher
      -> UserVoiceRegistry
      -> MiniMax provider client

pipeline/process.py
  -> smart_wiring.py
    -> Gateway internal match/register calls
    -> MiniMax provider adapter
  -> smart/auto_voice_review.py
    -> pure decision inputs and outputs only
```

`src/services/smart/auto_voice_review.py` 只接收“已有匹配结果、样本时长、consent、quota”等输入并产出决策。Gateway 匹配 HTTP 调用应放在 `process.py` / `smart_wiring.py` 这类 composition 层，而不是放进纯决策模块。

### 不应继续分散的逻辑

以下逻辑不应继续散落在智能版、工作台和后期编辑各自流程中：

- `{speaker} Clone` 这类 label 拼接。
- 是否扣克隆点数。
- 是否复用已有个人音色。
- 克隆成功后写入 `user_voices` 的字段映射。
- 来源视频和来源说话人的字段构造。
- provider、tts provider、platform 的兼容性判断。

这些逻辑应收敛到统一模块，流程层只传上下文和展示决策结果。

## 命名规则

### 用户可见 label

新克隆音色的用户可见名称建议改为：

```text
{speaker_name} · {clone_time}
```

示例：

```text
芒格 · 2026-05-16 14:32
Speaker A · 2026-05-16 14:32
```

规则：

- `speaker_name` 优先使用用户可见说话人名。
- 如果没有说话人名，使用稳定 fallback，例如 `说话人 A` 或 `Speaker A`。
- `clone_time` 使用用户产品侧默认时区展示，当前可按 Asia/Shanghai。
- 如果同一分钟内出现重名，可以在展示层追加计数后缀，例如 `(2)`，不要向用户展示 provider voice_id 片段。

### Provider voice_id

MiniMax provider 侧 `voice_id` 保持机器安全 ID，不使用中文名、空格、标点或本地化时间。

推荐继续使用类似：

```text
vt_{safe_speaker_id}_{timestamp_ms}
```

## 数据模型建议

在 `user_voices` 上增加来源和匹配字段。建议采用“关键匹配字段列化，扩展信息 JSON 化”的方式。

### 推荐新增列

| 字段 | 类型建议 | 用途 |
| --- | --- | --- |
| `source_job_id` | string nullable | 来源任务 ID，便于追踪 |
| `source_type` | string nullable | `youtube` / `upload` / `local` / `unknown` |
| `source_ref` | string nullable | YouTube URL、video id、上传文件引用等 |
| `source_content_hash` | string nullable | 同一视频匹配主键，优先复用现有 Job.source_content_hash |
| `source_upload_md5` | string nullable | 上传视频 MD5，可选补充字段 |
| `source_video_title` | string nullable | 来源视频标题或任务标题 |
| `source_speaker_id` | string nullable | 当前已有字段，继续作为来源说话人机器 ID |
| `source_speaker_name` | string nullable | 来源说话人用户可见名称 |
| `source_speaker_name_key` | string nullable | 规范化后的说话人名，用于中等匹配 |
| `source_published_at` | datetime nullable | YouTube 发布时间或来源视频发布时间 |
| `source_content_summary` | text nullable | 可选，视频内容摘要 |
| `source_content_era` | string nullable | 可选，内容年代或历史时期 |
| `source_content_tags` | JSON nullable | 可选，内容标签 |
| `clone_sample_seconds` | float nullable | 克隆样本总秒数 |
| `clone_sample_segment_ids` | JSON nullable | 克隆样本来源片段 |
| `created_from` | string nullable | `smart_auto` / `studio_manual` / `post_edit` / `cli_import` |

### 关于 MD5 和 source_content_hash

项目中已经存在 `Job.source_content_hash` 的概念，上传视频可使用内容 hash，YouTube 可使用规范化 video id，例如：

```text
youtube:{video_id}
```

因此不建议把 MD5 作为唯一匹配字段。若产品上需要显示或兼容 MD5，可新增 `source_upload_md5`，但匹配主逻辑优先使用 `source_content_hash`。

### YouTube 规范化规则

YouTube 来源必须在 Gateway 侧规范化成稳定 video id，再写入：

```text
youtube:{video_id}
```

建议提供一个单一函数作为契约：

```python
def canonicalize_youtube_source_content_hash(url: str) -> str | None:
    ...
```

至少覆盖：

```text
https://www.youtube.com/watch?v=abc&t=10s -> youtube:abc
https://youtu.be/abc -> youtube:abc
https://www.youtube.com/shorts/abc -> youtube:abc
https://m.youtube.com/watch?v=abc -> youtube:abc
https://www.youtube.com/live/abc -> youtube:abc
```

重复上传、转载、剪辑和不同 video id 的相同内容可以先 false negative，不做跨视频内容识别。

### 索引建议

推荐增加查询索引：

```text
(user_id, source_content_hash, source_speaker_id)
(user_id, source_content_hash, source_speaker_name)
(user_id, source_ref)
```

不建议对 `(user_id, source_content_hash, source_speaker_id)` 增加唯一约束。用户可能希望对同一个视频同一个说话人重新克隆更高质量的音色，系统应允许多条候选存在，再由匹配规则选出默认推荐项。

### upsert 语义

引入来源字段后，`add_user_voice()` 的 upsert 规则需要钉死：

- `(user_id, voice_id)` 命中已有行时，`label`、`expired_at`、校准字段可按现有规则更新。
- `source_*` 字段应尽量视为 immutable provenance。
- 现有 `source_*` 为空、调用方传入非空时，可以一次性补齐。
- 现有 `source_*` 非空、调用方再次传入不同非空值时，默认不覆盖，并记录告警或审计。
- 如确实需要修正来源信息，应走单独的管理员修复路径，而不是普通 upsert 静默覆盖。

## 匹配规则

匹配只在同一 `user_id` 范围内进行。

### 说话人名规范化

中等匹配需要稳定的 `source_speaker_name_key`。首期规则建议保持保守：

- Unicode NFKC。
- 去除首尾空格、中点、连字符和下划线。
- 英文 lowercase。
- 连续空白折叠为单个空格。
- 不做跨语言翻译、罗马音推断或繁简转换，避免误匹配。

### 强匹配

满足以下条件时可认为强匹配：

- `user_id` 相同。
- `source_content_hash` 非空且相同。
- `source_speaker_id` 相同。
- provider、tts provider、platform 与当前任务兼容。
- 音色未过期、未删除、状态可用。

强匹配可在智能版中自动复用，在工作台中作为首选推荐。

匹配按 provider 隔离。MiniMax 克隆音色只能在兼容的 MiniMax TTS/平台路径复用，不能跨 provider 复用于 CosyVoice 或其他 provider。UI 展示候选音色时应展示 provider 标识，避免用户误解“曾经克隆过但这次没有命中”的原因。

### 中等匹配

满足以下条件时作为中等匹配：

- `user_id` 相同。
- `source_content_hash` 非空且相同。
- `source_speaker_name` 规范化后相同或高度接近。
- provider 兼容。

中等匹配不建议静默自动复用。工作台应提示用户确认；智能版可按配置决定是否自动复用，默认建议进入保守复用或 handoff。

### 弱匹配

满足以下条件时作为弱匹配：

- `source_content_hash` 相同但 `source_speaker_id` 不一致。典型原因是同一视频两次任务的 diarization 说话人编号顺序发生变化，例如同一个人第一次是 `speaker_a`，第二次是 `speaker_b`。
- 视频标题、内容摘要、内容年代、说话人名存在相似性。

弱匹配用 `source_speaker_name_key` 辅助承接这种 ID 抖动，但只用于排序和提示，不应用于自动复用。

### 辅助字段的作用

`source_content_summary`、`source_content_era`、`source_content_tags` 只作为解释和排序辅助，不作为单独复用依据。

原因：

- 内容摘要可能由不同模型或不同版本生成。
- 年代概念可能指视频发布时间、视频内容年代、人物说话年代，语义容易混淆。
- 仅凭内容相似不能证明同一个说话人或同一条视频。

建议字段命名保持明确：

- `source_published_at` 表示视频发布时间。
- `source_content_era` 表示内容语义年代。

### 过期音色和质量信号

过期音色不参与强匹配自动复用。但如果同一视频同一说话人只匹配到过期音色，工作台可以轻量提示：

```text
曾经为这个视频的此说话人克隆过音色，但音色已过期。可以重新克隆。
```

候选音色列表中可展示 `clone_sample_seconds`、创建时间和来源片段数量，作为用户选择旧候选或重新克隆的质量参考。

## 后端接口建议

### 匹配接口

新增 Gateway 内部匹配接口：

```text
POST /api/internal/user-voices/match
```

请求字段：

```json
{
  "user_id": "user_123",
  "job_id": "job_123",
  "source_type": "youtube",
  "source_ref": "https://www.youtube.com/watch?v=...",
  "source_content_hash": "youtube:abc123",
  "speaker_id": "speaker_a",
  "speaker_name": "芒格",
  "provider": "minimax_voice_clone",
  "tts_provider": "minimax_tts",
  "platform": "minimax_domestic"
}
```

响应字段：

```json
{
  "matched": true,
  "confidence": "strong",
  "voice": {
    "id": 42,
    "voice_id": "vt_speaker_a_...",
    "label": "芒格 · 2026-05-16 14:32",
    "source_video_title": "Daily Journal Annual Meeting",
    "source_speaker_name": "芒格",
    "created_at": "2026-05-16T14:32:00+08:00"
  },
  "reason": "same_source_content_hash_and_speaker_id"
}
```

### 复用接口

工作台前端应复用现有音色选择提交路径，不新增第二条“提交音色选择”路径。

```text
approveVoiceSelection
```

命中强匹配后，前端直接提交已有个人音色的 `voice_id`，跳过 `/voice-clone`。Gateway 在提交选择时记录一次非计费 `voice_reuse` 审计即可。

## 智能版流程

智能版自动克隆前增加匹配步骤，并明确顺序：

1. 检查 `smart_consent.auto_voice_clone is True`。
2. 从 job 和 speaker 决策中收集 `user_id`、`source_content_hash`、`source_ref`、`speaker_id`、`speaker_name`。
3. 调用 Gateway 内部匹配接口。
4. 强匹配命中时，直接使用个人音色库中的 `voice_id`。
5. 记录智能版决策为 `reused_user_voice`。
6. 不调用 MiniMax clone provider。
7. 不消耗克隆音色点数。
8. 不新增重复个人音色。
9. 未命中时，才检查 clone sample 时长、quota water mark 和 provider clone 条件。
10. 满足新克隆条件时走现有自动克隆流程。

样本时长 `<10s` 和 `quota_remaining <= safety_watermark` 只应阻止新克隆，不应阻止复用已有个人音色。

智能版侧建议新增决策状态：

```text
reused_user_voice
```

并在质量报告或 sidecar 中记录：

```text
voice_review_decision: reused_user_voice
matched_voice_id
matched_user_voice_id
match_confidence
match_reason
```

这样后续排查时能区分“本次真正克隆”和“本次复用已有音色”。

## 工作台和后期编辑流程

用户点击“克隆音色”时，前端先请求 Gateway 匹配。

### 强匹配

弹窗提示：

```text
发现可复用音色：芒格 · 2026-05-16 14:32
来源：同一视频 / 同一说话人
复用不会消耗克隆点数。
```

按钮建议：

```text
复用此音色
重新克隆（消耗 X 点）
```

如果用户选择重新克隆，应提示该操作会新增一个个人音色，占用个人音色库容量。

### 中等匹配

弹窗提示：

```text
发现可能可复用音色
```

展示来源、创建时间、说话人名、克隆样本秒数、来源片段数量和试听入口，由用户决定是否复用。

### 无匹配

维持现有克隆流程，展示克隆成本，用户确认后再扣点克隆。

## 计费和额度规则

复用已有个人音色时：

- 不调用克隆 provider。
- 不 shadow reserve 克隆点数。
- 不 capture 克隆点数。
- 不消耗用户个人音色库克隆额度。
- 可记录一次非计费复用审计，但不计入克隆消费。

真正发起新克隆时：

- 保持现有 reserve/capture/release 逻辑。
- 克隆成功后保存个人音色库。
- 保存时写入来源字段和新的 label。

智能版当前如采用套餐内自动克隆，也应区分 provider 克隆消耗和用户可见点数消耗。即使命中复用对用户价格没有变化，也应避免消耗 provider 克隆额度和用户音色库容量。

### 审计落点

不要为复用再造一套独立事件管道。推荐扩展现有 usage meter 语义，记录非计费复用事件，例如：

```text
record_voice_reuse(source_voice_id=...)
```

`record_voice_reuse()` 可以作为现有 usage meter 存储结构上的薄包装，内部如有必要可复用同一张表，但对 callsite 保持语义清晰。工作台路径用该记录表达“复用了个人音色且未扣克隆点数”。智能版路径除了记录非计费复用审计，还应在 smart sidecar 中写入：

```text
voice_clone_decision=reused_user_voice
matched_user_voice_id
matched_voice_id
match_confidence
match_reason
```

## 兼容性和迁移

历史个人音色缺少来源字段。迁移后应允许字段为空。

历史音色匹配策略：

- 没有 `source_content_hash` 的历史音色不参与强匹配。
- 可按 `notes`、`source_speaker_id`、创建任务 ID 做有限回填，但不作为首期必须项。
- 用户仍可手动选择历史音色。
- Phase 0 不回填历史 jobs。Phase 0 之前创建的任务及其衍生克隆不参与强匹配，只能通过 Phase 1 之后的新任务和新克隆逐步建立可复用关系。

旧 label 不需要批量重命名，避免用户已熟悉的名称突然变化。新规则只应用于新克隆音色。若需要后续治理，可单独做“历史音色补全来源信息和重命名”任务。

## 测试计划

### 后端测试

- 创建 YouTube 和上传任务时稳定写入 `source_content_hash`。
- 新克隆保存时写入来源字段。
- 新克隆 label 为 `{speaker_name} · {clone_time}`。
- provider voice_id 仍保持机器安全 ID。
- `add_user_voice()` 保持现有 upsert 行为，并能保存新增来源字段。
- `source_*` 已有非空值时，普通 upsert 不静默覆盖。
- 强匹配返回已有个人音色。
- 中等匹配返回候选但标记为非静默复用。
- 过期音色、provider 不兼容音色不参与匹配。
- 复用路径不触发克隆点数 reserve/capture。
- 真正克隆失败时仍 release 点数。

### 智能版测试

- 强匹配时不调用 clone provider。
- 强匹配在 sample/quota 检查之前生效。
- 强匹配时决策记录为 `reused_user_voice`。
- 未匹配时继续走现有自动克隆逻辑。
- 匹配接口失败时 fail-closed，不应误用未知音色。
- 模块化后智能版不进入工作台 clone capture 扣点路径。

### 前端测试

- 工作台克隆弹窗命中强匹配时展示复用提示。
- 点击复用后绑定已有个人音色，不调用克隆接口。
- 点击重新克隆时展示并执行现有扣点克隆流程。
- 后期编辑页与工作台页行为一致。
- 个人音色库展示新增来源信息时，在字段缺失的历史音色上不报错。

## 分阶段实施建议

### Phase 0: source_content_hash 前置任务

- 在创建任务主路径稳定填充 `source_content_hash`。
- YouTube URL 规范化为 `youtube:{video_id}`。
- 上传视频由 Gateway 侧 streaming SHA-256 生成内容 hash。
- `editing/commit copy_as_new` 继续继承源任务 hash。
- 不回填历史 jobs，历史任务及其衍生克隆不参与强匹配。
- 强匹配禁止 `NULL == NULL`。
- 增加 `test_create_job_populates_source_content_hash_youtube_and_upload`。

### Phase 1: 来源字段和命名

- 增加 `user_voices` 来源字段和索引。
- 扩展 `add_user_voice()` 入参。
- 抽出统一 label 构造函数和来源字段构造函数。
- 明确 `source_*` 字段普通 upsert 下不可被非空覆盖。
- 工作台克隆和智能版登记时写入来源字段。
- 新克隆 label 改为 `{speaker_name} · {clone_time}`。
- 更新后端和前端类型定义。

### Phase 2: 匹配接口

- 增加 Gateway 个人音色匹配服务。
- 增加内部匹配接口。
- 增加强匹配和中等匹配测试。
- 暂不改变现有克隆入口，只验证匹配结果。

### Phase 3: 统一克隆编排模块

- 建立 `VoiceCloneOrchestrator` 或 `UserVoiceCloneService`。
- 将匹配、扣点、provider clone、登记个人音色、失败释放整合到统一服务。
- 工作台、后期编辑和智能版共享匹配、provider clone 和登记逻辑。
- 工作台和智能版使用不同 entrypoint，保留各自计费语义。
- 保留 `src/services/voice_clone.py` 作为低层 provider client。
- 增加 `test_smart_no_extra_clone_charge_after_orchestrator_unification`。

### Phase 4: 工作台复用提示

- 克隆弹窗打开或提交前调用匹配接口。
- 强匹配显示复用提示。
- 复用路径不调用 `/voice-clone`，不扣点。
- 用户仍可选择重新克隆。

### Phase 5: 智能版自动复用

- 智能版在 consent 之后、sample/quota 检查之前调用匹配接口。
- 强匹配时写入 `reused_user_voice` 决策。
- 未命中时保持现有克隆流程。
- 更新智能版报告和 smoke tests。

### Phase 6: 内容和年代增强

- 从任务标题、YouTube 元数据、用户上传信息或已有分析结果中补充 `source_video_title`、`source_published_at`、`source_content_summary`、`source_content_era`。
- 将内容和年代作为候选排序和解释字段。
- 不把内容和年代作为单独自动复用条件。

## 风险和注意事项

- 不应在前端决定扣点结果。前端只展示 Gateway 返回的匹配和计费事实。
- 不应跨用户匹配或复用音色。
- 不应仅凭视频内容摘要或年代自动复用音色。
- 不应把空 `source_content_hash` 当成强匹配。
- 不应把 provider voice_id 改成人类可读 label。
- 不应因为统一克隆模块而合并工作台和智能版的计费语义。
- 不应新增与现有音色选择提交路径重复的复用 endpoint。
- 不应对同一视频同一说话人增加唯一约束，用户需要保留重新克隆更好音色的能力。
- 来源视频 URL、上传 hash、内容摘要都可能包含用户隐私信息，日志中应避免泄露完整来源内容。

## 验收标准

- 新创建的 YouTube 和上传任务稳定写入非空 `source_content_hash`。
- 新克隆音色在个人音色库中显示为 `{speaker_name} · {clone_time}`。
- 新克隆音色保存来源视频和来源说话人字段。
- 同一用户、同一视频、同一说话人的强匹配可被 Gateway 识别。
- 工作台命中强匹配时可复用已有音色，且不扣克隆点数。
- 智能版命中强匹配时，即使样本不足或 clone quota 到达水位线，也能跳过自动克隆并复用已有音色。
- 智能版复用和智能版套餐内自动克隆都不进入工作台 clone capture 扣点路径。
- 未命中匹配时，现有克隆流程、扣点流程和失败释放流程保持不变。

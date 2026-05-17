# 个人音色候选前置与智能版复用策略方案

Date: 2026-05-17

Status: Draft

Related:
- `docs/plans/2026-05-16-voice-clone-library-reuse-plan.md`
- `docs/graphs/GITNEXUS_SMART_AUTO_REVIEW_GRAPH.md`
- `docs/graphs/GITNEXUS_REVIEW_GRAPH.md`
- `docs/graphs/GITNEXUS_EDITING_POST_EDIT_GRAPH.md`

## 背景

上一阶段已经完成个人音色库来源字段、同源强匹配、Studio 克隆弹窗复用提示、Smart 强匹配自动复用、后期编辑复用审计等基础能力。

当前剩余的产品问题是：个人音色库仍然更像“克隆动作前的补充提示”，而不是所有音色选择流程的一等候选源。用户在工作台音色选择页或后期编辑音色修改页，第一眼看到的仍主要是官方 TTS 音色推荐；只有点击“克隆音色”后才看到可复用个人音色。这不符合用户心智：如果我已经克隆过某个人，系统应该优先推荐这个个人音色，而不是先推荐官方音色或诱导再次克隆。

另一个问题是 Smart 当前只自动接受同视频同 speaker_id 的强匹配。跨视频同一人物的个人音色、同视频 speaker_id 抖动但名字一致的音色，对自动决策来说风险较高，但对用户确认来说很有价值。需要把“可自动复用”和“需要用户确认的候选”分开。

## 目标

- 个人音色库成为 Studio、后期编辑和 Smart 的统一候选源。
- Studio / 后期编辑的音色列表优先展示个人音色候选，再展示官方 TTS 推荐音色。
- Smart 对强匹配个人音色自动复用，不调用克隆 provider，不消耗克隆点数。
- Smart 遇到可能匹配的个人音色时，可按后台策略暂停流程，让用户确认是否复用。
- 支持跨视频弱候选，但仅用于排序和用户确认，不自动复用。
- 后台提供智能版音色策略开关，控制自动克隆、复用已有音色、弱匹配是否暂停。
- 复用行为必须审计，便于后续统计复用率、误报率和节省的克隆成本。

## 非目标

- 不做跨用户共享个人音色。
- 不把跨视频弱匹配用于自动复用。
- 不在本阶段引入声纹 embedding 或外部声纹识别服务。
- 不改变 Gateway 作为套餐、价格、权益和扣点事实来源的边界。
- 不把官方音色推荐逻辑复制到前端。前端只展示后端返回的候选和原因。
- 不在本阶段实现试听对比 UI。试听对比作为后续增强，但本方案预留接口字段。

## 当前实现简述

当前匹配服务位于 `gateway/user_voice_service.py`：

- `match_user_voices()` 要求 `source_content_hash` 非空。
- 只查当前用户未过期的 `user_voices`。
- 默认 provider 三元组为 `minimax_voice_clone / minimax_tts / minimax_domestic`。
- `strong`：同 `source_content_hash` + 同 `source_speaker_id`。
- `medium`：同 `source_content_hash` + 同 `source_speaker_name_key`。
- `weak`：同 `source_content_hash` + speaker_id 不同。
- `auto_reuse_allowed` 当前仅 `strong=true`。

当前 Studio 行为：

- 用户点击“克隆音色”打开 `VoiceCloneModal`。
- 弹窗调用 `/job-api/jobs/{job_id}/voice-match` 查询个人音色库。
- 命中后提示用户“复用此音色”。
- 用户确认复用后跳过 `/voice-clone`，最终审批时带 `voice_reuse=true`。

当前 Smart 行为：

- 只有 `smart_consent.auto_voice_clone is True` 时才查询个人音色强匹配。
- internal endpoint 只接受 `confidence == "strong"`。
- 强匹配生成 `VoiceReviewChoice.REUSED`，跳过样本和 quota 检查。
- 未命中强匹配才进入样本提取、quota 水位线、provider clone。

当前后期编辑行为：

- 复用 Studio 的 `VoiceCloneModal`。
- 复用后通过 `/editing/voice-map` 写入 `voice_reuse=true`。
- Job API 记录 `voice_reuse_postedit` 非计费审计事件。

## 核心设计

### 候选源优先级

所有需要“为说话人选择音色”的交互都应采用同一候选顺序：

1. 自动可复用个人音色
2. 需要用户确认的个人音色候选
3. 官方 TTS 推荐音色
4. 官方音色列表中的其他音色
5. 显式新克隆

这不是简单 UI 排序，而是产品语义：个人音色是用户已经付出过克隆成本和人工确认成本的资产，应该优先被复用。

### 匹配等级

建议把当前 `strong / medium / weak` 扩展为更明确的 `match_scope + confidence`：

```text
same_source_strong
same_source_named
same_source_speaker_id_changed
cross_source_named_person
```

初期可以仍向前端暴露 `confidence`，但后端内部应记录更细的 `reason` 和 `evidence`。

Provider 兼容性采用 MVP 保守策略：`provider / tts_provider / platform` 三元组必须严格匹配。未来如果 MiniMax 等 provider 出现跨 platform 可复用的同源 `voice_id`，需要单独定义兼容矩阵，不能在本阶段隐式放宽。

#### same_source_strong

条件：

- 同一用户
- 同一 provider 三元组
- `source_content_hash` 相同
- `source_speaker_id` 相同

行为：

- Studio / 后期编辑：自动预选或排在第一位，仍允许用户改选。
- Smart：可自动复用。
- 不扣克隆点数。

#### same_source_named

条件：

- 同一用户
- 同一 provider 三元组
- `source_content_hash` 相同
- `source_speaker_name_key` 相同
- speaker name 不是泛化名称

行为：

- Studio / 后期编辑：作为“可能匹配”候选展示。
- Smart：按后台配置决定是否暂停让用户确认。
- 不自动复用。

#### same_source_speaker_id_changed

条件：

- 同一用户
- 同一 provider 三元组
- `source_content_hash` 相同
- speaker_id 不同
- 可以有相同或相近 speaker name，也可以只是同源 speaker 抖动候选

行为：

- Studio / 后期编辑：作为低置信候选展示。
- Smart：按后台配置决定是否暂停让用户确认。
- 不自动复用。

#### cross_source_named_person

条件：

- 同一用户
- 同一 provider 三元组
- `source_content_hash` 不同或为空
- `source_speaker_name_key` 相同
- speaker name 通过泛化名称过滤

行为：

- Studio / 后期编辑：展示为“跨视频同名候选”。
- Smart：只能在后台开启“弱匹配暂停确认”时触发暂停。
- 不自动复用。

### 泛化名称过滤

跨视频弱候选必须过滤泛化名称。以下名称不得进入跨视频弱匹配：

```text
speaker_a
speaker_b
speaker c
Speaker A
Speaker B
未知说话人
未知说话人1
男声
女声
主持人
嘉宾
采访者
受访者
旁白
Narrator
Host
Guest
```

还应过滤：

- 规范化后长度过短的名称。
- 只有单个字母或纯编号的名称。
- 只含 `speaker`、`unknown`、`voice`、`person` 等占位语义的名称。

过滤顺序必须是：先执行 `normalize_speaker_name_key()`，再调用泛化名称判断。这样 `Speaker A`、`ＳＰＥＡＫＥＲ Ａ`、`speaker_a`、`speaker a` 会收敛到同一类占位名称。

建议定义明确函数契约：

```python
def is_generic_speaker_name_key(key: str | None) -> bool:
    """Return true when a normalized speaker name is too generic for cross-source matching.

    Accepts ``None`` and returns ``False`` (caller may pass DB-nullable column directly).
    """
```

该函数与 `normalize_speaker_name_key()` 是配套关系（先 normalize 再判断 generic），建议落在同一模块 `gateway/user_voice_service.py`，避免 matcher 再 import 第二个模块。

MVP 只覆盖中文和英文常见占位名称，其他语言暂不过滤，接受少量 false negative。后续如果发现日文、韩文、法文、西语等占位名称造成误匹配，再按数据补充黑名单。

基础用例：

| 原始名称 | normalize 后 | 是否过滤 | 原因 |
|---|---|---:|---|
| `Speaker A` | `speaker a` | 是 | 英文占位名 |
| `ＳＰＥＡＫＥＲ Ａ` | `speaker a` | 是 | 全角归一后命中 |
| `speaker_1` | `speaker_1` | 是 | speaker + 编号 |
| `话者2` | `话者2` | 是 | 中文占位名 + 编号 |
| `人物 3` | `人物 3` | 是 | 泛化人物 + 编号 |
| `主持人` | `主持人` | 是 | 角色名 |
| `查理·芒格` | `查理·芒格` | 否 | 具体人名 |
| `Elon Musk` | `elon musk` | 否 | 具体人名 |

### 跨视频弱候选排序信号

跨视频候选只做排序，不做自动复用。排序建议：

1. speaker name key 完全一致。
2. 最近用户确认复用过的同名音色优先。
3. 最近克隆的音色优先。
4. `clone_sample_seconds` 更长优先。
5. 已完成语速校准的音色优先。
6. `source_content_era` 接近优先。
7. `source_content_tags.channel` 相同优先。
8. `source_content_tags.categories/tags` 重合度高优先。

这些信号只影响候选排序和解释，不得把跨视频弱候选提升为自动复用。

`最近用户确认复用过` 可以先从现有复用审计中尽力推断；如果没有稳定低成本查询路径，Phase 1 可以跳过该信号，后续再用 `last_user_confirmed_reuse_at / reuse_confirmed_count` 等字段补强。它不是单独的 `match_scope`。

其中 #6-#8 依赖 `source_content_era` 和 `source_content_tags` 的内容增强字段。当前生产数据允许这些字段为空；Phase 1-4 可以实现空值跳过逻辑，但不能把“内容相似性排序”作为验收前提。真正依赖内容和年代的排序增强应随原方案 Phase 6 单独验收。

## 后端 API 设计

### 统一候选接口

建议新增或扩展一个统一接口，供 Studio、后期编辑和 Smart 复用：

```text
POST /api/internal/user-voices/candidates
POST /job-api/jobs/{job_id}/voice-candidates
```

内部实现可复用现有 `match_user_voices()`，但返回结构应从“是否匹配一个音色”扩展为“候选列表”。

接口应支持一次请求多个 speaker，或直接把候选挂到现有 voice selection review payload 中返回，避免前端对 N 个 speaker 发起 N 次 HTTP 请求。后端返回已排序结果，前端不重新实现排序规则。

返回示例：

```json
{
  "speaker_id": "speaker_a",
  "source_content_hash": "youtube:abc",
  "auto_reuse_voice": {
    "voice_id": "vt_...",
    "confidence": "strong",
    "match_scope": "same_source_strong",
    "auto_reuse_allowed": true
  },
  "personal_voice_candidates": [
    {
      "voice_id": "vt_...",
      "label": "查理·芒格 · 2026-05-16 19:17",
      "confidence": "strong",
      "match_scope": "same_source_strong",
      "requires_user_confirmation": false,
      "reason": "same_source_content_hash_and_speaker_id",
      "evidence": {
        "source_video_title": "巴菲特谈接班与投资",
        "source_speaker_name": "查理·芒格",
        "clone_sample_seconds": 22.4
      }
    }
  ],
  "official_voice_candidates": [
    {
      "voice_id": "male-qn-qingse",
      "provider": "minimax",
      "reason": "current_official_tts_matcher"
    }
  ]
}
```

### 兼容现有接口

短期可以保留现有：

```text
POST /job-api/jobs/{job_id}/voice-match
POST /api/internal/user-voices/match
```

但建议把它们实现为统一候选接口的薄包装：

- `voice-match` 返回 top personal candidate。
- Smart internal match 返回 top strong candidate。
- 新 UI 使用候选接口。

这样避免 Studio 和后期编辑各自拼一套规则。

## Studio 工作台流程

### 当前问题

用户需要先点击“克隆音色”，系统才提示个人音色可复用。这个动作语义不对：用户并不一定想克隆，他只是想选最合适音色。

### 建议流程

1. 进入 voice selection review。
2. 批量加载所有主说话人的统一候选，或直接从 review payload 读取候选。
3. 如果存在 `same_source_strong`：
   - 默认预选该个人音色。
   - 在音色列表顶部展示“个人音色库 · 强匹配”。
   - 标记“不扣克隆点数”。
4. 如果存在弱候选：
   - 展示在官方音色之前。
   - 标记“可能匹配，需要确认”。
   - 用户点击后可选择使用。
5. 官方音色推荐继续展示，但排在个人音色候选之后。
6. 用户仍可点击“重新克隆”。

### 前端展示建议

列表分组：

```text
个人音色库
  强匹配：查理·芒格 · 2026-05-16 19:17
  可能匹配：查理·芒格 · 2026-05-13 10:05

推荐官方音色
  ...

其他官方音色
  ...
```

候选卡片展示字段：

- 音色 label
- 匹配原因
- 来源视频标题
- 来源说话人
- 样本时长
- 最近克隆时间
- 是否已校准语速

不展示：

- `source_content_hash`
- MD5 / SHA-256
- provider 原始机器 ID
- segment id 列表

## 后期编辑流程

后期编辑的音色修改应与 Studio 使用同一个候选接口和排序规则。

建议流程：

1. 打开某个说话人的音色修改面板。
2. 加载该说话人的个人音色候选和官方音色候选。
3. 个人音色候选排在官方音色前。
4. 用户选择个人音色后，写 `voice_map` 时带 `voice_reuse=true`。
5. `record_voice_reuse` 记录 `voice_reuse_postedit` 事件。

后期编辑不应只在“克隆弹窗”里提示复用。音色修改本身就是选择音色，个人音色应自然出现在列表中。

## Smart 智能版流程

### 后台策略字段

建议在 admin settings 中新增：

```text
smart_auto_clone_enabled: bool = true
smart_reuse_user_voice_enabled: bool = true
smart_pause_on_possible_user_voice_match: bool = false
```

含义：

- `smart_auto_clone_enabled`：是否允许 Smart 自动新克隆。
- `smart_reuse_user_voice_enabled`：是否允许 Smart 复用已有个人音色。
- `smart_pause_on_possible_user_voice_match`：遇到弱候选是否暂停给用户确认。

这三个开关必须独立。复用已有音色不等于新克隆，不能被 `auto_voice_clone=false` 一起关掉，除非管理员明确关闭复用。

这些开关应挂在 Gateway 现有 admin settings 事实源中，例如项目已有的 `admin_settings.json` 或对应 Gateway 管理接口；前端只消费 Gateway 返回值，不硬编码默认策略。

### Consent × Admin 决策矩阵

本方案选择语义 A：`smart_consent.auto_voice_clone` 只约束“新克隆”，不约束“复用已有个人音色”。原因是复用不调用 clone provider、不消耗克隆点数，也不违反 `no_extra_charge_without_confirmation`。如果后续产品需要让用户单独控制复用，可再新增 `smart_consent.auto_voice_reuse`，但本阶段不引入新 consent 字段。

| `smart_consent.auto_voice_clone` | `smart_reuse_user_voice_enabled` | `smart_auto_clone_enabled` | 预期行为 |
|---:|---:|---:|---|
| true | true | true | 先查个人音色；强匹配自动复用；弱匹配按暂停开关处理；未复用时可新克隆 |
| false | true | true | 先查个人音色；强匹配自动复用；弱匹配按暂停开关处理；未复用时不能新克隆，走 preset |
| false | true | false | 先查个人音色；强匹配自动复用；弱匹配按暂停开关处理；未复用时走 preset |
| true | false | true | 不查个人音色；可新克隆 |
| true | true | false | 先查个人音色；强匹配自动复用；弱匹配按暂停开关处理；未复用时走 preset |
| false | false | true | 不查个人音色；不能新克隆，走 preset |
| true | false | false | 不查个人音色；不能新克隆，走 preset |
| false | false | false | 不查个人音色；不能新克隆，走 preset |

如果弱匹配暂停开启且用户拒绝候选，后续是否能新克隆仍由 `smart_consent.auto_voice_clone && smart_auto_clone_enabled` 决定。

### 推荐决策顺序

对每个主说话人：

1. 如果 `smart_reuse_user_voice_enabled=true`，查询个人音色候选。
2. 如果有 `same_source_strong`，自动复用。
3. 如果有 possible candidate：
   - 若 `smart_pause_on_possible_user_voice_match=true`，暂停到 voice review，提示用户确认。
   - 否则忽略弱候选，继续后续决策。
4. 如果需要新克隆：
   - 检查用户 consent。
   - 检查 `smart_auto_clone_enabled`。
   - 检查样本时长。
   - 检查 quota 水位线。
   - 调用 clone provider。
5. 如果不允许新克隆或条件不足，走官方 preset。

关键不变量：

- 强匹配复用不受样本时长限制。
- 强匹配复用不消耗 clone quota。
- 强匹配复用不调用 provider。
- 弱匹配不能自动复用。
- `smart_auto_clone_enabled=false` 只禁止新克隆，不应默认禁止强匹配复用。

复制出来的 Smart job 必须重新查询个人音色候选，不依赖原 job 的 voice decision 记录。`copy_as_new` 继承 `source_content_hash` 时，强匹配应再次命中；如果原个人音色已删除或过期，则按正常流程 fall through 到新克隆或 preset。

### Smart 弱匹配暂停

暂停原因建议：

```text
possible_user_voice_match_requires_confirmation
```

质量报告给用户的解释：

```text
发现可能匹配的个人音色，需要你确认是否复用。复用不会消耗克隆点数；如果不确定，可以选择官方音色或重新克隆。
```

后续试听增强：

- 播放当前视频该说话人的原音频片段。
- 播放候选个人音色的 TTS 试听。
- 用户确认复用、拒绝并继续克隆、或选择官方音色。

本阶段只预留数据结构，不强制实现试听 UI。

该开关默认关闭。若管理员开启 `smart_pause_on_possible_user_voice_match`，Smart job 提交页或提交确认弹窗必须说明：系统发现可能匹配的个人音色时，任务可能暂停等待用户确认。否则用户会把 Smart 理解为“全自动跑到底”，突然进入 review 会破坏预期。长期可以考虑把该能力做成用户侧 opt-in，例如“严格复用个人音色模式”。

## 计费和审计

规则：

- 复用个人音色永远不扣克隆点数。
- 新克隆才进入 Studio 的 clone reserve/capture，或 Smart 的套餐内自动克隆逻辑。
- Smart 的 `no_extra_charge_without_confirmation` 契约不得因为复用/克隆模块化而改变。

必须记录的事件：

```text
studio_user_voice_auto_selected
studio_user_voice_confirmed
studio_user_voice_candidate_rejected
smart_user_voice_reused_strong
smart_possible_user_voice_match_paused
smart_possible_user_voice_match_confirmed
smart_possible_user_voice_match_rejected
postedit_user_voice_confirmed
```

可以先复用现有 `UsageMeter.record_voice_reuse()`，但事件 `extra` 中要保留：

- `event_id`
- `source`
- `match_scope`
- `match_confidence`
- `match_reason`
- `source_user_voice_id`
- `source_content_hash`
- `source_speaker_id`
- `user_action`

`user_action` 建议限定为以下枚举，避免不同入口写出不同词：

```text
auto_selected
confirmed
rejected
changed_to_official
clone_requested
ignored_by_policy
```

新克隆被拦截时，事件 `reason_code` 也建议有限定枚举，区分 consent 拒绝 vs 管理员关闭：

```text
new_clone_blocked_by_consent              # smart_consent.auto_voice_clone = false
new_clone_blocked_by_admin                # admin.smart_auto_clone_enabled = false
new_clone_blocked_by_consent_and_admin    # 两者都拒绝
```

否则后续从审计数据反查“为什么没新克隆”时只能看到统一的 fall-through 到 preset，无法区分用户拒绝还是后台策略关闭。

用户在同一个 job 内拒绝过的个人音色候选，建议写入现有 `review_state.json` 的 voice selection review payload，例如 `rejected_user_voice_candidates: list[str]`。它跟 voice selection 生命周期一致，也已有并发管理；不要再散落到 `metering_snapshot` 或临时 sidecar 中。

## 数据模型影响

现有 `user_voices` 来源字段基本够用：

- `source_content_hash`
- `source_video_title`
- `source_type`
- `source_ref`
- `source_speaker_id`
- `source_speaker_name`
- `source_speaker_name_key`
- `source_published_at`
- `source_content_summary`
- `source_content_era`
- `source_content_tags`
- `clone_sample_seconds`
- `clone_sample_segment_ids`
- `created_from`

本阶段不建议新增声纹字段。跨视频弱匹配先基于 speaker name 和来源 metadata 排序。

如果后续需要更强跨视频匹配，可以新增：

```text
voice_embedding_ref
speaker_identity_confidence
last_user_confirmed_reuse_at
reuse_confirmed_count
reuse_rejected_count
```

但这些不进入本阶段。

## 兼容性和历史音色

Phase 0 之前创建的历史个人音色通常缺少 `source_content_hash`，不会进入强匹配或同源候选。它们仍应在个人音色库页面可见，也可以通过后续“浏览全部个人音色 / 手动选择个人音色”的入口被用户主动选择，但不参与自动复用或弱匹配排序。

如果用户历史音色很多，后续可以单独做“历史音色来源回填”专项：从旧 job、审计事件、文件名或用户手工标注中补 `source_content_hash / source_speaker_name_key / source_video_title`。这不进入本方案 Phase 1-4。

## 实施阶段

### Phase 1：统一候选接口和 matcher 扩展

内容：

- 扩展 matcher 支持同源强候选、同源弱候选、跨视频同名候选。
- 增加泛化名称过滤。
- 返回候选列表而不是单一 match。
- 保持旧 `voice-match` 和 internal `match` 兼容。

验收：

- 同视频同 speaker_id 返回 `same_source_strong`。
- 同视频同名不同 speaker_id 返回 possible candidate。
- 跨视频同名返回 possible candidate。
- `Speaker A / 未知说话人 / 主持人` 不参与跨视频弱匹配。
- 跨 provider 不匹配。
- 过期音色不匹配。

### Phase 2：Studio 和后期编辑候选前置

内容：

- Voice selection 页面加载每个 speaker 的个人音色候选。
- 个人音色候选排在官方音色前。
- 强匹配默认预选。
- 弱候选展示“需要确认”。
- 后期编辑音色修改复用同一候选接口。

验收：

- 用户不点击“克隆音色”也能看到个人音色候选。
- 强匹配个人音色默认选中且不扣克隆点。
- 选择个人音色后审批 payload 带 `voice_reuse=true`。
- 后期编辑选择个人音色后 `/editing/voice-map` 带 `voice_reuse=true`。
- 官方音色推荐仍可选。

### Phase 3：Smart 策略开关

内容：

- Admin settings 新增三个开关。
- Smart 读取开关。
- `smart_reuse_user_voice_enabled=false` 时不查复用。
- `smart_auto_clone_enabled=false` 时禁止新克隆，但不禁止强匹配复用。

验收：

- 默认行为保持：允许复用，允许自动克隆，弱候选不暂停。
- 关闭自动克隆后，强匹配仍能复用。
- 关闭复用后，Smart 不查个人音色候选。
- 所有开关由 Gateway 管理，前端不硬编码。

### Phase 4：Smart 弱匹配暂停

内容：

- Smart 查询 possible candidates。
- 有 possible candidate 且后台开关开启时，进入 handoff。
- voice review payload 带候选列表和 match evidence。
- 用户确认复用后继续，不扣克隆点。
- 用户拒绝后可继续新克隆或选官方音色。

用户拒绝候选的审计事件由 Gateway 的 voice-selection/approve handler 写入，与现有 `_record_voice_reuse_events` 走同一个入口。Pipeline 不重新读 review_state 推断拒绝信息——避免 audit emit 路径分裂。

验收：

- 弱匹配不自动复用。
- 开关关闭时弱匹配不暂停。
- 开关开启时弱匹配暂停并展示候选。
- 用户确认复用记录审计。
- 用户拒绝候选也记录审计。

### Phase 5：试听对比增强

内容：

- 候选个人音色支持试听。
- 当前视频原音频片段支持试听。
- UI 提供左右对比。

验收：

- 试听失败不阻塞用户选择。
- 不引入新的默认外部 API 依赖。
- 试听调用计费和缓存策略清晰。

## 测试计划

### 后端 matcher

- `test_user_voice_candidates_same_source_strong_auto_reuse`
- `test_user_voice_candidates_same_source_named_requires_confirmation`
- `test_user_voice_candidates_cross_source_named_person_requires_confirmation`
- `test_user_voice_candidates_filters_generic_speaker_names`
- `test_user_voice_candidates_filters_generic_names_after_normalization`
- `test_user_voice_candidates_provider_isolation`
- `test_user_voice_candidates_skips_expired`
- `test_user_voice_candidates_sorting_prefers_recent_long_calibrated`

### Studio

- `test_voice_selection_candidates_personal_before_official`
- `test_voice_selection_strong_personal_voice_preselected`
- `test_voice_selection_reuse_payload_sets_voice_reuse_true`
- `test_voice_selection_clone_still_charges_when_user_chooses_clone`

### Post-edit

- `test_postedit_voice_candidates_personal_before_official`
- `test_postedit_voice_map_reuse_records_non_billable_audit`

### Smart

- `test_smart_strong_reuse_works_when_auto_clone_disabled`
- `test_smart_reuse_works_when_consent_disables_new_clone`
- `test_smart_reuse_disabled_skips_user_voice_candidates`
- `test_smart_possible_match_pauses_when_enabled`
- `test_smart_possible_match_ignored_when_pause_disabled`
- `test_smart_possible_match_never_auto_reuses`
- `test_smart_no_extra_clone_charge_for_reuse`

### Admin settings

- `test_admin_settings_defaults_for_smart_voice_policy`
- `test_admin_settings_update_smart_voice_policy`
- `test_frontend_consumes_gateway_smart_voice_policy`

## 风险和防护

### 跨视频误匹配

风险：同名不同人、泛化名字、AI 误识别 speaker name。

防护：

- 跨视频候选只提示，不自动复用。
- 泛化名称过滤。
- UI 明确展示来源视频、来源说话人和匹配原因。
- 记录用户拒绝候选事件，后续用于调权。

### Smart 过度暂停

风险：弱候选过多导致智能版经常暂停，削弱自动化体验。

防护：

- `smart_pause_on_possible_user_voice_match` 默认关闭。
- 每个 speaker 最多展示有限候选，例如 3 个。
- 只在候选分数达到阈值时暂停。

### 前端规则漂移

风险：Studio 和后期编辑各自拼候选排序，规则不一致。

防护：

- 后端返回已排序候选。
- 前端只负责展示和用户选择。
- Studio / Post-edit 使用同一个 candidate API。

### 计费语义混淆

风险：把复用当作 clone capture，或 Smart 自动克隆被错误收取单次克隆点。

防护：

- 复用事件统一走 `record_voice_reuse()`。
- 新克隆和复用在结果对象中明确区分。
- Smart 不进入 Studio clone reserve/capture 路径。

## 开放问题

1. Studio 强匹配是否应默认预选，还是只排第一但不预选？
   - 建议默认预选，但 UI 明确可改。
2. 跨视频同名候选的默认数量上限是多少？
   - 已定：每 speaker 最多 3 个。`match_user_voices()` 的 `limit` 参数默认值与候选 API 默认值都统一到 3，避免 Studio/Smart/Post-edit 各传不同 magic number。
3. Smart 弱匹配暂停默认是否开启？
   - 建议默认关闭，后台可开启。
4. 用户拒绝某个候选后，是否在同一个 job 内不再提示？
   - 已定：同 job 内写入 `review_state.json`，避免反复打扰。
5. 是否需要对“用户确认过的跨视频候选”提升到后续自动复用？
   - 本阶段不建议。可以先增加 `last_user_confirmed_reuse_at` 和 `reuse_confirmed_count` 作为未来依据。

## 推荐结论

建议按 Phase 1 到 Phase 4 逐步实施，先让个人音色库在 Studio 和后期编辑中成为第一候选源，再让 Smart 通过后台策略控制强复用和弱匹配暂停。

最重要的产品边界是：

- 强匹配可以自动复用。
- 弱匹配只能提示用户确认。
- 跨视频弱匹配绝不自动复用。
- 复用已有个人音色永远不扣克隆点数。
- 新克隆和复用必须在审计与计费语义上保持分离。

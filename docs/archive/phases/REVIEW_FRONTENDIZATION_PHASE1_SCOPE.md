# REVIEW_FRONTENDIZATION_PHASE1_SCOPE.md

## 1. 本阶段唯一目标

将 `speaker_review` 和 `translation_review` 的主处理路径搬进新前端，使单用户在新前端内即可发现 review、进入 review 页面、完成 approve / continue，并看到任务状态真实前进。

旧 Web UI 本阶段保留为 fallback，但不再作为这两类 review 的主路径。

## 2. 为什么现在做这一阶段

- 当前新前端已经能覆盖新建翻译、当前任务、项目详情，但 review 仍需跳转旧 Web UI，体验割裂。
- 单用户 Web Console 的连续使用链路已经基本收口，最明显的剩余断层就是 `speaker_review` 和 `translation_review` 仍未原生前端化。
- 现有后端已经提供这两类 review 的读取与 approve 契约，具备做最小原生页面的条件，不需要先做第二批页面或重构 review 系统。

## 3. 本阶段必须做

- 明确 `speaker_review` 与 `translation_review` 的现有后端契约：
  - review 数据从哪里取
  - approve 动作走哪个现有接口
  - approve 后是否已自动 continue
  - 页面真正必须依赖哪些字段
- 在新前端最小实现两个原生页面：
  - `speaker_review`
  - `translation_review`
- 将当前任务页、项目详情页里的 review 入口改为优先进入新前端 review 页面。
- 保留旧 Web UI fallback 入口，供异常或未覆盖能力时兜底。

## 4. 本阶段明确不做

- 不做 `voice_review` 原生化
- 不做第二批页面
- 不做“我的项目”
- 不做设置页
- 不做 internal 页面
- 不做登录/注册
- 不做数据库
- 不做多用户
- 不做商业化页面
- 不做 failed resume UI
- 不重构整个 review 系统
- 不改后端核心 review 语义，除非是极小必要适配

## 5. 本阶段依赖的现有后端契约

- review 发现入口：
  - 当前任务数据里的 `review_gate.stage`
  - 旧 Web UI 快照 `/api/state` 中的 `results.review_flow.active_review`
- review 页面展示数据：
  - `speaker_review` 依赖 `/api/state` 返回的 `results.transcript_review.items` 与 `results.review_flow.stages.speaker_review.payload`
  - `translation_review` 依赖 `/api/state` 返回的 `results.translation_review.items` 与 `results.review_flow.stages.translation_review.payload`
- approve 契约：
  - `POST /api/review/speaker/approve`
  - `POST /api/review/translation/approve`
- 当前真实语义：
  - approve 接口内部已经会调用 `continue_after_review(...)`
  - 因此前端不需要额外发明新的 continue 状态机
- 页面最小必须字段：
  - `project_dir`
  - `segment_id`
  - `speaker_id`
  - `display_name`
  - `source_text`
  - `cn_text`
  - `tts_cn_text`
  - `speaker_confirmed`
  - `transcript_confirmed`
  - `translation_confirmed`
  - `rewrite_requested`
  - `review_updated_at`
  - `review_gate.stage`

## 6. 验收标准

- 当任务进入 `speaker_review` 时，新前端可直接进入原生 review 页面并完成 approve，任务状态随后真实前进。
- 当任务进入 `translation_review` 时，新前端可直接进入原生 review 页面并完成 approve，任务状态随后真实前进。
- 当前任务页与项目详情页不再把这两类 review 的主入口指向旧 Web UI。
- 在正常链路下，单用户完成这两类 review 不再依赖旧 Web UI。
- 当原生 review 页面异常或契约不满足时，旧 Web UI fallback 仍可用。

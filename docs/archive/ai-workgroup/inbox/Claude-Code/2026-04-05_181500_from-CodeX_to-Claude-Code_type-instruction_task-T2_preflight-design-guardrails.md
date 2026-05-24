---
id: T2-msg-001
task: T2
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: ""
requires_human: false
created_at: 2026-04-05 18:15 Asia/Shanghai
---

# T2 前置注意事项：DESIGN.md 更新结论与执行边界

## 背景

当前已确认以下事实：

- `T1` 已完成并放行，目的主要是前端布局拆分、providers-only root layout、`(marketing) / (auth) / (app)` route groups 建立。
- `T0` 的 gateway 真相源统一已完成主体交付，`GET /api/plans` 已存在。
- Trae 的 `WG2` 第二轮修订报告已可采纳。
- 根目录正式 `DESIGN.md` 已更新，并吸收了 `WG3` 的增量收口结论：
  - `/D:/Claude/AIVideoTrans_Codex_web_mvp/DESIGN.md`

这条消息不是正式的 `Task 2` 实施指令，而是 **Task 2 开始前必须读取的前置 guardrails**，用于避免 marketing 实现跑偏。

## CodeX 结论：是否需要回改 T1 / T0

### 1. T1 不需要因为 DESIGN.md 更新而重做

`T1` 的核心交付是结构性迁移，不是正式 marketing 视觉实现，因此：

- `providers-only` root layout
- `(marketing) / (auth) / (app)` 三层拆分
- `/` 从重定向改为 marketing 占位入口
- guest / logged-in CTA 的最小切换

这些都 **继续有效，不需要返工**

但请注意：

- 当前 marketing 占位页和占位 header 里的视觉只是 **临时占位实现**
- 在 `Task 2` 正式实现 marketing 页面时，应主动覆盖它们，而不是把占位视觉继续当成最终设计

具体需要在 `Task 2` 中被覆盖/替换的占位点：

- `frontend-next/src/app/(marketing)/page.tsx`
  - 当前仍是最小占位页
  - 当前 logo/图形使用 `from-violet-500 to-cyan-500`
- `frontend-next/src/app/(marketing)/layout.tsx`
  - 当前 header logo 也仍是 `from-violet-500 to-cyan-500`

这与新版 `DESIGN.md` 中“不要默认落入 AI purple 风格”的要求不一致，但它属于 **Task 2 应覆盖的临时占位问题**，不是要求你回头重开 `T1`

### 2. T0 不需要因为 DESIGN.md 更新而重做 gateway 真相源

`T0` 的核心是 gateway truth consolidation，与 `DESIGN.md` 没有冲突：

- `gateway/plan_catalog.py`
- `GET /api/plans`
- `billing.py` / `job_intercept.py` 改为派生消费 gateway 真相源

这些都继续有效，不需要因设计文档更新而返工

但有一个 **既有技术注意事项** 需要你在 `Task 2` 接 pricing/trial 页面前记住：

- `frontend-next/src/lib/billing/get-plans.ts` 当前使用 `fetch("/api/plans")`
- 这在浏览器端可用，但在 Node / server component / SSR 环境下不是天然安全的
- 因此如果 `Task 2` 的 pricing / trial 页面计划用 SSR 或 server component 直接取 plans，你不能默认复用它而不处理

换句话说：

- 这不是 `DESIGN.md` 导致的返工
- 但它是 `Task 2` 实施时必须留意的前置技术边界

## Task 2 必须遵守的 DESIGN.md 结论

请在后续 `Task 2` 实施中严格遵守以下结论：

### 1. 分层适用范围

- `marketing` 层强适用 `DESIGN.md`
- `(app)` workspace、billing、admin 只继承 foundations + guardrails
- 不要把 marketing 表达层直接外溢到 workspace、billing、admin

### 2. Marketing 视觉方向

- 整体方向应是 `dark-capable / contrast-led`
- Hero / demo 可使用更深的媒体导向表面
- pricing / FAQ / forms 应优先保证中文长文本可读性，必要时转为更中性、更亮的阅读面
- 不要默认使用紫色 AI 模板风格
- 外部参考站只能作为气质参考，不能直接克隆 UI

### 3. 首页

首页正式实现时，优先遵循以下结构：

- Hero
- product proof / demo
- feature explanation
- pricing
- FAQ
- final CTA

并且：

- CTA 用直接中文表达
- hero 文案不要过度抽象或诗化
- 当前占位页的紫到青渐变图形不应沿用为最终品牌表达

### 4. 定价页

marketing 层 pricing presentation 只能按以下口径呈现：

- `Free`
- `Plus`
- `Pro`

同时必须遵守：

- `Trial` 是状态 / 转化入口，不是第四个长期 tier
- 如需展示 `Trial`，应以 banner / tag / entry 方式呈现，而不是第四张套餐卡
- 价格、分钟数、配额、支付方式声明，必须消费 gateway 真相源，不得手写硬编码

### 5. Trial 页

- Trial 页应强调低摩擦和信任感
- 必须明确解释试用结束后会发生什么
- 不得默认暗示自动扣费
- Trial 天数、分钟数、资格规则，不得由静态 marketing 文案擅自定义

### 6. App / Billing / Admin 禁止项

后续若 `Task 2` 或其他任务碰到 `(app)` / billing / admin，请记住这些内容不应直接套入：

- hero-led drama
- oversized landing-page typography
- marketing slogans as primary UI hierarchy
- 戏剧化动效
- 为视觉氛围牺牲信息密度与扫描效率

## Task 2 实施前的技术提醒

如果后续正式执行 `Task 2`，请先在任务开始时显式判断以下问题：

1. pricing / trial 页面是否打算在 server component / SSR 中直接取 `GET /api/plans`
2. 如果是，需先修正 `get-plans.ts` 的 server-safe 读取方式，或明确采用 client-side 获取方案
3. 不要在 marketing 页面里为了临时占位重新手写价格、minutes、trial 数字

## 禁止事项

在没有单独指令时，以下内容仍禁止提前进入 `Task 2`：

- 不擅自修改 gateway 真相源字段定义
- 不擅自冻结 Trial 数字
- 不擅自新增新的 pricing tier
- 不擅自更改 payment / auth / subscription 迁移顺序
- 不把 `DESIGN.md` 当成修改 `(app)` / billing / admin 视觉风格的许可

## 需要你记住的一句话

`T1` 的结构交付继续有效，`T0` 的 gateway 真相源继续有效；  
`Task 2` 需要做的是 **基于新版 DESIGN.md 正式实现 marketing 层**，而不是回头重写前两个阶段。

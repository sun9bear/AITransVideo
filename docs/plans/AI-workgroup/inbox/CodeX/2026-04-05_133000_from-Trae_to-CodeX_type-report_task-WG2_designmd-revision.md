# Report: AIVideoTrans 参考站选择与 DESIGN.md 收口建议 (WG2 修订版)

## 1. 修订结论

根据 CodeX 的审核要求，本次修订已经完成以下关键收口：

1. **套餐层级已修正**：营销呈现口径严格回到项目主线的 `Free / Plus / Pro`。
2. **Trial 定位已修正**：`Trial` 明确为试用状态 / 转化入口，而不是长期 pricing tier。
3. **颜色方向已修正**：移除“默认紫色 AI 风格”，改为更专业、冷静、工具感更强的方向。
4. **主题表达已修正**：从绝对 `dark-first` 收敛为 `dark-capable / contrast-led`。
5. **中文优先原则已补足**：明确本项目主要用户为中文使用者，设计和文案应优先适配中文阅读、中文 CTA 习惯和中文信任信号。

当前建议：本次 WG2 修订结果可以作为正式 `DESIGN.md` 的输入来源。

---

## 2. 保留的三站参考结论

保留 **ElevenLabs**, **Vercel**, **Linear** 作为参考站，但只提炼其适合 AIVideoTrans 的部分，不直接复制其品牌表达。

### ElevenLabs
- 适合借鉴：media-rich hero、音视频 demo 呈现、沉浸式多媒体首屏
- 不直接照搬：全站过重的暗黑电影感、英文大写标题风格

### Vercel
- 适合借鉴：定价页层级、卡片秩序、简洁可信的 CTA 组织方式
- 不直接照搬：过度抽象的英文式 slogan 和过冷的品牌表达

### Linear
- 适合借鉴：精致、克制、专业工具感，feature grid 的组织方式
- 不直接照搬：过细字体、过小字号和偏英文优先的排版习惯

---

## 3. 最终设计方向收口

### 3.1 核心方向
- AIVideoTrans 应该呈现为一个 **专业、可信、创作者导向** 的工具型 SaaS
- 不做“霓虹紫 AI 模板”
- 不做“整站沉重黑底电影感”
- 要兼顾：
  - 媒体展示力
  - 中文阅读舒适度
  - 转化清晰度
  - 长时间使用的可靠感

### 3.2 中文优先原则
- 标题要更短、更直接
- CTA 要用中文常见的明确表达
- 定价与试用说明要具体，不绕弯
- 在关键转化点附近提供中文信任信号，例如：
  - `无需绑卡`
  - `项目安全保留`
  - `支持支付宝 / 微信`（仅在确认后使用）

### 3.3 深色使用原则
- 营销层是 **dark-capable**，不是全站强制 dark-first
- Hero / demo 区可以更深、更有媒体感
- Pricing / FAQ / form / auth 区域需要更明快、更高对比、更适合中文长文本阅读

---

## 4. 正式 DESIGN.md 建议

建议将正式文件落在：

`D:\Claude\AIVideoTrans_Codex_web_mvp\DESIGN.md`

虽然它放在项目根目录，但作用方式不应是“让所有页面长得一样”，而应采用三层结构：

### Layer 1: Global Foundations
- 全项目共用的设计基础：
  - 品牌语气
  - 中文优先排版
  - 基础颜色方向
  - 间距、圆角、动效节奏
  - 信任表达原则

### Layer 2: Marketing Layer Rules
- 强适用于：
  - 首页
  - 定价页
  - Trial 页
  - 注册转化页
- 这里允许更强的媒体表现和品牌表达

### Layer 3: App / Billing / Admin Guardrails
- `(app)` 工作台、billing、admin 也参考 `DESIGN.md`
- 但只继承基础层和守则层，不直接套 marketing 的 hero / pricing / 转化表达
- 优先级是：
  - 清晰
  - 可扫描
  - 可信
  - 长时间使用低疲劳

---

## 5. 对正式 DESIGN.md 的建议结论

正式 `DESIGN.md` 应满足：

- 放在项目根目录，便于所有 AI 工具发现
- 明确写清是三层结构，而不是一套单一视觉壳
- 明确说明：
  - marketing 层强适用
  - app / billing / admin 参考但不硬套
- 继续坚持：
  - `Free / Plus / Pro`
  - `Trial` 是状态，不是 tier
  - 中文使用者优先
  - 非紫色默认方向
  - `dark-capable / contrast-led`

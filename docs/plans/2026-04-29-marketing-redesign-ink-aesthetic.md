# Marketing 站重构：水墨意境 + 转化型叙事

**日期**：2026-04-29
**状态**：方案草案，待第一阶段任务拆分（T1-Tn）
**作用范围**：仅 `frontend-next/src/app/(marketing)/` 与 `frontend-next/src/components/marketing/`，不动 `(app)/workspace`、`(app)/studio`
**触发**：站点 [aitrans.video](https://aitrans.video/) 现状盘查 + 用户希望"重构有独特感、符合中文审美"
**指导隐喻**：
> 西方油画 → 中国水墨画 = 跨文化本地化的视觉化。
> 这个比喻和产品价值主张严丝合缝，不是装饰，是叙事本身。

---

## 1. 背景与目标

### 1.1 现状盘查（基于 SSR HTML 抓取，2026-04-29）

按严重度排序的 5 个真实问题：

1. **`/pricing` 页 SSR 不渲染真实价格**。HTML 里只有"三档套餐覆盖..."描述文字 + FAQ，没有 Free / Plus / Pro 卡片、价格、特性对比。`use-plans.ts` 是 client-only fetch 且无 SSR fallback。**用户进定价页看到空架子，转化漏斗到这里直接断掉**。优先级最高，可能就是个 fetch bug。
2. **`product-proof.tsx` 的"真实产品证明"是文字仿造的 mockup**。Job ID 用 `Bed88548...` `28854cd7...` 占位 hash 渲染，不是真实截图。组件名叫 ProductProof 但**没有 proof**。
3. **Hero 缺视觉证据**。文案 `"精准对齐的视频翻译，直出剪映草稿"` 命中差异化没问题，但 hero 区只有文字 + 两个按钮，没有任何能"看一眼就懂"的视觉。
4. **Trial 页 + Trial banner 双重硬编码** `7 天 / 20 分钟`。Gateway 改一次 trial policy，前端两个地方要同步改。`use-plans.ts` 已经存在，trial 数据应该走同一条线。
5. **`workflow-showcase.tsx` 四步纯文字**。这是产品最强的故事（导入 → 翻译配音 → 人工复核 → 导出剪映），但用纯文字讲完了，每步至少应该有一张产品局部截图。

### 1.2 不是问题的部分（确认保留）

- 信息架构顺序：hero → product-proof → features → workflow → pricing-preview → trial-banner → faq → final-cta。这个排序就是经过两轮讨论后的目标顺序，**已经做对了**，不动结构。
- 页脚合规信息（运营主体 / 地址 / 邮箱 / 退款 / 隐私 / 条款）齐全。中文备案合规这一面已经站住。
- 导航简洁（首页 / 定价 / 免费试用 / 登录），没有冗余菜单。

### 1.3 第一阶段 KPI

**只盯一个**：marketing 页首屏访问 → `/trial` 或 `/workspace` 的点击率。

不在第一阶段考核：注册转化、付费转化、demo 任务完成率。这些指标在 marketing 层之外有更多干扰变量。

### 1.4 显式不做（Non-goals）

- ❌ 不动 `(app)/workspace` 和 `(app)/studio`（用户在干活，意境是干扰）
- ❌ 不做匿名/游客 demo（成本、滥用、版权风险，第二阶段评估"预置 demo + 单段交互"形态）
- ❌ 不做费用试算器（第二阶段，依赖更稳定的 Gateway pricing API）
- ❌ 不做 i18n 多语言（第一阶段把中文转化做扎实再抽 i18n）
- ❌ 不做 Before/After A/B 播放器（第二阶段，依赖真实样例素材）
- ❌ 不做 SEO 全套（OG image / sitemap / JSON-LD 推到第二阶段，先把首屏视觉立住）

---

## 2. 设计概念：油画 → 水墨

### 2.1 隐喻的产品对应

| 视觉语言 | 产品语义 |
|---|---|
| 油画（厚涂、饱和、信息密集） | 西方原始素材（英文视频、稠密信息、剪辑师拿到的"死字幕"） |
| 中间过渡（颜料溶解、墨化） | AIVideoTrans 的处理流程（DSP 对齐、人工复核、增量重生成） |
| 水墨（留白、单笔、朱砂印章） | 交付给中文创作者的最终成片（剪映草稿、可继续精修、专业输出） |

这个比喻是少有的**视觉概念 = 产品价值主张**的情况。HeyGen / Rask / Synthesia 等竞品不可能用，因为他们不是为中文创作者做的。**3-5 年的视觉壁垒**。

### 2.2 五个雷区（避免做成博物馆 / 茶叶品牌站）

1. **不要全站水墨化**。水墨是前台戏剧化的语言，不是工作流的语言。Marketing 走水墨，`/workspace` 必须保持冷峻工具感。
2. **不要堆中式符号**。禁用：龙、灯笼、祥云、福字、毛笔字横批、卷轴边框、宣纸纹理铺满。
3. **不要用毛笔字做正文**。书法只能做 logo / hero 大字 / 章印。
4. **不要把油画做得太具象**。要做意境层面的对比，不是图像层面的转换（避免变成"AI 滤镜玩具"）。
5. **当心 WebGL 性能黑洞**。能用 SVG + CSS 实现就别上 WebGL，否则移动端会卡。

### 2.3 把"油画→水墨"拆成 6 个抽象维度

落到具体设计 token：

| 维度 | 油画方 | 水墨方 | 在网站上怎么用 |
|---|---|---|---|
| **信息密度** | 满构图 | 大留白 | 每个 section 强制 60% 以上空白 |
| **色彩** | 饱和厚重 | 黑白灰 + 朱砂 | 宣纸米白主调 + 朱砂红 CTA |
| **笔触** | 块面堆叠 | 单笔起承转合 | 分割线、按钮 underline 用有起笔/收笔的 SVG path |
| **质感** | 油脂厚涂 | 水墨晕染 | hover 状态用墨点扩散动画（CSS radial-gradient） |
| **构图** | 透视纵深 | 平远高远 | 不用 3D mockup，所有截图正面、悬浮、阴影做层次而非透视 |
| **节奏** | 满 | 虚-实-虚-实 | 内容区交替"密集信息块"和"大留白引言区"，呼吸感 |

### 2.4 参考方向（避免走偏）

**要看的**：
- Apple 中国春节短片视觉调性（克制的中式 + 现代）
- 故宫文创近 3 年包装（朱砂 + 米白 + 极简版式）
- 三联生活周刊 / 单读杂志（中文衬线 + 留白 + 编辑性强的版面）
- 字节跳动品牌系统（中文型设极简）

**不要看**：
- 知乎旧版水墨主题
- 各种"诗词 H5"
- 新中式茶饮店（茶颜悦色等）
- 国潮文创合集

---

## 3. 分层策略

```
┌──────────────────────────────────────────┐
│ Marketing 层：完整水墨意境（浅色）       │
│ - / (首页)                               │
│ - /pricing                               │
│ - /trial                                 │
│ - /contact /privacy /refund /terms       │
└──────────────┬───────────────────────────┘
               │ 用户点 CTA
               ▼
┌──────────────────────────────────────────┐
│ 过渡层：30% 水墨（浅色，但更克制）       │
│ - /login /signup                         │
│ - /workspace 列表页（首页性质，不是任务  │
│   工作页）                               │
└──────────────┬───────────────────────────┘
               │ 用户进入具体任务
               ▼
┌──────────────────────────────────────────┐
│ 工具层：完全不动（深色冷峻 Synthetix）   │
│ - /workspace/[id]                        │
│ - /studio/[id]                           │
│ - /admin                                 │
└──────────────────────────────────────────┘
```

**理由**：用户在 marketing 层是"决定要不要用"的心情，意境戏剧化合适；进入 workspace 是"干活"心情，任何意境都是干扰。

**双主题在同一站点的实现**：
- `frontend-next/src/app/(marketing)/layout.tsx` 顶层注入 `data-theme="ink"` 属性
- `frontend-next/src/app/(app)/layout.tsx` 顶层注入 `data-theme="synthetix"` 属性
- `globals.css` 用 `[data-theme="ink"] { ... }` 选择器隔离 token
- 两套 token 不互相覆盖，CSS 级别完全隔离

---

## 4. 设计 Token（可粘贴到 globals.css）

### 4.1 Marketing 层（`data-theme="ink"`）

```css
[data-theme="ink"] {
  /* 底色：宣纸 */
  --ink-paper:          #F5F0E6;  /* 主底（仿宣纸米白） */
  --ink-paper-2:        #EDE6D6;  /* 卡片 / 二级面 */
  --ink-paper-3:        #E2D9C5;  /* 三级面 / 隔断 */

  /* 墨色：文字与笔触 */
  --ink-black:          #1A1A1A;  /* 主文字（不用纯黑，留温度） */
  --ink-gray-1:         #2E2E2E;  /* 次主文字 */
  --ink-gray-2:         #4A4A4A;  /* 副文字 */
  --ink-gray-3:         #8A8580;  /* 辅助 / placeholder */
  --ink-gray-4:         #B8B0A0;  /* 极弱 / 分割线 */

  /* 朱砂：唯一暖色，唯一 CTA 色 */
  --cinnabar:           #C73E3A;  /* 主 CTA / 章印 / 关键数字 */
  --cinnabar-hover:     #B5302D;  /* 主 CTA hover */
  --cinnabar-soft:      rgba(199, 62, 58, 0.08);  /* 朱砂软背景 */

  /* 辅助色（克制使用，仅状态语义） */
  --bamboo-green:       #5A8A6B;  /* 成功 */
  --ochre:              #B5793A;  /* 警告 */
  --error-red:          #C73E3A;  /* 错误 = 朱砂同色 */

  /* 圆角：克制 */
  --radius-sm:          4px;
  --radius-md:          6px;
  --radius-lg:          8px;     /* 上限，不用 12px+，避免"圆角 SaaS" */

  /* 阴影：极轻，模拟纸张抬起 */
  --shadow-sm:          0 1px 2px rgba(26, 26, 26, 0.04);
  --shadow-md:          0 4px 12px rgba(26, 26, 26, 0.06);
  --shadow-lg:          0 12px 32px rgba(26, 26, 26, 0.08);

  /* 间距节奏（呼吸感关键） */
  --space-section:      120px;   /* 桌面 section 间距 */
  --space-section-mob:  72px;    /* 移动 section 间距 */
}
```

### 4.2 字体

```css
[data-theme="ink"] {
  /* Hero / 大标题 */
  --font-display:       "Noto Serif SC", "Source Han Serif SC", Georgia, serif;
  --font-display-weight: 900;  /* Black */

  /* 次标题 / 段落小标题 */
  --font-heading:       "Noto Serif SC", "Source Han Serif SC", Georgia, serif;
  --font-heading-weight: 600;  /* SemiBold */

  /* 正文 */
  --font-body:          "HarmonyOS Sans SC", "PingFang SC", "Microsoft YaHei", "Inter", sans-serif;

  /* 数字 / 价格 / 代码 */
  --font-mono:          "Space Grotesk", "JetBrains Mono", monospace;
}
```

**字体加载策略**：
- Noto Serif SC Black → 通过 `next/font/google` 子集化加载，仅加载 hero 用到的字符（动态扫源码）
- HarmonyOS Sans SC → 自托管 woff2，body / 段落主力
- 中文 fallback 链 PingFang → YaHei 保证 macOS / Windows 正常

### 4.3 工作台层（`data-theme="synthetix"`，**保持现状不动**）

明确写下来作为契约，避免后续误改：

```css
/* 工作台层 token 不在本 plan 范围。保留 globals.css 现有定义不动。 */
[data-theme="synthetix"] {
  /* 现有深色钢青蓝色调，本 plan 不修改 */
}
```

---

## 5. 叙事骨架：问题 → 演示 → 信任 → 行动

### 5.1 完整新顺序

```
Header (sticky, 滚动后 CTA 高亮)
  ↓
[问题] Hero（钩子 + 标题 + 油画→水墨大图）
  ↓
[演示] ProductProof（4 张真截图）
  ↓
[演示] WorkflowShowcase（四步 + 截图）
  ↓
[演示] Features（DSP / 工程化 / 增量重生成）
  ↓
[信任] TrustBanner（新增，6 图标）
  ↓
[信任] PricingPreview（修 SSR bug + Gateway 驱动）
  ↓
[信任] TrialBanner（Gateway 驱动）
  ↓
[信任] FAQ（顺序重排）
  ↓
[行动] FinalCTA
  ↓
Footer
```

### 5.2 四幕的目标 / 现状 / 改造

#### 第一幕「问题」—— 60% 用户在这里决定要不要往下看

| 项 | 内容 |
|---|---|
| **目标** | 5 秒内让用户意识到"这就是我的痛点" |
| **现状差距** | Hero 直接讲产品特性，跳过问题陈述 |
| **改造** | Hero 上方加 pre-headline 钩子：`长视频翻译总在三件事翻车 —— 口型对不上、改一句要重跑全片、剪辑师拿到的是死字幕`<br>主标题保留：`精准对齐的视频翻译，直出剪映草稿`<br>副标题改对照式：`不是又一个 AI 配音工具。是把这三件事单独做对的工作台。`<br>Hero 大图：油画→水墨过渡（图 2） |

#### 第二幕「演示」—— 决定用户要不要继续读

| 项 | 内容 |
|---|---|
| **目标** | 用产品本身说话，不用形容词 |
| **现状差距** | ProductProof 是文字 mockup；Workflow 四步纯文字 |
| **改造** | ProductProof 替换为 4 张真截图（任务创建 / 结果列表 / 复核时间轴 / 剪映草稿目录树）<br>Workflow 每步加一张产品局部截图<br>Hero 区可选叠加 30 秒静音自动循环录屏（依赖用户提供素材，非阻塞） |

#### 第三幕「信任」—— 决定用户要不要点 CTA

| 项 | 内容 |
|---|---|
| **目标** | 把所有"不踩坑"信号集中爆发 |
| **现状差距** | 信任信号散落各处（trial banner / pricing FAQ / footer），不形成合力 |
| **改造** | **新增 TrustBanner** 在 Features 和 PricingPreview 之间，6 个图标 + 一句话：<br>① 无需绑卡 · 试用结束不自动扣费<br>② 任务失败不计费<br>③ 增量重生成只算改动部分<br>④ 项目数据保留 7 天<br>⑤ 仅处理已授权内容<br>⑥ 退款政策清晰可查<br>**修 PricingPreview SSR bug**（最高 ROI，可能就是个 fetch 配置问题）<br>**TrialBanner 改 Gateway 驱动**（消除 7 天/20 分钟硬编码）<br>**FAQ 顺序重排**：`视频来源` / `Studio vs Express` 前置（消除使用前疑虑）；`增量重生成` / `导出格式` 后置（已决定用了在确认细节） |

#### 第四幕「行动」—— 临门一脚

| 项 | 内容 |
|---|---|
| **目标** | CTA 清晰、低门槛、可重复出现 |
| **现状差距** | CTA 散在 hero / pricing / final-cta，文案重复都是"免费开始试用"，无梯度 |
| **改造** | **三个 CTA 位置 + 三种诉求**，避免疲劳：<br>Hero：`免费开始试用`（主，朱砂） + `查看 30 秒产品演示`（次，平滑滚动到演示区）<br>Pricing：`领取 7 天试用`（主） + `查看完整套餐`（次）<br>Final：`免费开始`（主） + `先看定价`（次）<br>**新增 sticky 导航 CTA**：滚动超过 hero 后，顶部"免费开始试用"按钮变高亮态 + 微动效 |

---

## 6. 资产清单

### 6.1 AI 生图（已生成）

| 资产 | 文件 | 用途 | 处理 |
|---|---|---|---|
| **Hero 主图** | `D:\Claude\temp\ChatGPT Image 2026年4月29日 23_46_12.png` | 首页 Hero 背景，油画→水墨横向过渡，左侧带青花瓷瓶 + 红绒布的元叙事 | ① 抹掉原生 AI 印章<br>② 转 webp 多档（1920w / 1280w / 768w）<br>③ 落到 `frontend-next/public/marketing/hero-paper.{webp,jpg}` |
| **Secondary 背景** | `D:\Claude\temp\ChatGPT Image 2026年4月29日 23_44_01.png` | 第二位置（pricing banner 顶部 / features section 半透明叠层），多峰山脉 + 爆裂式过渡 | 同上处理，落到 `hero-secondary-bg.{webp,jpg}` |

**使用红线**：原图右下角的"印章"是 AI 瞎画的字符，**必须抹掉**，否则放大会糊字掉档。前端用独立 `<SealStamp>` SVG 组件叠在该位置。

### 6.2 手写 SVG（不用 AI 生成）

| 资产 | 文件 | 说明 |
|---|---|---|
| **章印 logo** | `frontend-next/src/components/marketing/seal-stamp.tsx` | 朱砂方印，主字「译」（单字最有力），可控的"残缺感"边缘，可参数化字符 / 大小 / 颜色 |
| **飞白分割线** | `frontend-next/src/components/marketing/ink-divider.tsx` | 横向 SVG path，带飞白笔触，section 间分隔 |
| **工作流四步图标** | `frontend-next/src/components/marketing/workflow-icons.tsx` | 简笔水墨风：上传 / 翻译 / 复核 / 导出 |
| **印章 hover 微动效** | `seal-stamp.tsx` 内置 | hover 时印章右下角浮现一笔收尾 |

### 6.3 真实截图（待用户提供）

| # | 截图 | 用途 | 处理建议 |
|---|---|---|---|
| 1 | 新建翻译任务页 | ProductProof slot 1 | 1280px 宽，PNG 转 webp，可裁去边缘 chrome |
| 2 | 项目结果列表 | ProductProof slot 2 | 同上 |
| 3 | 时间轴复核界面（高亮一段在改译文） | ProductProof slot 3、Workflow step 3 复用 | 同上，可加马赛克遮敏感 ID |
| 4 | Studio 三引擎选音色 Tab | ProductProof slot 4、Features 区 | 同上 |
| 5 | 剪映草稿包目录树（资源管理器截图） | Workflow step 4 | 转 webp |
| 6（可选） | 任务运行进度条 | Workflow step 2 | 可省略 |

**截图必须具备**：
- 不暴露真实用户邮箱 / 视频内容（敏感信息打码）
- 高 DPR（Retina 截图，源像素 ≥ 2x 显示像素）
- 浅色主题（如果工作台是深色，先用 marketing 浅色 mockup 替代，第一阶段不要求工作台支持双主题）

### 6.4 录屏（可选，第一阶段不阻塞）

- **30 秒静音自动循环录屏**：时间轴对齐 + 三引擎选音色 + 剪映草稿包打开
- 格式：mp4 + webm 双轨道，`<video preload="none" muted loop playsInline>` + IntersectionObserver 滚到才 play
- 落到 `frontend-next/public/marketing/hero-clip.{mp4,webm}`

### 6.5 文案（已盘点）

- Hero / pricing / trial / FAQ 文案 90% 复用现有，只改 hero 的 pre-headline + 副标题
- TrustBanner 6 项一句话需要新写（见 §5.2）
- 不需要重写法律 / 合规 / 服务条款页

---

## 7. 组件级变更清单

| 组件 | 现状 | 目标 | 改造点 | 依赖资产 | 优先级 |
|---|---|---|---|---|---|
| `hero.tsx` | 纯文字 + 两按钮 | 油画→水墨大图为背景，文字落右半，主 CTA 朱砂红 | ① 加 pre-headline 钩子<br>② 副标题改对照式<br>③ 加 hero 大图 (object-fit: cover, 右半留白叠文字)<br>④ 主 CTA 朱砂 + 落款式 hover 动效<br>⑤ 次 CTA 平滑滚到 ProductProof | hero-paper.webp、SealStamp | P0 |
| `product-proof.tsx` | 文字仿造 UI（Job ID 占位 hash） | 4 张真截图卡片 | ① 删除 mock UI 文字仿造<br>② 用 next/image 渲染 4 张截图<br>③ 截图下方一句话说明（保留现有文案） | 用户提供 4 张截图 | P0 |
| `workflow-showcase.tsx` | 四步纯文字 | 四步配产品局部截图 + 飞白分割 | ① 每步加 next/image<br>② 步骤间用 InkDivider 替代默认分割<br>③ 数字 01-04 改用 Space Grotesk 大字 + 朱砂 | 用户提供 5 张截图、InkDivider | P1 |
| `features.tsx` | 三个特性卡片 | 保留三特性，加水墨视觉锚点 | ① 卡片背景叠 hero-secondary-bg 半透明<br>② 标题字体改 Noto Serif SC<br>③ 关键数字（精度 / 时长 / 节省 %）用朱砂 | hero-secondary-bg.webp | P2 |
| **`trust-banner.tsx`** | **新增** | 6 图标 + 一句话信任卡片 | 新建组件，6 项见 §5.2 | InkDivider、6 个简笔 SVG 图标 | P0 |
| `pricing-preview.tsx` | SSR 不渲染价格（重大 bug） | SSR 渲染 Free / Plus / Pro 三档 | ① 排查 use-plans.ts 为何 SSR 失败<br>② 加 SSR fallback（Gateway 不可达时静态默认值，不阻塞渲染）<br>③ 价格数字用 Space Grotesk 大字 + 朱砂 | 排查 Gateway `/api/plans` 接口可用性 | **P0（最高优先级）** |
| `trial-banner.tsx` | 硬编码 `7 天 / 20 分钟` | Gateway 驱动 | ① 删硬编码字符串<br>② 通过 `usePlans()` 取 trial 数据<br>③ 加 SSR fallback 以防接口不通 | use-plans.ts 已存在 | P0 |
| `faq.tsx` | 4 项 FAQ | 重排 + 主题色更新 | 顺序：视频来源 / Studio vs Express / 增量重生成 / 导出格式 | 无 | P1 |
| `final-cta.tsx` | 主 CTA + 次 CTA | 朱砂主 CTA + 章印装饰 | 主 CTA 改朱砂红 + 章印图样 | SealStamp | P1 |
| `site-header.tsx` | 静态导航 | sticky + 滚动后 CTA 高亮 | ① position: sticky<br>② IntersectionObserver 监听 hero 出视野<br>③ CTA 切到朱砂高亮态 + 微动效 | 无 | P1 |
| `site-footer.tsx` | 现有合规信息 | 章印 logo + 字体微调 | ① 替换 AV 字母 logo 为 SealStamp<br>② 字体更新 | SealStamp | P2 |
| **`globals.css`** | 现有钢青深色主题 | 加 `[data-theme="ink"]` token 块 | 见 §4.1、§4.2 | 字体 webfont | P0 |
| **`(marketing)/layout.tsx`** | 现有 layout | 顶层注入 `data-theme="ink"` | 一行 prop 改动 | globals.css 更新 | P0 |

**P0 必须在第一阶段交付**，P1 第一阶段尽量做完，P2 可推到第二阶段。

---

## 8. 已确认决定（决策日志）

| # | 决定 | 状态 | 备注 |
|---|---|---|---|
| D1 | 第一阶段范围只圈 marketing 层（`/`、`/pricing`、`/trial`），不动 workspace / studio | ✅ 已锁定 | CodeX 修正版采纳 |
| D2 | 叙事骨架：问题 → 演示 → 信任 → 行动 | ✅ 已锁定 | 用户明确肯定 |
| D3 | 视觉概念：西方油画 → 中国水墨意境 | ✅ 已锁定 | 用户提议 |
| D4 | 双主题分层：marketing 浅色水墨 / workspace 深色不动 | ✅ 已锁定 | 由 hero 图色调反推 |
| D5 | 主色：朱砂 `#C73E3A`（不考虑其他红） | ✅ 已锁定 | 平衡度最佳 |
| D6 | 字体：Noto Serif SC Black（Hero）+ HarmonyOS Sans SC（正文）+ Space Grotesk（数字） | ✅ 已锁定 | |
| D7 | LOGO：朱砂方印，单字「译」 | ✅ 已锁定 | 不用「译·像」二字（显挤） |
| D8 | Hero 主图 = `23_46_12.png`（带青花瓷瓶 + 红绒布） | ✅ 已锁定 | 留白多 30%，元叙事强 |
| D9 | 第二位置背景 = `23_44_01.png`（多峰山脉爆裂） | ✅ 已锁定 | 用于 features section / pricing banner 顶 |
| D10 | 章印用独立 SVG，不沿用 AI 生图原生印章 | ✅ 已锁定 | AI 印章是糊字 |
| D11 | 第一阶段 KPI = marketing → /trial 或 /workspace 点击率 | ✅ 已锁定 | |
| D12 | 装饰 SVG 全部手写，不用 AI 生 | ✅ 已锁定 | 矢量 + 性能 + 可控 |
| D13 | AI 生图模型 = `gpt-image-1.5`（gpt-image-2 需组织验证） | ✅ 已锁定 | 实测 1.5 输出已超预期 |
| D14 | 不做匿名 demo / 计算器 / i18n / Before-After / SEO 全套 | ✅ 已锁定 | 第二阶段评估 |
| D15 | 章印 logo 替换 site-header / site-footer 的 "AV" 字母方块 | ✅ 已锁定 | 视觉差异化拉满，成本极低 |

---

## 9. 开放问题 / 待用户提供

| # | 待办 | 类型 | 阻塞优先级 |
|---|---|---|---|
| O1 | **真实产品截图 4-5 张**（任务创建 / 结果列表 / 复核时间轴 / 三引擎音色 / 剪映目录树） | 用户提供 | P0（阻塞 ProductProof / Workflow） |
| O2 | **30 秒 Hero 录屏**（可选，第一阶段不阻塞） | 用户提供 | P2 |
| O3 | **`/pricing` SSR 不渲染价格的根因**：是 use-plans.ts 没 SSR、Gateway `/api/plans` 不通、还是其他配置？ | 排查任务 | P0 |
| O4 | **是否分阶段提交**：建议先 token + 章印 + globals.css → 再 hero → 再 ProductProof + 截图替换 → 再 trust-banner + pricing 修 bug → 再 workflow + footer。每步独立 commit 便于回退 | 实施策略 | P1 |
| O5 | **Hero 大字 pre-headline 文案最终版**（草案：`长视频翻译总在三件事翻车 —— 口型对不上、改一句要重跑全片、剪辑师拿到的是死字幕`），是否完全采用 / 微调 / 改写 | 用户拍板 | P1 |
| O6 | **TrustBanner 6 项的"项目数据保留 7 天"**：和 Gateway 实际策略是否一致？需对齐文案口径，避免和实际行为冲突 | 对齐 | P1 |
| O7 | **章印篆字「译」的字形**：用思源宋体 SC 的「译」字直接拓印感处理，还是请人手写一枚篆刻？前者 1 小时落，后者更地道但要外包 | 工艺决策 | P2 |
| O8 | **是否保留 `/contact /privacy /refund /terms` 当前样式**，还是这些纯文本页也跟进浅色水墨主题？建议**第一阶段一并跟进**（成本低，避免主题割裂） | 范围 | P1 |

---

## 10. 第一阶段交付边界

### 10.1 必交付（P0）

- [x] `globals.css` 加 `[data-theme="ink"]` token 块（§4.1、§4.2）
- [x] `(marketing)/layout.tsx` 顶层注入 `data-theme="ink"`
- [x] `seal-stamp.tsx` 章印 SVG 组件
- [x] `ink-divider.tsx` 飞白分割线 SVG 组件
- [x] `hero.tsx` 用图 2 重构（pre-headline + 大图 + 朱砂 CTA）
- [x] `product-proof.tsx` 替换为真截图（依赖 O1 用户提供）
- [x] `trust-banner.tsx` 新组件（6 项一句话）
- [x] `pricing-preview.tsx` 修 SSR 不渲染价格的 bug
- [x] `trial-banner.tsx` Gateway 驱动（消除硬编码）
- [x] hero 大图资产处理（去原生印章 + webp 多档）
- [x] 字体 webfont 自托管 + next/font 配置

### 10.2 第一阶段力争（P1）

- [ ] `workflow-showcase.tsx` 加产品局部截图
- [ ] `faq.tsx` 顺序重排
- [ ] `final-cta.tsx` 朱砂 + 章印
- [ ] `site-header.tsx` sticky + 滚动 CTA 高亮
- [ ] `(marketing)/contact /privacy /refund /terms` 跟进主题
- [ ] Hero pre-headline 文案最终版（依赖 O5）

### 10.3 第二阶段（不在本 plan 范围）

- 匿名/游客 demo（预置任务 + 单段交互）
- 费用试算器
- Before/After A/B 播放器
- i18n 中英双语
- SEO 全套（OG image / sitemap / JSON-LD）
- 工作台层引入水墨元素（如果用户体验数据支持）

---

## 11. 回归守卫思路

### 11.1 编译 / 类型 / lint

第一阶段不引入新依赖（next/font 已有、SVG 手写），守住：

```bash
cd frontend-next && npm run build     # next build standalone 通过
cd frontend-next && npm run lint      # eslint clean
```

### 11.2 视觉回归（人工抽查清单）

每完成一个组件后人工过一遍：

- [ ] Hero 在 1920 / 1440 / 768 / 375 四档分辨率下文字不被大图遮挡
- [ ] 朱砂红 `#C73E3A` 出现位置 ≤ 3 处（CTA + 章印 + 关键数字），不超量
- [ ] 章印在 dark mode `[data-theme="synthetix"]` 下不出现（验证 token 隔离）
- [ ] PricingPreview 在 Gateway `/api/plans` 不可达时回退到 SSR fallback，不空白
- [ ] TrialBanner 数字（"7 天 / 20 分钟"）来自 Gateway 而非硬编码（grep 源码验证）

### 11.3 性能（PageSpeed / Lighthouse）

第一阶段目标：

| 指标 | 阈值 | 备注 |
|---|---|---|
| LCP | < 2.5s | Hero 大图 webp + 多档 + `<img loading="eager">` |
| CLS | < 0.1 | 字体加载 fallback metrics adjust |
| FCP | < 1.8s | 关键 CSS inline |
| TBT | < 200ms | 不引入 WebGL，水墨纹理纯 CSS |

### 11.4 SEO 不退化

第一阶段不主动做 SEO 提升，但必须**不退化**：
- 每页 `<title>` 和 `<meta description>` 不丢
- `<h1>` 仍存在且语义正确
- 现有 footer 合规链接（隐私 / 条款 / 退款）保留可点

### 11.5 守卫测试（可选，第一阶段后期补）

如果时间允许，可加一个 frontend-side 的简单回归测试：

```ts
// frontend-next/__tests__/marketing-theme.test.tsx
test('marketing layout injects data-theme="ink"', () => { ... })
test('trial banner does not contain hardcoded "7 天"', () => { ... })
test('pricing preview renders price cards in SSR', () => { ... })
```

---

## 附：实施顺序建议（O4 草案）

按依赖图最稳的提交顺序，每步独立 commit 便于回退：

```
1. token + 章印 + 字体        →  无视觉变化，CSS 基础就位
2. globals.css ink theme      →  首次开关浅色（仅在 marketing layout 生效）
3. seal-stamp + ink-divider   →  组件就位，未使用
4. hero.tsx 重构              →  首次视觉变化（验证大图 + 字体 + 朱砂）
5. trust-banner 新增          →  插入 features 和 pricing 之间
6. pricing-preview 修 bug     →  解锁定价转化
7. trial-banner Gateway 化    →  解除硬编码绑定
8. product-proof 真截图       →  最大视觉差异化（依赖用户提供截图）
9. workflow-showcase 加图     →  叙事完整
10. site-header / footer      →  首尾呼应
11. faq / final-cta 微调      →  收尾
```

每步交付后人工抽查 §11.2 清单对应项。

---

**本 plan 第一份完成，待用户审阅后启动 T1（globals.css token + seal-stamp 组件）**。

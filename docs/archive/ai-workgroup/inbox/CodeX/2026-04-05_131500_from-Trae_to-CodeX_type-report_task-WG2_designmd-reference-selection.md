# Report: AIVideoTrans 营销层参考站选择与 DESIGN.md 草稿

## 1. 对 awesome-design-md 的理解

- **这个仓库是做什么的**：这是一个收集了众多优秀开发者/SaaS 网站 `DESIGN.md` 文件的开源集合。`DESIGN.md` 是一种纯文本的设计系统文档，专门用于让 AI Agent（如 Google Stitch 或 Trae）读取，从而生成视觉风格高度一致的 UI。
- **为什么它可能对当前项目有帮助**：AIVideoTrans 正在推进前端表达层和营销页的建设。通过引入业界顶尖产品的 `DESIGN.md`，我们可以快速为 AI 设定准确的视觉基调和设计规范，避免 AI 在生成页面时自由发挥导致风格割裂或缺乏质感。
- **它更适合营销层还是工作台层**：**更适合营销层（Marketing Layer）**。该仓库提取的规范大多来自各产品的官网和营销落地页，侧重于品牌调性、视觉冲击力、大排版和氛围感；而工作台层（App Layer）通常需要更高的数据密度、复杂的交互状态和长时间使用的视觉舒适度。

## 2. 选出的 3 个参考站

### ① ElevenLabs
- **适合的原因**：AIVideoTrans 的核心业务是视频翻译与配音。ElevenLabs 作为顶尖的 AI 语音平台，其“暗黑电影感、音频波形美学”与本项目的多媒体/视听属性完美契合。
- **最值得借鉴的 3-5 个点**：
  - 暗色主题下的高对比度文本排版，极具沉浸感。
  - 媒体优先（Media-rich）的 Hero 区域展示，非常适合放置视频翻译前后的对比 Demo。
  - 细微的发光效果（Glows）和渐变，精准传达 AI 的“魔法感”与科技感。
  - 播放器、波形等音视频相关 UI 元素的视觉隐喻。
- **不该照搬的地方**：营销页过于沉浸的暗黑电影感和强烈的发光特效不应带入 `(app)` 工作台层，否则会干扰用户进行精细的字幕编辑和任务管理。

### ② Vercel
- **适合的原因**：Vercel 代表了现代 SaaS 营销页的标杆——极简、精准、极客感。AIVideoTrans 作为一个多用户 SaaS 工作台，需要向用户传达专业、高效、可靠的品牌形象。
- **最值得借鉴的 3-5 个点**：
  - 极致干净的黑白灰对比与精准的细边框（Borders）运用。
  - 清晰、极简的 Pricing 页面卡片设计与套餐对比表。
  - 极简且低摩擦的 Auth / Trial 注册转化流程。
  - 优秀的无衬线字体排版（Typography hierarchy），信息层级分明。
- **不该照搬的地方**：Vercel 的极简冷淡风在展示丰富的视频/音频多媒体内容时可能显得过于干瘪，需要结合 ElevenLabs 的多媒体表现力来中和。

### ③ Linear
- **适合的原因**：Linear 定义了新一代生产力工具的审美（Linear-style），强调速度、精确和微妙的视觉愉悦感，非常适合 AIVideoTrans 这种强调工作流效率的 SaaS 产品。
- **最值得借鉴的 3-5 个点**：
  - 带有微妙渐变和光影的 Feature Grid（特性网格/Bento Box）卡片。
  - 极具质感的按钮（CTA）和干脆利落的交互反馈。
  - 针对高阶用户的“键盘优先/快捷键”视觉暗示。
  - 优雅的深色模式色彩系统（深灰背景 + 高亮强调色）。
- **不该照搬的地方**：营销页中大面积的背景光晕和超大号标题排版，不适合直接放入 `(app)` 层的 Job 列表或字幕编辑区。

## 3. 三者综合后的设计方向

- **AIVideoTrans 的营销层设计气质应该是什么**：
  - **"Cinematic Precision"（电影级的精准）**。既要有 AI 视听产品（ElevenLabs）的沉浸感与魔法感，让用户一眼被视频翻译的效果震撼；又要有现代 SaaS（Vercel/Linear）的专业、克制与高效，让用户信任其作为生产力工具的可靠性。
- **更像哪类产品，而不像哪类产品**：
  - 更像面向专业创作者和团队的现代生产力工具（如 RunwayML, Linear, Vercel）。
  - 不像传统的、臃肿的企业级软件，也不像过于活泼、玩具化的 2C 娱乐应用。
- **哪些视觉原则最重要**：
  - **Dark-first 优先**：营销页以深色模式为主，深邃的背景能最大程度凸显视频和音频 Demo 的色彩与内容。
  - **Content over Chrome**：UI 框架退居幕后，让“翻译前后的视频对比”成为绝对的视觉焦点。
  - **Subtle AI Accents**：通过微妙的渐变、边框流光或特定强调色来暗示 AI 能力，拒绝大面积滥用高饱和度渐变。
  - **Crisp Typography**：使用现代无衬线字体，保持极高的可读性和信息层级。

## 4. `AIVideoTrans DESIGN.md` 草稿

```markdown
# DESIGN.md - AIVideoTrans (Marketing Layer)

## 1. Brand / Tone
- **Keywords**: Cinematic, Precise, Professional, AI-Powered, Effortless.
- **Vibe**: "Cinematic Precision". A professional SaaS for video translation and dubbing. It feels like a high-end studio tool built with modern web technologies.
- **Theme**: Dark-mode first for the marketing layer to make video/audio content pop, with subtle AI-inspired glows.

## 2. Color Direction
- **Backgrounds**: Deep void blacks and very dark grays (e.g., `#000000`, `#0A0A0A`, `#111111`).
- **Surfaces/Cards**: Slightly elevated grays with subtle borders (e.g., `#1A1A1A` with `#2A2A2A` borders).
- **Primary Accent**: A vibrant, tech-forward color (e.g., Electric Blue `#0070F3` or Amethyst Purple `#8A2BE2`) used sparingly for primary CTAs and AI magic moments.
- **Text**: High-contrast white (`#FFFFFF`) for headings, muted grays (`#888888` or `#A1A1AA`) for secondary text.

## 3. Typography Direction
- **Font Family**: Modern sans-serif (e.g., Inter, Geist, or SF Pro).
- **Headings**: Tight tracking (letter-spacing), bold or semi-bold weights. Large and impactful on the Hero section.
- **Body**: Highly legible, regular weight, generous line-height (1.5 - 1.6) for readability.
- **Monospace**: Used for technical details, job IDs, or API references (e.g., JetBrains Mono, Geist Mono).

## 4. Layout Rules
- **Hero Section**: Centered, bold headline, clear subheadline, primary CTA, immediately followed by a high-quality video/audio translation interactive demo.
- **Feature Grids**: Bento-box style or Linear-style cards with subtle gradients on hover.
- **Spacing**: Generous whitespace (padding/margin) between sections to let the content breathe. Use an 8px baseline grid.
- **Max Width**: Constrain content to a readable max-width (e.g., 1200px) on large screens.

## 5. CTA Style
- **Primary CTA**: Solid accent color background, white text, subtle glow or shadow. Slightly rounded corners (e.g., `rounded-md` or `rounded-lg`).
- **Secondary CTA**: Transparent background, subtle border, text color matching the border. Changes background on hover.
- **Interaction**: Crisp, fast transitions (e.g., 150ms ease-in-out). No bouncy or overly playful animations.

## 6. Pricing Page Guidance
- **Structure**: Clear, side-by-side tier cards (e.g., Free/Trial, Pro, Enterprise).
- **Highlight**: Visually emphasize the "Pro" or most popular tier with a subtle border glow or badge.
- **Data Display**: Use clean checkmarks for features. Keep the feature comparison table minimalist with alternating row colors or simple bottom borders.
- **Numbers**: Any specific prices, minutes, or quotas are `待 Task 0 真相源统一后锁定`.

## 7. Trial Page Guidance
- **Frictionless**: Minimal form fields. Focus on "Start translating now".
- **Social Proof**: Include small trust badges or a testimonial near the signup form.
- **Visuals**: Keep the layout split—left side for the auth/trial form, right side for a beautiful product shot or abstract AI visualization.
- **Limits**: Trial duration or quota is `待项目开发者确认`.

## 8. Do / Don't
- **DO**: Use dark mode to make video content stand out.
- **DO**: Use subtle, high-quality animations (e.g., fade-ins, subtle scales).
- **DO**: Keep borders and dividers thin (1px) and low-contrast.
- **DON'T**: Use overly bright, cartoonish illustrations.
- **DON'T**: Clutter the hero section with too much text; let the video demo speak.
- **DON'T**: Use heavy drop shadows on dark mode; use borders and subtle glows to create depth.
```

## 5. 适用边界说明

- **仅限营销层**：这份 `DESIGN.md` **只适用于 `(marketing)` 层**（包括首页、定价页、Trial 页）。
- **隔离工作台层**：**不应直接套到 `(app)` 工作台层**。工作台层（如 workspace, review flow, job detail）需要处理高密度的任务列表、复杂的视频时间轴和字幕编辑，过度的暗黑电影感和发光特效会导致视觉疲劳。工作台层应采用更中性、对比度适中、支持浅色模式的实用主义设计。
- **Bridge 跨层复用建议**：
  - **可以作为 bridge 的部分**：色彩系统（基础灰度与主色调）、字体排版规范（Typography）、基础组件库（Button, Input 的交互反馈与圆角规范）可以跨层复用，以保持品牌一致性。
  - **不行作为 bridge 的部分**：大面积的渐变背景、超大号的 Hero 标题排版、沉浸式的纯黑背景（工作台可能需要更亮的面板来区分层级）、以及营销导向的重度动画。
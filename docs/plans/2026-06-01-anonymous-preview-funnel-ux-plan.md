# 视频翻译匿名预览漏斗 UX 改造方案

**状态：** PROPOSED / 待产品与工程评审  
**日期：** 2026-06-01  
**范围：** 网站首屏样本播放器、匿名试用漏斗、预览任务、方案选择、Smart 音色克隆赠点预扣、登录认领、成本与滥用控制。

本文档提出视频翻译产品的用户体验与后端流程改造方案。本文不是实施授权；套餐、价格、试用、点数和权益仍以 Gateway 为唯一真源。

---

## 1. 背景问题

当前任务创建体验偏工程视角：用户在看到自己视频效果之前，就要理解并选择 `free / express / studio / smart` 等服务模式。对用户来说，这不是自然决策。用户真正关心的是：

- 我的视频翻译出来是否自然；
- 配音是否接近原声；
- 要花多少钱；
- 多久能出结果；
- 能不能修改；
- 能不能下载视频、字幕和剪映草稿。

目标漏斗应该先证明价值，再让用户注册、付费和选择完整交付方案。

---

## 2. 改造目标

1. **降低首步摩擦。** 访客无需注册即可上传本地视频。YouTube URL 不对匿名/Free 用户开放。
2. **尽快制造“我的视频也能译制”的证明时刻。** 匿名用户能看到短预览，但不能无成本消耗完整生产能力。
3. **把匿名成本钉死。** 匿名预览必须受 3 分钟时长上限、队列优先级、水印、不可下载、限频和克隆限制约束。
4. **把方案选择改成用户语言。** 用户选择的是“免费体验 / 低成本快速出片 / 高质量原声译制”，不是内部 pipeline。
5. **在自然承诺点要求登录。** 更长预览、真实音色克隆、完整生成、保存任务、下载视频/字幕、编辑和导出剪映草稿都需要登录和点数/支付门槛。
6. **保持项目核心架构不变。**
   - TTS unit 仍是 `SemanticBlock`。
   - 对齐仍是 DSP-first。
   - 字幕 retiming 仍是确定性逻辑。
   - 主要交付目标仍包含剪映草稿和可编辑产物，而不是只交付 rendered MP4。

---

## 3. 非目标

- 不重写 `src/pipeline/process.py` 的生产主流程。
- 不把套餐、价格、权益真源迁移到前端。
- 不允许匿名用户下载生产产物。
- 不允许匿名、兜底或便利路径静默调用付费或持久音色克隆 API。Express 匿名 CosyVoice 临时克隆只能在明确授权、服务端 gate 和限频通过后运行。
- 不让 Smart 形成 Gateway 信用点数策略之外的第二套计费事实。
- 不在默认本地测试路径引入真实外部服务依赖。

---

## 4. 当前代码事实与成本约束

需要纳入方案的当前事实：

- Smart 默认费率是 `100` 点/分钟，见 `gateway/pricing_schema.py` 与 `gateway/credits_service.py`。
- 音色克隆当前默认用户侧成本是 `500` 点/个，见 `gateway/pricing_schema.py` 与 `gateway/voice_selection_api.py`；本方案建议把 MiniMax/正式克隆扣点提高到 `600` 点/个。
- 成本模型里 MiniMax 音色克隆是固定 RMB 成本项，见 `gateway/cost_management.py`。
- MiniMax 官方文档当前说明（2026-06-02 已逐字核对三处官方页面）：复刻音色若 7 天内未正式调用，系统会删除（voice_clone 页原文：“复刻得到的音色若 7 天内未正式调用，则系统会删除该音色”）；快速复刻音色为**未激活状态，需正式调用一次 TTS 才可在声音管理接口查询到**（get_voice 页原文：“快速复刻得到的音色为未激活状态，需正式调用一次才可在本接口查询到”）。本方案“克隆后立即用预览 TTS 激活”即据此设计；因激活在克隆后数秒内发生，设计对 7 天窗口不敏感。见 [voice_clone](https://platform.minimaxi.com/docs/api-reference/voice-cloning-clone) 与 [get_voice](https://platform.minimaxi.com/docs/api-reference/voice-management-get)。
- MiniMax 官方提供删除音色接口，删除后该 `voice_id` 无法再次使用（delete_voice 页原文：“删除后，该 voice_id 将无法再次使用”）；删除范围为 Voice Cloning / Voice Generation API 生成的 voice_id，官方**未**声明“必须先激活才能删除”，因此激活失败后清理未用 `voice_id` 的路径可行；实现仍应对删除失败兜底（退回依赖 MiniMax 7 天自动 GC）。见 [delete_voice](https://platform.minimaxi.com/docs/api-reference/voice-management-delete)。
- 当前公开文档未看到已激活 API 克隆音色的保存数量和保存时长硬上限；但产品侧仍应设置内部个人音色库限额，避免体验、管理和供应商策略变化风险。
- CosyVoice 音色登记当前成本模型可按 0 RMB 克隆费处理，但匿名 Express 预览仍会消耗 worker/GPU、上传、存储、清理、队列和 TTS 成本，因此不能视为无限免费。
- Smart 克隆成功会通过 `UsageMeter.record_voice_clone(...)` 记录 usage/cost 事件。
- Smart `capture_full` 结算目前按服务模式分钟数捕获任务点数，不会自动额外加收克隆点数。
- Free service mode 已经有 consent、每日额度、时长上限、水印和下载限制等 hard gates。
- Express auto-clone 已经具备正确的安全形态：server-confirmed consent、admin gate、allowlist/cap、reservation、runtime gate、失败回预设音色。

结论：匿名阶段不能给“前 10 分钟完整预览”。大多数短视频本身小于 10 分钟，这会让匿名预览变成完整生产，成本发生在转化之前。当前拍板：Free 和 Express 匿名预览上限为 3 分钟；Smart 不开放匿名预览，必须注册/登录并具备 trial/付费 Smart 权限；YouTube URL 不对匿名/Free 用户开放。

---

## 4.1 Entitlement 决策

漏斗档位必须与 Gateway entitlement 对齐，不允许前端硬编码绕过 `get_effective_allowed_service_modes(...)`。

| 用户状态 | 可见/可用档位 | Smart 权限 | 备注 |
| --- | --- | --- | --- |
| 匿名 | 免费版、快捷版 | 不可用 | 仅本地上传；免费/快捷各最多预览 1 次，3 分钟水印预览；不可下载 |
| Free 且无 trial | 免费版、快捷版 | 不可用 | 仅本地上传；Free 用户即使有 500 点 free bucket，也不能进入 Smart |
| Trial 用户 | 免费版、快捷版、智能版；Studio 取决于开关 | 可用 | 需要把 trial overlay 增加 Smart 权限；仍受 Smart 双 kill switch 控制；YouTube 可按开关开放 |
| Plus/Pro 用户 | 免费版、快捷版、智能版；Studio 取决于开关/计划 | 可用 | 以 Gateway plan catalog/runtime pricing 为准；YouTube 可开放 |

Smart 权限仍必须同时满足：

- service mode 在用户 effective allowed modes 中；
- `AVT_ENABLE_SMART_MODE` 开启；
- admin `smart_mode_enabled` 开启；
- preview/full job 的点数、consent、clone gate 通过。

Studio 不进入匿名漏斗，不提供免费预览。Studio 作为登录后的正式付费工作台能力处理，并增加后台开关：

- `studio_mode_enabled`
- `studio_visible_for_trial`
- `studio_user_allowlist`
- `studio_plan_allowlist`

默认建议：Studio 只对正式付费用户开放；trial 是否可见由后台开关决定。

YouTube URL 权限建议：

| 用户状态 | YouTube URL |
| --- | --- |
| 匿名 | 不开放 |
| Free 且无 trial | 不开放 |
| Trial | 默认关闭，可通过 `youtube_url_enabled_for_trial` 灰度开放 |
| Plus/Pro | 可开放，仍需授权确认、时长限制和风控 |
| allowlist 用户 | 可由 `youtube_url_user_allowlist` 单独开放 |

---

## 5. 推荐总漏斗

推荐漏斗：

`首屏样本 A/B 播放 -> 立即试用 -> 免登录本地上传 -> 合规+时长+人声分析 -> 推荐方案 -> 免费/快捷免登录 3 分钟水印预览 -> 登录后认领任务 -> trial/付费 Smart 3 分钟增强预览 -> 完整生成/保存/下载/编辑/剪映草稿`

核心取舍：

- 匿名阶段只生成 3 分钟内的短预览，不生成 10 分钟全量预览。
- Smart 不开放匿名预览；必须注册/登录且具备 trial/Plus/Pro Smart 权限。
- Express 匿名预览可走 CosyVoice 临时克隆，但必须有授权、服务端 gate、限频和失败回预设音色。
- 登录后 Smart 可用 trial/赠点预扣 MiniMax 音色克隆点数，优先克隆主说话人；克隆成功后作为正式个人音色进入用户音色库，后续任务复用。
- 所有未付费预览都加水印且不可下载。
- 完整交付和可下载产物全部进入登录/点数/支付门槛。

---

## 6. 首屏样本视频播放器

首屏应先展示产品证明，而不是静态营销口号。

桌面端：

- 鼠标 hover 后静音自动播放译制版样本；
- 半透明按钮可切换“译制版 / 英文原片”；
- 原片与译制版切换时保持播放进度同步，形成强 A/B 对比；
- 默认显示“下一段样本”按钮；
- 播放到第二个样本后增加“上一段样本”按钮；
- 控件不遮挡核心人物口型、字幕和视频主体。

移动端：

- 使用 `muted + playsinline` 自动播放；
- 页面滑出首屏或播放器离开 viewport 后暂停播放；
- 使用 `IntersectionObserver` 控制播放/暂停；
- 只预载当前 poster 和必要资源，避免首屏带宽过高。

首屏醒目位置放 `立即试用` CTA。点击后不进入注册页，直接打开试用输入框。

---

## 7. 匿名试用输入

点击 `立即试用` 后出现任务输入框：

- 拖拽上传区域；
- 文件选择按钮；
- 上传进度实时展示；
- 匿名和 Free 不显示 YouTube URL 输入框；
- Trial/Plus/Pro 用户在权限开关允许时显示 YouTube URL 输入框；
- YouTube 下载/探测进度只在登录且有 URL 权限的路径展示；
- 当前处理阶段明确展示：
  - 上传中；
  - 读取视频信息；
  - 提取预览片段；
  - 合规检测；
  - 准备预览；
  - 生成预览。

匿名用户获得一个匿名 session token。登录后通过 claim token 把匿名预览任务绑定到账号。

---

## 8. 合规与媒体分析

在任何昂贵处理前先做：

- 文件类型、大小、时长检查；
- YouTube URL 权限检查；
- YouTube URL 可用性检查（仅登录且有 URL 权限的用户）；
- 内容使用权/声音授权声明；
- 合规检测（复用现有 `src/services/content_compliance.py`，口径见 §8.1）；
- 视频时长估算；
- 人声密度估算；
- 说话人数估算；
- 主说话人候选识别；
- 音频质量评估；
- 是否适合 Smart 原声克隆路径判断。

如果合规失败，必须在翻译/TTS/clone 等昂贵步骤前停止。

### 8.1 合规检测口径（复用现有模块）

复用 `src/services/content_compliance.py`（policy `mainland_china_content_compliance`），两层结构：

- **Layer 1 本地规则**：对标题/简介/可得文本做关键词规则匹配，可在 ASR 前先拦明显违规；
- **Layer 2 LLM 审核**：依据《互联网信息服务管理办法》等法规审标题+简介+**英文转录稿**，输出 `pass | block | needs_manual_review`。

落地决策：

- **顺序**：Layer 2 需要转录稿，故现实链路是「本地规则预筛 → ASR（仅 teaser 片段）→ LLM 合规 → 通过才进翻译/TTS/clone」。匿名只有本地上传、通常无标题/简介，因此匿名合规实质是「转录稿审核」，ASR 成本在 block 之前必然发生（teaser 仅 3 分钟，可接受）。
- **fail-closed**：合规模块异常/超时一律按**不通过**处理，不得 fallback 成 pass。
- **匿名无人工复审**：anonymous/未登录路径下 `needs_manual_review` 视为**软拦截**（提示「该内容需登录后人工复核」），不得自动放行进 TTS/clone。
- 合规结果写入 preview record 审计字段，与媒体分析、consent 一起保留。

---

## 9. 上传后方案选择

合规和媒体分析完成后，弹出简洁的方案选择框。不要做密集参数表，而是默认推荐一个方案，并允许用户切换。

推荐规则示例：

- 只想试试看：推荐免费体验版；
- 预算敏感、想快速出片：推荐快捷版；
- 人声清晰、口播/课程/访谈类、用户可能重视原声：推荐智能版；
- 音频质量差或不适合克隆：提示预览将使用预设音色。

方案卡片面向用户，不面向 pipeline：

| 用户名称 | 内部模式 | 用户理解 |
| --- | --- | --- |
| 免费体验版 | `free` | 免登录预览 3 分钟，登录后可下载水印体验产物 |
| 快捷版 | `express` | 免登录预览 3 分钟，可用 CosyVoice 临时克隆，10 点/分钟 |
| 智能版 | `smart` | 需登录/trial 才可预览 3 分钟，顶配翻译与配音质量 |
| 工作台版 | `studio` | 不进入匿名漏斗；后台开关控制是否对 trial/付费用户开放 |

每张卡片必须展示：

- 价格；
- 预览长度；
- 是否水印；
- 是否真实克隆音色；
- 是否包含编辑；
- 是否包含剪映草稿；
- 哪些动作需要登录。

---

## 10. 匿名预览

匿名预览不是前 10 分钟，而是 3 分钟内的短预览。Smart 不开放匿名预览。

规则：

- Free 匿名预览：3 分钟硬上限；
- Express 匿名预览：3 分钟硬上限；
- Smart 匿名预览：不开放，提示注册/登录后可进入智能版预览；
- 选择代表性强、人声密集的片段；
- 使用所选档位可安全运行的真实能力；
- 所有匿名预览都加水印；
- 可降分辨率或码率；
- 不提供下载；
- 不提供字幕导出；
- 不提供剪映草稿导出；
- 进入低优先级队列；
- 有 TTL 自动清理。

理由：3 分钟足够用户判断翻译、配音、字幕时序和整体质量，同时避免匿名阶段变成完整生产。Express 的匿名临时克隆只允许走 CosyVoice 路径，且必须受 admin gate、consent、daily/active cap、临时音色清理和失败回预设保护。

---

## 11. 登录与任务认领

以下动作需要注册/登录：

- 生成更长预览；
- 生成完整视频；
- 保存任务；
- 下载视频；
- 下载字幕；
- 下载 materials pack；
- 导出剪映草稿；
- 进入编辑；
- 运行真实音色克隆；
- 去除匿名预览限制。

登录后，匿名任务通过一次性 claim token 绑定到账户。绑定后保留：

- 原始 source metadata；
- 所选方案；
- 推荐方案；
- 预览产物；
- 合规结果；
- 媒体分析结果；
- 价格估算；
- 授权/consent 审计信息。

---

## 12. 三档产品规则

### 12.1 免费体验版

建议文案：

> 免费体验版：可体验翻译配音效果，适合先试试看。带水印，不支持编辑和剪映草稿，Beta 阶段效果会随音频质量变化。

规则：

- 匿名：生成最多 3 分钟水印预览；
- 登录后：允许下载水印视频和字幕；
- 视频超过 10 分钟时，免费体验仍只处理体验窗口，不生成完整视频；
- 必须带水印；
- 不支持编辑；
- 不支持剪映草稿导出；
- 不提供 clean download；
- 受账号、设备、IP、自然日额度限制；
- MiMo/free voiceclone 路径仍需要声音授权声明和 kill switch 保护。

文案注意：避免直接写“效果不稳有起伏”这种自我劝退表达。推荐写“Beta 体验版，效果会随音频质量变化”。

### 12.2 快捷版

建议文案：

> 快捷版：10 点/分钟，低成本自动出片。适合想快速看到完整译制效果的内容。

规则：

- 匿名：生成最多 3 分钟水印预览；
- 匿名 Express 可以启用 CosyVoice 临时音色克隆，因为 CosyVoice voice enrollment 当前不产生直接 RMB 克隆费；
- 匿名 Express 克隆必须满足：
  - 用户明确勾选声音授权/克隆 consent；
  - server-confirmed consent；
  - admin 主开关开启；
  - allowlist/cap/rate limit 通过；
  - 临时音色不进入永久个人音色库；
  - 临时音色有 TTL 和 cleanup；
  - 失败时回预设音色并继续生成预览；
- 登录完整任务：10 点/分钟；
- 自动临时音色克隆必须有明确 consent 和服务端 gate；
- 克隆不可用、失败或被 gate 拒绝时，自动回预设音色继续；
- 登录后可预览最多 10 分钟；
- 下载相关资源、完整生成、字幕、编辑加购、剪映草稿加购都需要登录和点数/支付；
- 编辑可作为付费加购；
- 剪映草稿可作为付费加购。

### 12.3 智能版

建议文案：

> 智能版：100 点/分钟，顶配翻译与配音质量，稳定、流畅、自然。注册登录并具备 trial/付费权限后，可使用点数预扣音色克隆，让主说话人更接近原声。

规则：

- 匿名：不开放智能版预览；
- 登录/trial 预览：
  - trial 用户需要增加 `smart` 权限；
  - Free 且没有 trial 资格的用户没有 Smart 权限；
  - 预览时长最多 3 分钟；
  - 3 分钟水印预览本身不扣 Smart 分钟点数；
  - 如用户选择“克隆主说话人音色”，预扣 `600` 点；
  - Smart 预览产生的 MiniMax 音色不是临时音色；克隆成功后保存到用户个人音色库，标记来源 `smart_preview`，并关联 `preview_job_id`；
  - 预览生成阶段必须用该 `voice_id` 正式跑一次 TTS，以激活 MiniMax 可查询、可复用音色，避免 7 天未正式调用后被供应商删除；
  - Smart 预览克隆不复用 Express/CosyVoice 的临时音色 TTL 清理机制；
  - 用户个人音色库达到套餐限额时，不再创建新克隆，提示删除旧音色、升级或改用已有音色/预设音色；
  - 如果点数只够 1 个音色克隆，只克隆主说话人；
  - 如果点数不足 600，继续使用高质量预设音色或已有个人音色复用；
- 登录增强预览：
  - 如果可用点数足够预扣 1 个音色克隆，就克隆主说话人；
  - 如果点数足够多个克隆，按说话人优先级克隆；
  - 如果点数不足，继续使用高质量预设音色或已有个人音色复用；
  - 已有个人音色强匹配复用不收克隆点数；
- 完整任务：
  - 100 点/分钟；
  - 免费包含视频修改功能；
  - 免费导出剪映草稿；
  - 新音色克隆费用必须明确展示，正式 MiniMax 克隆建议 `600` 点/个；
  - 如果 Smart 预览阶段已经克隆并保存了可用个人音色，完整任务必须复用同一个 `voice_id`，不重复克隆、不重复收取克隆点数；
  - 小于 10 分钟的视频如需要新克隆，应额外预扣克隆点数，除非产品明确接受毛利损失；
  - 达到产品批准阈值的视频，不直接“白送克隆”，而是走平滑抵扣公式，避免价格倒挂。

个人音色库默认限额建议：

| 用户状态/套餐 | 个人音色库上限 |
| --- | ---: |
| Trial | 10 |
| Plus | 30 |
| Pro | 100 |

限额只代表最多保存多少个个人音色，不代表免费克隆次数。每次新 MiniMax 克隆仍需显式 consent，并按当前策略预扣/扣除 `600` 点；trial 赠点通常只够先克隆 1 个主说话人。

**与现有配额机制对账（不要另起炉灶，2026-06-02 已核对代码）：** Gateway 已有 per-user 音色库配额门，但**当前只挂在 CosyVoice 克隆路径**——`gateway/cosyvoice_clone/api.py` 用 `admin_settings.cosyvoice_clone_max_voices_per_user`，满则返回 409，且在**读样本/付费之前**拦截；计数走 `user_voice_service.count_active_voices_for_user_and_provider`，**按 provider 分别统计**。落地时必须定清三件事：

- 上表 10/30/100 **已定为跨 provider 合计**（见 §18 Phase 0）；底层按 provider 计数，故配额门须对各 provider 求和后比较，Express 临时音色 `include_temporary=False` 不计入。
- 现有配额是**单一 admin int**，本方案要**按套餐分级**（Trial/Plus/Pro），须把配额改成 plan-aware（或加分级映射），不要落成一个平值。
- MiniMax 克隆走 `gateway/voice_selection_api.py`，该路径**当前没有任何库容配额门**（仅有 `clone_in_progress` 锁），所以“Smart 检查个人音色库容量”对 MiniMax 是**净新增**——应照搬 CosyVoice 的“满则 409、读样本/付费之前”模式，而非另写一套。

推荐待评审计费策略：

| 场景 | 策略 |
| --- | --- |
| 匿名 Smart 预览 | 不开放 |
| 登录 Smart 3 分钟预览，点数 >= 600 | 预扣 600 点，克隆主说话人，保存到个人音色库并用于本次预览激活 |
| 登录 Smart 3 分钟预览，点数 < 600 | 不克隆，使用预设或已有音色 |
| Smart 预览已成功克隆并转完整任务 | 复用预览阶段的同一 `voice_id`，不重复扣 600 点 |
| Smart 完整任务 <10 分钟且需要新克隆 | 预扣任务点数 + 克隆点数 |
| Smart 完整任务 >=10 分钟且只需 1 个主说话人克隆 | 走平滑克隆抵扣，不做硬阈值白送 |
| 复用已有个人音色 | 不收克隆点数 |
| 克隆失败、未激活或未生成可用预览 | 退回预扣克隆点数，并清理可能已创建但不可用的 `voice_id` |

关键要求：运行时 `voice_clone_cost_credits` 需要从当前默认 `500` 调整到 `600`，并同步 Gateway pricing、voice selection pricing、前端展示和测试。Trial 用户通常会有 free bucket + trial bucket，可覆盖一次 600 点克隆；Free 且无 trial 的用户只有 free bucket 时不能进入 Smart，形成 entitlement 和点数的双重限制。个人音色库限额应由 Gateway/后台配置下发，前端只展示后端返回的剩余容量。

### 12.4 Smart 克隆平滑抵扣公式

不要采用“10 分钟以上包含克隆、10 分钟以下另收克隆”的硬切规则，否则 9 分钟可能比 10 分钟更贵。建议使用单调公式：

`总点数 = Smart分钟点数 + 克隆点数 - 长视频克隆抵扣`

建议初始参数：

- Smart 分钟点数：`100 × 源视频分钟数`；
- MiniMax 新克隆：`600 × 新克隆数量`；
- 从第 10 分钟开始产生克隆抵扣；
- 每超出 1 分钟抵扣 50 点；
- 每个克隆最多抵扣 600 点；
- 抵扣不能超过实际克隆点数；
- 复用已有个人音色不产生克隆费用，也不产生抵扣。

示例，1 个新克隆：

| 视频时长 | 分钟点数 | 克隆点数 | 抵扣 | 总点数 |
| ---: | ---: | ---: | ---: | ---: |
| 3 分钟 | 300 | 600 | 0 | 900 |
| 9 分钟 | 900 | 600 | 0 | 1500 |
| 10 分钟 | 1000 | 600 | 0 | 1600 |
| 12 分钟 | 1200 | 600 | 100 | 1700 |
| 22 分钟 | 2200 | 600 | 600 | 2200 |

---

## 13. Smart 主说话人克隆优先级

当登录/trial 后点数只够一个音色克隆：

1. 按以下因素确定主说话人：
   - 说话时长占比；
   - 台词数量；
   - 音频质量；
   - 样本是否适合克隆；
   - 合规与授权是否允许。
2. 克隆 API 调用前预扣 1 个克隆成本，建议为 `600` 点。
3. 只克隆主说话人。
4. 克隆成功后保存到用户个人音色库，后续完整任务复用同一个 `voice_id`。
5. 如果个人音色库已满，必须先删除旧音色、升级套餐或改用已有音色/预设音色，不能绕过限额继续克隆。
6. 其他说话人使用：
   - 已有个人音色强匹配；
   - Smart 选择的高质量预设音色；
   - keep-original / mute / background 策略。
7. UI 提示：

> 当前试用点数可覆盖 1 个音色克隆，系统将优先克隆主说话人。克隆成功后音色会保存到你的个人音色库，后续任务可继续使用。其他说话人会使用智能匹配音色。完整克隆更多说话人需补足点数后继续。

如果没有适合克隆的样本：

> 当前视频的人声样本暂不适合安全克隆，系统将使用高质量预设音色生成预览。

---

## 14. 成本与滥用控制

### 14.0 预览次数策略

初始灰度建议：

| 用户状态 | 免费版预览 | 快捷版预览 | 智能版预览 | Studio |
| --- | --- | --- | --- | --- |
| 匿名 | 仅 1 次，3 分钟，本地上传 | 仅 1 次，3 分钟，本地上传，可 CosyVoice 临时克隆 | 不开放 | 不开放 |
| Free 登录用户 | 每天 1 次免费预览，本地上传 | 可按后台开关给 1 次登录预览 | 不开放 | 不开放 |
| Trial 用户 | 每天 1 次免费预览 | 可给 1 次快捷预览 | 可给 1 次 3 分钟 Smart 预览；克隆需 600 点预扣 | 默认不开放，可开关/allowlist |
| Plus/Pro 用户 | 按权益/后台策略 | 按权益/后台策略 | 按权益/后台策略 | 默认开放或按后台策略 |

预览次数必须同时受这些 key 约束：

- anonymous session；
- signed cookie；
- IP；
- IP `/24` 网段；
- browser fingerprint；
- User-Agent；
- source hash / upload file hash；
- YouTube canonical id（仅登录且有 URL 权限）；
- Asia/Shanghai 自然日；
- 全局匿名预览并发和每日总量。

匿名能力必须有后台一键收紧开关：

- `anonymous_free_preview_enabled`
- `anonymous_express_preview_enabled`
- `anonymous_express_cosyvoice_clone_enabled`
- `anonymous_preview_max_seconds = 180`
- `anonymous_preview_daily_global_cap`
- `anonymous_preview_per_ip_cap`
- `anonymous_preview_per_device_cap`
- `anonymous_clone_daily_global_cap`
- `anonymous_clone_active_cap`

### 14.1 匿名成本上限

匿名预览必须受以下限制：

- Free/Express 匿名预览 3 分钟硬上限；
- Smart 不开放匿名预览；
- Express 匿名真实克隆仅允许 CosyVoice 临时克隆；
- Express 匿名 CosyVoice 临时克隆必须通过 consent、admin gate、allowlist/cap、rate limit 和 cleanup gate；
- 不渲染全片；
- 不开放下载；
- 所有预览加水印；
- 可降分辨率；
- 低优先级队列；
- 预览源文件和产物短 TTL 清理；
- IP / device / session / source hash 每日限额；
- 文件大小限制；
- 匿名和 Free 不开放 YouTube URL；
- Trial/Plus/Pro 的 YouTube URL 先探测 metadata，再决定是否提取短片段；
- 可疑行为触发验证码、短信或登录要求。

### 14.2 队列优先级

推荐队列优先级：

1. 已付费完整任务；
2. 登录增强预览；
3. 登录免费体验；
4. 匿名 Free/Express 预览。

匿名流量不能饿死付费任务和管理员任务。

### 14.3 水印策略

所有未付费预览都必须加水印：

- 匿名免费预览；
- 匿名快捷预览；
- 登录但未付费的预览；
- 完整付费捕获前生成的所有 preview。

理由：避免用户录屏未付费预览替代正式购买。

### 14.4 授权声明

以下场景需要明确授权声明：

- YouTube URL 导入；
- 任何复刻源说话人声音的路径；
- 任何创建临时或持久克隆音色的路径。

建议文案草案：

> 我确认已获得该视频内容及其中说话人声音的合法授权，或该使用属于法律允许范围；我理解未经授权复制他人声音可能产生法律风险。

最终文案需要产品/法务确认。

---

## 15. 状态模型

### 15.1 匿名预览任务状态

| 状态 | 含义 |
| --- | --- |
| `created` | 匿名 session 和预览任务创建 |
| `source_uploading` | 本地文件上传中 |
| `source_downloading` | 登录且有 URL 权限的 YouTube/source 下载或短片段提取中 |
| `source_ready` | source asset 可用 |
| `probing` | metadata、时长、人声分析中 |
| `compliance_checking` | 合规检测中 |
| `ready_for_mode` | 可以展示推荐方案和模式选择 |
| `preview_queued` | 预览任务已排队 |
| `preview_running` | 预览 pipeline 运行中 |
| `preview_ready` | 水印预览可播放 |
| `auth_required` | 用户请求了登录后动作 |
| `claimed` | 登录账号已认领 |
| `expired` | TTL 清理完成 |
| `rejected` | 合规/时长/source 策略拒绝 |
| `failed` | 技术失败 |

### 15.2 Smart 克隆预览状态

| 状态 | 含义 |
| --- | --- |
| `smart_login_required` | 匿名用户请求 Smart，必须先登录并具备 trial/付费权限 |
| `login_required_for_clone` | 用户请求原声克隆预览，需要登录 |
| `clone_consent_required` | 需要显式克隆/声音授权确认 |
| `clone_credit_checking` | 检查 trial/赠点/余额是否足够 |
| `voice_library_quota_full` | 个人音色库达到套餐上限，不能创建新克隆 |
| `clone_credit_reserved` | 克隆点数已预扣 |
| `main_speaker_clone_running` | 主说话人克隆中 |
| `multi_speaker_clone_running` | 多说话人克隆中 |
| `clone_activation_running` | 使用克隆 `voice_id` 进行正式 TTS 激活 |
| `clone_ready` | 克隆音色已保存到个人音色库，可用于预览/完整任务 |
| `clone_failed_refunded` | 克隆失败并退回点数 |
| `clone_skipped_insufficient_credits` | 点数不足，跳过克隆 |
| `preview_rendering` | 使用克隆/复用/预设音色生成预览 |
| `preview_ready` | 增强预览可播放 |

---

## 16. 后端改造范围

### 16.1 Gateway

建议新增或改造：

- 匿名 session 发放和 TTL；
- 与生产任务分离的 preview job 创建接口；
- 匿名上传接口，限制比登录上传更严格；
- YouTube URL probe / short extract 接口，并提供进度；
- preview compliance/result 接口；
- preview mode recommendation 接口；
- YouTube URL entitlement gate：
  - 匿名和 Free fail-closed；
  - trial 受 `youtube_url_enabled_for_trial` 控制；
  - paid 受 `youtube_url_enabled_for_paid` 控制；
  - allowlist 可通过 `youtube_url_user_allowlist` 覆盖；
- Studio visibility gate：
  - `studio_mode_enabled`；
  - `studio_visible_for_trial`；
  - `studio_user_allowlist`；
  - `studio_plan_allowlist`；
- claim 接口：
  - anonymous preview token + logged session -> bind user；
  - 一次性使用；
  - ownership 校验；
- preview artifact streaming：
  - 只能 stream；
  - 不给 download header；
  - 只暴露 watermark artifact；
- 限频：
  - IP；
  - IP `/24`；
  - device/session；
  - logged user；
  - source hash；
  - upload file hash；
  - YouTube canonical id；
  - Asia/Shanghai 自然日；
- Express anonymous CosyVoice temporary clone gate：
  - 只允许临时音色；
  - 不写入永久个人音色库；
  - 必须有 server-confirmed consent；
  - 受 admin 主开关、session/IP/device/source hash daily cap、active temp cap、TTL cleanup 约束；
  - 失败或 gate 拒绝时回预设音色；
- Smart clone preview reservation：
  - 读取 runtime `voice_clone_cost_credits`；
  - 检查可用点数；
  - 检查用户个人音色库容量（默认 Trial 10、Plus 30、Pro 100，后台可配置）——复用现有 `gateway/cosyvoice_clone/api.py` 的 `cosyvoice_clone_max_voices_per_user` 配额门模式（满则 409、在读样本/付费之前）+ `user_voice_service.count_active_voices_for_user_and_provider`（按 provider 计数）；**MiniMax 走的 `voice_selection_api.py` 当前无库容门，此门对 MiniMax 为净新增，须按上述模式补齐，并明确 10/30/100 是 per-provider 还是跨 provider 合计**；
  - 检查 `smart_preview_clone_daily_global_cap`、`smart_preview_clone_inflight_cap` 和供应商可用性；计数/配置不可用时 fail-closed；
  - provider call 前预扣；
  - MiniMax 克隆成功后写入用户个人音色库，记录 `provider=minimax`、`source=smart_preview`、`preview_job_id`、`voice_id`、consent 与 reservation；
  - 预览渲染必须用该 `voice_id` 正式跑一次 TTS，使音色进入可查询、可复用状态；
  - 激活、预览生成或合规失败时释放/退款，并调用供应商删除接口清理可能已创建但不可用的 `voice_id`（官方未要求“先激活才能删除”，故未激活 `voice_id` 可删；删除失败时兜底依赖 MiniMax 7 天自动 GC）；
  - 预览转完整任务时复用同一 `voice_id`，不重复克隆、不重复收取克隆点数；
  - 写 usage/cost audit。

建议不要直接把匿名 preview 语义塞进 `intercept_create_job` 的生产任务路径。独立 preview job surface 更容易保持 ownership、billing 和 artifact gate 清晰。

### 16.2 Pipeline

建议新增：

- preview segment selector：
  - 人声密集；
  - 代表性强；
  - 有硬时长上限；
  - 尽量避开片头、片尾、静音；
- preview request mode：
  - 只处理选中的时间窗口；
  - 仍使用 `SemanticBlock`；
  - 仍走 DSP-first alignment；
  - 只输出水印预览；
  - 不生成可下载 output pack；
- Express anonymous mode：
  - 可走 CosyVoice 临时克隆；
  - 克隆失败回预设；
  - 不调用 MiniMax 克隆；
- Smart anonymous mode：
  - 不开放；
  - 返回登录/trial 引导；
- logged Smart enhanced preview：
  - 通过 consent + reservation 后克隆主说话人；
  - 克隆结果作为正式个人音色保存，不走 MiniMax 临时音色 TTL 路径；
  - 使用该 `voice_id` 完成本次预览 TTS 激活；
  - 通过 `UsageMeter` 记录 voice clone usage；
  - 写足 audit metadata，供成本和后台查看。

### 16.3 Frontend

建议新增或改造：

- 首屏样本播放器组件；
- 原片/译制版同步切换；
- 样本上一段/下一段；
- 匿名试用输入框；
- 拖拽上传；
- 登录且有 URL 权限时的 YouTube 输入和进度；
- 匿名/Free 隐藏 YouTube 输入；
- Trial/Paid 根据 Gateway entitlement 显示 YouTube 输入；
- Studio 卡片不进入匿名选择；登录后按后台开关和套餐/allowlist 展示；
- 合规/分析/预览生成进度；
- 简洁方案推荐弹窗；
- 预览播放器；
- 匿名 Smart 登录/trial 引导；
- 登录/注册 handoff，保留 claim token；
- 登录后 Smart 克隆 consent + 点数预扣 UI；
- 完整任务点数估算：
  - 视频时长；
  - 所选模式；
  - 任务点数；
  - 克隆点数；
  - 是否抵扣；
  - 预扣后余额。

### 16.4 数据存储

推荐新建 preview job 存储，而不是复用生产 job 语义。

可选方案：

1. 新 DB 表：更清晰，适合后续 claim、风控和后台追踪。
2. 独立 JSON-store namespace：迁移少，但后续一致性和查询能力弱。

建议字段：

- anonymous session id hash；
- source type/ref；
- source asset pointer；
- source hash；
- upload file hash；
- YouTube canonical id（仅登录且有 URL 权限）；
- duration；
- media analysis；
- compliance status；
- selected mode；
- recommended mode；
- preview segment range；
- preview artifact keys；
- watermark policy；
- claim token hash；
- claimed user id；
- expiry time；
- consent payloads；
- clone reservation ids；
- personal voice asset id；
- provider voice id；
- voice source marker（如 `smart_preview`）；
- voice activation status。

---

## 17. 关键 UX 文案

### 17.1 匿名 Smart 登录引导

> 智能版需要注册登录并获得试用资格后才能预览。登录后可使用试用点数预扣音色克隆，优先克隆主说话人。克隆成功后音色会保存到你的个人音色库，后续任务可继续使用。

如果点数足够 1 个克隆：

> 当前点数可预扣 1 个音色克隆，系统会优先克隆主说话人，并保存到你的个人音色库。

如果点数不足：

> 当前点数不足以克隆音色，可继续使用高质量预设音色预览，或充值/升级后开启原声音色克隆。

如果个人音色库已满：

> 你的个人音色库已达当前套餐上限，可删除旧音色、升级套餐，或使用已有音色继续生成。

### 17.2 登录墙文案

按动作写，不使用笼统“请登录”：

- `登录后生成完整视频`
- `登录后保存任务`
- `登录后下载视频和字幕`
- `登录后导出剪映草稿`
- `登录并预扣点数后克隆主说话人音色，成功后保存到个人音色库`

### 17.3 点数估算文案

> 预计时长 12.4 分钟，智能版预计 1240 点；主说话人音色克隆需预扣 600 点，克隆成功并用于生成后确认扣除，音色将保存到个人音色库，失败会退回。

如果通过价格策略批准抵扣：

> 该视频满足智能版完整生成抵扣规则，主说话人克隆点数将在结算时抵扣。

---

## 18. 分阶段实施计划

### Phase 0：产品决策

需要先拍板：

- Free 匿名预览已拍板为 3 分钟；
- Express 匿名预览已拍板为 3 分钟，且可走 CosyVoice 临时克隆；
- Smart 预览已拍板为登录/trial 后 3 分钟；
- YouTube URL 已拍板：匿名和 Free 不开放；trial/paid 通过后台开关/allowlist 开放；
- Studio 已拍板：不对匿名开放，不做免费预览；默认只给正式付费用户，trial/allowlist 由后台开关控制；
- 匿名用户免费版仅 1 次预览，快捷版仅 1 次预览；
- 登录用户每天可免费预览 1 次免费版；
- trial 用户可免费预览 1 次智能版和 1 次快捷版；
- trial 用户是否增加 Smart 权限，建议作为本方案前置改造；
- MiniMax/正式音色克隆扣点建议从 500 调整到 600；
- Smart 预览 MiniMax 克隆已拍板为正式个人音色克隆，成功后保存到用户个人音色库，不走临时 TTL 路径；
- **已拍板**：个人音色库 = **跨 provider 总库容**（各 provider 调 `count_active_voices_for_user_and_provider` 求和；Express 临时音色因 `include_temporary=False` 默认天然排除，无需新增逻辑），上限 Trial 10 / Plus 30 / Pro 100，后台可配置且须 plan-aware（现有 `cosyvoice_clone_max_voices_per_user` 是单一平值，需改造）；
- Smart 预览克隆需要全局 daily/inflight cap，计数或配置不可用时 fail-closed；
- Smart 小于 10 分钟的新克隆额外预扣 600 点；
- Smart 大于等于阈值时采用平滑抵扣公式；
- **已拍板（默认规格，待设计细化）**：预览水印为半透明、抗裁剪（斜向平铺或周期位移），不遮挡口型；预览分辨率封顶 ≤720p、码率降档，确保预览观感不可替代付费下载；
- **已拍板（保守起步值，上线后按实测单位成本与转化调整）**：
  - `anonymous_preview_max_seconds = 180`；
  - `anonymous_preview_daily_global_cap = 500`；
  - `anonymous_preview_per_ip_cap = 3`、`anonymous_preview_per_device_cap = 2`（每自然日）；
  - `anonymous_clone_daily_global_cap = 100`、`anonymous_clone_active_cap = 20`；
  - `smart_preview_clone_daily_global_cap = 200`、`smart_preview_clone_inflight_cap = 5`；
  - 所有 cap 计数/配置不可用时 fail-closed；默认值必须显式写入配置，不得留空或无限；
- YouTube 和声音授权文案；
- Express 匿名 CosyVoice 临时克隆的 daily cap、active temp cap、TTL、队列权重；
- **已拍板**：合规检测复用 `src/services/content_compliance.py`，preview 路径 fail-closed，匿名 `needs_manual_review` 视为软拦截（见 §8.1）；
- **已拍板**：Phase 3 拆为 3a（仅预设音色预览，无克隆）/ 3b（Express 匿名 CosyVoice 临时克隆，挂 `anonymous_express_cosyvoice_clone_enabled` 开关 + 取证审计 + fail-closed 全局 cap）。

### Phase 1：首屏样本播放器

只改首屏样本播放器和 CTA，不改后端 preview。

验收：

- 桌面 hover 静音播放译制版；
- 原片/译制版切换同步进度；
- 上一段/下一段可用；
- 移动端 `muted + playsinline` 自动播放；
- 离开 viewport 自动暂停；
- 首屏性能不过度恶化。

### Phase 2：匿名 source intake

实现匿名本地上传、进度、probe 和合规检测。YouTube URL 只在登录且有 URL 权限的 trial/paid 路径出现。

验收：

- 匿名上传有严格限制；
- 匿名和 Free 看不到 YouTube URL 输入；
- trial/paid 的 YouTube URL 先 entitlement gate，再 probe，失败不进入昂贵处理；
- 合规按 §8.1 顺序：本地规则预筛 → ASR（仅 teaser）→ LLM 合规，block/软拦截发生在翻译/TTS/clone 前；合规模块异常时 fail-closed；
- 进度可见；
- preview record 会过期清理。

### Phase 3a：匿名 Free/Express 预览（仅预设音色，无克隆）

实现 Free/Express 3 分钟水印预览，仅用预设音色，不触发任何克隆。这是漏斗收益的主体，风险最低，先发。

验收：

- Free/Express 预览不超过 3 分钟；
- 所有预览有水印、不暴露下载；
- 匿名 Smart 不开放，返回登录/trial 引导；
- `anonymous_express_cosyvoice_clone_enabled` 默认关闭，本阶段不调用任何克隆 provider；
- 匿名用户免费版/快捷版各只能预览 1 次；
- 付费任务优先级高于匿名任务；
- 限频有效。

### Phase 3b：Express 匿名 CosyVoice 临时克隆（独立开关，后置加固）

在 3a 稳定后，再打开 `anonymous_express_cosyvoice_clone_enabled` 灰度 Express 匿名临时克隆。这是全方案法律/滥用风险最高的一段，必须单独发布。

验收：

- 仅走 CosyVoice 临时音色，绝不调用 MiniMax 克隆；
- consent + session/IP/device/source-hash cap + 全局 daily/active cap，全部 fail-closed；
- 失败或 gate 拒绝时回预设音色继续；
- 临时音色不写永久库、有 TTL cleanup；
- **取证审计**：记录 IP/`/24`/UA/指纹/source hash/consent 时间戳，保留期长于媒体 TTL，供投诉/下架追溯；
- 全局 cap 是唯一不可伪造的兜底，计数存储不可用时拒绝克隆并告警。

### Phase 4：登录认领与 Smart 增强预览

实现 claim token 和 Smart 克隆预览点数 gate。

验收：

- claim token 一次性绑定到登录用户；
- 克隆 consent 是显式动作；
- clone provider call 前已预扣点数；
- 点数只够一个克隆时只克隆主说话人；
- Smart 预览克隆成功后保存到个人音色库；
- 预览 TTS 使用该 `voice_id` 完成激活；
- 个人音色库达到套餐上限时拒绝新克隆并提示删除/升级/复用；
- 克隆失败退回点数；
- 激活或预览生成失败时清理可能已创建但不可用的供应商 `voice_id`；
- 复用已有个人音色不收克隆点数。

### Phase 5：完整任务转化

把 preview record 转成生产任务创建和结算。

验收：

- 生成完整任务前展示点数估算；
- 完整预览、下载、保存、编辑、剪映草稿都需要登录；
- 生产任务仍读取 Gateway pricing/entitlement 真源；
- preview artifact 不能绕过支付 gate；
- Smart 预览已保存的个人音色在完整任务中复用同一 `voice_id`；
- 预览转完整任务不能重复克隆、不能重复收取克隆点数；
- 后台成本报告能看到 preview/clone usage。

---

## 19. 测试计划

后端测试：

- anonymous session 创建和过期；
- preview 上传限制；
- 匿名/Free YouTube URL gate fail-closed；
- trial/paid YouTube probe 失败不触发昂贵处理；
- 合规拒绝停止 pipeline；
- Free/Express 3 分钟 preview duration cap；
- watermark-only artifact access；
- 匿名 Smart 不开放，也不写 billable clone usage；
- 登录 Smart 有 600 点可用时预扣一个克隆；
- 点数只够一个时只克隆主说话人；
- 点数足够多个时按优先级克隆；
- Smart 预览克隆成功后写入个人音色库；
- 个人音色库限额按 Trial 10、Plus 30、Pro 100 生效；
- Smart 预览 TTS 使用克隆 `voice_id` 完成激活；
- Smart 预览转完整任务复用同一 `voice_id`，不重复扣克隆点；
- 克隆失败释放 reservation；
- 激活或预览生成失败时释放 reservation 并清理不可用 `voice_id`；
- claim token 一次性绑定 ownership；
- anonymous session/cookie/IP/fingerprint/source hash 风控计数生效；
- Studio 不出现在匿名选择卡片；
- Studio 后台开关/allowlist 控制 trial/paid 可见性；
- 未登录或未付费不能下载产物。

前端测试：

- 样本播放器桌面/移动端行为；
- 拖拽上传进度；
- 匿名/Free 隐藏 YouTube 输入；
- trial/paid 有权限时显示 YouTube 进度状态；
- 方案推荐弹窗；
- 匿名 Smart 登录/trial 引导；
- 匿名选择中不显示 Studio；
- 登录墙按动作显示；
- Smart 克隆 consent 和点数估算；
- 登录后 claim flow。

集成测试：

- 匿名 Free/Express 端到端生成 3 分钟水印预览；
- 登录增强 Smart 预览只克隆主说话人；
- claimed preview 转完整任务；
- 付费任务优先于匿名任务；
- artifact gate 拒绝匿名下载。

---

## 20. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 匿名流量烧光生产成本 | 3 分钟上限、低优先级、限频、水印、TTL；Express 临时克隆受 cap |
| 匿名风控被绕过 | session/cookie/IP/IP 段/fingerprint/source hash 多因素计数；可疑时验证码或要求登录 |
| 用户误以为匿名 Smart 可用 | 匿名 Smart 直接显示登录/trial 引导 |
| 点数不足以兑现 Smart 克隆承诺 | trial/赠点/余额必须 >= 600；不足则走预设或复用音色 |
| Smart 预览 MiniMax 克隆导致个人音色无限膨胀 | Trial 10、Plus 30、Pro 100 的个人音色库限额；超限时要求删除旧音色、升级或复用 |
| MiniMax 预览克隆产生未激活音色后被供应商 7 天清理 | 预览生成阶段必须用克隆 `voice_id` 正式 TTS 激活；激活失败则退款并删除不可用 `voice_id` |
| Smart 预览转完整任务出现重复克隆或重复扣点 | 预览克隆保存为个人音色；完整任务复用同一 `voice_id`，capture-once，不 re-clone |
| Smart 预览克隆消耗供应商账号资源过快 | `smart_preview_clone_daily_global_cap`、`smart_preview_clone_inflight_cap`、个人库限额和 fail-closed 配置 |
| 小于 10 分钟视频变成免费完整生产 | 匿名只给 3 分钟；完整生成必须登录/扣点 |
| 免费流量阻塞付费任务 | 队列优先级和 worker quota |
| 预览产物替代付费下载 | stream-only、水印、低清晰度、owner/payment gate |
| 未经明确 consent 发生克隆 | consent + 预扣点数 + provider call 前 gate |
| 匿名任务登录后丢失 | claim token 绑定 |
| YouTube/声音权利风险 | 匿名/Free 不开放 YouTube；trial/paid 授权声明 + 合规前置 |
| Studio 工作台被匿名用户误用 | Studio 不进匿名漏斗；后台开关、套餐和 allowlist gate |
| UI 与 Gateway 价格漂移 | 前端只消费 Gateway 估算和定价接口 |

---

## 21. 推荐决策

建议评审批准：

1. Free 匿名预览 3 分钟，带水印，不可下载。
2. Express 匿名预览 3 分钟，带水印，不可下载，可走 CosyVoice 临时克隆。
3. Express 匿名临时克隆必须 consent + admin gate + cap + TTL cleanup，失败回预设。
4. Smart 不开放匿名预览，必须登录且具备 trial/Plus/Pro 权限。
5. YouTube URL 不对匿名/Free 开放；trial/paid 通过后台开关和 allowlist 开放。
6. Studio 不对匿名开放，不做免费预览；默认只给正式付费用户，后台开关可灰度 trial/allowlist。
7. 匿名用户免费版只能预览 1 次，快捷版只能预览 1 次。
8. 登录用户每天可免费预览 1 次免费版；trial 用户可免费预览 1 次智能版和 1 次快捷版。
9. Trial 用户增加 Smart 权限，但仍受 Smart 双 kill switch 控制。
10. MiniMax/正式音色克隆扣点从 500 调整为 600。
11. 登录 Smart 3 分钟预览本身不扣分钟点数；如克隆主说话人，预扣 600 点。
12. 点数只够 1 个克隆时，只克隆主说话人。
13. Smart 预览 MiniMax 克隆作为正式个人音色保存到用户音色库，不走临时 TTL 路径。
14. 个人音色库限额建议：Trial 10、Plus 30、Pro 100；限额不等于免费克隆次数。
15. Smart 预览克隆生成的 `voice_id` 必须用于本次预览 TTS 激活，后续完整任务复用同一 `voice_id`。
16. Smart 完整任务小于 10 分钟时，如需新克隆，应额外预扣克隆点数。
17. Smart 长视频克隆优惠采用平滑抵扣公式，不做硬阈值白送。
18. 完整生成、保存、下载、字幕、编辑和剪映草稿导出都必须登录并通过点数/支付 gate。

---

## 22. 待决问题

1. Trial overlay 增加 Smart 权限的具体实现是否直接复用 Plus allowed modes，还是显式配置 `trial.allowed_service_modes`？
2. Express 匿名 CosyVoice 临时克隆的 daily cap、active temp cap、TTL 和队列权重是多少？
3. Free 登录后是否允许下载完整 10 分钟水印体验，还是只下载 3 分钟预览？
4. Smart 长视频克隆平滑抵扣从第几分钟开始、每分钟抵扣多少、是否按克隆数量分别抵扣？
5. Studio 对 trial 是否默认不可见，还是允许通过 allowlist 灰度？
6. YouTube 导入和声音授权的最终法务文案是什么？
7. 未付费预览的水印强度、位置和分辨率如何设定？
8. 匿名限频具体数值是多少：IP、IP 段、设备、source hash、自然日分别如何限制？
9. MiniMax 已激活克隆音色是否存在商务合同层面的数量、保存时长或账号级资源限制？实现前需要用当前合同/控制台再确认一次。

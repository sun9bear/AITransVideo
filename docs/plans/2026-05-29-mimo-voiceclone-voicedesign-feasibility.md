# MiMo VoiceClone / VoiceDesign 接入可行性评估

**日期**：2026-05-29
**类型**：可行性调研（**只评估，不接自动路径**；非实施 plan）
**触发**：用户问 "MiMo TTS v2.5 有音色克隆吗" → 衍生的独立评估
**结论先行**：技术上可行，但 **不是 MiniMax 克隆的 drop-in 替代**；MiMo 的"克隆"机制与项目现有 `create_voice→voice_id→复用` 抽象根本不同。最佳落点是**新的、用户显式触发的"zero-shot 样本音色"或"一句话造音色"功能**，而非替换 Smart 的 MiniMax 克隆。需要一次 spike 验证质量后再决定是否做。

---

## 1. MiMo 提供什么（官方核实 2026-05-29）

MiMo-V2.5-TTS 是一个**三模型系列**（均限时免费、无截止日期）：

| 模型 | 能力 | 产物 |
|---|---|---|
| `mimo-v2.5-tts` | 基础合成，3 预置音色 + `<style>`/自然语言/音频标签风格控制 | 音频 |
| `mimo-v2.5-tts-voiceclone` | 几秒参考音频高保真复刻真人音色 | 音频（**无持久 voice id**） |
| `mimo-v2.5-tts-voicedesign` | 一段文字描述生成全新音色 | 音频（**无持久 voice id**） |

**关键机制（决定一切）**：
- 端点都是 `POST https://api.xiaomimimo.com/v1/chat/completions`，靠 `model` 区分。
- voiceclone：参考音频**每次合成内联传**——`audio.voice = "data:{mime};base64,$BASE64"`（≤10MB，mp3/wav），同步返回。
- **没有** create_voice 步骤、**没有** 可复用 voice id、**没有** 注册/轮询/有效期。
- voicedesign：音色描述写在 user 消息里，同样内联、一次性。
- 来源：官方 [v2.5-tts 用法文档](https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5)、[发布公告](https://platform.xiaomimimo.com/docs/zh-CN/news/v2.5-tts-release)。

> ⚠️ 这些 API 细节来自官方文档页 + WebFetch 摘要，**实施前必须 spike 实测**（确认 base64 字段名、MIME、响应结构、质量），与 Phase 0a 同样纪律。

## 2. 项目现有克隆架构（对照基线）

项目已有**两个**持久-voice-id 克隆 provider，共用同一抽象：

- **MiniMax**：`src/services/voice_clone.py::MiniMaxVoiceCloneClient.create_voice_clone(...)` → 持久 voice_id；Gateway 端点 `voice_selection_api.py::voice_clone_for_selection`（`POST /job-api/jobs/{id}/voice-clone`）。
- **CosyVoice**（mainland worker）：`real_cosyvoice.py::clone()` → `create_voice()` → 持久 voice_id；consent 硬门（`voice_clone_confirmed`）。

共同模式：**`clone(sample) → voice_id → 存库（VoiceRegistry / UserVoice，file-locked）→ 后续 TTS 引用 voice_id 复用`**。计费：`minimax:voice_clone ¥9.9/clone`，首次 T2A 合成时结算；shadow credits / quota 围绕"一次克隆事件"建模。Smart auto-clone：`compute_job_policy` 设 `voice_clone_enabled=True` / `voice_strategy=smart_auto`，受 `admin.smart_auto_clone_enabled` + 用户套餐 gate。

## 3. 根本架构错配

| 维度 | MiniMax / CosyVoice（现有） | MiMo voiceclone |
|---|---|---|
| 克隆事件 | 离散一次 `create_voice` | **无**——每次合成内联参考音频 |
| 产物 | 持久 `voice_id` | 无；零样本即时合成 |
| 复用 | voice_id 跨段/跨任务复用，存 UserVoice 库 | 无库可言；每次合成都要带参考音频 |
| 计费 | 按"克隆次数"（¥9.9/次，首次合成结算） | 按"合成 token"（现免费） |
| consent 门 | 卡在离散 clone 事件 | 没有离散事件→consent 要卡在"用这段参考音频合成" |
| 等待 | 异步/轮询（CosyVoice 阻塞轮询） | 同步直返 |

**结论**：MiMo "克隆" 本质是 **zero-shot voice matching**，根本不符合项目 `clone→voice_id→存库→复用` 这套抽象。**不能简单塞进现有 VoiceRegistry / UserVoice / shadow-credit 流程**——它是另一种东西。

## 4. 接入选项与复杂度

### 选项 A：把 MiMo voiceclone 当"用户上传样本 → 该说话人 zero-shot 配音"（Express/Studio，用户显式触发）
- 流程：用户为某说话人上传/选定一段参考音频 → 该说话人所有段的 TTS 走 `mimo-v2.5-tts-voiceclone`，**每段把参考音频内联**。
- 复杂度：**中**。要：(a) 参考音频存储 + 合成时取用；(b) `mimo_tts_provider` 加 voiceclone 调用分支（内联 base64）；(c) 前端"上传样本音色"入口（用户显式触发，满足硬约束）；(d) **绕过** VoiceRegistry/voice_id 复用模型（MiMo 无 id）。
- 注意：每段合成都传参考音频（≤10MB）→ 带宽 + 延迟成本；段多时累积。

### 选项 B：MiMo voicedesign "一句话造音色"（最有意思）
- 流程：用户用文字描述（"沉稳中年男声、播音腔"）生成音色 → 该说话人合成走 voicedesign，描述内联。
- 对项目的价值：现有 voice-matching 是在 MiniMax 604 / CosyVoice / VolcEngine 预置库里 rerank 匹配；voicedesign 能**按 S2 speaker profile 的画像描述直接生成**，可能比预置库匹配更贴。
- 复杂度：**中**。与 A 类似，但输入是描述文本而非音频，存储更轻。
- 仍是用户显式触发 / admin 配置，不进自动路径。

### 选项 C（不推荐）：替换 Smart 的 MiniMax 克隆
- ❌ Smart 整套 clone/quota/UserVoice mirror/provider exhaustion 围绕"持久 voice_id + 克隆次数计费"建模，MiMo 无 voice_id → 要重写一大片。
- ❌ 触碰 Smart 用户承诺面，需产品 sign-off。
- ❌ 建立在"现在免费"上，违反 plan §"不做"。

## 5. 硬约束（必须守）

- **付费 API 硬约束（CLAUDE.md）**：克隆必须用户显式触发，禁止 fallback/兜底/批量自动调用。MiMo 无离散克隆事件，consent 要卡在"用这段参考音频/这段描述去合成"的用户动作上。
- **plan §5.2 现状**：明确禁止 MiMo voiceclone/voicedesign 进入**自动路径 / fallback**。本评估**不推翻**该禁令——选项 A/B 都是**用户显式触发的新功能**，不是自动路径。
- **限免无截止**：MiMo TTS 系列免费、无截止日期、随时可能转收费且不一定提前通知。**绝不**把套餐/UX 建立在免费假设上（plan §"不做"）。转收费后是 token 计费，且按 §3 是 per-synthesis 计费（不是一次性 ¥9.9）。
- **Smart 不动**：仍 MiniMax。

## 6. 成本对比（粗略）

| | MiniMax 克隆 | MiMo voiceclone |
|---|---|---|
| 现在 | ¥9.9/次克隆，之后 voice_id 复用合成另计 | 免费（无截止） |
| 计费单位 | 克隆次数 | 合成 token（per-synthesis） |
| 转收费风险 | 价格稳定 | 随时转收费，且每段都计 |
| 长任务 | 克隆一次，多段复用，边际成本低 | 每段都带参考音频合成，转收费后边际成本随段数线性涨 |

**要点**：免费期 MiMo 更便宜；一旦转收费，per-synthesis 模型对**多段长视频**可能比 MiniMax "克隆一次复用" **更贵**。成本优势不稳。

## 7. 风险

1. **免费随时结束** → 成本模型翻转，不能依赖。
2. **per-synthesis 内联参考音频** → 带宽 / 延迟 / 10MB 上限；段多时放大。
3. **无 voice 库复用** → 不能像现在那样"克隆一次跨任务复用命名音色"。
4. **质量未验证** → 与 MiniMax/CosyVoice 克隆质量、风格遵循、音色一致性需 benchmark。
5. **法律/伦理 consent** → 克隆真人声音的授权问题（与 MiniMax 同级，但 per-synth 机制下 consent 落点不同）。
6. **API 细节未实测** → §1 机制来自文档摘要，须 spike 确认。

## 8. 推荐

- **可行，但定位为"新的用户显式触发功能"**，不是 MiniMax 克隆的替代，不进自动路径，不动 Smart。
- **最有价值的是选项 B（voicedesign 一句话造音色）**：和项目 voice-matching 的"按 speaker profile 选音色"诉求契合，输入是描述文本（无音频存储/带宽负担），且避开"克隆真人"的法律敏感。
- **前置**：先做一次 **spike**（确认 voiceclone/voicedesign 的真实请求结构 + 质量），再决定是否进入正式 plan。spike 免费、用户显式触发、docker exec 跑，符合约束。
- **不要**因为"免费"就急着做——免费无截止、随时转收费，且 per-synthesis 成本对长视频不利。

## 9. 工作量（若决定做选项 B 试点）

- spike（确认 API + 质量）：~0.5 天
- `mimo_tts_provider` 加 voicedesign 分支 + 描述透传：~1 天
- 前端"描述生成音色"入口（用户显式触发）+ 接线：~1–2 天
- 计量 / promotional 成本表达（复用 Phase 3 的 promotional 机制）：~0.5 天
- benchmark（vs 预置库匹配质量）：视样本而定
- **不碰**：VoiceRegistry / UserVoice / shadow credits / Smart clone 合同（MiMo 无 voice_id，不走这套）

## 10. 决策记录

- **2026-05-29**：评估发现 MiMo voiceclone/voicedesign 是 **per-synthesis 内联、无持久 voice id** 的 zero-shot 机制，与项目 `clone→voice_id→复用` 抽象根本不同 → 不能 drop-in 替代 MiniMax/CosyVoice 克隆。建议作为**用户显式触发的新功能**（首选 voicedesign），spike 验证后再开正式 plan；维持 plan §5.2 "不进自动路径" + Smart 不动 + 不依赖免费。

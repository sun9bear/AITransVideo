# Phase 4.2 — CosyVoice 克隆前端 wiring 方案

> **状态**：方案文档 v1（2026-05-26）
> **关联**：[Phase 4 go-live plan](2026-05-24-cosyvoice-phase4-go-live-plan.md) §Phase 4.1 已落地
> **作者**：Claude（Opus 4.7）
> **触发**：2026-05-26 部署后端 CosyVoice clone 全链路完成、跑通 OSS PUT/GET/DELETE 探针、把武汉 worker 切 live，但用户在前端选 CosyVoice tab 点"克隆音色"实际走的仍是旧 MiniMax endpoint —— Phase 4.1 漏接的最后一根线。

---

## §0 背景与触发事件

### 0.1 触发事件（2026-05-26 真实证据）

用户首次在前端跑 CosyVoice 克隆烟测，操作路径：
1. 新建任务 → 跑到音色选择步骤
2. 点说话人 A 的 **CosyVoice tab**（Phase 4.1 process.py:7523 supports_clone hotfix 后已可见）
3. 点"**克隆音色**"按钮 → modal 弹出 → 选样本 → 确认

期望：调武汉 worker / DashScope CosyVoice 克隆 API。
实际：

```
DB user_voices 最新行：
  created_at = 2026-05-26 03:18:05+00
  provider   = minimax_voice_clone       ← 走了 MiniMax
  voice_id   = vt_speaker_a_1779765478206  ← MiniMax voice_id 格式
  source_job_id = job_5304d1aac1664f9dbcfb2a3eb2cb7983

武汉 worker audit JSONL 最新行：
  2026-05-25T03:09 (昨日 phase40b livecheck) — 用户那次克隆从未到武汉
```

**用户的 MiniMax 账户被扣了一次克隆调用费用**（CosyVoice 0 调用，0 扣费）。前端"已克隆"badge 因为只看 `voice_type=cloned` 不区分 provider，所以亮起；下拉列表按 provider 过滤，所以 CosyVoice tab 下看不到这条 MiniMax clone。

### 0.2 根因

Phase 4.1 backend 在 [`gateway/cosyvoice_clone/api.py`](../../gateway/cosyvoice_clone/api.py) 落地了独立的 CosyVoice clone endpoint `POST /api/voice/cosyvoice/clone`，但**前端 `cloneVoiceForSelection()` 写死调旧 endpoint**：

```ts
// frontend-next/src/lib/api/voiceSelection.ts:48
`/jobs/${input.jobId}/voice-clone`   // ← 旧 MiniMax endpoint
```

两个 endpoint 契约**完全不同**：

| 维度 | 旧 MiniMax endpoint | 新 CosyVoice endpoint |
|---|---|---|
| **路径** | `POST /job-api/jobs/{id}/voice-clone` | `POST /api/voice/cosyvoice/clone` |
| **Content-Type** | `application/json` | `multipart/form-data` |
| **输入字节** | 不传字节，传 `segment_ids`，后端 ffmpeg 拼 | 传 `sample: UploadFile` 真音频 |
| **Consent** | 单 bool `consent_confirmed` | 三件套：`consent_voice_clone_confirmed="true"` + `consent_modal_version="2026-05-25-v1"` + `consent_confirmed_at=<ISO>` |
| **Provider 路由** | 不需要（隐含 MiniMax） | 需要 `target_model`（`cosyvoice-v3.5-flash` / `cosyvoice-v3.5-plus`） |
| **Job 上下文（Phase 4.1 已实现）** | `speaker_id` 隐含 → 后端读 transcript 拼音频 | `source_segments` + `source_job_id` 作为 audit hint，**不影响字节来源** |
| **Job 上下文（Phase 4.2 升级目标）** | 不变 | **`source_segments` 升级为主输入**：后端拼音频；`sample: UploadFile` 降为可选兼容入口（二选一互斥，详见 §4.1） |
| **样本要求** | 后端 ffmpeg 控；总时长 10-300s | sample_validator 强校验：3-60s、≤10MB、WAV PCM16 必须、≥16kHz |

Phase 4.1 的 7 个子阶段（A-G）+ 二轮 review + 部署都做完了；缺的就是这根接线。

### 0.3 影响范围声明（必须读完再继续）

| 不能动 | 原因 |
|---|---|
| **旧 MiniMax `/jobs/{id}/voice-clone` endpoint** | Studio 版人工 review 路径在用 |
| **Smart 版自动 MiniMax clone**（[`src/pipeline/process.py:3640-4100`](../../src/pipeline/process.py)） | 智能版的核心闭环之一，独立 consent 标志 `smart_consent.auto_voice_clone` |
| **`_register_smart_clone_in_user_voices()`**（[process.py:1374-1500](../../src/pipeline/process.py:1374)） | Smart auto-clone 注册回灌 Gateway 的内部链路 |
| **`gateway/user_voice_service.py:add_user_voice()`** | 注册函数本身**可以共用**，但**不要改签名**或默认值 |
| **现有 `VoiceCloneModal`**（[`VoiceSelectionPanel.tsx:1314+`](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx)） | MiniMax 路径专用，不要修改逻辑 |
| **`tests/test_voice_clone.py`、`tests/test_voice_selection_api.py`** | MiniMax 行为契约，禁止改断言 |

---

## §1 目标 / 非目标

### 1.1 目标

- **G1**：CosyVoice tab 下"克隆音色"按钮触发 `POST /api/voice/cosyvoice/clone`（武汉 worker 真链路），不再误调 MiniMax
- **G2**：UI 适配 CosyVoice 样本要求（3-60s、≤10MB、WAV PCM16/MP3/M4A、≥16kHz）+ target_model 选择 + consent 三件套
- **G3**：克隆成功的 voice **立即在当前任务的音色 dropdown 可选**；按 §12 保存策略，默认走「本任务临时克隆」分组（带"临时·剩余 N 天"标签），仅当用户勾"保存到我的音色库"才进长期「我的 CosyVoice 克隆音色」分组
- **G4**：MiniMax 克隆（Studio 人工 + Smart 自动）行为字节级不变，CI 守卫覆盖
- **G5**：错误路径（503 worker 未启用 / 503 OSS 未配 / 400 样本不合格）有明确文案

### 1.2 非目标

- **NG1**：不改 MiniMax clone modal、不改 MiniMax 后端 endpoint、不改 Smart 自动 clone 触发链
- **NG2**：不动 `voice_clone_cost_credits` 定价（Phase 4.1 已落 pricing）
- **NG3**：不做"克隆完直接全任务用"的自动 reuse（保持现有 voice_reuse 流程）
- **NG4**：不引入 VolcEngine 克隆（VolcEngine clone 目前 0 实现，不在本 plan 范围）
- **NG5**：不重构 `VoiceCloneModal` —— 保持独立，方便回滚

### 1.3 验收标准（DoD）

1. ✅ CosyVoice tab + "克隆音色"按钮 → `POST /api/voice/cosyvoice/clone` 200 → 武汉 worker audit JSONL 出现 `operation: clone, provider: cosyvoice_voice_clone` 条目
2. ✅ DB `user_voices` 新行 `provider="cosyvoice_voice_clone"`、`voice_id="cosyvoice-v3.5-*"` 格式、**默认 `is_temporary=TRUE, expires_at=now+7d`**（见 §12）
3. ✅ 克隆完成后**当前任务的音色 dropdown 立即可选**；**长期"我的 CosyVoice 克隆音色"分组**仅在用户主动勾"保存到我的音色库"时显示该音色
4. ✅ MiniMax tab + "克隆音色"按钮 → `POST /jobs/{id}/voice-clone` 仍 200 → 武汉 worker audit JSONL **不** 出现新条目（MiniMax 路径 voice 仍 `is_temporary=FALSE`，行为字节级不变）
5. ✅ Smart 版任务自动 clone → `provider="minimax_voice_clone"` 仍正常注册（CI 守卫 + 手动 smoke）
6. ✅ 错样本（≤3s / >60s / 非 PCM16 WAV / 总时长越界）→ **前端禁用"开始克隆"按钮 + 红字提示**；用户绕过前端（直接打 endpoint）时**后端 400** 早 fail，**绝不打武汉 worker**（守 worker 调用预算 + DashScope 额度）
7. ✅ 临时音色过 7 天 → 清理任务删 DashScope + 删本地 user_voices 行；用户后续 regenerate 引用该 voice_id → 友好提示"音色已过期，请重新克隆"，**绝不静默重克隆**
8. ✅ 跨 phase 回归测全绿 + 新增 5 条以上守卫测试

---

## §2 当前状态审计（来自 2026-05-26 codebase grep）

### 2.1 MiniMax 路径（不动）

| 文件 | 行号 | 角色 |
|---|---|---|
| [`gateway/voice_selection_api.py`](../../gateway/voice_selection_api.py) | 1-885 | `voice_clone_for_selection()` 主 handler，读 segment_ids → ffmpeg 拼 → MiniMax 客户端 → `add_user_voice()` |
| [`gateway/voice_selection_api.py`](../../gateway/voice_selection_api.py) | 887-960 | `_concat_segments_ffmpeg()` 拼接函数 —— **可复用** |
| [`src/services/voice_clone.py`](../../src/services/voice_clone.py) | 1-300 | `MiniMaxVoiceCloneClient` HTTP 客户端 |
| [`src/pipeline/process.py`](../../src/pipeline/process.py) | 3640-4100 | Smart 自动 clone 触发链；strict 检查 `smart_consent.auto_voice_clone is True` |
| [`src/pipeline/process.py`](../../src/pipeline/process.py) | 1374-1500 | `_register_smart_clone_in_user_voices()` 内部回灌 |
| [`frontend-next/.../VoiceSelectionPanel.tsx`](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx) | 1314-1590 | 现有 `VoiceCloneModal` —— **不动** |
| [`frontend-next/.../voiceSelection.ts`](../../frontend-next/src/lib/api/voiceSelection.ts) | 44-57 | `cloneVoiceForSelection()` —— **不动**，保持 MiniMax 调用 |

### 2.2 CosyVoice 后端（Phase 4.1 已部署）

| 文件 | 行号 | 角色 |
|---|---|---|
| [`gateway/cosyvoice_clone/api.py`](../../gateway/cosyvoice_clone/api.py) | 177-500 | `cosyvoice_clone()` endpoint，已有 5 层 fail-closed |
| [`gateway/cosyvoice_clone/sample_validator.py`](../../gateway/cosyvoice_clone/sample_validator.py) | - | 样本校验（3-60s / ≤10MB / WAV PCM16 / MP3 / M4A） |
| [`gateway/cosyvoice_clone/audio_processor.py`](../../gateway/cosyvoice_clone/audio_processor.py) | - | 内部转码处理 |
| [`gateway/cosyvoice_clone/sample_uploader.py`](../../gateway/cosyvoice_clone/sample_uploader.py) | 1-420 | `AliyunOssUploader`（presign GET 不含 `ResponseContentType` —— A0a 已永久化，commit `1575b68f`, 2026-05-26 落 US prod gateway image） |
| [`gateway/mainland_voice_worker.py`](../../gateway/mainland_voice_worker.py) | - | Gateway 侧 HMAC client |
| [`src/services/mainland_worker/client_factory.py`](../../src/services/mainland_worker/client_factory.py) | - | Pipeline 子进程 env-only factory（昨天加了 `is_worker_enabled_in_env()`） |

### 2.3 CosyVoice 前端（缺口）

| 文件 | 行号 | 缺口 |
|---|---|---|
| [`frontend-next/.../voiceSelection.ts`](../../frontend-next/src/lib/api/voiceSelection.ts) | - | **0 函数**调用 `/api/voice/cosyvoice/clone` |
| [`frontend-next/.../VoiceSelectionPanel.tsx`](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx) | 994-1003 | "克隆音色"按钮 onClick 调旧 modal，不分流 |
| 无 | - | 无 `CosyVoiceCloneModal` 组件 |
| 无 | - | 无 `ConsentModal` 组件（Phase 4.1 plan §授权文案 v1 只在文档里） |

### 2.4 共享基础设施（两边都用）

| 文件 | 角色 |
|---|---|
| [`gateway/user_voice_service.py`](../../gateway/user_voice_service.py) | `add_user_voice()`，新增 voice 行 |
| [`gateway/voice_calibration_hook.py`](../../gateway/voice_calibration_hook.py) | clone 完成 hook |
| [`/gateway/user-voices`](../../gateway/user_voice_api.py) | 查询用户音色库的 REST |

---

## §3 关键设计决策

### 3.1 决策 A：音频字节路径 —— Option C ✅

**问题**：CosyVoice endpoint 当前要 `sample: UploadFile = File(...)`（multipart 真音频字节）。前端没有 Web Audio API 拼 WAV 的代码。

**候选**：
- **A**：前端 fetch 每段 `audioUrl` → 用 Web Audio API decodeAudioData → 拼 → 重新编码 WAV 16-bit / 16kHz → multipart 上传。优点：后端契约不动。缺点：~200 行音频处理代码，浏览器兼容性、采样率转换坑多。
- **B**：限制用户只选一段。优点：最简单。缺点：CosyVoice 推荐 10-20s prompt，单段往往不够；用户体验降级。
- **C** ✅：**后端加 `by_segment_ids` 模式 + 复用 `_concat_segments_ffmpeg()`**。优点：契约改一处，前端逻辑最薄，复用已经验证过的 ffmpeg 流水。缺点：CosyVoice endpoint 签名要变（向后兼容方式：`sample` 改 `Optional`，新加 `source_segments` 作为主入口）。

**选 C**。理由：
1. ffmpeg 拼接是已经 production-grade 跑了 1 年+ 的代码
2. 前端不需要新依赖、不需要测浏览器音频兼容
3. 改 endpoint 签名是 backwards-compatible（`sample` 改 Optional，二选一）
4. CosyVoice 的样本要求"WAV PCM16 / 16kHz" 正好是 ffmpeg 输出的格式（24kHz 改 16kHz 一行参数）

### 3.2 决策 B：UI 入口 —— "克隆音色"按钮 onClick 内部分流 ✅

**问题**：CosyVoice tab 和 MiniMax tab 共用同一个"克隆音色"按钮（VoiceSelectionPanel.tsx:994）。点击时怎么决定开哪个 modal？

**候选**：
- **B1**：按 tab 完全独立 —— 两个按钮 "克隆 MiniMax 音色" / "克隆 CosyVoice 音色"。优点：完全无歧义。缺点：UI 拥挤；MiniMax tab 下"克隆 CosyVoice"按钮没意义。
- **B2** ✅：**同一按钮，onClick 时按 `state.selectedProvider` 分流开不同 modal**。优点：UI 一致；用户心智模型清晰（在哪个 tab 下就克隆哪个 provider 的）。缺点：状态依赖性变强。

**选 B2**。

### 3.3 决策 C：Consent modal —— 独立组件，CosyVoice 专用 ✅

**问题**：CosyVoice endpoint 要 consent 三件套（`consent_voice_clone_confirmed="true"` literal + `consent_modal_version="2026-05-25-v1"` literal + `consent_confirmed_at=<ISO>`）。MiniMax 不需要这个版本号契约。

**候选**：
- **C1**：复用现有 MiniMax modal 的 consent checkbox。缺点：版本号契约会漂移；MiniMax consent 是单 bool。
- **C2** ✅：**新建 `CosyVoiceConsentModal` 独立组件**。一次性显示授权文案 v1 全文，要求用户**显式**点"我已阅读并同意"。点同意后才解锁"开始克隆"按钮。

**选 C2**。文案直接用 Phase 4.1 plan §授权文案 v1（已经 review 过）。

### 3.4 决策 D：target_model 选择 UX —— 默认 flash + 高级展开 plus ✅

**问题**：CosyVoice 有两个模型 `cosyvoice-v3.5-flash`（快）和 `cosyvoice-v3.5-plus`（高质量但慢/贵）。

**候选**：
- **D1**：modal 顶部 radio button 二选一。
- **D2** ✅：**默认 flash**（隐藏 plus），高级折叠面板里露 plus。理由：99% 用户用 flash 够了；plus 给重度用户。
- **D3**：完全隐藏 plus，强制 flash。缺点：plan 既然定义了 plus，应让用户能选。

**选 D2**。flash 即可让用户跑通；后续 admin 可以根据用量决定是否把 plus 提到一线。

### 3.5 决策 E：克隆音色在下拉的归类 —— 临时 / 长期分开显示 ✅（v4 细化）

**问题**：克隆出来的 CosyVoice voice 该怎么显示在 CosyVoice tab 下拉里？

v3 的选择是单一独立 grouping（「我的 CosyVoice 克隆」）。**v4 根据 §12 保存策略细化**：

下拉**两个新增 grouping**（在现有「女声 / 男声 / 其他」之上）：

| Grouping | 包含 | 显示规则 |
|---|---|---|
| **本任务临时克隆** | `is_temporary=TRUE` **且** `source_job_id == current_jobId` 的 voice 行 | 仅在该 voice 所属任务里显示；voice option 文字后带"临时·剩余 N 天"标签 |
| **我的 CosyVoice 克隆音色** | `is_temporary=FALSE` 的所有 voice 行（用户主动保存的） | 跨任务全局显示 |

**注意**：临时音色**不**跨任务展示。Task A 克隆的临时音色，在 Task B 的 dropdown 里**看不到**——避免用户在 B 任务误用了 A 任务的临时音色，结果 7 天后被清理引起困惑。

**命名**：用文字标签，**不带 emoji**（v3 原 emoji 的"🎤"去掉，避免与 MiniMax 现有「我的克隆音色」风格漂移）。

**MiniMax 路径** dropdown 行为**不动**——历史 voice 仍混在 MiniMax tab 现有 grouping 里，等 Phase 4.4 统一处理。

### 3.6 决策 F：Phase 4.1 两条 hotfix 拆分提交 ✅（Codex 2026-05-26 收紧）

最初 plan v1 想把 3 条 hotfix 在 **A0 pre-step** 一次性 commit。**Codex 审核驳回**——这会造成生产 strictly worse state：CosyVoice tab 显示"克隆音色"按钮，但前端 dispatch 未接好，点击仍走 MiniMax，**继续误扣 MiniMax 账户**。比按钮压根不显示还坑。

**修订后的拆分**：

| 子阶段 | 包含 commit | 时机 | 状态 |
|---|---|---|---|
| **A0a** ✅ 已落 | `gateway/cosyvoice_clone/sample_uploader.py` + `tests/test_cosyvoice_clone_sample_uploader.py` | **可立即 land** —— OSS 修复独立可用，不依赖前端 dispatch | 2026-05-26 commit `1575b68f` 已推 main + US prod gateway image rebuild + OSS probe PASS |
| **A0b** ⏸ 待 | `src/pipeline/process.py` (supports_clone 跟 worker enabled 走) + `src/services/mainland_worker/client_factory.py` (新 `is_worker_enabled_in_env()` helper) | **必须**等 Phase 4.2 D-E（前端 CosyVoiceCloneModal + provider 分流）落地后**同一 PR** 一起合 | 本地 git 工作树保持 modified-未 commit；US prod 已回滚到 backup（CosyVoice tab 暂不显示克隆按钮） |

**为什么这样拆**：
- A0a 是 backend-only 修复，影响范围只在新 CosyVoice clone path（旧 MiniMax 不动）。修了立刻好。
- A0b 改变前端按钮可见性，但**没有**改变按钮 onClick 的目标 endpoint。在 Phase 4.2 D-E 落地之前 land A0b = "曝光一个会误扣 MiniMax 的按钮"，反向伤害用户。所以 A0b 必须**和**前端 dispatch fix 在同一 atomic land。

**生产回滚证据**（2026-05-26 03:30）：

```
US prod /opt/aivideotrans/app/src/pipeline/process.py
  pre-rollback md5 = af4375d95723be92474747ce6b521432 (含 supports_clone runtime gate)
  post-rollback md5 = a7f983c4566f17eabba796a36cf7c1f8 (回到 backup .bak.phase41ui.20260526)

US prod /opt/aivideotrans/app/src/services/mainland_worker/client_factory.py
  pre-rollback md5 = 979ff5d6b29700aa244211b03db29f29 (含 is_worker_enabled_in_env)
  post-rollback md5 = 992a111614065cdf988d395c0df06a80 (回到 backup .bak.phase41ui.20260526)

post-rollback container grep:
  "supports_clone": prov == "minimax"     ← 回到 minimax-only 硬编码
```

---

## §4 后端改动清单（最小化）

### 4.1 `gateway/cosyvoice_clone/api.py`

**改 `sample: UploadFile = File(...)` 为 Optional + 新增 `source_segments` / `source_job_id` 作为可选主输入**：

```python
@router.post("/clone")
async def cosyvoice_clone(
    target_model: str = Form(...),
    speaker_id: str = Form(...),
    speaker_name: str = Form(...),
    consent_voice_clone_confirmed: str = Form(...),
    consent_modal_version: str = Form(...),
    consent_confirmed_at: str = Form(...),
    # 改：原 ``sample: UploadFile = File(...)`` 变 Optional
    sample: UploadFile | None = File(None),
    # 改：原 ``source_segments`` 是可选 hint，现在升级为可选主输入
    source_segments: str | None = Form(None),  # JSON list, e.g. "[3,7,11]"
    source_job_id: str | None = Form(None),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    ...
    # === Layer 0 新增：二选一互斥验证 ===
    if (sample is None) == (source_segments is None):
        raise HTTPException(
            400, detail={
                "code": "invalid_input_mode",
                "message": "必须二选一传 sample（multipart 字节）或 source_segments（任务段号列表，需带 source_job_id）"
            }
        )
    if source_segments is not None and not source_job_id:
        raise HTTPException(400, detail={"code": "source_job_id_required"})
    
    ...
    # Layer 1-3 不变（认证、feature flag、uploader backend）
    ...
    
    # === Layer 4 新增分支：从 job segments 拼字节 ===
    if source_segments is not None:
        sample_bytes = await _assemble_audio_from_job_segments(
            job_id=source_job_id,
            speaker_id=speaker_id,
            segment_ids=json.loads(source_segments),
            user=auth_user,
            db=db,
        )
    else:
        sample_bytes = await sample.read()
    
    # Layer 5: validate_sample_bytes(sample_bytes) —— 不变
    ...
```

**新加 `_assemble_audio_from_job_segments()` helper**（约 80-100 行）：

**所有权 + speaker 边界检查（4 层，全部必过，任一失败 → 400/403）**——Codex v3 review P1.3 收紧。"克隆音色"是声音克隆授权边界，**任一 segment 不属于声明的 (user, job, speaker) 三元组都不能进 worker**，前端筛选不算数：

1. `job_id` 归属 `user` —— 防 A 用户用 B 用户的 job 克隆（与 voice_selection_api.py 的所有权检查复用）
2. **加载 job 的 transcript**（DB / JSON store），构造 `{segment_id → speaker_id}` 映射
3. **每个 segment_id ∈ source_segments**：必须存在于该 transcript（防伪造）+ 必须 `transcript[segment_id].speaker_id == claimed_speaker_id`（防跨 speaker 借声音）
4. 上述任一不过 → `raise HTTPException(403, detail={"code": "segment_ownership_violation", "offending_segment_id": ..., "expected_speaker": ..., "actual_speaker": ...})`，**不打 worker**

通过后：
- 调用现有 `voice_selection_api._concat_segments_ffmpeg()` 或抽公共
- 输出：bytes，格式 WAV PCM16 / 16kHz / mono（CosyVoice 要求）
- 拼接最大时长 cap 60s（hard limit，与 sample_validator 一致）

**为什么前端筛选不算数**：前端只是 UX 便利层。攻击者可以直接 POST `/api/voice/cosyvoice/clone`，绕过前端筛选，传 `source_segments=[B 的某段 id]` + `speaker_id=A` → 用 A 的额度克隆出 B 的声音。后端 speaker 边界校验是唯一防线。

**ffmpeg 输出格式微调**（如果当前 `_concat_segments_ffmpeg` 输出 24kHz，要支持新参数 `target_sample_rate=16000`，给 CosyVoice 路径用 16kHz）。

### 4.2 `gateway/voice_selection_api.py`

**只做一个抽公共**：把 `_concat_segments_ffmpeg()` 提到 `gateway/_audio_assembly.py` 或类似的公共模块。MiniMax 仍调旧签名（24kHz），CosyVoice 调 16kHz 版本。

**关键约束**：MiniMax 的 caller 签名**不变**，参数默认值保留旧行为。

### 4.3 `gateway/cosyvoice_clone/sample_validator.py`

**已经存在**，不需要改（接受 WAV/MP3/M4A、3-60s 时长校验已就位）。

### 4.4 受影响行数估算

| 文件 | LOC delta |
|---|---|
| `gateway/cosyvoice_clone/api.py` | +60-80 |
| `gateway/voice_selection_api.py` | +5-15（抽公共） |
| 新 `gateway/_audio_assembly.py`（如选抽公共） | +80（抽出来） |
| 测试 | +200-300 |
| **后端合计** | ~350-400 行 |

---

## §5 前端改动清单

### 5.1 新建 `frontend-next/src/lib/api/cosyvoiceClone.ts`

新文件，约 80-120 行。函数签名：

```ts
export type CosyVoiceTargetModel = 'cosyvoice-v3.5-flash' | 'cosyvoice-v3.5-plus'

export interface CosyVoiceCloneInput {
  jobId: string
  speakerId: string
  speakerName: string
  segmentIds: number[]
  targetModel: CosyVoiceTargetModel
  consentConfirmedAt: string   // ISO 8601 from Date.toISOString()
}

export interface CosyVoiceCloneResult {
  voiceId: string
  userVoiceId: string
  targetModel: string
  provider: 'cosyvoice_voice_clone'
}

export async function cosyvoiceCloneVoice(
  input: CosyVoiceCloneInput,
): Promise<CosyVoiceCloneResult>
```

实现要点：
- 构造 `FormData`，**source_segments** 字段值为 `JSON.stringify(segmentIds)`
- 不传 `sample` 字段（Option C 路径）
- `consent_voice_clone_confirmed` 必须是字符串 `"true"`（注意：不是 boolean，C.2 review fix #5 已锁死）
- `consent_modal_version` 写死 `"2026-05-25-v1"`（与后端常量同步）
- 错误处理：解析后端 `detail.code`（503 `worker_unavailable` / 400 `sample_too_short` 等）→ 抛带 code 的 typed error

### 5.2 新建 `frontend-next/src/components/workspace/CosyVoiceCloneModal.tsx`

约 350-450 行。结构（参考但不照搬现有 `VoiceCloneModal`）：

```
┌─────────────────────────────────────────────┐
│ 克隆 CosyVoice 音色 — {speakerName}        │
├─────────────────────────────────────────────┤
│ Step 1: 选择样本片段                         │
│   [x] 第 1 段 (3.2s) "Lorem ipsum..."       │
│   [x] 第 3 段 (5.7s) "Dolor sit amet..."    │
│   [ ] 第 7 段 (2.1s) ← 灰，<3s 单独不够     │
│                                              │
│   已选总时长：8.9s  ✓ 在 3-60s 范围内       │
│   建议：10-20s 效果最好                      │
├─────────────────────────────────────────────┤
│ Step 2: 模型选择                             │
│   ● CosyVoice v3.5 Flash（推荐 — 快、便宜）│
│   ○ CosyVoice v3.5 Plus（高质量、较慢）    │
│   [▼ 高级选项]                              │
├─────────────────────────────────────────────┤
│ Step 3: 授权（必填）                         │
│   ⚠️ [点击查看完整授权说明]                  │
│   [_] 我已阅读并同意上述授权条款            │
├─────────────────────────────────────────────┤
│   预估费用：{voice_clone_cost_credits} 点    │
│                                              │
│   [取消]              [开始克隆 ←disabled]   │
└─────────────────────────────────────────────┘
```

关键点：
- **样本前端校验**：从 `getSpeakerAudioSegments()` 拿段时长，前端就过滤 / 警示 `<3s` 单独不可用、总时长 `<3s 或 >60s` disabled 提交按钮
- **target_model 默认 flash**，plus 在折叠面板里
- **consent checkbox 默认 false**，必须勾选才解锁提交（与后端 `consent_voice_clone_confirmed="true"` 字符串严校验对齐）
- 点"点击查看完整授权说明"打开 `CosyVoiceConsentModal`
- 提交时调 `cosyvoiceCloneVoice()` → 成功后 `onSuccess({ voiceId, userVoiceId })`，外层 panel state 写入 user voices state map

### 5.3 新建 `frontend-next/src/components/workspace/CosyVoiceConsentModal.tsx`

约 80-150 行。一个 modal 显示授权文案 v1 全文（从 [Phase 4 plan §授权文案](2026-05-24-cosyvoice-phase4-go-live-plan.md#授权文案) 拷贝），底部一个"我已阅读"按钮。点击后回到主 clone modal 自动勾上 consent checkbox。

### 5.4 修改 `frontend-next/src/components/workspace/VoiceSelectionPanel.tsx`

最小侵入。改两处：

**改 1：clone 按钮 onClick 分流**（约 994-1003 行）：

```tsx
// before
{showClone ? (
  <button onClick={() => setCloneModalSpeaker(sp.speakerId)}>
    {state?.isCloning ? '克隆中...' : '克隆音色'}
  </button>
) : null}

// after
{showClone ? (
  <button onClick={() => {
    if (currentProvider === 'cosyvoice') {
      setCosyvoiceCloneModalSpeaker(sp.speakerId)
    } else {
      setCloneModalSpeaker(sp.speakerId)  // MiniMax 路径不动
    }
  }}>
    {state?.isCloning ? '克隆中...' : '克隆音色'}
  </button>
) : null}
```

**改 2：新增 modal state + 渲染**（在文件底部 ~1590 行附近）：

```tsx
const [cosyvoiceCloneModalSpeaker, setCosyvoiceCloneModalSpeaker] = useState<string | null>(null)

// 既有 VoiceCloneModal 渲染（不动）
{cloneModalSpeaker && <VoiceCloneModal ... />}

// 新增 CosyVoiceCloneModal 渲染
{cosyvoiceCloneModalSpeaker && (
  <CosyVoiceCloneModal
    speakerId={cosyvoiceCloneModalSpeaker}
    speakerName={...}
    jobId={jobId}
    segments={...}
    onClose={() => setCosyvoiceCloneModalSpeaker(null)}
    onSuccess={(result) => {
      // 写入 voiceStates，自动选中刚克隆的音色
      setVoiceStates((prev) => ({
        ...prev,
        [cosyvoiceCloneModalSpeaker]: {
          ...prev[cosyvoiceCloneModalSpeaker],
          voiceId: result.voiceId,
          isCloning: false,
        },
      }))
      // 刷新 user voices 列表
      refetchUserVoices()
      setCosyvoiceCloneModalSpeaker(null)
    }}
  />
)}
```

**改 3：dropdown 添加 2 个新 grouping**（约 953-968 行附近，v4 根据 §12/§3.5 细化）：

在现有 dropdown 的 `optgroup` 前面**按顺序**加 2 组：

```tsx
{/* 1. 本任务临时克隆（is_temporary=TRUE 且 source_job_id == jobId） */}
{currentProvider === 'cosyvoice' && cosyvoiceJobTempVoices.length > 0 ? (
  <optgroup label={`本任务临时克隆 (${cosyvoiceJobTempVoices.length})`}>
    {cosyvoiceJobTempVoices.map((v) => (
      <option key={v.voiceId} value={v.voiceId}>
        {v.label} · 临时·剩余 {daysUntil(v.expiresAt)} 天
      </option>
    ))}
  </optgroup>
) : null}

{/* 2. 长期保存的克隆（is_temporary=FALSE） */}
{currentProvider === 'cosyvoice' && cosyvoiceSavedVoices.length > 0 ? (
  <optgroup label={`我的 CosyVoice 克隆音色 (${cosyvoiceSavedVoices.length})`}>
    {cosyvoiceSavedVoices.map((v) => (
      <option key={v.voiceId} value={v.voiceId}>{v.label}</option>
    ))}
  </optgroup>
) : null}

{/* 现有官方预设 grouping 不动 */}
```

两个集合的过滤逻辑：
- `cosyvoiceJobTempVoices`：`getUserVoices()` filter `v.provider === 'cosyvoice_voice_clone' && v.isTemporary === true && v.sourceJobId === jobId`
- `cosyvoiceSavedVoices`：`getUserVoices()` filter `v.provider === 'cosyvoice_voice_clone' && v.isTemporary === false`

⚠️ **关键约束**：临时音色**不能**跨任务展示——Task A 的临时音色在 Task B dropdown 里**看不到**（避免用户跨任务误用 + 过期被清引起困惑）。

### 5.5 修改 `frontend-next/src/lib/api/voiceSelection.ts`

**只动 1 处**：`UserVoiceEntry.provider` 字段已经存在，前端代码现在已经能区分。不需要改 type definitions。

### 5.6 受影响行数估算

| 文件 | LOC delta |
|---|---|
| `frontend-next/.../cosyvoiceClone.ts`（新） | +100 |
| `frontend-next/.../CosyVoiceCloneModal.tsx`（新） | +400 |
| `frontend-next/.../CosyVoiceConsentModal.tsx`（新） | +120 |
| `frontend-next/.../VoiceSelectionPanel.tsx`（改） | +50 |
| 单元/集成测试 | +200-300 |
| **前端合计** | ~870-970 行 |

---

## §6 守卫测试清单（关键）

新增 **5 类 9 条以上** 守卫测试。

### 6.1 MiniMax 路径不被触碰（AST 守卫）

- **G6.1.1**：[`tests/test_phase42_minimax_unchanged_guard.py`] AST 扫 `gateway/voice_selection_api.py` —— 验证 `voice_clone_for_selection` 函数签名、`_concat_segments_ffmpeg` 签名（如果抽公共了允许新签名扩展，但旧签名兼容）字节级相同
- **G6.1.2**：扫 `frontend-next/.../voiceSelection.ts::cloneVoiceForSelection` —— 确认仍调 `/jobs/${jobId}/voice-clone`，未改成 CosyVoice endpoint
- **G6.1.3**：扫 `frontend-next/.../VoiceSelectionPanel.tsx::VoiceCloneModal` —— 字符串扫不含 `cosyvoice` 字面量

#### Codex 2026-05-26 新增：跨向 endpoint 互斥守卫

- **G6.1.4**（**hard guard**）：grep + AST 扫 `frontend-next/src/components/workspace/CosyVoiceCloneModal.tsx` 和 `frontend-next/src/lib/api/cosyvoiceClone.ts` —— **绝不**出现以下字符串字面量：
  - `/jobs/`（旧 MiniMax endpoint 路径片段）
  - `voice-clone`（旧 endpoint 后缀）
  - 函数名 `cloneVoiceForSelection`
  
  失败示例（必须 red）：CosyVoice modal 不小心 import 了 `cloneVoiceForSelection`。
  
- **G6.1.5**（**反向 hard guard**）：扫 `VoiceCloneModal`（旧 MiniMax）模块 + `cloneVoiceForSelection` 函数 —— **绝不**出现：
  - `cosyvoice` 字面量
  - `/api/voice/cosyvoice/`
  - 函数名 `cosyvoiceCloneVoice`
  
  失败示例：MiniMax 模块不小心 import 了 cosyvoice client。

### 6.2 Smart 自动 clone 不被触碰（AST 守卫）

- **G6.2.1**：扫 `src/pipeline/process.py:3640-4100` 区间 AST —— Smart 自动 clone 触发逻辑节点字节级相同
- **G6.2.2**：扫 `_register_smart_clone_in_user_voices` 函数体 —— 确认仍 POST `/api/internal/user-voices/register-smart`，未改 provider 字段默认

### 6.3 CosyVoice endpoint 二选一互斥 + speaker 边界（单元）

- **G6.3.1**：传 `sample` + `source_segments` 都给 → 400 `invalid_input_mode`
- **G6.3.2**：都不给 → 400 `invalid_input_mode`
- **G6.3.3**：只给 `source_segments` 不给 `source_job_id` → 400 `source_job_id_required`
- **G6.3.4**：`source_job_id` 不属于当前 user → 403 `job_ownership_violation`

#### Codex v3 P1.3 新增：speaker 边界（关键防越权）

- **G6.3.5**：构造 transcript `seg1.speaker_id=A, seg2.speaker_id=B`；请求传 `speaker_id=A, source_segments=[1, 2]` → 后端必须 **403** `segment_ownership_violation`，response detail 含 `offending_segment_id=2`，**不打 worker**，**不上传 OSS**
- **G6.3.6**：变体——`speaker_id=A, source_segments=[2]`（单段越界） → 403
- **G6.3.7**：变体——`source_segments=[9999]`（id 不在 transcript） → 403 `segment_not_found`
- **G6.3.8**：合规 case——`speaker_id=A, source_segments=[1]` 且 `seg1.speaker_id=A` → 200 + worker 收到 audio

### 6.4 Provider 分流前端测试（Vitest / Playwright）

- **G6.4.1**：点击"克隆音色"，`state.selectedProvider === 'cosyvoice'` → CosyVoiceCloneModal 出现，VoiceCloneModal 不出现
- **G6.4.2**：相反场景，MiniMax 时只出旧 modal
- **G6.4.3**：CosyVoiceCloneModal consent checkbox 未勾时提交按钮 disabled
- **G6.4.4**：CosyVoiceCloneModal target_model 默认 `cosyvoice-v3.5-flash`

### 6.5 端到端契约（integration）

- **G6.5.1**：CosyVoiceCloneModal → mock fetch 验证 multipart body 含 `consent_voice_clone_confirmed=true`（字面字符串）+ `consent_modal_version=2026-05-25-v1` + `source_segments=[...]` + `source_job_id=...`
- **G6.5.2**：CosyVoice clone 200 响应 → 触发 `onSuccess`，外层 user_voices state 增加一条 `provider="cosyvoice_voice_clone"` 行

#### Codex 2026-05-26 新增：URL 互斥契约

- **G6.5.3**（**契约级**，Codex v3 编号修正，原 G6.4.5）：Vitest mock fetch，记录所有发出的 HTTP request 的 URL：
  - 场景 A：CosyVoiceCloneModal 触发的 clone 流，**所有** request URL 必须**只包含** `/api/voice/cosyvoice/clone`，**不包含** `/jobs/` 或 `voice-clone` 任一 substring
  - 场景 B：VoiceCloneModal（MiniMax）触发的 clone 流，**所有** request URL 必须**只包含** `/jobs/${jobId}/voice-clone`，**不包含** `/api/voice/cosyvoice/` substring
  - 任一 substring 越界 → 测试 fail
  
  **这是本 plan 最关键的回归守卫**——直接挡住 2026-05-26 那种"按钮显示但 dispatch 走错"的事故复发。

### 6.6 文档保持同步守卫（轻量）

- **G6.6.1**：Phase 4 go-live plan 加 "see Phase 4.2 plan" 引用
- **G6.6.2**：本 plan 的 §11 执行记录与实际 commit 一致

---

## §7 实施分阶段（A0-F）

| Phase | 内容 | 估时 | 关键产物 | 状态 |
|---|---|---|---|---|
| **A0a** ✅ | sample_uploader OSS 修复独立 commit + push main + US prod gateway image rebuild | 0.5h | commit `1575b68f`、US prod 已部署、OSS probe PASS | 已完成 2026-05-26 |
| **A0b** ⏸ | process.py supports_clone + client_factory helper —— **必须**与 D-E 同 PR | - | 本地 modified 未 commit | 等 D-E |
| **A** | 后端 `source_segments` 主输入模式：endpoint 改 + helper + 抽公共 ffmpeg | 4-6h | gateway PR #N | 待启动 |
| **B** | 守卫测试 G6.1 / G6.2 / G6.3 / **G6.1.4 / G6.1.5**（必须先于 frontend 代码改动！） | 3-4h | test PR #N | 待启动 |
| **C** | 前端 `cosyvoiceClone.ts` API client + 单元测试 | 2-3h | frontend PR #M-1 | 待启动 |
| **D** | 前端 `CosyVoiceCloneModal.tsx` + `CosyVoiceConsentModal.tsx` + 单元测试 | 6-8h | frontend PR #M-2 | 待启动 |
| **E** | VoiceSelectionPanel.tsx 集成 + provider 分流 wiring + 集成测试 G6.4 / G6.5 / **G6.5.3** + **A0b 在同 PR 里 commit 上去** | 3-4h | frontend PR #M-3 | 待启动 |
| **F** | 部署到 US prod（gateway rebuild image + frontend rebuild + **admin_setting `cosyvoice_clone_general_availability_enabled` 保持 false = admin-only** + admin 真烟测）→ 烟测通过后 admin 后台**翻 `cosyvoice_clone_general_availability_enabled = true` = 开放全用户**（运行时翻，不需 container restart） | 2-3h | release + Stage 1 烟测报告 | 待启动 |

总估时：**21-31 工时**，约 3 工作日。

**关键依赖顺序**：
- A0 → A，A0 → B，A0 → C
- A + B → D
- D → E
- E → F

**B 必须先于 D-E** —— 守卫测试先就位，再写新代码，避免新代码无意中改动 MiniMax 路径却被 review 漏掉。

---

## §8 Rollout & Rollback

### 8.1 灰度策略（Codex 2026-05-26 v2 收紧 —— 后端强约束 + 前端展示层）

**两段灰度**——避免"一键全开放"翻车。

**架构原则**（Codex v3 review 关键修正）：

- ⚠️ **env 不是运行时可翻的灰度开关** —— 改 env 必须 container recreate（不可热翻），不适合做"出问题立刻拨回"的紧急 gating
- ⚠️ **前端 gating 不是安全边界** —— `currentUser.role === 'admin'` 只能控按钮可见性，**绕过前端直接 POST endpoint 仍能调用**。把 admin-only 当安全要求时必须后端强约束

**正确做法**：

| 层 | 角色 | 实现 |
|---|---|---|
| **后端 admin_settings**（**唯一真源**） | Stage 1 / 2 灰度开关 | 新增 setting `cosyvoice_clone_general_availability_enabled`（默认 false）→ admin 后台运行时可翻 |
| **后端 endpoint 强约束**（**安全边界**） | Stage 1 时拒绝非 admin 调用 | `cosyvoice_clone()` Layer 0.5 检查：`if not settings.cosyvoice_clone_general_availability_enabled and not user.is_admin and user.id not in clone_beta_user_allowlist: raise 403` |
| **前端**（**展示层，不是安全层**） | UI 不让 普通用户看到入口 | 从 admin_settings API 拿 `cosyvoice_clone_general_availability_enabled`；为 false 且当前用户非 admin → 不渲染按钮；轮询/SSE 同步 |

#### Stage 1: admin-only

A0b + Phase 4.2 D-E 落地后，**第一次部署到 US prod 必须保持 admin-only**：

- 部署后 admin 后台 `cosyvoice_clone_general_availability_enabled` 保持 **false** 默认
- **后端 endpoint 强约束**生效：普通用户（含 API 直接 POST）一律 403 `cosyvoice_clone_beta_admin_only`
- 前端：admin 自己看得到按钮；普通用户不渲染（admin_settings 拉 false）
- **Admin 自己（用户本人）跑 1 次真烟测**：选样本 → 克隆 → 验证武汉 worker audit JSONL 出现 `provider=cosyvoice_voice_clone` 条目 + DB `user_voices` provider 列正确 + DashScope 后台扣费正确

#### Stage 2: 全用户开放

- **烟测通过后**（不是部署完，是真烟测通过），admin 后台**翻** `cosyvoice_clone_general_availability_enabled = true`
  - 这是**真正运行时翻**（不需 container restart），admin_settings 读 DB，立刻生效
  - endpoint Layer 0.5 检查跳过 admin / allowlist 限制 → 全用户可调
  - 前端轮询拉到 true → 全用户能看到按钮
- worker 未启用 → 前端 `supports_clone=false` → CosyVoice tab 下"克隆音色"按钮不显示（A0b 落地后）
- worker 启用但 OSS 未配 → endpoint 503 → 前端展示"克隆服务暂时不可用，请联系管理员"
- 全部配齐 → 用户可点击克隆

**为什么要 admin-only Stage 1**：2026-05-26 事故证明"上线即开放"的风险——按钮一显示，普通用户就会点击，错路径会**立刻**扣 MiniMax。Admin-only 给我们 1-2 天 production traffic 验证窗口，烟测出问题只影响 admin（=用户本人），不影响付费用户。

**回滚紧急路径**：Stage 2 上线后发现严重 bug → admin 后台翻回 `cosyvoice_clone_general_availability_enabled = false` → 普通用户立即 endpoint 403，前端按钮消失。**这是运行时翻**，不需任何 container restart 或 env 改动。比 §8.2 level 2 `compose up --force-recreate` 快几个数量级。

### 8.2 Rollback 紧急步骤

如果发现问题需要紧急关闭 CosyVoice 克隆：

**level 1**（最快，零代码 deploy）：在 admin 后台翻 `cosyvoice_clone_general_availability_enabled = false`（详见 §8.1）→ 普通用户立刻 endpoint 403、前端按钮自动隐藏（前端轮询 admin_settings）→ admin 仍可继续访问做排障

**level 2**（关整条 worker 通道）：US prod `.env` 改 `AVT_MAINLAND_VOICE_WORKER_ENABLED=false` → **必须 recreate gateway 和 app 两个容器**（env 同时影响 gateway endpoint Layer 1 fail-closed 和 app pipeline 的 `supports_clone` 输出，单 recreate 一边会造成"按钮消失但 endpoint 仍可调"或反之的不一致）：

```bash
cd /opt/aivideotrans
docker compose --env-file /opt/aivideotrans/config/.env up -d \
  --no-deps --force-recreate gateway app
```

→ 前端按钮消失 + endpoint 503 + pipeline 子进程的 `is_worker_enabled_in_env()` 返 False 双侧一致

**level 3**（彻底）：revert frontend commit + revert backend api.py commit，回到 Phase 4.1 完成状态

### 8.3 监控信号

部署后第一周关注：
- 武汉 worker audit JSONL 每天 `operation=clone` 条数（应 ≥ 用户实际点击次数）
- DB `user_voices` provider 分布：`cosyvoice_voice_clone` 行 / `minimax_voice_clone` 行
- 错误率：`/api/voice/cosyvoice/clone` 4xx / 5xx 比例
- OSS bucket 每日对象数（应等于"克隆次数 × 1"，因为成功 clone 完会删除）

---

## §9 用户决策 / Resolved Decisions（v4 拍板）

原 v1-v3 §9 列了 5 个 open questions，**2026-05-26 全部拍板**：

1. ✅ **target_model 默认值**：工作台默认 `cosyvoice-v3.5-flash`；`cosyvoice-v3.5-plus` **作为高级选项开放**（modal 顶部"模型选择"区域，默认折叠"高级"展开显示 plus，用户可主动 A/B）。两者价格策略见 #5。
2. ✅ **克隆音色 grouping 命名**：「**我的 CosyVoice 克隆音色**」——不带 emoji，与 MiniMax tab 现有「我的克隆音色」措辞风格对齐，便于多 provider 并列时用户认知统一。
3. ✅ **consent modal 法律文案**：Stage 1 admin-only 灰度期间**沿用 Phase 4.1 plan §授权文案 v1 现成文案**；Stage 2 全用户开放**之前必须**做一次律师 review，律师改动单独跑（不阻塞 Phase 4.2 主体实施）。
4. ✅ **Smart / 快捷版自动克隆**：**不进入 Phase 4.2 实施范围**。Smart 版当前自动 clone 仍走 MiniMax 不动。CosyVoice 自动克隆方向落到 **Phase 4.3**（详见 §13 新增）。
5. ✅ **价格策略**：Phase 4.2 **沿用现有 `voice_clone_cost_credits`** 字段（避免引入价格分级复杂度），但 user_voices 行**必须记录 `provider` + `target_model`**（DB schema 已有），方便后续按 provider/model 分级定价时 backfill。

新增决策（v4 补，本次产品讨论沉淀）：

6. ✅ **克隆音色保存策略**：默认**任务级临时音色**，**不**自动写进"我的音色库"长期保留。详见 §12 新增"克隆音色保存策略"。

---

## §10 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| MiniMax 路径被新代码无意破坏 | M | High | §6.1 AST 守卫 + B 阶段先于 D-E |
| Smart 自动 clone 被无意影响 | L | Critical | §6.2 守卫 + 不动 process.py:3640-4100 区间 |
| ffmpeg 输出 16kHz 与 24kHz 不匹配 | M | M | A 阶段加参数化，MiniMax 默认仍 24kHz |
| consent modal 用户体验差（"又一个弹窗"） | M | M | 一次性确认后 24h 内同 job 不重弹（state 缓存） |
| 多人并发克隆 OSS 上传冲突 | L | L | OSS key 含 sha256 hash + uuid，本质不会冲突 |
| WG 隧道断 → 克隆超时 | L | M | gateway 已有 WorkerNetworkError 处理 → 503 |
| 前端用户重复点击 | M | L | modal 提交按钮 onClick 后立即 disable |
| `source_segments` JSON parse 失败 | L | L | 后端 try/except → 400 `source_segments_invalid_json` |

---

## §12 克隆音色保存策略（v4 新增 — 产品决策）

CosyVoice 克隆音色**默认不进个人音色库长期保留**。理由：用户绝大多数克隆只为单次任务用一次，DashScope 端长期保留每条克隆 voice 会浪费 quota（CosyVoice 账户有最大克隆音色数限制），同时用户音色库会被一次性使用的临时音色塞满，可用性差。

### 12.1 两种保存层级

| 层级 | 触发条件 | DB 行为 | UI 行为 | DashScope 端 | TTL |
|---|---|---|---|---|---|
| **任务级临时音色**（默认） | 用户在 CosyVoiceCloneModal 完成克隆，不勾"保存到我的音色库" | `user_voices` 写入一行，`is_temporary=true`，`expires_at=now+7d` | 当前任务的音色选择 dropdown 显示，**带"临时"badge**；**不**显示在长期"我的 CosyVoice 克隆音色"分组 | 保留 voice_id 直到 expires_at；过期后由清理任务删 | 7 天 |
| **长期保留音色** | 用户在 modal 勾"保存到我的音色库"（默认 unchecked） | `user_voices` 写入一行，`is_temporary=false`，`expires_at=NULL` | 立即出现在长期"我的 CosyVoice 克隆音色"分组，跨任务可见 | 永久保留（直到用户手动从音色库删） | 无限 |

### 12.2 为什么 7 天

- 覆盖任务**重试**（用户对结果不满意 → 失败重试，1-2 天）
- 覆盖**后编辑 regenerate**（修改阶段重生成单段 / 全量 TTS，7 天足够用户完成编辑闭环）
- 覆盖**用户短期返工**（看完导出觉得某段配音不对 → 回来再 regenerate）
- 不覆盖**长期保留**——超过 7 天还想用这个声音 = 用户应主动保存

### 12.3 schema 影响

Phase 4.1 已落 `user_voices` 表，本节决策**需新增 2 列**：

```sql
ALTER TABLE user_voices
  ADD COLUMN is_temporary BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN expires_at TIMESTAMP WITH TIME ZONE NULL;

CREATE INDEX idx_user_voices_expires_at
  ON user_voices (expires_at)
  WHERE expires_at IS NOT NULL;  -- partial index, only for non-NULL
```

写到 Phase 4.2 Phase A 后端实施清单。Phase 4.1 已落 voice 行默认 `is_temporary=FALSE` 不影响向后兼容（旧 MiniMax 长期音色行为不变）。

### 12.4 过期清理任务

新增 daily cron / scheduled task：
- 扫 `user_voices WHERE expires_at < now() AND is_temporary = TRUE`
- 调用 DashScope `delete_voice(voice_id)` 删远端
- 删本地 `user_voices` 行（或 soft-delete，加 `deleted_at` 列保留审计）
- 失败重试 3 次 → 失败转人工 admin 通知

**关键约束**：清理任务不能误删 `is_temporary=FALSE` 的长期音色，也不能删 MiniMax 路径的 voice（provider != `cosyvoice_voice_clone` 跳过）。

### 12.5 用户体验细节

- **克隆完成提示**：
  - 临时（默认）："✅ 克隆成功 — 此音色为**任务级临时音色**，7 天后自动清理。如需长期保留，[在我的音色库手动保存]"
  - 长期："✅ 克隆成功 — 已保存到我的音色库"
- **过期后用户在另一个任务想用这个音色**：dropdown 不显示（已删）；用户记得克隆过 → 提示"该音色已过期，请重新克隆"，**绝不静默重克隆**（CLAUDE.md 付费 API 硬约束）
- **regenerate 跨过 expires_at 边界**：clean task 删 voice 前，**先扫**当天有 `regenerate_tts` event 引用该 voice_id 的 job，**延 expires_at 7 天**。避免用户编辑中途音色被清

### 12.6 影响 Phase 4.2 实施范围

新增的工作量：

| 项 | LOC delta | 估时 |
|---|---|---|
| schema migration（2 列 + partial index） | +30 | 0.5h |
| user_voice_service.add_user_voice() 加 is_temporary 参数 | +15 | 0.5h |
| CosyVoiceCloneModal 加"保存到我的音色库"checkbox | +25 | 0.5h |
| cosyvoiceCloneVoice() API client 加 `save_to_library` 参数 | +5 | 10min |
| cosyvoice_clone endpoint 接 `save_to_library` form field | +20 | 0.5h |
| dropdown 显示"临时"badge | +30 | 0.5h |
| 过期清理 scheduled task（新模块 `gateway/cosyvoice_clone/expiry_sweeper.py`） | +120 | 2-3h |
| expires_at 延期 hook（regenerate event listener） | +60 | 1-2h |
| 守卫测试（temp 标记、过期不删长期、跨 provider 不误删、regenerate 续命） | +200 | 3-4h |
| **§12 合计** | ~505 | ~10-12h |

**注意**：§12 实施工作量比 v3 §7 估算多约 1 工作日。Phase 4.2 总估时上调 21-31h → **31-43 工时（约 4-5 工作日）**。

---

## §13 Phase 4.3 方向 — 快捷版自动克隆（**不进入 Phase 4.2 实施**）

本节**只是方向声明**，**不属于 Phase 4.2 落地范围**。目的是在 Phase 4.2 文档里钉死后续产品决策，避免实施时漂移。

### 13.1 快捷版（Smart 自动）当前与未来

| 维度 | 当前（Phase 4.1 结束态） | Phase 4.3 目标 |
|---|---|---|
| Provider | MiniMax 唯一（`smart_inline_auto_approve` 路径只走 MiniMax）| **CosyVoice 加入**作为快捷版默认 |
| 默认模型 | MiniMax voice clone v1 | `cosyvoice-v3.5-flash`（成本 / 速度优势） |
| 用户授权 | 任务级 `smart_consent.auto_voice_clone == True`（CLAUDE.md 硬约束）| 不变 —— **必须显式任务级勾选**，绝不静默克隆 |
| 保存策略 | 自动写 user_voices `is_temporary=false`（旧逻辑） | 改为 **`is_temporary=true` + 7d TTL**（与 §12 工作台默认对齐） |
| 多说话人 | 逐说话人 sequential 克隆 | 不变，可并行优化是 Phase 4.4 |
| 失败 fallback | hard fail（CLAUDE.md 禁止静默 fallback 到其它付费 API） | 不变 |

### 13.2 Phase 4.3 关键决策点（等启动时再讨论）

1. Smart 版界面如何呈现"任务级 CosyVoice 自动克隆"的开关 UI（一个 checkbox vs 高级折叠）
2. MiniMax / CosyVoice 二选一时如何引导（默认哪个？admin 后台配置 default provider？）
3. 快捷版自动克隆失败时，是否提示用户切手动 review 路径
4. 快捷版自动克隆的成本预览（DashScope CosyVoice flash 单价 vs MiniMax）

### 13.3 为什么不进 Phase 4.2

- Phase 4.2 范围**严格限定为工作台版手动 review 路径**的前端 dispatch wiring
- 快捷版自动克隆涉及：
  - `src/pipeline/process.py:3640-4100` Smart 触发逻辑改造（**Phase 4.2 守卫 G6.2 要求字节级不变**）
  - 不同 consent contract（Smart auto vs Studio manual）
  - 不同 schema migration（快捷版可能需要批量克隆的 batch_id 列）
- Phase 4.2 范围放纵会导致 plan 膨胀 + 实施周期翻倍，违背增量交付原则

---

## §11 执行记录（落地时填）

> 按 Phase 4 plan 风格 append。

### A0a — 已完成 (2026-05-26)
- [x] commit `1575b68f`：`fix(cosyvoice): drop ResponseContentType override in presigned URL`
- [x] push origin main
- [x] US prod `/opt/aivideotrans/app/gateway/cosyvoice_clone/sample_uploader.py` 同步（SCP 推 + md5 `0ef7ff548f91c2b3b823d86f38af459b` 双侧一致）
- [x] US prod `docker compose --env-file /opt/aivideotrans/config/.env up -d --build gateway` 成功
- [x] container md5 与本地一致；OSS PUT/GET/DELETE probe PASS（presign URL 无 `response-content-type`，GET 200 + sha256 完整 + 删后 404）

### A0b — pending (与 D-E 合并提交)
- [ ] commit process.py + client_factory.py（保持本地 modified 状态）

### Codex 2026-05-26 v2 收紧动作
- [x] 回滚 US prod 上 process.py + client_factory.py 到 `.bak.phase41ui.20260526`（消除"按钮显示但 dispatch 走错"风险）
- [x] plan §3.6 改为 A0a/A0b 拆分版本
- [x] plan §6.1 新增 G6.1.4 / G6.1.5（endpoint 互斥 hard guard）
- [x] plan §6.5 新增 G6.4.5（URL 互斥契约测试，后 v3 重命名 G6.5.3）
- [x] plan §8.1 改为两段灰度（admin-only Stage 1 → 全用户 Stage 2）

### Codex 2026-05-26 v3 收紧动作（7 条 review fix）
- [x] **P1.1**：§8.1 admin-only 改为**后端 admin_setting 强约束 + endpoint Layer 0.5 403** 为安全边界；env 不再做灰度开关；前端只是展示层
- [x] **P1.2**：§8.2 rollback level 2 必须 recreate `gateway` **和** `app` 两个容器（env 同时影响 gateway endpoint 和 app pipeline）
- [x] **P1.3**：§4.1 `_assemble_audio_from_job_segments` 加 4 层 ownership 检查（user / job / speaker / segment 全验，前端筛选不算数）+ §6.3 加 G6.3.5-G6.3.8 守卫测试
- [x] **P1.4**：§0.2 表格"Job 上下文"行拆 Phase 4.1 vs Phase 4.2 两行，消除"hint vs 主输入"语义矛盾
- [x] **P2.5**：§2.2 sample_uploader 行删"仍未 commit"，改"A0a 已永久化 1575b68f"
- [x] **P2.6**：§1.3 DoD #5 改"前端禁用提交 + 后端 400 fail-closed 不打 worker"
- [x] **P2.7**：G6.4.5 改名 G6.5.3，归类正确

### Codex 2026-05-26 v3 复核 stale 残留修复（2 条）
- [x] **§7 表 E 行**：旧编号 `G6.4.5` → 同步改为 `G6.5.3`
- [x] **§7 表 F 行**：admin-only flag 语义反向修正。原写"开 true → admin-only，验证后 false → 开放"，与 §8.1 新设计相反。改为：`cosyvoice_clone_general_availability_enabled`，**false = admin-only**（部署默认 + Stage 1），admin 真烟测通过后 admin 后台**翻 true = 全用户开放**（运行时翻，不需 container restart）

### v4 产品决策落档（2026-05-26 用户拍板 + Codex 建议拆 Phase 4.3）

- [x] **§9**：5 个 open questions 全部 resolved（含 v4 新增第 6 条保存策略）
- [x] **§12 新增**：克隆音色保存策略（任务级临时 7d 默认 + 用户主动保存进长期音色库；schema 加 `is_temporary` + `expires_at` 列 + partial index；过期清理 scheduled task；regenerate 延 TTL；用户体验细节）
- [x] **§13 新增**：Phase 4.3 快捷版自动克隆方向声明（不进入 Phase 4.2 实施范围，避免 scope 膨胀）
- [x] **§1.3 DoD**：从 6 条扩到 8 条，加临时音色默认、临时不跨任务、过期不静默重克隆
- [x] **§3.5 决策 E 细化**：dropdown 拆「本任务临时克隆」+「我的 CosyVoice 克隆音色」两个 grouping，去 emoji 与 MiniMax 风格对齐
- [x] **§5.4 dropdown 实现代码**：v3 单 grouping → v4 双 grouping + 临时音色限本任务过滤
- [x] **§7 总估时上调**：21-31h → 31-43h（§12 增加 ~10-12h，主要在清理 task + schema + 过期续命）

### A 后端
- [ ] endpoint 改造
- [ ] helper 抽出
- [ ] 单元测试

### B 守卫
- [ ] G6.1 / G6.2 / G6.3

### C 前端 API client
- [ ] cosyvoiceClone.ts

### D 前端 modals
- [ ] CosyVoiceCloneModal
- [ ] CosyVoiceConsentModal

### E 集成
- [ ] VoiceSelectionPanel 分流
- [ ] dropdown grouping
- [ ] G6.4 / G6.5

### F 部署
- [ ] gateway image rebuild
- [ ] frontend image rebuild
- [ ] US prod deploy
- [ ] 真烟测（用户主动点击克隆）

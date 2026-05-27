# Phase 4.2 E.2 — `source_segments` 选段 UI 实施 Spec

**作者：** Claude Code
**版本：** v2.2（2026-05-27 Codex PR #16 P2 修：ms 精度边界）
**日期：** 2026-05-27
**前置：** E.1 已合并（PR #15，commit `d716ad1e`）file-upload 通路 + A0b `supports_clone` 恢复
**后置：** F 阶段 — admin-only 灰度 stage 1 + GA flip + 部署烟测

---

## §0 v2 决策（Codex 2026-05-27 复审锁定）

v1 spec 留了 5 个 open question，Codex 审完要求开工前全部拍定。本节是**优先级最高的决策记录**，与下面正文冲突时以本节为准；§4–§7 已按本节同步改写。

1. **VoiceModifyTab 在 E.2 只保留 file upload 路径**，不接 source_segments picker。
   - 理由：editing 阶段使用 baseline transcript 段（音频本身不变）虽然技术上可行，但 UI 上让用户在"编辑后的任务"里看到"原始转写段落"会引起认知混淆——用户会问"这是我刚改的段还是改之前的段？"。保守做法：editing 路径继续只走文件上传，等到有 edit-aware segment endpoint（F 之后专门方案）再放开。
   - 实施：`VoiceModifyTab.tsx` 调用 `<CosyVoiceCloneModal>` 时**显式不传** `defaultSourceJobId`，让 modal 直接 falback 到 file-only（D.2 二轮 P2 fix 已保证：无 `defaultSourceJobId` 时根本不出现 segments radio）。

2. **客户端总时长阈值固定 3000 – 60000 毫秒**（与后端 `gateway/cosyvoice_clone/sample_validator.py` 的 `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` **同单位**严格匹配）。
   - **v2.2 关键决策**（Codex PR #16 P2 fix）：前端在内部计算 + 校验 **全部用毫秒**，**不**用 `SpeakerAudioSegment.durationS`（端点返回的 duration_s 是一位小数 round 值——例如真实 2.96s 会显示为 3.0s，前端放行后被后端 ms 精度拒收，是边界 case bug）。
   - **聚合源**：`totalSelectedMs = sum(Math.max(0, seg.endMs - seg.startMs))`——`endMs - startMs` 是毫秒级精确差值，与后端 ms 校验完全同精度。
   - 已选段总时长 `< 3000ms` → 红色提示"还需 X.X 秒"（展示层除 1000 toFixed(1)），**禁用**提交按钮
   - 已选段总时长 `> 60000ms` → 红色提示"超出 X.X 秒"，**禁用**提交按钮
   - **UI 展示**可以继续 `totalSelectedMs / 1000`.toFixed(1) 展示成秒；**校验不读展示值**。
   - **守卫 cross-check（v2.2 改写）**：
     - 测试扫前端 modal 含字面量 `selectedDurationMs < 3000` 和 `selectedDurationMs > 60000`
     - 测试扫 picker 含 `endMs - startMs` 模式
     - 测试扫 picker 暴露 `onSelectedDurationMsChange: (ms: number) => void` prop
     - 测试扫后端 `sample_validator.py` 含 `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000`
     - 后端无需改；后端本来就是 ms 单位

3. **`CosyVoiceCloneModal.tsx` 的 `sourceSegmentIds?: number[]` prop 保留**。
   - D.2 已经把这个 prop 作为 modal 公开契约锁定（含强类型 + placeholder 拒绝守卫），E.2 **不**移除。
   - Modal 行为：
     - `sourceSegmentIds` 作为**外部注入的初始值**：modal 打开时把 prop 值拷进内部 `selectedSegmentIds` state
     - Picker 改的是**内部** `selectedSegmentIds`
     - 提交时始终传**内部** `selectedSegmentIds`，不读 prop
   - 这样 D.2 的 modal 接口契约不破，同时 picker 选择自由。

4. **跨 speaker 防护多一道前端子集校验，picker → modal 回传契约写死**。
   - **Picker 必须声明 prop**：

     ```ts
     onAvailableSegmentIdsChange: (ids: number[]) => void
     ```

   - Picker 加载完 `getSpeakerAudioSegments(jobId, speakerId)` 后**立即**调一次该 callback，传入全集 `ids = segments.map(s => s.segmentId)`。
   - **Modal 必须把该 callback 传给 picker** 并把入参包成 `Set<number>` 存到内部 state：

     ```ts
     const [availableSegmentIds, setAvailableSegmentIds] = useState<Set<number>>(
       new Set(),
     )
     // 传给 picker
     onAvailableSegmentIdsChange={(ids) => setAvailableSegmentIds(new Set(ids))}
     ```

   - 提交前 modal 层 assert：

     ```ts
     selectedSegmentIds.every((id) => availableSegmentIds.has(id))
     ```

     不满足 → 阻断提交 + 红色错误"选段不属于当前说话人，请重新选择"（**不**进入 consent 流程）。
   - 后端 A.2b 的 4 层 ownership 仍是最终防线（picker 状态可能因用户切换 speaker / 网络重排而泄漏旧选）；前端 subset assert 把状态错误提前到提交前。
   - **守卫**：picker 声明该 prop、modal 传该 prop、modal 含 `availableSegmentIds.has(` assert——三处都有守卫（见 §6 #12、#18、#19）。

5. **静态守卫加两条（明示）**：
   - 5a. Picker 文件**不得 import** `submitCosyvoiceClone`，不得出现字面量 `/api/voice/cosyvoice/clone`（除注释）。付费请求只能由 modal consent 后发起。
   - 5b. Modal 中 `setSampleMode("file")` 邻近**必须含** `setSelectedSegmentIds([])`；`setSampleMode("segments")` 邻近**必须含** `setSampleFile(null)`。XOR 状态机不允许残留。

---

## §1 背景 & 目的

E.1 让 CosyVoice 克隆 modal 在 VoiceSelectionPanel / VoiceModifyTab 中可见且可点击，但**只通了文件上传一路**：用户必须自己提供一段 3–60s 的 WAV/MP3/M4A。

E.2 要接通**第二条样本来源**：直接从用户**当前任务的转写片段**里选段，由后端 ffmpeg 拼成样本。这是 Phase 4.2 plan v4 §3.5 明文确认的"**主输入路径**"——后端 A.2b 已把 endpoint 改成 `source_segments` 为主、`sample` 为兼容入口；前端这一侧的 picker 是 E.2 的内容。

**为什么这条路径更安全 / 更易用：**

- 用户**不需要**自己拿外部音频文件，避免本人手机录音质量差、背景噪音、采样率不达标等问题；
- 后端拼接的音频天然属于该 (user, job, speaker) 三元组——所有权检查路径短；
- 用户能在 UI 里**听**每段试听后再选，比对着波形猜哪段干净更可靠。

但这条路径**风险更集中**：选错段就会克隆错人。Spec 把风险消化到守卫层。

---

## §2 范围

### 做什么（E.2 in-scope）

- 新增 `CosyVoiceSegmentPicker` 组件：在 `CosyVoiceCloneModal` 内部、`sampleMode === "segments"` 分支下渲染。
- 列出**只属于当前 speaker** 的 `SpeakerAudioSegment`（已有 `/jobs/{jobId}/speaker-audio/{speakerId}` 端点）。
- 用户可勾选多段；UI 实时显示总时长，对**固定 3.0–60.0 秒**区间做客户端校验（§0 决策 2）。
- 提交时由 modal 内部 `selectedSegmentIds: number[]` 透传到 API client；modal 公开 prop `sourceSegmentIds?: number[]` 作为**外部注入初始值**保留不删（§0 决策 3）。
- **只接入** `VoiceSelectionPanel`（approve 路径）。`VoiceModifyTab`（editing 路径）E.2 阶段继续只走 file upload（§0 决策 1）。
- 提交前 modal 层做**子集校验**：所选 id 必须全部属于当前 picker 加载的 speaker 段全集（§0 决策 4）。
- 完整覆盖的 Python 静态守卫测试（沿用 D.2 / E.1 模式，**不**引入 Vitest / RTL）。

### 不做什么（E.2 out-of-scope，留 F 或后续）

- ❌ **不改后端**——`POST /api/voice/cosyvoice/clone` 的 `source_segments` 路径在 A.2b 已经完成（含 4 层所有权 + speaker 边界检查），无需动。如果需要新加 endpoint，本 spec 失败、必须重写。
- ❌ **不改 MiniMax 克隆**——`/api/voice/voice-clone` 和它的 `VoiceCloneModal`（不是 CosyVoice）继续不动。
- ❌ **不改 D.2 ConsentModal**——consent 流程已锁死。
- ❌ **不部署生产**——E.2 合并到 main，但下一步仍是写 F 的部署灰度方案。
- ❌ **不跑 alembic upgrade**——E.2 不改 schema。
- ❌ **不引入新的付费 API 调用点**——picker 选段不发起任何 paid 调用；只在用户走完 picker → consent → submit 完整流程后，才进入已有的 `submitCosyvoiceClone()`。
- ❌ **不引入 JS 测试栈**——Python 静态守卫 + `npx tsc --noEmit` + `npm run lint` 是唯一验收手段。

---

## §3 现状（E.1 之后）

### 3.1 后端

- `POST /api/voice/cosyvoice/clone`（A.2b）：
  - 接受 `source_segments: str | None`（JSON list，例如 `"[3,7,11]"`）+ `source_job_id: str | None`
  - 严格 `type(x) is int` 解析，拒绝 bool/float/string；拒绝空数组（A.2b `_parse_source_segments`）
  - 4 层所有权 + speaker 边界检查（plan §4.1）
  - `sample` 和 `source_segments` 严格 XOR

- `GET /jobs/{jobId}/speaker-audio/{speakerId}`（既有）：
  - 返回 `{ segment_id, start_ms, end_ms, duration_s, source_text, audio_url, dubbing_mode }[]`
  - 已经做 speaker filter（后端只返回 `speaker_id == 入参`的段）
  - MiniMax 旧 clone path 也用这个端点取段——**E.2 不动这个端点**

### 3.2 前端（E.1 落地点）

`frontend-next/src/components/voice-clone/CosyVoiceCloneModal.tsx`：
- 接受 `sourceSegmentIds?: number[]` prop（D.2 字段名 + 类型已锁，**严格 `number[]`**）
- 当且仅当 `defaultSourceJobId && sourceSegmentIds && sourceSegmentIds.length > 0` 时，segments 模式 radio 才**启用**；否则只显示一个 disabled 占位 radio（D.2 二轮 P2 fix）
- 提交时若 `sampleMode === "segments"`：透传 `sourceJobId` + `sourceSegmentIds` 到 API client
- D.2 二轮 P2 fix 强制：sampleMode 默认始终是 `"file"`，用户必须显式切到 `"segments"`

> **E.2 调整（§0 决策 3）：** Modal 的 `sourceSegmentIds?: number[]` prop **保留不删**，行为升级为"外部注入的初始值"——modal 打开时把它拷进内部 `selectedSegmentIds` state；segments radio 的启用条件改成 `Boolean(defaultSourceJobId)` 单独足够（picker 会负责"段全集非空"的展示层逻辑）。
>
> **E.2 调整（§0 决策 1）：** `VoiceModifyTab` 上的 modal 渲染处 E.2 阶段**显式不传** `defaultSourceJobId`，让 modal 自然回落到 file-only 分支。

`frontend-next/src/lib/api/cosyvoiceClone.ts`：
- 客户端 mutex：file/segments 互斥 + 空数组拒绝（Layer 6.5 mirror）
- 严格 `number[]` 类型，TypeScript 编译时拒绝 string 漂移
- `JSON.stringify(sourceSegmentIds)` 写入 FormData

`VoiceSelectionPanel.tsx`（approve 流程，行 1208–1222）：
```tsx
<CosyVoiceCloneModal
  speakerId={cosyvoiceCloneModalSpeaker}
  defaultSourceJobId={jobId}
  sourceSegmentIds={undefined}     // ← E.2 要把它填上
  onSuccess={...}
/>
```

`VoiceModifyTab.tsx`（editing 流程，行 1463–1477）：同样 `sourceSegmentIds={undefined}`。

**E.2 的核心任务**：
1. **`VoiceSelectionPanel.tsx`** 引入 picker，把 segments 模式接通；modal 内部 `selectedSegmentIds` 改 picker 选择，提交时透传给 API client。
2. **`VoiceModifyTab.tsx`** 不接 picker（§0 决策 1），把 `defaultSourceJobId={jobId}` 改为**不传**（连 `sourceSegmentIds` 也保持 `undefined`），让 modal 回落到 file-only 分支。

### 3.3 既有可复用资产

- `getSpeakerAudioSegments(jobId, speakerId)` API client — 已有
- `SpeakerAudioSegment` 类型 — 已有，含 `audioUrl` 用于试听
- VoiceSelectionPanel 行 1305-1313 的 `playSegment(seg)` 试听模式 — 可参考
- 时间码格式化 `formatTimecode(start_ms)` — 已有
- VoiceModifyTab 在 editing 模式下已经有 `segmentsBySpeaker: Map<speakerId, EditingSegment[]>` — 用户态片段已在内存里

---

## §4 实施方案

### E.2.1 — 新建 `SegmentPickerPanel` 组件

**位置：** `frontend-next/src/components/voice-clone/CosyVoiceSegmentPicker.tsx`（与 modal 同目录）

**约 250-350 行。** 纯展示 / 选择组件——**不发起任何付费 API**。

**Props 契约（v2.1 锁定）：**

```ts
interface CosyVoiceSegmentPickerProps {
  /** 必填——picker 只显示此 speaker 的段；跨 speaker 防御层 #1。 */
  speakerId: string
  /** 必填——`getSpeakerAudioSegments(jobId, speakerId)` 的入参。 */
  jobId: string
  /** 当前选中段 id 集合；强类型 number[]，防止 string 漂移。 */
  selectedSegmentIds: number[]
  /** 选择变更回调；父组件 (CloneModal) 持有状态。 */
  onChange: (next: number[]) => void
  /**
   * **§0 决策 4 / v2.1 锁定的 picker→modal 回传契约。**
   *
   * Picker 加载完 `getSpeakerAudioSegments` 后立即调用一次，传入**段全集**
   * （`segments.map(s => s.segmentId)`）。Modal 收到后包装成 `Set<number>`
   * 存到内部 state，供提交前子集 assert 使用。
   *
   * 必填——v2.1 守卫禁止 picker 不声明此 prop / modal 不传此 prop（§6 #18 / #19）。
   */
  onAvailableSegmentIdsChange: (ids: number[]) => void
  /** 可选：editing 模式下，父已有内存段，跳过网络请求并直接用 props 段列表。
   *  注意 E.2 阶段不进入 editing 路径（§0 决策 1），该 prop 保留为 future hook。 */
  preloadedSegments?: SpeakerAudioSegment[]
  /** 禁用整个 picker（提交中 / 网络错误等）。 */
  disabled?: boolean
}
```

**UI 结构：**

```
┌─────────────────────────────────────────────────┐
│ 从当前任务转写中选段                            │
│ 总时长 8.9s / 限 3–60s ✓                       │
├─────────────────────────────────────────────────┤
│ ▶ [✓] 00:01.2  "Lorem ipsum dolor..."   3.2s    │
│ ▶ [✓] 00:08.4  "Sit amet consectetur..."5.7s    │
│ ▶ [ ] 00:14.1  "Adipiscing elit..."     2.1s    │
│ ▶ [ ] 00:18.3  "Ut enim ad minim..."    4.8s    │
│ ...                                              │
└─────────────────────────────────────────────────┘
```

**关键逻辑：**

1. **加载段**：
   - 如 `preloadedSegments` 非空 → 直接用（E.2 不走这条；future hook）
   - 否则 `getSpeakerAudioSegments(jobId, speakerId)` 拉取一次
   - 按 `startMs` 升序
   - **v2.1 锁定**：加载完成后立即调 `onAvailableSegmentIdsChange(segments.map(s => s.segmentId))`，把段全集 id 数组回传给父 modal。父 modal 包装成 `Set<number>` 存到 internal state 供提交前子集 assert 用。
2. **试听**：
   - 每行一个 ▶ 按钮，与现有 `playSegment(seg)` 同款 HTML5 `<audio>` 流
   - 同时只允许一段播放
3. **选择**：
   - checkbox 切换；`selectedSegmentIds` 用 `Set` 去重再 `Array.from()` 回 `number[]`
   - **不允许提交时出现非 `number`**（TS 严格）
4. **总时长指示器（§0 决策 2 锁定阈值 3.0-60.0 s）**：
   - 实时合计已选段 `durationS`
   - `< 3.0s` 显示红色"还需 X.Xs 才能克隆"
   - `> 60.0s` 显示红色"超出 60s 上限 X.Xs"
   - `3.0 ≤ x ≤ 60.0` 显示绿色 ✓
   - 阈值字面量与 backend `gateway/cosyvoice_clone/sample_validator.py` 的 `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` 一致（注意：后端是毫秒；守卫做 1000× 换算）—— 测试守卫 cross-check 两侧常量同步
5. **空状态 / 错误状态**：
   - 段为空（speaker 没有可拼段）→ "当前说话人没有可拼段，请用文件上传"
   - 网络错误 → 可重试按钮 + 错误文案（**不**自动 fallback 到 file mode——避免静默切付费路径）

**编码约束：**

- **不 import** `submitCosyvoiceClone`（§0 决策 5a）—— 守卫扫
- **不出现字面量** `/api/voice/cosyvoice/clone`（注释除外）——守卫扫
- **不调用 `/api/voice/cosyvoice/*` 任何端点**——只读 `speaker-audio` 端点
- **不写入** localStorage / sessionStorage / cookie——选择状态是 transient

---

### E.2.2 — `VoiceSelectionPanel.tsx` 接入

**当前位置：** 行 1208–1222（已是 D.2 + E.1 落点）

**改动：** 极小——`CosyVoiceCloneModal` 渲染行不再传 `sourceSegmentIds={undefined}` 显式值（让默认值生效），其它不动。所有 picker 状态在 modal 内部管理。

```tsx
{cosyvoiceCloneModalSpeaker ? (
  <CosyVoiceCloneModal
    open
    speakerId={cosyvoiceCloneModalSpeaker}
    speakerName={...}
    defaultSourceJobId={jobId}   // ← E.2 关键：传 jobId 启用 picker
    // sourceSegmentIds 不再显式传 undefined；prop 保留但 modal 默认为 [] 内部 state
    onClose={() => setCosyvoiceCloneModalSpeaker(null)}
    onSuccess={(voice) => handleCloneComplete(...)}
  />
) : null}
```

**Modal 内部状态升级（picker 内嵌方案，v2.1 写法）：**

```tsx
// 内部 state——所有 picker 操作 mutate 这个
const [selectedSegmentIds, setSelectedSegmentIds] = useState<number[]>([])
// 段全集——picker 加载完段后通过 onAvailableSegmentIdsChange callback 设置
const [availableSegmentIds, setAvailableSegmentIds] = useState<Set<number>>(
  new Set(),
)

// 打开 modal 时，若 prop sourceSegmentIds 有值，作为初始值拷入（保留 D.2 契约）
useEffect(() => {
  if (open && sourceSegmentIds && sourceSegmentIds.length > 0) {
    setSelectedSegmentIds([...sourceSegmentIds])
  }
}, [open, sourceSegmentIds])

// segmentsModeAvailable 改为只看 jobId——picker 自己处理"段为空时的友好提示"
const segmentsModeAvailable = Boolean(defaultSourceJobId)

// 提交闸——v2.1 子集 assert
const handleSubmitClick = () => {
  if (!canRequestConsent) return
  if (sampleMode === "segments") {
    const allOwned = selectedSegmentIds.every((id) =>
      availableSegmentIds.has(id),
    )
    if (!allOwned) {
      setSubmitState({
        kind: "error",
        message: "选段不属于当前说话人，请重新选择",
        code: "client_segments_not_subset",
      })
      return
    }
  }
  setConsentOpen(true)
}

// 提交时永远读 internal state，不读 prop
sourceSegmentIds:
  sampleMode === "segments" ? selectedSegmentIds : undefined,
```

**Picker 嵌入位置：** modal 中 `sampleMode === "segments"` 的分支下渲染：

```tsx
<CosyVoiceSegmentPicker
  speakerId={speakerId}
  jobId={defaultSourceJobId!}
  selectedSegmentIds={selectedSegmentIds}
  onChange={setSelectedSegmentIds}
  onAvailableSegmentIdsChange={(ids) =>
    setAvailableSegmentIds(new Set(ids))
  }
  disabled={isLoading}
/>
```

---

### E.2.3 — `VoiceModifyTab.tsx` 接入（editing 路径）

**§0 决策 1：editing 路径 E.2 阶段不接 picker，只保留 file upload。**

**改动：** 当前位置行 1463–1477 的 modal 渲染要做**两处微调**：

1. **删除** `defaultSourceJobId={jobId}` 这一行（不传 jobId → modal 不会显示 segments radio，自然回落 file-only）。
2. 保留 `sourceSegmentIds={undefined}`（或不写，让默认生效）。

```tsx
{cosyvoiceCloneModalSpeaker ? (
  <CosyVoiceCloneModal
    open
    speakerId={cosyvoiceCloneModalSpeaker}
    speakerName={...}
    // E.2 §0 决策 1：editing 路径不传 defaultSourceJobId，modal 自然回落 file-only
    onClose={() => setCosyvoiceCloneModalSpeaker(null)}
    onSuccess={(voice) => handleCloneComplete(...)}
  />
) : null}
```

**为什么 editing 阶段先不接：**

- UI 上若放 segments picker 会让用户混淆"原始段 vs editing 段"——他们在编辑中改过段文本和 voice_map，但克隆 picker 拉的是 baseline 音频。用户合理预期是"我选的段就是我刚改的段"，但 picker 用的是 `/jobs/{jobId}/speaker-audio/{speakerId}` 即 baseline。
- 真要在 editing 阶段允许选段，需要一个 edit-aware endpoint（识别 editing 状态、返回 baseline 但加 UI 提示"以下是原始转写段，与你的编辑无关"）。这是独立工作，留到 E.2 之后。
- 退化到 file upload 不损失功能——用户在 editing 阶段照样可以传一段外部音频克隆。

---

### E.2.4 — 互斥 / 校验逻辑（关键安全层）

**前端四层防御（v2 新增 L1.5 子集 assert）：**

| 层 | 位置 | 校验内容 |
|---|---|---|
| L1 UI 闸 | `CosyVoiceCloneModal.canRequestConsent` | sampleMode==="file" 时要求 `sampleFile != null`；sampleMode==="segments" 时要求 `selectedSegmentIds.length > 0` 且总时长 ∈ [3.0, 60.0]（§0 决策 2 硬阈值） |
| **L1.5 子集 assert（§0 决策 4）** | `CosyVoiceCloneModal.handleSubmitClick` | 提交前 `selectedSegmentIds.every(id => availableSegmentIds.has(id))`；不满足 → 阻断 + 红色文案，**不**进入 consent 流程 |
| L2 客户端 mutex | `cosyvoiceClone.ts::submitCosyvoiceClone` | 已就位（D.2）——`client_sample_source_mutex` / `client_missing_source_segments` |
| L3 类型系统 | TS 严格 | `selectedSegmentIds: number[]`——任何 `string` 漂移在 `npx tsc --noEmit` 阶段就 red |
| **L4 mode 切换状态机（§0 决策 5b）** | `setSampleMode` callsites in modal | 切到 "file" 必须邻近 `setSelectedSegmentIds([])`；切到 "segments" 必须邻近 `setSampleFile(null)`。XOR 状态机不允许残留 |

**后端三层防御**（已就位，不动）：

- 后端 Layer 6.5: `sample` XOR `source_segments`
- 后端 4 层所有权检查（A.2b）：job_id 归属 + transcript 段存在 + 段 speaker == 声明 speaker + 段不可伪造
- 后端 strict `type(x) is int` 拒绝 bool/float/string

**为什么三前 + 三后？** 前端是 UX 便利层，攻击者可以直接 POST 绕过；前端筛选**不算数**——这条来自 D.2 的注释:

> 攻击者可以直接 POST `/api/voice/cosyvoice/clone`，绕过前端筛选，传 `source_segments=[B 的某段 id]` + `speaker_id=A` → 用 A 的额度克隆出 B 的声音。后端 speaker 边界校验是唯一防线。

E.2 不删/不弱化任何后端层；前端层只是把"提交后等 400/403"的烂体验提前到"提交按钮变灰"。

---

### E.2.5 — 用户体验细节

- **默认仍然是 `sampleMode === "file"`**（D.2 二轮 P2 fix 保留）——用户必须显式切到 segments tab 才进入 picker。
- **Picker 加载中**显示 skeleton；网络错误显示中文重试。
- **空 speaker**（没有可拼段）显示中文文案"该说话人没有可用片段，请用文件上传"，不让模式切换。
- **段被禁用**（duration < 0.5s 等噪音段）显示灰色 + tooltip"段过短"；本 spec 不强制做，可放 F 优化。
- **总时长违规**显示红色错误条 + 禁用提交按钮（与 file 上限超出 10MB 同款交互）。
- **试听同时只允许一段**：参考 VoiceSelectionPanel 行 1305-1313 的 audioRef pattern。
- **关闭 modal 取消选择**：modal 关闭后所有内部 state 重置（D.2 已有 useEffect, E.2 加 `setSelectedSegmentIds([])`）。

---

## §5 风险 → 防御（按用户给的 4 条 + 2 条补充）

| # | 风险 | 防御层 |
|---|---|---|
| R1 | 选段跨 speaker 混入（picker 显示了别的 speaker 的段，或 UI 状态在切换 speaker 时残留旧选） | **L1 picker**：只调 `getSpeakerAudioSegments(jobId, speakerId)`，端点本身按 speaker filter；**L1.5 modal 子集 assert（§0 决策 4）**：提交前 `selectedSegmentIds ⊆ availableSegmentIds` 否则阻断；**L2 测试守卫**：扫 `CosyVoiceSegmentPicker.tsx` 中 `getSpeakerAudioSegments` 入参必须用 prop `speakerId`，扫 modal 含 subset assert；**L3 后端**：A.2b 4 层 ownership 检查（不动）|
| R2 | `sourceSegmentIds` 类型漂移到 string | **L1 TS 严格**：`useState<number[]>`、`onChange: (n: number[]) => void`；**L2 测试守卫**：grep 守卫禁止 `source_segments.*string` / `Array<string>` / `.toString()` on segment ids 在 CosyVoice clone 路径出现；**L3 D.2 已有的 `test_d2_source_segment_ids_typed_as_number_array` 继续守 API client 边界（**§0 决策 3：prop 保留不破坏该测试**）|
| R3 | file 和 source_segments 同时提交（互斥失效） | **L1 modal state**：`sampleMode` 切换时清空对侧 state（§0 决策 5b）；**L2 客户端 mutex**（D.2 已有）；**L3 后端 Layer 6.5**（A.2b 已有）；**L4 测试守卫**：扫 modal 两个 `setSampleMode` 调用点必须邻近清理对侧 |
| R4 | 误改 MiniMax 旧 clone path | **L1 文件隔离**：新组件位于 `voice-clone/CosyVoiceSegmentPicker.tsx`，**不**改 `voice-clone/VoiceCloneModal.tsx`（MiniMax）；**L2 测试守卫**：AST 扫 E.2 PR 修改文件清单，不允许触碰 `VoiceCloneModal.tsx`（不含 `CosyVoice` 前缀）+ `voiceLibrary.ts` MiniMax-specific 函数 |
| R5（§0 决策 5a 锁定） | Picker 误发付费请求 | **L1 编码约束**：picker 只 `import { getSpeakerAudioSegments }`，禁止 `import { submitCosyvoiceClone }`；**L2 测试守卫**：grep `CosyVoiceSegmentPicker.tsx` 不含 `submitCosyvoiceClone` / `/api/voice/cosyvoice/clone`（除非是注释提及） |
| R6（§0 决策 5b 锁定） | XOR 状态机泄漏（mode 切换后旧选段还残留） | **L1 modal effect**：`setSampleMode("file")` 时显式 `setSelectedSegmentIds([])`，反之 `setSampleMode("segments")` 时 `setSampleFile(null)`；**L2 测试守卫**：grep modal 文件两个清理点都存在 |
| R7（v2 新增） | editing 阶段误选 baseline 段，用户期望 vs 实际不一致 | **L1 §0 决策 1**：editing 路径 E.2 阶段不接 picker，VoiceModifyTab 不传 `defaultSourceJobId`，modal 自然回落 file-only；**L2 测试守卫**：扫 `VoiceModifyTab.tsx` 中 `<CosyVoiceCloneModal>` 渲染**不出现** `defaultSourceJobId=` 字面量 |

---

## §6 静态守卫测试（新文件）

**位置：** `tests/test_phase42_e2_segment_picker_guards.py`

**守卫项（v2.1，共 19 条；§0 五条决策每条至少 1 个守卫，picker→modal 回传契约新增 2 条）：**

| # | 名称 | 锁定的 §0 决策 / 风险 |
|---|---|---|
| 1 | `test_e2_segment_picker_file_exists` | sanity |
| 2 | `test_e2_segment_picker_speaker_id_required_in_api_call` — picker 必须以 prop `speakerId` 调 `getSpeakerAudioSegments` | R1 |
| 3 | `test_e2_segment_picker_no_paid_api_imports` — picker 文件不 import `submitCosyvoiceClone` | §0 决策 5a / R5 |
| 4 | `test_e2_segment_picker_no_clone_endpoint_in_source` — picker 源文件不出现字面量 `/api/voice/cosyvoice/clone`（注释除外） | §0 决策 5a / R5 |
| 5 | `test_e2_modal_internalizes_selected_segment_ids` — modal 含 `useState<number[]>([])` 且至少一处 `setSelectedSegmentIds` setter | §0 决策 3 |
| 6 | **`test_e2_modal_keeps_source_segment_ids_prop`** — modal 公开接口**保留** `sourceSegmentIds?: number[]`（防止开工时被误删） | §0 决策 3 |
| 7 | **`test_e2_modal_initializes_internal_state_from_prop`** — modal 含 `useEffect` 在 `open === true && sourceSegmentIds.length > 0` 时把 prop 拷入内部 state | §0 决策 3 |
| 8 | `test_e2_modal_resets_segments_on_mode_switch_to_file` — `setSampleMode("file")` 邻近含 `setSelectedSegmentIds([])` | §0 决策 5b / R3 / R6 |
| 9 | `test_e2_modal_resets_file_on_mode_switch_to_segments` — `setSampleMode("segments")` 邻近含 `setSampleFile(null)` | §0 决策 5b / R3 / R6 |
| 10 | `test_e2_modal_close_resets_segments` — modal 关闭 useEffect 含 `setSelectedSegmentIds([])` | R3 |
| 11 | **`test_e2_modal_three_to_sixty_second_threshold_literal`** — 前端 modal `canRequestConsent` 在 segments 模式下出现字面量 `3` / `3.0` 和 `60` / `60.0`（秒）；同时后端 `gateway/cosyvoice_clone/sample_validator.py` 含 `MIN_DURATION_MS = 3_000` 和 `MAX_DURATION_MS = 60_000`（毫秒）。守卫读两侧字面量做 ×1000 换算比对，任一侧改了会同步红 | §0 决策 2 |
| 12 | **`test_e2_modal_subset_assert_before_submit`** — modal 中 `handleSubmitClick`（或同名提交闸函数）含 `availableSegmentIds` + `.has(` 模式的子集校验 | §0 决策 4 / R1 |
| 13 | **`test_e2_voice_modify_tab_no_default_source_job_id`** — `VoiceModifyTab.tsx` 中 `<CosyVoiceCloneModal>` 渲染**不**出现 `defaultSourceJobId=`（让 modal 回落 file-only） | §0 决策 1 / R7 |
| 14 | `test_e2_voice_selection_panel_passes_default_source_job_id` — `VoiceSelectionPanel.tsx` 中 `<CosyVoiceCloneModal>` 渲染**含** `defaultSourceJobId={jobId}` | §0 决策 1（反向 sanity） |
| 15 | `test_e2_no_string_segment_id_drift` — 全前端 grep：`source_segments.*string` / `Array<string>` 紧跟 segment 上下文 / `.toString()` 用于 segment id — 全部禁止 | R2 |
| 16 | `test_e2_minimax_voice_clone_modal_untouched` — 该文件不含 cosyvoice 任何字面量 + AST 无新增 cosyvoice import | R4 |
| 17 | `test_e2_no_vitest_or_jsdom_introduced` — `package.json` 不新增 vitest / @testing-library / jsdom / happy-dom | 项目硬约束 |
| 18 | **`test_e2_picker_declares_on_available_segment_ids_change_prop`** — `CosyVoiceSegmentPicker.tsx` 的 props 接口含 `onAvailableSegmentIdsChange: (ids: number[]) => void`；picker 加载完成路径含 `onAvailableSegmentIdsChange(` 调用 | v2.1 / §0 决策 4 |
| 19 | **`test_e2_modal_passes_on_available_segment_ids_change_to_picker`** — `CosyVoiceCloneModal.tsx` 渲染 `<CosyVoiceSegmentPicker>` 时传入 `onAvailableSegmentIdsChange=` prop；handler 内含 `setAvailableSegmentIds(new Set(` 模式 | v2.1 / §0 决策 4 |

**沿用 D.2 测试架构：**

- 加 `_strip_comments` helper（已有）
- 文件路径常量 + 工厂方法
- 每条测试一条 assertion + 中文 docstring 解释为什么这条边界要存在

**辅助验收（CI / 本地）：**

- `cd frontend-next && npx tsc --noEmit` 通过
- `cd frontend-next && npm run lint` 0 errors（已有 32 warnings 不动）
- `python -m pytest tests/test_phase42_e2_*.py tests/test_phase42_e1_*.py tests/test_phase42_d*_*.py tests/test_cosyvoice_clone_*.py` 全绿

---

## §7 验收标准 (DoD)

提交 PR 前必须满足：

- [ ] 新组件 `CosyVoiceSegmentPicker.tsx` 落地，~250-350 行，含完整中文 docstring
- [ ] `CosyVoiceCloneModal.tsx` 增加内部 `selectedSegmentIds` state + `availableSegmentIds` set；**保留** `sourceSegmentIds?: number[]` 公开 prop（§0 决策 3）
- [ ] Modal `useEffect` 在 open 时把 prop `sourceSegmentIds` 拷进内部 state（§0 决策 3）
- [ ] Modal `handleSubmitClick`（或同名闸）含子集 assert（§0 决策 4）
- [ ] Modal 含 3.0–60.0 秒字面量阈值（§0 决策 2），与 backend `sample_validator` 常量同步
- [ ] Modal `setSampleMode` 两个 callsite 邻近含对侧 reset（§0 决策 5b）
- [ ] `VoiceSelectionPanel.tsx`：`<CosyVoiceCloneModal>` 渲染含 `defaultSourceJobId={jobId}`，不显式传 `sourceSegmentIds`
- [ ] `VoiceModifyTab.tsx`：`<CosyVoiceCloneModal>` 渲染**不**含 `defaultSourceJobId=`（§0 决策 1）
- [ ] Picker 声明 `onAvailableSegmentIdsChange: (ids: number[]) => void` prop，加载完成路径回传段全集（v2.1）
- [ ] Modal 渲染 picker 时传 `onAvailableSegmentIdsChange={(ids) => setAvailableSegmentIds(new Set(ids))}`（v2.1）
- [ ] 新增 `tests/test_phase42_e2_segment_picker_guards.py`，19 条 Python 静态守卫，全绿
- [ ] `npx tsc --noEmit` 通过；`npm run lint` 0 errors（与 E.1 后基线一致）
- [ ] 跨 phase 回归：D.2 + E.1 + E.2 + cosyvoice clone backend 全绿（~340+ 条）
- [ ] **不**改任何后端文件（gateway / src / migrations）
- [ ] **不**改 MiniMax 旧 clone 文件（`VoiceCloneModal.tsx`、`voiceLibrary.ts` MiniMax 部分）
- [ ] **不**改 `package.json`（除非依赖必须，需在 PR 描述里说清）
- [ ] PR 描述含本 spec v2 链接 + 受影响代码行数 + 守卫测试增加数
- [ ] 推 PR 后跑 `@codex review`，至少跑 1 轮（一般 2-3 轮）

---

## §8 不做的事 / 出范围（再列一遍重要的）

| 项 | 原因 |
|---|---|
| 改后端 `POST /clone` | A.2b 已完成；无需改 |
| 改 `/jobs/{jobId}/speaker-audio/{speakerId}` 端点 | MiniMax 也在用，**碰一下 = 跨 phase risk** |
| 改 MiniMax 克隆 | 用户硬约束，旧路径不动 |
| **editing 路径接 source_segments picker** | **§0 决策 1**：等 edit-aware endpoint 出来再做，E.2 留 file-only |
| **移除 `sourceSegmentIds?: number[]` modal prop** | **§0 决策 3**：D.2 已锁，E.2 保留并升级为"外部注入初始值" |
| **picker 加载失败自动回落 file mode** | **§0 决策 5（暗含）**：避免静默切付费路径，picker 失败只重试 |
| 部署生产 | 留 F |
| 跑 alembic upgrade | E.2 不改 schema |
| 把 picker 抽给 MiniMax 复用 | 复用 = 改两条路径风险；F 之后再评估 |
| 给 picker 加"AI 推荐最佳 5 段"按钮 | LLM 调用属付费 API，需独立用户授权流程，留更后阶段 |
| 给 picker 加波形可视化 | UI nice-to-have；E.2 不做 |

---

## §9 后续：F 阶段

E.2 合并到 main 后，F 阶段开始：

1. **F.1 — admin-only stage 1 滚动**：
   - `cosyvoice_clone_admin_only` admin flag 已在 D.1 落地
   - F.1 在 admin UI 加灰度白名单 UI（已有），同时在 admin healthz 加 CosyVoice clone smoke probe（端到端 dry run，不真发 worker，只验 OSS + clone-gate）
2. **F.2 — GA flip**：
   - 把 `general_availability_enabled` 切到 true
   - 同时关掉 admin-only flag
   - 新加付费可观测面板（DashScope cost / day / user）
3. **F.3 — US prod 部署烟测**：
   - 用 `D:\daili\scripts\Deploy-Via-154.cmd` 部署 app + gateway
   - 部署前必须 `psql` 检查 in-flight pipeline（feedback_compose_env_file_recreate.md 教训）
   - 部署后跑 worker smoke + OSS probe + clone-gate probe + 一个 admin 测试 clone

F 阶段独立 spec 在 E.2 合并后再起。

---

## §10 时间估算 / 提交粒度

参考 E.1：从首次 PR 到合并 ~13 commit、~6-8 轮 Codex review。E.2 比 E.1 小（只动前端 + 测试，不动后端 / pipeline），估计：

- **首次 PR commit**：~5-7 个 commit
  - C1：`feat: add CosyVoiceSegmentPicker component`
  - C2：`feat: internalize sourceSegmentIds in CosyVoiceCloneModal`
  - C3：`refactor: VoiceSelectionPanel + VoiceModifyTab clean up sourceSegmentIds prop`
  - C4：`test: add 12-15 E.2 static guards`
- **Codex 复审轮次**：估 2-4 轮（picker 是新组件，第一轮通常发现 1-2 条 P1 + 几条 P2）
- **总耗时估**：2-4 小时（含审核等待）

---

## §11 v1 → v2 决策记录（Codex 2026-05-27 复审已锁）

v1 的 5 条 open question 在 v2 全部落档为 §0 决策，本节只是审计轨迹。

| # | v1 open question | v2 决策 |
|---|---|---|
| Q1 | picker 该放进 modal 还是独立组件？ | **内嵌 modal**——modal 持有 `selectedSegmentIds` + `availableSegmentIds` state（§0 决策 3） |
| Q2 | editing 模式是否复用 baseline `speaker-audio` 端点？ | **E.2 不接 editing 路径**——VoiceModifyTab 只保留 file upload，等 edit-aware endpoint 再放开（§0 决策 1） |
| Q3 | 总时长校验阈值是否锁死？ | **锁死 3.0–60.0 秒**——客户端字面量 + 测试守卫与 backend `sample_validator` cross-check（§0 决策 2） |
| Q4 | `sourceSegmentIds` prop 是否移除？ | **保留**——升级为"外部注入初始值"，不破坏 D.2 公开契约（§0 决策 3） |
| Q5 | picker 失败是否 fallback 到 file？ | **不 fallback**——picker 失败只显示重试按钮，避免静默切付费路径（§0 决策 5 暗含） |

v2 新增的硬约束（v1 没显式提）：

- 跨 speaker 防护必须有**前端子集 assert**——不能只靠后端 ownership 检查（§0 决策 4）
- mode 切换的 XOR 清理必须有**测试守卫**逐个验（§0 决策 5b）
- picker 文件不得有任何付费 API 入口（import / endpoint 字面量）——加 2 条守卫（§0 决策 5a）

**v2 → v2.1 二轮订正（Codex 2026-05-27 二审）：**

- **常量符号对齐**：阈值守卫不能引用不存在的 `MIN_SAMPLE_SECONDS` / `MAX_SAMPLE_SECONDS`；实际后端是 `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000`（单位毫秒）。守卫做 1000× 换算后比对（§6 #11）。
- **picker → modal 回传契约写死**：v2 只说"通过 onSegmentsLoaded 或 onChange 传出"偏松；v2.1 锁定 prop 名 `onAvailableSegmentIdsChange: (ids: number[]) => void`，picker 必须声明 + modal 必须传 + 实际包成 `Set<number>`。新增 2 条守卫（§6 #18 / #19），总数 17 → 19。

**v2.1 → v2.2 三轮订正（Codex PR #16 P2 fix）：**

- **ms 精度边界**：v2.1 客户端总时长聚合用 `seg.durationS`（一位小数 round），会让真实 2.96s → 显示 3.0s → 前端放行 → 后端 `MIN_DURATION_MS = 3000` ms 精度拒收，边界 case 双层不一致。v2.2 改写为**毫秒优先**：
  - Picker 内部聚合用 `endMs - startMs`（精确毫秒差值）
  - Picker 暴露 prop **重命名**为 `onSelectedDurationMsChange: (ms: number) => void`
  - Modal state 重命名 `selectedDurationSeconds` → `selectedDurationMs`
  - Modal `canRequestConsent` 校验从 `< 3` / `> 60`（秒）改为 `< 3000` / `> 60000`（毫秒），与后端**完全同单位**
  - UI 展示继续 `ms / 1000` 后 toFixed(1) 展示秒，但**校验不读展示值**
  - 守卫 #11 同步改写（断言 ms-literal modal 校验 + picker `endMs - startMs` + picker ms prop + 后端 ms 常量；4 条 invariant 一个测试里）

---

## §12 触发实施

Spec 通过 Codex 复审后：

1. 开 PR 分支：`codex/cosyvoice-phase42-e2-segment-picker`（与 E.1 命名一致）
2. 按 §4 实施
3. 跑 §6 验收
4. 提 PR，`@codex review`
5. 迭代修，直到 Codex 全绿
6. 合并到 main（rebase 模式，与 E.1 一致）
7. 不部署，等 F.3

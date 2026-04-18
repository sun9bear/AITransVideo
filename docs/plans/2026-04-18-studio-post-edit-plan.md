<!-- /autoplan restore point: /c/Users/Administrator/.gstack/projects/AIVideoTrans_Codex_web_mvp/main-autoplan-restore-20260418-195428.md -->
# Studio 任务二次修改 + 工作区 / 任务卡 UX 改造方案

- 创建日期：2026-04-18（v1 初稿）
- 修订日期：2026-04-18（v2，吸收 Claude Code + CodeX 两份评审意见后重写）
- 作者：sun9bear（Claude Opus 4.7 协助起草与修订）
- 状态：**待实施**
- 依赖方案：
  - `docs/plans/2026-04-18-express-studio-output-filter-plan.md`（Express / Studio 输出分层已完成）
  - `docs/plans/2026-04-08-three-engine-voice-selection-plan.md`（Studio 三引擎音色选择）

## 0. 背景与目标

Studio 版生成的配音视频目前只能一次性过稿：用户在 Stage 6（翻译审核）、Stage 7（音色选择）确认后一路跑到 Stage 9，进入 `succeeded` 就无法再调整。实际诉求：

- 某一两句翻译不满意，想单独改文本
- 某说话人音色不对，想换一个或换引擎
- 某几段 TTS 对齐不佳（时长挤压 / 拉伸严重），想让它重合成
- 工作区 / 任务卡片暴露了 `Job ID`、AssemblyAI、内部 UUID 等技术细节

**本方案目标：**

1. Studio 已完成任务可进入 **"视频修改"** 态：音色 + 翻译 + 段落拆分 + 单段 / 批量 re-TTS + 字幕同步 + 二次确认重合成
2. 翻译审核页（Stage 6）同款**按需加载 + 局部更新**体验，段落拆分不再整页重载
3. 工作区 / 任务卡 / 列表页做 UX + 脱敏优化
4. 任务命名从"Job <uuid>"换成用户友好的中文标题

**非目标（本方案不处理）**：

- Express 任务的二次修改（Express 免审核定位，不提供修改入口）
- 多语言 target_language（另见 `2026-04-15-i18n-target-language-direction.md`）
- 单段 re-TTS 的时长跨段影响（用现有 alignment + retime 机制兜底，不做局部 re-align）

---

## 1. 核心决策汇总

| 编号 | 决策 | 结论 |
|------|------|------|
| D1 | Job 状态机扩展 | **只新增 `editing`；commit 后复用现有 `running`**（不新增 `processing` 公共态；见 D21）|
| D2 | 重新生成视频触发 | 用户显式点击"重新生成视频"才跑，不做自动重合成 |
| D3 | 新旧产物策略 | 用户确认修改时弹框："覆盖原任务"（默认）/ "保存为副本" |
| D4 | 成本透明度 | 单段 re-TTS、voice clone 按次提示扣点并二次确认；"重新生成视频"本身不额外计费 |
| D5 | TTL 续期规则（简化版） | 副本 `expires_at = min(now + 7d, 最近活着的同副本族副本.expires_at + 24h)`；首个副本直接 `now + 7d`；"覆盖"不延长 |
| D6 | 一键批量 re-TTS | 视频修改页顶部放"一键合成所有未合成段落"按钮，先弹成本预览再执行 |
| D7 | TTS 临时缓存策略 | 新 TTS 落 `editor/editing/tts_segments_draft/`；用户"确认修改"时原子替换；取消则丢弃；API 扣点不退 |
| D8 | 字幕同步 | 重新生成视频时自动重跑 SRT 生成（zh / en / bilingual） |
| D9 | 异常段高亮 | `alignment_method === 'force_dsp'` 段红边框 + 时长偏差文案（绝对秒数 + 百分比） |
| D10 | 任务名规则 | YouTube 取 `yt-dlp` title 截断到 **12 中文字符**（等效显示宽度 24）；本地上传取文件名；均无则 `上传视频 YYYY-MM-DD 001`；重名后缀 `_xxxx` |
| D11 | 取消按钮 | 所有"取消审核" / "取消修改"加二次确认，文案明确"已消耗点数不退" |
| D12 | 卡片 UX | Job ID 不作主标题；"进入工作台"按钮卡片中央居中；过期倒计时按 >3d / 1-3d / <1d 三级变色 |
| D13 | 管理员日志 | "关键进展"详细日志仅 admin 可见 |
| D14 | 工作区副标题脱敏 | 删除重复信息块；脱敏 AssemblyAI / task UUID / provider 名等 |
| D15 | 重新生成视频二次确认 | 弹窗提示"重新生成后无法回到编辑状态，是否继续？" |
| D16 | 重命名能力 | 任务卡 "..." 菜单加"重命名" |
| D17 | 副本默认名 | 源任务叫 A → 副本默认 `A · 副本 N`（N 为该源任务下已有副本数 +1） |
| D18 | 列表页分区 | `editing` 态任务与 `waiting_for_review` 混排（同"待办"区），仅靠卡片 badge 区分 |
| D19 | 副本链可视化 | 本方案不做 tree / timeline；卡片最多显示 `· 派生自 <源名>` 小字 |
| ~~D20~~ | ~~re-TTS 计费单价独立~~ | **废弃**（见 D30） |
| D21 | 状态机简化（吸收 CX1） | 只新增 `editing`；commit 重合成走 `running`，不引入新的 `processing` 公共状态 |
| D22 | editing 可变文件统一目录（吸收 C1 / H3） | 所有编辑态可变文件集中在 `project_dir/editor/editing/`；cancel 时整目录删；commit 时原子应用到 baseline |
| D23 | TTL 作用域修正（吸收 CX2） | TTL 查询必须加 `WHERE user_id = :uid AND root_job_id = :rid`，并 `SELECT ... FOR UPDATE` |
| D24 | editing 闲置清理（吸收 H1 + CodeX 二审 P1） | `Job.editing_touched_at` 字段。`enter-edit` + 所有 editing 态 mutation（PATCH 段落 / split / 单段 re-TTS / 批量 re-TTS / 音色修改 / accept / revert）都必须刷新此字段。cleanup 扫超 24h 未刷新的 editing → 自动 cancel。**产品文案可安全使用"闲置 24h"，因为任何用户动作都算 touch** |
| D25 | 日志脱敏 server-side（吸收 C3） | `GET /jobs/{id}/logs` 后端按 role gate；非 admin 返回脱敏后内容；前端 `isAdmin` 仅 cosmetic |
| D26 | commit 失败路径不得重入 TTS（吸收 C2） | draft 缺段用 baseline `tts_segments/` 兜底；永不在 commit 管线里自动 call TTS API；T12 加 AST 守卫 |
| D27 | 副本产物 hardlink（吸收 M5） | **Linux 主机，直接用 `os.link`**：副本创建时源 `tts_segments/{sid}.wav` hardlink 到副本目录；draft 段为真实文件。**覆盖 hardlink 必须走 unlink+write 或 `os.replace`，严禁 `open('wb')` 原地写（会污染源 inode）** |
| D28 | copy_as_new 执行通道（吸收 CX3） | 新增 `submit_job_from_existing_project_dir(project_dir, start_stage='alignment', ...)` 入口；不走 ingest / transcribe |
| D29 | Feature flag 双端 gate（吸收 CodeX 补充） | `AIVIDEOTRANS_ENABLE_POST_EDIT` 同时 gate UI 入口 + 后端 `enter-edit` / `editing/*` 端点 |
| D30 | re-TTS 定价复用（吸收 M6） | **不新增独立单价字段**；单段 re-TTS 按原 TTS 定价（段落时长 × 对应 provider 时长单价）扣点；前端成本预览直接调用现有定价计算 API |
| D31 | 脱敏 provider 列表动态化（吸收 M1） | `SENSITIVE_PATTERNS` 中的 provider 名不硬编码，从 `llm_registry` / `tts_providers` 等 registry 动态生成 |
| D32 | 实施分段（吸收 CodeX 补充） | **Phase 0** 前置：数据层 + 状态触点清单 + display_name / expires_at / editing_touched_at 字段落地；**Phase 1** 主方案：post-edit 能力 + 翻译审核页 + UX 脱敏。每 Phase 独立可上线可回滚 |
| D33 | 重合成 badge 区分（吸收 A1） | `running` 态卡片/工作区文案根据 `edit_generation > 0` 区分：`正在生成` vs `正在重合成 · 第 N 次修改` |
| D34 | copy_as_new 两阶段提交（吸收 CodeX 二审 P1） | **Phase A（准备）**：创建新目录 / hardlink / 复制 JSON / apply editing diff / 创建新 Job 记录 / 提交 runner 并确认 accept。**Phase B（清理源）**：回写源任务 `succeeded` + 清 `editing_touched_at` + `rm -rf source/editor/editing/`。Phase A 任一步失败则整体回滚新目录 + 新 Job 记录，源 `editor/editing/` 与 `editing` 态保持不动，用户可 retry/cancel |
| D35 | Phase B 异常告警 channel（autoplan CEO §2）| `copy_as_new` Phase B 期间 DB / FS 异常（罕见）写 `job_events` 表 `level='critical'` + `event_type='editing.commit_phase_b_failed'` + 完整上下文，admin 面板在"任务管理"页红色高亮显示，运维可手工介入 |
| D36 | `segment_id` 端点入参校验（autoplan CEO §3 / 深度防御）| 所有接收 `segment_id` 路径参数的端点（PATCH / re-TTS / accept / revert / split）统一用 regex `^[a-z0-9_]{1,64}$` 校验；不合法返回 400。防御路径穿越 + 不依赖 DB 兜底 |
| D37 | re-TTS 前端 inFlight 锁（autoplan CEO §4）| 单段 re-TTS 按钮点击后前端立即 `disabled={inFlight}` + 段落本地 status 标 `tts_loading`；API 响应或 timeout 前拒绝二次点击。避免重复扣点 |
| D38 | 批量 re-TTS 部分失败响应结构（autoplan CEO §4）| `POST /regenerate-all-tts` 响应体：`{succeeded_count, failed_count, failed_segment_ids[], total}`；前端弹 Toast "已合成 X 段，失败 Y 段（seg_042, seg_088）" + 失败段在列表中标 `tts_failed` |
| D39 | 批量 re-TTS 异步化（autoplan CEO §7）| 批量 re-TTS 不同步阻塞 Gateway；复用现有 `background_task_system`（[`docs/plans/2026-04-16-background-task-system-plan.md`](2026-04-16-background-task-system-plan.md)）提交后台任务，前端通过 `job_events` 轮询或 SSE 收进度。单段 re-TTS 保持同步 |
| D40 | editing 生命周期事件（autoplan CEO §8 / observability）| 新增 `src/services/jobs/editing_events.py`，在每个状态转换点写 `job_events`。事件类型：`editing.entered` / `editing.mutation` (mutation_type: text/split/tts/voice) / `editing.commit_started` / `editing.commit_succeeded` / `editing.commit_failed` / `editing.cancelled` (reason: manual/idle_auto/commit_rollback) / `editing.commit_phase_b_failed`（D35）|
| D41 | Phase 0 migration 分批回填（autoplan CEO §9）| `display_name` / `expires_at` / `root_job_id` 回填脚本分批处理，每批 500 条 + 批次间 100ms sleep，避免长事务锁表。migration 提供 `--batch-size` / `--dry-run` 参数 |
| D42 | admin 强制 cancel editing（SELECTIVE EXPANSION E4）| admin 面板任务详情页，editing 态任务新增"强制取消修改"按钮，走 `editing/cancel` 路径 + 留 audit log `cancelled_by_admin=<admin_user_id>`。运维场景 |
| D43 | 任务卡"修改"直达按钮（SELECTIVE EXPANSION E6）| Studio + `succeeded` + feature flag 的任务卡右上角，除 "..." 菜单外直接暴露"修改"按钮（icon + 文字），一键跳 `/workspace/{id}/edit`。降低入口门槛 |
| D44 | 异常段顶部统计横幅（autoplan Design §1）| 视频修改页若存在 `alignment_method === 'force_dsp'` 段 > 0，在段列表顶部显示横幅 "⚠ 有 N 段时长异常，点击定位"，点击滚动到首个异常段 |
| D45 | enter-edit loading 反馈（autoplan Design §3）| `POST /enter-edit` 前端调用期间显示全屏或按钮 loading "正在准备修改环境..."，最长超时 10s（创建 `editor/editing/` 目录 + DB 转换正常应 < 1s） |
| D46 | running badge 配色对齐项目主色（autoplan Design §5）| 方案原写的 "running 蓝色 badge" 改为 **青色 `#06B6D4`**（项目 secondary color，见 CLAUDE.md 设计系统），避免引入项目色系外的蓝色 |
| D47 | 响应式 + a11y 规范（autoplan Design §6）| 视频修改页 / 翻译审核页 / 工作区页补响应式断点（桌面 ≥1024 / 平板 768-1024 / 手机 <768）+ a11y（键盘导航、ARIA landmarks、44px 触摸 target、对比度）。见 §7.9 |

---

## 2. 实施分段

### Phase 0：数据层 + 状态触点清单（前置）

**目标**：把"新字段 + 状态枚举新增 + 现有所有状态触点改造"一次做完，不引入任何 post-edit 业务能力。Phase 0 上线后系统行为对用户完全透明（`editing` 态无途径进入）。

**交付物**：
- `Job` 表新增 4 个字段（display_name / expires_at / editing_touched_at / root_job_id）+ `copy_of_job_id` + `edit_generation`
- `JobStatus` 枚举新增 `editing`（前后端都认识，但未开启写入路径）
- 状态触点 8 个文件全部兼容新枚举
- 任务命名 + 脱敏 + 过期倒计时颜色 + 卡片 UX 优化（这部分和用户立即可见）
- cleanup 扫 editing_touched_at + 24h（未来 Phase 1 启用后生效，Phase 0 不会触发）

**价值**：独立可发布（UX 改善已可见），数据模型稳固后 Phase 1 才有立足点。

### Phase 1：Post-edit 核心 + 翻译审核页改造 + 管理员日志

**目标**：在 Phase 0 稳固的数据层之上实现 editing 工作流、视频修改页、翻译审核页按需加载、管理员日志脱敏。

**交付物**：
- `POST /enter-edit` / `/editing/commit` / `/editing/cancel`
- 视频修改页 `/workspace/{id}/edit`
- 翻译审核页虚拟滚动 + split 局部更新
- LogViewer server-side 脱敏 + role gate
- Feature flag `AIVIDEOTRANS_ENABLE_POST_EDIT` 双端 gate

**风险**：集中在状态转换和 commit 失败回退路径，T12 的守卫测试必不可少。

---

## 3. 数据模型改动

### 3.1 `Job` 表（gateway DB）

```sql
ALTER TABLE jobs ADD COLUMN display_name         VARCHAR(60)                  NULL;
ALTER TABLE jobs ADD COLUMN expires_at           TIMESTAMP WITH TIME ZONE     NULL;
ALTER TABLE jobs ADD COLUMN editing_touched_at   TIMESTAMP WITH TIME ZONE     NULL;
ALTER TABLE jobs ADD COLUMN copy_of_job_id       VARCHAR(64)                  NULL;
ALTER TABLE jobs ADD COLUMN root_job_id          VARCHAR(64)                  NULL;
ALTER TABLE jobs ADD COLUMN edit_generation      INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_jobs_root_user_expires
    ON jobs (root_job_id, user_id, expires_at);
CREATE INDEX idx_jobs_copy_of_job_id
    ON jobs (copy_of_job_id);
CREATE INDEX idx_jobs_editing_touched_at
    ON jobs (editing_touched_at)
    WHERE editing_touched_at IS NOT NULL;
```

字段语义：
- `display_name`：用户可见标题；首次自动生成，用户可改；长度 ≤ 60
- `expires_at`：Phase 0 回填 `created_at + 7d`；Phase 1 副本按 D5 公式写入
- `editing_touched_at`：语义 = "用户最后一次在 editing 态做动作的时间"。写入 / 刷新时机见 §5.4.1；`commit` / `cancel` 成功后清空；cleanup 扫超 24h 未刷新的 editing → 自动 cancel（D24）
- `copy_of_job_id`：副本指向直接父任务；可能为 null（原始任务）或 1 层链（副本的副本也允许）
- `root_job_id`：副本族的根任务 ID（原始任务自己的 `root_job_id = job_id`）；用于 TTL 作用域
- `edit_generation`：每次 `editing → running → succeeded` 完成 +1

**Migration 回填规则**：
- `display_name`：用 `getJobDisplayTitle` 等价算法回填（见 §6.2）
- `expires_at`：`status NOT IN ('running', 'queued')` 的取 `COALESCE(updated_at, created_at) + 7d`；进行中的 job 留 NULL（由下次 touch 时写入）
- `root_job_id`：所有现存 job 回填 `= job_id`（都是原始任务，没有副本关系）
- `copy_of_job_id` / `editing_touched_at` / `edit_generation`：默认值即可

### 3.2 `JobRecord`（[src/services/jobs/models.py:75](../../src/services/jobs/models.py:75)）

```python
@dataclass(slots=True)
class JobRecord:
    # ... 现有字段 ...
    display_name: str | None = None
    expires_at: str | None = None              # ISO-8601
    editing_touched_at: str | None = None      # ISO-8601
    copy_of_job_id: str | None = None
    root_job_id: str | None = None
    edit_generation: int = 0
```

`__post_init__` 补 normalize；对 `root_job_id` 如果入参为空就填 `self.job_id`（保证 Phase 0 回填的语义成立）。

### 3.3 `JobSummary`（前端）

`frontend-next/src/types/jobs.ts`：

```ts
export interface JobSummary {
  // ... 现有字段 ...
  title?: string                      // display_name 的前端投影
  expiresAt?: string
  editingTouchedAt?: string | null
  copyOfJobId?: string | null
  rootJobId?: string | null
  editGeneration?: number
}
```

`JobStatus` 类型：

```ts
export type JobStatus =
  | 'queued'
  | 'running'
  | 'waiting_for_review'
  | 'editing'                         // ← 新增
  | 'succeeded'
  | 'failed'
  | 'cancelled'
```

### 3.4 段落异常时长字段（manifest 暴露）

[src/modules/alignment/alignment_orchestrator.py:63](../../src/modules/alignment/alignment_orchestrator.py:63) 已持久化 `rewrite_count` + `alignment_method`。manifest → Job API → 前端链路缺"实际时长 vs 目标时长"。

`editor/manifest.json` 每段新增：
- `duration_target_ms`：目标时长（原音频片段长度）
- `duration_actual_ms`：实际 TTS 后音频长度
- `duration_diff_ratio`：(actual - target) / target，正负数

Translation review payload + Edit page segments payload 同步带出。

### 3.5 `editor/editing/` 目录结构（D22 核心）

**设计原则**：editing 态所有可变文件集中在一个子目录，baseline 绝对不被改动；cancel = `rm -rf editor/editing/`；commit = 原子应用到 baseline。

```
project_dir/editor/
├── transcript.json                    # baseline，editing 期间只读
├── segments.json                      # baseline，editing 期间只读
├── manifest.json                      # baseline，editing 期间只读
├── tts_segments/                      # baseline，editing 期间只读
│   └── {segment_id}.wav
└── editing/                           # ← 本方案新增，editing 态存在时才有
    ├── segments.json                  # editing 期间的可变段落（baseline 副本，允许改 cn_text / split）
    ├── voice_map.json                 # 用户改过音色的段落：{segment_id: {provider, voice_id}}
    ├── tts_segments_draft/            # 用户显式触发重合成的新 TTS
    │   └── {segment_id}.wav
    └── segment_status.json            # 每段编辑状态：{ "seg_042": "text_dirty" | "tts_dirty" | "voice_dirty" | "accepted" }
```

**状态转换：**

| 事件 | `editor/editing/` 的变化 | `editing_touched_at` |
|------|------------------------|---------------------|
| `enter-edit` | 创建目录；`cp segments.json editing/segments.json`；其他文件空 / 空目录 | **写入 now** |
| `PATCH segments/{sid}` | 更新 `editing/segments.json` 对应段；`segment_status.json` 标 `text_dirty` | **刷新** |
| `POST split-segment` | 更新 `editing/segments.json`（1 段变 2 段）；新增两个段 status = `text_dirty` | **刷新** |
| `POST segments/{sid}/regenerate-tts` | 产物写 `editing/tts_segments_draft/{sid}.wav`；status 改 `tts_dirty` | **刷新** |
| `POST regenerate-all-tts` | 批量写 draft；每段 status 改 `tts_dirty` | **刷新** |
| 音色修改（写 voice_map） | 更新 `editing/voice_map.json`；受影响段 status 改 `voice_dirty`（不自动 re-TTS） | **刷新** |
| accept / revert 单段 | 更新 `segment_status.json` | **刷新** |
| `editing/cancel` | `rm -rf editor/editing/` | **清空** |
| `editing/commit` 成功（overwrite 或 copy_as_new Phase B 成功后）| 见 §7.8 两阶段流程 | **清空** |
| `editing/commit` 失败 | `editor/editing/` 不动，job 回 `editing` 态让用户决定 retry / cancel | **不变**（保留上次 mutation 的时间） |

**副本（copy_as_new）产物处理：**

1. 新副本 job_id 创建，分配新 project_dir `/projects/{copy_job_id}/`
2. baseline JSON 文件**真实复制**（不 hardlink，未来要独立修改）：`cp -r source/editor/{transcript.json, segments.json, manifest.json} copy/editor/`
3. TTS 段 **hardlink**（D27）：遍历 `source/editor/tts_segments/*.wav`，对每个文件 `os.link(src, dst)`
4. 用户在副本里改过的段，产物 = 真实文件（从 draft 覆盖过来，见 §3.5.1 "hardlink 安全覆盖"）
5. 原任务状态回到 `succeeded`，draft 目录删掉（覆盖分支和副本分支互不污染）

#### 3.5.1 hardlink 安全覆盖规范（D27 延伸，强制）

**背景**：hardlink 是"多个路径指向同一个 inode"。用 `open(path, 'wb')` 写文件会**原地修改 inode 内容**，导致源任务 / 其他副本的同名文件被意外修改。必须用 unlink + write 或 `os.replace` 来"解除旧 hardlink 路径、绑定到新 inode"。

**适用范围**：所有可能写入 `editor/tts_segments/*.wav`、`editor/manifest.json`、`editor/segments.json` 这类**可能是 hardlink 的路径**的地方。editing/ 目录内的 draft 文件不受此约束（它们总是新建文件）。

**标准写法**：

```python
# 单段 re-TTS 产物落盘到副本 baseline 路径（commit 覆盖时）
def write_audio_safely(dst: Path, data: bytes) -> None:
    """hardlink-safe 覆盖写入。"""
    tmp = dst.with_suffix(dst.suffix + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dst)   # atomic; 解除 dst 原 hardlink 路径

# commit 时把 draft 应用到 baseline
def apply_draft_segment(draft_path: Path, baseline_path: Path) -> None:
    """从 draft 覆盖到 baseline，安全处理 hardlink。"""
    if baseline_path.exists():
        baseline_path.unlink()        # 解除 hardlink
    shutil.move(str(draft_path), str(baseline_path))
```

**禁止写法**：

```python
# ❌ 绝对禁止：会污染源任务 / 其他副本
with open(wav_path, 'wb') as f:
    f.write(new_audio)
```

**守卫测试**（见 §16.4 `test_hardlink_isolation_after_overwrite`）：构造源 + 副本场景，在副本里 re-TTS 一段，断言源任务的对应 wav 未被修改（对比 hash）。

---

---

## 4. Job 状态机（D1 / D21）

### 4.1 现状

[src/services/jobs/service.py:155](../../src/services/jobs/service.py:155) 硬性要求 `status == 'waiting_for_review'` 才能做审核操作。完成态（`succeeded`）不接受 mutation。[src/services/jobs/process_runner.py:117](../../src/services/jobs/process_runner.py:117) 的 active / stale 判断也只认老集合。

### 4.2 新状态机（简化版，吸收 CX1）

```
succeeded ──[POST /jobs/{id}/enter-edit]──→ editing
editing   ──[PATCH /jobs/{id}/segments/{sid}]──→ editing (修改段文本)
editing   ──[POST /jobs/{id}/segments/{sid}/regenerate-tts]──→ editing (生成 draft)
editing   ──[POST /jobs/{id}/regenerate-all-tts]──→ editing (批量 draft)
editing   ──[POST /jobs/{id}/editing/cancel]──→ succeeded (丢弃 editing/)
editing   ──[POST /jobs/{id}/editing/commit]──→ running   ← 复用现有 running！
running   ──[pipeline 跑完 alignment→publish]──→ succeeded (edit_generation += 1)
running   ──[失败]──→ editing (保留 editing/，用户决定 retry / cancel)
```

关键不变式：
- `editing` 态下 baseline 文件**绝对不被改动**
- `editing` 态不允许再次 `enter-edit`（返回 409）
- `running` 态（重合成中）不允许再次 `commit` 或 `enter-edit`
- `running` 态由 `edit_generation` 区分是初次跑还是重合成（D33）

### 4.3 状态触点清单（Phase 0 必须全部覆盖）

CodeX 指出的硬编码触点，Phase 0 每处都要扫 + 修：

| # | 文件 | 当前识别的状态集 | 改动要求 |
|---|------|-----------------|---------|
| 1 | [frontend-next/src/types/jobs.ts](../../frontend-next/src/types/jobs.ts) | `JobStatus` 类型 | 加 `editing` |
| 2 | [frontend-next/src/features/jobs/selectors.ts](../../frontend-next/src/features/jobs/selectors.ts) | `ACTIVE_JOB_STATUSES` 等 | `editing` 视为"活跃"；仍需轮询 |
| 3 | [frontend-next/src/app/(app)/projects/page.tsx:41](../../frontend-next/src/app/(app)/projects/page.tsx:41) | 列表页"最新活跃任务"选择 | 把 editing 纳入候选 |
| 4 | [frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:168](../../frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:168) | `isProcessing` 分支 | editing 单独处理：不显示"正在处理"，显示"等你确认修改" |
| 5 | [src/services/jobs/models.py:75](../../src/services/jobs/models.py:75) | JobRecord.status 校验 | 接受 `editing` |
| 6 | [src/services/jobs/service.py:155](../../src/services/jobs/service.py:155) | `continue_job` / `cancel_job` 校验 | editing 下 cancel 走 `editing/cancel` 路径（不是普通 cancel） |
| 7 | [src/services/jobs/process_runner.py:117](../../src/services/jobs/process_runner.py:117) | active / stale job 判断 | editing 不应算 stale（没有跑中进程），但应算"占用中"（cleanup 跳过） |
| 8 | [gateway/job_intercept.py](../../gateway/job_intercept.py) | gateway 层状态校验 | editing 态访问仍需 ownership 校验；editing 态不允许某些 admin 操作 |

**Phase 0 T0-1 强制产物**：一份 `docs/internal/status-touchpoints-2026-04-18.md`，列出所有触点的 commit hash + 验证方法（AST 断言或手动回归脚本）。

---

## 5. TTL 续期规则（D5 / D23）

### 5.1 规则公式（修正版）

```python
def compute_copy_expires_at(
    user_id: str,
    root_job_id: str,
    now: datetime,
    conn,
) -> datetime:
    seven_days_later = now + timedelta(days=7)
    row = conn.execute(
        """
        SELECT expires_at FROM jobs
        WHERE user_id = :uid
          AND root_job_id = :rid
          AND expires_at IS NOT NULL
          AND expires_at > :now
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE
        """,
        {"uid": user_id, "rid": root_job_id, "now": now},
    ).fetchone()
    if row is None:
        return seven_days_later
    return min(seven_days_later, row.expires_at + timedelta(hours=24))
```

**约束三件套：**
- `user_id = :uid`：避免跨用户串扰（D23，修正 CX2 发现的多租户漏洞）
- `root_job_id = :rid`：精确锁定"副本族"，不再用全局 `source_content_hash` 作为族身份
- `FOR UPDATE`：避免并发 commit 读到同一个 prev（D23，吸收 H2）

### 5.2 规则要点

- **首次创建普通任务**：`expires_at = created_at + 7d`；`root_job_id = job_id`
- **首次保存副本**：副本族只有源任务一个（且源任务不算"活副本"，不参与 sibling 查询），走 `now + 7d`；`root_job_id = 源的 root_job_id`
- **后续保存副本**：`min(now + 7d, prev.expires_at + 24h)`
- **覆盖**：`expires_at` 不变
- **删除副本**：不回溯其他副本的 TTL

### 5.3 cleanup 逻辑调整

[src/services/web_ui/cleanup.py:47](../../src/services/web_ui/cleanup.py:47) 改为：

```python
def is_expired(job: Job, now: datetime) -> bool:
    # 活跃状态不删
    if job.status in {"running", "queued"}:
        return False
    # editing 态：按 editing_touched_at + 24h 判定自动 cancel（见 5.4）
    if job.status == "editing":
        return False  # cleanup 不直接删，由 editing_idle_scanner 处理
    # 其他状态按 expires_at 判断
    if job.expires_at is not None:
        return now >= job.expires_at
    # 兜底：老数据无 expires_at
    fallback = (job.updated_at or job.created_at) + timedelta(days=7)
    return now >= fallback
```

### 5.4 editing 闲置自动 cancel（D24）

#### 5.4.1 `editing_touched_at` 刷新点清单（D24 延伸，强制）

以下所有动作的服务端 handler 完成业务逻辑成功后、返回响应前，必须把 `editing_touched_at = now` 写入 DB：

| 动作 | 触发端点 | 刷新理由 |
|------|---------|---------|
| 进入编辑态 | `POST /jobs/{id}/enter-edit` | 建立基线时间 |
| 改段落文本 | `PATCH /jobs/{id}/segments/{sid}` | 用户正在打字 |
| 拆分段落 | `POST /jobs/{id}/review/split-segment` | 用户结构性修改 |
| 单段 re-TTS | `POST /jobs/{id}/segments/{sid}/regenerate-tts` | 花钱了，必须算活跃 |
| 批量 re-TTS | `POST /jobs/{id}/regenerate-all-tts` | 同上 |
| 音色修改 | `POST /jobs/{id}/editing/voice-map`（写 voice_map） | 用户交互 |
| accept / revert 单段 draft TTS | `POST /jobs/{id}/segments/{sid}/accept` / `.../revert` | 用户决策 |

**禁止刷新的动作**（只读）：`GET /jobs/{id}/editing/segments`、日志查询、播放器预览等。只读不刷新，避免"用户开着页面挂机"被误判为活跃。

**建议实现**：在 `src/services/jobs/editing.py` 里封装一个 `touch_editing(job_id)` helper，所有上述 handler 在完成业务后统一调用，保持一致性。配合 §16.4 守卫测试 `test_editing_touched_at_refresh_on_mutation` 逐端点断言。

#### 5.4.2 扫描实现

新增 `src/services/web_ui/editing_idle_scanner.py`：

```python
def scan_editing_idle(now: datetime) -> list[JobId]:
    """扫描 editing 态超过 24h 未 touch 的 job，自动 cancel。"""
    cutoff = now - timedelta(hours=24)
    candidates = query_jobs(
        status='editing',
        editing_touched_at__lt=cutoff,
    )
    for job in candidates:
        execute_editing_cancel(job.id, reason='idle_24h_auto_cancel')
    return [job.id for job in candidates]
```

cleanup 主循环（`CLEANUP_INTERVAL_SECONDS = 6h`）调用它，每 6 小时扫一次。

**闲置 UI 提示**：`(now - editing_touched_at) > 20h` 时前端小条提示"你的编辑已闲置接近 24 小时，将在 X 小时后自动放弃"。注意这里用 `touched_at` 不是 `started_at`，文案"闲置"语义与字段匹配。

### 5.5 UI 提示

保存副本的确认 Modal 必须显示：

> 本副本将保留至 **2026-04-25 17:27**（约 6 天 23 小时）

前端 `new Date(computed_expires_at)` 渲染本地时区 + `formatDistanceToNowStrict`。

---

## 6. 任务命名规则（D10 / D16 / D17）

### 6.1 长度上限

- 标题主体：≤ 12 个中文字符（等效显示宽度 24，英文 1 / 中日韩 2）
- 重名后缀：`_` + 4 位随机（小写字母 + 数字），显示宽度固定 5
- 总上限：17 个显示宽度单位
- DB `VARCHAR(60)` 留冗余（用户手改 / 副本后缀可能超 17）

显示宽度计算函数见 §9.3。

### 6.2 自动起名决策树

```
1. YouTube 源：
   yt-dlp 返回非空 title → truncate_to_width(title, 24) → 冲突加 "_xxxx"

2. YouTube 源但 title 为空（私有/删除/401，吸收 M2）：
   降级走分支 3

3. 本地上传（文件名非空）：
   os.path.splitext(filename)[0] → truncate_to_width(..., 24) → 冲突加 "_xxxx"

4. 无文件名：
   "上传视频 YYYY-MM-DD 001"
   - YYYY-MM-DD 为用户本地时区当天
   - 001 = 该用户当天所有"走分支 4 命名"任务的顺序编号（001-999 三位）
```

### 6.3 冲突检测

- 作用域：同一用户的所有 Job（跨时间、跨状态）
- 检测时机：Job 创建时 + 手动改名时 + 保存副本时
- 冲突处理：循环生成 `_xxxx`（4 位），最多 5 次都冲突则继续追加（极端情况允许 > 17 宽度，但已经是百万分之一概率）

### 6.4 副本命名（D17）

默认 `<源任务 display_name> · 副本 N`：
- N = 源任务 `copy_of_job_id = source_id` 的已有副本数 + 1
- 超长时对主干截断，保证 `· 副本 N` 后缀完整
- 用户在保存副本 Modal 可改

### 6.5 用户手动重命名（D16）

- 卡片 "..." 菜单 → "重命名" → 小 Modal → 预填当前名
- 实时校验：长度、唯一性、禁用字符（仅禁 `<`, `>`, `"`, `/`, `\`, `\0`）
- 确认后 `PATCH /gateway/jobs/{id}` body `{"display_name": "新名"}`

---

## 7. 新增页面：视频修改页

### 7.1 路由与入口

- 路由：`/workspace/{jobId}/edit`
- 入口 A：Studio 已完成任务卡的"修改"按钮
- 入口 B：工作区页右上角"修改"按钮（仅 `succeeded` Studio 任务可见）
- 权限：`job.status === 'succeeded' && job.serviceMode === 'studio' && FEATURE_ENABLE_POST_EDIT`
- **Feature flag 双端 gate（D29）**：
  - 前端：`process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === '1'` 才渲染入口按钮
  - 后端：`enter-edit` / `editing/*` 端点启动时读 `AIVIDEOTRANS_ENABLE_POST_EDIT`，为 false 则 404
- 首次进入：前端调 `POST /jobs/{id}/enter-edit` 把状态切到 `editing`，服务端创建 `editor/editing/` + 写 `editing_touched_at`。**调用期间前端全屏 loading（D45）**："正在准备修改环境..."，超时 10s 报错，正常 < 1s
- 再次进入（状态已是 `editing`）：直接加载编辑态数据
- **异常段统计横幅（D44）**：段列表顶部若 `alignment_method === 'force_dsp'` 段数 > 0，显示 "⚠ 有 N 段时长异常，点击定位"，点击滚动到首个异常段

### 7.2 页面结构

```
┌───────────────────────────────────────────────┐
│ 顶部栏                                        │
│  < 返回  任务名 · 已修改 N 次                 │
│                    [放弃修改] [确认修改 ...]  │
├───────────────────────────────────────────────┤
│ 视频播放器（sticky top）                      │
│  ▶ ──●──────────────  01:23 / 04:56          │
├───────────────────────────────────────────────┤
│ Tab 栏：[音色修改] [翻译修改]                 │
├───────────────────────────────────────────────┤
│  [一键合成所有未合成段落（预计 X 点）]        │
│                                               │
│  段落列表（虚拟滚动）                         │
│  ┌────────────────────────────────┐           │
│  │ 00:00 - 00:03  发言人 A        │           │
│  │ 原文：...                       │           │
│  │ 译文：[textarea]                │           │
│  │ [拆分] [重新合成(~X点/秒估)] [撤销]│       │
│  │ ⚠ 时长超长 0.8s (+18%)          │           │
│  └────────────────────────────────┘           │
└───────────────────────────────────────────────┘
```

### 7.3 "音色修改" Tab

复用 [VoiceSelectionPanel.tsx](../../frontend-next/src/components/workspace/VoiceSelectionPanel.tsx) 的 UI + 后端音色查询 API，但：
- 不走 review gate 的 approve 提交
- 切换音色 / 克隆成功后写 `editor/editing/voice_map.json`（overwrite 语义：key 存在即用户显式改过）
- commit 时 voice_map 合入 `segments.json` 的 `voice_id` 字段

### 7.4 "翻译修改" Tab

| 能力 | 实现 |
|------|------|
| 虚拟滚动 | `SegmentVirtualList` 共享组件（§9.1） |
| 播放器进度联动 | `<video>` `onTimeUpdate` 二分查找段 → `scrollIntoView({ block: 'center' })` |
| 编辑文本 | 受控 `<textarea>`，blur / debounce 2s 触发 `PATCH /jobs/{id}/segments/{sid}` |
| 拆分段落 | "拆分"按钮 → inline 面板 → 服务端返回新段列表 → 本地 `splice(idx, 1, ...new_segments)` 原地替换 |
| 单段 re-TTS | 按钮带"(~X 点)"（按段时长 × 原 TTS 单价估算，D30）→ 确认 → `POST /jobs/{id}/segments/{sid}/regenerate-tts` → 出现"播放新版 / 接受 / 丢弃"按钮。**inFlight 锁（D37）**：点击后 `disabled` + 段 status 本地标 `tts_loading`，拒绝重复扣费 |
| 一键合成 | 顶部按钮，扫 `segment_status.json` 中 `text_dirty` / `voice_dirty` 的段 → 成本预览 → `POST /jobs/{id}/regenerate-all-tts`（**异步后台任务 D39**）→ SSE/轮询进度。**部分失败响应 D38**：`{succeeded_count, failed_count, failed_segment_ids[], total}`，UI 显示 "已合成 X 段，失败 Y 段（seg_042, seg_088）" + 失败段允许单独重试 |
| 撤销段落 | 每段"撤销"：文本回滚到 baseline `segments.json` 对应段；draft TTS 丢弃；status 回 `accepted` |
| 异常段高亮 | `alignment_method === 'force_dsp'` 红左边框 + ⚠ 标签 + "时长超长 0.8s（+18%）" |

### 7.5 "异常段"文案规范（D9）

段落数据：

```ts
interface SegmentTimingHint {
  kind: 'over' | 'under'
  absoluteDiffMs: number
  ratio: number  // 0.18 = 18%
}
```

渲染：
- 仅 `alignment_method === 'force_dsp'` 显示
- `over` → "时长超长 X.X 秒（+XX%）" 红色
- `under` → "时长过短 X.X 秒（-XX%）" 橙色
- 秒数保留 1 位小数；百分比取整
- 气泡提示：`⚠ 该段已自动重写 2 次仍超出目标时长，建议精简译文再重新合成`

### 7.6 顶部按钮行为

- **放弃修改** → 二次确认（D11 + D15）→ `POST /jobs/{id}/editing/cancel` → 服务端 `rm -rf editor/editing/` + 回 `succeeded` → 跳回工作区页
- **确认修改 ...** → 弹"合成方式选择" Modal（§7.7）

### 7.7 "确认修改" Modal（D3 + D15 + D4）

```
┌─────────────────────────────────────────────┐
│ 确认修改并重新生成视频                      │
├─────────────────────────────────────────────┤
│ ○ 覆盖原任务（推荐）                        │
│   • 原配音视频 / 素材包 / 字幕会被替换       │
│   • 过期时间不变：2026-04-25 17:27          │
│                                             │
│ ○ 保存为副本                                │
│   • 原任务保持不变                           │
│   • 副本名：[Original · 副本 1        ]    │
│   • 副本过期时间：2026-04-25 17:30          │
│                                             │
│ ─────────────────────────────────────────── │
│ 本次合成不额外扣费。已消耗的 TTS / 克隆点   │
│ 数（合计 X 点）不会因此退回。                │
│                                             │
│ 重新生成后无法回到本次编辑状态，是否继续？  │
│                                             │
│              [取消]  [确认并开始生成]       │
└─────────────────────────────────────────────┘
```

点击"确认并开始生成"：
- `POST /jobs/{id}/editing/commit` body `{strategy: "overwrite"|"copy_as_new", copy_display_name?: string}`
- 服务端走 §7.8 流程
- 成功 → 跳转对应任务的工作区页（`running` 状态）

### 7.8 commit 服务端流程（关键：D26 / D27 / D28）

**overwrite 分支**：

```
1. 原子应用 editor/editing/ → baseline：
   mv editor/editing/segments.json → editor/segments.json
   for sid in editor/editing/tts_segments_draft/:
       mv editor/editing/tts_segments_draft/{sid}.wav → editor/tts_segments/{sid}.wav
   合并 editor/editing/voice_map.json 到 editor/segments.json.voice_id
   rm -rf editor/editing/
2. 更新 DB：status='running'，edit_generation += 1，editing_touched_at=NULL
3. 提交 runner：submit_job_from_existing_project_dir(
       job_id=原 job_id, project_dir=原 dir, start_stage='alignment')
4. pipeline 从 alignment 跑到 publish（D26：永不重入 TTS，缺段就 FAIL，不 fallback）
5. 成功 → status='succeeded'；字幕 SRT 自动随 publish 阶段重生
6. 失败 → status='editing'；editor/editing/ 已经被 step 1 删了怎么办？
   → step 1 改为"先准备好，最后原子替换"：
     * 在 editor/editing-staging/ 里拼装合并结果
     * 成功生成视频后再 swap（editing-staging → tts_segments 等）
     * 失败：editing-staging 删掉，editing/ 不动
```

**copy_as_new 分支（D28 + D34 两阶段提交）**：

关键不变式：**Phase A 任一步失败，源任务的 `editor/editing/` 与 `editing` 状态保持不动**。只有 Phase A 全部成功、runner 已 accept 新 job，才进入 Phase B 清源。失败恢复不依赖人工介入。

```
───────── Phase A：准备（失败即整体回滚新目录 + 新 Job 记录）─────────

A1. 生成新 job_id；分配新 project_dir /projects/{new_job_id}/

A2. baseline 文件真实复制（shutil.copy2）：
    cp source/editor/transcript.json  → new/editor/transcript.json
    cp source/editor/manifest.json    → new/editor/manifest.json

A3. TTS 段 hardlink（D27，os.link）：
    for sid in source/editor/tts_segments/*.wav:
        os.link(source/editor/tts_segments/{sid}.wav,
                new/editor/tts_segments/{sid}.wav)

A4. 应用 editing diff 到 new（此时 source/editor/editing/ 仍然完整）：
    cp source/editor/editing/segments.json → new/editor/segments.json
    for sid in source/editor/editing/tts_segments_draft/:
        apply_draft_segment(draft_path, new/editor/tts_segments/{sid}.wav)
        # §3.5.1：先 unlink new 侧 hardlink 路径，再 shutil.move draft 过去
        # 源 tts_segments/{sid}.wav 的 inode 不被波及
    合并 source/editor/editing/voice_map.json 到 new/editor/segments.json.voice_id

A5. 创建新 Job 记录（status='queued'，不直接 running）：
    copy_of_job_id=源 job_id
    root_job_id=源的 root_job_id
    expires_at=compute_copy_expires_at(user_id, root_job_id, now)  # §5.1
    display_name=<源名> · 副本 N
    edit_generation=0

A6. 提交 runner：
    runner.submit_job_from_existing_project_dir(
        job_id=new_job_id, project_dir=new_dir, start_stage='alignment')
    # 同步调用，返回成功 = runner 已接受并入队
    # 失败（参数校验 / 队列满 / 任何异常）→ 走 A-rollback

A-rollback（A1-A6 任一步失败时触发）：
    - 删新 Job 记录（A5 产物）
    - rm -rf /projects/{new_job_id}/（A1-A4 产物，含 hardlink；删 hardlink 不影响源 inode）
    - 源任务 status 仍是 'editing'，editing_touched_at 不变
    - editor/editing/ 完整保留
    - 向前端抛明确错误："副本创建失败，你的编辑未丢失，可重试或改为覆盖"

───────── Phase B：清源（仅 Phase A 全部成功后执行）─────────

B1. source.status = 'succeeded'
B2. source.editing_touched_at = NULL
B3. rm -rf source/editor/editing/

Phase B 执行期间如果发生异常（罕见，纯 DB / 文件系统操作）：
  - **写 `job_events`**（D35）：`level='critical'` + `event_type='editing.commit_phase_b_failed'` + 完整上下文（source_job_id / new_job_id / failed_step / exception）
  - admin 面板"任务管理"页红色高亮显示该事件，运维手工介入（清源 editing/ + UPDATE source.status='succeeded'）
  - 不自动回滚（新副本已经 running，回滚会制造更混乱的状态）
  - 用户看到新副本正常跑；源任务暂时卡在 editing 态直到 admin 修复

───────── Phase C：pipeline 异步运行（runner 驱动）─────────

C1. new job status='queued' → 'running'（runner 正常 transition）
C2. alignment → publish 跑完 → status='succeeded'
C3. 中途失败 → new job status='failed'（与源任务完全解耦）
```

**关键保证（对应 §16.4 守卫测试 `test_copy_as_new_preserves_source_draft_on_runner_failure`）**：

- A6 runner.submit 失败时，源任务 `status='editing'` / `editing/` 目录 / `editing_touched_at` 全部保持调用前值
- 用户可以立刻 retry commit 或切换到 overwrite 策略

**关键守卫（D26）**：

commit 流程进入后，`src/pipeline/process.py` 从 `start_stage='alignment'` 启动，绝对不得调用 `tts_generator.generate_*()`。T12 AST 测试强制：

```python
# tests/test_post_edit_guards.py
def test_commit_pipeline_never_calls_tts_generator():
    """alignment/ 和 publish/ 的代码不能调 tts_generator，只能用现有产物。"""
    for path in glob("src/modules/alignment/**/*.py") + glob("src/modules/output/**/*.py"):
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = resolve_callee(node)
                assert not callee.startswith("tts_generator.generate"), \
                    f"{path}: commit 管线禁止调 TTS 生成 API"
```

### 7.9 响应式 + 可访问性规范（D47 / CLAUDE.md "响应式设计：桌面 + 手机 web 通用"）

适用范围：视频修改页、翻译审核页、工作区页、任务卡 / 列表页。

**断点：**

| 视口 | 宽度 | 视频修改页布局 |
|------|------|---------------|
| 桌面 | ≥ 1024px | 播放器 sticky top 30vh；段列表 70vh 可滚；段卡片 max-width 800px 居中 |
| 平板 | 768-1023px | 同桌面，段卡片全宽（边距 24px）|
| 手机 | < 768px | 播放器 aspect-ratio 16/9（约 40vh）；Tab 栏 sticky top；段卡片全宽（边距 12px）；段内元素垂直堆叠；单段 re-TTS 按钮换图标 |

**可访问性（WCAG 2.1 AA）：**

- **键盘导航**：Tab 在段卡片间移动；Enter 进入段 textarea；Esc 退出 textarea；Ctrl+Enter 提交当前段编辑；`/` 聚焦搜索（如果有）；`?` 显示快捷键帮助
- **ARIA landmarks**：`<main aria-label="段落编辑区">` 包段列表；`<aside aria-label="视频预览">` 包播放器；`<nav aria-label="修改阶段切换">` 包 Tab
- **触摸 target**：所有按钮 / 可点击区 ≥ 44×44 px（段卡片内按钮也不能小）
- **对比度**：force_dsp 段红边框配色满足 4.5:1 对比（深色主题下选 `oklch(0.65 0.2 25)` 级别红，不用浅红）；异常段文案同
- **Screen reader**：
  - force_dsp 段：`aria-label="段落 42，时长超长 0.8 秒，超出 18 百分比，建议精简译文"`
  - inFlight 的 re-TTS 按钮：`aria-busy="true"`
  - 批量合成进度：`<div role="status" aria-live="polite">已合成 15/30 段</div>`
- **焦点环**：所有交互元素 `:focus-visible` 显 2px 环（项目主色 #8B5CF6）
- **降动效**：`@media (prefers-reduced-motion: reduce)` 下 `scrollIntoView` 改 `behavior: 'auto'`，段卡片切换动画缩到 0ms

### 7.10 交互状态覆盖矩阵（D47 延伸）

| 交互 | Loading | Empty | Error | Success | Partial |
|------|---------|-------|-------|---------|---------|
| enter-edit | 全屏 loading "准备修改环境..." ≤10s（D45）| N/A | "无法进入修改，请重试" + 重试按钮 | 跳视频修改页 | N/A |
| 段列表加载 | 骨架屏 3 段 | N/A（已完成任务必有段）| "加载失败，重试" | 渲染 | N/A |
| 单段 PATCH | 段右上角小转圈 | N/A | Toast "保存失败，X 秒后重试" | 段右上角 ✓ 1s 后消失 | N/A |
| 单段 re-TTS | 按钮 disabled + "合成中..." | N/A | Toast "合成失败 (429/超时)" + "重试" | 段底出现试听+接受+丢弃 | N/A |
| 批量 re-TTS | 进度条 "X/Y 段" + 取消按钮 | N/A | Toast "全部失败，看详情" | Toast "全部合成完成" | Toast "成功 X 段，失败 Y 段（IDs）" 失败段红标 |
| commit 覆盖 | 全屏进度 "正在重合成..." | N/A | 回 editing 态 + Toast "合成失败，可重试或放弃" | 跳工作区页 running 态 | N/A |
| commit 副本 | 同上 | N/A | 回 editing 态 + "副本失败，编辑未丢失" | 跳新副本工作区页 | N/A |
| 放弃修改 | 瞬时 | N/A | "放弃失败" + 重试 | 跳回工作区页 | N/A |

---

## 8. 翻译审核页（Stage 6）改造

### 8.1 现状痛点

- [TranslationReviewPanel.tsx:217](../../frontend-next/src/components/workspace/TranslationReviewPanel.tsx:217) 客户端分页 20 条/页
- 拆分段落后 `window.location.reload()`（[:457](../../frontend-next/src/components/workspace/TranslationReviewPanel.tsx:457)）

### 8.2 改造要点

1. **抽共享组件 `SegmentVirtualList`**（§9.1），两页面共用
2. **拆分段 API 返回新段列表**：
   - 后端 `POST /jobs/{id}/review/split-segment` 返回：
     ```json
     { "replaced_segment_id": "seg_042",
       "new_segments": [<段对象>, <段对象>],
       "total_count": 87 }
     ```
   - 前端 `splice(index, 1, ...new_segments)` 原地替换，编号 UI 局部更新
3. 保留现有翻译审核业务逻辑（glossary / rewrite_requested / translationConfirmed），不引入 TTS 按钮
4. 拆分后不再 reload

---

## 9. 共享组件 / 共享逻辑

### 9.1 `SegmentVirtualList`

位置：`frontend-next/src/components/workspace/segments/SegmentVirtualList.tsx`

签名：

```tsx
interface SegmentVirtualListProps<T extends { segmentId: string; startMs: number; endMs: number }> {
  items: T[]
  estimatedItemHeight?: number
  activeSegmentId?: string | null
  renderSegment: (segment: T, index: number) => ReactNode
  onIntersect?: (visibleIds: string[]) => void
}
```

- 首选 `@tanstack/react-virtual`（先审 `package.json`，无则新增 dep）
- fallback 手写 `IntersectionObserver` + buffer 5 项
- `activeSegmentId` 变化时 `scrollToIndex(i, {align: 'center', behavior: 'smooth'})`

### 9.2 `usePlayerSegmentSync`

位置：`frontend-next/src/lib/react/usePlayerSegmentSync.ts`

```ts
export function usePlayerSegmentSync(
  videoRef: RefObject<HTMLVideoElement>,
  segments: ReadonlyArray<{ segmentId: string; startMs: number; endMs: number }>
): { activeSegmentId: string | null }
```

监听 `timeupdate`（throttle 200ms）+ 二分查找 + 返回当前段 ID。

### 9.3 显示宽度工具

- 后端：`src/utils/text_width.py`（`display_width` / `truncate_to_width`）
- 前端：`frontend-next/src/lib/text/width.ts`
- 共同单测

### 9.4 日志脱敏 server-side（D25）

**后端**：`src/services/jobs/logs_redactor.py`（新）

```python
# provider 名从 registry 动态生成（D31）
def _build_sensitive_patterns() -> list[re.Pattern]:
    providers = set()
    providers.update(name for name in llm_registry.iter_provider_names())
    providers.update(name for name in tts_registry.iter_provider_names())
    providers.update(['AssemblyAI', 'yt-dlp', 'ffmpeg'])  # 工具类
    alternatives = '|'.join(re.escape(p) for p in providers)
    return [
        re.compile(r'任务ID=[0-9a-f-]+'),
        re.compile(r'job[_ ]?id[:= ]+[0-9a-f-]+', re.IGNORECASE),
        re.compile(rf'\b({alternatives})\b', re.IGNORECASE),
        re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),
    ]

def redact_for_user(msg: str) -> str:
    for p in _SENSITIVE_PATTERNS:
        msg = p.sub('', msg)
    return re.sub(r'\s+', ' ', msg).strip()
```

**API 层（[src/services/jobs/api.py](../../src/services/jobs/api.py)）**：

```python
@app.get("/jobs/{job_id}/logs")
async def get_logs(job_id: str, request: Request):
    user = authenticate(request)
    events = load_events(job_id)
    if not user.is_admin:
        events = [ev._replace(message=redact_for_user(ev.message)) for ev in events]
    return {"events": events}
```

**前端**：`isAdmin` 只控制"是否渲染 LogViewer 组件"，不再用作脱敏开关（因为后端已脱敏）。

### 9.5 `getUserFacingProgressMessage` 的角色

[presentation.ts:197](../../frontend-next/src/features/jobs/presentation.ts:197) 继续做前端级 cosmetic 过滤（阶段名映射、去空），但 **不再承担敏感信息屏蔽责任**（那是后端的事）。

---

## 10. 工作区页优化（图 2）

文件：[frontend-next/src/app/(app)/workspace/\[jobId\]/page.tsx](../../frontend-next/src/app/(app)/workspace/[jobId]/page.tsx)

### 10.1 删除重复信息块

- 删掉 `{job.progressMessage}` 的顶部重复渲染（~[:227](../../frontend-next/src/app/(app)/workspace/[jobId]/page.tsx:227)）
- 保留阶段圆点 + `正在处理 · 媒体理解` 副标题

### 10.2 "正在处理 · XXX" 措辞（D33）

- `stageLabels`（[presentation.ts:3-14](../../frontend-next/src/features/jobs/presentation.ts:3)）保留现有映射
- 新增 `getUserFriendlyStageLabel`：`draft` → "正在合成配音"，`legacy_process_output` → "导出中"
- `edit_generation > 0` 的 running 态改副标题："正在重合成 · 第 N 次修改"

### 10.3 "关键进展" 管理员专属（D13 + D25）

非 admin 用户：隐藏 LogViewer，显示简化状态区（§10.4）。
admin 用户：显示完整 LogViewer，**后端已返回脱敏版本的日志**（前端不再做脱敏，见 D25 设计）。

```tsx
const { data: entitlements } = useSWR('entitlements', getEntitlements)
const isAdmin = entitlements?.ui?.show_admin_badge === true
{isAdmin && <LogViewer events={logs} />}
```

### 10.4 非管理员简化状态区

取代 LogViewer 的位置：
- `running`：大号进度 + 当前阶段友好名 + "预计还需 X 分钟"（`source_duration_seconds * 1.5` 估）
- `waiting_for_review`：大号 CTA "进入审核" + "已暂停等待你确认"
- `editing`：CTA "继续修改" + 闲置倒计时（`editing_touched_at` + 24h）
- `succeeded`：跳转结果页或显示产物卡片
- `failed`：友好错误卡 + "重试" / "联系支持"

---

## 11. 任务卡 / 列表页优化（图 1、图 3、D18）

### 11.1 卡片标题

- 从 `Job 76746adf...` 改为 `{display_name}`
- Fallback：`display_name` → `getJobDisplayTitle(job)` → "未命名视频"

### 11.2 "进入工作台"按钮居中（D12）

- 当前在卡片左下 → 改为主操作区居中大按钮
- 辅助操作（删除 / 重命名）收到右上角 "..." 菜单

### 11.3 取消审核二次确认（D11）

Dialog 文案：
> 取消后本任务将被标记为已取消，已消耗的点数不退。
>
> [返回] [确认取消]

### 11.4 过期倒计时颜色分级（D12）

- `> 3 days`：`text-muted-foreground`
- `1-3 days`：`text-amber-500`
- `< 1 day`：`text-red-500` + ⚠ icon
- 已过期：`text-red-600` + "即将删除"

### 11.5 "..." 菜单

- 重命名（D16）
- 删除
- 复制 Job ID
- 导出原始素材（仅 Studio + `succeeded`）
- **修改**（仅 Studio + `succeeded` + feature flag，D1 入口）

### 11.6 `editing` 态卡片展示（D18 / D33）

- `editing` 和 `waiting_for_review` 混排在"待办"区（按 `updated_at DESC`）
- badge（D46 配色对齐项目主色系）：
  - `waiting_for_review` → 橙色 `等待审核`
  - `editing` → 紫色 `修改中`（#8B5CF6 primary）
  - `running`（edit_generation=0） → **青色** `正在生成`（#06B6D4 secondary，不用蓝色）
  - `running`（edit_generation>0） → **青色** `正在重合成 · 第 N 次修改`
- 主 CTA 按钮文案：
  - `waiting_for_review` → `进入工作台`
  - `editing` → `继续修改`
  - `running` → `查看进度`
- 副本标记：`copy_of_job_id` 非空时，标题下小字 `· 派生自 <源名>`，不可点击（D19）

---

## 12. 字幕同步（D8）

- 字幕生成在 publish 阶段 [editor_package_writer.py:251](../../src/modules/output/editor/editor_package_writer.py:251)
- `editing → running (alignment → publish)` 会自动重跑字幕
- `_build_subtitle_slices` 读 `AlignedSegment.cn_text`，`AlignedSegment` 来自 `segments.json` → 已是 commit 后的新版本
- 三语 SRT 全部重跑，`editor/manifest.json.subtitles_updated_at` 刷新

---

## 13. API 端点新增 / 改动清单

### 13.1 职责分层（吸收 M4）

- **Gateway**：状态转换 + 业务规则 + 鉴权 + 扣费。包括 `enter-edit` / `editing/commit` / `editing/cancel` / `PATCH display_name` / 副本创建（内部调 Job API 做 project_dir 复制）
- **Job API**：project_dir 内容 CRUD。包括 `segments` 读取 / 单段 PATCH / 单段 re-TTS / 批量 re-TTS / editing 内容查询

### 13.2 Gateway 新增 / 改动

| 方法 | 路径 | 描述 |
|------|------|------|
| PATCH | `/gateway/jobs/{id}` | 支持 `display_name` 更新（重命名） |
| POST | `/gateway/jobs/{id}/enter-edit` | 状态切到 `editing`，创建 `editor/editing/` |
| POST | `/gateway/jobs/{id}/editing/cancel` | 丢弃 `editor/editing/`，回 `succeeded` |
| POST | `/gateway/jobs/{id}/editing/commit` | 提交修改，body `{strategy, copy_display_name?}` |

### 13.3 Job API 新增 / 改动

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/jobs/{id}/editing/segments` | 编辑态段落列表（含 timing hints、draft TTS URL、segment_status） |
| PATCH | `/jobs/{id}/segments/{sid}` | 修改单段文本（editing / waiting_for_review 都接） |
| POST | `/jobs/{id}/segments/{sid}/regenerate-tts` | 单段 re-TTS，产物写 draft |
| POST | `/jobs/{id}/regenerate-all-tts` | 批量 re-TTS |
| POST | `/jobs/{id}/review/split-segment` | **改造**：返回新段列表；接受 status ∈ {waiting_for_review, editing}（合并原 §12.2 两行） |
| GET | `/jobs/{id}/logs` | **改造**：按 role 返回脱敏 / 原文（D25） |

### 13.4 内部模块

- `src/services/jobs/editing.py`（新）：状态转换 + `editor/editing/` 管理
- `src/services/jobs/ttl.py`（新）：`compute_copy_expires_at` + cleanup 集成
- `src/services/jobs/copy_service.py`（新）：`copy_as_new` 的 project_dir hardlink + 新 Job 创建
- `src/services/jobs/logs_redactor.py`（新）：D25 / D31
- `src/services/tts/regenerate.py`（新）：单段 re-TTS 入口
- `src/services/web_ui/editing_idle_scanner.py`（新）：D24 闲置 cancel
- `src/services/jobs/runner_extensions.py`（新）：`submit_job_from_existing_project_dir(project_dir, start_stage, job_id)`，D28
- `src/services/jobs/editing_events.py`（新 / D40）：7 种事件类型的统一写入 helper
  ```python
  def emit_editing_event(
      job_id: str, event_type: str, level: str = 'info', **context
  ) -> None:
      """写 job_events 表，供 admin 面板 / observability 消费"""
  ```
  事件类型：`editing.entered` / `editing.mutation` / `editing.commit_started` / `editing.commit_succeeded` / `editing.commit_failed` / `editing.cancelled` / `editing.commit_phase_b_failed`
- `src/services/jobs/input_validators.py`（新 / D36）：`validate_segment_id(sid)` regex `^[a-z0-9_]{1,64}$`，被所有接收 `segment_id` 的 handler 调用

---

## 14. Phase 0 Task 拆分

### T0-1 — 状态触点清单编制（交付前强制产物）

- 逐个扫 §4.3 的 8 个文件，找出当前对状态集的所有硬编码点
- 产出 `docs/internal/status-touchpoints-2026-04-18.md`：每个点 file_path:line_number + 当前行为 + 期望行为（加 `editing` 后）
- 这是 T0-2 以后所有代码改动的参考表

### T0-2 — 数据模型 migration

- Alembic migration：加 6 个字段（display_name / expires_at / editing_touched_at / copy_of_job_id / root_job_id / edit_generation）
- **回填分批（D41）**：每批 500 条 + 批次间 `pg_sleep(0.1)` 或 Python sleep(100ms)，避免长事务锁表；migration 提供 `--batch-size` / `--dry-run` 参数
- 回填脚本：
  - `display_name` ← `getJobDisplayTitle` 等价 Python 版本
  - `expires_at` ← `COALESCE(updated_at, created_at) + 7d`（跑中 job 除外）
  - `root_job_id` ← `job_id`
- `JobRecord` dataclass 同步
- `JobSummary` 前端类型 + mapper
- 回归：老数据展示正常 + migration 对 10k+ jobs 表仍不锁表超 1s

### T0-3 — JobStatus 枚举扩展 `editing`

- 前后端枚举加 `editing`
- 按 T0-1 清单逐点改：
  - `ACTIVE_JOB_STATUSES` 加 `editing`
  - `isProcessing` 增 editing 分支（显示为"修改中"而非"正在处理"）
  - cleanup 跳过 editing
  - runner 的 active / stale 判断：editing 算"占用"但不是"跑中"
  - gateway `job_intercept` 处理 editing 的 ownership
- 单测：每个触点的分支行为

### T0-4 — 任务命名 + 脱敏基础

- `src/utils/text_width.py` + 前端版
- 自动起名（4 分支）+ 冲突检测 + `_xxxx`
- 重命名 API `PATCH /gateway/jobs/{id}`
- 前端卡片标题切 `title`
- `logs_redactor.py`（此时不接 API，只准备好）
- 单测：宽度 / 起名 / 冲突 / 脱敏 regex

### T0-5 — cleanup 改造

- 用 `expires_at` 做主要判断，`created_at + 7d` 兜底
- `editing_idle_scanner` 骨架（Phase 0 不会触发，因为没人进 editing 态）
- 集成测试：不同状态 / 不同时间的 job 期望行为

### T0-6 — 工作区页 + 任务卡 UX

- 删除重复信息块
- 过期倒计时颜色分级（>3d / 1-3d / <1d / expired）
- 卡片 CTA 按钮居中
- badge 样式（editing 紫色 / waiting 橙色 / running **青色 D46**（不用蓝色） / `edit_generation > 0` 显示"正在重合成 · 第 N 次修改"）
- 取消按钮二次确认
- `editing` 态卡片 UI（即使此时没有 editing job，也预先准备）
- **任务卡"修改"直达按钮（D43 / E6）**：Studio + succeeded + feature flag 任务卡右上角显示图标+文字按钮，一键跳 `/workspace/{id}/edit`
- 响应式断点覆盖（§7.9）：桌面 / 平板 / 手机三视口适配

### T0-7 — Phase 0 守卫测试

- AST 扫：所有读取 job status 的代码必须使用枚举常量（不得字符串字面量）
- migration 双向可回滚验证
- 回归守卫：现有所有功能（创建任务 / 审核 / 取消 / 下载产物）行为不变

---

## 15. Phase 1 Task 拆分

### T1-1 — Job API + Gateway 端点骨架

- `enter-edit` / `editing/cancel` / `editing/commit`（骨架，commit 不跑 pipeline）
- 并发保护：同一 job 同时 enter-edit 返回 409
- feature flag 双端 gate（D29）
- 集成测试：状态转换

### T1-2 — `editor/editing/` 目录管理

- `src/services/jobs/editing.py`：
  - 创建 / 销毁 / 状态查询
  - **`touch_editing(job_id)` helper**（§5.4.1）：所有 editing 态 mutation handler 完成业务后统一调用，刷新 `editing_touched_at`
- `src/services/jobs/editing_events.py`（D40）：`emit_editing_event()` helper
- `src/services/jobs/input_validators.py`（D36）：`validate_segment_id()` regex 校验
- `GET /jobs/{id}/editing/segments` 返回 baseline + editing diff（**只读，不刷新 touched_at**）
- `PATCH /jobs/{id}/segments/{sid}` 写 `editing/segments.json` + `touch_editing()` + `emit_editing_event('editing.mutation', mutation_type='text')` + segment_id 校验
- `segment_status.json` 维护

### T1-3 — 视频修改页基础框架

- 路由 `/workspace/{jobId}/edit`
- 顶部栏 + 视频播放器 + Tab 栏
- 翻译修改 Tab：虚拟滚动 + 播放器同步 + 文本编辑（暂不含 TTS 按钮）
- 异常段高亮（读 `alignment_method` + 三时长字段）

### T1-4 — 翻译审核页改造

- 抽 `SegmentVirtualList` 共享组件
- `usePlayerSegmentSync` hook（翻译审核页不直接用，留给视频修改页）
- split API 返回新段列表
- 前端 `splice` 原地替换；移除 `window.location.reload()`

### T1-5 — 单段 re-TTS + draft 管理

- `POST /jobs/{id}/segments/{sid}/regenerate-tts` → 产物写 **editing/tts_segments_draft/**（始终新建文件，不涉及 hardlink）
- **入参校验（D36）**：`segment_id` 走 `validate_segment_id()` regex 校验
- 前端"重新合成"按钮 + 成本预览（**按段时长 × 原 TTS 单价，D30**）+ 试听
- **inFlight 锁（D37）**：前端按钮 `disabled={inFlight}`，段 status 本地 `tts_loading`
- 扣费路径复用 shadow credits（同初次 TTS 走同一扣费逻辑）
- **emit `editing.mutation`（mutation_type=tts）事件**（D40）
- 集成测试：re-TTS 不动 baseline tts_segments + `test_regenerate_tts_writes_to_draft` + `test_segment_id_format_validation`

### T1-6 — 一键批量 re-TTS + 音色修改 Tab

- `POST /jobs/{id}/regenerate-all-tts` **异步后台任务（D39）**：提交到 `background_task_system`（[`docs/plans/2026-04-16-background-task-system-plan.md`](2026-04-16-background-task-system-plan.md)），立即返回 `task_id`；前端通过 `GET /jobs/{id}/editing/tts-progress` 或 SSE 收进度
- 循环扫 dirty 段 → 逐段跑 draft
- **关键：单段失败就继续下一段但不自动 retry（D26 延伸）**；最终响应结构 D38：`{task_id, succeeded_count, failed_count, failed_segment_ids[], total}`
- 前端一键按钮 + 成本预览 Modal + 进度条 + 部分失败 Toast
- 音色修改 Tab 复用 VoiceSelectionPanel → 写 `editing/voice_map.json`
- **emit `editing.mutation`（mutation_type=voice 或 tts_batch）事件**（D40）
- 集成测试：`test_regenerate_all_tts_partial_failure`（mock 一段抛 429，断言部分成功响应结构）

### T1-7 — LogViewer server-side 脱敏（D25）

- `src/services/jobs/logs_redactor.py` 接 API
- `GET /jobs/{id}/logs` role-gated redaction
- provider 列表从 registry 动态生成（D31）
- 前端去掉 `isAdmin` 驱动的脱敏逻辑
- 单测：admin 看原文 / 非 admin 看脱敏

### T1-8 — copy_as_new 执行通道（D28 + D34）

- `src/services/jobs/runner_extensions.py`：`submit_job_from_existing_project_dir`
- `src/services/jobs/copy_service.py`：
  - JSON 文件真实复制（`shutil.copy2`）
  - `tts_segments/*.wav` 逐一 `os.link`（D27）
  - 提供 `write_audio_safely` / `apply_draft_segment` 工具（§3.5.1）
  - **两阶段提交封装**（D34）：`create_copy_job_two_phase(source_job_id, copy_display_name)` 函数严格按 §7.8 的 Phase A → Phase B 顺序执行；Phase A 任一步失败自动走 A-rollback
- 副本 Job 创建 + TTL 计算（D23）+ default display_name
- 集成测试：
  - 验证 hardlink 创建成功（`os.stat().st_nlink >= 2`）
  - 副本 commit 从 alignment 起跑
  - 源任务完整性（hash 不变）
  - **hardlink 隔离**：副本覆盖 wav 后源文件 hash 仍 = 原值
  - **两阶段失败恢复**：mock runner.submit 抛异常 → 断言源 editing 态完整保留（对应 `test_copy_as_new_preserves_source_draft_on_runner_failure`）

### T1-9 — editing/commit 全流程（D26）

- overwrite 分支：editing-staging → swap → pipeline
- copy_as_new 分支：调 T1-8
- 失败恢复：回 `editing`，`editor/editing/` 保留
- **守卫测试**：AST 扫 alignment / publish 模块不得调 tts_generator
- E2E：覆盖一次 + 副本一次 + 副本失败后 retry

### T1-10 — editing 闲置 cancel + admin 强制 cancel（D24 + D42）

- `editing_idle_scanner` 接入 cleanup 主循环
- 前端 `(now - editing_touched_at) > 20h` 时显示提示条
- **admin 强制 cancel 按钮（D42 / E4）**：admin 面板任务详情页 editing 态下显示"强制取消修改"按钮，走 `editing/cancel` + 留 audit log `cancelled_by_admin=<admin_user_id>` + emit event `editing.cancelled`(reason=admin_force)
- 守卫测试：`test_editing_touched_at_refresh_on_mutation`（逐端点断言 mutation 刷新、GET 不刷新）
- 集成测试：构造 25h 未 touch 的 editing job → 验证自动 cancel；构造 25h 但 1 小时前刚 PATCH 过的 → 验证不 cancel；admin 强制 cancel → editing/ 删干净 + audit log 写入

### T1-11 — 字幕 / 素材包同步确认

- 验证 publish 阶段使用最新 `segments.json`
- 如果发现依赖 `transcript.json` 的地方读旧数据，修复
- 手动 QA：修改译文 → 重合成 → 下载 SRT / 素材包内容一致

### T1-12 — 守卫测试 + 文档

- `tests/test_post_edit_guards.py`：
  - editing 态禁止再次 enter-edit
  - baseline `tts_segments/` 在 editing 期间 mtime 不变
  - 副本的 `expires_at` 服从 §5.1 公式（含 user_id / root_job_id 约束）
  - display_name 冲突后缀生成
  - logs_redactor 覆盖所有 registry provider 名
  - alignment / publish 模块 AST 不得 call tts_generator
- `CLAUDE.md` 增"视频修改工作流"节

---

## 16. 风险 / 依赖 / 测试

### 16.1 主要风险

1. **editing 态跨会话恢复**：用户关浏览器后 editing 数据保留（DB 状态 + 磁盘 editing 目录）。UI 要明确"上次编辑进行中"。闲置 24h 后自动 cancel。
2. **commit 中途失败**：
   - overwrite：先 staging 再 swap，保 baseline 原子性
   - copy_as_new：源任务完全不动，新副本失败时新 job 标 failed，用户可删
3. **副本并发 TTL 竞争**：`SELECT FOR UPDATE` 保护
4. **付费 API 自动触发**：D26 / T1-6 / T1-9 / T1-12 层层防守
5. **hardlink 污染**（D27）：副本覆盖 wav 时若用 `open('wb')` 会污染源 inode。§3.5.1 规定 unlink+write / `os.replace` 规范 + 两条守卫测试（`test_hardlink_isolation_after_overwrite` / `test_no_raw_open_wb_on_shared_paths`）强制验证
5. **编辑中的产物下载状态**：
   - editing 态：原产物仍可下载（打"你正在修改此任务"小标签）
   - running（edit_generation>0）：下载按钮禁用 + "正在重新生成"
   - 失败回 editing：下载恢复

### 16.2 依赖

- `@tanstack/react-virtual`（若未引入加 dep）
- Alembic migration 已成熟
- Linux `os.link` 天然支持（D27）
- feature flag 新 env var `AIVIDEOTRANS_ENABLE_POST_EDIT` / `NEXT_PUBLIC_ENABLE_POST_EDIT`

### 16.3 测试策略

- **单测**：`text_width` / `compute_copy_expires_at` / `redact_for_user` / 自动起名
- **集成测试**：状态转换 / enter-edit→cancel / enter-edit→commit（overwrite / copy）/ 副本 TTL 全场景
- **E2E**（手动）：
  - 修改 1 段文本 + re-TTS + 覆盖 commit
  - 修改 3 段 + 一键合成 + 副本 commit
  - 修改后放弃 → 验证原产物未动
  - 连续 3 个副本 → 验证第 3 个的 `expires_at` 受 `prev + 24h` 限制
  - 闲置 25h → 自动 cancel
  - 跨用户同 source_content_hash → TTL 不串扰
- **付费 API 审计**：
  - AST 扫 `tts_generator.generate_*` 调用点
  - 手动 code review commit 流程的代码路径
  - CI 集成：任何 PR 触碰 alignment / publish 模块需过 AST 守卫

### 16.4 回归守卫清单

扩展 `tests/test_post_edit_guards.py`：

| 测试 | 断言 |
|-----|------|
| `test_editing_status_reentrancy` | editing 态二次 `enter-edit` 返回 409 |
| `test_baseline_immutability` | editing 期间 `tts_segments/` mtime 不变 |
| `test_copy_ttl_respects_user_and_lineage` | 副本 TTL 只受同 user_id + 同 root_job_id 影响 |
| `test_copy_ttl_select_for_update` | 并发两个 commit 不会读到同一 prev |
| `test_display_name_conflict_suffix` | 冲突时稳定生成 `_xxxx` |
| `test_logs_redactor_covers_registry_providers` | registry 增新 provider 后脱敏仍覆盖 |
| `test_commit_pipeline_no_tts_generator_call` | alignment / publish AST 不含 `tts_generator.generate_*` |
| `test_editing_idle_scanner` | 25h editing job 被自动 cancel |
| `test_hardlink_copy_preserves_source` | 副本 hardlink 后源 tts_segments 可独立修改不影响副本 |
| `test_hardlink_isolation_after_overwrite` | 副本用 `apply_draft_segment` 覆盖 wav 后，源任务同名 wav 的 sha256 仍 = 覆盖前值（证明 hardlink 被正确解除，未污染源 inode） |
| `test_no_raw_open_wb_on_shared_paths` | AST 扫 `copy_service.py` / `editing.py` / `tts/regenerate.py` 等模块，禁止出现 `open(..., 'wb')` 直接写 `editor/tts_segments/` 下的路径（只能走 `write_audio_safely` / `apply_draft_segment`）|
| `test_editing_touched_at_refresh_on_mutation` | 逐个调用 §5.4.1 清单里的每个 mutation 端点，断言调用后 `editing_touched_at` 已刷新；同时断言 `GET` 类只读端点**不**刷新（反向测试）|
| `test_copy_as_new_preserves_source_draft_on_runner_failure` | mock `runner.submit_job_from_existing_project_dir` 抛异常；断言调用返回失败后：源 `status='editing'` / `editor/editing/` 文件 hash 完整不变 / `editing_touched_at` 不变 / 新 project_dir 已被清理 / 新 Job 记录不存在 |
| `test_commit_overwrite_happy_path` | editing → commit overwrite → 成功。断言 pipeline 跑完后产物更新、`edit_generation=1`、`editing_touched_at=NULL` |
| `test_commit_copy_as_new_happy_path` | editing → commit copy_as_new → 成功。断言新副本 `status='succeeded'`、源 `status='succeeded'`、副本 `copy_of_job_id=源` / `root_job_id=源的 root` |
| `test_regenerate_tts_writes_to_draft` | 单段 re-TTS 产物只出现在 `editing/tts_segments_draft/{sid}.wav`；baseline `tts_segments/{sid}.wav` mtime/sha 不变 |
| `test_regenerate_all_tts_partial_failure` | mock 3 段中 1 段 TTS 抛 429，断言响应 `{succeeded_count:2, failed_count:1, failed_segment_ids:['seg_042']}`；成功段的 draft 文件存在 |
| `test_segment_id_format_validation` | PATCH / re-TTS / accept / revert / split 端点的 `segment_id` 不合法（含 `..` / `/` / 长度 > 64 / 大写）时返回 400；不进入业务逻辑 |
| `test_editing_not_marked_stale_by_reaper` | 构造 editing 态 job 且无 worker 进程 → 跑 `reap_stale_active_jobs()` → 断言 status 仍 `editing`、未被标 failed。**关键**：防止 `editing ∈ ACTIVE_JOB_STATUSES` 被 reap stale 误杀（见 T0-1 清单 §0）|
| `test_editing_included_in_active_statuses` | 前后端 `ACTIVE_JOB_STATUSES` 都包含 `editing`；前后端 `WORKER_ACTIVE_STATUSES`（若前端也引入）仅含 `queued/running` |

---

## 17. 迁移与回滚

### 17.1 上线顺序

**Phase 0**（一次性 migration + 代码上线）：
1. DB migration（加字段 + 回填）
2. 代码上线（JobStatus 加 editing / 状态触点全部兼容 / UX 改造）
3. feature flag `AIVIDEOTRANS_ENABLE_POST_EDIT=false`（默认关）
4. 回归验收：老功能行为不变、新 UX 可见

**⚠ 硬约束（CodeX 审核 2026-04-18 T0-2 完成时锁定）：在 migration 015 成功 apply 到目标 DB 之前，禁止以下操作：**
- 启动或重启 Gateway / Job API 进程（`gateway/models.py` 已声明新列，连到未迁移的 DB 会在 ORM 查询时 schema mismatch 异常）
- 部署代码到任何生产 / staging 环境
- 运行依赖现有 `jobs` 表结构的 DB 集成测试

**允许的验证手段**（T0-2 / T0-3 开发期）：
- Python 纯单测（不触 DB）
- TypeScript 类型检查 / lint
- 纯前端 / mapper 逻辑测试
- `alembic upgrade head` 的 dry-run（`alembic upgrade head --sql`）

**Phase 1**：
1. 代码上线（editing 端点 / 视频修改页 / 翻译审核页改造 / server-side 脱敏）
2. feature flag 仍 false，先内部开 `true` 跑 dogfood
3. 观察一周无回归 → 灰度开 `true`
4. 全量开启

### 17.2 回滚策略

- Phase 0：feature flag 已经 false，改动只影响 UX（可单独 revert UX commit）；migration 可保留字段不用
- Phase 1 代码可回滚（feature flag 关即可让新端点 404）
- 若已有 editing 态 job：回滚前跑一次 `editing_idle_scanner` 强制全部回 `succeeded`

### 17.3 Phase 0 Apply Runbook（T0-7 收尾后）

**目标**：把 Phase 0 代码层产物真正 apply 到运行环境 + 做 smoke 回归，之后才开 Phase 1。**CodeX 审核放行条件**。

**执行顺序（严格）：**

**Step 1 — Dry-run migration 015**
```bash
# 本地 dev（Windows）
.venv/Scripts/python -m alembic -c gateway/alembic.ini upgrade head --sql > /tmp/migration-015.sql
# 检查 SQL：7 ADD COLUMN + 4 CREATE INDEX + 2 批次 UPDATE (root_job_id / expires_at)
```

**Step 2 — Apply migration 到目标 DB**
- **本地 dev**：`.venv/Scripts/python -m alembic -c gateway/alembic.ini upgrade head`
- **生产（Linux, Docker）**：
  ```bash
  # 1. 通过 Via-154 脚本部署代码到主机
  D:\daili\scripts\Upload-Via-154.cmd
  D:\daili\scripts\Deploy-Via-154.cmd
  # 2. 在 gateway 容器内跑 alembic（此时 gateway 进程仍是旧镜像，但这是 gateway 的 ORM，DDL 只动表）
  docker exec <gateway-container> alembic -c alembic.ini upgrade head
  # 3. 重启 gateway / job-api（让 ORM 重新加载 schema）
  docker restart aivideotrans-gateway aivideotrans-app
  ```

**Step 3 — DB 层正确性断言**
```sql
-- 在 psql 里执行
\d jobs  -- 看 7 新列 + 4 新索引都存在

-- 回填正确
SELECT COUNT(*) FROM jobs WHERE root_job_id IS NULL;
-- 预期: 0（所有行都被回填 root_job_id = job_id）

SELECT COUNT(*) FROM jobs
  WHERE status NOT IN ('queued', 'running') AND expires_at IS NULL;
-- 预期: 0（非活跃任务都填了 expires_at）

SELECT COUNT(*) FROM jobs
  WHERE status IN ('queued', 'running') AND expires_at IS NULL;
-- 预期: 所有活跃任务的数量（按设计留 NULL）

-- Alembic head 指针对
SELECT version_num FROM alembic_version;
-- 预期: '015_post_edit_fields'
```

**Step 4 — Gateway / Job API 冷启动不 schema mismatch**
- `docker logs aivideotrans-gateway --tail 100` 无 ORM 异常
- `curl https://<host>/health` 返回 200

**Step 5 — Smoke Checklist（§17.4）全部 ✓**

**Step 6 — landing-ready**：任一条失败立即走 §17.2 回滚（`alembic downgrade -1`）

### 17.4 Smoke Checklist（Phase 0 landing gate）

按顺序跑，任何 ❌ 项 = **rollback trigger**。

**A. 老路径完全不回归（feature flag 默认 off）**
- [ ] 登录 → 列表页渲染 + 历史任务全部显示
- [ ] 创建新翻译任务（YouTube URL）→ 状态从 `queued` 正常推进到 `running`
- [ ] 任务跑完 → `succeeded` + 结果页可播放 + 下载按钮工作
- [ ] Cancel 一个 `running` 任务 → 状态变 `cancelled`
- [ ] 等待审核的任务（如 Studio）→ "进入工作台" CTA 跳转正常，审核流程未改变

**B. 新字段 / 新 UI 渲染不破坏（无 editing 数据也不能炸）**
- [ ] 列表页过期倒计时颜色正确分级：`>3d` 灰 / `1-3d` 橙 / `<1d` 红（找几个不同年龄的历史任务目测）
- [ ] 任务卡 badge：`succeeded`=绿 / `running`=青 / `failed`=红 / `waiting_for_review`=橙（**不应**看到紫色"修改中"，因为没 editing 数据）
- [ ] 任务卡 title 显示 `display_name` 或 fallback（NULL 时走 `buildJobTitle`）
- [ ] 任务卡右上角**不显示**"修改"图标按钮（feature flag off）
- [ ] 直接访问 `https://<host>/workspace/<id>/edit` → 404（路由未创建）
- [ ] `workspace/{id}` 页面：status badge 和 header 正常，无 "isEditing" 专属卡片出现

**C. Gateway 层契约**
- [ ] 并发限制功能正常（创建 N+1 个任务会 409）—— 现在 SQL 多了 "editing" 但没 editing job，行为等同
- [ ] 扫 gateway 日志 `docker logs aivideotrans-gateway --tail 200` 无 SQLAlchemy `column "xxx" does not exist`

**D. Cleanup 非侵入**
- [ ] `docker exec aivideotrans-app python -c "from src.services.web_ui.cleanup import cleanup_expired_projects; print(cleanup_expired_projects())"` 返回 summary（可能是空 list）；无 Exception
- [ ] idle_scanner 默认 no-op callback 不触发任何状态转换（scan 完 log 写 `candidates=0 cancelled=0`）

**E. 前端基础**
- [ ] 浏览器 devtools Network 面板 `/api/jobs` 返回 JSON 新增 7 字段（`display_name` 等）
- [ ] Console 无 error

---

### 17.5 Rollback Triggers（任一触发即回滚）

- §17.3 Step 4 gateway 启动 schema 异常
- §17.4 A 任何一条 ❌（老路径 break）
- §17.4 C Gateway 日志出现 ORM schema error
- 用户报告"Cannot create new job" / "任务卡不显示"

**回滚命令：**
```bash
# 生产
docker exec <gateway-container> alembic -c alembic.ini downgrade -1
docker restart aivideotrans-gateway aivideotrans-app
# 前端代码也 revert 到 pre-T0-2 commit（因为 mapper / types 依赖新字段）
git revert <T0-2..T0-7 commits>
D:\daili\scripts\Deploy-Via-154.cmd
```

**回滚后验证**：`alembic current` 显示 `014_background_tasks`，Gateway 启动无异常，老功能正常。

---

## 18. 已锁定的补充决策汇总

以下决策在 v2 评审后并入 §1 决策表，此处汇总便于 review：

- D21（状态机简化）：只新增 `editing`，复用 `running`，不造 `processing`
- D22（editing 目录）：`editor/editing/` 子目录集中所有可变文件
- D23（TTL 作用域）：`user_id` + `root_job_id` + `SELECT FOR UPDATE`
- D24（闲置清理）：`editing_touched_at` + 24h 自动 cancel
- D25（日志脱敏）：server-side + role gate
- D26（commit 失败）：禁止自动重入 TTS + AST 守卫
- D27（副本产物）：Linux `os.link` hardlink
- D28（copy_as_new 执行通道）：`submit_job_from_existing_project_dir(start_stage='alignment')`
- D29（feature flag）：UI + 后端端点双端 gate
- D30（re-TTS 定价）：**复用原始 TTS 定价**（废弃独立单价字段）
- D31（脱敏动态化）：provider 列表从 registry 动态生成
- D32（实施分段）：Phase 0 前置 + Phase 1 主方案
- D33（重合成 badge）：`edit_generation > 0` 时副标题 "正在重合成 · 第 N 次修改"
- D34（copy_as_new 两阶段提交，CodeX 二审 P1）：Phase A 准备 + Phase B 清源；A 失败整体回滚不动源 draft
- D24（修订）：`editing_started_at` 改名 `editing_touched_at`，所有 mutation 必须 `touch_editing()` 刷新，"闲置 24h" 文案语义与字段对齐

**autoplan v3 补充决策（2026-04-18 Phase 1 CEO + Phase 2 Design + Phase 3 Eng 内部评审）：**

- D35（Phase B 告警 channel，CEO §2）：commit_phase_b_failed 写 job_events level=critical
- D36（segment_id 入参校验，CEO §3）：regex `^[a-z0-9_]{1,64}$` 深度防御
- D37（re-TTS inFlight 锁，CEO §4）：前端防重复扣费
- D38（批量 re-TTS 部分失败响应，CEO §4）：`{succeeded_count, failed_count, failed_segment_ids[]}`
- D39（批量 re-TTS 异步化，CEO §7）：复用 background_task_system
- D40（editing 生命周期事件，CEO §8）：7 种事件类型，editing_events.py helper
- D41（migration 分批，CEO §9）：500 条/批避免锁表
- D42（admin 强制 cancel editing，SELECTIVE EXPANSION E4）：运维场景
- D43（任务卡"修改"直达按钮，SELECTIVE EXPANSION E6）：UX 降低门槛
- D44（异常段顶部统计横幅，Design §1）：⚠ 有 N 段时长异常
- D45（enter-edit loading 反馈，Design §3）："正在准备修改环境..."
- D46（running badge 青色对齐项目主色，Design §5）：#06B6D4 代替蓝色
- D47（响应式 + a11y 规范，Design §6）：§7.9 / §7.10 三视口 + WCAG 2.1 AA

---

## 19. Autoplan 决策审计（Decision Audit Trail）

/autoplan 2026-04-18 Phase 1 CEO + Phase 2 Design + Phase 3 Eng 自动决策完整记录。每条对应一个可追溯的原则 + 理由。

| # | Phase | 决策 | 采纳 | 原则 | 理由 | 拒绝方案 |
|---|-------|------|------|------|------|---------|
| 1 | CEO 0C-bis | 选方案 A（editing 态 + two-phase） | ✅ | P1 完整性 + P3 pragmatic | 需求精准、复用高 | B 重跑违背需求；C 过度工程 |
| 2 | CEO 0F | Mode = SELECTIVE EXPANSION | ✅ | 上下文默认 | feature enhancement → SELECTIVE | — |
| 3 | CEO 0D-E1 | 副本链 tree/timeline 可视化 | ❌ Reject | P4 DRY | D19 已拒绝，保持 | — |
| 4 | CEO 0D-E2 | editing 多步 undo stack | ⏸ Defer to TODOS.md | P5 overeng | 段级撤销已够，多步栈非 V1 | — |
| 5 | CEO 0D-E3 | 跨 tab 编辑 lock | ⏸ Defer | P3 pragmatic | 单人项目非首要 | — |
| 6 | CEO 0D-E4 | admin 强制 cancel editing | ✅ Approve | P2 boil lakes | blast radius + <1d CC | — |
| 7 | CEO 0D-E5 | segment-level diff 审计日志 | ⏸ Defer | P3 pragmatic | 非 V1 必需 | — |
| 8 | CEO 0D-E6 | 任务卡"修改"直达按钮 | ✅ Approve | P2 boil lakes | UX 改进 | — |
| 9 | CEO §2 | Phase B 失败告警 channel | ✅ 加 D35 | P5 explicit | 闭合 Error & Rescue gap | — |
| 10 | CEO §3 | segment_id regex 校验 | ✅ 加 D36 | P1 + P5 | 深度防御 + 不依赖 DB 兜底 | — |
| 11 | CEO §4 | re-TTS inFlight 锁 | ✅ 加 D37 | P5 explicit | 前端常规防重复提交 | — |
| 12 | CEO §4 | 批量 re-TTS 部分失败响应 | ✅ 加 D38 | P1 完整性 | 用户感知部分成功 | — |
| 13 | CEO §7 | 批量 re-TTS 异步化 | ✅ 加 D39 | P4 DRY + P5 | 复用 background_task_system | 同步阻塞 150s+ 不可接受 |
| 14 | CEO §8 | editing 生命周期事件 | ✅ 加 D40 | P2 boil lakes | observability is scope | — |
| 15 | CEO §9 | migration 分批回填 | ✅ 加 D41 | P5 explicit | 10k+ 任务表避免锁表 | — |
| 16 | Design §1 | 异常段顶部横幅 | ✅ 加 D44 | P1 完整性 | 信息架构 gap | — |
| 17 | Design §2 | 交互状态矩阵（§7.10） | ✅ 加 | P1 完整性 | 10 种交互 × 5 状态全覆盖 | — |
| 18 | Design §3 | enter-edit loading 反馈 | ✅ 加 D45 | P5 explicit | 用户旅程过渡清晰 | — |
| 19 | Design §5 | running badge 青色对齐项目 | ✅ 加 D46 | P5 explicit | 项目主色系是紫+青 | 蓝色不在设计系统 |
| 20 | Design §6 | 响应式 + a11y 规范 | ✅ 加 D47 | P1 完整性 + CLAUDE.md 约定 | 三视口 + WCAG 2.1 AA | — |
| 21 | Eng §6 | 补 5 条守卫测试 | ✅ 加 | P1 完整性 | commit happy path / re-TTS draft / 批量部分失败 / segment_id 校验 | — |

**Lake Score: 21/21**（所有决策选 complete option；defer 仅用于明显超出 V1 scope 的项）

**Taste Decisions 最终 Gate 呈现**（reasonable people could disagree）：
- E4 admin 强制 cancel（approve）—— 可争议点：是否真需要这个 admin UI？我倾向 approve
- E6 卡片直达"修改"按钮（approve）—— 可争议点："..."菜单够用吗？我倾向 approve 降低门槛
- D39 批量 re-TTS 异步化 vs 同步 —— 可争议点：单次 ≤ 5 段的情况下同步就够了吗？我倾向异步因为 UX 更可控

---

## 20. GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review`（via /autoplan）| Scope & strategy | 1 | **clean** | SELECTIVE EXPANSION，2 expansions accepted / 3 deferred to TODOS，8 sections 发现全部闭合为 D35-D41 |
| Codex Review | `/codex review` | Independent 2nd opinion | 3（外部 CodeX CLI 人工审核）| **clean** | 二审提 P1×2 + 三审提 P2×1 已全部闭合（D21/D23/D28/D34/D36 等）|
| Eng Review | `/plan-eng-review`（via /autoplan）| Architecture & tests (required) | 1 | **clean** | 0 critical gaps；补 5 条守卫测试；test plan artifact 已写入 `~/.gstack/projects/AIVideoTrans_Codex_web_mvp/` |
| Design Review | `/plan-design-review`（via /autoplan）| UI/UX gaps | 1 | **clean** | 初始 6/10 → 9/10；补响应式 + a11y + 交互状态矩阵 + 异常段横幅 |

- **CODEX:** 3 轮外部 CodeX CLI 人工审核（用户复制 CodeX 意见）：一审 C1-C3+H1-H3 全修；二审 P1×2 全修；三审 P2×1 全修
- **CROSS-MODEL:** Claude（本 session）+ CodeX（外部）高度一致，无 tension point
- **UNRESOLVED:** 0
- **VERDICT:** CEO + ENG + DESIGN CLEARED — ready to implement。Phase 0 前置可立即开工（T0-1 → T0-7）。Phase 1 在 Phase 0 稳固后启动，全程 feature flag 保护。

---

以上。Phase 0 与 Phase 1 各自独立可交付可回滚；Phase 0 完成后系统对用户透明，Phase 1 feature flag 灰度上线。

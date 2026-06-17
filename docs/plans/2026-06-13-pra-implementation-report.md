# PR-A（多语言互翻地基）实施汇报 — 供合并评估

> 生成于 2026-06-14。覆盖范围：多语言互翻方案 v3（`docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md`）下 **PR-A** 的全部实施——上一会话的 **part 1（已合 main）** + **part 2（已在 `codex/ml-pr-a-part2-rebase` 基于 main 重放）**。
> 原始实施分支：`claude/ml-pr-a`（worktree `D:\Claude\worktrees\ml-pr-a`，HEAD = `d43a0e6e` + 未提交 part-2）。当前合并准备分支：`codex/ml-pr-a-part2-rebase`。

---

## 0. 执行摘要 / 合并就绪度结论

- **PR-A = 多语言互翻的"门禁 + 地基"切片，不是功能上线。** 它建好了语言对注册表、Job 语言字段持久化、entitlement 闸、create-path 校验、双层能力 gate、facts 端点、cost 维度、前端入口，以及一道**代码级硬闸**。
- **默认方向 英文→中文 零回归**：所有非默认逻辑都 gated，默认路径行为与改动前一致（多重证明）。
- **中文→英文（zh-CN→en）目前不可用、也建不出来**：`pipeline_ready=False` 代码常量 → create-path 返 **409**，**连 admin 翻开关都拦**。这是 by design（管线尚未适配，详见 §6）。
- **质量**：经**内部 37-agent 对抗评审** + **CodeX 外部评审** + **Codex rebase 复核**，发现的真问题已修复；定向回归全绿；前端 tsc 0 错、eslint 0 error（仅既有 warning）。
- **合并就绪度**：代码层面可合（作为 flag-gated 地基，与 part-1 同模式）。合并前建议：① 显式 pathspec stage/commit；② 跑一次干净全量 CI；③ 前端浏览器 smoke；④ 项目主 review。需明确接受的风险点：zh→en 管线未就绪（已被代码硬闸挡住，误开 flag 出不了事）。

---

## 1. Part 1 — 基础切片（上一会话，**已 cherry-pick 进 main `38336734`**）

worktree commit `d43a0e6e`，8 文件 +930 行：

| 文件 | 行 | 内容 |
|---|---:|---|
| `src/services/language_registry.py` | +225 | **新建**。`LanguagePairProfile`（含内部付费能力位 `adapted_paid_capabilities`）+ `SUPPORTED_LANGUAGE_PAIRS`（`en->zh-CN` 满集 / `zh-CN->en` **空集** → §4 付费 fail-closed）+ `normalize_language`（别名→canonical，未知→None）+ `resolve_language_pair`（未知→None，**不静默回默认**）+ `make_pair_key` + 默认常量 |
| `gateway/alembic/versions/036_job_language_fields.py` | +72 | **新建**。jobs 表加 `source_language`/`target_language`/`language_pair`，NOT NULL + server_default `en`/`zh-CN`/`en->zh-CN`；down_revision=`035_anonymous_preview`，additive，downgrade 全 drop |
| `gateway/models.py` | +17 | gateway `Job` 模型三字段 lockstep |
| `src/services/jobs/models.py` | +22 | Job API `JobRecord` 三字段 lockstep + to_dict/from_dict |
| `gateway/job_intercept.py` | +24 | 三构造点：create-path `Job()` 用默认常量、`copy_as_new` 从源行复制、`metering_snapshot` 加 `language_pair` |
| `tests/test_language_registry.py` | +184 | **20 个测试**：normalize/resolve/能力位/默认对 |
| `tests/test_job_language_fields.py` | +381 | **AST 守卫**：lockstep（registry == models == migration）、036 迁移契约、JobRecord round-trip、两个 `Job()` 构造点逐 kwarg、metering dict 含 language_pair、匿名白名单不含语言字段 |
| `tests/test_gateway_editing_commit_sync.py` | +5 | `_FakeJobRow` 补三字段 |

> part-1 当时 CodeX 外审：**无 P1**，2 条 P2 测试加固已采纳。

---

## 2. Part 2 — §1–§7 + 硬闸（本会话，**未提交**）

15 tracked 改动 + 4 新文件，tracked diff 约 **+562 / −28 行**（基于最新 main 重放后）。

### §1 admin 旋钮 — `gateway/admin_settings.py` (+20)
- `language_pairs_enabled: StrictBool = False`（主开关；**StrictBool 防字符串 "1"/"on" 被松散解析误开付费特性**）
- `language_pairs_user_allowlist_enabled: StrictBool = True`
- `language_pairs_allowlist: list[str] = []`（user_id 口径，对齐 cosyvoice 克隆白名单）

### §2 entitlements — `gateway/entitlements.py` (+78)
- `get_effective_allowed_language_pairs(user, *, admin=None) -> list[str]`：
  - 默认对 `en->zh-CN` **恒含**（零回归，不受任何配置影响）
  - `zh-CN->en` 仅当 `language_pairs_enabled=True` **且**（白名单关 → 所有登录用户 / 或 admin / 或 user.id ∈ allowlist）
  - admin_settings 读不到 → **fail-closed**，只返默认对

### §3 create-path 校验 + 非交互 override + Job API 持久化 — `gateway/job_intercept.py` (+166)、`src/services/jobs/service.py` (+18)、`src/services/jobs/api.py`
create-path 判定链（按顺序）：
1. 解析 `source_language`/`target_language`；**任一缺失 → 默认对**（OR 逻辑，零回归；修了 CodeX/内部评审抓的 AND bug）
2. `resolve_language_pair` → None → **400 `unsupported_language_pair`**
3. entitlement 不在列 → **403 `language_pair_not_allowed`**
4. **`pipeline_ready=False` → 409 `language_pair_not_yet_available`**（代码硬闸，连 admin 拦）
5. canonical pair 注入 forwarded body（让 Job API JobRecord 落规范化值）
6. **requires_review override**：仅 `service_mode=="studio"` 且 pair ∈ 非交互集（zh→en）→ `False`（D1 非交互 lane；收窄到 studio 是 CodeX P2 修复，避免破坏 smart 的 review-gated auto-review）
7. `Job()` 用 `resolved_pair.*`（替代 part-1 的默认常量；AST 守卫已同步更新）

Job API 持久化：`submit_job` 加 `source_language`/`target_language` 参数 → 经 registry 重解析 → `JobRecord` 落三字段。**这道补漏很关键**：否则 §4 gate 会对所有 job 读到默认 `en->zh-CN`、误放行 zh→en 的 post-edit。

### §4 双层能力 gate — `src/services/jobs/api.py` (+59)
- helper `_require_language_pair_capability(record, capability)`：读 `record.language_pair` → 查 `adapted_paid_capabilities`，空集（zh→en）→ `JobConflictError`(→409)；未知/legacy pair → 回默认满集（零回归）
- 两个 wrapper `_gate_pair_post_edit` / `_gate_pair_suggest_split`
- **应用到 16 个 editing 写路径**（15 个 post_edit gate + 1 个 suggest_split gate；GET speakers 列表保持 read-only 不 gate）：enter-edit / editing-commit / segments-split / split-many / suggest-split / segments-update / segments-status / regenerate-tts / accept-draft / discard-draft / regenerate-all-tts / regenerate-selected-tts / voice-map POST / editing-speakers create / retry-profile / revert-unsynced-text

### §5 facts 端点 + job summary 语言字段 — `gateway/job_intercept.py`、`gateway/main.py` (+5)
- `GET /api/language-facts`（`require_auth`，匿名→只返默认）：按 entitlement 过滤，每条带 `pair_key`/`label`（中文）/`is_default`/`pipeline_ready`/`workflow_capabilities`
  - **D5**：`workflow_capabilities`（展示位 `["transcribe","translate","tts","subtitles","jianying"]`）与内部 `adapted_paid_capabilities` **不复用常量名**
- `_merge_gateway_job_metadata`：把 PG 行三字段 overlay 进 job summary 响应

### §6 cost 按 pair 聚合 — `gateway/cost_management.py` (+36)
- 窗口级 `cost_per_minute_by_pair`（用 PG `job.language_pair`，对应 plan §11.7 / Phase 8 观测）+ 每 job payload 加 `language_pair`
- **刻意不做 per-row dataclass 维度**（一任务=一 pair，per-row 冗余，KISS/YAGNI）
- Rebase 复核修正：不再从 `services.language_registry` import，改用本地 lockstep fallback 常量，确保 `cost_management` 可独立导入，不依赖 `src/` path 注入顺序。

### §7 前端
| 文件 | 行 | 内容 |
|---|---:|---|
| `types/jobs.ts` | +13 | `CreateTranslationJobInput` + `JobSummary` 加语言字段 |
| `types/api.ts` | +5 | `ApiJobRecord` 加三字段 |
| `lib/api/mappers.ts` | +3 | `toJobSummary` 映射 |
| `lib/api/jobs.ts` | +9 | `submitTranslationJob` 仅在非默认 pair 才发语言字段 |
| `lib/api/languageFacts.ts` | **新** | facts client（fail-closed 回默认） |
| `components/workspace/TranslationForm.tsx` | +52 | 语言方向 selector（`facts.length>1` 才显示；非就绪 pair 标「即将上线」+ **disabled**；提交护栏：非就绪绝不发语言字段） |
| `app/(app)/admin/settings/page.tsx` | +76 | 「多语言支持（内测·管线未就绪勿在生产开启）」SettingSection，3 旋钮 + 全量 POST 契约 + ⚠️ 警示 |

### 硬闸（pipeline_ready，本会话最后据 CodeX P1 加的）— `language_registry.py` (+11) 等
- `LanguagePairProfile.pipeline_ready`（**代码常量**：`en->zh-CN`=True / `zh-CN->en`=False）
- create-path 非就绪 → 409（见 §3 步骤 4）；facts 透出；前端 disable + 提交护栏
- **意义**：把"别在管线就绪前开 flag"从**靠运维纪律**升级成**靠代码**——翻 admin 开关绕不过，解禁须改 registry 常量走 PR

### 测试 — `tests/test_language_pairs_part2.py`（**新，24 个测试**）+ 更新 part-1 两条 AST 守卫
覆盖：§2 entitlements（8）、§4 能力 gate（6）、§3 create-path（400/403/409/partial-input/override-forward 守卫）、§5 facts（anon/admin/非白名单/pipeline_ready）、§6 cost 守卫、注册表 pipeline_ready flag。

---

## 3. 评审历程

### 内部 37-agent 对抗评审（4 维度 × 逐条 adversarial verify）
- **3 个真问题，全修**：① partial-input `and`→`or`（CRITICAL，只填一个语言字段会误 400）；② §4 缺 7 个 mutation gate（CRITICAL/D4 防御纵深）；③ 缺登录非白名单用户的 facts 测试。
- 余 30 findings 正确驳回（多条把"正确的 fail-closed/零回归实现"误报为缺陷）。

### CodeX 外部评审（codex-cli 0.139.0，`--uncommitted`，xhigh）
- **[P2] 已修**：`requires_review` override 原对所有 service_mode 生效 → 会清掉 `smart` 的 review flag、破坏 Smart 的 review-gated auto-review。**收窄到 `service_mode=="studio"`**。
- **[P1] 已加固为代码硬闸**：CodeX 实测确认端到端管线仍写死 GA 方向（`process.py _enforce_english_source_language`、Gemini 提示词 en→zh、`cn_text`/`zh-CN` target），zh→en 跑不通。→ 新增 `pipeline_ready` 代码硬闸（见上）。
- 两道评审一致确认：gating / fail-closed / 零回归机制本身正确。

### Codex rebase 复核（2026-06-14）
- **补齐 D4 漏 gate**：`regenerate-selected-tts`、`editing/speakers` POST create、`editing/speakers/{id}/retry-profile`、`editing/revert-unsynced-text` 加 `_gate_pair_post_edit`；`GET editing/speakers` 保持 read-only。
- **修复 import 顺序耦合**：`cost_management.py` 不再依赖其它 gateway 模块先注入 `src/` 到 `sys.path`。
- **文案对齐硬闸事实**：admin 多语言说明改为“开启主开关只展示即将上线入口，创建仍被后端 409 拦截”。
- **基于最新 main 重放**：part-2 已落到 `codex/ml-pr-a-part2-rebase`，同时保留 main 后续匿名 Express per-mode admin 旋钮。

---

## 4. 测试与验证汇总

| 验证项 | 结果 |
|---|---|
| `test_language_pairs_part2.py`（新） | **24 passed** |
| 定向回归组 A（create-job/policy/list-metadata/editing×3/语言守卫/commit-sync） | **250 passed** |
| 定向回归组 B（legacy-cleanup/phase2-download/admin-settings/registry/entitlements/语言守卫） | **159 passed** |
| 硬闸后回归（语言×3 + create-job） | **128 passed** |
| 前端 `tsc --noEmit` | **0 错** |
| 前端 eslint（改动文件） | **0 错**（3 个预存 warning，非本次引入） |
| 运行态 import（cost_management / job_intercept / entitlements） | **OK** |
| Codex rebase 验证：`py_compile` + `cost_management` 单独 import | **OK** |
| Codex rebase 验证：editing/create 定向回归 | **266 passed** |
| Codex rebase 验证：frontend `npx tsc --noEmit` | **0 错** |
| Codex rebase 验证：frontend eslint 改动文件 | **0 errors / 3 existing warnings** |

> 注：因本机存在 ~335 条预存（与本改动无关）的跨文件 database-stub 污染失败，采用**定向 batch** 验证而非全量套件。**干净环境的全量 CI 尚未跑**——列为合并前待办。

---

## 5. 当前状况

- 分支 `claude/ml-pr-a`：part-1 已 commit（`d43a0e6e`，且已在 main `38336734`）；part-2 原始实现仍在该 worktree 未提交。当前合并准备分支为 `codex/ml-pr-a-part2-rebase`，已基于最新 main 重放。
- worktree 干净（评审期间的临时 commit / node_modules junction 均已清理）。
- 文档已落地：本报告 + `2026-06-13-pra-part2-implementation-map.md`（实施地图，已更新为"已实施"+CodeX 处置+部署闸）。

---

## 6. 关键约束 / 部署闸（务必评估）

1. **`language_pairs_enabled` 默认 False，且 zh→en 有代码硬闸**：即使误开 flag + 加白名单，create-path 也 **409**，建不出会烧点数的坏任务。
2. **zh→en 端到端管线尚未适配**（CodeX 实测 + plan v3 列 8 条高危）：source 语言 enforcement、翻译方向、音色池去中文、matchable 原子撤回、CosyVoice fallback fail-closed、字幕 per-script、字符计费、§4 能力位适配。
3. **解禁 zh→en 的唯一路径**：后续管线 PR（PR-W/CD/F）做完 → 改 `pipeline_ready` 常量 → PR → review。**不是翻开关**。

---

## 7. 合并前待办 + 后续路线

**合并前（建议）：**
- [ ] 显式 pathspec stage/commit（勿带入 `.codegraph/`、`.codex_worktrees/`、既有 `tests/test_process_pipeline.py` 本地改动等）
- [ ] 干净环境全量 CI
- [ ] 前端浏览器 smoke（语言方向 selector「即将上线」disable 态 + admin section）
- [ ] 项目主 review，明确接受"zh→en 管线未就绪、由硬闸挡住"

**要真正最小试用 zh→en，还需（后续 PR，非本切片）：**
- PR-W：音色池去中文 + matchable 原子撤回
- PR-CD：翻译方向模板化（zh→en 输出英文）+ source enforcement 改造
- PR-F：字幕 per-script 引擎（CJK 字符级 → Latin 词级）
- 计费语言维度 + CosyVoice fallback fail-closed
- 最后把 `pipeline_ready` 翻 True + 逐 capability 适配 `adapted_paid_capabilities`
- plan v3 对整体估 **4–6 周**

---

*本报告基于 git 实测数据（commit stat / diff stat / 测试计数 / grep 核实）+ 两道评审记录生成，未凭记忆断言管线细节（管线现状引自 CodeX 实测与 plan v3 已核实条目）。*

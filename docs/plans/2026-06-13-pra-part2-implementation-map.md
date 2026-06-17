# PR-A part 2 实施地图（已实施，已基于 main 重放，待 review/commit）

- 状态：**§1–§7 已实施**。原始实现仍在 worktree `claude/ml-pr-a` 未提交；已在当前主仓分支 `codex/ml-pr-a-part2-rebase` 基于最新 `main` 重放并解决与匿名 Express per-mode admin 旋钮的冲突。PR-A part 1 已合入 main（commit `38336734`，cherry-pick 自分支 `claude/ml-pr-a` 的 `d43a0e6`）。
- **实施摘要（2026-06-13/14）：** §1 admin_settings（3 字段）+ §2 entitlements `get_effective_allowed_language_pairs` + §3 create-path 校验（400 unsupported / 403 not-allowed / 409 pipeline-not-ready）+ requires_review override（D1 非交互 lane）+ Job API 持久化（submit_job/JobRecord 落 resolved pair，§4 gate 读得到真值）+ §4 双层能力 gate（16 个编辑写路径：15 post_edit + 1 suggest_split；GET speakers read-only 不 gate）+ §5 `/api/language-facts` + job summary 三字段 + §6 cost `cost_per_minute_by_pair` 窗口聚合 + §7 前端（types/mappers/jobs client/languageFacts client/TranslationForm 语言方向 selector/admin SettingSection）。
- **验证：** 新增 `tests/test_language_pairs_part2.py` + 更新 part-1 AST 守卫；定向回归 `tests/test_language_pairs_part2.py tests/test_job_language_fields.py` **42 passed**；editing/create 回归组 **266 passed**；`py_compile` OK；`cost_management` 单独 import OK；前端 `npx tsc --noEmit` 0 错；改动文件 eslint 0 errors（仅 `TranslationForm.tsx` 既有 3 warnings）。
- **决策记录：** §6 只做窗口级 `cost_per_minute_by_pair`（用 PG `job.language_pair`），不做 per-row dataclass 维度（一任务=一 pair，per-row 冗余，KISS/YAGNI）；§4 用 `JobConflictError`→409（与本文件既有 lifecycle-conflict 映射一致，Gateway 是面向用户的第一道 403 gate）。
- **CodeX 外审（codex-cli 0.139.0, --uncommitted, xhigh）：** 1 P1 + 1 P2，均已处置：
  - **[P2] 已修**——`requires_review` override 原本对所有 service_mode 生效，会清掉 `smart` 的 review flag、破坏 Smart 的 review-gated auto-review 分支。已**收窄到 `service_mode == "studio"`**（express/free 本就 False，no-op；plan D1 本就是 Studio lane）。新增 source 守卫断言 studio scoping。
  - **[P1] 设计性 staging，非 PR-A 代码 bug + 已加固警示**——CodeX 正确指出端到端管线仍写死 GA 方向（`process.py _enforce_english_source_language`、Gemini 提示词 en→zh、`cn_text`/`zh-CN` target），所以 zh-CN→en 任务**跑不通**。但 PR-A 是序列第 1 片，管线支持在后续 PR-W/CD/F；安全闸 = `language_pairs_enabled` **默认 False（StrictBool）**。已把 admin UI 该 section 标题/描述加显式 ⚠️「管线未就绪，勿在生产开启」。
  - **🚩 部署闸（已从"靠纪律"升级为"靠代码"）：** create-path 现有**代码级硬闸**——`LanguagePairProfile.pipeline_ready`（registry 常量，`en->zh-CN`=True / `zh-CN->en`=False）。非就绪 pair 即使 admin 开了 `language_pairs_enabled` 且用户在 allowlist，create-path 也返 **409 `language_pair_not_yet_available`**（在 forward 前返回，连 admin 都拦）。解禁 `zh-CN→en` 必须改 registry 常量走 PR（PR-W/CD/F），**翻 admin 开关无法绕过**。admin 开关只控 facts 可见性 → 前端 selector 显示「即将上线」并 disable，提交端再加护栏（非就绪不发语言字段）。
  - CodeX 与内部 37-agent review 一致确认：gating / fail-closed / 零回归机制本身正确。
- **Codex rebase 复核（2026-06-14）：** 补齐 4 个漏 gate（`regenerate-selected-tts`、`editing/speakers` POST create、`retry-profile`、`revert-unsynced-text`）；`cost_management.py` 改为本地 fallback 常量，消除 `src/` path 注入顺序依赖；admin 多语言文案明确“创建仍被后端 409 拦截”；重放时保留 main 的匿名 Express per-mode admin 设置。
- 执行基线：`docs/plans/2026-06-13-multilingual-mutual-translation-plan-v3.md` §4 PR-A 行 + §5 Phase 1 + §2.2/§2.4/§3 决策框。
- 工作树：原始实现位于 `D:\Claude\worktrees\ml-pr-a`（分支 `claude/ml-pr-a`，已含 part-1 代码）；当前合并准备位于主仓分支 `codex/ml-pr-a-part2-rebase`。
- part 1 已落地的可复用件：`src/services/language_registry.py`（`LanguagePairProfile.adapted_paid_capabilities`、`SUPPORTED_LANGUAGE_PAIRS`={`en->zh-CN` 满集 / `zh-CN->en` 空集}、`normalize_language`、`resolve_language_pair`、`DEFAULT_SOURCE_LANGUAGE/DEFAULT_TARGET_LANGUAGE/DEFAULT_LANGUAGE_PAIR`、`ALL_PAID_CAPABILITIES`、`CAPABILITY_{PROBE,S2,SUGGEST_SPLIT,POST_EDIT}`）；`Job`/`JobRecord` 已带三字段。

## 依赖顺序

`admin_settings 字段 → entitlements 函数 → create-path 校验+requires_review override → 双层 gate(jobs/api.py) → facts 端点+job summary 语言 → cost 聚合 → 前端`。每步带测试 + AST/lockstep 守卫；末尾 CodeX 复审 + 定向回归。

---

## 1. admin_settings（`gateway/admin_settings.py`）

镜像 `free_tier_voiceclone_enabled`(line~129) / `chunked_upload_anonymous_enabled`(line~425) / `cosyvoice_clone_user_allowlist`(line~247) / `express_cosyvoice_auto_clone_*`(line~304/336/591) 模式。

- 在 `AdminSettings` 类（line~430，`chunked_upload_anonymous_daily_gb` 之后）加：
  - `language_pairs_enabled: StrictBool = False`（主开关；**用 StrictBool 防 Pydantic 松散 bool 强转误开付费特性**，见 cosyvoice_clone_general_availability_enabled 注释 line~254-261）。
  - `language_pairs_allowlist: list[str] = []`（user_id 字符串 beta 白名单，镜像 `cosyvoice_clone_user_allowlist`）。
  - `language_pairs_user_allowlist_enabled: StrictBool = True`（白名单模式开关，镜像 `express_cosyvoice_auto_clone_allowlist_enabled`）。
  - （可选）`language_pairs_per_user_daily_cap: int = 10` + `@field_validator` bounds [1,1000]（镜像 express cap，line~591-602）。
- 加 `validate_language_pairs_settings(s)`（line~662-684 区域，仿 `validate_anonymous_express_tts_exclusion`）：enabled+allowlist_enabled+空 allowlist → warn/422。
- 在 `update_admin_settings()`（line~764，`validate_anonymous_express_tts_exclusion(body)` 之后）调用它。
- 持久化：GET `/api/admin/settings`(line~725) `model_dump()`；POST(line~733-766) `save_settings()` 走 `file_lock(SETTINGS_FILE)`+`atomic_write_json`（已就绪，无需改）。SETTINGS_FILE=`$AIVIDEOTRANS_CONFIG_DIR/admin_settings.json`。

## 2. entitlements（`gateway/entitlements.py`）

镜像 `get_effective_allowed_service_modes(user, *, settings=None) -> list[str]`(line~28-105)：plan base → admin 增益 → kill-switch（fail-closed，admin_settings 读不到则按关）。

- 在 line~105 后加 `get_effective_allowed_language_pairs(user: User | None, *, settings=None) -> list[str]`：
  - 恒含 `DEFAULT_LANGUAGE_PAIR`（`en->zh-CN`，零回归）。
  - `zh-CN->en` 仅当：`load_settings().language_pairs_enabled` 为真 **且**（`language_pairs_user_allowlist_enabled` 为假 → 所有登录用户；或为真 → user.id in `language_pairs_allowlist` 或 user 是 admin）。
  - admin_settings 读不到 → 只返默认对（fail-closed）。
  - 返新 list。

## 3. create-path 校验 + requires_review override（`gateway/job_intercept.py`）

`intercept_create_job`（line~1085+）+ `compute_job_policy`（line~421-567）。

- 解析 request_data 后（line~1104-1120 区域）：取 `source_language`/`target_language`，`normalize_language` → `resolve_language_pair`。None → 400 `unsupported_language_pair`。再 `get_effective_allowed_language_pairs(user)` 校验 pair_key 在列；不在 → 403 `language_pair_not_allowed`。存 resolved profile。
- `compute_job_policy(...)` 加参 `resolved_language_pair=None`；各 service_mode 分支构 policy 后：若 `resolved_language_pair.language_pair == "zh-CN->en"` → `policy["requires_review"] = False`（仅此对，解耦 Studio 的 True；§3 决策框非交互 lane）。
- Job() 构造（line~1642-1685，part-1 现写 DEFAULT 常量 line~1663-1665）→ 改写 resolved profile 的 source/target/.language_pair。
- metering_snapshot（line~1714-1723）：用 resolved profile 的 language_pair（此处 `job` 尚未建，别用 `job.language_pair`）。
- copy_as_new（part-1 已从源行复制，无需改）。

## 4. 双层 gate（`src/services/jobs/api.py`）

第二道闸（Gateway 已是第一道）：post_edit / suggest_split 当 job.language_pair 的 `adapted_paid_capabilities` 不含该能力时拒（`zh-CN->en` 空集 → 全拒）。

- handler：enter-edit / editing/commit / segments split / suggest-split / editing mutations（line~1071 附近，**确切行号待定**）。
- 读 JobRecord.language_pair → `SUPPORTED_LANGUAGE_PAIRS[pair].supports_paid_capability(CAPABILITY_POST_EDIT / CAPABILITY_SUGGEST_SPLIT)`，假则按既有错误模式拒（仿 `AVT_ENABLE_POST_EDIT` 404 / `validate_segment_id`）。
- `language_registry` 在此可 import（src 在 path）。

## 5. facts 端点 + job summary 语言（`gateway/main.py` + `gateway/job_intercept.py`）

- `gateway/main.py`（line~675 后，catch-all proxy 前）加 `GET /api/language-facts`（带 auth）：遍历 `SUPPORTED_LANGUAGE_PAIRS`，按 admin/entitlement 过滤 enabled 对，返 `[{pair_key, label, workflow_capabilities:["transcribe","translate","tts","subtitles","jianying"]}]`。**workflow_capabilities 是展示位,≠ 内部 adapted_paid_capabilities（§2.2 D5）**。需给 profile 加 `label`（中文）+ workflow_capabilities 常量（注意：不复用 adapted_paid_capabilities 名）。
- `intercept_list_jobs`/`intercept_get_job` 响应 builder：把 Job 行的 source_language/target_language/language_pair 带进 JSON。

## 6. cost 聚合（`cost_management.py` — 路径待确认，plan 引 line~1023/1197）

- 在 cost_per_minute / by-pair rollup 的 group-by 维度加 `language_pair`（读 metering_snapshot.language_pair，part-1 已写入）。
- admin cost 端点若 surface 这些聚合，同步加维度。

## 7. 前端（`frontend-next/src/`）

- `types/jobs.ts`：`CreateTranslationJobInput` 加 `sourceLanguage?`/`targetLanguage?`（仿 voiceA/voiceB）；`JobSummary` 加 `sourceLanguage?`/`targetLanguage?`/`languagePair?`。
- `lib/api/jobs.ts` `submitTranslationJob`（line~69-152，line~92 后）：input 有则加 `source_language`/`target_language` 到 requestBody（仿 expressAutoVoiceClone 条件加入）。
- `components/workspace/TranslationForm.tsx`：加语言方向 selector，默认「英文 → 中文」；非 GA pair 标「内测」。
- `lib/api/mappers.ts` `toJobSummary`（line~80-114）：映射三字段。
- `app/(app)/admin/settings/page.tsx`：`AdminSettings` interface + `DEFAULT_SETTINGS`（line~158-262）加 `language_pairs_enabled` 等（**全量 POST 契约：所有字段必须进 state，否则 save 时被后端默认静默覆盖**）；「Smart 个人音色策略」section 后（line~942）加「多语言支持」SettingSection（仿 tts_speed_adjustment_enabled 的 toggle+子区 line~691-733）。
- 验证需 dev server（`cd frontend-next && npm run dev`）。

---

## GAPS（探查未覆盖/不全，实施前先补读）

1. **cost_management.py 整面未探**（agent failed）：先 Glob/Grep 定位文件 + 聚合函数 + group-by。
2. **jobs/api.py 确切 handler 行号 + 错误模式**未定：读 api.py post-edit/suggest-split 区段。
3. **create-path 请求 schema**：`CreateTranslationJobInput` 后端是否已有 source/target language 字段，还是要新增解析。
4. entitlements 的 `get_effective_plan_gate` / admin bypass 细节未全读。
5. 前端 `ApiJobRecord`(types/api.ts) 是否已有三字段。
6. 决策：unsupported pair 返 400 vs 403；allowlist 用 user_id vs email（建议 user_id，对齐 cosyvoice）。

## 验收（实施后）

- 新测试：entitlements gate（默认对恒可/zh→en 受 admin+allowlist 控）、create-path 校验（unsupported→4xx、zh→en requires_review=False）、双层 gate（zh→en 拒 post_edit/suggest_split）、facts 端点、cost 按 pair 聚合。
- lockstep/AST 守卫延续 part-1 风格。
- CodeX 外审 + 定向回归（gateway create/editing/metering/cost + 前端 build/lint）。

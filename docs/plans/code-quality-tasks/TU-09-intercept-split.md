# TU-09 · gateway/job_intercept.py route family 拆分

- **目标 / 价值**：`gateway/job_intercept.py` 当前 **6,880 行**（是 800 行上限的 8.6 倍），在过去 6 周内增长了 108%，内部混杂 8 个不同关注点。单函数 `intercept_create_job` 约 1,627 行（`:1565`–`:3192`）。庞大的单文件拖慢审查速度、加剧多 agent 并行改动的合并冲突风险，并让 Gateway 层的业务意图难以追踪。本单元通过将路由分族迁移到各自子模块，把 `job_intercept.py` 降至 **4,500 行以下**，同时保持 `gateway/main.py` 对外路由路径与 API 契约完全不变。
- **关联发现**：STRUCT-02（`job_intercept.py` 6,880 行 +108%；8 关注点混杂）；PRIOR-19（`intercept_create_job` 膨胀 +332%）
- **前置依赖**：建议在 TU-12 / TU-13（改 Job API / Gateway 路由侧）之前执行，避免同区域并发冲突；TU-03 质量护栏脚手架建议先落（pytest 配置 / file-size guard 可量化收尾）。本单元本身不依赖 TU-03 完成。
- **建议分支**：`quality/intercept-split`
- **预估工时**：L（分 4 批迁移，每批约 M；含 contract 测试编写时间）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令：`grep` → `Select-String`、`tail -n` → `Select-Object -Last N`、`test -f` → `Test-Path`、`wc -l` → `(Get-Content file | Measure-Object -Line).Lines`；避免 `<(...)` 进程替换。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **只迁独占函数**：第一阶段每个 family 只迁移该族独占的函数；凡在两个及以上 family 中被引用的 helper，暂留在 `job_intercept.py` 原位，不在 Step 2–5 中提前搬入 `intercept/shared.py`。
- **shared.py 按需建立**：`intercept/shared.py` 不在 Step 6 用候选列表批量迁入；仅当某 helper 在两处以上子模块中**明确**被引用时才移入，以候选清单作参考而非执行清单。
- **main.py 第一 PR 不改导入来源**：Step 2–5 的每个 family PR 里，`gateway/main.py` 继续 `from job_intercept import ...`（通过 re-export 保持兼容），**不**切换为 `from intercept import ...`；等全部 family 拆分稳定、回归验证通过后，再单独 PR 切换导入来源，减少回归面。
- **Step 6 不强制切 main.py**：Step 6 只做 `__init__.py` 收口和按需 `shared.py`，更新 main.py 导入来源是可选后续 PR，而非本单元硬性步骤。
- **contract 测试先行**：Step 1 的 contract 测试套件必须在任何代码迁移前建立并全绿，是后续每步的守门条件。
- **逐族提交，每提交跑一次测试**：Step 5 各子族各自独立 commit + 测试，不批量提交。

---

## 不在本单元范围（out-of-scope）

- 修复 `intercept_create_job` 内部逻辑 bug 或性能问题（PRIOR-19 的深层重构）
- 拆分 `intercept_create_job` 函数内部（体量 ~1,627 行）——只整体搬迁到新子模块，内部不重构
- `gateway/voice_selection_api.py` 的 `_verify_job_ownership` 去重（DRY-07，独立任务）
- `compute_job_policy` 的类型注解补全（TS-05，独立任务）
- `Job create` 入口 Pydantic 验证模型（TS-06，独立任务）
- `intercept_list_jobs` 的全表 SELECT 性能问题（PERF-003，独立任务）
- 前端路由变更

---

## 必守不变量

以下不变量在本单元每次 commit / PR 中必须保持：

1. **read route 不触发 settlement**：`intercept_get_job`（`:3226`）、`intercept_list_jobs`（`:1360`）只做读+mirror 聚合，不调用 `settle_job_credit_ledger` / `mirror_job_terminal_state` / 任何改变 credit/payment 事实的路径。迁移后通过 AST grep 或测试守卫验证。
2. **list/get mirror 不改 payment/credit 事实**：镜像字段写回（`_merge_gateway_job_metadata`）仅聚合展示字段（display_name / tts_model / quality_tier 等），迁移不得意外引入 credit 变更调用。
3. **post-edit whitelist 保持前后端 path parity**：`_POST_EDIT_TRANSITION_SUBPATHS`（`:4991`）和 `_POST_EDIT_SIMPLE_MUTATION_SUBPATHS`（`:4999`）的常量定义在迁移后必须与 `CLAUDE.md` 中记录的端点白名单完全一致；`_is_post_edit_mutation_subpath`（`:5044`）逻辑不变。
4. **admin gate coverage 测试必跑**：`tests/test_admin_gate_coverage.py` 中的 `test_every_admin_route_has_gate_call` 和 `test_admin_route_count_baseline` 每次 commit 后必须通过。
5. **gateway/main.py 路由路径不变**：10 个公开符号（`intercept_create_job` / `intercept_delete_job_v2` / `intercept_get_job` / `intercept_job_subresource` / `intercept_language_facts` / `intercept_list_jobs` / `intercept_rename_job` / `intercept_suggested_copy_name` / `update_job_metering` / `update_source_metadata`）的导入路径只变模块来源，`main.py` 中对外注册的 URL path 字符串绝对不变。
6. **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / LLM / ASR 绝不在 fallback / except / retry / batch 中自动触发；不因迁移把任何 `_settle_smart_clone_reservation_from_job_state` 或 settlement 调用路径意外移入 read-only 路由子模块。
7. **默认测试不接真实外部服务**：迁移后所有测试文件继续 mock 数据库和外部服务，不新增实网络调用。
8. **process.py 走 Option B**：本单元只改 gateway 层，不触碰 `src/pipeline/process.py`。

---

## Step 0 · 确认现状

```bash
# 0-a. 建分支
git switch -c quality/intercept-split

# 0-b. 确认 job_intercept.py 行数（应为 6,880）
wc -l gateway/job_intercept.py

# 0-c. 确认关键公开符号的实际行号（以下列出 spec 值，执行者核对实际值）
grep -n "^async def intercept_list_jobs\|^async def intercept_create_job\|^async def intercept_get_job\|^async def intercept_job_subresource\|^async def intercept_delete_job_v2\|^async def intercept_rename_job\|^async def intercept_suggested_copy_name\|^async def update_source_metadata\|^async def update_job_metering\|^async def intercept_language_facts" gateway/job_intercept.py
# 预期（main 分支实测值，行号如有漂移以本命令输出为准）：
#   intercept_language_facts   :1323
#   intercept_list_jobs        :1360
#   intercept_create_job       :1565
#   intercept_get_job          :3226
#   intercept_job_subresource  :3303
#   intercept_delete_job_v2    :6220
#   intercept_rename_job       :6291
#   intercept_suggested_copy_name :6447
#   update_source_metadata     :6495
#   update_job_metering        :6708

# 0-d. 确认关键内部符号行号
grep -n "^def compute_job_policy\|^def _error_response\|^PLAN_CATALOG\|^def _is_post_edit_mutation_subpath\|^async def _approve_voice_selection_with_quality_sync\|^async def _post_edit_voice_preview_with_policy\|^async def _post_edit_mutation_with_policy\|^async def _continue_with_gateway_lock\|^async def _serve_redacted_logs\|^_POST_EDIT_TRANSITION_SUBPATHS\|^_JIANYING_DRAFT_SUBPATHS\|^_DOWNLOAD_KEY_RE\|^_STREAM_KIND_RE" gateway/job_intercept.py

# 0-e. 确认 main.py 导入块（:133）和路由注册（:694–:735）
grep -n "intercept_\|update_job_metering\|update_source_metadata" gateway/main.py

# 0-f. 现有路由测试基线（必须全绿才能继续）
python -m pytest tests/test_gateway_route_coverage.py tests/test_gateway_route_registration.py tests/test_admin_gate_coverage.py -q
# 预期：全 passed，0 failed

# 0-g. 记录目标子目录（不存在则需在 Step 1 创建）
test -d gateway/intercept && echo "EXISTS" || echo "NOT_YET"
```

**⚠️ 注意**：项目有多个 `.codex_worktrees/` 分支；上述命令在主工作树的 `quality/intercept-split` 分支上执行，不要在 worktree 目录里操作。

---

## Step 1 · 建子包骨架 + contract 测试套件（先测后迁的前提）

**目标**：在迁移任何代码之前，建立一套 **route contract 测试**，覆盖每个 route family 的关键约束。这套测试在迁移前和迁移后都必须通过（是"先红后绿"的 contract 性质，而非"因重构才绿"的实现测试）。

**动作**：

1. 在 `gateway/` 下新建 `intercept/` 子包，仅含 `__init__.py`（空文件，作为包标记）：

   ```bash
   mkdir -p gateway/intercept
   touch gateway/intercept/__init__.py
   ```

2. 新建测试文件 `tests/test_intercept_route_contracts.py`，验证以下 contract（不依赖实现迁移，当前就对 `job_intercept.py` 直接测试）：

   - **read-family（list / get）不调用 settlement 相关函数**：用 AST 扫描或 mock 验证 `intercept_list_jobs` 和 `intercept_get_job` 的调用链中不出现 `settle_job_credit_ledger` / `mirror_job_terminal_state` 等符号。
   - **post-edit whitelist 完整性**：`_is_post_edit_mutation_subpath` 对 `CLAUDE.md` 中记录的全部白名单路径返回 `True`，对一组非白名单路径返回 `False`。
   - **admin gate：read-only subresource 不触发 gate**：`intercept_get_job` 路径不经过 admin-only guard。
   - **metering / source-metadata 端点无 auth 依赖**（内部调用，无 `require_auth`，只有 `_require_internal_access`）：从 `main.py` 路由注册中验证依赖项。
   - **公开符号名称稳定性**：从 `job_intercept.py` / 迁移后 `intercept/` 包中能 `import` 到 10 个公开符号；`main.py` 的导入行为不变。

3. 在测试文件顶部复用已有的 `database` stub 模式（参考 `test_gateway_route_coverage.py:16-31`）。

**文件**：
- `gateway/intercept/__init__.py`（新建，空文件）
- `tests/test_intercept_route_contracts.py`（新建）

**该步验收**：

```bash
# contract 测试在迁移前必须全部通过（当前实现）
python -m pytest tests/test_intercept_route_contracts.py -v
# 预期：全 passed，0 failed，0 error

# 确认子包存在
test -f gateway/intercept/__init__.py && echo "OK"

# 确认 job_intercept.py 行数未变（本步不改实现）
wc -l gateway/job_intercept.py
# 预期：6880（或与 Step 0 一致）
```

**commit 边界**：

```bash
git commit -- gateway/intercept/__init__.py tests/test_intercept_route_contracts.py \
  -m "test: add route contract tests for job_intercept route families (pre-split)"
```

---

## Step 2 · 迁移第一批：job-read family（`intercept_list_jobs` + `intercept_get_job` + `intercept_language_facts`）

**目标**：把纯读（无 settlement，无 state transition）的三个路由及其专属帮助函数迁移到 `gateway/intercept/job_read.py`。

**涉及符号（以实际行号为准，下同）**：
- `intercept_language_facts`（`:1323`）：返回语言对元数据，纯读，无 DB 写。
- `intercept_list_jobs`（`:1360`）：列任务，只聚合 DB 只读 + upstream 只读代理。
- `intercept_get_job`（`:3226`）：读单任务，只调 `_merge_gateway_job_metadata`（只读聚合）。
- `_job_json_record_from_payload`（`:3192`）：帮助函数，仅被 `intercept_get_job` 调用。
- `_serialize_response_value`（`:345`）：帮助函数，被 list/get 使用。
- `_merge_gateway_job_metadata`（`:351`）：被 list/get 使用，纯聚合无写。
- `_snapshot_gateway_job_metadata`（`:393`）：被 create + get 使用——**此函数同时被 create 使用，不能从原文件完全移走**，应改为在 `job_read.py` 中 `from job_intercept import _snapshot_gateway_job_metadata`（暂保留原定义位置），或把它提前到 `intercept/shared.py`（见 Step 6 说明）。本步保守做法：`job_read.py` 中直接从原模块 `import`，不搬函数体。

**具体做法**：

1. 新建 `gateway/intercept/job_read.py`，在顶部用相对 import 引入所需依赖（`from .. import job_intercept` 或直接 `from ..job_intercept import _merge_gateway_job_metadata, ...`）。
2. 将 `intercept_list_jobs`、`intercept_get_job`、`intercept_language_facts`、`_job_json_record_from_payload` 的函数体**剪切**（不要复制，防止双活）到 `job_read.py`。
3. 在原 `job_intercept.py` 中，用 `from intercept.job_read import ...` 保留重导出（re-export），维持 `from job_intercept import intercept_list_jobs` 对外兼容。格式：

   ```python
   # job_intercept.py — re-export after route family split (TU-09)
   from intercept.job_read import (
       intercept_get_job,
       intercept_language_facts,
       intercept_list_jobs,
   )
   ```

4. **不修改** `gateway/main.py` 的任何内容（re-export 保证向后兼容）。

**文件**：
- `gateway/intercept/job_read.py`（新建）
- `gateway/job_intercept.py`（改：删函数体，加 re-export）

✅ 已决策（CodeX 2026-06-25）：本步只迁移 job-read 族独占的函数；`_parse_job_list_pagination`（`:324`）、`_serialize_response_value`（`:345`）等如被多个 family 共用，**保留在 `job_intercept.py` 原位**，不在本步移入 `intercept/shared.py`；仅在两处以上子模块明确引用时才移入 shared.py（见 Step 6）。`job_read.py` 中对共享 helper 的引用使用 `from ..job_intercept import ...`。

**该步验收**：

```bash
# contract 测试和现有路由测试全部通过
python -m pytest tests/test_intercept_route_contracts.py tests/test_gateway_route_coverage.py tests/test_gateway_list_jobs_metadata.py -v
# 预期：全 passed，0 failed

# job_read.py 存在且包含三个公开符号
grep -c "^async def intercept_" gateway/intercept/job_read.py
# 预期：3

# job_intercept.py 中这三个函数不再有函数体（只有 re-export import）
grep -n "^async def intercept_list_jobs\|^async def intercept_get_job\|^async def intercept_language_facts" gateway/job_intercept.py
# 预期：0 行（全已迁走）

# main.py 导入不变（不应有 intercept.job_read 出现在 main.py）
grep "intercept.job_read\|from intercept" gateway/main.py
# 预期：0 行

# 行数下降确认
wc -l gateway/job_intercept.py
# 预期：比 Step 0 基线少约 300–500 行（视共享帮助函数取舍而定）
```

**commit 边界**：

```bash
git commit -- gateway/intercept/job_read.py gateway/job_intercept.py \
  -m "refactor: extract job-read route family to gateway/intercept/job_read.py"
```

---

## Step 3 · 迁移第二批：job-management family（`intercept_delete_job_v2` + `intercept_rename_job` + `intercept_suggested_copy_name` + `_verify_job_ownership`）

**目标**：把任务生命周期管理（删除、重命名、建议名）迁移到 `gateway/intercept/job_management.py`。

**涉及符号**：
- `intercept_delete_job_v2`（`:6220`）
- `_verify_job_ownership`（`:6256`）——注意 `voice_selection_api.py:222` 也有同名函数（DRY-07），本步保留两份并列，不合并（DRY-07 是独立任务）
- `intercept_rename_job`（`:6291`）
- `intercept_suggested_copy_name`（`:6447`）
- 仅被这三个函数调用的帮助函数（如 `_looks_like_truncated_source_title` `:1182`、`_looks_like_youtube_id_fallback` `:1191`、`_should_replace_display_name_from_s2` `:1200`、`_fetch_user_existing_display_names` `:1221`、`_branch4_prefix_for_source` `:1247`、`_fetch_user_branch4_sequence_today` `:1251`）

  > 上述帮助函数在 Step 0 的 grep 输出中核对——若有跨族调用，本步不迁，留在原文件。

**具体做法**：

1. 新建 `gateway/intercept/job_management.py`，将上述函数剪切进去。
2. 在 `job_intercept.py` 中加 re-export：

   ```python
   from intercept.job_management import (
       intercept_delete_job_v2,
       intercept_rename_job,
       intercept_suggested_copy_name,
       _verify_job_ownership,
   )
   ```

3. **不修改** `gateway/main.py`。

**文件**：
- `gateway/intercept/job_management.py`（新建）
- `gateway/job_intercept.py`（改：删函数体，加 re-export）

**该步验收**：

```bash
# 现有重命名、suggested-copy-name 测试全通过
python -m pytest tests/test_intercept_route_contracts.py tests/test_gateway_rename_job.py tests/test_gateway_suggested_copy_name.py -v
# 预期：全 passed，0 failed

# 四个公开符号在新文件中存在
grep -c "^async def intercept_delete_job_v2\|^async def _verify_job_ownership\|^async def intercept_rename_job\|^async def intercept_suggested_copy_name" gateway/intercept/job_management.py
# 预期：4

# job_intercept.py 不再有这四个函数体
grep -n "^async def intercept_delete_job_v2\|^async def _verify_job_ownership\|^async def intercept_rename_job\|^async def intercept_suggested_copy_name" gateway/job_intercept.py
# 预期：0 行

# admin gate 测试通过
python -m pytest tests/test_admin_gate_coverage.py -v
# 预期：全 passed

# 行数再次下降
wc -l gateway/job_intercept.py
```

**commit 边界**：

```bash
git commit -- gateway/intercept/job_management.py gateway/job_intercept.py \
  -m "refactor: extract job-management route family to gateway/intercept/job_management.py"
```

---

## Step 4 · 迁移第三批：metering-callbacks family（`update_job_metering` + `update_source_metadata`）

**目标**：把内部回调（仅 `_require_internal_access` 保护，无 `require_auth`）迁移到 `gateway/intercept/metering.py`。

**涉及符号**：
- `update_job_metering`（`:6708`）
- `update_source_metadata`（`:6495`）
- `_sanitize_s2_display_name`（`:1168`）——仅被 `update_source_metadata` 使用（在 Step 0 grep 中核实）

**具体做法**：

1. 新建 `gateway/intercept/metering.py`，将上述函数剪切进去。
2. 在 `job_intercept.py` 中加 re-export：

   ```python
   from intercept.metering import update_job_metering, update_source_metadata
   ```

3. **不修改** `gateway/main.py`。

**⚠️ 安全检查**：迁移后必须用 grep 验证 `metering.py` 中不存在 `settle_job_credit_ledger` / `mirror_job_terminal_state` 调用：

```bash
grep -n "settle_job_credit_ledger\|mirror_job_terminal_state" gateway/intercept/metering.py
# 预期：0 行（此族是 callback-only，不结算信用）
```

**文件**：
- `gateway/intercept/metering.py`（新建）
- `gateway/job_intercept.py`（改：删函数体，加 re-export）

**该步验收**：

```bash
# metering 测试通过
python -m pytest tests/test_job_metering_writeback.py tests/test_intercept_route_contracts.py -v
# 预期：全 passed，0 failed

# 两个符号在新文件
grep -c "^async def update_" gateway/intercept/metering.py
# 预期：2

# settlement 不在 metering.py 中
grep -c "settle_job_credit_ledger\|mirror_job_terminal_state" gateway/intercept/metering.py
# 预期：0

# 行数再次下降
wc -l gateway/job_intercept.py
```

**commit 边界**：

```bash
git commit -- gateway/intercept/metering.py gateway/job_intercept.py \
  -m "refactor: extract metering-callbacks family to gateway/intercept/metering.py"
```

---

## Step 5 · 迁移第四批：subresource dispatcher 中的子族（voice review / post-edit policy / artifacts download）

**目标**：`intercept_job_subresource`（`:3303`）本身作为统一入口**留在 `job_intercept.py`**（暂不搬），但把它内部调用的三个大型子族的实现函数迁移到独立文件，使 `intercept_job_subresource` 变成薄路由层（dispatcher 模式）。

**子族与涉及符号**：

1. **voice-review 子族** → `gateway/intercept/voice_review.py`
   - `_approve_voice_selection_with_quality_sync`（`:4588`）
   - `_post_edit_voice_preview_with_policy`（`:5860`）
   - `_record_voice_reuse_events`（`:3968`）
   - `_record_voice_candidate_rejection_events`（`:4039`）
   - `_aggregate_quality_tier_from_speakers`（`:3935`）
   - `_fetch_cosyvoice_public_voice_ids`（`:4215`）
   - `_fetch_known_cosyvoice_clone_voice_ids`（`:4251`）
   - `_enrich_speakers_with_clone_routing`（`:4274`）

2. **post-edit policy 子族** → `gateway/intercept/post_edit_policy.py`
   - `_is_post_edit_mutation_subpath`（`:5044`）
   - `_utc_now`（`:5075`）、`_as_aware_utc`（`:5079`）
   - `_post_edit_policy_key`（`:5096`）、`_post_edit_limits_for_user`（`:5115`）
   - `_should_shadow_settle_job_credits`（`:5120`）
   - `_post_edit_root_id`（`:5131`）、`_post_edit_usage`（`:5135`）、`_save_post_edit_usage`（`:5142`）
   - `_post_edit_root_job_for_update`（`:5149`）
   - `_post_edit_job_expires_at`（`:5162`）
   - `_post_edit_limit_exceeded`（`:5174`）
   - `_post_edit_increment`（`:5186`）、`_post_edit_daily_counter`（`:5190`）
   - `_post_edit_existing_copy_count`（`:5197`）
   - `_post_edit_mutation_with_policy`（`:5606`）
   - `_POST_EDIT_TRANSITION_SUBPATHS`（`:4991`）、`_POST_EDIT_SIMPLE_MUTATION_SUBPATHS`（`:4999`）

3. **artifacts-download 子族** → `gateway/intercept/artifacts_download.py`
   - `_emit_download_event`（`:3844`）
   - `_resolve_r2_redirect`（`:3494`）
   - `_legacy_lazy_resolve_publish_dubbed_video`（`:3633`）
   - `_resolve_r2_stream_redirect`（`:3679`）
   - `_derive_download_filename`（`:3820`）
   - `_DOWNLOAD_KEY_RE`（`:4975`）、`_STREAM_KIND_RE`（`:4985`）
   - `_JIANYING_DRAFT_SUBPATHS`（`:4963`）（Jianying 是下载/交付型子族，归入此模块）

4. **logs / concurrency 子族** → `gateway/intercept/subresource_misc.py`
   - `_is_admin_user`（`:4834`）
   - `_redact_job_record_in_place`（`:4844`）
   - `_serve_redacted_logs`（`:4896`）
   - `_continue_with_gateway_lock`（`:3874`）

**具体做法**：

对每个子族，按以下顺序操作：
1. 新建对应文件，剪切函数体及常量。
2. 在 `job_intercept.py` 中用 `from intercept.<module> import ...` 替换剪走的符号（re-export 或直接调用）。
3. `intercept_job_subresource` 函数体内的 `_xxx()` 调用改为 `from intercept.<module> import _xxx` 风格（或在文件顶部统一 import）。
4. **逐子族提交**，每提交一次跑一次测试。

**⚠️ 关键约束**：
- `_POST_EDIT_TRANSITION_SUBPATHS` 迁移后，`intercept_job_subresource` 中的 `if subpath in _POST_EDIT_TRANSITION_SUBPATHS:` 必须通过 import 引用，不能重复定义。
- `_emit_download_event` 是 `gateway/storage/event_log.py::emit_download_event` 的薄 delegator（见 CLAUDE.md）；迁移时不改实现，只搬位置。
- 每个新文件顶部必须有注释说明该文件是何种路由族，并引用本计划文档路径（`# Part of TU-09 route family split — docs/plans/code-quality-tasks/TU-09-intercept-split.md`）。

**文件**：
- `gateway/intercept/voice_review.py`（新建）
- `gateway/intercept/post_edit_policy.py`（新建）
- `gateway/intercept/artifacts_download.py`（新建）
- `gateway/intercept/subresource_misc.py`（新建）
- `gateway/job_intercept.py`（改：大幅删减，加 import）

**该步验收**（逐子族迁移后各跑一次，最终统一跑）：

```bash
# voice review 测试
python -m pytest tests/test_gateway_voice_selection_quality_sync.py tests/test_intercept_route_contracts.py -v

# post-edit 测试
python -m pytest tests/test_post_edit_phase0_guards.py tests/test_post_edit_guards_fill.py tests/test_smart_post_edit_gate.py tests/test_cleanup_post_edit.py tests/test_intercept_route_contracts.py -v

# artifacts download / jianying 测试
python -m pytest tests/test_gateway_jianying_routes.py tests/test_jianying_phase1_acceptance.py tests/test_intercept_route_contracts.py -v

# 行数目标：job_intercept.py 降至 4,500 行以下（收尾指标）
wc -l gateway/job_intercept.py
# 若仍高于 4,500，说明有大的帮助函数未迁完，检查 Step 6

# 全量路由 + admin gate 回归
python -m pytest tests/test_gateway_route_coverage.py tests/test_gateway_route_registration.py tests/test_admin_gate_coverage.py -v
# 预期：全 passed，0 failed
```

**commit 边界**（逐子族各一个 commit）：

```bash
git commit -- gateway/intercept/voice_review.py gateway/job_intercept.py \
  -m "refactor: extract voice-review sub-family to gateway/intercept/voice_review.py"

git commit -- gateway/intercept/post_edit_policy.py gateway/job_intercept.py \
  -m "refactor: extract post-edit policy sub-family to gateway/intercept/post_edit_policy.py"

git commit -- gateway/intercept/artifacts_download.py gateway/job_intercept.py \
  -m "refactor: extract artifacts-download sub-family to gateway/intercept/artifacts_download.py"

git commit -- gateway/intercept/subresource_misc.py gateway/job_intercept.py \
  -m "refactor: extract logs/concurrency sub-family to gateway/intercept/subresource_misc.py"
```

---

## Step 6 · 共享帮助函数整理 + `intercept/__init__.py` 公开 API 收口

**目标**：清理步骤 2–5 之后仍留在 `job_intercept.py` 的共享帮助函数（被多个子族共用，未被迁走的），并通过 `gateway/intercept/__init__.py` 暴露一个稳定的公开 API。

**动作**：

1. 审查 `job_intercept.py` 剩余内容，识别被多个新子族模块共用的帮助函数（如 `_error_response`、`_snapshot_gateway_job_metadata`、`compute_job_policy`、`PLAN_CATALOG`、`_parse_job_list_pagination`、`_insufficient_credits_response` 等）。

2. 按需迁移共享工具函数到 `gateway/intercept/shared.py`（✅ 已决策，CodeX 2026-06-25）：**不批量迁移候选清单**；仅当某 helper 在两个及以上已迁子模块中被明确引用时，才移入 `shared.py`。执行者先用 grep 统计各候选函数的实际跨族引用数，再按需决定：

   ```bash
   # 候选参考（需在 Step 0 grep 基础上确认跨族引用次数，≥2 处才移入）
   _error_response          # :488  — 预期跨族引用多，优先核查
   _insufficient_credits_response  # :713
   _gate_service_mode       # :730
   compute_job_policy       # :798  — 被 create + list
   PLAN_CATALOG             # :757
   ```

   若某候选在 Step 2–5 迁移后只剩 `job_intercept.py` 中的一个引用点，则保留原位，不移入 `shared.py`。`intercept_create_job` 及其专属的 YouTube probe / 匿名限额 / 哈希 / reservation 帮助函数（`:95`–`:1321`）**不在本步**迁移（属于 PRIOR-19 任务范畴）——保留在 `job_intercept.py` 直到 create-admission family 专项拆分。

3. 在 `gateway/intercept/__init__.py` 中列出公开导入，使外部代码可以 `from intercept import intercept_list_jobs` 而不必关心子模块路径：

   ```python
   # gateway/intercept/__init__.py
   from .job_read import intercept_get_job, intercept_language_facts, intercept_list_jobs
   from .job_management import (
       intercept_delete_job_v2,
       intercept_rename_job,
       intercept_suggested_copy_name,
       _verify_job_ownership,
   )
   from .metering import update_job_metering, update_source_metadata
   ```

   > 注意：`intercept_create_job` / `intercept_job_subresource` 继续从 `job_intercept.py` 直接导出（尚未迁，不要在 `__init__.py` 中虚构它们的位置）。

4. **`gateway/main.py` 导入来源本步不切换**（✅ 已决策，CodeX 2026-06-25）：`main.py` 继续 `from job_intercept import ...`，通过 re-export 保持兼容，**不**在本步改为 `from intercept import ...`。等全部 family 拆分稳定、全量回归验证通过后，再以单独 PR 切换导入来源（可选后续步骤，减少本单元回归面）。

**文件**：
- `gateway/intercept/shared.py`（新建，仅当有 ≥2 处跨族引用的 helper 确认后才建）
- `gateway/intercept/__init__.py`（改）
- `gateway/job_intercept.py`（改，继续缩减）
- `gateway/main.py`（**本步不改**；导入来源切换留作可选后续 PR）

**该步验收**：

```bash
# 收尾行数检查（硬性指标）
wc -l gateway/job_intercept.py
# 预期：< 4,500 行（目标 ≤4,499）

# 全量回归
python -m pytest tests/test_gateway_route_coverage.py tests/test_gateway_route_registration.py tests/test_admin_gate_coverage.py tests/test_intercept_route_contracts.py tests/test_gateway_create_job.py tests/test_gateway_list_jobs_metadata.py tests/test_gateway_rename_job.py tests/test_gateway_suggested_copy_name.py tests/test_job_metering_writeback.py tests/test_gateway_job_policy.py -q
# 预期：全 passed，0 failed

# main.py 路由路径字符串未变（以下应与 Step 0 输出完全一致）
grep -E '"(/job-api/|/gateway/)[^"]+"' gateway/main.py | sort > /tmp/routes_after.txt
# 手工 diff /tmp/routes_before.txt（Step 0 时保存）与 /tmp/routes_after.txt
# 预期：无差异

# contract 测试：公开符号 import 可达
python -c "from intercept import intercept_get_job, intercept_list_jobs, intercept_language_facts, intercept_delete_job_v2, intercept_rename_job, intercept_suggested_copy_name, update_job_metering, update_source_metadata; print('OK')"
# 预期：打印 OK，无 ImportError
```

**commit 边界**：

```bash
# shared.py 仅在有跨族 helper 需迁时才包含
git commit -- gateway/intercept/__init__.py gateway/job_intercept.py \
  -m "refactor: expose stable intercept package API via __init__.py (TU-09 cleanup)"

# 若有共享 helper 移入 shared.py，单独一个 commit：
# git commit -- gateway/intercept/shared.py gateway/intercept/__init__.py gateway/job_intercept.py \
#   -m "refactor: move shared helpers with ≥2 cross-family refs to intercept/shared.py"

# main.py 导入来源切换（可选后续 PR，不在本单元）：
# git commit -- gateway/main.py \
#   -m "refactor: switch main.py imports from job_intercept re-exports to intercept package"
```

---

## 测试计划（新增 / 回归）

### 新增测试（Step 1 建立，全程有效）

| 文件 | 覆盖内容 |
|------|---------|
| `tests/test_intercept_route_contracts.py` | read-family 不调 settlement；post-edit whitelist 完整性；metering 无 require_auth；公开符号 import 可达；路由路径与 main.py 注册一致 |

### 回归测试（每步必跑，最终全量）

| 测试文件 | 覆盖 family |
|---------|------------|
| `tests/test_gateway_route_coverage.py` | subresource 路由派发（不走 catch-all） |
| `tests/test_gateway_route_registration.py` | main.py 路由注册完整性 |
| `tests/test_admin_gate_coverage.py` | admin gate 覆盖 |
| `tests/test_gateway_list_jobs_metadata.py` | list / get 路由 |
| `tests/test_gateway_create_job.py` | create-admission（job_intercept.py 剩余部分） |
| `tests/test_gateway_rename_job.py` | job-management family |
| `tests/test_gateway_suggested_copy_name.py` | job-management family |
| `tests/test_job_metering_writeback.py` | metering-callbacks family |
| `tests/test_gateway_job_policy.py` | compute_job_policy（shared） |
| `tests/test_post_edit_phase0_guards.py` `test_post_edit_guards_fill.py` `test_smart_post_edit_gate.py` `test_cleanup_post_edit.py` | post-edit policy family |
| `tests/test_gateway_voice_selection_quality_sync.py` | voice-review family |
| `tests/test_gateway_jianying_routes.py` `test_jianying_phase1_acceptance.py` | jianying / artifacts-download family |

### 收尾量化指标（可机器验证）

```bash
# 指标 1：job_intercept.py 行数 < 4500
python -c "
lines = sum(1 for _ in open('gateway/job_intercept.py'))
assert lines < 4500, f'FAIL: {lines} lines >= 4500'
print(f'OK: {lines} lines < 4500')
"

# 指标 2：intercept/ 子包存在且包含 4+ 子模块
ls gateway/intercept/*.py | wc -l
# 预期：>= 5（__init__ + job_read + job_management + metering + ≥1 subresource 子族）

# 指标 3：新 contract 测试全部 passed
python -m pytest tests/test_intercept_route_contracts.py -q
# 预期：0 failed
```

---

## 回滚方案

| 粒度 | 方案 |
|------|------|
| 整个单元 | 优先 `git revert <commit-range>`（逆序 revert）。⚠️ 仅本地未 push 的 feature 分支 + 项目主确认才用 `git reset --hard` 到 Step 0 起始 commit |
| 单批迁移 | 每步均有独立 commit（显式 pathspec），`git revert <该 commit hash>` 即可撤销该族迁移，re-export 被撤回后符号重新由原函数体提供 |
| `__init__.py` 切换 | 若 Step 6 的 main.py 切换引入问题，`git revert` Step 6 commit；main.py 恢复走 job_intercept re-export，行为完全等价 |
| 生产回滚 | 本单元是纯内部模块重组（无数据库 migration、无 env var 变更、无 API 路径变更），回滚只需重部署旧镜像，无 alembic downgrade 步骤 |

**需要保留的回滚检查点**：
- Step 0 完成、Step 1 commit 前（基线 + contract 测试存在、无实现变更）
- Step 2–4 每步 commit 后（分批可独立回滚）
- Step 5 每子族 commit 后
- Step 6 最终 commit 后

---

## 完成定义（DoD）

- [ ] `wc -l gateway/job_intercept.py` 输出 **< 4,500**
- [ ] `gateway/intercept/` 子包存在，包含 `__init__.py` + 至少 `job_read.py` / `job_management.py` / `metering.py` / `voice_review.py` / `post_edit_policy.py` / `artifacts_download.py` / `subresource_misc.py`
- [ ] `tests/test_intercept_route_contracts.py` 全部 **passed**，0 failed
- [ ] `python -m pytest tests/test_gateway_route_coverage.py tests/test_gateway_route_registration.py tests/test_admin_gate_coverage.py -q` 全部 **passed**，0 failed
- [ ] 全量回归套件（Step 6 验收清单中列出的全部测试）**0 failed**
- [ ] `grep -n "settle_job_credit_ledger\|mirror_job_terminal_state" gateway/intercept/job_read.py gateway/intercept/metering.py` 输出 **0 行**（read / metering 族无结算调用）
- [ ] `gateway/main.py` 中注册的 URL path 字符串（`/job-api/...`、`/gateway/...`）与 Step 0 基线 **完全一致**（diff 无差异）
- [ ] `gateway/main.py` 的导入语句**未改为** `from intercept import ...`（第一 PR 阶段继续通过 job_intercept re-export，切换留后续可选 PR）
- [ ] 每步均只迁移该族**独占**函数；共享 helper 留在 `job_intercept.py`，`intercept/shared.py` 仅含确认跨族引用 ≥2 处的 helper
- [ ] 每步均使用**显式 pathspec** 提交（`git commit -- <files>`），**未使用 `git add .`**
- [ ] 每步均为独立 commit，commit message 符合 `refactor: ...` / `test: ...` 前缀约定

# Phase 1 交接文档（2026-04-19）

> **新会话请先读本文档**，完整了解 Studio 任务二次修改项目的当前状态、已完成内容、下一步计划、硬约束、CodeX 配合模式。读完后无需再翻其他文件就能直接工作。

---

## 0. 一句话背景

"Studio 任务二次修改" 项目：让已完成的 Studio 任务可以进入 `editing` 态做文本 / 音色 / 单段 TTS 增量修改，最终覆盖原任务或保存为副本。Phase 0（数据层 + UX 静态接线）+ Phase 1（业务 API + 前端 MVP）代码完成并已部署到 **US 生产（feature flag off 影子态）**。

## 1. 唯一方案主文档

[`docs/plans/2026-04-18-studio-post-edit-plan.md`](../plans/2026-04-18-studio-post-edit-plan.md)

- 47 条决策（D1-D47，D20 废弃）
- 分段实施：Phase 0（数据 + UX 基底）/ Phase 1（功能 API + 前端）
- §17.3-17.5 包含 Runbook + Smoke Checklist + Rollback Triggers

## 2. 当前 Git 状态

**18 个 commit**（`b44992e..c24c92e`），main 分支直推：

```
Phase 0 (7):
  b44992e docs(plan)          方案 v3 + Runbook
  8c122f6 docs(internal)      T0-1 触点清单
  cbbfb49 feat(data)          T0-2+T0-3 migration 015 + editing 枚举
  507aeb9 feat(jobs)          T0-4 命名 + 脱敏 + CJK 宽度
  2669477 feat(cleanup)       T0-5 cleanup + idle_scanner 骨架
  262c2ba feat(frontend)      T0-3前端+T0-6 UX
  b06655b test(phase0-guards) T0-7 25 条守卫

Phase 1 (10) + P1 修复 (1):
  bc3cc3c feat(editing)       T1-1 端点骨架
  73cb86b feat(editing)       T1-2 segments CRUD
  0043087 feat(editing)       T1-5 单段 re-TTS + draft
  5f9ccf0 feat(editing)       T1-6 批量 + voice_map
  0fd8b25 feat(gateway)       T1-7 日志脱敏
  cd82390 feat(editing)       T1-8+T1-9 copy_as_new + commit 两阶段
  d26f925 feat(cleanup)       T1-10 idle cancel callback
  d8bf0b6 feat(editing)       T1-3+T1-4+T1-11 视频修改页+split+字幕
  ff30447 test+docs           T1-12 守卫 33 条 + CLAUDE.md
  a2c7088 docs(CLAUDE)        补 CLAUDE.md 漏配
  c24c92e fix(gateway+main)   CodeX P1×2 修复（commit DB sync + idle wire）
```

CodeX 已三审放行（`c24c92e` 是三审通过版本）。

## 3. 生产部署状态

| 主机 | alembic | 后端代码 | Gateway 代码 | 前端代码 | feature flag |
|------|---------|---------|-------------|---------|-------------|
| **US** (5.78.122.220) | 015_post_edit_fields ✅ | Phase 1 已部署 ✅ | Phase 1 已部署 ✅ | **未部署** ❌ | 后端 `AVT_ENABLE_POST_EDIT` 默认 false；前端 `NEXT_PUBLIC_ENABLE_POST_EDIT` 未设 |
| **SG** | 014_background_tasks（Phase 0 未 apply）| 老代码 | 老代码 | 老代码 | — |

### US 当前运行态确认点（后续 smoke 引用）

```bash
# 容器健康
/d/daili/scripts/SSH-US-Via-154.cmd "docker ps --format 'table {{.Names}}\t{{.Status}}'"
# 预期 5 容器全 healthy（app/gateway/next/caddy/postgres）

# 新代码 grep 落地
/d/daili/scripts/SSH-US-Via-154.cmd "docker exec aivideotrans-gateway grep -c '_apply_editing_commit_gateway_side' /opt/gateway/job_intercept.py"
# 预期 2

# Job API 启动日志
/d/daili/scripts/SSH-US-Via-154.cmd "tail -20 /opt/aivideotrans/data/runtime_logs/job-api.stdout.log"
# 预期 "Job API started at http://127.0.0.1:8877"
```

## 4. 测试套件快速索引

```bash
# 核心 Phase 1 套件（新会话首次运行确认基线）
.venv/Scripts/python -m pytest \
  tests/test_gateway_editing_commit_sync.py \
  tests/test_phase1_guards.py \
  tests/test_editing_commit.py \
  tests/test_editing_endpoints.py \
  tests/test_editing_segments.py \
  tests/test_editing_tts.py \
  tests/test_editing_batch_and_voice_map.py \
  tests/test_copy_service.py \
  tests/test_idle_scanner_integration.py \
  tests/test_gateway_logs_redaction.py \
  tests/test_subtitle_sync_contract.py \
  tests/test_post_edit_phase0_guards.py \
  tests/test_cleanup_post_edit.py \
  tests/test_legacy_cleanup_guards.py \
  tests/test_job_service.py \
  tests/test_job_api.py -q
# 基线：265+ passed

# 前端
cd frontend-next && npx tsc --noEmit   # exit=0
```

**关键守卫测试**（若动后端 commit / editing 路径都要跑）：
- `test_phase1_guards.py` — 33 条契约（付费 API AST 扫 + 模块结构 + 前后端路径 parity + feature flag 覆盖）
- `test_post_edit_phase0_guards.py` — 25 条（editing ∈ ACTIVE ∉ WORKER 等不变式）
- `test_gateway_editing_commit_sync.py` — 9 条（CodeX 二审补，P1-1 / P1-2 回归防护）

## 5. 下一步工作计划（优先级顺序）

### A. 部署前端 + flag on（**推荐立即做**）

**目标**：让你（solo 测试用户）能真实在浏览器走完 enter-edit → 文本编辑 → commit overwrite/copy_as_new 的 e2e 流程。

**步骤**：
1. 打包前端 Phase 1 改动（3 文件）：
   - `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`（新）
   - `frontend-next/src/lib/api/editing.ts`（新）
   - `frontend-next/src/components/workspace/TranslationReviewPanel.tsx`（改，split 局部更新）
2. 以及这些前端文件（T0-6 时写了但未部署到生产）：
   - `frontend-next/src/components/status-badge.tsx`
   - `frontend-next/src/features/jobs/expiry.ts`
   - `frontend-next/src/app/(app)/projects/page.tsx`
   - `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx`
   - `frontend-next/src/features/jobs/selectors.ts`
   - `frontend-next/src/types/api.ts` / `types/jobs.ts`
   - `frontend-next/src/lib/api/mappers.ts`
   - `frontend-next/src/lib/text/width.ts`（新）
3. Next.js 是 docker image 内代码（**不是 bind mount**），需要 `docker compose build next` + `docker compose up -d next` 或 push 新 image + restart
4. 把 `AVT_ENABLE_POST_EDIT=true` 写入 US 的 `.env`，`docker restart aivideotrans-gateway`
5. 前端 build 时传 `NEXT_PUBLIC_ENABLE_POST_EDIT=1`
6. 在浏览器登录 admin，挑一个已有的 Studio `succeeded` 任务测试

**预期可验证**：
- 任务卡出现 "修改" 直达按钮（D43）
- 进入视频修改页、段落列表加载、文本编辑、保存
- "放弃修改" 正常回 succeeded
- commit overwrite → 任务 status 变 running，跑完 alignment+publish（真实 pipeline！会消耗 ffmpeg 时间但**不消耗付费 API**因为 D26 守卫）
- commit copy_as_new → Gateway DB 出现新 Job row + 副本任务在列表页出现

**预期不可验证**（需要 B 任务）：
- 单段 re-TTS 按钮 → 返 501 Toast "功能即将上线"（正确行为）
- 批量 re-TTS → 所有段 failed（因为 caller 未接）

**风险**：生产 alignment+publish 会跑真实 ffmpeg + 可能调 alignment 内部的 LLM rewrite（如果译文长度匹配出问题，LLM 会被调用重写 —— 这是原有 pipeline 行为，不是新引入的付费路径）。solo 测试场景可接受。

### B. TTS provider wiring（专项，A 之后做）

**目标**：让 `regenerate_segment_tts` 的 `tts_caller` 默认 fallback 到真实 TTS 合成。

**约束（CLAUDE.md 硬约束）**：
- 付费 API 只能由用户显式触发 endpoint 调用
- 不能在 fallback / retry 里静默调

**实施思路**：
1. 看 `src/services/tts/tts_generator.py` / `tts_strategy.py` 现有接口
2. 写 `src/services/tts/segment_regenerate.py`:
   - `build_real_segment_tts_caller(config, ...)` 返回 `SegmentTTSCaller`
   - 内部调 `tts_generator` 或对应 provider
3. 在 `main.py run_job_api_command` 里把 caller 注入 `JobService`（类似 `inject_editing_cancel_callback` 模式）
4. 加 AST 守卫：
   - `tests/test_phase1_guards.py` 扫 `src/services/tts/segment_regenerate.py` 的 caller 只通过 DI 暴露，无 `except:` 里的静默调用
5. 测试：mock TTS API（`monkeypatch` httpx）验证 caller 行为

### C. SG 同步部署（A+B 完成后）

按 Phase 0 + Phase 1 相同流程部 SG，但用户明确说过"SG 目前可以不部署"，**除非用户主动要求否则跳过**。

### D. Phase 2 推迟项（不在本 Phase 范围）

见 `docs/plans/2026-04-18-studio-post-edit-plan.md` §18，参考以下列表判断是否要接（solo 用户场景多数可忽略）：

- 响应式 + a11y 完整（D47）
- 虚拟滚动（§9.1）
- 视频播放器 + 拖动联动段落（§7.2）
- 音色修改 Tab 前端 UI（API 已 ready）
- 单段 draft TTS 试听 streaming endpoint
- 副本链可视化 (D19 已 rejected)

## 6. 硬约束（新会话每次开工前过一遍）

项目 `CLAUDE.md` 规定的硬约束，任何违反都要主动阻止：

1. **付费 API 不能自动调用** — Voice Clone / TTS / LLM / ASR 等都必须用户显式触发
   - 禁止模式：`except: fallback_to_paid_api()` / 默认帮用户做 / batch 无上限
   - T1-5 的 `_not_wired_tts_caller` 就是为守这条而设计的占位符
   - AST 守卫：`tests/test_phase1_guards.py` 扫 alignment/output 模块不得 import tts_generator
2. **不创建 worktree / 新分支** — 直接在 main 改
3. **main 分支直推** — 不走 PR 流程（单人项目）
4. **临时文件放 `D:\Claude\temp\`** — 不放桌面 / 仓库根
5. **Windows 主机只部署到 US/SG 两台 Linux 生产主机** —— 通过 `D:\daili\scripts\*-Via-154.cmd` 脚本
   - `SCP-US-Via-154.cmd <local> <remote>`
   - `SSH-US-Via-154.cmd "<cmd>"`
   - `Deploy-US-Via-154.cmd <local> <remote> "<cmd>"` — SCP + 远端执行
6. **容器代码部署**：
   - `src/` 是 bind mount（主机改文件 + `docker restart aivideotrans-app`）
   - `gateway/` 是 docker image 内（需要 `docker cp` + restart）
   - Next.js 前端也是 docker image 内（需要 `docker compose build next` + restart）
7. **Feature flag 双端 gate（D29）**：
   - 后端 `AVT_ENABLE_POST_EDIT` 默认 false → Gateway 返 404 屏蔽所有 editing 端点
   - 前端 `NEXT_PUBLIC_ENABLE_POST_EDIT=1` 才渲染入口

## 7. CodeX 配合模式

**这个项目的工作流**是：

1. 你（Claude）交付一个 Task / 修复
2. 用户把你的 report 贴给 CodeX CLI 或另一个会话做 review
3. CodeX 返回结构化评论（P1 = 必修 / P2 = 建议 / Medium 可延后）
4. 你根据意见修复，每轮重跑 pytest + tsc + commit
5. 直到 CodeX 返回 "没有新的 blocking finding"

**历史交互次数**：Phase 0 + Phase 1 一共被 CodeX review 过 7-8 轮：
- 3 轮 P1/P2 修复（方案 doc 阶段）
- 2 轮 P1 修复（代码阶段：T0-2 autocommit_block API + T0-4 registry import 路径）
- 1 轮 P1×2 修复（Phase 1 完成后：Gateway commit DB sync + idle cancel 启动接入）

**典型 CodeX 格式**：
```
::code-comment{title="[P1] xxx" body="..." file=".../file.py" start=N end=M priority=1 confidence=0.99}
```

新会话收到 CodeX 反馈时：
- 先读 file + line 对应的代码
- 理解 CodeX 的 concern 是否真的 blocking（大多数是）
- 修复 + 加回归测试 + commit
- 回复用户时明确标注 "P1-1 修复 / P1-2 修复" 而不是堆在一起

## 8. 重要决策 log（避免 relearn）

以下是过去讨论中花时间 converge 的关键点，新会话直接 inherit：

- **状态机**：只新增 `editing`，commit 后走 `running`（不造 `processing`）— D21
- **TTL 规则**：`min(now+7d, prev.expires_at+24h)`，scoped by `(user_id, root_job_id)` — D23
- **editing 文件隔离**：`editor/editing/` 子目录，baseline `editor/tts_segments/` 永不动 — §3.5
- **copy_as_new 两阶段**：Phase A 失败保留源 editing/；Phase B 清源 — D34
- **hardlink 策略**：Linux 主机 `os.link`（生产是 Linux） — D27
- **commit 不重入 TTS**：alignment/publish 代码禁调 tts_generator（AST 守卫） — D26
- **TTS 复用原定价**：不单独计费（废弃 D20） — D30
- **日志脱敏服务端**：Gateway 拦截 `/logs` + 非 admin 脱敏 — D25
- **Feature flag 双端 gate** — D29
- **editing_touched_at** 字段名（之前是 started_at，CodeX 二审改名） — D24

## 9. 文件 / 模块索引（按职责）

**editing 状态管理**（所有新逻辑）：
- `src/services/jobs/editing.py` — enter/cancel/commit + touch helper + events
- `src/services/jobs/editing_segments.py` — segments CRUD + segment_status
- `src/services/jobs/editing_tts.py` — 单段 re-TTS + draft accept/discard
- `src/services/jobs/editing_voice_map.py` — 音色覆盖
- `src/services/jobs/editing_batch.py` — 批量 re-TTS
- `src/services/jobs/editing_commit.py` — commit 两策略（overwrite + copy_as_new）
- `src/services/jobs/copy_service.py` — hardlink + apply_draft_segment + prepare_copy
- `src/services/jobs/runner_extensions.py` — submit_job_from_existing_project_dir
- `src/services/jobs/input_validators.py` — segment_id regex / strategy whitelist
- `src/services/jobs/logs_redactor.py` — provider 名动态脱敏
- `src/services/web_ui/editing_idle_scanner.py` — 24h 闲置自动 cancel
- `src/services/web_ui/cleanup.py` — TTL 清理（优先 expires_at + legacy fallback）

**Gateway 扩展**：
- `gateway/job_intercept.py` — `_editing_transition_with_lock` + `_apply_editing_commit_gateway_side` + `_serve_redacted_logs` + `_is_post_edit_mutation_subpath`
- `gateway/config.py` — `enable_post_edit` 字段

**前端**（已写未部）：
- `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx` — 视频修改页 MVP
- `frontend-next/src/lib/api/editing.ts` — 完整 API 客户端 + TS 类型
- `frontend-next/src/features/jobs/expiry.ts` — 过期倒计时三级配色
- `frontend-next/src/components/status-badge.tsx` — editing 紫色 + 第 N 次修改文案
- `frontend-next/src/components/workspace/TranslationReviewPanel.tsx` — split 局部更新

## 10. 给新会话的开场话术建议

用户可能第一句话就是"继续开发" / "开 A" / "开 B"。新会话第一步：

1. 读本文档（你在读）
2. 读方案文档 `docs/plans/2026-04-18-studio-post-edit-plan.md`（可以只扫 §1 决策表 + §17.3 Runbook）
3. 跑 `git log --oneline -25` 看 commit 历史
4. 根据用户指示（A / B / C / D）开工
5. 工作前明确告知用户"我读完了交接文档，当前状态 X，即将做 Y，预期 Z"

**CodeX review 触发时机**（用户口吻）：
- "把你刚才的总结贴给 CodeX"
- "CodeX 审核意见如下：..."
- 你看到 `::code-comment{...}` 格式 = CodeX 输出

---

## 附：部署命令速查（新会话经常需要）

```bash
# 打包后端 Phase 1 差异（已在 c24c92e 固化，US 上已有，一般不用再跑）
tar czf /d/Claude/temp/phase1-deploy.tar.gz \
  src/services/jobs/editing*.py \
  src/services/jobs/copy_service.py \
  src/services/jobs/runner_extensions.py \
  src/services/jobs/input_validators.py \
  src/services/jobs/service.py \
  src/services/jobs/api.py \
  src/services/jobs/models.py \
  src/services/web_ui/editing_idle_scanner.py \
  main.py \
  gateway/job_intercept.py \
  gateway/config.py

# SCP + 远端 unpack
cmd.exe //c "D:\daili\scripts\SCP-US-Via-154.cmd D:\Claude\temp\phase1-deploy.tar.gz /tmp/phase1-deploy.tar.gz"
/d/daili/scripts/SSH-US-Via-154.cmd "cd /tmp && rm -rf phase1-unpack && mkdir phase1-unpack && tar xzf phase1-deploy.tar.gz -C phase1-unpack/"

# 部署到运行态（app 容器 src 是 bind mount，gateway 是 docker cp）
/d/daili/scripts/SSH-US-Via-154.cmd "rsync -av /tmp/phase1-unpack/src/ /opt/aivideotrans/app/src/ && cp /tmp/phase1-unpack/main.py /opt/aivideotrans/app/main.py && docker cp /tmp/phase1-unpack/gateway/job_intercept.py aivideotrans-gateway:/opt/gateway/job_intercept.py && docker cp /tmp/phase1-unpack/gateway/config.py aivideotrans-gateway:/opt/gateway/config.py"

# Restart + smoke
/d/daili/scripts/SSH-US-Via-154.cmd "docker restart aivideotrans-app && sleep 5 && docker restart aivideotrans-gateway && sleep 15 && docker ps --format 'table {{.Names}}\t{{.Status}}'"
```

---

END. 新会话按本文档开工即可。

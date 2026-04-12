# Session Handoff — 2026-04-09

> 本文档供下一个 Claude Code / 开发者会话快速了解当前进度和待办事项

---

## 当前进度

### V3 点数体系 — Shadow Pilot（已完成 + 已部署）

- **V3-0 ~ V3-6** 全部完成并通过复核
- **已部署到 US 生产主机**（5.78.122.220），migration 009 已执行
- Shadow bucket / ledger / metering 链路已验证通过
- Pilot observability runbook 和 deployment checklist 已就绪

**最新修复（本会话）：**
- Shadow credits 链路修复：create-time 无 duration 时仍写入 snapshot + source-metadata 回调补做 late reserve + ensure_free_bucket
- 已验证：grant ✓ → reserve ✓ → capture ✓，bucket remaining/reserved 正确

**试运行初步数据（8 个 V3 任务）：**
- K_actual 均值 ≈ 281（冻结假设 250，偏低 12%）
- Rewrite 触发率 ≈ 86%（冻结假设 30%，严重偏低）
- Express/Studio 各 50%
- `credits_estimated` 在 snapshot 中偶尔为 null（source-metadata 和 pipeline metering 并发写入 race condition，non-critical）

### Express 多说话人支持（已完成 + 已部署）

- `_normalize_speakers` 放开到 1-10
- auto 检测支持 N 人（去掉 >2 限制）
- translator `speaker_voices` dict 支持 N 人音色分配
- 前端说话人下拉增加 3-6 人选项
- 已部署到 US 主机

### Studio 音色确认独立阶段（设计完成，待实施）

- 设计文档：`docs/plans/2026-04-07-voice-selection-review-stage-design.md`（含 §8 集成细节，已通过 spec review + Gemini review）
- 状态：**设计完成，等待实施**

---

## 下一步待办

### 优先级 1：Studio 音色确认阶段实施

按设计文档 Phase 1-5 实施：

| Phase | 内容 | 关键文件 |
|-------|------|----------|
| 1 | 后端：review_state 新增 stage + Pipeline 暂停点 | `review_state.py`, `process.py` |
| 2 | 后端：Gateway 新增 4 个端点 | `job_intercept.py` 或新文件, `main.py` |
| 3 | 后端：voice_clone.py + admin_settings 配置 | `voice_clone.py`, `admin_settings.py` |
| 4 | 前端：VoiceSelectionPanel + VoiceCloneModal | 新组件 |
| 5 | 测试 + 部署 | |

### 优先级 2：V3 Pilot 持续观测

- 按 `docs/plans/2026-04-07-v3-pilot-observability-runbook.md` 每周采数
- 重点关注 K-value 和 rewrite rate 与冻结假设的偏差
- 2-4 周后做校准评审

### 优先级 3：Studio voice_id="auto" bug

- 现象：Studio 模式用户未选音色时 voice_id 停留在 "auto"，MiniMax 返回 `status_code=2054 voice id not exist`
- 根因：音色确认独立阶段实施后自然解决（强制所有说话人必须有音色才能继续）

---

## 下一个会话阅读清单

| 优先级 | 文件 | 读什么 |
|--------|------|--------|
| 1 | `CLAUDE.md` | 项目约束 |
| 2 | `docs/plans/2026-04-07-voice-selection-review-stage-design.md` | 完整设计 spec |
| 3 | `src/services/review_state.py` | ReviewStateManager 机制 |
| 4 | `src/pipeline/process.py` 850-1000 行 | voice review gate |
| 5 | `src/services/voice_clone.py` 340-370 行 | `_clone_voice()` 方法 |
| 6 | `gateway/admin_settings.py` 前 80 行 | AdminSettings 模型 |
| 7 | `gateway/job_intercept.py` `update_job_metering` 函数 | 内部回调端点模式 |
| 8 | `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx` | workspace tab 渲染 |
| 9 | `frontend-next/src/lib/api/reviews.ts` | `resolveActiveReviewStage()` |
| 10 | `frontend-next/src/components/workspace/TranslationReviewPanel.tsx` 前 50 行 | review panel 模式 |

## 注意事项

1. **DESIGN.md 风格** — UI 用 slate/teal 色系，不用紫色，系统中文字体
2. **付费 API 约束** — 克隆音色必须用户显式点击触发（CLAUDE.md 硬约束）
3. **部署方式** — Gateway 需 `docker compose build gateway`，App 是 bind mount 重启即可
4. **Python 执行** — 本机用 `C:\Users\Administrator\.local\bin\python.cmd`

---

## 关键 commit 记录

| Commit | 内容 |
|--------|------|
| `a860a55` | V3 点数体系 shadow pilot 全量（V3-0 ~ V3-6 + 部署） |
| `65ea25e` | Studio 音色选择独立阶段 + 个人音色库 + 统一音色匹配 |
| `052dc74` | 三引擎音色选择 + CosyVoice/MiniMax 统一匹配 |
| `1ff877b` | shadow credits 链路修复 + Express 多说话人支持 |

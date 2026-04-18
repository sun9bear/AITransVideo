# docs/archive

本目录存放**非当前执行依据**的历史文档：已完成的方案、已被取代的设计、历史阶段验收、调查记录、被放弃的方向。

所有文档都只作**历史背景与溯源**使用，**不是当前实现指引**。当前真源请走：

- `docs/QUICKSTART.md` — 新协作者第一入口
- `CLAUDE.md` / `AGENTS.md` — 协作与架构规则
- `docs/graphs/` — 图谱索引（新会话先读）
- `docs/plans/` — **当前**仍在推进的方案
- `docs/specs/` — **当前**仍有效的规格（`2026-03-29-commercialization-foundation-design.md` / `2026-04-04-pricing-and-plans-api-contract.md`）

---

## 目录结构

| 子目录 | 内容 | 何时该看 |
|--------|------|----------|
| `snapshots/` | 顶层状态快照：CURRENT_PROJECT_STATUS、COMMERCIALIZATION_HANDOVER、FREE_VS_PAID_TTS、CosyVoice status 等 + 两份 pre-refactor 早期文档（PROJECT_BRIEF、Roadmap V3.1） | 回查某个时间点项目叙事 |
| `phases/` | `JOB_SERVICE_PHASE_A1` / `LINUX_MIGRATION_SCOPE` / `REFACTOR_PHASE1_SUMMARY` / `PHASE_A_IMPLEMENTATION_TASKS` / `REVIEW_FRONTENDIZATION_PHASE1/2_SCOPE` / `WEB_CONSOLE_*_SCOPE` | 阶段边界定义已关闭 |
| `acceptance/` | 各阶段验收笔记（`*_ACCEPTANCE_NOTE.md`、`2026-04-03-*` 基线/审计、`STABLE_BASELINE_SUMMARY`、`v2-staging-go-live-checklist` 等） | 查某阶段交付标准 |
| `plans/` | 已完成或已被取代的 plan（2026-03-24 ~ 2026-04-14 大批） | 查某 feature 的原始设计 |
| `specs/` | 已完成或被取代的 spec（长视频稳定性、会员订阅、dynamic voice library、volcengine 豆包等） | 查某架构决策的原始论证 |
| `reviews/` | 历史 code/review 报告（CODEX_REVIEW_REPORT_2026-03-19/23、OPTIMIZATION_REPORT_2026-03-23、REAL_VIDEO_PROGRESS_ANALYSIS、COMMERCIAL_READINESS_CHECKLIST） | 回顾历史技术债 |
| `handover/` | 历史会话交接文档（2026-04-01/02、V3/S2/CosyVoice/phase1 各类 handoff） | 查某个交接点的上下文 |
| `deployment-legacy/` | 单机 / Windows 远程工作台 / Linux 迁移阶段的部署文档（已被 Docker Compose + Caddy 统一替代） | 仅做历史参考 |
| `web-ui-single-user/` | Web UI 8876 单机时代的架构文档（`WEB_UI_STATUS/ROADMAP`、`PUBLIC_WEB_UI_SECURITY_BOUNDARY`、`JOB_API_A1_QUICKSTART`、`WEB_CONSOLE_*_GUIDELINES`；注意路由/端口全部与当前不一致） | 回顾 Web UI 为何下线 |
| `s2-investigations-2026-04-08/` | S2 审校阶段调查与草案（7 份，均已被 `plans/2026-04-08-s2-three-pass-split-plan` 落地解决） | 回查 S2 优化思路起源 |

> Hermes 方向 4 份文档未归档，已单独收在 `docs/plans/hermes/`（无时间表但保留可见）。被 `plans/2026-04-13-*` 两份取代的 `2026-04-12-probe-tts-calibration.md` 和 `2026-04-13-voice-speed-catalog.md` 归入本 archive 的 `plans/`。

---

## 归档约定

- 文档一旦进入 `archive/`，视为**只读**。如需复活某方向，应在 `docs/plans/` 下起新方案，在新方案里明确 `Supersedes:` 指回归档路径，而不是直接改归档内容。
- 被取代的方案在归档时应在文件头标 `Status: superseded` 与 `Superseded-by: <新方案路径>`（见 `docs/plans/` 下保留文档的 metadata header 格式）。
- 归档目录结构由本 README 维护，新增子目录需同步更新此表。

## 本次重组记录

2026-04-17 legacy cleanup 批次进行了一次集中归档：顶层散落的 5 份快照、`docs/phases/` (10)、`docs/acceptance/` (14)、`docs/reviews/` (5)、`docs/architecture/` 里的 Web UI 文档 (9)、`docs/deployment/` 部分 (5)、`docs/issues/` + `docs/analysis/` 全部 (7)、`docs/plans/` 已完成 (20+)、`docs/specs/` 已完成 (9)、`docs/superpowers/` 部分 (2 份被取代的 plan)，共约 80 份归入对应子目录。原目录 `phases/` `acceptance/` `reviews/` `issues/` `analysis/` `superpowers/` 已清空删除。Hermes 4 份（原 `docs/superpowers/plans/` + `docs/superpowers/specs/`）保留在 `docs/plans/hermes/`，不归档。

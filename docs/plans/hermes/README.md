# docs/plans/hermes/

Hermes 内部监控 / ops 控制面平台的设计与分期实施方案。

## 当前状态

- **设计完成度**：平台设计（`2026-04-11-hermes-platform-design.md`）+ 3 期实施方案全部完整
- **代码落地**：❌ 零 commit，尚未启动实施
- **时间表**：无明确时间表
- **保留理由**：设计本身有系统性价值；当项目真需要独立 ops 控制面时（例如 admin 页面无法承载更复杂的观测 / 自动诊断需求时），可以直接进入实施

## 目录内容

| 文件 | 角色 |
|------|------|
| `2026-04-11-hermes-platform-design.md` | 平台总体设计：ops / research / copilot 三条能力 |
| `2026-04-11-hermes-ops-control-plane-phase1.md` | Phase 1：ops 控制面最小闭环（异常检测 + 结构化报告 + Telegram 投递） |
| `2026-04-11-hermes-phase15-phase2.md` | Phase 1.5 + Phase 2：能力扩展与样本收集 |
| `2026-04-11-hermes-phase3-copilot.md` | Phase 3：受控内部 admin copilot |

## 重要边界

- Hermes 平台设计独立于当前项目主线，不阻塞任何正在进行的工作
- 若未来有团队成员想推进 Hermes，应先在本目录加一份"启动前决策"说明，重新评估以下问题：
  - 现有 admin 监控（`/admin/jobs`、`/admin/s2-monitor`、`/admin/credits-monitor`）是否已经够用？
  - 如果启动 Hermes，能带来的新能力具体是什么？
  - 是否有稳定的维护者承诺这条线？
- 否则本目录只作为**设计档案**，不作为近期执行依据

## 相关历史

原计划存放在 `docs/superpowers/plans/` + `docs/superpowers/specs/`，2026-04-17 legacy cleanup 时从 `archive/parked-directions/superpowers/` 提回活跃目录，以保持方向可见性（但不改变"无时间表"的现实）。

同批放在 `docs/superpowers/` 下的 `2026-04-12-probe-tts-calibration.md` 和 `2026-04-13-voice-speed-catalog.md` 已被 `docs/archive/plans/2026-04-13-probe-tts-calibration-redesign-plan.md` 和 `docs/archive/plans/2026-04-13-voice-speed-precalibration-plan.md` 取代，归档于 `docs/archive/plans/`，不纳入 Hermes 目录。

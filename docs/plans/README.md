# Plans 状态索引

最后更新：2026-05-24

本文件是 `docs/plans/` 的快速导航层，用来说明当前阅读顺序、主题归类和方案实施状态。它不替代原方案、图谱文档或代码；开发时仍应以图谱、对应方案、代码和测试共同判断。

架构敏感任务应先读 `docs/graphs/GITNEXUS_PROJECT_GRAPH.md`，再进入相关子图。

## 状态说明

| 状态 | 含义 |
| --- | --- |
| `ACTIVE` | 当前执行依据、当前决策记录或仍需优先参考的基线。 |
| `IN_PROGRESS` | 正在实施、生产 rollout/观测仍未完全闭环，或仍有明确后续任务。 |
| `DONE` | 已实现；除非仍是 live contract，否则主要作为历史依据保留。 |
| `DEFERRED` | 已明确延后，通常需要触发条件或新任务再启动。 |
| `NOT_STARTED` | 有设计文档，但未看到实质实现开始。 |
| `FROZEN` | 暂停/冻结方向；没有新方案前不要直接实施。 |
| `SUPERSEDED` | 已被更新的方案、基线或实现路径替代。 |
| `RECORD` | 审计、结果记录、handoff、部署记录，不是待实施方案。 |
| `ARCHIVED` | 已移出当前 plans，保留在 archive 供追溯。 |

## 推荐阅读顺序

1. `docs/graphs/GITNEXUS_PROJECT_GRAPH.md`
2. `docs/graphs/README.md`
3. 本文件
4. 下方主题对应的当前方案
5. 图谱或方案列出的代码与测试

## 主题地图

| 主题 | 当前状态 | 当前主要文档 | 备注 |
| --- | --- | --- | --- |
| Smart Auto Pipeline | `IN_PROGRESS` | `2026-05-24-smart-auto-pipeline-rebaseline.md`, `2026-05-04-smart-auto-pipeline-plan.md`, `2026-05-13-smart-mvp-p2-implementation-plan.md`, `2026-05-15-smart-mvp-p3-decisions.md` | 智能版目前正在实施中。P2 launch blockers 已闭环，但 P5 metrics、文档漂移清理、shadow verifier 后续试点仍未关闭。 |
| Smart analytics / Phase 1b reports | `IN_PROGRESS` | `2026-05-22-smart-analytics-v1.md`, `2026-05-21-speaker-tts-subtitle-alignment-optimization-plan.md` | 管理端 analytics 与报告分析已进入架构事实；新行为开关仍应保持 shadow/report-first，除非新方案明确提升为自动路径。 |
| 商业化 / 价格 / 支付 | `IN_PROGRESS` | `docs/specs/2026-04-04-pricing-and-plans-api-contract.md`, `2026-05-22-wechatpay-native-integration-plan.md` | Gateway 是套餐、试用、价格、权益和支付策略真源。微信支付 Native 仍是未来接入方案，当前 provider 仍以 stub/渐进迁移为主。 |
| CSRF / 生产安全 | Phase 1 write-route coverage `DONE` | `2026-05-21-samesite-csrf-decision-plan.md`, `2026-05-21-csrf-phase1*.md` | 当前决策是保留 `SameSite=Lax`，并对浏览器态写路由加 same-origin guard；不要简化描述成 `lax -> strict` bug fix。 |
| Pan backup | `IN_PROGRESS` | `2026-05-13-admin-pan-backup-design.md`, `2026-05-14-admin-pan-backup-implementation-plan.md`, `2026-05-18/19 pan handoff docs`, `docs/deployment/PAN_BACKUP_DEPLOY.md` | 管理端归档/恢复代码已存在；生产 rollout 仍取决于部署配置、OAuth credentials、dry-run 和 backfill。 |
| R2 / storage / cleanup | `IN_PROGRESS` | `2026-04-21-cloudflare-r2-deployment-plan.md`, `2026-04-23-phase2-r2-download-minimal.md`, `2026-05-07-disk-relief-via-r2-publisher-and-ttl.md` | 代码路径已存在，包含 parity cleanup；生产 token/config 与运维 rollout 仍是环境相关事项。 |
| Editing / post-edit | `IN_PROGRESS` | `2026-04-18-studio-post-edit-plan.md`, `2026-05-09-studio-editing-add-speaker.md`, `2026-05-17-edit-page-redesign.md`, `2026-05-17-edit-page-phase1-impl.md` | 主编辑模型已存在；UI/产品打磨和高级编辑流仍在迭代。 |
| Jianying draft delivery | `DONE`，仍有 polish | `2026-05-02-jianying-draft-delivery-integration-plan.md` | 按需生成剪映草稿已实现；注意清理仍声称仅 Studio 支持的旧注释/旧文档。 |
| Support / notifications | `IN_PROGRESS` | `2026-05-08-ai-customer-support-handoff-plan.md`, `docs/support/*` | 帮助中心、support orchestrator、通知、email/log handoff 已存在；Chatwoot 和微信客服仍是 deferred stub。 |
| Frontend experience | `DONE/PARTIAL` | `2026-05-21-frontend-experience-polish-plan.md`, `2026-05-21-frontend-polling-governance-plan.md` | 多数体验优化已落地；精确状态以代码和测试为准。 |
| CosyVoice domestic worker | `NOT_STARTED` | `2026-05-24-cosyvoice-domestic-worker-plan.md` | 与现有 CosyVoice public voice provider 是不同方向；大陆 worker/clone relay 尚未成为主路径。 |
| Hermes | `FROZEN` | `hermes/README.md`, `hermes/*.md` | 停放方向；没有新激活方案前不要实施。 |
| AI-workgroup | `ARCHIVED` | `docs/archive/ai-workgroup/` | 多智能体协作协议和消息记录，仅作历史追溯，不是当前执行系统。 |

## 文件状态索引

| 文件 | 状态 | 当前备注 |
| --- | --- | --- |
| `2026-04-09-prompt-model-management-plan.md` | `IN_PROGRESS` | `llm_registry` 和 admin model settings 已存在；legacy `LLMRouter` 兼容路径仍未完全清掉。 |
| `2026-04-15-i18n-target-language-direction.md` | `DEFERRED` | 非中文目标语泛化先停放，等真实第二语言对需求再启动。 |
| `2026-04-16-background-task-system-plan.md` | `DONE` | Background task system 与前端 polling hooks 已实现。 |
| `2026-04-16-ui-navigation-redesign-plan.md` | `DONE` | Next app 导航和翻译弹窗已实现。 |
| `2026-04-16-video-output-subtitles-player-plan.md` | `DONE` | 结果媒体、字幕和播放器输出已实现。 |
| `2026-04-17-legacy-migration-cleanup.md` | `DONE` | Legacy frontend/server cleanup 已完成，作为历史依据保留。 |
| `2026-04-17-llmrouter-deprecation.md` | `IN_PROGRESS` | 仍有 fallback/兼容路径时不能视为完全关闭。 |
| `2026-04-17-migration-debt-fixes.md` | `DONE/PARTIAL` | 大部分 debt fixes 已落地；SameSite 策略后来调整为 Lax + same-origin guard。 |
| `2026-04-18-express-studio-output-filter-plan.md` | `DONE` | Express/Studio artifact filtering 已实现。 |
| `2026-04-18-studio-post-edit-plan.md` | `IN_PROGRESS` | Editing 架构已存在，产品硬化继续。 |
| `2026-04-21-cloudflare-r2-deployment-plan.md` | `IN_PROGRESS` | R2 部署和生产行为仍依赖环境配置。 |
| `2026-04-21-phase01-implementation-checklist.md` | `RECORD` | 部署 checklist/记录。 |
| `2026-04-22-alipay-audit-marketing-site-cleanup-plan.md` | `DONE` | Marketing/audit cleanup 已完成。 |
| `2026-04-22-cloudflare-r2-phase-assessment.md` | `RECORD` | 阶段评估记录。 |
| `2026-04-22-phase01-rollout-notes.md` | `RECORD` | Rollout 记录。 |
| `2026-04-23-phase2-r2-download-minimal.md` | `IN_PROGRESS` | 代码已存在；生产 config/observability 决定最终 rollout 状态。 |
| `2026-04-24-video-translation-quality-cost-optimization-plan.md` | `IN_PROGRESS` | 部分质量/成本工作已落地；后续 Phase 1b report 工作替代了部分方向。 |
| `2026-04-28-job-level-llm-tts-cost-metering-plan.md` | `IN_PROGRESS` | UsageMeter 与 cost reports 已存在；完整 ledgering 仍是后续阶段。 |
| `2026-04-29-marketing-redesign-ink-aesthetic.md` | `DONE` | Marketing redesign 已实现。 |
| `2026-04-30-translation-quality-cost-optimization-plan.md` | `SUPERSEDED/DEFERRED` | 旧方向大体被 Phase 1a/1b report-first 质量工作替代。 |
| `2026-05-01-marketing-featured-demos-implementation.md` | `DONE` | Featured demos 已实现；runtime/admin demo 工具属于后续。 |
| `2026-05-02-code-review-report.md` | `RECORD` | Review 记录；执行前应与更新审计交叉核对。 |
| `2026-05-02-jianying-draft-delivery-integration-plan.md` | `DONE` | 剪映草稿生成集成已存在。 |
| `2026-05-02-subtitle-cue-generation-v2-plan.md` | `IN_PROGRESS` | Cue v2 foundation 已存在；完整 canonical migration 仍在推进。 |
| `2026-05-03-geo-optimization-plan.md` | `DONE/PARTIAL` | SEO foundation 已实现；后续 SEO 应另开新方案。 |
| `2026-05-03-runner-and-llm-audit-hardening-plan.md` | `DONE` | Runner hardening 与 LLM audit safeguards 已落地。 |
| `2026-05-04-smart-auto-pipeline-plan.md` | `IN_PROGRESS` | Smart 主方案；当前状态以 2026-05-24 rebaseline 为准。 |
| `2026-05-04-subtitle-audio-sync-plan.md` | `IN_PROGRESS` | Sync foundation 已落地；edit path refinements 继续。 |
| `2026-05-04-user-edit-audit-data-optimization-plan.md` | `DONE/PARTIAL` | Edit audit data 已存在；后续 analytics 可继续扩展。 |
| `2026-05-06-smart-precise-audio-separation-plan.md` | `NOT_STARTED` | 没有新批准和依赖复核前不要启动。 |
| `2026-05-06-smart-shadow-eval-p0-results.md` | `RECORD` | Smart P0 结果记录。 |
| `2026-05-06-smart-shadow-evaluator-design.md` | `RECORD` | P0 evaluator 设计记录；实施状态以 evaluator plan/results 为准。 |
| `2026-05-06-smart-shadow-evaluator-plan.md` | `DONE` | Smart P0 evaluator 已实现。 |
| `2026-05-06-smart-shadow-sim-design.md` | `RECORD` | P1 simulator 设计记录。 |
| `2026-05-06-smart-shadow-sim-p1-done-note.md` | `RECORD` | Smart P1 完成记录。 |
| `2026-05-06-smart-shadow-sim-plan.md` | `DONE` | Smart P1 simulator 已实现。 |
| `2026-05-07-disk-relief-via-r2-publisher-and-ttl.md` | `IN_PROGRESS` | R2 push/sweeper/parity 工作已存在；cleanup policy 仍需谨慎运维。 |
| `2026-05-08-ai-customer-support-handoff-plan.md` | `IN_PROGRESS` | P1/P2 support flow 已存在；Chatwoot/WeChat handoff 延后。 |
| `2026-05-08-p2-17-pipeline-parallelization-plan.md` | `DONE/PARTIAL` | Safe 17a parallelization 已落地；更大范围 parallelization 受共享状态约束。 |
| `2026-05-09-studio-editing-add-speaker.md` | `DONE/PARTIAL` | Add-speaker editing 核心已存在；audit/polish 后续仍在。 |
| `2026-05-09-studio-editing-add-speaker-deploy.md` | `RECORD` | 部署/handoff 记录。 |
| `2026-05-09-voice-cps-auto-calibration-plan.md` | `IN_PROGRESS` | Clone hook 和 review preflight 已存在；继续验证生产行为。 |
| `2026-05-10-codebase-audit-remediation-plan.md` | `RECORD/IN_PROGRESS` | Remediation planning 记录；行动前先看更新审计。 |
| `2026-05-10-heuristic-learning-smart-hermes-integration-plan.md` | `FROZEN` | Hermes-style learning 方向不活跃。 |
| `2026-05-13-admin-pan-backup-design.md` | `IN_PROGRESS` | 当前 Pan backup 设计依据。 |
| `2026-05-13-smart-mvp-p2-implementation-plan.md` | `IN_PROGRESS` | Smart 实施细节；最新状态看 2026-05-24 rebaseline。 |
| `2026-05-14-admin-pan-backup-implementation-plan.md` | `IN_PROGRESS` | Pan 实施计划；rollout 依赖部署凭据和配置。 |
| `2026-05-15-smart-mvp-p3-decisions.md` | `ACTIVE` | 当前 Smart P3/P4 决策记录；P3/P4 verifier auto path 延后。 |
| `2026-05-16-voice-clone-library-reuse-plan.md` | `DONE/PARTIAL` | Candidate-first reuse 已实现，仍需监控。 |
| `2026-05-17-edit-page-phase1-impl.md` | `DONE/PARTIAL` | Edit page phase 实施记录；以后续 UI 代码为准。 |
| `2026-05-17-edit-page-redesign.md` | `IN_PROGRESS` | Editing UI/产品 redesign 仍在迭代。 |
| `2026-05-17-user-voice-candidate-first-plan.md` | `DONE/PARTIAL` | Candidate-first voice UX/API 路径已存在；关注 auto-reuse 指标。 |
| `2026-05-18-pan-backup-phase5b-handoff.md` | `RECORD` | Pan backup handoff 记录。 |
| `2026-05-18-pan-backup-session-handoff.md` | `RECORD` | Pan backup session handoff。 |
| `2026-05-19-pan-backup-phase7b-handoff.md` | `RECORD` | Pan UI/API handoff；backend implementation 已不再未知。 |
| `2026-05-21-csrf-phase1a-admin-settings-disk-design.md` | `DONE` | Admin settings/disk write guard 阶段。 |
| `2026-05-21-csrf-phase1b-admin-write-coverage.md` | `DONE` | Admin write guard coverage 阶段。 |
| `2026-05-21-csrf-phase1c-user-write-coverage.md` | `DONE` | User write guard coverage 阶段。 |
| `2026-05-21-csrf-phase1d-job-auth-write-coverage.md` | `DONE` | Job/auth write guard coverage 阶段。 |
| `2026-05-21-csrf-phase1e-upload-job-subresource-coverage.md` | `DONE` | Upload/job subresource guard 阶段。 |
| `2026-05-21-csrf-phase1g-support-visitor-cookie.md` | `DONE` | Support visitor cookie write guard 阶段。 |
| `2026-05-21-frontend-experience-polish-plan.md` | `DONE/PARTIAL` | 多数 planned polish 已落地；精确 UI 状态看代码。 |
| `2026-05-21-frontend-polling-governance-plan.md` | `DONE` | Visibility-aware polling governance 已存在。 |
| `2026-05-21-samesite-csrf-decision-plan.md` | `ACTIVE` | 当前决策：保留 Lax，并依赖 same-origin write guards。 |
| `2026-05-21-speaker-tts-subtitle-alignment-optimization-plan.md` | `IN_PROGRESS` | Phase 1a/1b report-first 质量工作正在推进。 |
| `2026-05-22-smart-analytics-v1.md` | `IN_PROGRESS` | Analytics v1 已存在；auto-reuse 和 margin metrics 是后续。 |
| `2026-05-22-wechatpay-native-integration-plan.md` | `NOT_STARTED` | 当前代码仍是 WeChatPay stub provider。 |
| `2026-05-24-cosyvoice-domestic-worker-plan.md` | `NOT_STARTED` | 大陆 worker 设计；不是当前 CosyVoice public voice 主路径。 |
| `2026-05-24-smart-auto-pipeline-rebaseline.md` | `ACTIVE` | 当前 Smart 执行基线；Smart 整体仍按实施中管理。 |

## 停放子目录

| 目录 | 状态 | 备注 |
| --- | --- | --- |
| `hermes/` | `FROZEN` | 停放的内部 ops/control-plane 设计。 |

## 已归档方向

| 归档路径 | 状态 | 备注 |
| --- | --- | --- |
| `docs/archive/ai-workgroup/` | `ARCHIVED` | 历史多智能体协作协议和 inbox 消息。 |

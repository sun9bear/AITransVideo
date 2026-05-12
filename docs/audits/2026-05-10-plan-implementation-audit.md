# 方案执行状态审计

> 日期：2026-05-10
> 范围：`docs/plans/` 下全部 40 份方案文档
> 方法：逐份阅读方案原文 → 交叉验证代码库中实际存在/缺失的文件

---

## 状态汇总

| 状态 | 数量 | 占比 |
|---|---|---|
| **DONE** — 已完成 | 19 | 47.5% |
| **IN_PROGRESS** — 部分完成 | 7 | 17.5% |
| **NOT_STARTED** — 未启动 | 4 | 10.0% |
| **ABANDONED** — 已废弃 | 3 | 7.5% |
| **设计/记录文档**（非执行方案） | 7 | 17.5% |

---

## 一、已完成（DONE）

| # | 方案文档 | 方案范围 | 代码证据 |
|---|---|---|---|
| 1 | `2026-04-16-background-task-system-plan.md` | 异步导出任务（素材打包 zip + 视频生成 FFmpeg），DB 驱动队列、后台执行器、前端轮询 | `gateway/background_task_*.py` 全套、`src/services/jobs/video_render_async.py`、`frontend-next/src/lib/react/useBackgroundTask.ts`、14+19+4 测试 |
| 2 | `2026-04-16-ui-navigation-redesign-plan.md` | 导航重构：TranslationForm 抽取、NewTranslationDialog、/projects 升级为"视频翻译"主页、侧边栏重组 | `TranslationForm.tsx`、`NewTranslationDialog.tsx`、`/projects/page.tsx` 卡片化、`app-shell.tsx` 导航入口 |
| 3 | `2026-04-16-video-output-subtitles-player-plan.md` | 字幕切片（zh/en/bilingual）、视频流式 Range 206、ResultMediaCard、materials-availability | `editor_package_writer.py:26` SubtitleSlice、`api.py:447` stream 端点、`LazyVideoPlayer`、三轨混合 |
| 4 | `2026-04-17-legacy-migration-cleanup.md` | 死代码删除（frontend/、build/、server.py/handler.py、web-ui 子命令）、配置语义化、file_lock、回归守卫 | `src/services/_file_lock.py` 存在、`JOB_API_BASE` 硬编码已清零、`test_legacy_cleanup_guards.py` 10 个契约测试 |
| 5 | `2026-04-17-migration-debt-fixes.md` | 8 个关键修复：credits FOR UPDATE、continue_job 防重入、internal API 安全、错误净化、生产 auth 强制等 | `credits_service.py` 7 处 `with_for_update()`、`_continue_with_gateway_lock`、`_send_sanitized_error`、`validate_production_safety` |
| 6 | `2026-04-18-express-studio-output-filter-plan.md` | Express 模式输出过滤：仅显示配音视频，Studio 保留全套 artifact | `api.py:359` 按 service_mode 过滤、`EXPRESS_ALLOWED_DOWNLOAD_KEYS`、`ResultMediaCard.tsx` serviceMode prop、`test_job_api_express_filter.py` |
| 7 | `2026-04-21-phase01-implementation-checklist.md` | CF Tunnel 部署操作清单（~13 步） | Phase 0 probes 脚本存在、cloudflared-us 容器运行中、Caddy loopback 已配置、回滚演练 4s |
| 8 | `2026-04-22-alipay-audit-marketing-site-cleanup-plan.md` | 支付宝审核准备：运营信息集中、product-proof、pricing-assurance、法律页面更新 | `company-info.ts` 存在、`product-proof.tsx` 在主页渲染、`pricing-assurance.tsx` 在定价页渲染 |
| 9 | `2026-04-22-phase01-rollout-notes.md` | Phase 1 部署实录（Tunnel 割接、UFW 加固、回滚演练） | 已完成——这是事后记录，不是待执行方案 |
| 10 | `2026-04-29-marketing-redesign-ink-aesthetic.md` | Ink 美学营销站改版：CSS token、seal-stamp、ink-divider、hero 朱砂 CTA、trust-banner | `globals.css` `[data-theme="ink"]` tokens、`seal-stamp.tsx`、`ink-divider.tsx`、`hero.tsx:74` cinnabar CTA |
| 11 | `2026-05-01-marketing-featured-demos-implementation.md` | 5 组精选 Demo 片段（original+dubbed+poster）、marquee 轮播、`FeaturedDemos` 组件 | `public/marketing/demos/` 5 组 16 文件、`featured-demos*.tsx` 4 个组件、`globals.css:677` marquee keyframe |
| 12 | `2026-05-02-jianying-draft-delivery-integration-plan.md` | 剪映草稿按需交付：Runner、substep 状态机、fingerprint 缓存、API 端点、前端按钮 | `jianying_draft_runner.py`、`jianying_draft_*` 4 个模型字段、`POST /generate-jianying-draft`、`output_dispatcher.py` 无 jianying_backend（K1 rollback 确认） |
| 13 | `2026-05-03-geo-optimization-plan.md`（Phase 1） | Sitemap、robots.txt、SEO metadata、JSON-LD 结构化数据、middleware 放行 | `sitemap.ts`、`robots.ts`、`json-ld.tsx` 4 个组件、`middleware.ts` publicExactPaths、`(auth)/layout.tsx` noindex |
| 14 | `2026-05-03-runner-and-llm-audit-hardening-plan.md` | 剪映 Runner 加固（lock/fingerprint/substep/幂等）+ LLM 付费调用审计 | `jianying_draft_runner.py` file_lock、fingerprint + attempt_id + substep 字段、`classify_llm_error()` |
| 15 | `2026-05-04-subtitle-audio-sync-plan.md`（Phase A-D） | 字幕与配音精确同步：tts_input_cn_text → 同步检测 → faster-whisper DTW 对齐 | `DubbingSegment.tts_input_cn_text`、`cue_validator.py` text_audio_drift、`whisper_align/` 子进程 runner + DTW、指纹缓存 + 双闸门 |
| 16 | `2026-05-06-smart-shadow-evaluator-plan.md` | P0 离线评估工具：collector + analyzer + fixtures + guards | `smart_shadow_eval_collector.py`、`smart_shadow_eval_analyzer.py`、5 个 fixture 目录、5 个测试文件 |
| 17 | `2026-05-06-smart-shadow-sim-plan.md` | P1 shadow simulator：6 阶段决策模型 + diff_kind 分类 + aggregator | `smart_shadow_sim_simulator.py`、`smart_shadow_sim_aggregator.py`、21 commits、94 测试通过 |
| 18 | `2026-05-08-p2-17-pipeline-parallelization-plan.md`（17a） | 对齐并行化：ThreadPoolExecutor + paid_fallback semaphore | `AVT_ALIGN_MAX_WORKERS=2`、`_align_all_parallel()`、`test_aligner_concurrency.py` |
| 19 | `2026-05-09-studio-editing-add-speaker.md` | 编辑态新增说话人：speakers.json registry、voice profile 异步推断、segment 重分配、前端对话框 | `editing_speakers.py`、`editing_voice_profile.py`、`EditPageSpeakerCreateDialog.tsx`、`EditPageSpeakerProfileBadge.tsx` |

---

## 二、部分完成（IN_PROGRESS）

| # | 方案文档 | 已完成 | 待完成 | 关键缺失 |
|---|---|---|---|---|
| 1 | `2026-04-09-prompt-model-management-plan.md` | llm_registry 落地、reviewer provider dispatch、旧 _MODEL_MAP 删除、前端 prompts 管理页 | Gap 4：LLMRouter 观察期至 2026-05-16 | `router.py` 仍在运行、`process.py:93` 仍导入 LLMRouter |
| 2 | `2026-04-18-studio-post-edit-plan.md` | Phase 0 数据层完成（JobRecord editing 字段）、编辑状态机、8 个 editing 模块、前端编辑页、feature flag、idle scanner | Phase 1 主流程 production 验证未完成；SegmentVirtualList 共享组件未确认 | 编辑工作流已编码但未全流程 verified |
| 3 | `2026-04-23-phase2-r2-download-minimal.md` | 代码 T1-T13 全部完成：r2_client.py、backend_router.py、event 打点、14 个测试通过 | 生产 R2 token 未配置、三网验收未做、7 天稳定性观察未开始 | `gateway/storage/r2_client.py:1` 代码就绪但生产 `.env` 仍是 `backend=local` |
| 4 | `2026-04-24-video-translation-quality-cost-optimization-plan.md` | P0 benchmark fixture + credits_actual closed loop + pre-TTS rewrite 结构化字段 + short-DSP denoising | P1 duration_budget/gate_history、P2 speaker verifier、P3 multi-candidate translation | P1-P3 全部未启动 |
| 5 | `2026-04-28-job-level-llm-tts-cost-metering-plan.md` | usage_meter.py 落地、tts_billed_chars 路径修复、FIELD_STATUS LIVE_PARTIAL 标注 | `_report_job_metering` 不含 llm_usage_summary/tts_usage_summary 分桶字段；LLM_PRICE_CATALOG 不存在；JobUsageMeter 完整类不存在 | metering 有基础但不够精细 |
| 6 | `2026-05-02-subtitle-cue-generation-v2-plan.md` | Phase 1a/1b：SubtitleCue 模型、segmenter、builder、validator、SRT writer、weak-boundary + trailing punct | Phase 2：final_cn_lines 未从 SemanticBlock 移除；DraftBackend/CaptionRetimer 未迁移到 canonical cues；mixed-token 识别未完成 | `editor_package_writer.py:659` 旧 `_build_subtitle_slices` 仍存在 |
| 7 | `2026-05-09-voice-cps-auto-calibration-plan.md` | T0 基础设施（budget + inflight dedupe + bounded primitives）、T1 clone 后自动校准 hook | T2 review preflight 模块存在但**未接入** `job_intercept.py`；T3 admin batch 未实现 | `voice_calibration_review_preflight.py` 文件存在但在 `_approve_voice_selection_with_quality_sync` 中无调用 |

---

## 三、未启动（NOT_STARTED）

| # | 方案文档 | 方案范围 | 说明 |
|---|---|---|---|
| 1 | `2026-05-04-smart-auto-pipeline-plan.md`（P2-P4） | 智能版自动交付：P2 fake TTS/clone 套件、P3 verifier、P4 integration | P0+P1 已完成（shadow eval/sim 工具链），但 P2 起全部未启动——环境变量 `AVT_ENABLE_SMART_MODE` 全文零命中 |
| 2 | `2026-05-06-smart-precise-audio-separation-plan.md` | 智能版 AI 精准人声/背景分离（audio-separator 后端） | 零实施，7 个 phase 全部未启动；`precise_separator.py` 不存在；甚至 Phase 0 的 `-ac 2` 修复也未做 |
| 3 | `2026-05-07-disk-relief-via-r2-publisher-and-ttl.md`（Stage B） | R2 parity gatekeeper：`r2_parity.py`、cleanup delegation、3 AM 调度 | Stage A 已完成（proactive push + sweeper），Stage B 未启动——`r2_parity.py` 不存在 |
| 4 | `docs/plans/hermes/` | 内部监控运维控制面（Hermes）：Phase 1 ops、Phase 2 expansion、Phase 3 copilot | README 自述："代码落地：零 commit，尚未启动实施" |

---

## 四、已废弃/搁置（ABANDONED）

| # | 方案文档 | 废弃原因 | 证据 |
|---|---|---|---|
| 1 | `2026-04-15-i18n-target-language-direction.md` | 方案 A（DB 止血）完成，方案 B（完整 target_language 参数化）搁置等待真正的非英→中需求触发 | `VoiceMatchRequest.target_language` 字段预埋但上游 selector 未消费 |
| 2 | `2026-04-30-translation-quality-cost-optimization-plan.md` | Phase A/B/C/D 全部未落地——`translation_length_calibrator.py` 不存在，CPS ratio 仍硬编码 1.8 | `translator.py:2471`: `_ENGLISH_TO_CHINESE_CHAR_RATIO = 1.8` |
| 3 | `docs/plans/AI-workgroup/` | AI agent 协作协议（CodeX ↔ Claude Code ↔ Trae 星形拓扑），4 个 task 全部停留在 "ready" 状态，working/done 目录为空 | 协议定义了但从未执行 |

---

## 五、设计/评估/记录类文档（非执行方案）

这些文档本身不产生代码，属于设计规范、评估结果或部署记录：

| # | 文件 | 类型 |
|---|---|---|
| 1 | `2026-04-17-llmrouter-deprecation.md` | 观察期跟踪文档（2026-05-16 决策节点，观察中） |
| 2 | `2026-04-22-cloudflare-r2-phase-assessment.md` | 独立评估报告（已确认 Phase 1 75% 完成） |
| 3 | `2026-05-02-code-review-report.md` | 代码审查报告（3 HIGH 已修复，11 MEDIUM 待验证） |
| 4 | `2026-05-06-smart-shadow-eval-p0-results.md` | P0 实测结果报告（38-job 扫描） |
| 5 | `2026-05-06-smart-shadow-evaluator-design.md` | P0 评估器设计规范 |
| 6 | `2026-05-06-smart-shadow-sim-design.md` | P1 模拟器设计规范 |
| 7 | `2026-05-06-smart-shadow-sim-p1-done-note.md` | P1 完成记录（21 commits、94 测试） |

---

## 六、未完成任务的计划偏差（Deviation）

以下是与设计方案对比发现的**已规划但代码中缺失的具体事项**：

| 计划 | 偏差项 | 严重度 |
|---|---|---|
| `migration-debt-fixes.md` T8 | `auth.py:84` — `samesite` 仍为 `"lax"`，计划要求改为 `"strict"` | 中 |
| `ui-navigation-redesign.md` | `ProjectCard.tsx` 被规划但从未创建——功能通过内联卡片逻辑实现，无功能损失 | 低 |
| `pipeline-parallelization-plan.md` | 17b/17c/17d 被 Codex 审查明确禁止（数据依赖 + 共享状态风险），计划设计正确 | N/A（正确废弃） |
| `voice-cps-auto-calibration-plan.md` | T2 preflight 模块 `voice_calibration_review_preflight.py` 文件存在，但**未被 `job_intercept.py` 接入调用** | 高 |
| `studio-post-edit-plan.md` | editing 主流程虽已编码，但 `accept_draft_tts` 路径不写 `tts_input_cn_text` → text-audio sync 在 accept draft 场景下不工作 | 中 |
| `subtitle-audio-sync-plan.md` | Phase A5/A6 编辑回写：`editing_batch.py` 全文件零次提及 `tts_input_cn_text` | 中 |

---

## 七、按日期的趋势分析

```
2026-04-09 → 2026-04-17：基础架构期（8 方案）
  ├── DONE: 6/6（执行方案全部完成）
  └── OBSERVING: llmrouter-deprecation 观察中

2026-04-18 → 2026-04-23：商业化 + R2 部署期（8 方案）
  ├── DONE: 6/6（执行方案全部完成）
  ├── IN_PROGRESS: studio-post-edit（Phase 0 完成）+ phase2-r2-download（代码完成未上线）
  └── PARKED: i18n-target-language

2026-04-24 → 2026-05-02：质量/计量/营销/Jianying 期（8 方案）
  ├── DONE: 5/5（执行方案全部完成）
  ├── IN_PROGRESS: quality-cost-optimization + job-level-metering
  └── ABANDONED: translation-quality-cost-optimization

2026-05-03 → 2026-05-09：优化/智能/校准期（12 方案）
  ├── DONE: 5 方案（含 2 个 shadow eval 工具链）
  ├── IN_PROGRESS: smart-auto（P0+P1 完成，P2+ 未启动）
  ├── NOT_STARTED: smart-precise-separation + hermes + r2 stage B
  ├── ABANDONED: AI-workgroup
  └── IN_PROGRESS: subtitle-sync（Phase A-D 完成）、voice-calibration（T0+T1 完成）

```

**模式**：早期方案（4 月）执行率 90%+；5 月方案出现越来越多的 NOT_STARTED 和 IN_PROGRESS（部分完成）——反映系统复杂性增长后，"完整实施一个方案"的成本在上升，以及更多方案被设计为分阶段交付。

---

## 八、建议行动项

| 优先级 | 行动 | 关联方案 |
|---|---|---|
| **立即** | T2 preflight 接入 `job_intercept.py`（模块已存在，只差一行调用） | `voice-cps-auto-calibration-plan.md` |
| **本周** | 修复 `samesite="lax"` → `"strict"` | `migration-debt-fixes.md` T8 |
| **本周** | R2 production token 配置 + 三网验收 | `phase2-r2-download-minimal.md` |
| **本月** | LLMRouter 观察期到期 → 执行 11 步清理或再次延期 | `prompt-model-management-plan.md` `llmrouter-deprecation.md` |
| **本月** | editing batch 路径补齐 `tts_input_cn_text` 操作 | `subtitle-audio-sync-plan.md` Phase A5/A6 |
| **择机** | 决定 AI-workgroup 和 hermes 是否继续保留（超过 1 个月无进度） | 归档或激活 |

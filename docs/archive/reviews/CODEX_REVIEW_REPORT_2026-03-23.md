# 项目审计 + 商用就绪度报告

> 日期：2026-03-23
> 范围：代码质量、架构合理性、死代码、商用差距
> 基准：`encapsulated-growing-spark.md`（Phase 2 多用户 + Next.js 迁移）

---

## 当前进度快照

| Phase 2 步骤 | 状态 |
|---|---|
| Step 1 — FastAPI Gateway 层 | ✅ 完成 |
| Step 2 — PostgreSQL + 用户注册登录 | ✅ 完成 |
| Step 3 — Job 绑定 user_id + 隔离 | ✅ 完成 |
| Step 4 — Next.js + shadcn/ui 前端迁移 | ✅ 完成（11 页面全部实现） |
| Step 5 — 启用强制认证 | ✅ 完成（域名 + HTTPS + 登录 + basic_auth 已移除） |

### 补充完成项（2026-03-23 本次会话）

| 项目 | 状态 |
|------|------|
| Alembic 迁移管理 | ✅ 已存在（`gateway/alembic/versions/001_baseline.py`） |
| 文件下载归属校验 | ✅ Gateway 拦截 `/api/result-download` 和 `/api/project-file`，校验 job_id 归属 |
| PG 每日备份 | ✅ `scripts/pg_backup.sh` + cron 每日 3:00 执行，两台主机已配置 |
| Gateway 异常日志 | ✅ 4 处 `except: pass` → `logger.exception()` |
| Caddy basic_auth | ✅ 已移除，改为 Gateway session auth + 域名 HTTPS |

---

## 问题清单（按建议优先级排列）

### P0 — 阻塞商用上线

#### 1. ~~Caddyfile 残留 basic_auth，与 session auth 冲突~~ ✅ 已修复（2026-03-23）

- **修复内容**：Caddyfile 和 Python 生成器（`public_entry_caddy.py`）中的 `basic_auth` 块已移除，线上两台主机已部署域名 + Let's Encrypt HTTPS + Gateway session auth
- **域名**：`aivideotrans.site`（新加坡）、`us.aivideotrans.site`（美国）

#### 2. 无任务队列，单 job 硬限制

- **位置**：`src/services/jobs/service.py:84-88`
- **现状**：`submit_job()` 检测到有活跃 job 直接抛 `JobConflictError`（HTTP 409）。`JOB_STATUS_QUEUED` 只是初始状态名，创建后立即启动子进程，没有排队逻辑
- **影响**：多用户环境下，一个用户提交任务后其他所有用户都被阻塞，无法使用。商用场景完全不可接受
- **修复方向**：
  - 移除 `_find_active_job()` 冲突检查
  - 新 job 以 `queued` 状态入库，不立即启动
  - `ProcessJobRunner` 增加队列调度：当前 job 结束后自动拉取下一个
  - Gateway DB 增加 `queue_position` 字段（可选）
- **工作量**：3-5 天

#### 3. 无用量计量与计费

- **现状**：零用量追踪。无 API 调用计数、无视频时长统计、无用户配额、无费用记录
- **影响**：无法收费、无法控制成本、无法提供定价方案
- **修复方向**：
  - Job 级别记录：视频时长、ASR 调用次数、翻译 token 数、TTS 调用次数
  - 用户级别累计：月度用量、配额上限
  - 后续接入支付（Stripe 等）
- **工作量**：5-8 天（计量）+ 后续计费集成

#### 4. 无存储保留策略，磁盘无限增长

- **位置**：`src/services/jobs/`、项目输出目录
- **现状**：产出文件（视频、音频、字幕、中间件）永久保留，无 TTL、无自动清理、无磁盘告警
- **影响**：多用户使用后磁盘迅速耗尽，无法预估存储成本
- **修复方向**：
  - 定义保留策略（如：结果保留 30 天、中间文件 7 天）
  - 定时清理任务
  - 磁盘水位告警
- **工作量**：2-3 天

---

### P1 — 上线后会被频繁投诉

#### 5. ~~`web_ui.py` 9,386 行巨型单文件~~ ✅ 已完成（2026-03-23）

- **修复内容**：删除 5,592 行死代码（内嵌 HTML/CSS/JS，已被 Next.js 取代），剩余 3,794 行拆分为 `src/services/web_ui/` 包（16 个模块，最大 831 行）
- **详见**：`docs/reviews/OPTIMIZATION_REPORT_2026-03-23.md`

#### 6. 无通知系统，任务完成/失败用户无感知

- **现状**：任务完成后无任何通知。用户必须手动刷新页面查看状态
- **影响**：视频翻译通常需要 10-60 分钟，用户不可能一直盯着页面
- **修复方向**：
  - 最小方案：邮件通知（任务完成/失败）
  - 增强方案：WebSocket 推送 + 浏览器通知
- **工作量**：2-4 天（邮件）

#### 7. 长视频稳定性未验证

- **现状**：`COMMERCIAL_READINESS_CHECKLIST.md` 明确标记为风险项。50+ 分钟视频成功率未知，无分段处理
- **影响**：用户提交长视频后可能失败且无法恢复，退款纠纷
- **修复方向**：
  - 设定明确的时长上限（如 30 分钟）并在 UI 提示
  - 压测常见时长区间，记录成功率
  - 长期：实现分段处理
- **工作量**：2-3 天（限制 + 压测）；分段处理 >> 1 周

#### 8. docker-compose 硬编码默认密码

- **位置**：`docker-compose.yml` 第 48 行
- **现状**：`POSTGRES_PASSWORD: ${PG_PASSWORD:-avt_dev_2026}`，默认密码明文写在仓库中，且 postgres 使用 `network_mode: host` 暴露 5432 端口
- **影响**：安全隐患，任何可访问主机的人可用默认密码连接数据库
- **修复**：移除默认值，要求 `.env` 必须配置；或改为 docker network 内网通信
- **工作量**：< 1 小时

#### 9. ~~Gateway 吞掉异常不记日志~~ ✅ 已修复（2026-03-23）

- **修复内容**：`gateway/job_intercept.py` 中 4 处 `except Exception: pass` 全部改为 `logger.exception()`，异常信息将记录到日志

#### 10. CODEX_REVIEW_REPORT P1 安全发现未验证修复

- **位置**：`CODEX_REVIEW_REPORT_2026-03-19.md`
- **现状**：报告列出 6 条 P1 发现（Review 端点信任 caller-supplied `project_dir`、音频预览绕过白名单等），标记为已关闭但无修复代码证据
- **影响**：如果未实际修复，存在路径遍历/文件读取风险
- **修复**：逐条复查代码，确认每条 P1 已有对应防护
- **工作量**：1-2 天

---

### P2 — 影响产品竞争力与可维护性

#### 11. 语言方向硬编码 En→Zh

- **位置**：`ProjectWorkflowConfig` — `translation_target_language: str = "zh-CN"`
- **现状**：目标语言锁定中文，无 UI 选择，源语言依赖 ASR 但无用户控制
- **影响**：产品只能服务英→中场景，市场极度受限
- **修复方向**：UI 增加语言对选择 → config 传参 → 翻译/TTS 提供商按语言路由
- **工作量**：5-8 天

#### 12. ~~Provider 配置加载代码大量重复~~ ⚠️ 部分修复（2026-03-23）

- **已修复**：`_summarize_config_source()` 和 `_classify_api_key_source_type()` 两个完全重复的 helper 函数提取到 `src/services/provider_config_helpers.py` 共享模块
- **未做**：`from_env()` 中 10 个字段 resolve 调用的结构性重复推迟处理（风险/收益比不够理想，需更多测试覆盖后再动）

#### 13. 前端 `getErrorMessage()` 复制 9 份

- **位置**：`NewTranslationPage.tsx`、`CurrentTaskPage.tsx`、`ProjectDetailPage.tsx`、`MyProjectsPage.tsx`、`SpeakerReviewPage.tsx`、`TranslationReviewPage.tsx`、`VoiceReviewPage.tsx`、`TranslationConfigReviewPage.tsx`、`VoiceLibraryPage.tsx`
- **现状**：完全相同的函数（判断 ApiError → Error → fallback 消息）复制了 9 次
- **影响**：修改错误处理策略需改 9 处
- **修复**：提取到 `lib/api/errors.ts` 导出
- **工作量**：30 分钟
- **备注**：如果旧 frontend/ 将废弃（已有 frontend-next/），此问题只需在 frontend-next 中避免即可

#### 14. 前端工具函数重复

- **现状**：
  - `asString()` 在 `voiceLibrary.ts` 和 `SettingsPage.tsx` 各定义一次
  - `normalizeText()` 在 `reviews.ts` 和 `presentation.ts` 各定义一次
  - 剪贴板复制逻辑在 `VoiceLibraryPage` 和 `SettingsPage` 重复
- **修复**：提取到 `lib/utils/`
- **工作量**：30 分钟
- **备注**：同 #13，如果旧 frontend/ 将废弃则只需在 frontend-next 中避免

#### 15. 前端 Review 页面过大（500-700 行/个）

- **位置**：`routes/review/` 下 4 个页面
- **现状**：`TranslationReviewPage` ~700 行，`SpeakerReviewPage` ~600 行，包含数据获取、分页、表单、提交逻辑
- **影响**：组件难以测试和复用
- **修复**：拆分为子组件 + 自定义 hooks
- **工作量**：2-3 天
- **备注**：同上，frontend-next 中如果已重新实现，旧代码可不处理

#### 16. 轮询间隔魔法数字散落

- **现状**：前端各页面轮询间隔硬编码不一致（3000ms/4000ms/8000ms/15000ms/30000ms），无集中配置
- **修复**：提取到 `lib/config/polling.ts` 常量文件
- **工作量**：30 分钟

#### 17. 无用户文档 / 帮助中心

- **现状**：无 FAQ、无新手引导、无产品文档、无操作说明
- **影响**：商用用户上手成本高，客服压力大
- **修复**：至少提供基础使用指南 + 页面内 tooltip
- **工作量**：3-5 天

#### 18. 无监控告警

- **现状**：仅有 `/gateway/health` 端点和 docker healthcheck，无 Prometheus/metrics、无日志聚合、无告警
- **影响**：生产故障无法及时发现
- **修复方向**：最小方案用 structured logging + 简单告警脚本；完整方案接 Prometheus + Grafana
- **工作量**：2-3 天（最小方案）

---

### P3 — 技术债务，择机清理

#### 19. 前端死代码

| 位置 | 内容 |
|------|------|
| `NewTranslationPage.tsx:29-30` | `voiceA`/`voiceB` state 声明后从未使用 |
| `presentation.ts:210` | `getReviewPageMessage()` 导出后从未调用 |
| `lib/api/jobs.ts:61` | `continueJob()` 导出后从未引用 |

#### 20. 类型安全薄弱

- 前端大量 `Record<string, unknown>` 代替正确类型（`reviewGate`、`errorSummary`、`fallback_summary`）
- 后端 `control_panel.py` 使用 `type: ignore[attr-defined]` 动态属性赋值
- `config_loader.py` 用 `dict[str, object]` 作为类型

#### 21. API 响应格式不统一

- Gateway 返回 `{"user": {...}}`、`{"success": true}`
- Job API 返回 `{"jobs": [...]}`、`{"job": {...}}`
- 错误消息 Gateway 用中文，Job API 用英文
- 无统一 response envelope、无 OpenAPI 文档

#### 22. API 密钥管理——全局共享，无隔离

- **现状**：所有用户共享同一组 provider API key（OpenAI/Gemini/MiniMax 等），明文存于 `autodub.local.json` 或环境变量
- **影响**：无法按用户计费、无法隔离用量、密钥泄露影响所有用户
- **修复方向**：短期可接受（平台统一采购 key）；长期需要 per-tenant key vault

#### 23. ~~`.gitignore` 不完整~~ ✅ 已完成（2026-03-23）

- **修复内容**：16 行 → 45 行，新增 `.env`/`.env.*`、`node_modules/`、`.next/`、`*.tsbuildinfo`、IDE、构建产物、临时文件等规则

#### 24. 后端其他大文件

| 文件 | 行数 |
|------|------|
| `pipeline/process.py` | 2,594 |
| `services/control_panel.py` | 1,909 |
| `services/gemini/translator.py` | 1,697 |
| `modules/media_understanding/providers.py` | 1,521 |

#### 25. ~~50+ 文档文件平铺根目录~~ ✅ 已完成（2026-03-23）

- **修复内容**：48 个 .md 文件从根目录归类到 `docs/phases/`(10)、`docs/acceptance/`(10)、`docs/architecture/`(12)、`docs/deployment/`(6)、`docs/reviews/`(5)、`docs/`(5) 六个子目录

#### 26. 配置环境变量无文档

- `docker-compose.yml` 引用多个 env var（`AIVIDEOTRANS_ROOT`、`PG_PASSWORD`、auth 相关）但无 `.env.example`
- Provider env var 前缀不一致（`AUTODUB_TRANSLATION_*` vs `AUTODUB_TTS_*`）
- 配置来源优先级（env → file → hardcoded）无文档说明

---

## 已关闭条目汇总（截至 2026-03-23）

| 条目 | 优先级 | 关闭方式 |
|------|--------|----------|
| #1 Caddy basic_auth | P0 | 另一会话修复：移除 basic_auth，部署域名 + HTTPS |
| #5 web_ui.py 拆分 | P1 | 本会话：删除 5592 行死代码 + 拆分为 16 模块 |
| #9 Gateway 异常日志 | P1 | 另一会话修复：4 处 pass → logger.exception() |
| #12 Provider 重复代码 | P2 | 本会话：部分修复（helper 提取） |
| #23 .gitignore | P3 | 本会话：16 → 45 行 |
| #25 文档整理 | P3 | 本会话：48 文件归类到 6 子目录 |
| 文件下载归属校验 | 补充 | 另一会话：Gateway 拦截校验 job_id 归属 |
| PG 每日备份 | 补充 | 另一会话：pg_backup.sh + cron |

## 商用最短路径总结（更新版）

```
现在 ──── P0 (2-3周) ──── P1 (2-3周) ──── 最小可商用
         │                 │
         ├ #1 ✅ 已关闭      ├ #5 ✅ 已关闭
         ├ #2 任务队列       ├ #6 通知系统
         ├ #3 用量计量       ├ #7 长视频限制+压测
         └ #4 存储保留       ├ #8 密码安全
                            ├ #9 ✅ 已关闭
                            └ #10 安全复查
```

**P0 剩余 3 项（#2 任务队列、#3 计量计费、#4 存储保留），P1 剩余 3 项（#6 通知、#7 长视频、#8 密码、#10 安全）。**
**预估：剩余 P0 + P1 ≈ 4-5 周全职开发，可达到最小可商用状态。**

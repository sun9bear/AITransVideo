# Cloudflare + R2 + 双境外节点部署优化方案

- 创建日期：2026-04-21（v1 初稿）
- 修订日期：2026-04-21（v2，吸收 CodeX 一审意见后重写）
- 修订日期：2026-04-21（v3，吸收 Claude Code 二审 + CodeX 二审后重写；**方向性调整：US 继续作主节点，SG 先不启用**）
- 修订日期：2026-04-21（v4，吸收 CodeX 三审后重写；**R2 走原生域名 + Phase 0/3 加探针 + MVP 目标降级**）
- 作者：sun9bear（Claude Opus 4.7 协助起草与修订）
- 状态：**待实施**
- 依赖 / 关联：
  - `docs/plans/2026-04-16-background-task-system-plan.md`（后台任务系统，复用其 task 模型）
  - `docs/plans/2026-04-17-legacy-migration-cleanup.md`（内部 API key / config dir 约定）

---

## v2 修订摘要（2026-04-21）

v1 初稿在代码级落地上与当前仓库实际情况有若干不匹配，CodeX 审核后全部吸收：

| 修订点 | v1 错误 / 过于乐观 | v2 修正 |
|--------|---------|---------|
| **域名结构** | 拆 `app.*` + `api.*` 双子域 | **单域名** `app.yourdomain.com`，路径前缀路由 `/job-api/*` / `/api/*` / `/gateway/*`。现有 cookie `SameSite=strict` + host-only（[auth.py:80-88](../../gateway/auth.py)）+ 前端 `NEXT_PUBLIC_JOB_API_BASE_URL` 默认相对路径（[config.ts:1](../../frontend-next/src/lib/api/config.ts)），双域会断登录态 |
| **Tunnel 入口** | `cloudflared → http://localhost:443` + Caddy Let's Encrypt + 关 80 端口 | **Tunnel 直连 upstream**：`localhost:3000` (Next) / `localhost:8880` (Gateway)。Caddy 可保留（`tls internal` 本地自签）或直接绕过。ACME 挑战不再需要 |
| **R2 source 接入** | 假设有 `src/pipeline/ingest.py`；pipeline 按需从 R2 拉 | **边界归一化**：Gateway 侧在启动 pipeline **前**把 R2 对象下载到 `project_dir/source.xxx`。pipeline 内部零改动，[process_runner.py:276](../../src/services/jobs/process_runner.py) 继续传本地 `--source-ref`。不引入 `downloading_source` 新状态 |
| **下载主路径** | 只改了 `background_task_api.py` materials pack | 覆盖 [src/services/jobs/api.py](../../src/services/jobs/api.py) 的 `/jobs/{id}/download/{key}` 和 `/jobs/{id}/stream/{kind}`（真正吃带宽的是这两个）+ [downloads.ts](../../frontend-next/src/lib/api/downloads.ts) 的 URL 构造。下载返回 302 或 JSON；stream 端点短期保留本地（Range 播放），后期迁 R2 |
| **Pipeline 文件名** | 引用不存在的 `ingest.py` / `publish.py` | 改为真实文件：[src/pipeline/process.py](../../src/pipeline/process.py)（pipeline 唯一入口）+ `src/services/jobs/` 相关模块 |
| **前端运行时** | 方案强制迁 `@cloudflare/next-on-pages`（已 deprecated） | **前端不迁运行时**。保留 Next.js 16 + standalone + Docker 容器（现状），Caddy 或 Tunnel 直连容器。如果后期决定迁，走 **Cloudflare Workers + OpenNext**（官方推荐方向），不走 Pages |
| **Phase 顺序** | ① Tunnel ② 上传 ③ 下载 ④ YouTube | **① Tunnel 单域名隐藏源站 → ② R2 承接下载（解决带宽/磁盘）→ ③ 浏览器直传 R2 → ④ US YouTube worker**。先把"运维止血"和"用户体验提升"解耦，不把大陆提速绑定到运行时迁移 |
| **R2 source 工时** | 0.5d | **2-3d**：新字段 + migration + Gateway 下载前置 + `source_metadata` + 测试 |
| **验收硬指标** | `首屏 <2s`、`API P95 <1s` | 改为"**相对基线提升**"：上线前先用电信/联通/移动各跑一次基线（mtr / 下载 speedtest / 首屏 wpt），上线后对比取差值。不在方案里写绝对数字 |

**v2 版本后续章节全部对齐以上修正**。若读到旧设计（`app/api` 拆分、`NEXT_PUBLIC_API_BASE`、pipeline ingest/publish、`downloading_source` 状态、next-on-pages 构建命令），以本修订摘要 + v2 正文为准。

---

## v3 修订摘要（2026-04-21）

v2 经 Claude Code 二审 + CodeX 二审，又找出若干代码级偏差和方向性优化空间。v3 全部吸收（详细对账见 § 16 附录）：

| 修订点 | v2 问题 | v3 修正 |
|--------|--------|--------|
| **主节点策略** | SG 当主 + US 当 YouTube worker，Phase 1 上线即需 US→SG 数据迁移 | **US 继续作主节点**（零数据迁移），所有 Phase 1-5 改造都在现网 US 落地。SG 机器暂不启用；仅在实测大陆用户体验不达标时作为 Phase 6 可选迁移目标。YouTube 下载能力**不拆独立 worker**，就是 US 主节点的一个现有子能力 |
| **下载响应格式** | D28（JSON `{download_url}`）与 § 5.2.1（302）自相矛盾 | **统一 302-only**。删 D28 的 JSON 方案；前端 `downloads.ts` / `ResultMediaCard.tsx` 零改动 |
| **事件契约** | 方案写 `source.download_started` 等新 `event_type`，但 [events.py:8-12](../../src/services/jobs/events.py) 只认 `log` / `status`；[mappers.ts:119-123](../../frontend-next/src/lib/api/mappers.ts) 不消费 payload | 改用**现有 `status` 事件 + payload 承载子阶段**（`payload.sub_stage = "r2_downloading"` 等）。不扩 `event_type` 枚举，不改 events.py / mappers.ts 的基础契约；仅在前端 UI 组件里补读 `payload.sub_stage` 渲染文案（工时 0.3d）|
| **`api.py` 代码风格** | § 5.2.3 示例用 FastAPI `@router.get`，但 [api.py:8](../../src/services/jobs/api.py) 是 `BaseHTTPRequestHandler + ThreadingHTTPServer`，手工路由分派 | § 5.2.3 重写为 `do_GET` / `self.send_response(302)` 风格，贴合现有代码 |
| **`source_type` 枚举** | 方案写 `local_file`；但 [models.py:11](../../src/services/jobs/models.py) 正式值是 `local_video`，`local_file` 只在 [job_intercept.py:496-500](../../gateway/job_intercept.py) 做兼容归一化 | 全文统一 `youtube_url \| local_video \| r2`；`local_file` 仅作为历史兼容别名说明 |
| **不存在的文件引用** | B9 引用 `src/services/jobs/record_line.py` | 改为真实文件 [models.py](../../src/services/jobs/models.py)（`JobRecord.to_dict/from_dict`）+ [store.py](../../src/services/jobs/store.py)（`JobStore.save_job/load_job`）|
| **background_task 注册** | B12 只写"入队"一句，未列 `TASK_EXECUTORS` 注册 | 补 B12.5：改 [background_task_executors.py:277](../../gateway/background_task_executors.py) 注册表新增 `publish_artifacts_to_r2` / `backfill_legacy_artifact` 两个 executor；工时 +0.3d |
| **R2 分片大小** | 10 MB/片 → 2GB 文件 = 200+ Class A op/次，1000 任务/月即吃完 10% 免费额度 | 改 **25 MB/片**（R2 推荐范围 8-100 MB）→ 2GB = ~82 op/次。成本表 § 8.3 补 op/月列 |
| **CF 大陆 Plan B** | v2 只说 "重试"，对 GFW 偶发干扰无应对 | 新增 § 10.3：R2 强制走 `files.yourdomain.com` Proxied（默认 `*.r2.cloudflarestorage.com` 大陆常被 RST）；备选边缘（EdgeOne 海外 / Gcore）DNS CNAME 切换预案；不承诺 SLA |
| **MVP 打包** | Phase 1-6 一口气 16d | **Phase 1+2 = MVP（5d）**：CF Tunnel + R2 下载（最高 ROI）。上线后跑基线，数据不好直接撤退；数据好再继续 Phase 3-5 |
| **撤退方案** | v2 分 Phase 回滚，缺整体撤退 | 新增 § 11.6：三网实测如果任一不达标，DNS 切回 US 直连 IP + 保留老代理路径 4 周 |
| **`/stream/{kind}`** | v2 推迟到 Phase 6 | Phase 2 **条件 opt-in**：任务时长 ≤ 25 min 走 R2 预签名 URL（30 min 过期够播完）；> 25 min 保留本地 Range 流 |
| **§0.3 目标 SLA** | 残留"首屏 <2s、P95 <1s"硬数字，与 D29 相对基线口径冲突 | 去掉硬数字，与 § 11.3 / § 15 基线快照对齐 |

**v3 版本后续章节全部对齐以上修正**。若读到旧设计（SG 当主、US 独立 Worker、JSON `{download_url}`、`source.*` 新事件类型、`local_file` 枚举、`record_line.py`、10MB 分片），以本修订摘要 + v3 正文为准。

---

## v4 修订摘要（2026-04-21）

v3 经 CodeX 三审后发现 R2 能力边界硬冲突 + 前端 payload 链路断点 + 内部口径矛盾。v4 全部吸收（详细对账见 § 18 附录）：

| 修订点 | v3 错误 / 过于乐观 | v4 修正 |
|--------|---------|---------|
| **R2 + custom domain presign** | D33 / § 4.4 / r2_client 双 endpoint 设计假设 presigned URL 可签给 `files.yourdomain.com`。**Cloudflare 官方明确不支持**：[Presigned URLs 文档](https://developers.cloudflare.com/r2/api/s3/presigned-urls/) 原文 "Presigned URLs work with the S3 API domain and cannot be used with custom domains" | **全部走原生 `<account>.r2.cloudflarestorage.com`**。D33 推翻重写为：不强制 custom domain；r2_client 去掉 `get_r2_public_client` 双 endpoint；§ 4.4 custom domain 段落改为"Phase 2b 备胎方案" |
| **MVP 加速承诺** | "大陆下载速度 ≥ 3x 提升" / "首屏 LCP ≥30% 下降" 当作硬门槛 | **改为"不劣化或小幅提升 + US 源站出站带宽显著下降（≥50%）+ 回滚简单"**。大陆加速效果改为 Phase 0 三网实测数据决定（D36）|
| **Phase 0 探针深度** | 只跑"当前 US 直连"基线 | **加三类必测探针**（D37）：① `app.yourdomain.com` 经 Tunnel 的三网访问；② `*.r2.cloudflarestorage.com` 真实下载稳定性；③ **一次真实 multipart 上传样本**（不等 Phase 3）。三项都入 § 15 基线快照 |
| **Phase 3 确定性** | 4 天确定实施项 | **降级为"上传可行性灰度关卡"**（D38）。Phase 0 上传探针 fail → Phase 3 要重评审方案；Phase 0 上传探针 OK → Phase 3 按原计划但灰度优先 |
| **payload 链路到 UI 的完整性** | F9 "只改 UI 组件"，工时 0.3d | [mappers.ts:115-125](../../frontend-next/src/lib/api/mappers.ts) `toJobLogEntries` 丢弃 payload；[types/jobs.ts:91-99](../../frontend-next/src/types/jobs.ts) `JobLogEntry` 无 payload 字段 → F9 必须扩 3 处：types + mappers + UI。工时 0.3d → **0.5d** |
| **`/stream` 口径矛盾** | § 5.2.1 端点全景表写 "Phase 2 不动"；D35 + § 5.2.5 写 "条件 opt-in" | **全文统一为条件 opt-in**：§ 5.2.1 表 / D7 / D35 / § 5.2.5 / 验收清单、Smoke Test 都改为"`≤25 min` 走 R2 302 / `>25 min` 本地 Range" 一套说法 |
| **Phase 2 磁盘措辞** | "磁盘不再增长" | Phase 2 不启用 TTL 清理 + pipeline 仍落盘本地 workspace，真实效果是**出站带宽转嫁**不是磁盘止血。改为"**出站带宽显著下降 + 为 Phase 5 磁盘清理铺路**" |
| **方向 B（public bucket + Worker HMAC）** | v3 不存在 | 新增 **Phase 2b 备胎**：仅当 MVP 实测"应用访问正常但 R2 原生域名下载不稳"时启用，用 Cloudflare Worker 校验 HMAC（免费额度够），artifacts bucket 设 public + custom domain。**不影响上传链路**（上传桶必须私有）|
| **方向 C（本地兜底）** | v3 不存在 | 新增 `force_local=1` **运维开关**（D40）：任一下载端点收到此参数时忽略 R2 走本地 FileResponse。仅作紧急兜底，不作主方向 |

**v4 后续章节全部对齐以上修正**。若读到 v3 残留（double endpoint、custom domain 必须、3x 加速承诺、Phase 3 确定 4 天、`/stream` 表内"不动"），以本修订摘要 + v4 正文为准。

---

## 0. 背景与目标

### 0.1 现状痛点

1. **网络隔离**：主要用户在中国大陆，当前单台美国服务器（`5.78.122.220`）大陆不可直连，测试用户用代理访问。
2. **核心 API 境外绑定**：Gemini / AssemblyAI / YouTube 必须境外调用，国内无法替代。
3. **TTS provider 全部在国内**：MiniMax（新加坡+北京）、VolcEngine 豆包、CosyVoice，境外服务器已验证可调用。
4. **存储天花板**：当前 US 节点单盘 150 GB，单任务 1.4-2.8 GB（含源视频），百任务即爆。
5. **上传/下载链路经 Python Gateway**：[gateway/upload.py:17](../../gateway/upload.py) 2 GB 限制，单请求无分片；[gateway/background_task_api.py:203-243](../../gateway/background_task_api.py) 老任务走 Python 进程流 zip。大陆上传美国必然超时。
6. **域名未备案**：不走国内 CDN / OSS，需完全基于海外基础设施。
7. **运维裸奔**：无监控、无备份、无告警、部署靠 `Deploy-Via-154.cmd` 手动脚本。

### 0.2 资源约束

- **域名**：国外域名，**不做 ICP 备案**
- **服务器**：已有**美国 5.78.122.220**（当前生产主节点）+ **新加坡 5.223.84.82**（v3 阶段暂不启用，留作 Phase 6 可选迁移目标）
- **预算**：初期用户少，**尽量压低月度成本**
- **扩展性**：方案要可平滑演进到多 worker / 多 region；但初期不做提前优化

### 0.3 目标（v3：相对基线提升，不写绝对数字）

1. 大陆用户访问速度**相对 Phase 0 基线显著提升**（三网实测 LCP / API RTT / 下载速度改善比例参见 § 11.3 验收标准 + § 15 基线快照）
2. 上传 2 GB 源视频**支持分片 + 断点续传**，**不经 Gateway Python 进程**
3. 成品视频下载**走 CDN**，**不吃源站出站带宽**
4. 本地磁盘占用**不再单调增长**（Phase 5 启用 TTL 清理后稳定）
5. **零暴露源站 IP**（全部 Cloudflare Tunnel）
6. 月度总成本**可控**（初期仅增加 R2 使用费；具体见 § 8 成本估算）

### 0.4 非目标

- ICP 备案、国内 OSS/CDN（等用户量爆发再考虑）
- 多 region HA（单节点 + Cloudflare SLA 对初期用户量足够）
- Kubernetes / 容器编排升级（docker-compose 继续用）
- **US→SG 数据迁移**（初期不做；仅 Phase 6 作为可选路径备选，见 § 7 Phase 6 和 § 附录 C）

---

## 1. 核心决策汇总

| 编号 | 决策 | 结论 |
|------|------|------|
| D1 （v3 改）| 主计算节点位置 | **美国（现网 5.78.122.220）**。零数据迁移；Phase 1-5 所有改造在此落地。SG 新加坡节点 v3 阶段不启用，留作 Phase 6 可选迁移目标（实测大陆体验不达标时再启用）|
| D2 （v3 改）| YouTube 下载 | **不拆独立 Worker**。YouTube 下载能力仍然是 US 主节点的现有 pipeline 子步骤（[src/modules/ingestion/youtube/downloader.py](../../src/modules/ingestion/youtube/downloader.py) 不动），改造只在"下载完成后把源视频推 R2"这一步。US 本来就在境外，无跨境 IP 风控动机 |
| D3 | 前端托管（v2 改） | **保留 Next.js 16 + standalone + Docker 容器**（现状不变）。Tunnel 直连容器 `localhost:3000`。`next-on-pages` 已 deprecated，Pages 方案暂不采用；后期如迁走 **Workers + OpenNext**（官方引导） |
| D4 | API 跨境通路 | **Cloudflare Tunnel**（cloudflared）。零入站端口暴露，源站 IP 隐藏 |
| D5 | 对象存储 | **Cloudflare R2**（S3 兼容，出口流量免费，免费额度 10 GB）|
| D6 | 上传路径 | **浏览器 → R2 Multipart 直传**。Gateway 只发预签名 URL，不经手文件字节 |
| D7 （v4 改）| 下载路径 | **Gateway 鉴权 → 302 跳转 R2 预签名 GET URL**。改造范围：`/job-api/jobs/{id}/download/{key}`（主下载）+ `/api/jobs/{id}/tasks/{task_id}/download`（materials pack）+ `/job-api/jobs/{id}/tts-segments-zip` + `/job-api/jobs/{id}/stream/{kind}`（**条件 opt-in**：≤25min 走 R2 302，>25min 保留本地 Range，见 D35）|
| D8 （v3 改）| YouTube 下载流 | US 主节点本地 yt-dlp 下载到 `jobs/{job_id}/source.xxx`（现有流程不变）→ pipeline 完成后，**成品视频推 R2**（不是源视频）。源视频仅作为处理中间产物，由 Phase 5 的 TTL 清理处理 |
| D9 | 本地磁盘策略 | 只存**处理中任务的临时产物**（30 天 TTL 清理）；成品立即推 R2 |
| D10 | PG 备份 | 每日 cron `pg_dump` → `gzip` → `aws s3 cp` 到 `r2://avt-backups/`，保留 30 天 |
| D11 | 监控 | **Uptime Kuma 自建**（SG 上跑容器），免费，告警推飞书/TG webhook |
| D12 （v4 改）| 域名结构 | **单域名 `app.yourdomain.com`**（保留现有 `NEXT_PUBLIC_JOB_API_BASE_URL` 相对路径默认值），所有 `/job-api/*` `/api/*` `/gateway/*` 都走同一域。cookie same-origin 不改。**R2 走原生域名 `<account>.r2.cloudflarestorage.com`**（D33 + v4：不配 custom domain）|
| D13 | 向后兼容 | 老任务 `FileResponse` 路径保留 fallback，新任务强制 R2 路径 |
| D14 | 存储 bucket 分桶 | `avt-uploads` / `avt-artifacts` / `avt-backups` 三桶独立生命周期 |
| D15 （v3 改）| 上传分片大小 | **25 MB/片**（R2 推荐 8-100 MB）。2 GB 文件 ≈ 82 op，比 10 MB 方案 200 op 减少 60% Class A 调用数，成本更稳。前端并发池 3-4，重试成本仍可控 |
| D16 | 预签名 URL 有效期 | 上传 part URL **2 h**，下载 GET URL **30 min** |
| D17 | CORS 策略（v2 改） | R2 uploads bucket CORS 白名单只含 `https://app.yourdomain.com`（单域名）；artifacts bucket 若通过 custom domain 公开 GET，需要单独配 |
| D18 | YouTube worker 鉴权 | SG ↔ US 双向 `X-Internal-Key`（复用 `AVT_INTERNAL_API_KEY`，见 [internal_auth.py](../../gateway/internal_auth.py)），最少 32 字符 |
| D19 | 对象 key 命名 | `uploads/{user_id}/{yyyy}/{mm}/{upload_id}_{safe_filename}`；`artifacts/{user_id}/{job_id}/{type}_{filename}` |
| D20 | 断点续传 | 前端 `localStorage` 记已上传 parts 列表；刷新后继续 |
| D21 | 成品清理 | R2 artifacts 按用户计划保留 7 / 30 / 90 天（lifecycle + metadata） |
| D22 | 灾备 | R2 跨 region replication（可选，增量成本低） |
| D23 | 回滚策略 | 所有 Phase 可独立回滚；docker 镜像 tag 化；CF Tunnel DNS 一键切流量 |
| D24 | Feature flag | `AVT_STORAGE_BACKEND=r2\|local`；默认 `local` 兼容老部署，生产设 `r2` |
| D25（v2 新增）| R2 source 边界归一化 | **pipeline 零改动**。Gateway 层在启动 pipeline 前把 R2 对象下载到 `project_dir/source.xxx`（Job API 新增预处理 hook）。`process_runner.py` 继续传本地 `--source-ref`。JobRecord 新增 `source_r2_key` 仅作溯源，不传入 pipeline |
| D26（v2 / v3 改）| 不引入新 Job 状态 | `downloading_source` / `uploading_artifact` 这类瞬态**不新增公共状态**。YouTube 下载期间 Job 仍然是 `queued`；子阶段进度通过 **D31** 的 `status` 事件 + `payload.sub_stage` 暴露（**不新增 `event_type`**；v2 原文的 `source.download_*` 作废）|
| D27（v2 新增）| Caddy 去留 | **保留** Caddy（docker-compose 不动）。Tunnel 直连 Gateway `localhost:8880` 和 Next `localhost:3000`，**绕过** Caddy 的公网入口。Caddy 仅用于 admin 本地访问（SG 内网 127.0.0.1:443）或后期重新启用 |
| D28 （v3 改）| 下载端点响应格式 | **统一 302-only**。Gateway 鉴权后 `self.send_response(302) + self.send_header("Location", presigned_url)`；前端 `downloads.ts` / `ResultMediaCard.tsx` **零改动**（`<a download>` / `<video src>` 浏览器自动跟随）。有 R2 key 走 302，无 R2 key（老任务）走现有 FileResponse。**v2 中 JSON `{download_url}` 方案作废** |
| D29（v2 新增）| 验收指标口径 | 不写绝对数字（`<2s` 等）。上线前后各用**中国电信 / 联通 / 移动** 3 条链路跑基线（mtr / wpt / speedtest），写入本方案附录 § 15 基线快照，上线后对比提升比例 ≥ 30% 视为达标 |
| D30（v3 新增）| Phase 1+2 打包 MVP | **Phase 1（CF Tunnel）+ Phase 2（R2 下载）= MVP，5-6 天**。MVP 上线后跑三网基线验收（§ 11.3）；数据不达标则启用 § 11.6 撤退方案，不继续 Phase 3-5；数据达标再决定是否推进 |
| D31（v4 改）| 事件契约复用 | `source.download_*` 等子阶段**不新增 `event_type`**；用现有 `status` 事件 + `payload.sub_stage` 承载（`"r2_uploading" / "r2_downloading" / "r2_localized"` 等）。[events.py](../../src/services/jobs/events.py) 后端**不扩 `SUPPORTED_EVENT_TYPES` 白名单**。但前端 payload 链路要**三处联动改**（v4 纠正 v3）：① [types/jobs.ts:91-99](../../frontend-next/src/types/jobs.ts) `JobLogEntry` 加 `payload?: Record<string, unknown>`；② [mappers.ts:115-125](../../frontend-next/src/lib/api/mappers.ts) `toJobLogEntries` 补 `payload` 映射（v3 漏掉）；③ UI 组件消费。工时 F9 = 0.5d（v3 误估 0.3d）|
| D32（v3 新增）| `source_type` 枚举口径 | 正式三元组 **`youtube_url \| local_video \| r2`**（对齐 [models.py:9-11](../../src/services/jobs/models.py)）；`local_file` **仅** 作为历史兼容别名在 [job_intercept.py:496-500](../../gateway/job_intercept.py) 做归一化，不进入 JobRecord / 不进入前端类型 |
| D33 （v4 推翻重写）| R2 domain 策略 | **预签名 URL 全部走原生 `<account>.r2.cloudflarestorage.com`**（Cloudflare 官方约束：presigned URL **不支持** custom domain，[文档](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)）。v3 的"强制 custom domain + 双 endpoint 签名"设计**全部作废**。Custom domain `files.yourdomain.com` 在 v4 阶段**不配置**；保留为 D39 Phase 2b 备胎方案（public bucket + Worker HMAC）的基础设施 |
| D34（v3 新增）| 撤退方案 | MVP 上线后 48h 内三网实测任一不达标：DNS CNAME 切回 US 直连 IP（5 min）；Caddy 容器恢复 80/443 监听；ACME 重新拉证；保留原代理访问路径 4 周再决定下一步。**Caddyfile 和 Caddy 容器不删**，只降级 |
| D35（v3 新增）| `/stream/{kind}` 条件迁 R2 | **任务时长 ≤ 25 min**（final_video 绝大多数场景）走 R2 预签名 URL 302；**> 25 min** 保留现有本地 Range 流。判据：pipeline 完成时记录 `final_video.duration_seconds` 到 JobRecord，下载端点按此分支。v4 确认：这是 `/stream` 唯一口径，§ 5.2.1 表 / D7 / Smoke Test 全部对齐此说法 |
| **D36（v4 新增）** | MVP 验收目标降级 | 不承诺 "大陆下载 ≥3x 加速" / "LCP ≥30% 下降"；改为三条相对温和的硬指标：① **下载不劣化**（三网实测相对 Phase 0 基线 ±10% 浮动可接受）；② **US 源站出站带宽下降 ≥50%**；③ **回滚可在 15min 内完成**（§ 11.6）。大陆加速效果属于"额外收益"，实测到算赚到，不写进验收门槛 |
| **D37（v4 新增）** | Phase 0 探针三项必做 | MVP 启动前，基线测试必须包含：<br>**① 前端可达性**：三网实测 `app.yourdomain.com` 经 Tunnel 的 HTTP LCP、API P50；<br>**② R2 下载稳定性**：三网实测 `<account>.r2.cloudflarestorage.com/<public-test-file>` 下载 100MB 的速度、丢包、RST 率；<br>**③ R2 上传可行性**：至少一次**真实 multipart 上传**（手工跑 AWS CLI 或 boto3 脚本上传 2GB 样本），记录成功率、分片失败率、总耗时；<br>**三项都写入 § 15 基线快照**，决定 Phase 2/3 放行标准（见 D38）|
| **D38（v4 新增）** | Phase 3 降级为"可行性关卡" | Phase 3（R2 浏览器直传）**不再是 4 天确定实施**。判据：<br>- Phase 0 探针 ③ 上传成功率 ≥ 80% → Phase 3 按原计划推进（4d）<br>- 成功率 60-80% → Phase 3 先做**前端 UX 灰度 + 失败清晰提示**，不强切默认路径（+1d UI 工作）<br>- 成功率 < 60% → Phase 3 重评审：要么启用 Phase 2b 方向 B（但上传桶必须私有仍不解），要么推迟上传改造等 SG 迁移 |
| **D39（v4 新增）| Phase 2b 下载链路备胎 | 触发条件：MVP 实测"应用访问正常，但 `*.r2.cloudflarestorage.com` 下载在三网任一不稳定"。方案：artifacts bucket 设 public + 绑 `files.yourdomain.com` custom domain + 部署 Cloudflare Worker 校验 Gateway 发的 HMAC 签名。上传链路**不变**（上传桶必须私有，仍走原生 R2 endpoint）。工时 +1-1.5d（Worker 开发 + HMAC 签名/校验 + 测试） |
| **D40（v4 新增）| `force_local=1` 运维开关 | 下载端点（download / stream / tts-segments-zip）接受可选查询参数 `?force_local=1`；Gateway 识别后忽略 R2 key 直接走本地 FileResponse。仅用于紧急兜底（R2 不可用或 presign URL 大陆不通时手工切流）。默认不暴露给前端，运维手工 curl 调用或通过 feature flag 临时开启 |

---

## 2. 架构总览

### 2.1 拓扑图（v4：US 单节点 + Cloudflare + R2 原生域名）

```
                    ┌──────────────────────┐
                    │   国内用户浏览器       │
                    └────────┬─────────────┘
                             │ 所有请求 → app.yourdomain.com
                             │ + <account>.r2.cloudflarestorage.com (R2 原生域名, v4 D33)
                   ┌─────────┴──────────────────┐
                   │  Cloudflare (全球边缘)      │
                   │  ─ DNS  ─ SSL  ─ CDN        │
                   │  ─ DDoS防护  ─ Brotli压缩   │
                   └─┬──────────────────┬───────┘
                     │                  │
         app.*.com   │  *.r2.cf...com   │
         (US Tunnel) │  (R2 原生域名)    │
                     │                  │
         ┌───────────▼───────────┐ ┌────▼──────────┐
         │  CF Tunnel (cloudflared)│ │  Cloudflare R2 │
         │  ingress rules:         │ │  buckets:      │
         │    / → localhost:3000   │ │   - uploads    │
         │    /job-api/* → :8880   │ │   - artifacts  │
         │    /api/*     → :8880   │ │   - backups    │
         │    /gateway/* → :8880   │ └────────────────┘
         │    /auth/*    → :8880   │         ▲
         │    (其它 static → :3000)│         │ 浏览器 PUT 分片 25MB
         └────┬────────┬───────────┘         │ / 浏览器跟随 302 GET
              │        │                     │ (预签名 URL，原生 R2 endpoint)
     ┌────────▼──┐ ┌──▼─────────┐            │
     │ Next 容器 │ │ Gateway    │ ←──────────┘
     │ :3000     │ │ :8880      │
     │ standalone│ │ FastAPI    │
     └───────────┘ └──┬─────────┘
                      │ loopback 127.0.0.1
                      ▼
       ┌──────────────────────────────────┐
       │ 美国主节点 US (5.78.122.220)       │
       │ 单机 docker-compose               │
       │ ─ Caddy（保留，tls internal，     │
       │          仅 127.0.0.1:443 admin） │
       │ ─ gateway (8880)                  │
       │ ─ app / Job API (8877)            │
       │ ─ postgres                        │
       │ ─ cloudflared-us                  │
       │ ─ uptime-kuma                     │
       │                                   │
       │ Pipeline 子能力（内部调用）：       │
       │  ─ yt-dlp 下载 YouTube 到本地      │
       │  ─ AssemblyAI 转录                 │
       │  ─ Gemini S2 审校                  │
       │  ─ MiniMax/VolcEngine/CosyVoice    │
       │    TTS 合成                        │
       │  ─ Pipeline 完成后推产物到 R2      │
       └──────────────────────────────────┘

       ┌──────────────────────────────────┐
       │ SG 新加坡节点 (5.223.84.82)       │
       │ **v3 阶段不启用**                  │
       │ 保留机器作为 Phase 6 可选迁移目标：│
       │ 若 MVP 上线后实测大陆用户体验不达  │
       │ 标，可启动 US→SG 迁移（见附录 C）  │
       └──────────────────────────────────┘
```

**关键点**（v4 对齐）：
- **US 单节点**：零数据迁移，Phase 1-5 全部在现网 US 落地
- **单域名 `app.yourdomain.com`**，所有 HTTP 流量都走同一域 → cookie same-origin / 前端默认相对路径零改动
- **Tunnel 直连 upstream**（Next:3000、Gateway:8880），**不经 Caddy 443**，因此不需要 Let's Encrypt / ACME 80 端口
- **Caddy 保留**但降级为内网管理入口（`tls internal` 自签证书 + 仅 `127.0.0.1:443`），**撤退时 5 min 可切回公网**（见 § 11.6）
- **R2 走原生域名 `<account>.r2.cloudflarestorage.com`**（D33 / Cloudflare 官方约束：presigned URL 不支持 custom domain）。大陆访问稳定性属于 Phase 0 必测探针，**不预先承诺 ≥3x 加速**
- **`files.yourdomain.com` custom domain**：v4 阶段**不配置**；仅在 MVP 实测 R2 原生域名下载稳定性不达标时，作为 Phase 2b 备胎方案（public bucket + Worker HMAC，见 D39）
- **YouTube 下载能力**：US 本地现有 yt-dlp 链路不动，**不拆独立 worker**

### 2.2 网络流量示意

| 流量类型 | 路径 | 大小 | 走源站带宽吗 |
|---------|------|------|--------------|
| 前端 HTML/JS/CSS | 浏览器 → CF 边缘缓存（命中时）/ → Tunnel → Next 容器（SSR） | KB 级 | ✅ 仅 SSR miss |
| API 请求 | 浏览器 → CF Edge → CF Tunnel → SG Caddy → Gateway | KB 级 | ✅（低）|
| 源视频上传 | 浏览器 → R2 Edge（直传）| 2 GB | ❌ |
| 成品视频下载 | R2 Edge → 浏览器 | 500 MB | ❌ |
| YouTube 下载 | US Worker → YouTube → R2（直推）| 2 GB | ❌ SG / ❌ 用户 |
| TTS 调用 | SG → MiniMax 新加坡 | 几 MB | ✅（SG 内） |
| Gemini 调用 | SG → Google | KB 级 | ✅（SG 内） |

**关键结论**：用户感知的**大流量通路全部在 Cloudflare 边缘**，SG 源站只跑**控制面 + AI 计算**。

---

## 3. 服务器分工详细（v3）

### 3.1 US 主节点（现网，5.78.122.220）

**硬件**：沿用现有配置，**不做任何硬件 / 位置变动**。

**服务清单**（docker-compose，**大部分沿用现有**，只加 `cloudflared-us` 和 `uptime-kuma`）：

| 容器 | 镜像 | v3 前 | v3 后 |
|------|------|-------|-------|
| `caddy` | caddy:2.9.1 | 公网 80/443 (ACME) | **降级**：127.0.0.1:443 `tls internal` admin；撤退备份 |
| `gateway` | 自构建 | 8880 host | 8880 host（不变）|
| `app` | 自构建 | 8877 host | 8877 host（不变）|
| `next` | 自构建 | 3000 host | 3000 host（不变）|
| `postgres` | postgres:16-alpine | 5432 host | 5432 host（不变）|
| `cloudflared-us` | cloudflare/cloudflared:latest | — | **新增**，Tunnel 客户端（出站）|
| `uptime-kuma` | louislam/uptime-kuma:1 | — | **新增**（Phase 5）|

**防火墙**（UFW，Phase 1 切流量后执行）：
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp        # SSH (保留；可换端口)
# 不开 80 / 443 / 3000 / 8877 / 8880！Tunnel 走出站
```

**为什么不开 80/443**：cloudflared 走出站到 Cloudflare 边缘，所有入站流量反向回流，**源站零入站端口暴露**。

**Pipeline 子能力**（全部在 US 节点内部完成，无跨节点 RPC）：
- yt-dlp 下载 YouTube 视频到 `jobs/{job_id}/source.{ext}`（现有代码不动）
- AssemblyAI 转录（境外出站）
- Gemini S2 审校 Pass 1/2/3（境外出站）
- MiniMax / VolcEngine / CosyVoice TTS（出站，国内 API 境外可调用，现状已验证）
- 对齐 + publish + materials_pack（本地计算）
- Pipeline 完成后 `publish_artifacts_to_r2`（异步 background task，详见 § 5.2.2）

### 3.2 SG 节点（5.223.84.82，v3 阶段不启用）

**状态**：保留不动，作为 **Phase 6 可选迁移目标**。

**何时启用**：
1. MVP（Phase 1+2）上线后**三网基线**实测发现 US + CF 边缘对大陆用户仍然不达标（比如某运营商下载长期 < 1 MB/s 或 LCP > 5 s）
2. 或者需要给 MiniMax 新加坡节点的 TTS 延迟敏感任务做就近调用

**启用路径**：见 § 附录 C：**US→SG 迁移手册**（v3 内不执行，仅预案）。

### 3.3 为什么 v3 保留 US 作主节点（方向决策理由）

1. **零数据迁移**：现网 PG + projects/ 目录 + `.env` 全部在 US，Phase 1 上线不需要搬数据
2. **YouTube 下载已经在 US 节点**：IP 风控低，不需拆独立 worker
3. **Cloudflare 边缘离大陆用户更近**：大陆 → CF 边缘 RTT ~50 ms；CF 边缘 → US 源站是边缘到 Argo Tunnel 的专线，不是普通公网直连。用户感知的延迟瓶颈是**浏览器 → CF 边缘**这一段，不是 CF → 源站
4. **TTS 调用从 US 已验证**：MiniMax / VolcEngine 从 US 调稳定，改节点反而要重测
5. **成本**：少跑一台 SG ~¥150/月
6. **运维风险**：单节点 + Cloudflare SLA 初期够用；不启用 SG 就没有跨节点同步 / 双写的复杂度

---

## 4. Cloudflare 组件详细

### 4.1 DNS 迁移（v4 单域名 + US 为源，不配 custom domain）

```
原域名 NS → Cloudflare NS（在 CF 控制台找你的 NS 名）
DNS 记录:
  app.yourdomain.com      CNAME <us-tunnel-uuid>.cfargotunnel.com  🟠Proxied
  status.yourdomain.com   CNAME <us-tunnel-uuid>.cfargotunnel.com  🟠Proxied

# files.yourdomain.com 不配置:v4 阶段预签名 URL 走原生 <account>.r2.cloudflarestorage.com
#   (D33, Cloudflare 官方 presigned URL 不支持 custom domain)
# Phase 2b 备胎方案启用时才会新增 files.yourdomain.com CNAME 到 R2 bucket custom domain
```

**SSL 模式**：`Flexible` 或 `Full`（**不是** `Full strict`）。Tunnel 自带加密通道，源站不需 TLS 证书。

**为什么这样**：
- 前端、API 全部走 `app.yourdomain.com`，cookie same-origin 不变
- R2 下载 / 上传 URL 全部指向原生 `<account>.r2.cloudflarestorage.com`（签名 URL 的 endpoint 约束）
- 大陆访问 R2 原生域名的稳定性由 Phase 0 探针 ②（D37）实测确定
- 所有子域都 🟠Proxied（走 CF 代理，享 CDN + DDoS 防护 + 隐藏源站 IP）
- **v4 不再有 `yt.internal.*` 和 `files.*` 子域**：YouTube 下载留在 US 主节点；R2 走原生域名

### 4.2 前端部署（v2 保留 Docker 容器）

**决策**：**前端运行时不迁移**。继续用 [frontend-next/Dockerfile](../../frontend-next/Dockerfile) + `output: "standalone"`，docker-compose 里的 `next` 容器监听 `:3000`，由 Tunnel 直连。

理由：
- `@cloudflare/next-on-pages` 已被 Cloudflare 标记 deprecated（官方仓库 README 自述）
- 官方当前引导是 **Cloudflare Workers + OpenNext**（见参考链接 § 14）
- 迁移框架运行时（SSR adapter）风险高：Next.js 16 + App Router + standalone 在 Workers 上兼容面还在快速演化；可能需要重写部分 server components / middleware
- "大陆访问提速"的核心收益来自 CF Tunnel 隐藏源站 + 边缘 CDN 缓存静态资源，**不需要**把整个 SSR 迁到边缘

**frontend-next 配置保持原样**，只新增一点：
- `frontend-next/next.config.ts` 增加静态资源的 CDN 友好缓存头（`Cache-Control: public, max-age=31536000, immutable` for `/_next/static/*`）— 让 CF 边缘把静态资源缓存住，SSR 动态响应不缓存
- `.env.production` 里 `NEXT_PUBLIC_JOB_API_BASE_URL` **保持空（相对路径）**，不新增 `NEXT_PUBLIC_API_BASE`

**未来升级路径**（Phase 6+，非本期范围）：
- 若大陆边缘命中率不理想，再评估迁 **OpenNext for Cloudflare Workers**（[opennext.js.org](https://opennext.js.org/cloudflare)）
- 或者用 **CF Pages 传统路径**（静态 + Functions，不含 Next SSR adapter），只适用于以纯 SPA 方式部署

### 4.3 CF Tunnel（v3 US 直连 upstream）

**US 上安装 cloudflared**（推荐 docker-compose，和其他服务一致）：

1. 在 CF Dashboard → Zero Trust → Networks → Tunnels 创建 tunnel `avt-us`，拿到 `TUNNEL_TOKEN_US`
2. 配置 DNS：`app.yourdomain.com` / `status.yourdomain.com` → CNAME 到 `<us-tunnel-uuid>.cfargotunnel.com`
3. 用 config file 方式（精确路径路由，推荐）：

**`/opt/aivideotrans/cloudflared/config.yml`**：

```yaml
tunnel: <us-tunnel-uuid>
credentials-file: /etc/cloudflared/<us-tunnel-uuid>.json

ingress:
  # 优先级从上往下匹配
  - hostname: app.yourdomain.com
    path: ^/(job-api|api|gateway|auth|internal)(/.*)?$
    service: http://localhost:8880
    originRequest:
      connectTimeout: 30s
      tlsTimeout: 10s
      httpHostHeader: app.yourdomain.com
      noHappyEyeballs: true

  - hostname: app.yourdomain.com
    service: http://localhost:3000     # Next.js standalone（默认兜底所有其他路径）
    originRequest:
      connectTimeout: 30s

  - hostname: status.yourdomain.com
    service: http://localhost:3001

  - service: http_status:404
```

**关键说明**：
- Tunnel 直接指向**业务进程端口**（Next:3000, Gateway:8880），不再经过 Caddy
- 无需 Let's Encrypt / ACME 80 端口；Cloudflare ↔ 源站走 cloudflared 加密通道（Argo Tunnel 协议）
- `path` 正则匹配，把 API 路径与静态/SSR 分开路由
- 源站无需暴露公网端口；`ufw deny 80, 443, 8880, 8877, 3000, 3001`

**docker-compose 新增**（US，追加到现有 docker-compose.yml）：

```yaml
cloudflared-us:
  image: cloudflare/cloudflared:latest
  restart: unless-stopped
  command: tunnel --no-autoupdate --config /etc/cloudflared/config.yml run
  volumes:
    - /opt/aivideotrans/cloudflared:/etc/cloudflared:ro
  network_mode: host
  depends_on:
    - gateway
    - next
```

**SG 节点**：v3 阶段不部署 cloudflared。Phase 6 启用时再在 SG 上建 tunnel `avt-sg`，DNS CNAME 切换即可完成流量迁移。

**Caddy 的去留**：
- **保留**容器，但 [Caddyfile](../../Caddyfile) 改为**只监听 127.0.0.1:443**，`tls internal` 自签证书，仅用于本机 admin 工具（未来自建 Portainer、pgAdmin 等）访问
- **不删** Caddyfile 和 docker-compose 条目（撤退方案 § 11.6 依赖；5 min 内可恢复公网入口）
- 现有的 `AUTODUB_PUBLIC_HOST=aitrans.video` 的 ACME 模式在 Phase 1 切流量后**停用但不删**

### 4.4 CF R2 配置

**创建 buckets**（`wrangler` 或控制台）：

```bash
wrangler r2 bucket create avt-uploads
wrangler r2 bucket create avt-artifacts
wrangler r2 bucket create avt-backups
```

**生成 API Token**（R2 → Manage API Tokens）：
- 权限：`Object Read & Write`
- 资源：`avt-uploads, avt-artifacts, avt-backups`
- 获取 `Access Key ID` 和 `Secret Access Key`（写入 **US `.env`**，路径 `/opt/aivideotrans/config/.env`）

**CORS 配置**（v2 单域名，每个桶都配）：

```json
[
  {
    "AllowedOrigins": ["https://app.yourdomain.com"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

**Lifecycle 规则**：

```
avt-uploads:
  - 成品任务的 source 7 天后转 archive（可选）
  - 30 天未被引用的 orphan 删除（需要清理脚本配合）

avt-artifacts:
  - 根据用户计划保留 7/30/90 天（走 object metadata）

avt-backups:
  - 30 天以上删除
```

**自定义域名（v4：不配置）**

**Cloudflare 官方约束**：[Presigned URLs 文档](https://developers.cloudflare.com/r2/api/s3/presigned-urls/) 原文 "Presigned URLs work with the S3 API domain (`<ACCOUNT_ID>.r2.cloudflarestorage.com`) and **cannot be used with custom domains**"。因此：
- v4 MVP 阶段**不配置** R2 Custom Domain
- 所有预签名 URL（上传分片 PUT / 下载 GET）**全部指向** `<account_id>.r2.cloudflarestorage.com`
- 大陆访问稳定性由 Phase 0 探针 ②（D37）实测确定

**Phase 2b 备胎**：如果 MVP 上线后实测 R2 原生域名在三网任一下载不稳定，触发 D39 方案：
- 将 `avt-artifacts` bucket **设为 public** + 绑定 `files.yourdomain.com` custom domain
- 部署 Cloudflare Worker 校验 Gateway 发的 HMAC 签名（免费额度 100k req/天，不需 Pro plan）
- 此时**下载链路**（GET 成品）切 `files.yourdomain.com`；**上传链路仍走原生域名**（上传桶必须私有，不能 public）
- 工时 +1-1.5d，不在 MVP 范围

**环境变量**（US 节点 `.env`，v4 简化）：

```bash
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
R2_ACCOUNT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
R2_ACCESS_KEY_ID=xxxxxxxxxxxxxxxxxxxxxxxx
R2_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

R2_UPLOADS_BUCKET=avt-uploads
R2_ARTIFACTS_BUCKET=avt-artifacts
R2_BACKUPS_BUCKET=avt-backups
AVT_STORAGE_BACKEND=r2

# v4 删除: R2_PUBLIC_BASE (不再做 custom domain 签名)
# Phase 2b 启用时再加回: R2_PUBLIC_BASE=https://files.yourdomain.com + WORKER_HMAC_SECRET
```

**boto3 client 单 endpoint 模式**（§ 5.1.2 `r2_client.py` 里实现）：**v3 的双 endpoint 设计作废**。`get_r2_client()` / `presign_get` / `presign_upload_part` 全部用同一个原生 endpoint 签名。

---

## 5. 三条核心数据链路

### 5.1 用户上传链路（R2 Multipart 直传）

#### 5.1.1 时序

```
浏览器            SG Gateway            R2
  │                  │                   │
  │ 1. presign 请求  │                   │
  ├─────────────────>│                   │
  │  {name, size}    │                   │
  │                  │ 2. CreateMultipart│
  │                  ├──────────────────>│
  │                  │<── upload_id ─────│
  │                  │ 3. 为每片签 URL   │
  │                  │  (SigV4)          │
  │<── 分片URL列表 ──┤                   │
  │                  │                   │
  │ 4. 并发 PUT 分片 │                   │
  ├──────────────────┼──────────────────>│
  │     ... 分片 N  │                   │
  │                  │                   │
  │ 5. complete 请求 │                   │
  ├─────────────────>│                   │
  │ {etags}          │ 6. CompleteMultip │
  │                  ├──────────────────>│
  │                  │<── final key ─────│
  │<── object_key ───┤                   │
  │                  │                   │
  │ 7. 创建 Job      │                   │
  ├─────────────────>│                   │
  │ {object_key}     │                   │
```

#### 5.1.2 Gateway 侧新增模块

**新文件** `gateway/storage/r2_client.py`：

```python
"""R2 S3-compatible client wrapper.

Unified entry for all R2 operations. Falls back to local FS when
AVT_STORAGE_BACKEND=local for dev environments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import boto3
from botocore.client import Config


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    endpoint: str  # v4: 唯一 endpoint, 原生 <account>.r2.cloudflarestorage.com
    uploads_bucket: str
    artifacts_bucket: str
    backups_bucket: str

    @classmethod
    def from_env(cls) -> "R2Config":
        return cls(
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            endpoint=os.environ["R2_ENDPOINT"],
            uploads_bucket=os.environ.get("R2_UPLOADS_BUCKET", "avt-uploads"),
            artifacts_bucket=os.environ.get("R2_ARTIFACTS_BUCKET", "avt-artifacts"),
            backups_bucket=os.environ.get("R2_BACKUPS_BUCKET", "avt-backups"),
        )
    
    # v4 删除: public_base 字段 (R2 presigned URL 不支持 custom domain)
    # Phase 2b 启用时再加回: public_base + HMAC secret 用于 Worker 验签


@lru_cache(maxsize=1)
def get_r2_client():
    """Single R2 client, native S3 API endpoint.

    v4 notes: Cloudflare does NOT support presigned URLs against custom
    domains (see https://developers.cloudflare.com/r2/api/s3/presigned-urls/).
    All signing (GET + PUT + multipart) MUST use this native client.
    v3 的 get_r2_public_client() 双 endpoint 设计在 v4 作废。
    """
    cfg = R2Config.from_env()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        config=Config(
            signature_version="s3v4",
            region_name="auto",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def create_multipart_upload(bucket: str, key: str, content_type: str) -> str:
    client = get_r2_client()
    resp = client.create_multipart_upload(
        Bucket=bucket,
        Key=key,
        ContentType=content_type,
    )
    return resp["UploadId"]


def presign_upload_part(
    bucket: str, key: str, upload_id: str, part_number: int, expires: int = 7200
) -> str:
    """Sign with native R2 endpoint (D33)."""
    client = get_r2_client()
    return client.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires,
    )


def complete_multipart_upload(
    bucket: str, key: str, upload_id: str, parts: list[dict]
) -> dict:
    """parts: [{'PartNumber': int, 'ETag': str}, ...]"""
    client = get_r2_client()
    return client.complete_multipart_upload(
        Bucket=bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])},
    )


def abort_multipart_upload(bucket: str, key: str, upload_id: str) -> None:
    client = get_r2_client()
    client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)


def presign_get(
    bucket: str, key: str, expires: int = 1800, filename: Optional[str] = None
) -> str:
    """Sign with native R2 endpoint. 302 Location points to
    <account>.r2.cloudflarestorage.com (D33, v4).
    """
    client = get_r2_client()
    params = {"Bucket": bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def put_object_from_file(bucket: str, key: str, local_path: str) -> None:
    client = get_r2_client()
    client.upload_file(local_path, bucket, key)


def head_object(bucket: str, key: str) -> Optional[dict]:
    client = get_r2_client()
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except Exception:
        return None


def delete_object(bucket: str, key: str) -> None:
    client = get_r2_client()
    client.delete_object(Bucket=bucket, Key=key)
```

**新文件** `gateway/upload_presign.py`：

```python
"""Presigned upload endpoints.

Frontend flow:
  1. POST /gateway/upload-video/presign  → get part URLs
  2. Browser uploads each part directly to R2
  3. POST /gateway/upload-video/complete → finalize multipart
  4. Frontend passes returned object_key to create-job API
"""
from __future__ import annotations

import math
import os
import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from gateway.auth import require_user  # existing dep
from gateway.storage import r2_client

router = APIRouter()

_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_CHUNK_SIZE = 25 * 1024 * 1024               # 25 MB (v3 D15: R2 Class A 成本优化)
_ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}
_SAFE_NAME_RE = re.compile(r"[^\w.\-]")


class PresignRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=256)
    size: int = Field(..., gt=0, le=_MAX_UPLOAD_BYTES)
    content_type: str = Field("application/octet-stream", max_length=128)


class PartURL(BaseModel):
    part_number: int
    url: str


class PresignResponse(BaseModel):
    upload_id: str
    object_key: str
    bucket: str
    chunk_size: int
    parts: list[PartURL]


class CompletePart(BaseModel):
    part_number: int
    etag: str


class CompleteRequest(BaseModel):
    upload_id: str
    object_key: str
    parts: list[CompletePart]


class CompleteResponse(BaseModel):
    object_key: str
    size: int


def _safe_filename(name: str) -> str:
    base = _SAFE_NAME_RE.sub("_", name).strip("_.")
    if not base:
        base = "file"
    return base[:120]


def _validate_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(400, f"不支持的视频格式: {ext}")
    return ext


def _build_object_key(user_id: str, filename: str) -> tuple[str, str]:
    upload_id = uuid.uuid4().hex[:12]
    now = datetime.utcnow()
    safe = _safe_filename(filename)
    key = f"uploads/{user_id}/{now:%Y/%m}/{upload_id}_{safe}"
    return upload_id, key


@router.post("/gateway/upload-video/presign", response_model=PresignResponse)
async def presign_upload(
    req: PresignRequest,
    user: Annotated[dict, Depends(require_user)],
):
    _validate_ext(req.filename)
    user_id = user["user_id"]
    _upload_token, object_key = _build_object_key(user_id, req.filename)

    bucket = r2_client.R2Config.from_env().uploads_bucket
    upload_id = r2_client.create_multipart_upload(
        bucket=bucket, key=object_key, content_type=req.content_type
    )

    num_parts = math.ceil(req.size / _CHUNK_SIZE)
    if num_parts > 10000:  # R2 max
        raise HTTPException(400, "文件过大，分片数超限")

    parts = [
        PartURL(
            part_number=i,
            url=r2_client.presign_upload_part(bucket, object_key, upload_id, i),
        )
        for i in range(1, num_parts + 1)
    ]

    return PresignResponse(
        upload_id=upload_id,
        object_key=object_key,
        bucket=bucket,
        chunk_size=_CHUNK_SIZE,
        parts=parts,
    )


@router.post("/gateway/upload-video/complete", response_model=CompleteResponse)
async def complete_upload(
    req: CompleteRequest,
    user: Annotated[dict, Depends(require_user)],
):
    user_id = user["user_id"]
    if not req.object_key.startswith(f"uploads/{user_id}/"):
        raise HTTPException(403, "object_key 与当前用户不匹配")

    bucket = r2_client.R2Config.from_env().uploads_bucket
    try:
        r2_client.complete_multipart_upload(
            bucket=bucket,
            key=req.object_key,
            upload_id=req.upload_id,
            parts=[{"PartNumber": p.part_number, "ETag": p.etag} for p in req.parts],
        )
    except Exception as exc:
        r2_client.abort_multipart_upload(bucket, req.object_key, req.upload_id)
        raise HTTPException(500, f"合并分片失败: {exc}")

    meta = r2_client.head_object(bucket, req.object_key)
    size = int(meta.get("ContentLength", 0)) if meta else 0
    return CompleteResponse(object_key=req.object_key, size=size)


@router.post("/gateway/upload-video/abort")
async def abort_upload(
    upload_id: str,
    object_key: str,
    user: Annotated[dict, Depends(require_user)],
):
    user_id = user["user_id"]
    if not object_key.startswith(f"uploads/{user_id}/"):
        raise HTTPException(403)
    bucket = r2_client.R2Config.from_env().uploads_bucket
    r2_client.abort_multipart_upload(bucket, object_key, upload_id)
    return {"status": "aborted"}
```

**挂载路由** 在 [gateway/main.py](../../gateway/main.py)：

```python
from gateway.upload_presign import router as upload_presign_router

app.include_router(upload_presign_router)
```

**改造老端点** [gateway/upload.py](../../gateway/upload.py)：
- 保留，但标为 **deprecated**
- 加特性开关：`if os.environ.get("AVT_STORAGE_BACKEND") == "r2": raise HTTPException(410, "改用 /upload-video/presign")`
- 两周过渡期后删除

#### 5.1.3 前端侧改造

**新文件** `frontend-next/src/lib/api/r2Upload.ts`：

```typescript
const CHUNK_SIZE = 25 * 1024 * 1024;  // must match backend (v3 D15)
const STORAGE_KEY_PREFIX = "avt:upload:";

export interface UploadProgress {
  uploaded: number;
  total: number;
  ratio: number;
}

interface PartURL {
  part_number: number;
  url: string;
}

interface PresignResponse {
  upload_id: string;
  object_key: string;
  bucket: string;
  chunk_size: number;
  parts: PartURL[];
}

interface CompletedPart {
  part_number: number;
  etag: string;
}

interface ResumeState {
  upload_id: string;
  object_key: string;
  parts_url_cache: PartURL[];
  completed_parts: CompletedPart[];
  total_size: number;
  filename: string;
}

function storageKey(filename: string, size: number): string {
  return `${STORAGE_KEY_PREFIX}${filename}:${size}`;
}

function saveResumeState(state: ResumeState) {
  localStorage.setItem(
    storageKey(state.filename, state.total_size),
    JSON.stringify(state)
  );
}

function loadResumeState(filename: string, size: number): ResumeState | null {
  const raw = localStorage.getItem(storageKey(filename, size));
  return raw ? JSON.parse(raw) : null;
}

function clearResumeState(filename: string, size: number) {
  localStorage.removeItem(storageKey(filename, size));
}

export async function uploadVideoToR2(
  file: File,
  onProgress: (p: UploadProgress) => void,
  signal?: AbortSignal
): Promise<{ object_key: string; size: number }> {
  // Try resume first
  let state = loadResumeState(file.name, file.size);

  if (!state) {
    const presignResp = await fetch("/gateway/upload-video/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        size: file.size,
        content_type: file.type || "application/octet-stream",
      }),
      signal,
    });
    if (!presignResp.ok) throw new Error(`presign failed: ${presignResp.status}`);
    const p: PresignResponse = await presignResp.json();
    state = {
      upload_id: p.upload_id,
      object_key: p.object_key,
      parts_url_cache: p.parts,
      completed_parts: [],
      total_size: file.size,
      filename: file.name,
    };
    saveResumeState(state);
  }

  const completedNumbers = new Set(state.completed_parts.map(p => p.part_number));
  const concurrency = 4;
  const queue = state.parts_url_cache.filter(
    p => !completedNumbers.has(p.part_number)
  );

  let uploadedBytes = state.completed_parts.length * CHUNK_SIZE;

  async function uploadPart(part: PartURL): Promise<CompletedPart> {
    const start = (part.part_number - 1) * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const blob = file.slice(start, end);

    // Retry 3 times per part
    let lastErr: unknown;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch(part.url, {
          method: "PUT",
          body: blob,
          signal,
        });
        if (!res.ok) throw new Error(`part ${part.part_number}: ${res.status}`);
        const etag = res.headers.get("etag")?.replace(/"/g, "") ?? "";
        uploadedBytes += (end - start);
        onProgress({
          uploaded: uploadedBytes,
          total: file.size,
          ratio: uploadedBytes / file.size,
        });
        return { part_number: part.part_number, etag };
      } catch (err) {
        lastErr = err;
        if (signal?.aborted) throw err;
        await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
      }
    }
    throw lastErr;
  }

  // Parallel worker pool
  const results: CompletedPart[] = [...state.completed_parts];
  const workers = Array.from({ length: concurrency }, async () => {
    while (queue.length > 0) {
      const part = queue.shift();
      if (!part) break;
      const done = await uploadPart(part);
      results.push(done);
      state!.completed_parts = results;
      saveResumeState(state!);
    }
  });
  await Promise.all(workers);

  // Complete
  const completeResp = await fetch("/gateway/upload-video/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      upload_id: state.upload_id,
      object_key: state.object_key,
      parts: results,
    }),
    signal,
  });
  if (!completeResp.ok) throw new Error(`complete failed: ${completeResp.status}`);
  const body = await completeResp.json();

  clearResumeState(file.name, file.size);
  return body;
}
```

**改造表单** [frontend-next/src/components/workspace/TranslationForm.tsx](../../frontend-next/src/components/workspace/TranslationForm.tsx) 第 228 行附近：

```typescript
import { uploadVideoToR2 } from "@/lib/api/r2Upload";

async function handleSubmit(file: File, fields: FormFields) {
  setUploadProgress(0);
  const { object_key } = await uploadVideoToR2(file, (p) => {
    setUploadProgress(p.ratio);
  });

  // 用 object_key 而不是 FormData 创建 Job
  await fetch("/job-api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...fields,
      video_source: { type: "r2", object_key },
    }),
  });
}
```

#### 5.1.4 Job 创建侧改造（v2 边界归一化）

**核心原则（D25）**：pipeline 内部**零改动**。[src/pipeline/process.py:4312](../../src/pipeline/process.py) 继续用 `Path(source_ref)` 复制本地文件进 workspace，[src/services/jobs/process_runner.py:276](../../src/services/jobs/process_runner.py) 继续把 `source_ref` 当 CLI 参数传。改造都在 Gateway / Job API 的**启动 pipeline 之前**那一层。

##### 数据模型新增

`src/services/jobs/models.py` JobRecord 新增字段（migration 补齐）：

```python
# JobRecord 新增字段 (source 相关)
source_r2_key: str | None = None        # uploads/{user_id}/.../source.mp4
source_r2_bucket: str | None = None     # 便于未来切 bucket
source_metadata: dict | None = None     # {"size": int, "duration": float, "title": str, ...}

# JobRecord 新增字段 (artifact 相关, § 5.2)
r2_artifacts: dict[str, str] | None = None  # {"final_video": "artifacts/.../final.mp4", ...}
final_video_metadata: dict | None = None    # {"duration_seconds": float, ...} (§ 5.2.5)

# 不动的字段 (v3 确认正式值):
# source_type: "youtube_url" | "local_video" | "r2"  ← v3 新增 "r2"
#   - D32: "local_file" 不是正式值, 仅在 gateway/job_intercept.py:496-500 做归一化
# source_ref:  依然是 pipeline 能直接读的**本地路径**
```

持久化：在 [src/services/jobs/models.py](../../src/services/jobs/models.py) 的 `JobRecord.to_dict/from_dict` 补序列化；[src/services/jobs/store.py](../../src/services/jobs/store.py) 的 `JobStore.save_job/load_job` 本身读 JSON 字段无需改（`from_dict` 能向前兼容处理缺字段）。**不需要** `record_line.py`（此文件不存在；v2 方案错引用已在 v3 修正）。

##### [gateway/job_intercept.py](../../gateway/job_intercept.py) `POST /job-api/jobs` 拦截

```python
async def handle_create_job(payload: dict, user: dict):
    source_type = payload.get("source_type", "youtube_url")
    
    if source_type == "r2":
        # 新路径：用户已直传到 R2
        object_key = payload["source_r2_key"]
        _assert_key_belongs_to_user(object_key, user["user_id"])
        meta = r2_client.head_object(R2Config.from_env().uploads_bucket, object_key)
        if not meta or meta["ContentLength"] < 1024:
            raise HTTPException(400, "R2 source 不存在或为空")
        
        # 关键:不把 r2:// 传给 pipeline,先生成 JobRecord,稍后 Gateway 预下载
        payload["source_ref"] = f"__PENDING_R2__"   # 占位,runner 启动前会被替换
        payload["source_r2_key"] = object_key
        payload["source_r2_bucket"] = R2Config.from_env().uploads_bucket
        payload["source_metadata"] = {"size": meta["ContentLength"]}
    
    # youtube_url / local_video 路径: 完全不变
    # (注意: local_file 是旧别名, 此函数入口处会被归一化到 local_video, § 1 D32)
    return await forward_to_job_api(payload)
```

##### Job API / process_runner 的前置 hook

在 [process_runner.py](../../src/services/jobs/process_runner.py) 的 `_build_command` 调用**之前**，新增一个 `_materialize_source` 步骤：

```python
# src/services/jobs/source_materializer.py (新建)
from pathlib import Path
from gateway.storage import r2_client

def materialize_source_if_needed(job: JobRecord) -> str:
    """
    Ensure job.source_ref points to a local file that pipeline can read.
    If source_type == "r2", download from R2 to project_dir/source.xxx and
    rewrite source_ref in-place (persisted to JSON).
    
    Idempotent: if local file already exists with correct size, skip download.
    """
    if job.source_type != "r2":
        return job.source_ref  # 现有逻辑不变
    
    if not job.project_dir:
        raise RuntimeError("project_dir 必须在 materialize 之前建好")
    
    local_dir = Path(job.project_dir) / "source"
    local_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(job.source_r2_key).suffix or ".mp4"
    local_path = local_dir / f"source{ext}"
    
    expected_size = (job.source_metadata or {}).get("size")
    if local_path.exists() and expected_size and local_path.stat().st_size == expected_size:
        return str(local_path)  # 续跑时的幂等
    
    r2_client.get_r2_client().download_file(
        Bucket=job.source_r2_bucket,
        Key=job.source_r2_key,
        Filename=str(local_path),
    )
    
    # 回写 source_ref,后续 resume/status 都指本地
    job.source_ref = str(local_path)
    save_job_record(job)
    
    # v3 D31: 事件契约复用 —— 用现有 status 事件 + payload 承载子阶段
    # 不新增 event_type "source.localized"; events.py 只认 log/status
    emit_job_event(
        job.job_id,
        event_type="status",
        status="running",           # 或沿用当前 status, 由 service 决定
        message="源视频已就绪",
        payload={
            "sub_stage": "r2_localized",
            "local_path": str(local_path),
        },
    )
    return str(local_path)
```

在 `process_runner.submit` 或 `_build_command` 之前调一次：

```python
# process_runner.py (改动点)
def submit(self, job: JobRecord, continue_existing: bool) -> None:
    if not continue_existing:
        materialize_source_if_needed(job)   # ← 新增
    # 后面的 _build_command 完全不变
    command = self._build_command(job, continue_existing=continue_existing)
    ...
```

**好处**：
- pipeline（[src/pipeline/process.py](../../src/pipeline/process.py) 4000+ 行代码）零改动
- YouTube worker callback 后也走同一路径（callback 里只写 `source_r2_key`，不立刻下载；真正启动 pipeline 时才 materialize）
- 幂等：任务 resume 时本地文件已在，直接跳过
- 失败隔离：下载失败 → Job 标 `failed(reason="r2_download_failed")`，不污染 pipeline 状态

**状态机 + 事件契约**：不引入 `downloading_source`（D26）；不新增 `event_type`（D31）。

- 状态保持 `queued → running → succeeded/failed`
- R2 下载 / materialize / 推产物 等子阶段通过**现有 `status` 事件的 `payload.sub_stage` 字段**暴露：
  - `"r2_downloading"`（上传中/worker 下载中）
  - `"r2_localized"`（已拉到本地 workspace）
  - `"publishing_artifacts_to_r2"`（pipeline 完成后推产物）
  - `"artifacts_published"`（推完）
- 前端 UI（`WorkspaceProgress.tsx` 等）扩展读 `payload.sub_stage` 映射到中文进度文案
- **v4 修正**（CodeX 二审 P1-b）：光改 UI 不够。[mappers.ts:115-125](../../frontend-next/src/lib/api/mappers.ts) 的 `toJobLogEntries` 当前**丢弃 payload** 字段，`JobLogEntry` 类型也无 `payload`，所以 payload 数据到不了 UI。F9 必须**三处联动改**：
  1. [types/jobs.ts:91-99](../../frontend-next/src/types/jobs.ts) 给 `JobLogEntry` 加 `payload?: Record<string, unknown>`
  2. [mappers.ts:115-125](../../frontend-next/src/lib/api/mappers.ts) `toJobLogEntries` 补 `payload: event.payload ?? null`
  3. UI 组件消费 `entry.payload?.sub_stage`
- 工时：F9 = 0.5d（v3 误估 0.3d）

### 5.2 成品下载链路（v2 覆盖全部主路径）

**v1 遗漏**：原方案只改了 `background_task_api.py` 的 materials_pack 下载端点，但**真正吃带宽的是** [src/services/jobs/api.py:204](../../src/services/jobs/api.py) 的 `/jobs/{id}/download/{key}` 和 `/jobs/{id}/stream/{kind}`，前端由 [downloads.ts:32-45](../../frontend-next/src/lib/api/downloads.ts) 的 `buildResultDownloadUrl` 和 `buildStreamUrl` 构造。v2 必须全覆盖。

#### 5.2.1 下载端点改造全景

| 端点 | 当前实现 | v2 改造 | 前端调用点 |
|------|----------|---------|-----------|
| `/job-api/jobs/{id}/download/{key}` | 本地 FileResponse | **302 → R2 预签名 URL**（有 R2 key）；否则保留 FileResponse | [downloads.ts:32-35](../../frontend-next/src/lib/api/downloads.ts) `buildResultDownloadUrl` |
| `/job-api/jobs/{id}/stream/{kind}` (video/audio/poster) | 本地文件 Range 流 | **Phase 2 条件 opt-in**（D35）：`final_video.duration_seconds ≤ 25min` → 302 → R2 预签名；`> 25min` 或未知 → 保留本地 Range 流。前端 `buildStreamUrl` **零改动** | [downloads.ts:41-46](../../frontend-next/src/lib/api/downloads.ts) `buildStreamUrl` |
| `/job-api/jobs/{id}/tts-segments-zip` | 本地 zip FileResponse | **302 → R2 预签名**（如果已推）；否则 FileResponse 兜底 | [downloads.ts:25-29](../../frontend-next/src/lib/api/downloads.ts) |
| `/api/jobs/{id}/tasks/{task_id}/download` (materials pack) | 本地 FileResponse | **302 → R2 预签名**（新 task 必走）；老 task FileResponse | [downloads.ts:116](../../frontend-next/src/lib/api/downloads.ts) |
| `/job-api/jobs/{id}/segments/{sid}/draft-audio` | 本地小 wav（KB 级）| **不改**（文件小，不值得 R2） | [downloads.ts:53-57](../../frontend-next/src/lib/api/downloads.ts) |

**关键设计选择：302 跳转 vs JSON 响应**

采用 **302 直接跳转**（而非 JSON `{download_url}`），理由：
- ✅ `<a href="/job-api/jobs/.../download/final_video" download>` 老前端代码零改动（浏览器自动跟随 302）
- ✅ `<video src="/job-api/.../stream/video">` 未来迁 R2 时也透明（若改 stream 端点同样返回 302，`<video>` 元素跟随）
- ✅ 减少前端网络往返（不需要先 fetch JSON 再跳转）
- ❌ 唯一劣势：预签名 URL 暴露给浏览器地址栏 / history；缓解：30 分钟过期 + `object_key` 含 `user_id` 前缀（别人即使拿到 URL，用过一次就失效）

#### 5.2.2 产物推送（Pipeline 完成后）

**改造位置**：不是 `publish.py`（该文件不存在）。真实钩子在 [src/services/jobs/service.py](../../src/services/jobs/service.py) 的 Job 完成事件（搜 `status == "succeeded"` 的落地点），或 [src/pipeline/process.py](../../src/pipeline/process.py) 的 pipeline 终态 hook。

**推荐方案**：新增异步 task（复用 [docs/plans/2026-04-16-background-task-system-plan.md](2026-04-16-background-task-system-plan.md) 的 background_task 框架），在 Job 变 `succeeded` 时入队 `push_artifacts_to_r2`：

```python
# src/services/jobs/artifact_publisher.py (新建)
from pathlib import Path
from gateway.storage import r2_client
from gateway.storage.r2_client import R2Config

# 需要推 R2 的产物清单(对齐 DOWNLOADABLE_ARTIFACT_KEYS)
_ARTIFACT_SPEC = [
    ("final_video", "video/final.mp4", "final.mp4"),
    ("dubbed_audio", "audio/dubbed.wav", "dubbed.wav"),
    ("subtitles_zh", "subtitles/zh.srt", "subtitles_zh.srt"),
    ("subtitles_en", "subtitles/en.srt", "subtitles_en.srt"),
    ("subtitles_bilingual", "subtitles/bilingual.srt", "subtitles_bilingual.srt"),
    # TTS segments zip 按需生成,见 § 5.2.4
]

def publish_artifacts_to_r2(job_id: str) -> dict:
    job = load_job(job_id)
    if job.status != "succeeded":
        return {}
    project_dir = Path(job.project_dir)
    bucket = R2Config.from_env().artifacts_bucket
    r2_keys: dict[str, str] = {}
    
    for key, rel_path, dl_name in _ARTIFACT_SPEC:
        local = project_dir / rel_path
        if not local.exists():
            continue
        object_key = f"artifacts/{job.user_id}/{job_id}/{key}/{dl_name}"
        r2_client.put_object_from_file(bucket, object_key, str(local))
        r2_keys[key] = object_key
    
    # 持久化到 JobRecord(新字段 r2_artifacts: dict[str, str])
    job.r2_artifacts = r2_keys
    save_job_record(job)
    emit_job_event(job_id, "artifacts.published_to_r2", {"count": len(r2_keys)})
    return r2_keys
```

**入队点**（在 [src/services/jobs/service.py](../../src/services/jobs/service.py) 的 Job `succeeded` 落地处）：

```python
# 原逻辑不动
job.status = "succeeded"
save_job(job)

# v3 新增: 异步推 R2 (用 background_task 框架)
# 注意: 要先在 gateway/background_task_executors.py 的 TASK_EXECUTORS 字典
# 注册 "publish_artifacts_to_r2" (见 § 6.1 B12.5)
if os.environ.get("AVT_STORAGE_BACKEND") == "r2":
    enqueue_background_task(
        job_id=job.job_id,
        task_type="publish_artifacts_to_r2",
        params={},
    )
```

#### 5.2.3 `/jobs/{id}/download/{key}` 改造（v3：BaseHTTPRequestHandler 风格 + 302-only）

**关键 repo-fit**：[src/services/jobs/api.py:8](../../src/services/jobs/api.py) 是 `BaseHTTPRequestHandler + ThreadingHTTPServer`，**不是 FastAPI**。路由靠 `do_GET` 里手工分派 `path_parts`。改造要贴合这个风格。

**当前实现位置**：[src/services/jobs/api.py:204](../../src/services/jobs/api.py) 附近，`do_GET` 里形如：

```python
if len(path_parts) >= 4 and path_parts[2] == "download":
    job_id = path_parts[1]
    key = path_parts[3]
    # ... 现有: 本地文件读字节 → 写回响应体
```

**v3 改造**（伪代码，在现有 `do_GET` 的 download 分支里加前置判断）：

```python
# src/services/jobs/api.py 内 do_GET 方法片段
elif len(path_parts) >= 4 and path_parts[2] == "download":
    job_id = path_parts[1]
    key = path_parts[3]
    
    try:
        job = self._service.load_job(job_id)  # 已有的 service 调用
    except JobNotFoundError:
        self.send_error(HTTPStatus.NOT_FOUND)
        return
    
    # v4 D40: force_local=1 运维兜底开关
    query = urlparse(self.path).query
    force_local = parse_qs(query).get("force_local", ["0"])[0] == "1"
    
    # v4 新路径: 有 r2 key 且未强制本地, 就 302 到原生 R2 endpoint
    r2_key = None
    if not force_local:
        try:
            r2_artifacts = job.r2_artifacts or {}  # JobRecord 新字段 (见 B9)
            r2_key = r2_artifacts.get(key)
        except AttributeError:
            pass  # 老 JobRecord 没这个字段
    
    if r2_key:
        # 动态 import, 避免 import 循环 + 兼容 AVT_STORAGE_BACKEND=local 的老部署
        from gateway.storage import r2_client
        from gateway.storage.r2_client import R2Config
        
        filename = _DOWNLOAD_FILENAMES.get(key, f"{key}.bin")
        try:
            presigned = r2_client.presign_get(
                bucket=R2Config.from_env().artifacts_bucket,
                key=r2_key,
                expires=1800,
                filename=filename,
            )
        except Exception:
            logger.exception("presign failed, falling back to local")
            presigned = None
        
        if presigned:
            self.send_response(HTTPStatus.FOUND)  # 302
            self.send_header("Location", presigned)
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            return
    
    # Fallback: 老任务 / local backend / presign 失败
    # 走现有本地 FileResponse 风格的代码 (保持不变)
    self._serve_local_artifact(job, key)
    return


# 模块顶部 (或独立常量文件)
_DOWNLOAD_FILENAMES = {
    "final_video": "final.mp4",
    "dubbed_audio": "dubbed.wav",
    "subtitles_zh": "subtitles_zh.srt",
    "subtitles_en": "subtitles_en.srt",
    "subtitles_bilingual": "subtitles_bilingual.srt",
    # ... 对齐 frontend DOWNLOADABLE_ARTIFACT_KEYS
}
```

**关键点**：
- 所有改动都在**现有** `do_GET` 里加前置分支，老 FileResponse 逻辑保留
- `self.send_response(HTTPStatus.FOUND)` → HTTP 302；`Location` 指向 `<account>.r2.cloudflarestorage.com` 原生域名预签名 URL（v4 D33；Phase 2b 启用时改为 `files.yourdomain.com` + HMAC token）
- `Cache-Control: private, max-age=300` —— 允许浏览器 5 分钟内复用 302 响应（避免同一下载点多次打 Gateway 鉴权）
- presign 失败时优雅降级到本地 FileResponse

前端 [downloads.ts:32-35](../../frontend-next/src/lib/api/downloads.ts) **零改动**：
- `buildResultDownloadUrl` 继续返回 `/job-api/jobs/{id}/download/{key}`
- 浏览器请求该 URL，Gateway 发 302，浏览器跟随到 R2 预签名 URL，直接下载
- `<a href={url} download>` / `window.location.href = url` / `fetch(url).then(r=>r.blob())` 全部透明兼容

#### 5.2.4 `tts-segments-zip` 与 `materials-pack` 改造

- `tts-segments-zip`：zip 是按需合成的大文件，改造与 § 5.2.3 同模式（生成后推 R2，下次请求直接 302；首次合成仍在服务端）
- `materials-pack`：新 task（[gateway/background_task_api.py](../../gateway/background_task_api.py)）入队时，worker 生成 zip 后直接 `put_object_from_file` 推 R2，把 `r2_object_key` 存到 task 记录里。下载端点 302 → R2。老 task 用 `zip_path_str` 本地 fallback

#### 5.2.5 `/stream/{kind}` 条件 opt-in 迁 R2（v3 D35）

**v2 原计划**：推迟到 Phase 6。
**v3 调整**：Phase 2 就做，但是**条件 opt-in**——根据视频时长分流：

| 条件 | 路径 | 理由 |
|------|------|------|
| `final_video.duration ≤ 25 min` | 302 → R2 预签名 URL（`expires=1800`）| 30 min 过期足够播完；浏览器 `<video>` 一次性请求不中途重协商 |
| `duration > 25 min` 或未知 | 保留现有本地 Range 流 | 避免长视频播到一半签名过期导致卡住 |

**实现**（同 § 5.2.3 的 BaseHTTPRequestHandler 风格，加在 `stream` 分支里）：

```python
# src/services/jobs/api.py 内 do_GET 的 stream 分支
elif len(path_parts) >= 4 and path_parts[2] == "stream":
    job_id = path_parts[1]
    kind = path_parts[3]  # video | audio | poster
    
    job = self._service.load_job(job_id)
    duration = (job.final_video_metadata or {}).get("duration_seconds", 0)
    r2_key = (job.r2_artifacts or {}).get(f"{kind}_stream")
    
    # 条件 opt-in: ≤25min 且有 R2 key 才走 302
    if r2_key and 0 < duration <= 25 * 60:
        from gateway.storage import r2_client
        presigned = r2_client.presign_get(
            bucket=R2Config.from_env().artifacts_bucket,
            key=r2_key,
            expires=1800,
            # 注意: stream 不加 Content-Disposition: attachment
        )
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", presigned)
        self.end_headers()
        return
    
    # Fallback: 本地 Range 流 (现有逻辑不动)
    self._serve_local_stream(job, kind)
    return
```

**`final_video_metadata.duration_seconds`** 字段：pipeline 完成时用 `ffprobe` 记录；已有 / 可在 `publish_artifacts_to_r2` 里顺手计算。

**风险与 fallback**：
- 如果实测发现 ≤25min 仍然有浏览器播到一半卡顿（R2 签名 URL 被 GFW 干扰），把阈值降到 15min 或直接全回本地
- 长视频 opt-in 留给 Phase 6 专题（`?t=signed_token` + 前端 refresh 机制）

#### 5.2.5a 老 § 5.2.5 原文（保留用于对比）

**原 v2 原因**：
- R2 原生支持 Range 请求（HTTP 206），技术上可行
- 但视频播放器（HLS / DASH）场景复杂，迁移要考虑 CORS preflight、签名 URL 过期刷新、播放中 token 续期
- 当前播放主要在用户完成任务后 preview，流量有限
- **Phase 2 保留现状**（本地 stream）；Phase 6 专题迁移（含签名 URL auto-refresh）

#### 5.2.6 前端改造清单（v2）

| 文件 | v1 打算改 | v2 实际改动 |
|------|-----------|------------|
| [downloads.ts](../../frontend-next/src/lib/api/downloads.ts) | 改 URL 构造 | **不改**。服务端 302 自动生效 |
| [ResultMediaCard.tsx](../../frontend-next/src/components/workspace/ResultMediaCard.tsx) | 改 `<a>` 链接 | **不改**。浏览器跟随 302 |
| [jobs.ts](../../frontend-next/src/lib/api/jobs.ts) | 新增 `downloadTaskArtifact` | **不必要**。现有 `<a href={buildTaskDownloadUrl(...)} download>` 自动跟随 |

**v2 前端改动极小**的代价：服务端 302 跳转 + 预签名 URL 暴露在 Network 面板。用户 30 分钟内刷新或分享 URL 有效；超时后重新请求下载端点即可拿到新 URL。

### 5.3 YouTube 下载链路（v3：US 本地直下，不拆独立 worker）

**v3 决策变更（D2/D8）**：YouTube 下载能力**留在 US 主节点的现有 pipeline**，不再拆独立 worker。现有 [src/modules/ingestion/youtube/downloader.py](../../src/modules/ingestion/youtube/downloader.py) 及 pipeline 集成完全不动。

**现有流程（v2 前，不变）**：

```
用户提交 YouTube URL 
  → Gateway 创建 Job (source_type=youtube_url, source_ref=youtube_url)
  → process_runner 启动 pipeline
  → pipeline 内部调 yt-dlp 下载到 jobs/{job_id}/source.xxx
  → 继续后续步骤 (转录、审校、TTS、publish)
```

**v3 改造点**（只在 pipeline 末尾加一步）：

- Pipeline 完成后，`publish_artifacts_to_r2` 把**成品**（final_video、subtitles 等）推 R2
- **源视频不推 R2**：源视频只是处理中间产物，由 Phase 5 的本地 TTL 清理处理
- 不需要 `internal_callbacks.py` / `youtube_dispatcher.py` / 独立的 US worker 容器

**方案原 v2 的"独立 US worker 架构"已作废**。下文 § 5.3.1 / § 5.3.2 保留作为**历史参考**，Phase 1-5 实施时**不采用**；只在 Phase 6 需要 US→SG 迁移后、想把 yt-dlp 从 SG 剥离时才可能复用。

---

#### 5.3（v3 正文结束。以下 5.3.1 / 5.3.2 为历史 v2 参考，不实施）

### 附录 B: v2 独立 YouTube Worker 设计（不采用，仅保留给 Phase 6 备选）

> 以下内容原为 v2 § 5.3.1 / § 5.3.2。v3 不实施。如 Phase 6 启用 US→SG 迁移，且 SG 不便直连 YouTube 时，可参考此设计把 yt-dlp 能力重新拆出。

#### B.1 US Worker 服务（历史参考）

#### B.1.0 US Worker 服务（历史参考）

**新建仓库/目录** `services/youtube_worker/`：

**`services/youtube_worker/main.py`**：

```python
"""US YouTube Downloader Worker.

Called by SG gateway via internal HTTP RPC. Downloads video via yt-dlp,
uploads to R2, posts callback. Never stores persistently.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import boto3
import httpx
from botocore.client import Config
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="AVT YouTube Worker")
log = logging.getLogger("yt-worker")

INTERNAL_KEY = os.environ["AVT_INTERNAL_API_KEY"]
if len(INTERNAL_KEY) < 32:
    raise RuntimeError("AVT_INTERNAL_API_KEY must be ≥32 chars")

R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_BUCKET = os.environ.get("R2_UPLOADS_BUCKET", "avt-uploads")


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", region_name="auto"),
    )


def check_internal_auth(
    x_internal_key: Annotated[str | None, Header()] = None
):
    if x_internal_key != INTERNAL_KEY:
        raise HTTPException(401, "invalid internal key")


class DownloadRequest(BaseModel):
    youtube_url: HttpUrl
    job_id: str
    user_id: str
    callback_url: HttpUrl
    format: str = "bv*+ba/b"           # yt-dlp best video+audio
    cookies_b64: str | None = None      # optional, for member-only


class DownloadResult(BaseModel):
    job_id: str
    object_key: str
    size: int
    title: str
    duration: float
    ext: str


async def _download_and_upload(req: DownloadRequest) -> DownloadResult:
    tmpdir = Path(tempfile.mkdtemp(prefix=f"yt_{req.job_id}_"))
    try:
        cookie_file = None
        if req.cookies_b64:
            import base64
            cookie_file = tmpdir / "cookies.txt"
            cookie_file.write_bytes(base64.b64decode(req.cookies_b64))

        output_tpl = str(tmpdir / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "-f", req.format,
            "--no-playlist",
            "--print-json",
            "--no-simulate",
            "--output", output_tpl,
            str(req.youtube_url),
        ]
        if cookie_file:
            cmd.extend(["--cookies", str(cookie_file)])

        log.info("yt-dlp start job=%s url=%s", req.job_id, req.youtube_url)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("yt-dlp fail: %s", stderr.decode()[-500:])
            raise RuntimeError(f"yt-dlp exit {proc.returncode}")

        import json
        meta = json.loads(stdout.decode().splitlines()[-1])
        ext = meta["ext"]
        video_id = meta["id"]
        local_file = tmpdir / f"{video_id}.{ext}"
        if not local_file.exists():
            # yt-dlp 可能改名，兜底搜目录
            candidates = list(tmpdir.glob(f"{video_id}.*"))
            if not candidates:
                raise RuntimeError("下载产物不存在")
            local_file = candidates[0]
            ext = local_file.suffix.lstrip(".")

        size = local_file.stat().st_size
        object_key = f"uploads/{req.user_id}/youtube/{req.job_id}/source.{ext}"

        log.info("upload to r2 key=%s size=%d", object_key, size)
        s3 = get_s3()
        s3.upload_file(
            str(local_file), R2_BUCKET, object_key,
            ExtraArgs={"ContentType": f"video/{ext}"},
        )

        return DownloadResult(
            job_id=req.job_id,
            object_key=object_key,
            size=size,
            title=meta.get("title", ""),
            duration=float(meta.get("duration", 0)),
            ext=ext,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _notify_callback(callback_url: str, result: DownloadResult | None, error: str | None):
    async with httpx.AsyncClient(timeout=30) as client:
        payload = (
            {"status": "succeeded", **result.model_dump()}
            if result else
            {"status": "failed", "error": error}
        )
        for attempt in range(3):
            try:
                resp = await client.post(
                    callback_url, json=payload,
                    headers={"X-Internal-Key": INTERNAL_KEY},
                )
                if resp.status_code < 400:
                    return
            except Exception as e:
                log.warning("callback retry %d: %s", attempt, e)
            await asyncio.sleep(2 ** attempt)
        log.error("callback give up: %s", callback_url)


async def _job_runner(req: DownloadRequest):
    try:
        result = await _download_and_upload(req)
        await _notify_callback(str(req.callback_url), result, None)
    except Exception as e:
        log.exception("download failed job=%s", req.job_id)
        await _notify_callback(str(req.callback_url), None, str(e))


@app.post("/internal/youtube/download", dependencies=[Depends(check_internal_auth)])
async def enqueue(req: DownloadRequest, bg: BackgroundTasks):
    bg.add_task(_job_runner, req)
    return {"status": "accepted", "job_id": req.job_id}


@app.get("/health")
async def health():
    return {"status": "ok"}
```

**`services/youtube_worker/Dockerfile`**：

```dockerfile
FROM python:3.12-slim
WORKDIR /app

# yt-dlp 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    yt-dlp \
    fastapi \
    'uvicorn[standard]' \
    boto3 \
    httpx

COPY main.py /app/main.py

EXPOSE 8890
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8890", "--workers", "2"]
```

**US `docker-compose.yml`**：

```yaml
services:
  yt-worker:
    build: ./services/youtube_worker
    restart: unless-stopped
    environment:
      - AVT_INTERNAL_API_KEY=${AVT_INTERNAL_API_KEY}
      - R2_ENDPOINT=${R2_ENDPOINT}
      - R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
      - R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
      - R2_UPLOADS_BUCKET=${R2_UPLOADS_BUCKET}
    ports:
      - "127.0.0.1:8890:8890"   # 只监听 localhost, cloudflared 转发
    
  cloudflared-us:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARED_TOKEN_US}
    network_mode: host
```

#### B.2 SG 侧调用改造（历史参考）

**新文件** `gateway/youtube_dispatcher.py`：

```python
"""SG → US YouTube worker RPC."""
import os
import httpx
from gateway.internal_auth import internal_headers

YT_WORKER_URL = os.environ.get(
    "YT_WORKER_URL", "https://yt.internal.yourdomain.com"
)


async def dispatch_youtube_download(
    youtube_url: str, job_id: str, user_id: str, callback_url: str,
    cookies_b64: str | None = None,
) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{YT_WORKER_URL}/internal/youtube/download",
            headers=internal_headers(),
            json={
                "youtube_url": youtube_url,
                "job_id": job_id,
                "user_id": user_id,
                "callback_url": callback_url,
                "cookies_b64": cookies_b64,
            },
        )
        resp.raise_for_status()
        return resp.json()
```

**改造** [gateway/job_intercept.py](../../gateway/job_intercept.py) 的 Job 创建（v2 不引入新状态，D26）：

```python
# 当 payload 是 YouTube URL 时
if is_youtube_url(payload.video_url):
    # 创建 Job,状态仍是 queued(现有状态集不变)
    job = create_job_record(
        ...,
        source_type="r2",              # 标记为 r2 类型(占位)
        source_r2_key=None,            # worker 完成后回填
        status="queued",
    )
    
    # 派发 US worker(异步,立即返回)
    callback = f"https://app.yourdomain.com/internal/jobs/{job.job_id}/source-ready"
    await dispatch_youtube_download(
        youtube_url=payload.video_url,
        job_id=job.job_id,
        user_id=user["user_id"],
        callback_url=callback,
    )
    
    # 事件(非状态):给前端进度条
    emit_job_event(job.job_id, "source.download_started", {"provider": "youtube"})
    return job
```

**新增回调端点**（SG 侧，`/internal/*` 路径由 [Caddyfile:18](../../Caddyfile) 和 gateway 限制为仅 localhost + internal key）：

```python
# gateway/internal_callbacks.py (新建)
@router.post(
    "/internal/jobs/{job_id}/source-ready",
    dependencies=[Depends(verify_internal_key)],
)
async def source_ready(job_id: str, payload: SourceReadyPayload):
    job = load_job(job_id)
    if job is None:
        raise HTTPException(404)
    
    if payload.status == "succeeded":
        # 只回填元数据,不立刻下载到本地
        # 真正的下载由 source_materializer 在 pipeline 启动前触发(D25)
        job.source_r2_key = payload.object_key
        job.source_r2_bucket = R2Config.from_env().uploads_bucket
        job.source_metadata = {
            "title": payload.title,
            "duration": payload.duration,
            "size": payload.size,
        }
        save_job_record(job)
        emit_job_event(job_id, "source.download_ready", {
            "size": payload.size, "duration": payload.duration,
        })
        # 入队 pipeline(Job API 拿到会先调 materialize_source_if_needed)
        enqueue_pipeline(job)
    else:
        job.status = "failed"
        job.error_message = f"YouTube 下载失败: {payload.error}"
        save_job_record(job)
        emit_job_event(job_id, "source.download_failed", {"error": payload.error})
    return {"ok": True}
```

**和 § 5.1.4 的材料化流程一致**：
- YouTube 下载完 → worker 推 R2 → SG 回调回填 `source_r2_key` → pipeline runner 启动前 `materialize_source_if_needed` 把 R2 对象拉到 `project_dir/source.xxx` → pipeline 读本地路径（零改动）

**状态机（D26 再次确认）**：YouTube 任务和 R2 上传任务全周期都用现有状态 `queued → running → succeeded/failed`。进度通过 `job_events` 的 `source.download_started` / `source.download_ready` / `source.localized` 三个事件暴露给前端 UI；事件可渲染成进度条的子阶段文案（"正在从 YouTube 下载"/"下载完成，准备启动"）。

---

## 6. 代码改造清单（v3）

**v3 版本**：移除 US 独立 worker 相关模块（B7/B8/B17/B18/B19）；下载端点改造贴合 `BaseHTTPRequestHandler` 风格（B14）；补齐 background_task 注册（B12.5）+ UI 子阶段渲染（F9）；真实文件路径（B9 不再引用 `record_line.py`）。v3 总工时 ~14 人日（v2 16d → 省掉独立 worker 2d，补 0.6d UI/注册）。

### 6.1 后端改造

| # | 文件 / 模块 | 改动类型 | 工时 | 对应 Phase |
|---|-------------|---------|------|-----------|
| B1 | `gateway/storage/__init__.py` | 新建（空 package） | - | 2 |
| B2 | `gateway/storage/r2_client.py` | **新建** boto3 封装（head / presign / upload / multipart / delete）| 0.5d | 2 |
| B3 | `gateway/upload_presign.py` | **新建**（presign / complete / abort 三端点）| 1.0d | 3 |
| B4 | `gateway/main.py` | 挂载新路由 + 回调路由 | 0.2d | 2-4 |
| B5 | `gateway/upload.py` | 加 deprecated 开关（`AVT_STORAGE_BACKEND=r2` 时 410）| 0.2d | 3 |
| B6 | `gateway/job_intercept.py` | 支持 `source_type=r2`（已有 `local_file → local_video` 归一化不动；v3 D32）| 0.4d | 3 |
| ~~B7~~ | ~~`gateway/youtube_dispatcher.py`~~ | ~~SG → US RPC~~ | ~~0.3d~~ | **v3 取消** |
| ~~B8~~ | ~~`gateway/internal_callbacks.py`~~ | ~~`source-ready` 回调~~ | ~~0.3d~~ | **v3 取消** |
| B9 (v3 修) | [src/services/jobs/models.py](../../src/services/jobs/models.py) + [src/services/jobs/store.py](../../src/services/jobs/store.py) | `JobRecord` 新增 `source_r2_key` / `source_r2_bucket` / `source_metadata` / `r2_artifacts` / `final_video_metadata` 字段；`to_dict` / `from_dict` 序列化；`JobStore.save_job` / `load_job` 向前兼容缺字段。**不引用不存在的 `record_line.py`**（v2 错误） | **1.0d** | 2 |
| B10 | `src/services/jobs/source_materializer.py` | **新建**（R2 → 本地 workspace，幂等） | 0.5d | 3 |
| B11 | `src/services/jobs/process_runner.py` | 在 `submit` / `_build_command` 前调 `materialize_source_if_needed` | 0.3d | 3 |
| B12 | `src/services/jobs/service.py` | Job `succeeded` 钩子入队 `publish_artifacts_to_r2` | 0.3d | 2 |
| **B12.5 (v3 新增)** | [gateway/background_task_executors.py:277](../../gateway/background_task_executors.py) | **改 `TASK_EXECUTORS` 字典**：新增 `"publish_artifacts_to_r2"` executor；注意 [background_task_models.py](../../gateway/background_task_models.py) 的 `task_type` 列是 `String(32)`，业务上仍需避免与 `materials_pack` / `generate_video` 冲突 | **0.3d** | 2 |
| B13 | `src/services/jobs/artifact_publisher.py` | **新建**（产物推 R2；含 `ffprobe` 记录 `final_video.duration_seconds` 用于 § 5.2.5 条件分流） | 0.5d | 2 |
| B14 (v3 重写) | [src/services/jobs/api.py](../../src/services/jobs/api.py) | **BaseHTTPRequestHandler 风格**改造（见 § 5.2.3 / § 5.2.5）：`do_GET` 的 `download` / `stream` / `tts-segments-zip` 分支加前置判断，有 R2 key 则 `send_response(302) + Location`。**不引入 FastAPI `@router` 装饰** | **1.0d** | 2 |
| B15 | `gateway/background_task_api.py` | `/api/jobs/{id}/tasks/{tid}/download` 改为 302 → R2 | 0.3d | 2 |
| B16 | `gateway/materials_api.py` | 同上 + materials 生成 worker 产出后推 R2 | 0.5d | 2 |
| ~~B17~~ | ~~`services/youtube_worker/main.py`~~ | ~~US worker~~ | ~~1.0d~~ | **v3 取消** |
| ~~B18~~ | ~~`services/youtube_worker/Dockerfile`~~ | | ~~0.2d~~ | **v3 取消** |
| ~~B19~~ | ~~`services/youtube_worker/docker-compose.yml`~~ | | ~~0.2d~~ | **v3 取消** |
| B20 (v3 改) | `.env.example` / `/opt/aivideotrans/config/.env` | 加 R2 配置（8 个变量）+ `CLOUDFLARED_TOKEN_US`；**不加** `YT_WORKER_URL` | 0.1d | 1-3 |
| B21 | `scripts/backup_pg.sh` | **新建**（每日 PG dump → R2） | 0.3d | 5 |
| B22 | `scripts/cleanup_local.sh` + `verify_pushed_to_r2.py` | **新建**（本地磁盘 TTL 清理） | 0.5d | 5 |
| B23 | `tests/test_r2_client.py` | 用 moto / 本地 MinIO 做集成测试 | 0.5d | 2 |
| B24 | `tests/test_upload_presign.py` | 单元 + 端到端（mock R2） | 0.5d | 3 |
| B25 | `tests/test_source_materializer.py` | 单元测试（幂等 / 失败路径） | 0.3d | 3 |
| B26 | `tests/test_artifact_publisher.py` | 单元测试 | 0.3d | 2 |
| B27 (v3 改) | `tests/test_download_redirect.py` | 验证 302 行为 + 老任务 FileResponse fallback；**关键**：覆盖 [api.py](../../src/services/jobs/api.py) BaseHTTPRequestHandler 的 download / stream / tts-segments-zip 三分支（起一个 ThreadingHTTPServer 测试实例）| 0.4d | 2 |

### 6.2 前端改造（v2 改动极小）

| # | 文件 / 模块 | 改动类型 | 工时 |
|---|-------------|---------|------|
| F1 | `frontend-next/src/lib/api/r2Upload.ts` | **新建**（分片 + 断点续传 + 并发池） | 1.0d |
| F2 | `frontend-next/src/components/workspace/TranslationForm.tsx` | 第 228 行附近：改走 `r2Upload` + 进度条；Job 创建参数加 `source_type=r2, source_r2_key` | 0.5d |
| F3 | `frontend-next/src/lib/api/config.ts` | **不改**（保留 `NEXT_PUBLIC_JOB_API_BASE_URL` 默认相对路径） | - |
| F4 | `frontend-next/src/lib/api/downloads.ts` | **不改**（服务端 302 透明） | - |
| F5 | `frontend-next/src/components/workspace/ResultMediaCard.tsx` | **不改** | - |
| F6 | `frontend-next/next.config.ts` | `headers()` 里给 `/_next/static/*` 加 immutable cache 头（CF 边缘缓存） | 0.2d |
| F7 (v3 改) | [types/jobs.ts](../../frontend-next/src/types/jobs.ts) | 类型加 `source_r2_key?` / `source_metadata?` / `r2_artifacts?` 可选字段；`source_type` 类型保持 `'youtube_url' \| 'local_video' \| 'r2'`，**不加 `local_file`**（D32） | 0.2d |
| **F9 (v4 扩展)** | **三处联动改**：① [types/jobs.ts:91-99](../../frontend-next/src/types/jobs.ts) `JobLogEntry` 加 `payload?: Record<string, unknown>`；② [mappers.ts:115-125](../../frontend-next/src/lib/api/mappers.ts) `toJobLogEntries` 补 `payload: event.payload ?? null` 映射；③ UI 组件（`WorkspaceProgress.tsx` 或日志面板）消费 `entry.payload?.sub_stage` 映射中文文案（`r2_downloading` / `r2_localized` / `publishing_artifacts_to_r2` / `artifacts_published`）。v3 误判 "不改 mappers 只改 UI" 实际拿不到数据 | **0.5d** |
| F8（可选）| 上传失败友好提示组件 | 展示 resume state / 重试按钮 | 0.5d |

### 6.3 基础设施 / 运维

| # | 文件 / 动作 | 改动类型 | 工时 | Phase |
|---|-------------|---------|------|-------|
| I1 (v3 改) | `docker-compose.yml` (US) | 加 `cloudflared-us` + `uptime-kuma`；`caddy` 容器**保留**（Caddyfile 内部只 127.0.0.1:443 + tls internal）| 0.3d | 1 |
| I2 (v3 改) | [Caddyfile](../../Caddyfile) | 改监听 `127.0.0.1:443` + `tls internal`；**不删**（撤退备份）| 0.2d | 1 |
| I3 (v3 改) | `/opt/aivideotrans/cloudflared/config.yml` (US) | **新建** Tunnel ingress 规则（path 正则路由 `/job-api/*` 等 → 8880） | 0.3d | 1 |
| I4 (v3 改) | CF Dashboard | 创建 `avt-us` tunnel + DNS（app / files / status）+ R2 buckets + **R2 Custom Domain `files.*`**（D33 必须）+ CORS | 0.4d | 0-1 |
| ~~I5~~ | ~~CF Dashboard (US worker tunnel)~~ | | ~~0.2d~~ | **v3 取消** |
| I6 (v3 改) | UFW 规则（US）| `ufw deny 80,443,3000,3001,8877,8880`；只留 22 | 0.1d | 1 |
| I7 (v3 改) | Cron（US）| 每日 PG 备份 + 本地 TTL 清理 | 0.2d | 5 |
| I8 | Uptime Kuma 监控项配置 | 6-8 个 check + 飞书 webhook | 0.3d | 5 |

### 6.4 合计（v4）

| 类别 | v3 工时 | v4 工时 | 差异说明 |
|------|---------|---------|----------|
| 后端 | ~7.7d | **~7.7d** | 不变（双 endpoint 去掉不减工时，其它不改）|
| 前端 | ~2.7d | **~2.9d** | F9 0.3d → 0.5d（扩到 mappers + types）|
| 基础设施 | ~1.8d | **~1.6d** | -custom domain 配置 (0.2d) |
| 文档 + buffer | ~2.0d | ~2.0d | |
| **Phase 0 探针** | — | **+0.5d** | 新增三网 × 三探针实测（D37）|
| **总计** | **~14d** | **~14.7d** | 方向性轻微增加，主要是 Phase 0 探针与 F9 扩展 |

**Phase 1 + 2（MVP）= ~5.5d**（v3 5d + Phase 0 探针 0.5d）：
- Phase 0（~0.5d）：三网基线 + R2 原生域名探针 + 真实 multipart 上传样本（D37）
- Phase 1（~1.5d）：I1-I4、I6、B20（部分）
- Phase 2（~3.5d）：B1/B2/B9/B12/B12.5/B13/B14/B15/B16/B23/B26/B27/F6/F9 + 联调

**Phase 3（灰度关卡，工时浮动）**：
- Phase 0 上传成功率 ≥80% → 按 4.5d 推进
- 60-80% → 5.5d（+1d UI 灰度 / 失败提示）
- <60% → 重评审（可能推迟到 Phase 6 US→SG 迁移）

**Phase 2b（备胎，仅触发时）**：~1.5d（Worker HMAC + custom domain + 测试）

**Phase 4（v2 独立 worker）已取消**。

---

## 7. 分阶段落地（v3 重排：US 原地改造 + MVP 打包）

**v3 顺序的逻辑**：
1. **Phase 1 + Phase 2 = MVP（~5 天）**：CF Tunnel + R2 下载。在现网 US 就地改造，零数据迁移。上线后跑三网基线，数据不达标立即 § 11.6 撤退
2. MVP 数据达标后，再评估是否推进 Phase 3（R2 上传）+ Phase 5（运维）
3. **Phase 4（v2 的独立 YouTube Worker）已取消**：YouTube 下载留在 US 主节点现有 pipeline
4. **Phase 6（US→SG 迁移）为可选**：只在实测大陆体验不达标时启用（详见附录 C）

每个 Phase 独立上线 + 可独立回滚。

### ⭐ MVP = Phase 1 + Phase 2（5 天闭环）

MVP 完成即达成方案 80% 用户体验收益 + 90% 运维止血。MVP 上线后先观察 1-2 周，再决定 Phase 3-5 是否推进。

### v2 原顺序（已作废，保留说明）

v2 原文"新顺序逻辑"：
1. **先止血运维**（Tunnel 隐藏源站 + 下载迁 R2）→ 解决"磁盘爆 / 源站吃带宽 / 大陆下载慢"三大痛点
2. **再优化体验**（浏览器直传）→ 解决"大陆上传 2GB 超时"
3. **最后做专项**（US worker）→ 保护 SG IP 免受 YouTube 风控
4. **运维和 CI 放最后**（不阻塞业务改造）

每个 Phase 独立上线 + 可独立回滚。

### Phase 0：前置准备 + 三探针（1.5 天，v4 加强）

1. 注册 Cloudflare 账号（免费计划够）
2. 迁移域名 NS 到 CF
3. 开通 R2（需要绑定支付方式，有免费额度）
4. 创建 3 个 buckets（`avt-uploads` / `avt-artifacts` / `avt-backups`）+ API Token + CORS 配置（单 origin）
5. **不配 Custom Domain**（v4 D33：presigned URL 不支持 custom domain）
6. 生成 `AVT_INTERNAL_API_KEY`（`python -c "import secrets; print(secrets.token_urlsafe(32))"`）
7. **跑三探针**（v4 D37，全部数据进 § 15 基线快照）：

   **探针 ①：当前 US 直连基线（三网）**
   - 三网（电信 / 联通 / 移动）各用浏览器 + mtr + speedtest 跑一次：LCP、API `GET /jobs` P50、下载 100MB 成品速度
   - 作为 Phase 1 上线后对比数据

   **探针 ②：R2 原生域名下载稳定性（三网，v4 关键）**
   - 在 `avt-artifacts` 放一个 100MB 测试文件；用 Gateway 现有 boto3 凭据手工签一个 30min 过期的 presigned URL
   - 三网各跑 **5 次** `curl -w "speed=%{speed_download} code=%{http_code}\n" -o /dev/null <presigned-url>` 记录：
     - 平均下载速度、min/max、成功率
     - 是否有 RST / 连接超时 / HTTP 非 200
   - **放行判据**：任一运营商 < 1 MB/s 或成功率 < 90% → 记为"R2 原生域名大陆不稳"，MVP 上线后按 D39 启动 Phase 2b 备胎（artifacts public + Worker HMAC）

   **探针 ③：R2 真实 multipart 上传样本（三网，v4 关键）**
   - 手工跑：`aws s3 cp ./sample_2gb.mp4 s3://avt-uploads/test-<time>/ --endpoint-url=https://<account>.r2.cloudflarestorage.com --cli-chunk-size=26214400`
     （或写一个 boto3 脚本模拟前端 25MB 分片流程）
   - 三网（电信 / 联通 / 移动）各跑 **至少 1 次** 完整 2GB 上传，记录：
     - 总耗时、平均上行速度、分片重传次数、完成率
   - **Phase 3 放行判据**（D38）：
     - 三网成功率 ≥ 80% → Phase 3 按原计划 4.5d
     - 60-80% → Phase 3 先做 UI 灰度 + 失败清晰提示，+1d UI
     - < 60% → Phase 3 **重评审**（R2 原生域名上传大陆不稳，备胎方向 B 也不解决——上传桶必须私有；可能需要推迟到 Phase 6 SG 迁移）

**回滚**：CF 账号保留，DNS 切回原服务商（5 min 内生效）。

---

### Phase 1：Cloudflare Tunnel 隐藏 US 源站（1.5 天）

**目标**：大陆用户能通过 `app.yourdomain.com` 经 Cloudflare 边缘访问 **US 节点**，不走代理、不暴露 US IP。**不动业务代码**。

1. US 上部署 `cloudflared-us` 容器 + `/opt/aivideotrans/cloudflared/config.yml`（§ 4.3）
2. CF Dashboard 创建 tunnel `avt-us`，DNS 记录 `app.yourdomain.com` / `status.yourdomain.com`（**不配** `files.yourdomain.com`，v4 D33；仅当 Phase 2b 触发时再加）
3. 测试 Tunnel 通路：`curl -H "Host: app.yourdomain.com" https://<tunnel-uuid>.cfargotunnel.com/health`
4. 切流量：把**原有**的 DNS `A` 记录（指 US IP）换成 `CNAME` 指 tunnel；现有 `aitrans.video` 的 `AUTODUB_PUBLIC_HOST` 保留但通过 Tunnel 回源
5. Caddy 降级：改 Caddyfile 为 `127.0.0.1:443` + `tls internal`；或 Phase 1 先 `docker-compose stop caddy`，验证无影响后再按 I2 改配置
6. 关闭 US 公网入口：`ufw deny 80, 443, 3000, 8877, 8880`
7. 验证：E2E 登录、创建 Job（老 youtube 流程）、下载（老本地 FileResponse 流）
8. 用 Phase 0 的基线对比（§ 15）：三网首屏 / API P50 / 下载 10MB 样本的速度变化

**产出**：
- 大陆用户**不用代理就能访问**
- US IP 隐藏，DDoS 风险归零
- 静态资源走 CF 边缘缓存（F6 可在本 phase 顺手做）

**回滚**：DNS 改回 US IP + `ufw allow 80,443` + 恢复 Caddy 公网监听 + ACME 重新拉证（15 min 内）

---

### Phase 2：R2 承接下载（3-3.5 天）⭐ 最高 ROI

**目标（v4 降级）**：
- **US 源站出站带宽下降 ≥ 50%**（核心硬指标，成品视频 / 字幕 / materials / 在线播放 ≤25min opt-in 全部转嫁到 R2）
- **下载体验不劣化**：三网实测相对 Phase 0 基线 ±10% 浮动可接受
- **回滚简单**：`AVT_STORAGE_BACKEND=local` 一键切；或用 D40 `?force_local=1` 即时兜底
- **为 Phase 5 磁盘清理铺路**（D9）：Phase 2 不启用 TTL 清理，但产物推 R2 后 Phase 5 可安全删本地

**不承诺**（v4 D36）：
- 大陆下载速度加速（CF 原生域名在大陆表现取决于线路，Phase 0 探针② 实测决定）
- 磁盘止血（pipeline 仍写本地 workspace；Phase 5 才清理）

上传侧暂不改（保持老 `/upload-video` 流式；Phase 3 由 D38 关卡决定是否推进）。

1. Merge 后端 B1-B2（r2_client 单 endpoint，v4 D33）、B9（JobRecord 字段 + store 序列化）、B12+B12.5（service 钩子 + `TASK_EXECUTORS` 注册 `publish_artifacts_to_r2`）、B13（artifact_publisher + ffprobe 记 duration）、B14（api.py 下载 / stream / tts-segments-zip 三分支 302 改造 + `force_local=1` 开关 D40）、B15-B16（background_task / materials 302）
2. 前端 F7（类型）+ F9（`payload.sub_stage` 子阶段渲染）
3. 测试（B23 / B26 / B27）：`AVT_STORAGE_BACKEND=r2`，跑一个任务 → 成品自动推 R2 → 前端下载走 302 → 浏览器实际打到 `<account>.r2.cloudflarestorage.com` 原生域名（v4 D33）
4. 老任务（Phase 2 之前完成的）仍从本地读，验证 fallback
5. **灰度 1 周**：新任务全走 R2，观察 R2 费用 / 下载速度（三网实测）
6. **`/stream` 条件 opt-in 验证**：跑一个 30min 任务 → 确认走本地 Range 流；跑一个 10min 任务 → 确认走 R2 302
7. 本地磁盘 TTL 清理**暂不启用**（Phase 5 再做）

**产出（v4 修正）**：
- **US 源站出站带宽下降 ≥ 50%**（下载流量转嫁到 R2）
- 下载体验**不劣化**（三网 ±10% 浮动内）
- 如有加速收益是额外彩头（实测为准，不预设）
- 为 Phase 5 本地磁盘清理铺路

**回滚**：
- 即时开关：URL 加 `?force_local=1` 参数（D40），Gateway 立即走本地 FileResponse
- 短期回滚：`AVT_STORAGE_BACKEND=local`，api.py 的 302 路径被短路（R2 key 不回填），所有下载回本地 FileResponse（需保留旧代码分支 4 周）
- 彻底回滚：DNS 切回 US 直连 IP（参考 § 11.6 撤退预案）

**⭐ MVP 放行判据**（Phase 1 + Phase 2 完成后，v4 D36）：
- 硬指标 ①：US 源站出站带宽下降 ≥ 50%（通过 cloudflared metrics 或 cloudflare dashboard 流量图对比） → ✅
- 硬指标 ②：三网实测下载速度相对 Phase 0 ± 10% 浮动（不硬性要求加速） → ✅
- 硬指标 ③：撤退预案 § 11.6 操作走一遍，实测 15 min 内完成 → ✅
- **三项都达标** → 继续 Phase 3（按 D38 判据走）
- **任一不达标** → § 11.6 撤退；若硬指标 ② 三网有一网严重劣化但其他两网 OK，启动 Phase 2b 备胎（D39）

---

### Phase 2b：下载链路备胎（v4 新增，D39，仅条件触发）

**触发条件**：Phase 0 探针 ② **或** MVP 上线后实测 `<account>.r2.cloudflarestorage.com` 下载在三网任一不稳（< 1 MB/s 或成功率 < 90%）。

**方案**：
1. R2 Dashboard → `avt-artifacts` bucket → 设为 **Public Access（Custom Domain）**
2. R2 → Settings → Custom Domains → 绑定 `files.yourdomain.com`（CF DNS 自动配）
3. 部署 Cloudflare Worker（代码片段见下），拦截 `files.yourdomain.com/*` 请求：
   - 从查询参数读 `token` + `exp`
   - 校验 HMAC `hmac_sha256(path + exp, WORKER_HMAC_SECRET)`
   - 校验 `exp > now`
   - 通过 → 继续到 R2；失败 → 403
4. Gateway `presign_get` 改为生成自定义 URL：
   ```python
   def presign_get_via_worker(bucket, key, expires, filename):
       exp = int(time.time()) + expires
       path = f"/{bucket}/{key}"
       token = hmac.new(HMAC_SECRET, f"{path}{exp}".encode(), "sha256").hexdigest()
       return f"https://files.yourdomain.com{path}?token={token}&exp={exp}"
   ```
5. 老的 `presign_get`（原生 R2 endpoint）保留，作为 feature flag 切换：`R2_DOWNLOAD_MODE=native|worker_hmac`

**工时**：~1.5d（Worker 代码 + HMAC 签/校验 + 集成测试）

**关键限制**：
- **只解决下载**。上传桶不能 public，上传仍然走原生 R2 endpoint（受 Phase 0 探针 ③ 约束）
- Worker 免费额度 100k req/天：按每成品被下载 5 次 × 活跃用户 = 初期绝对够，月活千人也够
- artifact key 命名要 **不可枚举**（已有的 `artifacts/{user_id}/{job_id}/{type}_{filename}` 格式含 job_id UUID，符合要求）

---

### Phase 3：R2 浏览器直传关卡（v4 降级为可行性门禁，不是确定 4 天实施）

**判据（D38）**：Phase 3 是否做、怎么做，取决于 **Phase 0 探针 ③**（D37-③）和 MVP 上线后实测：

- Phase 0 三网上传成功率 ≥ 80% → **按原计划 4.5d 推进（路径 α）**
- 60-80% → **路径 β：+1d UI 灰度 + 失败提示**，不强切默认
- < 60% → **路径 γ：重评审**（R2 原生域名大陆上传不稳；B 备胎不解，上传桶必须私有；考虑推迟到 Phase 6 SG 迁移）

#### 路径 α（4.5d，Phase 0 数据理想）

1. Merge B3（upload_presign）、B4（挂路由）、B5（老 upload deprecated）、B6（job_intercept 支持 `source_type=r2`）、B10-B11（source_materializer + process_runner hook）
2. 前端 F1-F2（r2Upload.ts 25MB 分片 + TranslationForm 接入）
3. 测试（B24 / B25）：前端上传 2GB → R2 multipart → Job 创建 → materialize 到本地 → pipeline 跑通
4. **断点续传测试**：上传一半刷新页面，恢复进度
5. **失败路径测试**：R2 不可达时 presign 端点 503、合并失败时 abort multipart
6. **灰度**：内测用户先走 R2 上传 1 周
7. 默认切 R2，老 `/upload-video` 标 deprecated（保留 4 周）

#### 路径 β（5.5d，Phase 0 数据中等）

在 α 基础上：
- 前端 `r2Upload.ts` 增加**失败清晰提示 UI**：分片失败 → 显示失败编号；合并失败 → 一键重传
- **不强切默认**：TranslationForm 同时保留老 `/upload-video` 和新 R2 两个入口，用户可在失败时切老路径
- 灰度期延长到 2 周，观察三网成功率；三网都 ≥ 85% 才默认切 R2

#### 路径 γ（重评审，Phase 0 数据不理想）

Phase 3 **暂缓推进**。方向：
- 方向 γ-1：等 Phase 6 SG 迁移后，从 SG 调 R2 上传（SG → 新加坡出海 + MiniMax 新加坡节点原生优势）
- 方向 γ-2：仍保留老 `/upload-video` 流式上传（承认现状不改），重新评估是否值得为上传改架构
- 方向 γ-3：评估第三方对象存储方案（B2 + Cloudflare Bandwidth Alliance 等）

**产出（α/β 均满足）**：2GB 上传稳定、Gateway CPU / 内存压力归零、支持断点续传

**回滚**：前端 `NEXT_PUBLIC_UPLOAD_BACKEND=legacy`（feature flag），回走老端点。

---

### ~~Phase 4：US YouTube Worker~~（**v3 取消**）

v2 原计划把 YouTube 下载拆到独立 worker。v3 重新评估后**取消**：

- 动机不成立：YouTube 下载已经在 US 主节点，**没有"跨境 IP 风控"**（US → YouTube 都在美国本土互联）
- 成本不划算：拆独立 worker 要多跑一台机器 + 多一套 tunnel + 增加架构复杂度
- 真正启用 Phase 6（可选的 US→SG 迁移）时才需要拆 worker —— 见 § 附录 C（若 Phase 6 启用）

---

### Phase 5：运维增强（2-3 天）

1. US 上部署 Uptime Kuma 容器 + Tunnel 子域 `status.yourdomain.com`
2. 监控项（见 § 9.1）
3. 飞书/TG/Email webhook 告警接入
4. Cron 每日 PG 备份 → R2（`scripts/backup_pg.sh`，§ 9.2）
5. Cron 本地磁盘清理（`scripts/cleanup_local.sh`，只删 `r2_artifacts` 已齐的 Job 目录）
6. Cron 孤儿 R2 对象清理（`uploads/` 超 30 天未被任何 Job 引用 → 删）

**产出**：SLA 可观测、故障实时告警、数据有备份

---

### Phase 6（可选，非本期）：长尾优化 + US→SG 迁移备选

**6a. US→SG 迁移**（详细手册见 § 附录 C）—— 仅当 MVP / Phase 5 后实测大陆用户体验仍不达标：
- PG `pg_dumpall` → SG `pg_restore`
- `projects/` 目录 `rsync` 从 US 到 SG
- `.env` + cloudflared config 在 SG 重建
- YouTube 下载策略二选一：(a) SG 本地 yt-dlp；(b) 保留 US 作独立 YouTube worker（复用 § 附录 B 设计）
- DNS CNAME 切换（5 min 内）

**6b. 其他长尾**：
- `/stream/{kind}` 全量迁 R2（含签名 URL auto-refresh，适配长视频）
- 前端运行时评估迁 Cloudflare Workers + OpenNext（如果 Tunnel CF 缓存命中率低）
- 镜像 tag 化 + GitHub Actions CI/CD（替代 Deploy-Via-154）
- PG 读写分离 / Neon / Supabase（用户量起来后）
- JobRecord 下沉 PG（独立专题，不含本方案；简化 JSON+PG 双源真相割裂；参考 Claude Code 二审 #10）
- 多 region YouTube worker（JP / EU）

---

## 8. 成本估算

### 8.1 月度固定成本（v3）

| 项 | 费用 | 备注 |
|----|------|------|
| US 节点（现网，不变）| ~¥85-150 | 现有费用；不增加 |
| SG 节点 | ¥0 | **v3 阶段不启用**（Phase 6 启用时 ~¥85） |
| Cloudflare Tunnel | ¥0 | 免费 |
| Cloudflare R2 存储（20GB 以内）| ¥0 | 免费 10GB + 付费 ~¥1/月 |
| Cloudflare R2 出口 | ¥0 | 无论多少流量 |
| 域名 | ~¥6/月 | 年付，已有 |
| **新增固定月成本** | **~¥0** | 不换机器、不改账单 |

**v3 关键优势**：相对现状，**不新增任何固定月成本**。只有用量付费的 R2 部分会随使用增长。

### 8.2 按用量变化成本（v3 25MB 分片后）

假设月 100 活跃任务，每任务 2 GB 源视频上传 + 500 MB 成品产出：

| 项 | 量 | 计算 | 单价 | 月费 |
|----|----|------|------|------|
| R2 存储（成品 + 源临时）| 50 GB | 成品 50 GB + 源视频在 30 天 TTL 内平均 20 GB | $0.015/GB | ~$1.05 ≈ ¥8 |
| **R2 Class A（写）** | ~**10k** op | 100 任务 × (**82 part** + 1 create + 1 complete + ~10 artifact puts) ≈ 94 op/任务 | $4.5/百万 | $0.045 ≈ ¥0.3 |
| **R2 Class B（读）** | ~10k op | 每成品下载 5 次 × 100 任务 × ~10 small reads/次 | $0.36/百万 | $0.004 ≈ ¥0.03 |
| **合计** | | | | **~¥8** |

**v3 总月成本（初期 100 任务）**：**基线费用 + ~¥8 / 月**（对比 v2 `10MB 分片 5 万 op` 的约 ¥2 成本，v3 Class A 降到 ¥0.3）。

### 8.3 规模敏感性（v3 扩展 op/月 列）

| 任务量/月 | 存储（GB）| Class A op/月 | R2 月费（新增）| 相对 Phase 0 的新增成本 |
|-----------|-----------|---------------|----------------|-------------------------|
| 100 | 50 | ~10 k | ¥8 | ¥8 |
| 500 | 250 | ~50 k | ¥24 | ¥24 |
| 1000 | 500 | ~100 k | ¥45 | ¥45 |
| 5000 | 2500 | ~500 k | ¥200 | ¥200 + 可能升 US 机器 |
| 10000 | 5000 | **~1 M**（接近免费额度）| ¥400 | ¥400 + 必须升机器 |

**R2 免费额度**：Class A 免费 1M op/月 → 约支持 **10k 任务/月**不触发付费。Class B 免费 10M/月 → 不会触发。

**R2 出口免费是压箱底的关键优势**：就算用户量涨 100×，带宽成本仍是 ¥0。

---

## 9. 监控与告警

### 9.1 Uptime Kuma 配置

**监控项**：

| 监控 | 类型 | 频率 | 告警阈值 |
|------|------|------|----------|
| `GET https://app.yourdomain.com/health` | HTTP | 1 min | 2 次失败 |
| `GET https://app.yourdomain.com`（首屏 SSR） | HTTP | 5 min | 2 次失败 |
| **US 磁盘使用率**（v4 改） | Push（脚本上报）| 5 min | >80% |
| cloudflared-us tunnel 连通性 | Push（`cloudflared tunnel info`） | 5 min | 任何下线 |
| R2 读写心跳（原生域名） | Push（cron 脚本）| 10 min | 任何失败 |
| PG 连接数 | Push（Gateway 监控端点）| 5 min | >80% max |
| 每日出站带宽统计（对比 Phase 0 基线） | Push（cloudflared metrics）| 1 day | 超预期 ±30% |

~~`GET https://yt.internal.yourdomain.com/health`~~（v4 删除：独立 YouTube worker 已取消，YouTube 下载在 US 主节点 pipeline 内）

**告警通道**：
- 飞书 webhook（实时）
- Email（冗余）

### 9.2 关键脚本

**`scripts/backup_pg.sh`**：

```bash
#!/usr/bin/env bash
set -euo pipefail

DATE=$(date -u +%Y%m%d-%H%M)
BACKUP_FILE="/tmp/avt-pg-${DATE}.sql.gz"

docker exec aivideotrans-postgres pg_dumpall -U postgres \
  | gzip -9 > "${BACKUP_FILE}"

aws s3 cp "${BACKUP_FILE}" \
  "s3://avt-backups/postgres/daily/${DATE}.sql.gz" \
  --endpoint-url "${R2_ENDPOINT}"

rm -f "${BACKUP_FILE}"

# 清 30 天前的备份（R2 lifecycle 已配，这里兜底）
aws s3 ls "s3://avt-backups/postgres/daily/" --endpoint-url "${R2_ENDPOINT}" \
  | awk '$1 < "'$(date -u -d '30 days ago' +%Y-%m-%d)'" { print $4 }' \
  | xargs -r -I{} aws s3 rm "s3://avt-backups/postgres/daily/{}" \
      --endpoint-url "${R2_ENDPOINT}"
```

**`scripts/cleanup_local.sh`**：

```bash
#!/usr/bin/env bash
# 清 30 天前的 projects/ 目录（已推 R2 的任务）
set -euo pipefail

PROJECTS_DIR="${AIVIDEOTRANS_PROJECTS_DIR:-/opt/aivideotrans/data/projects}"
find "${PROJECTS_DIR}" -mindepth 2 -maxdepth 2 -type d \
  -mtime +30 \
  -exec python3 /opt/avt/scripts/verify_pushed_to_r2.py {} \; \
  -exec rm -rf {} \;
```

**`scripts/verify_pushed_to_r2.py`**（伪代码，防止删未推送的目录）：

```python
#!/usr/bin/env python3
"""Exit 0 if all artifacts in project_dir have r2_keys in manifest."""
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "manifest.json"
if not p.exists(): sys.exit(1)
m = json.loads(p.read_text())
sys.exit(0 if m.get("r2_artifacts") else 1)
```

---

## 10. 风险、缓解、回滚

### 10.1 风险矩阵（v3）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| CF 大陆线路偶发抖动（高峰 / 节点切换）| **中-高** | 用户访问慢 | 见 § 10.3 **CF 大陆 Plan B** |
| R2 停止服务 | 低 | 上传下载全挂 | 每日 PG 备份到 R2 本身（同域容灾有限），可选双写 Backblaze B2 |
| Gemini / AssemblyAI API 不稳 | 中 | 部分任务失败 | 已有重试；监控；未来可多出口轮转 |
| 预签名 URL 泄露 | 低 | 非法下载 | URL 30 min 过期；object key 含 user_id 前缀校验 |
| 用户恶意大文件上传 | 中 | R2 费用暴涨 | presign 时校验 size ≤ 2GB；用户配额表 |
| YouTube IP 风控 | 低 | 下载被拒 | US 在美国本土，风控低；必要时支持 cookies（Phase 6） |
| cloudflared 客户端崩溃 | 低 | API 全挂 | docker restart policy `unless-stopped`；Uptime Kuma 监控 |
| PG 数据丢失 | 极低 | 灾难性 | 每日备份 + 30 天保留；R2 跨区复制（可选）|
| Next 容器崩溃 | 低 | 前端打不开 | docker restart policy + Uptime Kuma 监控 |

### 10.2 通用回滚原则

**每个 Phase 都能独立回滚**，原则是：

1. **DNS 层切流量**：所有入口都是 `xxx.yourdomain.com`，DNS 改 CNAME 即可
2. **特性开关**：后端 `AVT_STORAGE_BACKEND` / 前端 `NEXT_PUBLIC_UPLOAD_BACKEND` 一键切
3. **代码向后兼容**：新老上传 / 下载端点并存 2 周，确认稳定再删
4. **数据兼容**：JobRecord 新字段可空，老任务不受影响

### 10.3 CF 大陆 Plan B（v3 新增）

**前提认知**：Cloudflare 在大陆**不是稳定商业 SLA**，是"尽力服务"。GFW 偶发干扰 + 运营商 QoS 降速是已知现象，尤其电信 / 联通晚高峰。

**三层备选**（从容易到激进，优先级从上到下）：

#### 10.3.1 R2 原生域名稳定性（v4 改：Phase 0 探针实测，不预设）

- v4 不强制 custom domain（D33 / Cloudflare 约束）
- 依靠 **Phase 0 探针 ②**（D37-②）实测 `<account>.r2.cloudflarestorage.com` 的三网稳定性
- 数据好 → 直接上 MVP
- 数据差 → 启用 **Phase 2b**（D39）：public bucket + Worker HMAC + custom domain，**仅解决下载链路**

#### 10.3.2 前端重试 + 优雅降级（代码侧，MVP 必做）

- `r2Upload.ts` 每个 part 失败重试 3 次，指数退避 500ms / 1s / 2s
- 下载 302 被中断：运维通过 `force_local=1` 紧急兜底（D40，URL 加参数，Gateway 立即回本地 FileResponse）
- `<video>` stream 播放失败时，前端 `onerror` fallback 到同 URL 加 `?force_local=1`（MVP 可做，也可 Phase 5 评估）

#### 10.3.3 备选边缘 CDN（撤退方案，可选）

如果 CF 在大陆稳定性持续不达标：

| 备选 CDN | 大陆访问 | 启用方式 | 成本 |
|----------|----------|----------|------|
| **字节 EdgeOne 海外加速** | 优（在大陆有 POP）| DNS CNAME 切 `app.*` 到 EdgeOne 边缘 | 按流量，付费 |
| **Gcore CDN** | 中-优（亚洲 POP 多）| 同上 | 流量包 |
| **直连 US IP（裸跑）** | 差但可用 | DNS 改回 A 记录 | ¥0 |

**切换动作**（5 分钟内完成）：
1. CF Dashboard → DNS → 改 `app.yourdomain.com` CNAME 指向备选 CDN
2. 备选 CDN 源站配置成 US IP（直连）或 CF Tunnel（通过 CF 隧道）
3. 源站 ufw 临时放行备选 CDN 的 IP 段

**不承诺 SLA**：方案里不写"99.9% 可用"这种数字。预期行为：95%+ 时间好用，偶发抖动时用户需等 1-5 分钟，严重时启用备选边缘。

---

## 11. 验收标准（v2 相对基线，不写绝对数字）

### 11.1 功能性

- [ ] 大陆用户（电信 / 联通 / 移动 各一台）**不用代理**能打开 `app.yourdomain.com` 并完成登录
- [ ] 注册 / 登录 / 列表页 / 详情页 / 工作台 / 编辑页全部可用（smoke test 用例清单见 § 11.4）
- [ ] 上传 2 GB 源视频成功；中途刷新 / 关标签后**从上次分片继续**
- [ ] 上传失败有明确错误提示（格式 / 超限 / 网络）
- [ ] YouTube URL 任务：US 本地 yt-dlp 正常下载（现有 pipeline 不变）
- [ ] 成品视频下载走 302 → R2 预签名 URL；浏览器 Network 面板可见跳转目标是 **`<account>.r2.cloudflarestorage.com`**（v4 D33 原生域名；仅 Phase 2b 备胎触发后才会是 `files.yourdomain.com`）
- [ ] 老任务（Phase 2 之前）仍可下载（FileResponse fallback 生效）
- [ ] 在线播放（`/stream/{kind}`）：≤25min 任务走 R2 302；>25min 任务走本地 Range 流（D35 opt-in）

### 11.2 非功能性

- [ ] US 源站 **80 / 443 / 3000 / 3001 / 8877 / 8880 端口从公网不可达**（`nmap -p 80,443,3000,3001,8877,8880 5.78.122.220` 全 filtered）
- [ ] Gateway 进程稳态 CPU、内存**相对 Phase 1 基线下降**（上传 / 下载不再经进程，预期 CPU ↓50%+，内存 ↓30%+）
- [ ] 本地 `projects/` 目录在 Phase 5 上线 30 天后稳定（不再单调增长）
- [ ] Uptime Kuma 监控覆盖 § 9.1 清单全部项
- [ ] 每日 PG 备份可恢复（每月做一次 restore 演练）
- [ ] 任一 Phase 回滚 ≤ 15 分钟可完成（DNS TTL 60s + feature flag + 镜像切换）

### 11.3 性能（v4 温和目标，不承诺大陆加速）

**指标**：在电信 / 联通 / 移动各一条链路上，MVP（Phase 1+2）上线后对比 Phase 0 基线（§ 15）：

**硬指标（MVP 放行必过）**：
- [ ] US 源站出站带宽**下降 ≥ 50%**（Cloudflare dashboard + cloudflared metrics 前后对比）
- [ ] 三网下载速度**不劣化 ±10%**（不要求提升；若有提升视为额外收益）
- [ ] 15 min 撤退演练一次走通（§ 11.6）

**软指标（记录不强求）**：
- LCP 变化（Phase 0 vs Phase 1/2 对比）写入 § 15.4
- API P50 变化同上
- `<video>` 首帧时间变化同上

**v4 去掉的承诺**：
- ~~首屏 LCP 下降 ≥ 30%~~（大陆 CF 线路不可预测，不预设）
- ~~下载速度 ≥ 3x Phase 0~~（同上；原生 R2 域名表现未知）
- ~~API P50 下降 ≥ 30%~~（同上）

**如果探针数据显示有加速空间**（Phase 0 探针 ① 与 ② 对比），可以在 § 15.4 列"预期加速值"作参考，但**不写入验收门槛**。

### 11.4 Smoke Test 用例清单（v3）

每个 Phase 上线后跑一遍：

| 用例 | Phase 1 | Phase 2 (MVP) | Phase 3 | Phase 5 |
|------|---------|---------------|---------|---------|
| 未登录打开首页 | ✓ | ✓ | ✓ | ✓ |
| 登录 / 登出 / 会话持久 | ✓ | ✓ | ✓ | ✓ |
| 创建 YouTube Job（US 本地 yt-dlp） | ✓ | ✓ | ✓ | ✓ |
| 创建本地上传 Job（< 100 MB） | ✓ (老上传) | ✓ (老上传) | ✓ (R2 直传) | ✓ |
| 创建本地上传 Job（2 GB） | ⚠️ 可能超时 | ⚠️ | ✓ (分片) | ✓ |
| 上传中刷新续传 | - | - | ✓ | ✓ |
| 下载 final_video | ✓ (本地) | ✓ (302→R2) | ✓ | ✓ |
| 下载 subtitles_zh | ✓ | ✓ | ✓ | ✓ |
| 下载 materials_pack | ✓ | ✓ | ✓ | ✓ |
| `<video>` 在线播放（stream） | ✓ | ✓ | ✓ | ✓ |
| Studio 进入编辑 → 修改 → commit | ✓ | ✓ | ✓ | ✓ |
| 三条链路（电信/联通/移动）均可达 | ✓ | ✓ | ✓ | ✓ |

### 11.5 成本

- [ ] 100 任务 / 月 新增成本 ≤ ¥50（对应 § 8.2 R2 费用）
- [ ] R2 存储用量按 lifecycle 线性增长（无 orphan 堆积；`avt-uploads` 下每月新增 ≈ 当月活跃任务数 × 平均源视频大小）
- [ ] R2 Class A / B 调用数与任务数线性相关（排除代码 bug 导致的风暴）

### 11.6 MVP 撤退预案（v3 新增）

**MVP（Phase 1+2）上线 48h 内实测**，任一下列条件触发，立即启动撤退：

| 触发条件 | 检测方式 |
|----------|---------|
| 三网 LCP 有一网相对基线**没有提升**（或反而下降）| § 15 基线对比 |
| 三网下载速度有一网 < 1 MB/s 持续 24h | § 11.3 下载测试样本 |
| Cloudflare Tunnel 掉线累计 > 5 min / 24h | Uptime Kuma 统计 |
| R2 presign URL 在大陆首次访问成功率 < 70% | 用户反馈 + 主动探测 |

**撤退步骤**（计划 15 min 内完成）：

```bash
# 1. DNS: app.yourdomain.com CNAME → US IP 直连 (5 min 传播, 实际 ~1 min)
#    CF Dashboard → DNS → 改 app 记录：CNAME -> A 5.78.122.220 (取消 🟠Proxied)

# 2. US 节点: 重开公网 80/443
ufw allow 80/tcp
ufw allow 443/tcp

# 3. 重启 Caddy 容器, ACME 重新拉 Let's Encrypt 证书 (~1 min)
docker-compose restart caddy
docker-compose logs -f caddy   # 等 "certificate obtained successfully"

# 4. 停 cloudflared (可选, 先保留观察 1 天再下)
docker-compose stop cloudflared-us

# 5. 验证:
#    - curl https://app.yourdomain.com/health  (直连 US IP)
#    - 用户继续用原代理路径访问 (恢复到 Phase 0)
```

**数据回滚**：R2 上已推的成品**不删除**（不占成本，留着之后重启 Phase 2 用）；`AVT_STORAGE_BACKEND=local` 让新任务不再推 R2，老任务下载走本地 FileResponse fallback。

**后续决策**：
- 撤退后观察 1 周，收集日志 + 三网用户反馈
- 方向 A：启用 **Phase 2b**（D39 / § Phase 2b 章节）—— 若下载硬指标不过
- 方向 B：启用 Phase 6 US→SG 迁移（见附录 C）
- 方向 C：承认当前方案对大陆不适用，维持现状 + 代理访问

### 11.7 Phase 3 独立判据（v4 新增，不受 MVP 绑定）

**关键原则**（CodeX 三审建议）：即使 Phase 1+2 MVP 数据达标，Phase 3（上传）**不能当然推进**。因为 MVP 验证的是"下载+访问"链路，上传链路在 v4 架构下同样受原生 R2 域名质量约束（不能走 custom domain，也不能通过 Phase 2b 补救）。

**Phase 3 独立门禁**（走 D38 判据，基于 Phase 0 探针 ③ 和 Phase 3 灰度测试）：

| Phase 0 探针 ③ 三网上传成功率 | Phase 3 推进方式 | 工时 |
|------------------------------|-----------------|------|
| ≥ 80% | 路径 α：按原 4.5d | 4.5d |
| 60-80% | 路径 β：UI 灰度 + 失败提示 + 2 周观察期 | 5.5d |
| < 60% | 路径 γ：Phase 3 暂缓；重评审（可能要 Phase 6 SG 迁移）| 不定 |

**Phase 3 上线后**还需要跑一次**真实用户 2GB 上传**的灰度周（内测 1 周），成功率再次 ≥ 80% 才默认全切。

---

## 12. FAQ

**Q1: 为什么不用 AWS S3 + CloudFront？**
A: CloudFront 大陆访问比 CF 慢，且 S3 出口费 $0.09/GB（月 500GB 就 $45），R2 出口免费是碾压性优势。

**Q2: 为什么不迁前端到 Cloudflare Pages / Workers？**
A: v1 想过，v2 推翻。`@cloudflare/next-on-pages` 官方已 deprecated；迁 Workers + OpenNext 是框架级迁移（risk 大 + Next 16 App Router 兼容面还在演化）。当前 Next.js standalone 容器 + Tunnel 直连已经能让 CF 边缘缓存静态资源，收益 80% 到手。真需要迁等 Phase 6 专题做。

**Q3: 为什么不用 Supabase / Neon 代替自建 PG？**
A: 初期成本差不多，但管控力弱（外部托管）、latency 增加。等用户量起来，再考虑迁 Supabase 省运维。

**Q4: Cloudflare 封号怎么办？**
A: 免费层服务合规即可，无风险。真正用户量上来了用 Pro 计划（$20/月）即有正式支持。

**Q5: 多 worker 怎么加？**
A: Phase 3 之后，新加 `worker-sg-2` 节点，docker-compose `scale app=2`，PG 行锁协调 Job 分配。

**Q6: 如何做灰度发布？**
A: 前端 `NEXT_PUBLIC_UPLOAD_BACKEND` 按用户 ID hash 分流；后端同时支持两套链路。

**Q7: YouTube worker 挂了怎么办？**
A: SG 端用 circuit breaker，连续 5 次失败切回 SG 本地 yt-dlp（降级）。告警触发人工介入。

**Q8: 预签名 URL 泄露了别人能下载我的视频吗？**
A: 能，但 30 min 过期。高敏感场景可缩短至 5 min。object key 包含 `user_id` 前缀，Gateway 检查所有权再签。

**Q9: 为什么不用 R2 Public Bucket 直接公开成品？**
A: 需要权限控制（只有任务所有者能下载），预签名 URL 是标准方案。未来可扩展到分享功能（临时 URL）。

**Q10: 测试环境怎么办？**
A: 开发者本机用 `AVT_STORAGE_BACKEND=local` 走传统路径，不依赖 R2。CI 可用 [MinIO](https://min.io/) 本地模拟 S3。

---

## 13. 后续优化方向（v2+）

按优先级：

1. **P1 / 用户增长触发**：
   - R2 lifecycle 按用户计划分层（免费 7 天、付费 30 天、企业 90 天）
   - 备案后迁主存储到阿里云 OSS，R2 降级为境外灾备

2. **P2 / 性能触发**：
   - SG 节点升 CPX41（8C16G）或新增 SG2
   - PG 读写分离（读库接 Supabase）
   - 批量 re-TTS 异步化 + Redis 队列

3. **P3 / 业务需求触发**：
   - 多 region YouTube worker（US + JP + EU）应对 IP 风控
   - 成品分享 URL（临时公开 + 密码保护）
   - 断点上传大小提升到 10GB（4K 长视频支持）

4. **P4 / 运维优化**：
   - GitHub Actions CI/CD 取代 Deploy-Via-154
   - Grafana Loki 日志聚合
   - Sentry 错误追踪

---

## 14. 参考资料

- [Cloudflare R2 S3 API 兼容性文档](https://developers.cloudflare.com/r2/api/s3/api/)
- [Cloudflare R2 Presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
- [Cloudflare Tunnel 官方教程](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- [Cloudflare Pages Next.js 指南](https://developers.cloudflare.com/pages/framework-guides/nextjs/)
- [Cloudflare Workers Next.js 指南](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/)（**后期迁移首选**）
- [OpenNext for Cloudflare](https://opennext.js.org/cloudflare)
- [@cloudflare/next-on-pages（已 deprecated）](https://github.com/cloudflare/next-on-pages) ← **不采用**，仅作参考
- [yt-dlp 高级用法](https://github.com/yt-dlp/yt-dlp#usage-and-options)
- 本项目现有方案：
  - [2026-04-17-legacy-migration-cleanup.md](2026-04-17-legacy-migration-cleanup.md)（internal_auth 约定）
  - [2026-04-16-background-task-system-plan.md](2026-04-16-background-task-system-plan.md)（后台任务模型）

---

## 15. 基线快照（Phase 0 必填）

Phase 0 执行时填写，用于后续 Phase 验收对比。

### 15.1 测试环境

- 测试日期：`____`
- 当前部署：美国 Hetzner `5.78.122.220`，直连公网（无 CF）
- 测试账户：`____`
- 样本任务：`job_id=____`（包含 final_video ~500MB，subtitles 若干）
- 样本下载对象：`____`（100 MB 固定文件，用于纯下载速度测试）

### 15.2 探针 ①：当前 US 直连基线（三链路各填一次）

> **口径说明**：Phase 1（2026-04-22）Cloudflare Tunnel 上线后，公网入口从
> "US 直连 IP + Caddy 443" 切到 "aitrans.video → CF 边缘 → cloudflared →
> US 回环"。本表的 "TTFB / API P50 / 下载速度" 反映的是**用户实际路径**
> （即经过 CF 边缘的 baseline），不是字面 "直连 US IP"。Phase 1 前的纯直连
> 数据仅供对照，见表下"历史对照"。

**当前基线（2026-04-23，Phase 1 Tunnel + `/probe/*` 样本路由已上线）：**

| 运营商 | 地理位置 | TTFB (ms, 5 次取最快) | API P50 (ms, 10 次) | 下载 100MB 速度 (MB/s) | 是否需代理 |
|--------|---------|----------------------|---------------------|-----------------------|-----------|
| 中国电信 | 武汉 | 1166 | 951 | 9.08 | 否（直连可达 HTTP 200） |
| 中国联通 | `____` | | | | |
| 中国移动 | `____` | | | | |

**历史对照（2026-04-22，Phase 1 前纯 IP 直连 + 未启 CF tunnel）：**

| 运营商 | 地理位置 | TTFB (ms) | API P50 (ms) | 下载 100MB 速度 (MB/s) | 是否需代理 |
|--------|---------|-----------|-------------|-----------------------|-----------|
| 中国电信 | 武汉 | 0 | 10012 | 0.00 | **是（完全封锁，GFW RST）** |

**对照结论（单运营商初步）：**
- GFW 对 US IP 直连端口 443 维持 RST 封锁（历史数据）
- aitrans.video 通过 CF 边缘后电信路径恢复可达，100MB 下载 9.08 MB/s ≈ 73 Mbps，
  远高于 § 11.3 MVP 放行下限（探针 ② 三网 ≥ 1 MB/s 判据的 9 倍裕度）
- TTFB / API P50 ~1s 级反映跨境 + CF 边缘 + tunnel 回源三段 RTT 累计，
  对"首屏慢但可用"的 SaaS 是可接受的（后续可用 CF Argo / Smart Routing 优化，
  但不进 MVP 放行门槛）

### 15.3 探针 ②：R2 原生域名下载稳定性（v4 D37-②，三链路各跑 5 次）

测试方法：用 boto3 手工签一个 30min presigned GET URL，指向 `avt-artifacts/test/100mb.bin`（临时 100MB 样本）。

| 运营商 | 平均速度 (MB/s) | min (MB/s) | max (MB/s) | 成功次数 / 5 | RST 次数 | 备注 |
|--------|---------------|-----------|-----------|-------------|---------|------|
| 中国电信 | 0.34 | 0.15 | 0.78 | 5/5 | 0 | ⚠️ 可达但慢；第1次128s完成(0.78)，第2-5次均超300s限速截断 |
| 中国联通 | | | | | | |
| 中国移动 | | | | | | |

**补充：17ce.com 178 节点覆盖测试（2026-04-22）**
- 覆盖：8 运营商 / 37 省份 / 178 监测点
- **R2 原生域名 `*.r2.cloudflarestorage.com` 全中国普遍可达**（解析到 Cloudflare 泛播 172.64.66.1）
- 三网平均 TTFB：电信 1.24s / 联通 1.16s / 移动 1.34s
- 海外对照组美国节点 TTFB 0.17s，差距 ~1s 来自跨境链路 + R2 回源
- HTTP 400 因 17ce 对 presigned URL 二次编码破坏签名，不代表封锁（家宽 curl 测试返回 200）
- **结论：GFW 层面未阻断 R2，Phase 2 方案可行；Phase 2b 备胎非必需**

**判据（D39 触发）**：
- [ ] 三网平均 ≥ 1 MB/s 且成功率 ≥ 90% → **R2 原生域名可用**，MVP 按 v4 推进
- [ ] 任一运营商 < 1 MB/s 或成功率 < 90% → **启用 Phase 2b**（public bucket + Worker HMAC）

### 15.4 探针 ③：R2 真实 multipart 上传样本（v4 D37-③，三链路各至少 1 次）

测试方法：`aws s3 cp sample_2gb.mp4 s3://avt-uploads/test/ --endpoint-url=https://<account>.r2.cloudflarestorage.com --cli-chunk-size=26214400`

| 运营商 | 总耗时 (min) | 平均上行 (MB/s) | 完成状态 | 分片重传次数 | 备注 |
|--------|-------------|-----------------|---------|-------------|------|
| 中国电信 | | | | | |
| 中国联通 | | | | | |
| 中国移动 | | | | | |

**Phase 3 放行判据（D38）**：
- [ ] 三网成功率 ≥ 80% → Phase 3 路径 α（4.5d）
- [ ] 60-80% → Phase 3 路径 β（5.5d UI 灰度）
- [ ] < 60% → Phase 3 路径 γ（暂缓 + 重评审）

### 15.5 源站运行指标（Phase 0 当天）

- US 磁盘使用：`____ GB / 150 GB`
- `projects/` 目录大小：`____ GB`
- 累计完成任务数：`____`
- Gateway 进程 CPU 稳态：`____%`
- Gateway 进程 RSS：`____ MB`
- 日均出站带宽（GB/day）：`____`（Phase 2 对比必需）

### 15.6 Phase N 上线后对比（重复填）

| Phase | 日期 | 出站带宽下降 % | 电信下载变化 % | 联通下载变化 % | 移动下载变化 % | 磁盘变化 | Gateway CPU 变化 |
|-------|------|---------------|-------------|-------------|-------------|---------|------------------|
| Phase 1 | | | | | | | |
| Phase 2 | | | | | | | |
| Phase 2b | | | | | | | |
| Phase 3 | | | | | | | |
| Phase 5 | | | | | | | |

**MVP 放行判据对照**：出站带宽下降 ≥ 50%（硬）+ 三网下载变化 ±10% 内（硬）+ 15min 撤退演练通过（硬）。

---

## 16. CodeX 代码级审核吸收明细（v2 新增）

v1 初稿经 CodeX 代码级审核后，全部 5 条 P1/P2 意见已吸收并反映在 v2 正文中。本节作为变更溯源。

### P1-1：双子域 `app.*` / `api.*` 会断登录态

**CodeX 原文核心**：
> 当前前端实际只认 `NEXT_PUBLIC_JOB_API_BASE_URL` 而不是 `NEXT_PUBLIC_API_BASE`；session cookie 是 host-only `SameSite=strict`；`apiClient` 依赖 fetch 默认的 same-origin 凭据模式。按方案直接切 `app` / `api`，登录态、鉴权、上传和任务轮询都会断。

**验证**：
- [frontend-next/src/lib/api/config.ts:4](../../frontend-next/src/lib/api/config.ts) — 只认 `NEXT_PUBLIC_JOB_API_BASE_URL`，默认 `/job-api`
- [gateway/auth.py:80-88](../../gateway/auth.py) — `samesite="strict"` + 无 `domain=` 参数 = host-only
- [frontend-next/src/lib/api/client.ts:46-53](../../frontend-next/src/lib/api/client.ts) — 未显式传 `credentials: "include"`，依赖 same-origin 默认行为
- [TranslationForm.tsx:228](../../frontend-next/src/components/workspace/TranslationForm.tsx) / [useBackgroundTask.ts:76](../../frontend-next/src/lib/react/useBackgroundTask.ts) / [entitlements.ts:27](../../frontend-next/src/lib/api/entitlements.ts) — 大量硬编码相对路径

**v2 吸收**：
- D12 改为**单域名** `app.yourdomain.com`，所有流量同源
- § 2.1 架构图重画
- § 4.1 DNS 配置只保留 `app.*` + 可选 `files.*`
- CORS 白名单单 origin
- 前端 F3-F5 **不改** `config.ts` / `downloads.ts` / `ResultMediaCard.tsx`

### P1-2：Tunnel 指向 `localhost:443` + Caddy Let's Encrypt + 关 80 端口自相矛盾

**CodeX 原文核心**：
> 方案要求 `Full (strict)` 和 Caddy 监听 443/TLS；Caddyfile 是公网 Let's Encrypt 模式；Phase 1 又要求关 80/443。这套组合要么协议不对，要么证书获取逻辑失效。

**验证**：
- [Caddyfile:5](../../Caddyfile) — 确实是 `{$AUTODUB_PUBLIC_HOST}` 走 ACME 模式
- Let's Encrypt ACME-01 需要 80 端口可达 → 关 80 后证书续期会失败

**v2 吸收**：
- D27（新决策）：Caddy 保留但降级为 `tls internal` 127.0.0.1 内网入口
- § 4.3 重写：Tunnel 直连 `localhost:3000` 和 `localhost:8880`，**绕过 Caddy**
- SSL 模式从 `Full (strict)` 改为 `Flexible` / `Full`（Tunnel 自带加密）
- 不再依赖 ACME 80 挑战

### P1-3：R2 source 接入侵入 pipeline，工时严重低估

**CodeX 原文核心**：
> 作业链条是"本地路径契约"贯穿到底：`process_runner.py:272` 把 `source_ref` 当 CLI 参数，pipeline 会 `Path(source_ref)` 复制本地文件进 workspace；方案给 YouTube 新增 `downloading_source` 状态，但现有状态集并不认识它。这不是 0.5d 级别补丁。

**验证**：
- [process_runner.py:272-276](../../src/services/jobs/process_runner.py) — `source_ref` 作为 CLI 参数直传
- [src/pipeline/process.py:4312](../../src/pipeline/process.py) — `Path(source_ref)` 本地文件操作
- `src/pipeline/ingest.py` / `src/pipeline/publish.py` **在仓库中不存在**（v1 完全虚构）
- JobRecord 状态集固定为 `queued/running/...`（[src/services/jobs/models.py:29](../../src/services/jobs/models.py)）

**v2 吸收**：
- D25（新决策）：**边界归一化** — pipeline 零改动，Gateway 侧先把 R2 对象拉到本地
- D26（新决策）：不新增 `downloading_source` 状态；用 `job_events` 传递进度
- § 5.1.4 重写：新建 `source_materializer.py`，在 `process_runner.submit` 前调用
- § 6.1 B9/B10/B11 单独列出，工时从 0.5d → 2.1d
- § 5.3 YouTube callback 改为回填 `source_r2_key`，不立即下载

### P2-1：下载主路径未覆盖（真正吃带宽的是 `/download/*` 和 `/stream/*`）

**CodeX 原文核心**：
> 方案主要改的是 task download 接口，但结果页真正使用的是 `downloads.ts` 直接构造 `/job-api/jobs/{id}/download/*` 和 `/stream/*`；`src/services/jobs/api.py` 仍然是从本地文件读字节返回。即使 materials pack 上了 R2，主视频下载和在线播放依然会继续绑住 SG 磁盘与带宽。

**验证**：
- [frontend-next/src/lib/api/downloads.ts:32-45](../../frontend-next/src/lib/api/downloads.ts) — `buildResultDownloadUrl` / `buildStreamUrl` 指 `/jobs/{id}/download/{key}` 和 `/stream/{kind}`
- [src/services/jobs/api.py:204](../../src/services/jobs/api.py) — 本地 FileResponse
- [ResultMediaCard.tsx:118](../../frontend-next/src/components/workspace/ResultMediaCard.tsx) — 播放器/下载按钮用这组 URL

**v2 吸收**：
- D7 改为覆盖**所有主下载端点**（含 `/jobs/{id}/download/{key}`、`/tts-segments-zip`、`/tasks/{tid}/download`）
- D28（新决策）：服务端 302 跳转 + 前端零改动（`<a download>` / `<video src>` 透明兼容）
- § 5.2 完全重写，含端点全景表
- § 5.2.5 明确：`/stream/{kind}` **Phase 2 不迁**（Range 请求 + 播放器复杂度），Phase 6 专题处理
- § 6.1 B14（api.py 下载端点改造）工时 1.0d，B15-B16 补齐 materials / background_task

### P2-2：CF Pages + `next-on-pages` 已 deprecated

**CodeX 原文核心**：
> 当前前端是 Next 16.2.1 + standalone 容器部署；Cloudflare 官方文档已经把"全栈 SSR Next.js"引导到 Workers/OpenNext；`next-on-pages` 官方仓库已标注 deprecated。把"大陆访问提速"绑定到前端运行时迁移，会把 Phase 1 从基础设施优化升级成框架迁移。

**验证**：
- [frontend-next/package.json:16](../../frontend-next/package.json) — Next 16.2.1
- [frontend-next/next.config.ts:3](../../frontend-next/next.config.ts) — `output: "standalone"`
- `@cloudflare/next-on-pages` GitHub README 标记 deprecated，引导到 OpenNext

**v2 吸收**：
- D3 改为**保留 standalone + Docker 容器**
- § 4.2 完全重写：前端不迁运行时，Tunnel 直连 `:3000`
- Phase 1 去掉"前端部署到 CF Pages"步骤（原 Phase 1 步骤 5-6 删除）
- Phase 6 新增（可选）：未来若缓存命中率不足，再评估 OpenNext

### 其他采纳建议

- **验收指标口径**：D29 改为"相对基线提升"，新增 § 15 基线快照
- **Phase 顺序**：按 CodeX 建议重排（Tunnel → R2 下载 → R2 上传 → US Worker）
- **文档标注方式**：所有引用代码路径带行号链接，便于后人核对

---

**v2 方案结束声明**（保留历史记录）— 共 16 节（v2）。

---

## 17. v3 二审吸收明细（Claude Code + CodeX 二审）

v3 基于 v2 再次审核，代码级问题全部修复；方向性调整把主节点留在 US、不再迁 SG。

### 17.1 Claude Code 二审（12 条）

| # | 意见 | v3 处理 |
|---|------|---------|
| 1 | `src/services/jobs/api.py` 不是 FastAPI 是 `BaseHTTPRequestHandler` | **吸收**。§ 5.2.3 / § 5.2.5 重写为 `do_GET` + `self.send_response(302)` 风格；B14 工时不变但语义更准 |
| 2 | `gateway/upload.py` 是 spool form 不是流式 | **部分吸收**（事实陈述）。不改方案；Phase 3 R2 直传上线后整个问题消失 |
| 3 | `TASK_EXECUTORS` 是固定字典，新增 task type 不是无成本 | **吸收**。新增 B12.5（0.3d），显式注册 `publish_artifacts_to_r2` |
| 4 | 缺 US→SG 数据迁移章节 | **吸收**。v3 不做 US→SG 迁移；附录 C 提供手册作 Phase 6 可选预案 |
| 5 | CF 大陆稳定性没 Plan B | **吸收**。新增 § 10.3 三层备选：R2 custom domain 强制 + 前端重试 + 备选边缘 CDN |
| 6 | R2 Class A 算账偏乐观 | **吸收**。D15 分片 10MB → 25MB；§ 8.3 补 op/月列；2GB 文件从 200 op → 82 op |
| 7 | Phase 顺序调整（US 先做主，SG 后加入） | **吸收并采用**。**方向性调整**：US 继续作主节点，SG 暂不启用（D1 / D2 / § 3.1 / § 7 全部重写）|
| 8 | Caddy 直接下线 | **不吸收**。Caddyfile:18-20 管着 `/api/internal/` loopback 限制，`tls internal` 降级更稳；§ 11.6 撤退依赖 Caddy 快速恢复 |
| 9 | `/stream` 推迟过保守 | **部分吸收**。D35 / § 5.2.5 改为条件 opt-in：≤25min 走 R2 302；>25min 保留本地 |
| 10 | JobRecord 下沉 PG | **不并入**（独立专题）。§ 7 Phase 6b 加一行引用 |
| 11 | 打包 MVP | **吸收**。§ 7 明确 **Phase 1+2 = MVP（5d）**；数据达标再继续 Phase 3+ |
| 12 | 撤退方案 | **吸收**。新增 § 11.6 MVP 撤退预案（触发条件 + 15 min 操作步骤 + 后续决策分叉）|

### 17.2 CodeX 二审（4 条 P1/P2 + 1 条 P3）

| # | 意见 | 证据 | v3 处理 |
|---|------|------|---------|
| P1-a | D28 JSON `{download_url}` 与 § 5.2.1 / F4 302-only 自相矛盾 | v2 D28 vs § 5.2.1 | **吸收**。D28 完全重写为 "统一 302-only；v2 JSON 方案作废" |
| P1-b | `source.download_*` 新 `event_type` 与 [events.py:8-12](../../src/services/jobs/events.py) 只认 `log/status` 冲突；[mappers.ts:119-123](../../frontend-next/src/lib/api/mappers.ts) 不消费 payload | 代码级验证 | **吸收**。D31 新增：复用 `status` 事件 + `payload.sub_stage`；events.py / mappers.ts 基础契约不动；UI 层 F9（0.3d）扩展读 payload |
| P2-a | `source_type` 写作 `local_file` 是旧别名，正式值是 `local_video`（[models.py:11](../../src/services/jobs/models.py)）| 代码级验证 | **吸收**。D32 新增：正式三元组 `youtube_url\|local_video\|r2`；`local_file` 仅作为历史兼容说明；全文统一替换 |
| P2-b | B9 引用不存在的 `record_line.py` | 代码级验证（仓库无此文件）| **吸收**。B9 改为引用真实文件 [models.py](../../src/services/jobs/models.py) + [store.py](../../src/services/jobs/store.py) |
| P3 | § 0.3 目标里绝对 SLA（`<2s / P95 <1s`）残留 | 和 D29 冲突 | **吸收**。§ 0.3 改为"相对基线提升"，具体数字在 § 11.3 + § 15 基线中锁定 |

---

## 附录 C. US→SG 迁移手册（v3 Phase 6 可选）

**何时启用**：MVP（Phase 1+2）上线后实测大陆用户体验不达标（参考 § 11.6 撤退预案触发条件），且 § 10.3 备选边缘 CDN 方案也试过仍未解决。

### C.1 迁移前置检查清单

- [ ] SG 节点（5.223.84.82）docker 环境就绪，通过 `Deploy-SG-Via-154.cmd` 可部署
- [ ] SG 节点磁盘 ≥ 当前 US `projects/` 大小的 1.5 倍
- [ ] SG 节点可连通 Gemini / AssemblyAI / MiniMax / VolcEngine（curl 测试）
- [ ] SG 节点 `.env` 已就位（PG 密码、INTERNAL_KEY、R2 凭据等；从 US 的 `.env` 拷贝）
- [ ] R2 配置无需改动（所有 object key 继续可用，SG 和 US 用同一账号同一 bucket）

### C.2 迁移步骤（计划 2-3 小时窗口）

**步骤 1：同步静态配置（可先做，不停机）**

```bash
# US 侧 → SG 侧
scp /opt/aivideotrans/config/.env sg-host:/opt/aivideotrans/config/.env
scp /opt/aivideotrans/config/gcp-service-account.json sg-host:/opt/aivideotrans/config/
scp /opt/aivideotrans/config/admin_settings.json sg-host:/opt/aivideotrans/config/
rsync -avz /opt/aivideotrans/caddy/ sg-host:/opt/aivideotrans/caddy/
```

**步骤 2：同步 `projects/` 目录（可先做，不停机）**

```bash
# 增量 rsync, 多次跑直到 diff 小
rsync -avz --delete \
  /opt/aivideotrans/data/projects/ \
  sg-host:/opt/aivideotrans/data/projects/
```

**步骤 3：启用维护模式（开始停机窗口）**

```bash
# US 侧: Caddy 返回 503 维护页
# 或临时通知用户 "维护中"
```

**步骤 4：PG dump + restore**

```bash
# US 侧
docker exec aivideotrans-postgres pg_dumpall -U postgres | gzip > /tmp/pg_dump.sql.gz
scp /tmp/pg_dump.sql.gz sg-host:/tmp/

# SG 侧
zcat /tmp/pg_dump.sql.gz | docker exec -i aivideotrans-postgres psql -U postgres
```

**步骤 5：最后一轮 `projects/` 增量同步**（捕获停机前的最后变化）

```bash
rsync -avz --delete /opt/aivideotrans/data/projects/ sg-host:/opt/aivideotrans/data/projects/
```

**步骤 6：SG 启动服务**

```bash
# SG 上
cd /opt/aivideotrans
docker-compose up -d postgres   # 等 DB ready
docker-compose up -d gateway app next cloudflared-sg
docker-compose logs -f          # 观察启动
# 本地 curl 验证
curl -H "Host: app.yourdomain.com" http://localhost:3000/health
```

**步骤 7：DNS 切换（决定性动作）**

```
CF Dashboard → DNS → app.yourdomain.com
  改 CNAME 目标: <us-tunnel-uuid>.cfargotunnel.com → <sg-tunnel-uuid>.cfargotunnel.com
  TTL: 60s (已是 Proxied, 实际走 CF 边缘, 传播 < 1 min)

同理 status.yourdomain.com 切 SG tunnel；R2 相关配置（v4 主路径用原生 <account>.r2.cloudflarestorage.com）无需改
如 Phase 2b 已启用: files.yourdomain.com custom domain 目标是 R2 bucket 不是 tunnel, 也无需随 SG 切换
```

**步骤 8：验证**

- 三网浏览器访问 `app.yourdomain.com`，登录、列表、详情都通
- 跑一个 YouTube 任务（验证 SG 下 YouTube 是否稳定 —— 如 SG 下 YouTube 不稳，启用 § B "US 独立 YouTube Worker"）
- 下载一个老任务的成品（验证 R2 key 继续可用）

### C.3 YouTube 下载策略选择

SG 启用后，YouTube 下载的两条路径：

**路径 a：SG 本地 yt-dlp**（最简单）
- 不需要独立 worker
- 风险：SG IP 做 YouTube 下载可能被限速 / 风控
- 测试方法：跑 10 个任务观察成功率 + 平均下载耗时；对比 US 历史数据

**路径 b：保留 US 作独立 YouTube Worker**（复用 § 附录 B 的 v2 设计）
- SG 主节点 + US worker 的架构（v2 原方案）
- 此时 § 附录 B 的代码清单（B7/B8/B17/B18/B19）重新激活

路径选择取决于路径 a 的实测。**默认先试路径 a**（零额外成本），不达标再切路径 b。

### C.4 迁移回滚

如果 SG 上线后发现问题：

```bash
# DNS 改回 US tunnel (1 min 生效)
CF Dashboard → app.yourdomain.com CNAME → <us-tunnel-uuid>.cfargotunnel.com

# PG 数据: SG 上的写入需要 dump 回 US (如果有新数据)
# 通常在首次切流量的 2-4 小时内，数据变化可控
```

US 节点在迁移后**保留 2-4 周**作为冷备，期间 PG 定时增量同步。确认 SG 稳定后再下 US。

---

**v3 方案结束声明**（保留历史记录）。

---

## 18. v4 三审吸收明细（CodeX 三审，2026-04-21）

v3 经 CodeX 三审发现 R2 能力边界硬冲突 + 前端 payload 链路断点 + 内部口径矛盾 + MVP 承诺过度乐观。v4 全部吸收，采用"A + 更严格探针"方向。

### 18.1 P1/P2 四条代码 / 事实性问题

| # | 意见 | 证据 | v4 处理 |
|---|------|------|---------|
| P1-a | R2 custom-domain presign 不可执行 | Cloudflare 官方 Presigned URLs 文档原文："cannot be used with custom domains" | **吸收**。D33 推翻重写（原生域名 only）；r2_client 删 `get_r2_public_client`；R2Config 删 `public_base`；§ 4.4 custom domain 段落改为"Phase 2b 备胎"；F9 旧描述里的 custom domain 引用清理 |
| P1-b | `payload.sub_stage` 到不了前端 UI | `mappers.ts:115-125` `toJobLogEntries` 丢 payload；`types/jobs.ts:91-99` `JobLogEntry` 无 payload 字段 | **吸收**。F9 从 0.3d 扩到 0.5d，改动 3 处：`types/jobs.ts` 加字段 + `mappers.ts` 映射 + UI 组件消费 |
| P2-1 | `/stream` 口径自相矛盾 | § 5.2.1 表 "Phase 2 不动" vs D35 "条件 opt-in" | **吸收**。§ 5.2.1 表格改为 "条件 opt-in"；全文（D7 / D35 / § 5.2.5 / 验收 / Smoke Test）统一一套说法 |
| P2-2 | Phase 2 "磁盘不再增长"误导 | Phase 2 不启用 TTL 清理 + pipeline 仍写本地 workspace | **吸收**。§ Phase 2 目标改为"出站带宽下降 ≥50% + 为 Phase 5 清理铺路"；"磁盘不再增长"全文删 |

### 18.2 方向性采纳：A + 更严格探针

| 修订项 | v3 | v4 |
|--------|-----|-----|
| R2 域名 | 强制 custom domain | **原生 `<account>.r2.cloudflarestorage.com`**（方向 A）|
| 大陆加速承诺 | "≥3x 下载速度"、"≥30% LCP 下降" | **不承诺加速**（D36）；只承诺"不劣化 + 带宽转嫁 + 回滚快" |
| Phase 0 | 一次三网基线 | **三探针**：① 当前基线 + ② R2 下载稳定性 + ③ R2 上传样本（D37）|
| Phase 3 | 确定 4 天实施 | **可行性关卡**（D38）：α/β/γ 三路径，基于 Phase 0 探针 ③ 决定 |
| 方向 B | 不存在 | **Phase 2b 备胎**（D39）：触发即启用 public bucket + Worker HMAC，仅解决下载 |
| 方向 C | 不存在 | **`force_local=1` 运维开关**（D40）：紧急兜底，不作主方向 |
| MVP 验证范围 | 隐式假设涵盖上传 | **显式分开**：MVP 只验下载+访问；上传由 § 11.7 独立判据 |

### 18.3 CodeX 最短结论（引用）

> "现在选 A；把 B 当下载链路备胎；不要选 C 做主线。"

v4 严格遵循此方向。

### 18.4 仍保留的 v2/v3 未改项

- **Caddy 保留**降级为 127.0.0.1 内网（二审 #8 拒绝理由仍有效）
- **JobRecord 下沉 PG 不并入**（二审 #10 独立专题）
- **事件契约用 `status` + payload.sub_stage**（D31，不扩 event_type；v4 只是扩展 F9 让 payload 到 UI）

---

**v4 初版方案结束声明**（保留历史记录）。

---

## 19. v4 四审吸收（CodeX 四审，2026-04-21，文档残留清理）

CodeX 四审**没有**提新架构问题，只指出 v4 初版在活跃章节还有 4 处 v3 残留未清。全部修复，无工时影响。

| # | 位置 | v4 初版残留 | v4 终版修正 |
|---|------|-----------|-----------|
| P1 | § 11.1 验收（行 2321）| 下载跳转目标写 `files.yourdomain.com` | 改为 `<account>.r2.cloudflarestorage.com`（v4 D33 原生域名） |
| P2-a | D31 决策表（行 150） | 仍写"mappers.ts 基础契约不动，只改 UI"，工时 0.3d | 与 F9 v4 描述对齐：三处联动（types + mappers + UI），工时 0.5d |
| P2-b | § 9.1 监控表（行 2176-2178）| 含 `yt.internal.yourdomain.com/health`、`SG 磁盘使用率` | 改为 `US 磁盘使用率` + cloudflared-us tunnel 连通性 + 出站带宽日监控；独立 YouTube worker 监控项已划除 |
| P2-c | D2 决策表（行 121）+ § 5.3 正文 | 引用不存在的 `youtube/downloader.py` | 改为真实路径 `src/modules/ingestion/youtube/downloader.py` |

**结论**：v4 方向不变（A + 三探针），只是文档口径全局对齐。CodeX 四审原话："v4 方向已经基本站住了 ... 可以（进入实施），但要先把上面 4 处 active section 的残留清掉"。

---

**v4 方案正式结束** — 共 19 节 + 附录 A/B/C。

**实施优先级（v4 最终）**：
1. **Phase 0（~1.5 天）** → 三探针跑完，数据进 § 15.2-15.4
2. **Phase 1 + 2（MVP ~5d）** → 在 Phase 0 探针 ② 数据达标的前提下推进；不达标则启 Phase 2b 备胎（~1.5d）
3. **MVP 放行三硬指标**（§ 11.3）通过 → 进 Phase 3
4. **Phase 3（关卡，工时浮动）** → 由 Phase 0 探针 ③ 和 Phase 3 灰度测试两重判据决定 α/β/γ 路径
5. **Phase 5 运维** + 可选 Phase 6 US→SG 迁移（附录 C）

**v4 基于 CodeX 三审 + 四审（2026-04-21）修订**。R2 + custom domain 能力边界冲突已纠正，MVP 承诺降级为可验证硬指标，Phase 3 上传由独立关卡判据驱动，活跃章节无 v3 口径残留。本文档自此作为实施蓝图。

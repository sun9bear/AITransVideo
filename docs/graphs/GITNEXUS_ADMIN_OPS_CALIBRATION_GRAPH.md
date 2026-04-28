# GitNexus Admin / Ops / Calibration 图

关联总图：`docs/graphs/GITNEXUS_PROJECT_GRAPH.md`

## 1. 范围

这张子图聚焦 Gateway 控制平面的 sidecar 轴线，重点是：

- admin pricing
- credits observability
- S2 monitor
- admin job logs / AI log analysis
- voice probe / calibration
- background tasks
- `job_intercept` 上的下载路由、显示名与镜像更新职责

其中前四项是 admin-only，后三项属于控制平面的运维化侧轴。

## 2. Admin / Ops / Calibration 主图

```mermaid
graph TD
    AdminPricing["AdminPricingPage"] --> AdminPricingAPI["admin pricing API"]
    CreditsMonitor["CreditsMonitorPage"] --> CreditsAPI["/api/admin/credits/*"]
    S2Monitor["S2MonitorPage"] --> S2API["/api/admin/s2-stats*"]
    JobLogs["Admin job log tools"] --> LogAPI["/api/admin/jobs/{job_id}/logs + analysis"]
    VoiceLibrary["Voice library / probe / calibrate"] --> VoiceAPI["/gateway/user-voices/*"]
    ExportTasks["Workspace export tasks"] --> BgTaskAPI["/api/jobs/{job_id}/tasks*"]
    Downloads["Download / rename sidecar"] --> Intercept["job_intercept.py"]

    AdminPricingAPI --> PricingRuntime["pricing_runtime / plan_catalog"]
    CreditsAPI --> CreditsObs["credits_observability.py"]
    S2API --> S2Backend["s2_monitor_api.py"]
    LogAPI --> AdminJobMonitor["admin_job_monitor_api.py"]
    VoiceAPI --> Calibrator["voice_speed_calibrator.py"]
    BgTaskAPI --> BgQueue["background_task_queue.py"]
    Intercept --> DisplayName["display_name / filename / copy mirror"]
    Intercept --> StorageRoute["storage.event_log + backend_router bridge"]

    BgQueue --> MaterialsPack["materials_pack executor"]
    BgQueue --> GenerateVideo["generate_video executor"]
    GenerateVideo --> RenderAsync["video_render_async.py"]
    RenderAsync --> Renderer["VideoRenderer"]

    PricingRuntime --> Truth["PricingPayload / PlanConfig truth"]
    CreditsObs --> Ledger["credits buckets / cost metrics / outliers"]
    S2Backend --> S2Artifacts["S2 artifacts / attempts / summaries"]
    AdminJobMonitor --> JobAPI["Job API logs + result summary"]
    Calibrator --> SpeedCatalog["voice_speed_catalog.py"]
```

## 3. admin pricing

- `frontend-next/src/app/(app)/admin/pricing/page.tsx` 仍然通过：
  `getAdminPricing()`
  `savePricingDraft()`
  `publishPricing()`
- pricing 发布后的运行时读取仍回到：
  `gateway/main.py:lifespan -> get_runtime_pricing() -> PricingPayload`

结论：admin pricing 是受权限控制的发布面，不是独立真源。

## 4. credits observability

- `frontend-next/src/app/(app)/admin/credits-monitor/page.tsx` 继续通过 admin 接口读取：
  `summary`
  `cost-metrics`
  `provider-breakdown`
  `outliers`
- `gateway/credits_observability.py` 仍然是 admin-only read surface

结论：credits monitor 是观测与核对，不是执行面。

## 5. S2 monitor 与 admin logs

### 5.1 S2 monitor

- `frontend-next/src/app/(app)/admin/s2-monitor/page.tsx` 调用：
  `fetchS2Stats(...)`
  `fetchJobDetail(jobId)`
- `gateway/s2_monitor_api.py` 聚合读取：
  `s2_review_result.json`
  `s2_pass1_result.json`
  `s2_pass2_result.json`
  `s2_pass3_result.json`

### 5.2 admin job logs / AI analysis

- `gateway/admin_job_monitor_api.py` 提供：
  `GET /api/admin/jobs/{job_id}/logs`
  以及 AI 日志裁剪与分析输入构造

结论：两者都是“围绕运行产物做诊断”的 sidecar，不属于主 pipeline 内核。

## 6. voice calibration

- `frontend-next/src/lib/api/voiceLibrary.ts` 继续提供：
  `probeVoice()`
  `calibrateVoiceSpeed()`
- `gateway/voice_speed_calibrator.py` 仍然是可复用单声线校准模块
- `src/services/tts/voice_speed_catalog.py` 仍然优先消费已校准 `chars_per_second`

这条闭环没有变，但它已经是稳定的控制平面能力，而不是临时脚本。

## 7. job_intercept 现在承接更多控制面职责

`gateway/job_intercept.py` 当前不只负责常规代理，还承担：

- 下载前的 `_maybe_r2_redirect(job_id, db)`
- 用户友好文件名 `_derive_download_filename(job)`：
  `display_name -> title -> job_id`
- `copy_as_new` 之后对 Gateway DB 的镜像更新

这说明 `job_intercept` 已经成为 Gateway 控制平面上的一个关键编排点。

## 8. 背景导出任务控制面

### 8.1 Gateway API

`gateway/background_task_api.py` 当前提供：

- `POST /api/jobs/{job_id}/tasks`
- `GET /api/jobs/{job_id}/tasks/{task_id}`
- `GET /api/jobs/{job_id}/tasks/latest`
- `GET /api/jobs/{job_id}/tasks/{task_id}/download`

任务类型当前只有两类：

- `materials_pack`
- `generate_video`

### 8.2 Queue 语义

`gateway/background_task_queue.py` 明确说明：

- `params_fingerprint` 用于 dedupe
- `params_fingerprint` 同时用于 latest state restore

这意味着控制面关心的不是“有没有任务”，而是“这个 job 在这组参数下的最新任务身份”。

## 9. 这张图适合回答什么问题

- 哪些面是 admin-only，哪些只是控制平面的 sidecar
- admin pricing、credits monitor、S2 monitor 分别站在什么层级
- background tasks、download routing、display_name 为什么不属于主 pipeline
- voice calibration 为什么应该归到控制平面，而不是塞进主流程

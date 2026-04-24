# Phase 2 最小 R2 下载闭环实施计划

**日期**：2026-04-23
**状态**：T1-T13 代码/文档完成；待生产 R2 `gateway-production` token + 线上三网验收
**上游方案**：[2026-04-21-cloudflare-r2-deployment-plan.md](./2026-04-21-cloudflare-r2-deployment-plan.md) Phase 2
**前置判据**：Phase 0 探针 ② 三网齐活通过 §11.3 D39 判据（commit `6fed91c`）
**指导原则（CodeX 2026-04-23）**：
> "做一个'服务端可切换目标'的下载入口，不要做成'前端知道 R2'"

---

## 1. 目标（Goal）

让 gateway 成为所有 artifact 下载的**唯一真源**，通过 **feature flag 切换** `local | r2` 两个后端，**前端零感知**。

具体行为：

- 旧行为（`AVT_DOWNLOAD_REDIRECT_BACKEND=local`，默认）：保持现在 gateway → Job API 直通返字节，不动任何逻辑
- 新行为（`AVT_DOWNLOAD_REDIRECT_BACKEND=r2`）：gateway 命中下载路径 → 查/建 R2 对象 → 返 302 presigned URL
- 新后端失败（签名失败 / 对象缺失 / R2 超时 / 配置缺失）**自动回落 local**，用户不应感知故障

## 2. 非目标（Non-goals）

显式**不做**的东西（留给后续 Phase）：

- ❌ 不碰上传路径（上传是 Phase 3，需要探针 ③ 数据）
- ❌ 不碰其他 artifact 类型（字幕 / tts_segments_zip 等），**只做 `publish.dubbed_video` 一种**
- ❌ 不做 CF 自定义域名 `files.aitrans.video`（那是移动用户体感差时的后续优化，见 §15.3 观察项）
- ❌ 不改 Job API 的 `_resolve_download_path` 或 disk 布局
- ❌ 不迁移历史任务数据到 R2（新任务才进 R2；旧任务继续走 local）
- ❌ 不做 R2 生命周期管理 / 过期清理（后续独立任务）
- ❌ 不做前端 UI 改动（零前端 diff 是硬约束）

## 3. 架构目标态

```
用户点下载按钮
    │
    ▼
GET /job-api/jobs/{job_id}/download/publish.dubbed_video
    │
    ▼
Gateway (aivideotrans-gateway:8880)
    │
    ├─ _verify_job_ownership(user, job_id)            [既有]
    │
    ├─ if AVT_DOWNLOAD_REDIRECT_BACKEND == 'r2':
    │     │
    │     ├─ 1. HEAD s3://avt-artifacts/jobs/{job_id}/publish.dubbed_video
    │     │    │
    │     │    ├─ 存在 → 直接签 URL, 返 302
    │     │    └─ 不存在 → 上传本地文件 → 签 URL → 返 302
    │     │
    │     ├─ (任何一步失败) → 记 warning 日志 + 事件 → 回落 local
    │     │
    │     └─ emit event: download.r2_redirect{job_id, user_id, key, bytes, signed_expires}
    │
    └─ else ('local', 默认):
          │
          └─ 透传到 Job API → read_bytes → 返字节   [既有行为]
```

**关键设计决定**：

- **URL 形状不变**：沿用 `/job-api/jobs/{id}/download/{key}`。前端代码零改动。
- **Gateway 决定 302**，不是 Job API。Gateway 已经是鉴权边界，storage abstraction 放这里对齐职责。
- **Lazy upload**：用户第一次下载才上传 R2。避免改 pipeline，也避免给从未下载的任务浪费 R2 存储。
  - 并发第一次下载用 `src/services/_file_lock.py` 的文件锁保护（同 job_id 同 key 加锁）
  - HEAD 检查是 idempotent 前置，正常情况下重复请求不会重复上传
- **回落条件（任一触发）**：
  - R2 配置缺失（`R2_ENDPOINT` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` 任一 unset）
  - HEAD 返 5xx
  - 上传超时（> 60s）
  - 签名异常（boto3 client error）

## 4. Feature flag 设计

新增到 `gateway/config.py`（pydantic `BaseSettings`，env_prefix `AVT_`）：

```python
class Settings(BaseSettings):
    # ...既有字段...

    # Phase 2 R2 下载切换（2026-04-23）
    download_redirect_backend: Literal["local", "r2"] = "local"
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_artifacts_bucket: str = "avt-artifacts"
    r2_presigned_expires_s: int = 120  # 2min — 窗口短，泄漏也无意义（用户拍板）
    r2_upload_timeout_s: int = 60
```

**环境变量**：
- `AVT_DOWNLOAD_REDIRECT_BACKEND=local` (默认) / `r2`
- `R2_ENDPOINT` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`（无 `AVT_` 前缀，匹配上游方案约定）
- `AVT_R2_ARTIFACTS_BUCKET=avt-artifacts`
- `AVT_R2_PRESIGNED_EXPIRES_S=120`（**不是 3600**）
- `AVT_R2_UPLOAD_TIMEOUT_S=60`

**启动校验**：`gateway/startup_checks.py` 新增 `validate_r2_backend`——当 `download_redirect_backend=='r2'` 但 `r2_endpoint` 或 secret 为空时，记 `CRITICAL` 日志并**自动降级为 `local`**（不崩溃启动）。

## 5. 文件级任务清单

| # | 文件 | 动作 | 说明 |
|---|------|------|------|
| T1 | `gateway/config.py` | edit | 加 Phase 2 相关字段（§4） |
| T2 | `gateway/startup_checks.py` | edit | 加 `validate_r2_backend` 启动校验 + 降级逻辑 |
| T3 | `gateway/storage/__init__.py` | new | 空包 |
| T4 | `gateway/storage/r2_client.py` | new | boto3 s3v4 client 单例，带超时；HEAD / upload / presign 三个 helper |
| T5 | `gateway/storage/backend_router.py` | new | `resolve_download_target(job_id, key) -> Either[str (302 URL), None (fallback to local)]`。失败时记事件 + 返 None |
| T6 | `gateway/job_intercept.py` | edit | `intercept_job_subresource` 里在 `_verify_job_ownership` 之后、转发 Job API 之前，加 `if path matches /download/publish.dubbed_video and backend=='r2': try resolve_download_target, 命中就 302; 未命中继续透传` |
| T7 | `gateway/events.py` 或 `src/services/jobs/events.py` | edit | 新增 `download.r2_redirect` / `download.r2_fallback` / `download.local` 三种事件，dual-write JSON + PG（对齐现有事件模式） |
| T8 | `docker-compose.yml` | edit | `gateway` service 的 `environment:` 块加新 env passthrough。默认值都是 `local` / 空字符串 |
| T9 | `gateway/requirements.txt` | edit | add `boto3>=1.34` + `botocore>=1.34` |
| T10 | `Dockerfile` (gateway) | edit | pip install step 会自动拉 boto3；若用 multistage build 需确认 deps 进了 runtime image |
| T11 | `tests/test_phase2_download_backend.py` | new | 单元/集成测试（§6） |
| T12 | `docs/plans/2026-04-23-phase2-r2-download-minimal.md` | this | 计划文档（本文件） |
| T13 | `CLAUDE.md` | edit | 加 "Phase 2 下载后端" 小节，记录 feature flag 语义 + R2 关闭时的默认行为 |

**预估工作量**：1.0-1.5 天（代码 ~0.8d + 测试 ~0.3d + 部署验证 ~0.2d）

## 6. 测试与验证

### 6.1 单元/集成测试（`tests/test_phase2_download_backend.py`）

用 `moto` 或手写 boto3 stub 覆盖：

| case | 前置 | 期望行为 |
|------|------|---------|
| backend=local | flag off | 走既有路径，不碰 R2 |
| backend=r2, 对象已在 R2 | HEAD 命中 | 只调 HEAD + presign，不上传；返 302 |
| backend=r2, 对象首次下载 | HEAD 404 | 上传 + presign；返 302 |
| backend=r2, R2_ENDPOINT 缺失 | 配置不完整 | 降级 local（log CRITICAL，不崩） |
| backend=r2, HEAD 超时 | 网络故障 | 回落 local（log warning，事件记录） |
| backend=r2, 上传超时 | 慢网 | 回落 local（事件记录） |
| backend=r2, 对象不存在且本地文件也不存在 | 真 404 | 透传 Job API 的 404（既有行为） |
| backend=r2, job 不属于用户 | 越权 | 403（_verify_job_ownership 先拦截） |
| backend=r2, 非 publish.dubbed_video 的 key | 例如字幕 | 透传走 local（Phase 2 只做这一种 artifact） |
| backend=r2, Express mode job | PUBLIC_RESULT_DOWNLOAD_KEYS 限制 | 既有 whitelist 逻辑继续生效 |

新增回归守卫（进 `tests/test_legacy_cleanup_guards.py` 或独立文件）：

- **前端代码 AST 扫描**：确认 `frontend-next/src/**/*.ts(x)` **不含** 字符串 `r2.cloudflarestorage` / `avt-artifacts` / `R2_` / `presigned`。保证"前端零感知 R2"契约不会在 Phase 3+ 被污染。

### 6.2 线上三网验收（手工）

Phase 2 部署到 US 后：

1. 跑一个真实 Studio 任务到完成（`status=succeeded`）
2. flag 仍为 `local`：点下载按钮，确认没坏任何现有行为
3. flag 翻到 `r2`：同一个任务、同一个下载按钮
4. 从 US 用 curl 模拟 GET 下载——预期返 302 到 r2 URL
5. 发给国内朋友（同一批做探针 ① ② 的朋友）：
   - 电信：打开下载链接，`curl -v` 查看 302 是否正确跟随、最终到 R2 原生域名
   - 联通：同上
   - 移动：同上
   - 每个运营商记录：HTTP 302 到哪、最终下载完整文件大小/速度
6. 验收判据（对齐 §11.3 但放宽）：
   - 三网 302 都成功跳转
   - 三网最终下载成功率 100%
   - 三网平均速度不差于探针 ② 的数据 20% 以内
7. 失败场景演练：
   - 故意把 bucket 里的对象删掉 → gateway 应 HEAD 404 → 触发 lazy upload → 下载仍成功
   - 故意把 `R2_ENDPOINT` 改成错的 → gateway 应降级 local → 下载仍成功
   - 故意把 `R2_SECRET_ACCESS_KEY` 改成错的 → gateway 应签名异常 → 回落 local → 下载仍成功

### 6.3 监控 & 审计

- Phase 2 上线后 7 天内观察 gateway 日志：`download.r2_fallback` 事件 < 1%（如果更高，说明回落路径在被意外触发）
- 每次 R2 302 事件记录：`job_id` / `user_id` / `key` / `file_size_bytes` / `presigned_expires_at` / `r2_head_latency_ms` / `r2_upload_latency_ms (nullable)`
- 这些事件也支撑后续 Phase 2b 决策：如移动用户体感差，可以基于事件数据分析"哪些用户/ISP 的 R2 下载失败多"

## 7. 部署与回滚

### 7.1 部署

1. 本地代码 + 测试完成，`pytest tests/test_phase2_download_backend.py` 全绿
2. `docker compose build gateway`（新 boto3 进镜像）
3. `docker-compose.yml` 默认 `AVT_DOWNLOAD_REDIRECT_BACKEND=local`——**先部署代码但不开 flag**
4. 通过 `D:/daili/scripts/Deploy-US-Via-154.cmd` 发代码到 US
5. US 上 `docker compose up -d --force-recreate gateway`，确认 gateway 健康、现有下载不受影响
6. **在 `/opt/aivideotrans/config/.env` 切 flag：`AVT_DOWNLOAD_REDIRECT_BACKEND=r2` + 配 R2 credentials**
7. `docker compose up -d --force-recreate gateway`（restart 不读 env_file，必须 recreate，见 MEMORY.md `feedback_tls_internal_trap.md`）
8. 执行 §6.2 线上三网验收

### 7.2 回滚

触发条件：
- 三网验收任一失败
- 上线 24h 内 `download.r2_fallback` 比例 > 5%
- 任意用户报告下载无法打开

回滚步骤（每步独立可用，按严重程度递增）：

1. **配置回滚**（<1min）：改 `.env` 的 `AVT_DOWNLOAD_REDIRECT_BACKEND=local`，`docker compose up -d --force-recreate gateway`。
2. **代码回滚**（<5min）：`git revert` Phase 2 的 merge commit，重新 build + deploy。

## 8. 风险与缓解

| 风险 | 可能性 | 影响 | 缓解 |
|------|--------|------|------|
| R2 首次上传超时（大视频 + 慢出站） | 中 | 首次用户等待变长 | Timeout 60s + 回落 local |
| 并发首次下载引发重复上传 | 低 | R2 多计一次 PUT 费用 | `_file_lock.py` per-key 锁 |
| presigned URL 泄漏 | 低 | URL 可复用 1h | TTL 默认 1h；短时窗内容可接受；不记录在服务日志的 body |
| 移动用户 R2 直连慢（探针 ② 2 MB/s） | 已知 | 首次加载感受 | 与现有 local 速度对比小（~4s/100MB 差异），不阻塞放行；长期看 Phase 2b CF 域名 |
| gateway boto3 增加冷启动时间 | 低 | 容器启动慢几秒 | 接受 |
| R2 账户 / token 过期导致全量回落 | 中 | 所有下载走 local（但仍可用） | 启动校验 + 定期 canary |

## 9. 出口契约（Phase 2 完成的定义）

**代码 / 测试 / 文档闭环（2026-04-24 已完成）**：

- [x] T1 `gateway/config.py` — 加 `download_redirect_backend` / `r2_*` / `jobs_dir` 字段
- [x] T2 `gateway/startup_checks.py` — `validate_r2_backend` + lifespan 写回 settings
- [x] T3 `gateway/storage/__init__.py` — 空包
- [x] T4 `gateway/storage/r2_client.py` — boto3 单例、HEAD / upload / presign + ASCII filename fallback
- [x] T5 `gateway/storage/backend_router.py` — `resolve_download_target` 单一决策点，所有 R2 异常返 None 回落 local
- [x] T6 `gateway/job_intercept.py` — 只在 `GET /download/publish.dubbed_video` 分支挂 R2 302
- [x] T7 事件打点 — `src/services/jobs/events.py` 新增三种 event type（`download.redirect.r2` / `download.fallback.local` / `download.local.direct`），gateway 侧直接写 JSONL（`{jobs_dir}/{job_id}.events.jsonl`），**不 import `services.jobs.events`** 避免 pydub 传染
- [x] T8 `docker-compose.yml` — gateway service 加 jobs 目录 bind mount (rw) + 8 个 env passthrough（默认 `local` + 空 credentials，零行为变化）
- [x] T9 `gateway/requirements.txt` — `boto3>=1.34,<2` + `botocore>=1.34,<2`
- [x] T10 `gateway/Dockerfile` — 走既有 `pip install -r requirements.txt`，无需改动
- [x] T11 `tests/test_phase2_download_backend.py` — 14 个 test function 覆盖 §6.1 全部 10 个 case（FakeR2 monkeypatch + `sys.modules` scrub + AST-level 前端扫描）
- [x] T12 本文件 — 阶段性出口契约更新（2026-04-24）
- [x] T13 `CLAUDE.md` — "Phase 2 下载后端" 小节，记录 feature flag 语义 + 默认 local + R2 异常回落契约

**测试状态**：

```
pytest -q tests/test_phase2_download_backend.py tests/test_legacy_cleanup_guards.py
24 passed in 2.35s
```

**生产验收待办**（代码已就绪，等运维窗口）：

- [ ] 申请 `gateway-production` R2 token（不复用探针 token，scope = `avt-artifacts/jobs/*`，权限 = `GetObject / PutObject / HeadObject`）
- [ ] US 部署：先部署代码保持 `AVT_DOWNLOAD_REDIRECT_BACKEND=local`，确认现有下载零变化
- [ ] `.env` 翻 flag 到 `r2` + 写入 credentials，`docker compose up -d --force-recreate gateway`
- [ ] §6.2 三网（电信 / 联通 / 移动）手工验收 302 + 下载成功率 100% + 速度不差于探针 ② 的 20%
- [ ] 失败演练：bucket 对象删除 / 错误 endpoint / 错误 secret 三项全部确认自动回落 local
- [ ] 稳定运行 7 天无 ERROR 级 fallback 事件（`download.fallback.local` < 1%）
- [ ] §15.3 补"Phase 2 真实流量数据"对照探针 ② 预签名数据

## 10. 锁定决议（2026-04-23 用户拍板）

| # | 决议 | 理由 |
|---|------|------|
| 1 | 首期 artifact = `publish.dubbed_video` | 最大 + 最常下，收益最高 |
| 2 | R2 key = `jobs/{job_id}/publish.dubbed_video{suffix}`，`suffix` 从本地文件实际后缀取（`.mp4` / `.mov` / 空）| Dashboard 可视 + 兼容未来不同容器格式 |
| 3 | 凭证：**新建 `gateway-production` 专用 token**，不复用探针 token | 最小权限 `GetObject / PutObject / HeadObject`，scope 限定 `avt-artifacts` bucket 的 `jobs/*` prefix；探针 token 继续 dev 用 |
| 4 | 审计事件：写入 `JobEvent` **JSONL schema/event stream**（`{jobs_dir}/{job_id}.events.jsonl`），不是独立 DB 表。事件类型：`download.redirect.r2` / `download.fallback.local` / `download.local.direct`，与 `src/services/jobs/events.py` 的 `SUPPORTED_EVENT_TYPES` 对齐。**注意：这三个都是"路由决策事件"，在 downstream 响应产出之前就已写入——不是"用户成功下载"的证据**（详见 §11.1 / §11.7）| 省 schema、查询一致、复用 `JobStore.load_events` 读路径 |
| 5 | 验收样本：先用现成已完成真实任务；稳定样本缺失再补一个固定测试任务 | 省一次真实付费 pipeline |

### 额外执行约束（避免返工）

- **Presigned URL TTL = 120s**（不是之前草案写的 3600s）——窗口短，泄漏也无意义
- **Presign 时必须带 `ResponseContentDisposition`**：保证下载文件名稳定为 `{job_friendly_name}.mp4`，不暴露内部 R2 key
- **任何 R2 异常必须自动回退 local**，用户无感知；日志 + `download.fallback.local` 事件打点用于事后定位

这些约束同步落到 §4 config + §6 测试 case + §11 事件表设计。

## 11. 实施后的实际工程决策记录（2026-04-24 补）

实施阶段遇到的契约级要点，超出原草案的范围，记在这里方便 Phase 3 接手时不踩坑：

### 11.1 pydub 传染 → gateway 手写 JSONL append（事件写入路径）

`src/services/jobs/events.py` 本身只是纯 dataclass，但它位于 `services.jobs.__init__.py` 的 import graph 下游——`services.jobs` 一旦 import，`process_runner` → `modules.output` → pydub 整条链就会连带加载。Gateway 容器不装 pydub（见 `display_name_orchestrator.py:30-35` 注释）。

**决策**：Gateway 侧 **不 import `JobEvent` / `JobStore`**，把事件写入抽成独立模块 `gateway/storage/event_log.py::emit_download_event`（纯 stdlib，无 fastapi / pydub 依赖），直接手写与 `JobEvent.to_dict()` schema 一致的 dict → `json.dumps` → append 到 `{jobs_dir}/{job_id}.events.jsonl`。

- `gateway/job_intercept.py._emit_download_event` 是一层极薄 delegator，只 re-export 这个 helper——保留历史调用点的可读性，实际逻辑在 `event_log.py` 里。
- 测试直接 import `storage.event_log.emit_download_event` 跑真 helper（见 `tests/test_phase2_download_backend.py::test_emit_download_event_writes_*`）。**不要** 在测试里重写 record shape——CodeX 2026-04-24 review 指出过：重写 shape 会导致生产 append 路径坏掉而测试仍绿。
- 任何未来扩展 download-related event type 的改动必须同时更新：
  1. `src/services/jobs/events.py` 的 `SUPPORTED_EVENT_TYPES` 集合
  2. `gateway/storage/event_log.py::_DOWNLOAD_EVENT_TYPES` 集合
  3. 回归守卫 `tests/test_phase2_download_backend.py::test_emit_download_event_supported_types_in_sync_with_jobs_events` 会在任一侧漏改时 red。

### 11.2 Gateway jobs/ 目录必须 bind mount

`settings.jobs_dir` 默认 `/opt/aivideotrans/app/jobs`，但 gateway 容器镜像里这个路径不存在。T8 在 docker-compose.yml 为 gateway service 加了与 app service 相同的 `${AIVIDEOTRANS_ROOT}/data/jobs:/opt/aivideotrans/app/jobs` bind mount，确保 download event JSONL 能落地到宿主机、与 Job API 看同一份 store。**不这么做的后果**：events 静默写失败，监控大盘查询不到任何 `download.*` 事件。

### 11.3 R2 key 带 suffix

`r2_key_for(job_id, "publish.dubbed_video", local_path=...)` 返回 `jobs/{job_id}/publish.dubbed_video.mp4`（从 `local_path.suffix` 取，若本地文件是 `.mov` 则 key 尾巴也是 `.mov`）。Dashboard 可视性 + 未来多容器格式的正交性是设计目标，不要改成无后缀。

### 11.4 Lock 路径必须在 jobs 目录下、不在 artifact 目录下

`_lock_path_for_key` 指向 `{settings.jobs_dir}/_r2_upload_locks/{sha256(key)}`，不是 artifact 所在的工程目录。理由：lazy upload 期间如果 lock 文件混在 artifact 目录里，后续 `editing/commit` 的 `overwrite` / `copy_as_new` 搬运时可能被误扫进来。回归守卫 `tests/test_phase2_download_backend.py::test_lock_path_not_in_artifact_dir` 固化。

### 11.5 前端零感知 R2 的 AST 扫描

`tests/test_phase2_download_backend.py::test_frontend_has_no_r2_leakage` 递归扫 `frontend-next/src/**/*.{ts,tsx,js,jsx,mjs}`，禁止出现 `r2.cloudflarestorage` / `avt-artifacts` / `X-Amz-Signature` / `X-Amz-Expires` / `X-Amz-Algorithm` / `AWS4-HMAC-SHA256`。Phase 3+（上传路径）接手时这个守卫要保留——用户永远不应在前端代码里看到 S3 / R2 的影子。

### 11.6 gateway 业务模块 hardcoded URL 规则继续生效

Phase 2 新增的 `gateway/storage/*.py` 没有写 `http://localhost:8877` / `http://127.0.0.1:8877`——`backend_router.py` 只接本地 Path，`r2_client.py` 只连 R2 endpoint。`tests/test_legacy_cleanup_guards.py::test_gateway_business_modules_no_hardcoded_job_api_url` 继续全绿。

---

以上 6 点是 Phase 3 上传路径的直接前置契约。任何回退（删 jobs bind mount / 改 event emit 走 import / 把 lock 放回 artifact 目录）都会被现有测试立即红。

### 11.7 事件打点语义：路由决策 ≠ 下载成功（CodeX 2026-04-24 review P3）

这三个事件打点的时机是 **routing decision time**，不是 **download-succeeded time**：

| 事件 | 写入时机 | 之后发生什么 |
|------|---------|-------------|
| `download.redirect.r2` | `resolve_download_target` 返 URL → 写事件 → 返 `RedirectResponse(302)` | 浏览器自行跟 302 到 R2；**gateway 不知道**客户端是否真的拿到了字节 |
| `download.fallback.local` | R2 路径抛异常 → 写事件 → 调 `proxy_request` 走 local | proxy 可能成功也可能再失败；事件 **不表达** local 最终成功 |
| `download.local.direct` | backend=local 默认分支，写事件 → 调 `proxy_request` | 同上，事件在 proxy 之前写入 |

**为什么不移到 proxy 之后**：
- 重定向路径：`RedirectResponse` 一旦返出 ASGI，gateway 就把控制权交给客户端，没有后续 hook 能知道 302 是否被跟随 / R2 下载是否成功。要知道 "用户下到没" 需要在 R2 侧装 access log 聚合（Phase 2 之外）。
- Local 透传路径：`proxy_request` 是流式返回字节的，要观测 "流到最后没" 需要 ASGI response middleware，超出 Phase 2 范围。

**对 rollout 仪表盘 / 告警口径的约束**：
- **不要** 把 `download.redirect.r2` 计数当 "R2 下载成功数"。它只是 "路由选择了 R2"。
- **不要** 把 `download.fallback.local` 当 "R2 故障 + local 成功"。它只是 "路由决策切到 local" ——local 可能紧接着 404 / 5xx。
- 正确解读：
  - `download.redirect.r2` 占比高 + 低 fallback → R2 路径健康
  - `download.fallback.local` 占比骤升 → R2 HEAD / upload / presign 正在失败（用 gateway WARNING 日志定位）
  - 真正的 "下载失败率" 要从 access log / upstream 4xx/5xx 单独算，不能从这三个事件推

生产验收（§6.2）的 "下载成功率 100%" 是**人工 curl 验证**，不是基于这三个事件的指标。

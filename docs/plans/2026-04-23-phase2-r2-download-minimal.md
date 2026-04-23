# Phase 2 最小 R2 下载闭环实施计划

**日期**：2026-04-23
**状态**：草案待审
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
    r2_presigned_expires_s: int = 3600  # 1h
    r2_upload_timeout_s: int = 60
```

**环境变量**：
- `AVT_DOWNLOAD_REDIRECT_BACKEND=local` (默认) / `r2`
- `R2_ENDPOINT` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`（无 `AVT_` 前缀，匹配上游方案约定）
- `AVT_R2_ARTIFACTS_BUCKET=avt-artifacts`
- `AVT_R2_PRESIGNED_EXPIRES_S=3600`
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

- [ ] T1-T10 代码全部合并到 main
- [ ] T11 测试全绿
- [ ] §6.1 全部 case 覆盖
- [ ] §6.2 三网验收 PASS
- [ ] Gateway US 上 flag=r2 稳定运行 7 天无 ERROR 级 fallback 事件
- [ ] §15.3 加一行"Phase 2 真实流量数据"对照探针 ② 预签名数据
- [ ] CLAUDE.md 加 "Phase 2 下载后端" 小节
- [ ] 失败演练（bucket 对象删除 / 错误 endpoint / 错误 secret）三项全部确认回落

## 10. 开放问题（需你拍板）

1. **首 artifact 选择确认**：我选 `publish.dubbed_video`（最大、最常下）——OK 吗？还是你想先切 `editor.dubbed_audio_complete`（次大）？
2. **R2 key 命名**：建议 `jobs/{job_id}/{artifact_key}` → 实际为 `jobs/abc123/publish.dubbed_video`（没有后缀，因为 artifact_key 本身就带）。是否改成 `jobs/{job_id}/publish.dubbed_video.mp4`（带扩展名以便 R2 dashboard 可视）？
3. **R2 credentials 来源**：用现在 probe ②/③ 的那把（account `ee9757...`）还是建一把新的 "gateway-production" 专用的 token？我建议**生产环境新建一把**，探针那把保留 dev 用。
4. **事件存储**：新增 3 个 download 事件写到哪？
   - 选项 A：`gateway/events.py` 独立表（新 table `download_events`）
   - 选项 B：复用现有 `JobEvent` 表（加 `event_type IN ('download.r2_redirect', ...)`）
   - B 更省 schema，查询一致。我倾向 B，除非你想给下载事件单独的数据保留策略。
5. **线上三网验收的样本任务**：需要你或某朋友事先跑一个完整翻译任务（花钱 LLM + TTS），才有 final_video 可以真实下载。要不要复用现有已完成任务？

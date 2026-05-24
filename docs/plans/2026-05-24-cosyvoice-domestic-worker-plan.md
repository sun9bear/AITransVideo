# CosyVoice 国内中转 / Worker 方案

日期：2026-05-24

## 目标

使用现有阿里云武汉 ECS 作为国内 `mainland voice worker`，承接 CosyVoice 国内版声音复刻和 TTS 合成，让美国生产主机不需要直接访问国内 DashScope/CosyVoice 端点，也不需要持有国内 DashScope API Key。

本方案是阶段化方案。第一目标是 POC 和管理员灰度，不是立刻替换当前 MiniMax 克隆主路径。

## 结论摘要

- 可以使用武汉 ECS 做 CosyVoice 国内 worker，但必须先验证阿里云账号是否有 CosyVoice 国内声音复刻权限。
- Worker 应该暴露窄 API，不做透明代理，不让美国主机认识 DashScope 国内接口。
- CosyVoice 克隆音色必须保存 `target_model`，后续 TTS 必须用同一模型。
- 5 Mbps 公网带宽是 Phase 3 的真实瓶颈，不只是流量费问题；Phase 0 必须实测 US 到武汉下载速度。
- 当前先做 Phase -1、Phase 0、Phase 1；不要向普通用户开放。

## 已知环境

### 武汉 ECS

- 公网 IP：`8.148.83.128`
- 地域：华中 1，武汉本地地域
- 系统：Ubuntu 24.04 64 位
- 规格：`ecs.e-c1m2.large`，2 vCPU，4 GiB 内存
- 系统盘：约 49 GiB，剩余约 36 GiB
- 数据盘：`/data` 约 196 GiB，剩余约 186 GiB
- 公网带宽：峰值 5 Mbps
- 带宽计费方式：按使用流量
- 控制台显示公网出网流量价格：`0.800 元/GB`
- 到期时间：2026-06-02 23:59:59，需要续费后才能作为生产依赖

### 当前进程和端口

已有服务：

- Docker：`atlas-app`、`atlas-postgres`
- Hermes dashboard：`127.0.0.1:9119`
- Hermes web UI：`127.0.0.1:8787`
- xray：`127.0.0.1:18080`、`127.0.0.1:18081`
- Nginx：公网 `80`，当前代理到 `127.0.0.1:8790`
- SSH：公网 `22`

避免占用：

- `8787`
- `8790`
- `9119`
- `5432`
- `18080`
- `18081`

建议 worker 监听：

- 本机端口：`127.0.0.1:8791`
- Nginx 路径：`/internal/voice-clone/cosyvoice/`
- 部署目录：`/opt/aivideotrans-mainland-worker/current`
- 数据目录：`/data/aivideotrans-mainland-worker`

目录和路由命名故意不写死 `cosyvoice-worker`，为未来豆包 ICL 2.0 或其他国内限定 provider 留扩展空间。

### 已测连通性

- 武汉 ECS 能访问 `https://dashscope.aliyuncs.com`。
- 美国生产主机能访问 `8.148.83.128:80` 和 `8.148.83.128:22`。
- `443` 未开放。
- `8789`、`8791` 等自定义公网端口未开放。
- 美国生产主机 IPv4 出口：`5.78.122.220`。

## 官方成本依据

阿里云 ECS 公网带宽只收公网出网流量，公网入网免费。

参考：

- ECS 公网带宽计费：https://help.aliyun.com/zh/ecs/public-bandwidth
- ECS 网络带宽机制：https://help.aliyun.com/zh/ecs/user-guide/network-bandwidth/
- CDT 公网流量价格：https://help.aliyun.com/zh/cdt/internet-data-transfers/

对本方案而言，主要计费流量是：

- 武汉 worker 把生成后的音频包传回美国主机。

通常不计费或很小：

- 美国主机把任务、文本、样本传入武汉 ECS：入网免费。
- 武汉 ECS 从 DashScope/OSS 下载结果：对 ECS 是入网，免费。
- API JSON 请求体：体积很小。

## 5 Mbps 带宽瓶颈

5 Mbps 不是费用问题，而是吞吐瓶颈。

换算：

- 5 Mbps = 0.625 MB/s
- 约 37.5 MB/min

粗略估算：

- 一个 10 分钟视频的 WAV 结果包可能是 20-30 MB。
- 顺序回传可能需要 30-50 秒。
- 两个任务并发回传会互相挤占带宽。
- 按使用流量模式下，5 Mbps 是出网峰值限制，不是稳定吞吐承诺。

Phase 0 必须实测：

- 从美国主机下载武汉 worker 测试文件，至少测 10 MB、50 MB、100 MB。
- 记录平均速度、P95 耗时、失败率。
- 如果单任务 30 MB 回传超过 60 秒，Phase 3 必须启用 OSS artifact 路径或升级带宽。

### Artifact 回传方案

默认路径：

```text
武汉 worker -> Nginx artifact URL -> 美国主机下载
```

适合：

- POC
- 小批量
- 单任务低并发

备选路径：

```text
武汉 worker -> 阿里云 OSS 临时对象 -> 美国主机用 signed URL 下载
```

优势：

- 回传不占 ECS 5 Mbps 出网瓶颈。
- OSS 带宽和可用性更适合大文件下载。

代价：

- OSS 公网出网也会产生费用。
- 需要额外管理 bucket、生命周期、签名 URL、权限和审计。

决策规则：

- Phase 0 下载实测良好，先用 ECS Nginx artifact。
- Phase 3 单任务回传超过 60 秒，切到 OSS artifact。
- 如果灰度阶段月出网持续超过 50 GB，再评估固定带宽或 OSS/CDT 成本。

## CosyVoice 产品约束

- CosyVoice 声音复刻能力依赖国内地域能力。
- 海外 DashScope endpoint 不足以覆盖 clone/design path。
- 克隆音色与 `target_model` 绑定，后续合成必须使用兼容模型。
- 美国主机不能假设自己可以直接用 CosyVoice 克隆音色合成。

参考：

- CosyVoice clone API：https://www.alibabacloud.com/help/en/model-studio/cosyvoice-clone-design-api
- CosyVoice SDK / 计费参考：https://help.aliyun.com/zh/model-studio/cosyvoice-ios-sdk

## 为什么选择窄 API Worker

不采用透明 HTTP 代理：

- 代理面过宽，容易把武汉 ECS 变成 DashScope 通用出口。
- API Key、请求体和 provider 细节会穿透到美国主机。
- 难以做按 job / speaker 的审计、重试和幂等。

不采用 WireGuard 隧道让美国主机直连国内 DashScope：

- 美国主机会直接认识国内 provider endpoint，破坏部署边界。
- 失败路径、付费调用和密钥管理更难收敛。
- 未来引入其他国内 provider 时会继续扩散复杂度。

不在武汉部署完整 pipeline：

- 会把 SemanticBlock、DSP 对齐、字幕 retiming、剪映草稿生成等主流程分裂到另一台机器。
- 破坏当前架构不变量：TTS 单元仍应由主 pipeline 决定，alignment 仍应 DSP-first，draft 输出仍由主流程生成。

因此 worker 只做：

- clone
- TTS batch
- artifact package
- provider deletion
- provider audit metadata

不做：

- 翻译
- 文本改写
- 说话人决策
- subtitle retiming
- DSP alignment
- Jianying draft generation

## 总体架构

```mermaid
flowchart LR
  subgraph US["美国生产主机"]
    GW["Gateway：权益 / 定价 / 策略"]
    API["Job API / Pipeline"]
    STORE["Job artifacts / review_state"]
  end

  subgraph CN["武汉 ECS"]
    NGINX["Nginx /internal/voice-clone/cosyvoice"]
    WORKER["Mainland Voice Worker"]
    TEMP["本地临时目录"]
    OSS["可选：阿里云 OSS signed artifact"]
  end

  subgraph ALI["阿里云国内 API"]
    DS["DashScope CosyVoice Clone + TTS"]
  end

  GW --> API
  API -->|"HMAC 内部请求"| NGINX
  NGINX --> WORKER
  WORKER --> TEMP
  WORKER --> OSS
  WORKER --> DS
  WORKER -->|"manifest + audio package"| API
  API --> STORE
```

Phase 0/1 mock-only 简图：

```mermaid
flowchart LR
  API["US Job API"]
  NGINX["武汉 Nginx"]
  WORKER["Mock Worker"]
  API -->|"HMAC /healthz + mock batch"| NGINX
  NGINX --> WORKER
  WORKER -->|"fake voice_id + silent WAV"| API
```

## Worker API

所有接口都是内部接口。美国主机每次请求必须签名。

通用请求头：

- `X-AVT-Key-Id`：HMAC key id
- `X-AVT-Timestamp`：Unix seconds
- `X-AVT-Nonce`：随机 UUID
- `X-AVT-Signature`：HMAC-SHA256
- `X-AVT-Job-Id`：job id

签名内容：

```text
method + "\n" + path + "\n" + timestamp + "\n" + nonce + "\n" + key_id + "\n" + sha256(body)
```

Worker 必须拒绝：

- 未知或已过期 `X-AVT-Key-Id`
- 时间偏移超过 300 秒
- 15 分钟内重复 nonce
- body 超过上限
- 未知调用方
- 缺少 provider 配置
- 未启用对应 provider

Worker 端维护 `{key_id: hmac_secret}` 表。旧 key 标记 `deprecated_at` 后允许短期并存，超过轮换窗口自动剔除。

### `GET /healthz`

健康检查，不触发付费 API。

响应：

```json
{
  "ok": true,
  "worker": "aivideotrans-mainland-worker",
  "region": "cn-wuhan",
  "providers": {
    "cosyvoice": {
      "configured": true,
      "mode": "mock"
    }
  }
}
```

### `POST /cosyvoice/clone`

用途：根据用户显式确认的样本创建 CosyVoice 自定义音色。

请求：

```json
{
  "job_id": "job_xxx",
  "user_id": "user_xxx",
  "speaker_id": "speaker_a",
  "speaker_name": "speaker_a",
  "target_model": "cosyvoice-v3.5-flash",
  "sample": {
    "kind": "download_url",
    "url": "https://us-host/internal/signed-artifact/...",
    "sha256": "..."
  },
  "source_segments": [12, 18, 19],
  "consent": {
    "voice_clone_confirmed": true,
    "confirmed_at": "2026-05-24T00:00:00Z"
  }
}
```

响应：

```json
{
  "ok": true,
  "voice_id": "cosyvoice_custom_xxx",
  "provider": "cosyvoice_voice_clone",
  "tts_provider": "cosyvoice",
  "target_model": "cosyvoice-v3.5-flash",
  "region_constraint": "mainland_only",
  "requires_worker": true,
  "platform": "dashscope_mainland",
  "sample_sha256": "...",
  "created_at": "2026-05-24T00:00:00Z"
}
```

说明：

- Worker 下载用户授权样本，必要时上传到阿里云 OSS 生成短期 signed URL。
- Clone 成功或失败后，默认删除原始样本。
- 不允许自动 fallback 到 MiniMax clone。

### `POST /cosyvoice/synthesize-batch`

用途：合成一个 job 或一个 speaker 的 CosyVoice 片段。

这个 endpoint 必须支持 `len(segments) == 1`。Studio post-edit 的单段 `regenerate-tts` 也走同一个 endpoint，不另开 `/synthesize-one`。

请求：

```json
{
  "job_id": "job_xxx",
  "target_model": "cosyvoice-v3.5-flash",
  "audio_format": "wav",
  "segments": [
    {
      "segment_id": 1,
      "speaker_id": "speaker_a",
      "voice_id": "cosyvoice_custom_xxx",
      "text": "需要合成的中文文本",
      "speech_rate": 1.0,
      "target_duration_ms": 3200,
      "text_hash": "..."
    }
  ]
}
```

响应：

```json
{
  "ok": true,
  "job_id": "job_xxx",
  "target_model": "cosyvoice-v3.5-flash",
  "segments": [
    {
      "segment_id": 1,
      "speaker_id": "speaker_a",
      "voice_id": "cosyvoice_custom_xxx",
      "audio_path": "segments/segment_001_speaker_a.wav",
      "duration_ms": 3180,
      "billed_chars": 24,
      "sha256": "..."
    }
  ],
  "package": {
    "kind": "zip",
    "download_url": "http://8.148.83.128/internal/voice-clone/cosyvoice/artifacts/...",
    "sha256": "...",
    "expires_at": "2026-05-24T01:00:00Z"
  }
}
```

小批量 POC 可以返回 base64；生产路径优先 artifact package。

`text_hash` 规范：

- `text_hash = sha256(text.encode("utf-8")).hexdigest()`
- 不做 Unicode normalize。
- 大小写敏感。
- Worker 必须根据收到的 `text` 重新计算；如果请求里带了 `text_hash`，两者不一致则拒绝。

### `DELETE /cosyvoice/voices/{voice_id}`

用途：删除 CosyVoice 自定义音色。

请求：

```json
{
  "job_id": "job_xxx",
  "user_id": "user_xxx",
  "reason": "user_deleted"
}
```

响应：

```json
{
  "ok": true,
  "voice_id": "cosyvoice_custom_xxx",
  "deleted_at": "2026-05-24T00:00:00Z"
}
```

删除失败时要写 retryable tombstone，不能静默丢失。

## 分发决策字段

必须在 voice metadata 中显式保存 worker 相关字段。

推荐字段：

```json
{
  "provider": "cosyvoice_voice_clone",
  "tts_provider": "cosyvoice",
  "platform": "dashscope_mainland",
  "target_model": "cosyvoice-v3.5-flash",
  "region_constraint": "mainland_only",
  "requires_worker": true,
  "worker_provider": "cosyvoice",
  "worker_region": "cn-wuhan"
}
```

字段语义：

- `region_constraint`: `"overseas_ok"` 或 `"mainland_only"`
- `requires_worker`: 由 `region_constraint == "mainland_only"` 派生，也可落库用于快速判断
- `target_model`: clone 和 TTS 的兼容模型
- `worker_provider`：当前是 `cosyvoice`，未来可扩展 `doubao`
- `worker_provider` / `worker_region` 目前是 metadata；未来多 provider / 多 worker 部署时用于 dispatch。

运行时 fork 规则：

```python
if voice.requires_worker:
    use_mainland_worker_client()
else:
    use_direct_provider()
```

不要只看 `provider == "cosyvoice_voice_clone"`。未来可能存在非克隆但 mainland-only 的音色，也可能有其他 provider 的国内限定资源。

## 与现有项目集成

### Gateway

Gateway 仍是计划、价格、试用、权益的唯一事实来源。

新增管理员配置：

- `mainland_voice_worker_enabled`
- `mainland_voice_worker_url`
- `mainland_voice_worker_hmac_key_id`
- `cosyvoice_clone_worker_enabled`
- `cosyvoice_clone_target_model`
- `cosyvoice_clone_allowed_plan_codes`
- `cosyvoice_clone_max_minutes_per_job`
- `cosyvoice_clone_max_speakers_per_job`

开关语义：

- `enabled`：管理员人工开关，持久化在 admin settings。
- `available`：运行时自动探测状态，由 worker 心跳和错误率派生。
- 生效条件：`enabled && available`。

管理员只修改 `enabled`。`available` 不应由管理员手动改写。

定价配置：

- CosyVoice clone 与 TTS 扣点仍从 Gateway runtime pricing 下发。
- 前端不能硬编码 provider 价格。
- CosyVoice clone 即使 provider 创建免费，也不能在产品上直接当作免费能力暴露。

### 用户音色库

CosyVoice clone 应与 MiniMax clone 分开记录。

推荐：

- 独立 quota 字段，例如 `cosyvoice_clone_voices`。
- 不占用现有 MiniMax clone quota。

理由：

- MiniMax 有音色创建成本。
- CosyVoice 当前创建自定义音色免费，但 TTS 按字符收费。
- 两者成本结构不同，混用 quota 会干扰用户行为和成本分析。

示例记录：

```json
{
  "provider": "cosyvoice_voice_clone",
  "tts_provider": "cosyvoice",
  "platform": "dashscope_mainland",
  "voice_id": "cosyvoice_custom_xxx",
  "target_model": "cosyvoice-v3.5-flash",
  "region_constraint": "mainland_only",
  "requires_worker": true,
  "worker_region": "cn-wuhan",
  "source_speaker_id": "speaker_a",
  "source_job_id": "job_xxx",
  "clone_sample_seconds": 18.2,
  "clone_sample_segment_ids": [12, 18, 19]
}
```

### Voice Selection UI

当前 Studio voice selection 已有 provider tab，且通过 `supports_clone` 控制是否显示克隆按钮。

后端准备好之前：

- CosyVoice `supports_clone` 继续为 false。

后端准备好之后：

- 只有当 `cosyvoice_clone_worker_enabled == true` 且用户权益允许时，CosyVoice 才返回 `supports_clone=true`。
- UI 文案必须说明这是国内 worker 复刻能力，用户需要显式确认。
- 不允许自动触发付费 clone。
- CosyVoice Tab 内是否新增“我的克隆音色”分组，留到 Phase 4 前单独做 UI plan，本方案不展开。

### Pipeline

保持现有架构不变量：

- TTS 单元是 SemanticBlock，不是 subtitle line。
- Alignment 仍 DSP-first。
- Subtitle retiming 仍是确定性数学逻辑。
- 主产物仍是 Jianying draft。

Pipeline 行为：

- 已选 CosyVoice cloned voice 且 `requires_worker=true` 时，TTS 走 mainland worker。
- 其他 CosyVoice 公共音色继续走现有 provider，除非 metadata 表示 mainland-only。
- Worker 返回 WAV 和 duration，主 pipeline 继续创建标准 `TTSResult`。
- 后续 alignment、retry、retiming、draft generation 不放到 worker。

### Studio Post-Edit / Regenerate TTS

单段重配音也必须兼容 worker。

规则：

- `POST /job-api/jobs/{id}/segments/{sid}/regenerate-tts` 判断该 segment 的 voice metadata。
- 如果 `requires_worker=true`，调用 `/cosyvoice/synthesize-batch`，其中 `segments` 只有 1 个元素。
- 不新增 `/synthesize-one`，避免两套重试和审计路径。
- 不允许 worker 不可用时静默切到 MiniMax 或其他 provider。

## Secret Management

武汉 ECS 是 DashScope 国内 API Key 的唯一放置点。

### 注入方式

推荐二选一：

- Docker Compose：`.env` 文件 + bind mount，只给 worker 容器读取。
- systemd：`EnvironmentFile=/etc/aivideotrans-mainland-worker/worker.env`。

HMAC secret 是双边 secret，需要美国 Gateway 和武汉 worker 同时持有。

初期采用人工同步：

- 美国侧写入 Gateway 运行环境，例如 `gateway/.env` 或生产 env secret。
- 武汉侧写入 `/etc/aivideotrans-mainland-worker/worker.env`。
- 不引入“Gateway 自动下发 secret 到 worker”的通道，避免扩大密钥面。

### 文件权限

要求：

- secret 文件 owner 为 root。
- 权限 `600`。
- worker 进程只读取必要环境变量。
- 不把 secret 写入镜像、git、日志、异常响应或健康检查。

### Key 轮换

建议：

- DashScope API Key：至少 90 天轮换一次。
- Worker HMAC secret：至少 90 天轮换一次。
- 支持 key id：`X-AVT-Key-Id`，允许新旧 key 短期并存。

Docker Compose 路线下，修改 `worker.env` 后必须重新创建 worker 容器，例如 `docker compose up -d worker`；不要只做 `docker restart`，否则 env 文件变更不会重新加载。

### 入侵应急

如果武汉 ECS 疑似泄漏：

1. 立即禁用 `mainland_voice_worker_enabled`。
2. 撤销 DashScope API Key。
3. 轮换 worker HMAC secret。
4. 删除 worker 临时 artifact。
5. 导出并封存 audit log。
6. 重新部署 worker 或更换 ECS。
7. 人工复核所有 provider voice deletion 状态。

## Retry 和付费 API 上限

所有会触发 provider 付费或资源创建的调用必须有硬上限。

Clone：

- 每次用户确认最多触发 1 次 clone provider call。
- 网络不确定错误可由用户再次显式点击重试。
- 后端不能自动重复 clone 超过 1 次。

TTS 单段：

- 单段 provider TTS 最多 3 次。
- 退避：1s -> 5s -> 15s。
- 三次失败后该 segment 标记失败。

Batch：

- batch 整体最多重提 1 次。
- batch 重提必须跳过已经成功且 sha256 校验通过的 segment。
- 幂等键：`job_id + segment_id + voice_id + target_model + text_hash + speech_rate`。

Worker 下载 artifact：

- 美国主机下载 package 最多 3 次。
- 失败后任务进入可恢复状态，不重新触发 provider TTS，除非缺失 segment 明确需要补合成。

禁止：

- 无限 loop。
- provider 失败后静默切换其他付费 provider。
- worker 不可用时自动触发 MiniMax clone。

## Worker Degraded Mode

Worker 不可用时需要显式降级。

判定：

- `/healthz` 连续 3 次失败，或
- 最近 5 分钟 TTS batch 失败率超过阈值，或
- Nginx 502/504 持续出现。

降级行为：

- Gateway 将 `cosyvoice_clone_worker_available=false` 写入运行时状态。
- `available` 由 worker 心跳自动维护，admin 不应手动改。
- Voice selection 隐藏 CosyVoice clone 入口，但保留公共音色 TTS。
- 已经选择 CosyVoice cloned voice 的任务进入 `awaiting_worker` 或 review pause。
- 不自动切换到 MiniMax。
- 管理员可手动改 provider 并让用户确认后继续。

恢复：

- `/healthz` 连续 3 次成功。
- 一次 mock synthesize 成功。
- 管理员重新启用灰度。

## 审计日志

Worker 本地写 JSONL：

- 路径：`/data/aivideotrans-mainland-worker/audit/worker-audit.jsonl`
- 权限：worker 可写，非公开。
- 内容只写 metadata，不写 raw audio，不写 API key。

字段：

- `event_id`
- `request_id`
- `job_id`
- `user_id`
- `speaker_id`
- `voice_id`
- `operation`
- `provider`
- `target_model`
- `provider_request_id`
- `status`
- `duration_ms`
- `billed_chars`
- `audio_seconds`
- `artifact_bytes`
- `error_code`
- `created_at`

同步策略：

- Worker 本地 JSONL append 是主记录。
- 每小时把 metadata batch 同步回美国主机，便于运营排查。
- raw audio 不做日志同步。
- artifact 走短 TTL，过期删除。

## 数据流

### Clone Flow

1. 用户在 Studio voice selection 选择 CosyVoice。
2. Gateway 判断用户权益和 `cosyvoice_clone_worker_enabled`。
3. UI 显示克隆入口。
4. 用户选择样本片段并显式确认。
5. 美国 Job API 提取并拼接授权样本。
6. 美国 Job API 调用武汉 worker `/cosyvoice/clone`。
7. Worker 必要时上传样本到 OSS signed URL。
8. Worker 调用 DashScope clone/design API。
9. Worker 返回 `voice_id`、`target_model`、`requires_worker`。
10. Gateway 写入用户音色库。
11. Voice selection 将该 speaker 标记为 CosyVoice cloned voice。

### TTS Flow

1. Pipeline 已生成 SemanticBlock / DubbingSegment。
2. 按 `voice_id + target_model + requires_worker` 分组。
3. Worker-required 的 CosyVoice 片段调用 `/cosyvoice/synthesize-batch`。
4. Worker 调用国内 CosyVoice TTS。
5. Worker 生成音频 package 和 manifest。
6. 美国主机下载 package。
7. Pipeline hydrate 标准 `TTSResult`。
8. 继续 DSP alignment、subtitle retiming、Jianying draft generation。

## Phase -1：账号能力验证

这是编码前置条件，不通过就不写 worker 代码。

步骤：

- 登录阿里云国内控制台。
- 确认 DashScope / 百炼账号已开通 CosyVoice clone/design 能力。
- 确认目标模型可用：优先 `cosyvoice-v3.5-flash`。
- 使用官方 curl 或 SDK 示例跑一次测试 clone。
- 获取测试 `voice_id`。
- 使用同一 `target_model` 合成一句短文本。
- 删除测试 voice。
- 查看账单确认真实计费行为。

通过标准：

- Clone API 可调用。
- TTS 可调用。
- 删除可调用。
- 账单与预期一致。
- 账号无额外人工审核或白名单阻塞。

## Rollout Plan

### Phase 0：连通性和 Worker Skeleton

- 创建 worker skeleton。
- 只实现 `/healthz`。
- 配置 Nginx 路径。
- 配置 HMAC 校验。
- 从美国主机调用 `/healthz`。
- 实测 10 MB、50 MB、100 MB artifact 下载速度。
- 不调用 DashScope。

通过标准：

- 美国主机能稳定访问 worker。
- HMAC 校验生效。
- 日志不泄漏 secret。
- 下载速度达到 Phase 3 预期，或明确启用 OSS artifact 备选路径。
- 不影响 Hermes、atlas app、Postgres。

### Phase 1：Mock Mode

- `/cosyvoice/clone` 返回 deterministic fake voice id。
- `/cosyvoice/synthesize-batch` 返回 silent WAV。
- 美国主机实现 worker client。
- 本地 tests 使用 fake worker，不访问网络。

通过标准：

- `main.py` 和 `pytest` 在干净本地环境可跑。
- Pipeline 能消费 worker-shaped TTS result。
- Studio post-edit 单段 regenerate 能走同一 batch endpoint。

### Phase 2：真实 Clone POC

- 仅 admin 可用。
- 使用一个或两个已授权样本。
- Phase 2 样本必须来自 admin / dev 团队自己的声音录音，不得使用任何真实用户数据。
- 优先测试 `cosyvoice-v3.5-flash`，因为成本低，POC 容错更好。
- 保存 `target_model` 和 `requires_worker`。
- 测试 provider deletion。

通过标准：

- Clone 成功。
- Clone voice 可删除。
- 样本在 worker 临时目录中按 TTL 删除。
- 账单符合预期。

### Phase 3：真实 TTS Batch POC

- 使用 Phase 2 的 voice 合成小批量。
- 下载 package 到美国主机。
- Hydrate TTS outputs。
- 继续 alignment 和 Jianying draft。
- 如果回传超过 60 秒，启用 OSS artifact 路径。

通过标准：

- 音频能被现有 DSP alignment 接收。
- timing metadata 符合 `TTSResult` 合约。
- 没有 worker 逻辑进入 subtitle retiming 或 draft generation。

### Phase 4：Studio 灰度

- 管理员配置打开。
- 仅 allowlist 用户可见 CosyVoice clone。
- 与 MiniMax 同素材对比。
- 灰度前再比较 `cosyvoice-v3.5-plus` 与 `cosyvoice-v3.5-flash`。

通过标准：

- Worker 启用 HTTPS / TLS。
- `443` 端口开放。
- 证书有效且自动续期。
- HTTP 明文入口不承载真实用户音频和真实 provider 请求。

指标：

- clone 成功率
- TTS 成功率
- 每分钟音频耗时
- 相似度主观评分
- 发音错误率
- 情绪表达
- 单分钟交付成本
- worker unavailable 次数

### Phase 5：产品决策

只有同时满足以下条件才扩大开放：

- 质量稳定可接受。
- worker 可用性达标。
- 成本显著低于 MiniMax。
- 失败处理和人工接管可控。
- 用户授权、删除和审计流程完整。

否则保持 admin-only 实验 provider。

## Rollback

回滚必须简单：

- 关闭 `cosyvoice_clone_worker_enabled`。
- 隐藏 CosyVoice clone 入口。
- MiniMax clone 主路径不受影响。
- 已经使用 CosyVoice clone 的任务继续通过 worker 完成，或进入 `awaiting_worker`。
- 不自动删除已有 CosyVoice user voices。
- 如果 worker 长期停用，把这些 voice 标记为 unavailable。

## Open Questions

- POC 后，`cosyvoice-v3.5-flash` 的实际质量是否足够，还是必须灰度 `plus`？
- 生产阶段 clone sample URL 是否直接走 OSS signed URL，还是先保留 worker 本地临时 URL 作为降级？
- CosyVoice clone 用户授权文案如何写，是否需要单独的声音生物特征提示？
- 国内 worker 未来是否承接豆包 ICL 2.0；若承接，是否复用同一 HMAC 和 artifact 子系统？

## 推荐启动顺序

先做 5 件事，再进入编码：

0. 确认武汉 ECS 已续费，建议至少续费 1 年，避免 POC 中途实例到期。
1. Phase -1：确认阿里云账号有 CosyVoice 国内 clone/design 权限。
2. Phase 0：实测美国主机到武汉 ECS 的 artifact 下载速度。
3. 确认 worker secret 注入方式和权限。
4. 确认 `requires_worker` / `region_constraint` 字段落库位置。
5. 确认 Studio post-edit 单段 regenerate 走 batch endpoint。

通过后，再开始 Phase 1 mock integration。

## 执行记录

### 2026-05-24 Phase -1 账号能力验证

DashScope key 已注入武汉 ECS：

- 文件：`/etc/aivideotrans-mainland-worker/worker.env`
- 权限：`600:root:root`
- 校验方式：只验证 `DASHSCOPE_API_KEY` 存在，不在终端输出 key 明文。

账号和国内 endpoint 验证结果：

- `list_voice` 调用 `https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization` 成功，HTTP 200。
- `create_voice` 使用 `target_model=cosyvoice-v3.5-flash` 成功，返回 voice id。
- `query_voice` 轮询成功，状态从 `DEPLOYING` 进入 `OK`。
- 使用同一个 `target_model=cosyvoice-v3.5-flash` 调用非实时 TTS `SpeechSynthesizer` 成功，HTTP 200。
- TTS 测试文本：`This is a short voice chain test.`
- TTS 计费用量：33 characters。
- 输出音频可下载，返回 WAV，测试下载大小 130604 bytes，文件头 `RIFF`。
- 测试 voice 已调用 `delete_voice` 清理，HTTP 200。

实测注意事项：

- 阿里官方样例 URL `https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/cosyvoice/cosyvoice-zeroshot-sample.wav` 可从武汉 ECS `curl` 下载，但 DashScope 服务端创建音色时返回 `BadRequest.InputDownloadFailed`。
- 将同一公开样例临时转托管到武汉 ECS 后，DashScope 服务端能访问 Nginx，但完整 1.8 MB 文件仍会下载失败。
- 从样例裁剪 10 秒、约 960 KB 的 WAV 后，`create_voice` 成功。
- 结论：Phase 1 worker 应主动规范 clone sample，建议生成 10-20 秒、WAV/MP3/M4A、优先小于 1 MB 的临时公网 URL；生产阶段优先 OSS signed URL，避免依赖 ECS 5 Mbps 出网和临时 Nginx。
- 官方样例是英文，使用中文 TTS 文本时返回 `InvalidParameter: Please ensure input text is valid.`；改为 `language_hints=["en"]` 并使用英文测试句后 TTS 成功。后续中文真人样本应传 `language_hints=["zh"]` 并用中文文本验证。

临时资源清理：

- 武汉 Nginx 临时 `/avt-poc/` location 已删除。
- `/var/www/avt-poc` 已删除。
- Nginx 已通过 `nginx -t` 并 reload。

### 2026-05-24 Phase 0 Preflight

武汉 ECS：

- SSH 可用，登录用户 `ecs-user`。
- 主机名 `atlas-invest`。
- Python `3.12.3`、Docker `29.1.3`、Nginx `1.24.0` 可用。
- 内存 available 约 2.2 GiB。
- `/` 剩余约 36 GiB，`/data` 剩余约 186 GiB。
- 现有公网监听仍只有 `22` 和 `80`；临时测速 Nginx server block 已在测试后删除。

美国主机到武汉：

- `http://8.148.83.128/` 可达。
- 10 MB 下载：约 16 秒，平均约 653 KB/s。
- 50 MB 下载：约 1 分 49 秒，平均约 470 KB/s。
- 50 MB 已超过 60 秒阈值，因此 Phase 3 不应默认依赖 ECS 5 Mbps 直接回传大 artifact。
- Phase 3 优先采用 OSS signed artifact，或先升级武汉 ECS 公网带宽。

武汉到美国：

- 武汉直连美国主机 `5.78.122.220:22` 可达。
- 武汉直连美国主机 `5.78.122.220:80`、`:443` 超时。
- 武汉本机 `127.0.0.1:18080` HTTP 代理可访问公网，出口 IP 为 `140.228.18.32`。
- `127.0.0.1:18081` 作为 SOCKS 代理测试超时。
- 经 `127.0.0.1:18080` 访问美国主机 HTTP 返回 `503`。

结论：

- Worker 通信模式应采用 US 主机主动请求武汉 worker，并由 US 主机主动拉取 artifact。
- 不要设计成武汉 worker 主动 callback 到 US HTTP/HTTPS。
- 大 artifact 回传需要 OSS 或带宽升级。

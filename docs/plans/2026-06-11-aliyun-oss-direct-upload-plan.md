# 大文件上传通道方案：Cloudflare 分片上传（主）+ OSS 直传（后备）

> 状态：**r2 修订版，待 CodeX 复审**。本文档是大文件上传的完整设计真值；实施偏离须回写。
>
> 修订记录：
> - r0（2026-06-11 午）：OSS 直传为主方案。
> - r1（2026-06-11 晚）：实测核实 CF 100MB 为单请求体限制 → 分片上传取代 OSS 为主方案，但只有摘要。
> - **r2（2026-06-11 夜）：按 CodeX 外审 7 条 findings 把分片方案扩成完整设计**（路由契约/状态机/
>   锁与幂等/磁盘 reserve/CSRF/admin 三处同步/测试矩阵）；OSS 全文降为附录 A。

## 1. 背景与问题（已实证）

- 生产公网唯一入口 = Cloudflare Tunnel（源站 443/80 实测不可达）。
- **CF 免费计划单 HTTP 请求体上限 100MB**，三重证据（2026-06-11）：
  官方 413 文档原文 "Max upload size: 100/100/200/500+ MB (Free/Pro/Business/Enterprise)"；
  120MB 实弹 → CF 边缘 413；**80MB 实弹 → 穿过边缘抵达源站**（返回源站 405 JSON）。
  官方对策原文："break up requests into smaller chunks, change DNS to DNS-only, or upgrade"。
- 用户实际被卡：394.5MB 视频上传失败；admin 应用层旋钮（已调至 1024MB）无法突破边缘上限。
- 限制是**单请求体**而非业务文件 → 应用层分片（每片 <100MB 独立请求）合法绕过。

## 2. 目标 / 非目标

**目标**
- 注册用户本地视频上传支持 ≤2GB（admin 旋钮可调），走现有 CF Tunnel，**零新增基础设施/密钥/流量费**。
- 断点续传 + 进度可见；合并后接入**现有** uploads/ 落盘约定与 job 创建流（pipeline 零感知）。

**非目标**
- 匿名免费档不开放分片（保持 ≤95MB 单请求路径，滥用面小）。
- 不动下载/R2 切面；不做 OSS（降为附录 A 后备，触发条件见 §6-Q2）。
- v1 不替换 ≤95MB 的现有单请求上传路径（前端按文件大小自动选路）。

## 3. 主方案设计：应用层分片上传

### 3.1 路由契约

所有路由挂 gateway（FastAPI），**全部要求登录态**（沿用现有 auth dependency）；
所有状态变更方法（POST/PUT/DELETE）**必须过 `require_same_origin_state_change`**
（CodeX P0：与现有 gateway 写路由一致，session cookie 不是 CSRF 防线）。

| # | Method/Path | 作用 | 关键校验 | 主要错误 |
|---|---|---|---|---|
| R1 | `POST /gateway/uploads/chunked/init` | 声明 `{size, sha256, chunk_size, file_name}` → 返回 `{upload_id, chunk_size, total_parts, received_parts:[]}` | CSRF；size ≤ admin 上限；chunk_size ∈ [admin 下限, 80MB]；**磁盘 reserve（§3.4）**；per-user 活跃 upload 数 ≤ 旋钮；**同 user 续传复用（§3.5）** | 403 csrf/未登录、413 over_limit、429 too_many_active、507 insufficient_storage |
| R2 | `PUT /gateway/uploads/chunked/{upload_id}/part/{n}` | raw body 写第 n 片（**`request.stream()` 流式落盘，禁 `request.form()`**，CodeX P2） | CSRF；ownership（§3.5）；state==receiving（§3.2）；n ∈ [0,total_parts)；Content-Length 必带且 ≤ chunk_size（末片 ≤ 余量）；流式超量即断 | 404 not_found（含非本人）、409 wrong_state、413 part_too_large |
| R3 | `POST /gateway/uploads/chunked/{upload_id}/complete` | 持锁合并 → 全文件 sha256 比对 → 移入 uploads/ 正式路径 → 返回现有 upload ref | CSRF；ownership；state receiving→completing（原子，§3.3）；分片齐全；**合并前磁盘二次 reserve** | 409 missing_parts/wrong_state、422 sha256_mismatch、507 |
| R4 | `GET /gateway/uploads/chunked/{upload_id}/status` | `{state, received_parts 位图, bytes_received}`（断点续传依据） | ownership（404 同形） | 404 |
| R5 | `DELETE /gateway/uploads/chunked/{upload_id}` | 用户主动放弃，清分片目录 | CSRF；ownership；state ∈ {receiving, failed} | 404、409 |

分片落盘隔离目录：`uploads/_chunked/{user_id}/{upload_id}/part_{n:05d}`；
`upload_id = uuid4().hex`（服务端生成，路径组件零信任拼接，正则 `^[a-f0-9]{32}$` 深度防御）。

### 3.2 状态机（CodeX P1：并发锁的前提）

```
receiving ──complete(全片齐+锁获取成功)──→ completing ──合并+校验成功──→ ready
    │                                         │
    │←──────(校验失败/IO错: 回 receiving 并记 failure_reason，或→failed)──┘
    │
    ├──TTL 到期(sweeper)──→ expired（清盘删除）
    └──DELETE──→ aborted（清盘删除）
```

| 状态 | part 写入 | complete | status | 持久化 |
|---|---|---|---|---|
| receiving | ✅ | ✅（转 completing） | ✅ | `state.json`（upload 目录内，含声明元数据+状态） |
| completing | ❌ 409 | ❌ 409（幂等：返回 in_progress） | ✅ | 同上 |
| ready | ❌ 409 | ✅ 幂等返回同一 upload ref | ✅ | 合并文件已移交 uploads/，目录留 state.json 短期供幂等 |
| failed/expired/aborted | ❌ | ❌ | ✅（failed 含 reason） | 清盘 |

### 3.3 锁与幂等（CodeX P1）

- **每 upload_id 一把跨进程文件锁**：复用 `src/services/_file_lock.py`（项目现有 reentrant
  file lock），锁文件 `uploads/_chunked/_locks/{upload_id}`（独立于数据目录，同 R2 lock 先例）。
- **part 写入**：先写 `part_{n}.tmp` → fsync → `os.replace()` 原子改名。同片重传 = 覆盖
  （仅 receiving 态允许；改名原子性保证读端永不见半截片）。
- **complete**：获锁 → 复检 state==receiving 且分片齐全 → 置 completing（state.json 原子写）
  → 释放期间 part 全被 409 → 顺序合并（流式 append，逐片校验长度）→ 全文件 sha256 比对
  → `os.replace()` 移入正式 uploads/ 路径 → 置 ready → 删分片。
- **幂等**：R3 在 ready 态重复调用返回同一 upload ref；completing 态返回 202 in_progress
  （客户端轮询 R4）。init 幂等见 §3.5。

### 3.4 磁盘预算与 reserve（CodeX P1：放大没算）

放大事实：分片目录 S + 合并文件 S 同时存在（合并完成才删分片）= **上传层峰值 2S**；
随后 pipeline 对 local_video 还会复制进 workspace（process.py 现有行为）= 任务期再 +S。
2GB 文件 → 上传层峰值 4GB、任务全程峰值 ~6GB。

控制三层：
1. **init 预检**：`statvfs 可用空间 - admin 磁盘保底(默认 20GB) ≥ 2×declared_size +
   Σ(全局 in-flight uploads 的 2×declared 未落地余量)`，不满足 → 507 fail-closed。
2. **complete 合并前二次预检**（init 后磁盘可能被别的任务吃掉）：可用 - 保底 ≥ declared_size。
3. **in-flight bytes 旋钮**：per-user 并发 upload 总声明字节 ≤ 旋钮（默认 4GB）、全局 ≤ 旋钮
  （默认 20GB）；每日配额（次数/GB）另设。全部 fail-closed。

### 3.5 断点续传与复用收窄（CodeX P1：杜绝跨用户探测）

- 续传键 = **(user_id, sha256, size, chunk_size)** 四元组，只在**该用户自己的活跃
  （receiving）upload** 里查找；命中 → 返回原 upload_id + received_parts 位图；不命中 → 新建。
  **绝不**做全局 sha256 去重/秒传（跨用户存在性探测 + 状态泄漏）。
- R2-R5 一律按 `upload_id AND user_id` 查 ownership；不存在与不属于本人**返回同形 404**
  （响应体逐字节一致，无时序侧信道放大）。

### 3.6 流式接收（CodeX P2）

- part 端点用 raw `PUT` + `async for chunk in request.stream()` 直写 `.tmp`，
  **不经过 `request.form()`/multipart**（避免 Starlette 临时文件双拷贝；参照
  `anonymous_preview_upload.handle_anonymous_upload` 现有流式实现）。
- 流式计数超 `min(chunk_size, 末片余量) + 1KB 容差` → 立即断流、删 tmp、413。
- gateway 自身无全局 body limit（uvicorn 默认不限），Caddy 不在 Tunnel 路径上；
  实施时复核 cloudflared → gateway 链路无中间 body 上限（80MB 实测已通过该链路）。

### 3.7 admin 旋钮（CodeX P2：full-body 语义，三处同步）

新增字段（独立命名空间 `chunked_upload_*`，**不复用任何 `anonymous_preview_*` 字段**）：

| 字段 | 默认 |
|---|---|
| `chunked_upload_enabled`（StrictBool，总开关） | False（部署后灰度开） |
| `chunked_upload_max_file_mb` | 2048 |
| `chunked_upload_chunk_mb` | 64（≤80 硬上限 validator） |
| `chunked_upload_per_user_active` | 2 |
| `chunked_upload_per_user_inflight_gb` / `_global_inflight_gb` | 4 / 20 |
| `chunked_upload_daily_per_user_gb` | 8 |
| `chunked_upload_disk_floor_gb` | 20 |
| `chunked_upload_ttl_hours`（未完成清扫） | 24 |

**硬性同步要求**：`/api/admin/settings` 是 full-body 整文档替换语义——新增字段必须**同一
commit** 内完成：① gateway `AdminSettings` Pydantic 字段+validator；② 前端 admin 设置页
类型 + `DEFAULT_SETTINGS`；③ 守卫测试断言两端字段集一致（防旧前端保存把新字段打回默认）。

### 3.8 清扫 sweeper

`chunked_upload_sweeper`（gateway 后台任务，复用 reservation sweeper 模式）：
每 10min 扫 `uploads/_chunked/`，state.json 超 TTL 且非 ready → 置 expired 清盘；
ready 超 1h（job 已创建或被放弃）→ 清 state.json 残留；孤儿目录（无 state.json）直接删。
日志计数进 runtime_logs JSONL。

### 3.9 前端

- 选路：文件 ≤95MB → 现有单请求路径；>95MB → 分片（阈值与 2GB 上限从 limits 类端点动态拉取，
  不硬编码——沿用 2026-06-11 APF limits 端点先例）。
- 切片 `chunk_mb`（init 返回为准）；并发 3 片；片级失败指数退避重试 3 次；
  页面刷新后凭文件重算 sha256 → init 命中续传 → 按位图补传。
- 进度 = bytes_received/size；complete 后轮询至 ready 拿 upload ref → 走现有 job 创建表单流。
- 失败文案明确（**不**自动回退单请求路径——大文件回 CF 单请求必 413）。

## 4. 分期实施

- **P1 后端**：R1-R5 + 状态机/锁/reserve/sweeper + admin 字段三处同步 + 全部单测；
  `chunked_upload_enabled=False` 上线休眠，admin 账号灰度。
- **P2 前端**：选路 + 切片上传组件 + 进度/续传 UI；灰度开放全部注册用户。
- **P3 评估**：实测中国用户大文件成功率/速度（CF 免费版无中国节点）；不达标 → 启动附录 A
  的 OSS 后备。

## 5. 测试矩阵

| 类 | 用例 |
|---|---|
| 状态机 | receiving/completing/ready/failed/expired 全转换；completing 拒 part(409)；ready 幂等 complete |
| 并发 | 双 complete 竞争只一个获锁；part 重传与 complete 互斥；锁释放后状态一致 |
| 安全 | 全部写方法缺 Origin → 403；跨用户 upload_id → 404 与不存在同形；upload_id 非法格式拒绝；路径穿越（n 越界/负数） |
| 限额 | size/chunk_size 超旋钮；per-user active 超限；inflight GB 超限；磁盘 reserve 不足 507（statvfs mock） |
| 完整性 | 片级长度/末片余量校验；全文件 sha256 不匹配 422 + 状态回退；tmp 残留不被当作有效片 |
| 续传 | 四元组命中返回位图；不同 user 同 sha256 不互通；位图补传后 complete 成功 |
| 流式 | 超量中断删 tmp；Content-Length 缺失拒绝 |
| admin 同步守卫 | 后端字段集 == 前端 DEFAULT_SETTINGS 字段集（AST/JSON 断言）；full-body 保存不丢新字段 |
| sweeper | TTL 过期清盘；孤儿目录清理；ready 残留回收 |
| e2e（显式触发） | 真实 >100MB 文件经公网 CF 全链到 job 创建（80MB/片以下已实证可过边缘） |

## 6. 开放问题（复审请给意见）

1. **Q1 合并 IO**：2GB 顺序合并约数十秒（Hetzner NVMe），completing 期间客户端轮询 R4——
   是否需要合并进度百分比？（倾向不做，v1 简单。）
2. **Q2 OSS 后备触发阈值**：建议"中国用户 >500MB 文件 P50 上传耗时 >15min 或成功率 <80%"
   时启动附录 A；阈值请复审定夺。
3. **Q3 sha256 前端计算成本**：2GB 浏览器端 WebCrypto 流式哈希约 10-20s——是否改为可选
   （无 sha256 则不支持续传、complete 只校验长度）？（倾向必算，完整性优先。）

---

# 附录 A：阿里云 OSS 直传方案（后备，r0 原文压缩保留）

> 仅当 §4-P3 实测不达标时启动。密钥资产：生产 `.env` 已有 `AVT_COSYVOICE_OSS_*` 五件套
> （CosyVoice 样本中转用，cn-beijing）；未合并分支 `codex/cosyvoice-aliyun-oss-uploader`
> 有 S3-compatible uploader 参考实现。

- **架构**：gateway 签 STS（15min、仅 `oss:PutObject` 限定 `uploads/{user_id}/{uuid}/` 前缀）
  → 浏览器 OSS JS SDK 分片直传（国内链路快一个量级）→ complete 后 gateway 回拉 worker
  GetObject 落盘本地 uploads/（守住 pipeline 本地路径契约）→ 校验 sha256 → 删 OSS 对象
  （bucket 生命周期 24h 兜底 + AbortIncompleteMultipartUpload 1 天）。
- **安全**：新建独立私有 bucket（`avt-user-uploads`）+ 独立 RAM 角色最小 policy（user_id
  注入收窄）；CORS 仅 aitrans.video；长期 AccessKey 永不出 gateway；签发限频+审计 JSONL；
  回拉前 HeadObject 校验 size、落盘后 sha256+ffprobe。
- **成本**：上行免费；回拉公网流出 ~¥0.50/GB（2GB ≈ ¥1/次）；存储瞬态忽略；
  per-user/全局每日 GB 配额旋钮 = 成本红线；传输加速先不开。
- **不选为主案的原因**（r1 决策）：分片方案零基础设施/零流量费/零新密钥面已可满足
  "能传 2GB"；OSS 仅在"传得快"上占优，留作实测兜底。

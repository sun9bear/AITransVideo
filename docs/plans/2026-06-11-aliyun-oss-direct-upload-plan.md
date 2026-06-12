# 大文件上传通道方案：Cloudflare 分片上传（主）+ OSS 直传（后备）

> 状态：**已实施（P1 后端 + P2 前端，2026-06-11，分支 claude/compassionate-thompson-80a70c）**。
> 本文档是大文件上传的完整设计真值；实施偏离已回写至 §8。上线前置：admin 后台打开
> `chunked_upload_enabled`（默认 False 休眠）。
>
> 修订记录：
> - r0（2026-06-11 午）：OSS 直传为主方案。
> - r1（2026-06-11 晚）：实测核实 CF 100MB 为单请求体限制 → 分片上传取代 OSS 为主方案，但只有摘要。
> - r2（2026-06-11 夜）：按 CodeX 外审 7 条 findings 把分片方案扩成完整设计（路由契约/状态机/
>   锁与幂等/磁盘 reserve/CSRF/admin 三处同步/测试矩阵）；OSS 全文降为附录 A。
> - **r3（2026-06-11 夜，二轮复审后）：补 4 个 P1**——init/reserve 全局原子锁、per-part
>   SHA256（X-Chunk-SHA256）+ 完整性失败可恢复路径、ready 未认领文件 claim/TTL 清理闭环、
>   job create 改 opaque upload ref（路径不再作能力凭证）；补 R6 limits 端点契约、前端哈希
>   改 Web Worker 增量实现；Q1/Q2 按复审意见落定。**连带发现现网加固项 H1（§7）。**

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
- ~~匿名免费档不开放分片（保持 ≤95MB 单请求路径，滥用面小）。~~
  **2026-06-12 作废**：项目主确认主漏斗痛点正是匿名试用入口（>100MB 被 CF 边缘
  掐断、面板报"网络错误"，prod 实测两次复现）。匿名档分片扩展立项为 §9（B 方案）。
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
| R2 | `PUT /gateway/uploads/chunked/{upload_id}/part/{n}` | raw body 写第 n 片（**`request.stream()` 流式落盘，禁 `request.form()`**，CodeX P2）；**必带 `X-Chunk-SHA256`**，服务端流式计算比对，存入 state（r3） | CSRF；ownership（§3.5）；state==receiving（§3.2）；n ∈ [0,total_parts)；Content-Length 必带且 ≤ chunk_size（末片 ≤ 余量）；流式超量即断；片哈希不符 → 拒收删 tmp | 404 not_found（含非本人）、409 wrong_state、413 part_too_large、422 part_hash_mismatch |
| R3 | `POST /gateway/uploads/chunked/{upload_id}/complete` | 持锁合并 → 全文件 sha256 比对 → 移入 uploads/ 正式路径 → 返回 **opaque upload ref = upload_id（不是文件路径，§3.10）** | CSRF；ownership；state receiving→completing（原子，§3.3）；分片齐全；**合并前磁盘二次 reserve** | 409 missing_parts/wrong_state、422 sha256_mismatch（处置见 §3.3-r3）、507 |
| R4 | `GET /gateway/uploads/chunked/{upload_id}/status` | `{state, received_parts 位图, bytes_received}`（断点续传依据） | ownership（404 同形） | 404 |
| R5 | `DELETE /gateway/uploads/chunked/{upload_id}` | 用户主动放弃，清分片目录 | CSRF；ownership；state ∈ {receiving, failed} | 404、409 |
| R6 | `GET /gateway/uploads/chunked/limits` | 只读：`{enabled, threshold_mb, max_file_mb, chunk_mb}` 供前端选路/切片（r3，CodeX P2） | 登录态；无 CSRF（GET）；`chunked_upload_enabled=false` → `enabled:false`（200，不是 404——前端据此隐藏入口） | 401 |

分片落盘隔离目录：`uploads/_chunked/{user_id}/{upload_id}/part_{n:05d}`；
`upload_id = uuid4().hex`（服务端生成，路径组件零信任拼接，正则 `^[a-f0-9]{32}$` 深度防御）。

### 3.2 状态机（CodeX P1：并发锁的前提）

```
receiving ──complete(全片齐+锁获取成功)──→ completing ──合并+校验成功──→ ready
    │                                         │
    │←─(瞬时 IO 错: 回 receiving, 记 failure_reason, 分片保留)─┤
    │                                         └─(全文件 sha256 不符: → failed_integrity,
    │                                            **清空全部分片**, 客户端须整体重传——
    │                                            r3: part 级已有 X-Chunk-SHA256 把关,
    │                                            走到这步=声明哈希错或磁盘损坏, 位图保
    │                                            留只会无限 422)
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
1. **init 预检（r3：必须原子）**：整段"检查 + 注册"在**全局 reserve 锁**
   （`uploads/_chunked/_locks/_reserve`，复用 `_file_lock`）内执行：获锁 → 扫描全部
   state.json 汇总 in-flight 声明字节 → 校验 `statvfs 可用 - 磁盘保底(默认 20GB) ≥
   2×declared + Σ(in-flight 2×declared 余量)` 且 per-user active/in-flight GB/global
   GB 均未超 → **在锁内写入本 upload 的 state.json（即注册 reserve）** → 释锁。
   并发双 init 串行化，杜绝同时看到"空间足够"双双通过（CodeX r2-P1）。不满足 → 507。
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
孤儿目录（无 state.json）直接删。日志计数进 runtime_logs JSONL。

**r3 新增：ready 文件 claim 闭环**（CodeX r2-P1：未认领的 2GB 终文件会长期滞留）：
- complete 成功后 state.json 保留，记录 `final_path` + `claimed_by_job: null`。
- job create（经 §3.10 的 opaque ref 解析）成功 → 把 job_id 回写 state.json（= claim）。
- sweeper：state==ready 且 `claimed_by_job is null` 且超 `chunked_upload_ready_ttl_hours`
  （新旋钮，默认 6）→ **删除 final_path 终文件** + 清 state；已 claim 的 → 终文件归现有
  uploads 生命周期管理，仅清 state 残留。删除前校验 final_path 仍在 uploads/ 根内（深度防御）。

### 3.9 前端

- 选路：文件 ≤95MB → 现有单请求路径；>95MB → 分片（阈值与 2GB 上限从 limits 类端点动态拉取，
  不硬编码——沿用 2026-06-11 APF limits 端点先例）。
- 切片 `chunk_mb`（init 返回为准）；并发 3 片；片级失败指数退避重试 3 次；
  页面刷新后凭文件重算 sha256 → init 命中续传 → 按位图补传。
- 进度 = bytes_received/size；complete 后轮询 R4 至 ready；**completing 期间 UI 显示
  "正在合并校验…"**（2GB 数十秒，不做百分比但不能像卡死——Q1 落定）。
- **sha256 必算，但实现纠正（r3，CodeX P2）**：`crypto.subtle.digest` 是 one-shot 接口，
  不能直接吃 2GB——用 **Web Worker + 增量 SHA-256 库**（如 hash-wasm 流式 API）分块喂入；
  哈希阶段单独显示进度（2GB 约 10-20s）。片级哈希同库在切片时顺带算。
- 失败文案明确（**不**自动回退单请求路径——大文件回 CF 单请求必 413）。

### 3.10 opaque upload ref：路径不作能力凭证（r3，CodeX P1）

complete 返回的 upload ref = **`upload_id`（不透明 token），不是文件路径**。job 创建时
前端把 `source: {type:"local_video", value:"chunked:{upload_id}"}` 传给 gateway；gateway
create 拦截层解析：按 `upload_id + 当前登录 user_id` 查 state==ready 的 upload →
校验通过才把 `source.value` **替换为服务端记录的 final_path** 转发 Job API，并回写 claim
（§3.8）。前端传回的任何绝对路径不再被信任。失败 → 403/404 同形。

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
| 完整性 | 片级 X-Chunk-SHA256 不符拒收；全文件 sha256 不符 → failed_integrity 清空分片（不留满位图死循环）；tmp 残留不被当作有效片 |
| 原子 reserve | 并发双 init 在 reserve 锁下串行：第二个见到第一个的注册量后正确 507/429 |
| claim 闭环 | ready 未认领超 TTL 终文件被删；已 claim 不删；opaque ref 解析校验 ownership+state；伪造路径型 source.value 被拒 |
| 续传 | 四元组命中返回位图；不同 user 同 sha256 不互通；位图补传后 complete 成功 |
| 流式 | 超量中断删 tmp；Content-Length 缺失拒绝 |
| admin 同步守卫 | 后端字段集 == 前端 DEFAULT_SETTINGS 字段集（AST/JSON 断言）；full-body 保存不丢新字段 |
| sweeper | TTL 过期清盘；孤儿目录清理；ready 残留回收 |
| e2e（显式触发） | 真实 >100MB 文件经公网 CF 全链到 job 创建（80MB/片以下已实证可过边缘） |

## 6. 开放问题（r3 已按二轮复审落定）

1. **Q1 合并进度** ✅ 落定：不做百分比；R4 在 completing 态返回 `state=completing`，
   前端显示"正在合并校验…"文案（§3.9），不让 UI 像卡死。
2. **Q2 OSS 后备阈值** ✅ 落定：">500MB P50 >15min 或成功率 <80%" 启动附录 A；
   **同时记录 P95 与失败原因分布**（弱网用户不被 P50 掩盖）——进 metering JSONL。
3. **Q3 前端哈希** ✅ 落定：必算；实现为 Web Worker + 增量 SHA-256 库（§3.9），
   不用 one-shot `crypto.subtle.digest`。

## 7. 连带发现的现网加固项（不阻塞本方案，单独排期）

**H1（来自二轮复审证据）**：现有 job create 链路对 `local_video` 的 `source.value`
（绝对路径）**不做"属于当前用户"的归属校验**（gateway job_intercept 只算 source hash；
Job API api.py 直接收 source.value）。已注册用户理论上可提交任意服务器路径作为源视频。
本方案的 §3.10 为分片通道关死该面；**存量单请求上传路径需要同等加固**：create 阶段强制
`source.value` 位于该用户的合法 uploads 命名空间内（或同样迁移到 opaque ref）。
单独任务处理，勿混入本方案实施。

## 8. 实施记录（2026-06-11，P1+P2 落地）

**代码落点**：

| 模块 | 文件 |
|---|---|
| 状态机/锁/reserve/claim（纯逻辑） | `gateway/chunked_upload_store.py` |
| R1-R6 路由（CSRF/auth/流式接收） | `gateway/chunked_upload_api.py` |
| TTL sweeper（loop + JSONL 审计） | `gateway/chunked_upload_sweeper.py` |
| admin 10 字段 + validators | `gateway/admin_settings.py`（`chunked_upload_*`） |
| opaque ref 解析 + claim 回写 | `gateway/job_intercept.py::intercept_create_job` |
| 接线（router + sweeper 启停） | `gateway/main.py` |
| 前端切片/续传/进度 | `frontend-next/src/lib/upload/chunkedUpload.ts` |
| Web Worker 增量哈希（hash-wasm） | `frontend-next/src/lib/upload/sha256.worker.ts` |
| 选路接入 | `frontend-next/src/components/workspace/TranslationForm.tsx` |
| admin 设置页（interface/DEFAULT/UI 段） | `frontend-next/src/app/(app)/admin/settings/page.tsx` |
| 测试（70 条） | `tests/test_chunked_upload_{store,api,sweeper,create_intercept,admin_sync_guard}.py` |

**实施偏离（设计真值以此为准）**：

1. **R2 片长度语义收紧**：plan 原文 "Content-Length ≤ chunk_size（末片 ≤ 余量）"；
   实现收紧为**必须等于协议长度** `min(chunk_size, size - n*chunk_size)`——合并偏移
   由 chunk_size 固定推导，接受短片会破坏合并完整性。超长 413 `part_too_large`，
   不足 422 `part_size_mismatch`，缺 Content-Length 411，缺/非法 `X-Chunk-SHA256` 422。
2. **expired / aborted 不作为落盘状态**：二者是"清盘"动作（目录直接删除），
   state.json 不存在这两个值；`failed` 实名为 `failed_integrity`（仅全文件哈希不符
   进入）；merge 瞬时 IO 错回 `receiving` + `failure_reason`，可重试 complete。
3. **kill-switch 作用面**：`enabled=False` 时 R1/R2/R3 同形 404（止住新增字节与
   合并 IO）；R4 status / R5 delete 保留（进行中客户端查询/清理）；R6 恒 200。
   sweeper 不看开关（关停期磁盘残留照样回收）。
4. **每日配额只做 GB 维度**：`uploads/_chunked/_usage/{YYYY-MM-DD}/{user_id}.json`，
   北京时间日界、声明即计（abort 不退，同 express daily cap 口径）；"次数"维度
   未单设（per_user_active + daily GB 已覆盖滥用面）。usage 目录 7 天后 sweeper 清。
5. **R6 阈值非旋钮**：`threshold_mb=95` 是 gateway 常量
   （`SINGLE_REQUEST_THRESHOLD_MB`），CF 边缘 100MB 的固定余量，无 admin 字段。
6. **前端在 enabled=false 时不硬拦大文件**：>95MB 回落现有单请求路径（保持旧行为，
   非 CF 部署仍可用；CF 部署由边缘 413）。"隐藏入口"语义实现为"不启用分片"，
   上传输入框本身不隐藏。
7. **硬边界兜底**（plan 未明示）：多片上传 chunk_size ≥ 1MB；total_parts ≤ 4096。
8. **claim 失败 best-effort**：upstream 成功后 claim 写回失败只 log WARNING，
   不回滚任务（最坏情形 ready 文件被 sweeper 按 ready_ttl 回收）。
9. **complete 整段持锁线程化**：锁+合并+校验+改名整体 `asyncio.to_thread`，
   2GB 合并不阻塞 gateway 事件循环。

**未做（按 plan 非目标）**：匿名档分片、≤95MB 路径替换、OSS 附录 A、H1 存量
local_video 归属校验（单独任务）、§4-P3 成功率 metering JSONL（灰度开启后补）。

## 9. 匿名档分片扩展（B 方案，2026-06-12 立项，**待 CodeX 评审后实施**）

> 背景：注册档分片（§3-§8）已上线 prod 后，实测确认主漏斗痛点在**匿名试用入口**
> （`anonymous_preview_max_upload_mb=200` 但 CF 边缘 100MB 先拦，100-200MB 文件
> 过前端校验、死在边缘 →「网络错误」）。项目主决策：匿名档开放分片，§2 原非目标
> 第一条作废。实施前先跑 CodeX 方案评审锁定 §9.6 开放点。

**设计原则：分片只是传输层替换。** 合并产物进入与现有 `POST /gateway/
anonymous-preview/upload` **完全相同**的 intake 管线（`run_intake_and_save`），
prescreen/probe/配额权威计数/record/create 流零改动；前端 complete 后拿到的
响应与现有 /upload 同形（`{preview_id, status, status_reason, mode,
admission_decision}`），后续轮询/create 不感知传输方式。

### 9.1 路由（挂 anonymous-preview 命名空间：`/gateway/anonymous-preview/chunked/*`）

| # | Method/Path | 与注册档（§3.1）的差异 |
|---|---|---|
| A1 | `POST /chunked/init` | 身份 = `get_or_create_anonymous_session`（CSRF 同 /upload 手动 try/except）；gate 三与门：env `enable_anonymous_preview` AND admin `anonymous_free_preview_enabled` AND admin `chunked_upload_anonymous_enabled`（任一关 → 同形 404）；**AD-8 peek 预检**（global/per-IP cap，同 /upload，上传前拦免浪费磁盘）；size ≤ `anonymous_preview_max_upload_mb`（**200MB，不是 2GB**）；per-session active ≤ 1（硬编码，不设旋钮）；磁盘 reserve 同 §3.4（in-flight 汇总与注册档**共享** global_inflight / disk_floor 预算）；**不设匿名每日 GB 旋钮**（200MB × per-IP cap 3 已自然封顶） |
| A2 | `PUT /chunked/{id}/part/{n}` | 同 R2（X-Chunk-SHA256 必带、流式、超量断流）；ownership 按匿名 session 隔离，同形 404 |
| A3 | `POST /chunked/{id}/complete` | 合并校验后**不停留 ready**：直接以终文件走 /upload 同款 intake（probe_fn/prescreen_fn/run_intake_and_save + ORM audit 路径持久化 + admission），成功返回 /upload 同形响应。**匿名档没有 ready 滞留态 / claim 闭环 / opaque ref**（§3.10 不适用）——complete 即消费，intake 失败删终文件返对应错误码 |
| A4/A5 | status / DELETE | 同 R4/R5，身份换匿名 session |
| A6 | `GET /chunked/limits` | `{enabled(三与门), threshold_mb, max_file_mb=anonymous_preview_max_upload_mb, chunk_mb}` |

### 9.2 store 复用

`chunked_upload_store` 的身份参数本就是字符串键：匿名传 `anon:{session_id_hash}`
（`_safe_segment` 已处理特殊字符）。store 核心零改动；续传四元组键含身份 →
匿名续传依赖同一 `avt_anon` cookie（清 cookie 丢续传，可接受）。

### 9.3 intake 接线（实施时以 anonymous_preview_upload.py 真实落点为准）

- merged 终文件产出到 `handle_anonymous_upload` 的同款落点约定；`source_hash` =
  已有的全文件 sha256；`byte_length` = declared_size。
- intake 在 complete handler 内 `asyncio.to_thread` 跑，**必须沿用 /upload 的
  `_run_sync` + 显式 `sync_db.commit()` 契约**（漏 commit = 静默回滚 →
  /create 恒 404，/upload 注释里的 2026-06-11 教训）。
- Set-Cookie 搬运（avt_anon）同 /upload 尾部手动 append 教训。

### 9.4 admin 旋钮

新增 `chunked_upload_anonymous_enabled: StrictBool = False`（独立熔断，与注册档
`chunked_upload_enabled` 互不影响；三处同步 + 守卫测试扩展）。清扫复用现有
sweeper（匿名目录形态一致）；TTL 是否对匿名单独收紧（如 6h）→ §9.6 交评审。

### 9.5 前端

试用弹窗（`NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW` 入口面板，实施时定位组件）：
>threshold 且 A6 `enabled` → 复用 `chunkedUpload.ts`（端点前缀参数化：注册档
`/gateway/uploads/chunked` vs 匿名 `/gateway/anonymous-preview/chunked`）+
sha256 哈希 worker；complete 直接拿 preview_id 接现有轮询。fetch 已带
`credentials: include`（avt_anon cookie）。

### 9.6 滥用面差异（CodeX 评审重点，开放点）

1. 身份可重置（清 cookie 即新会话）→ 防滥用锚点是 **AD-8 per-IP/global cap**
   （init 预检 + intake 权威计数）+ 磁盘 reserve + 单会话 active=1。评审问题：
   per-IP cap 3 是否足以约束"init 占磁盘不 complete"的并发面？
2. init **不计** APF 每日次数（上传失败不浪费配额）；权威计数仍在 intake
   （complete）一次——与现状"上传成功才计"语义一致。评审确认无双计/漏计。
3. 未 complete 的匿名分片占磁盘 → TTL 24h（注册档旋钮）对匿名是否太长？
   建议匿名沿用同一 TTL 起步，评审定是否加专用旋钮（如默认 6h）。

### 9.7 测试矩阵增量

匿名身份隔离（不同 anon session 同形 404）/ 三与门 gate（任一关 404）/
AD-8 预检 429 / complete→intake 全链（mock intake，校验 commit 契约与失败清理）/
无 avt_anon cookie 401 / 200MB 上限 413 / 注册档回归（互不影响）。

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

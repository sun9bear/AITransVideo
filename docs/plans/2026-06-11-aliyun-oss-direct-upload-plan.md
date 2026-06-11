# 阿里云 OSS 大文件直传通道方案（2026-06-11）

> 状态：**待外部审核**（CodeX）。本文档是完整设计真值；实施前任何偏离须回写本文。

## 1. 背景与问题（已实证）

- 生产公网唯一入口 = Cloudflare Tunnel（源站 443/80 实测不可达，2026-06-11 探测）。
- **Cloudflare 免费计划对请求体有 100MB 硬上限**。2026-06-11 实测：120MB POST 到
  `/gateway/anonymous-preview/upload` 被 CF 边缘直接 413（响应签名 cloudflare，
  Caddy/gateway 全程无痕迹）。
- 用户实际被卡：394.5MB 视频上传两次失败，前端只显示"网络错误"（admin 已把应用层
  上限调到 1024MB，但基础设施天花板是 100MB，应用旋钮无法突破）。
- **影响面**：匿名预览上传 + 注册用户本地视频上传走同一域名，全部受 100MB 限制。
  注册/付费用户传不了大视频是真实产品缺口。

## 2. 目标 / 非目标

**目标**
- 注册用户本地视频上传支持 **≤2GB**（上限做成 admin 旋钮，OSS 自身能力远超此值）。
- 上传体验：分片直传 + 断点续传 + 进度条（中国大陆用户走 OSS 国内链路，速度优）。
- 保住 pipeline **本地路径契约**：管线只见美国主机本地文件，OSS 对 pipeline 不可见
  （对齐 memory `feedback_deployment_plan_pitfalls` 第 3 条红线）。

**非目标**
- 匿名免费预览不走 OSS（免费样本调回 ≤95MB 走现有 CF 路径即可，防滥用面更小）。
- 不动下载/交付路径（R2 切面不变），不动 `前端零感知 R2` 守卫覆盖的任何约束。
- v1 不统一所有上传到 OSS：≤95MB 仍走现有 CF 直传（少一次跨境回拉，省 ¥0.5/GB）；
  仅 >95MB 走 OSS 通道。前端按文件大小自动选路。

## 3. 现有资产盘点

| 资产 | 位置 | 复用方式 |
|---|---|---|
| OSS 账号 + AccessKey | 生产 `.env`：`AVT_COSYVOICE_OSS_ACCESS_KEY_ID/SECRET/BUCKET/ENDPOINT/REGION`（为 CosyVoice 样本中转配置，默认 region cn-beijing） | **不直接复用 key**——它是服务端长期凭证。本方案需要为浏览器签发 STS 临时凭证，应新建独立 RAM 角色（§5）。账号/计费体系复用 |
| S3-compatible uploader 实现 | 未合并分支 `codex/cosyvoice-aliyun-oss-uploader` 的 `gateway/cosyvoice_clone/sample_uploader.py`（229 行，含 endpoint/超时/错误处理模式） | 回拉 worker 的 OSS client 写法参考；不直接合并该分支 |
| 阿里云运维经验 | 武汉 CosyVoice worker ECS 同账号体系 | 控制台操作熟路 |

## 4. 架构设计

```
浏览器(中国)                         Gateway(US, 经CF Tunnel)               OSS(cn-beijing)
   │ ① POST /gateway/uploads/oss/sts ──→ 鉴权+限频 → STS AssumeRole
   │ ←─ 临时凭证(15min,仅限前缀PutObject) ┘
   │ ② OSS JS SDK multipart 直传(10-20MB/片,并发,断点续传) ─────────────→ uploads/{uid}/{uuid}/src.mp4
   │ ③ POST /gateway/uploads/oss/complete ─→ 校验声明(key/size/sha256) → 入回拉队列
   │                                        ④ 回拉 worker: GetObject → 落盘
   │                                           uploads/ 本地(沿用现有 native
   │                                           upload 落盘/命名约定) → 校验
   │                                           sha256 → DeleteObject
   │ ⑤ 轮询 /gateway/uploads/{id}/status ←─ ready + 本地 upload ref
   │ ⑥ 用 upload ref 走【现有】job 创建流（pipeline 零改动）
```

**关键决策**
- **D1 STS 而非 AccessKey 进前端**：浏览器只见 15 分钟过期、仅能 `oss:PutObject` 到
  `uploads/{user_id}/{uuid}/` 单一前缀的临时凭证。长期 key 永不出 gateway。
- **D2 回拉而非挂载**：pipeline 契约 = 本地文件路径。回拉 worker 是唯一 OSS 读取方，
  落盘后 OSS 对象即删（双保险：bucket 生命周期 24h 兜底删 `uploads/` 前缀）。
- **D3 complete 不信客户端**：客户端只声明 key；gateway 用服务端凭证 HeadObject 校验
  实际 size/存在性，sha256 在回拉后本地复算比对。声明不符 → 拒绝 + 删对象。
- **D4 双路径按大小自动选**：`文件 ≤95MB → 现有 CF 直传`；`>95MB → OSS 通道`。
  阈值与 2GB 上限均为 admin 热旋钮（沿用 2026-06-11 APF 旋钮同一套 admin_settings 机制）。
- **D5 endpoint 不硬编码进前端 bundle**：STS 响应携带 endpoint/bucket/前缀，前端动态读。
  （类比"前端零感知 R2"精神；该守卫本身只扫 R2 字样，OSS 不触发，但同样理由适用。）

## 5. 安全清单（外审重点）

- [ ] **新建独立 bucket**（建议 `avt-user-uploads`，区别于 CosyVoice 样本 bucket）：
      私有读写、关闭公共访问、仅开 CORS 给 `https://aitrans.video`（PUT/POST，
      暴露 ETag header——分片上传必需）。
- [ ] **新建 RAM 角色 + 最小 STS policy**：仅 `oss:PutObject`/`oss:AbortMultipartUpload`
      resource 限定 `acs:oss:*:*:avt-user-uploads/uploads/${user_id}/*`；签发时注入
      user_id 到 policy（per-session 收窄），15min 过期。
- [ ] **签发端点防滥用**：登录态必须；per-user 限频（次/日 + 总 GB/日，admin 旋钮）；
      审计 JSONL 落 `runtime_logs/`（who/when/前缀/配额消耗）。
- [ ] **回拉前校验**：HeadObject size ≤ admin 上限；本地落盘后 sha256 比对 + ffprobe
      预检沿用现有上传后置校验链。失败 → 删对象 + 记审计 + 用户可见错误。
- [ ] **凭证卫生**：长期 AccessKey 仅存 gateway env；STS 凭证不落任何日志；
      守卫测试：AST 扫前端 bundle 源码不得出现 AccessKeyId 字面量/长期 key 模式。
- [ ] **生命周期规则**：`uploads/` 前缀 24h 过期删除 + `AbortIncompleteMultipartUpload`
      1 天（防半截分片堆积计费）。
- [ ] 付费约束定位：OSS 属**基础设施成本**（同 R2 先例，见 memory
      `feedback_paid_api_constraint_scope`），不触付费 API 硬约束；但回拉流量有真实
      成本 → per-user/全局配额旋钮即成本红线（§6）。

## 6. 成本模型（中国内地区域标准价，以控制台计费页为准）

| 计费项 | 单价 | 2GB 一次 |
|---|---|---|
| 上行（用户→OSS） | 免费 | ¥0 |
| 存储（标准型，瞬态 <24h） | ~¥0.12/GB/月 | ≈¥0.00 |
| **公网流出（OSS→美国主机回拉）** | **~¥0.50/GB** | **~¥1.00** |
| 请求 | ¥0.01/万次 | 忽略 |

- 规模估算：50 个 2GB/天 ≈ ¥50/天；对比单任务 AI 成本（ASR+翻译+TTS）占比小。
- **成本守卫**：per-user 每日上传 GB 配额 + 全局每日 GB 配额（admin 旋钮，超额 fail-closed
  拒签 STS）≈ 跨境流量费日上界 = 全局配额 × ¥0.5/GB。
- 传输加速**先不开**（额外 ~¥0.5-1.25/GB）：回拉是后台行为，慢 1-3 分钟用户无感；
  实测公网链路不够再议。

## 7. 分期实施

- **P1 后端通道**（gateway）：STS 签发端点 + complete/status 端点 + 回拉 worker
  （asyncio 任务，沿用 reservation sweeper 模式）+ admin 旋钮（阈值/上限/配额）
  + 审计 + 全部单测。隐藏灰度：仅 admin 账号可签 STS。
- **P2 前端**：上传组件分路逻辑 + OSS JS SDK 分片直传 + 进度/断点续传 UI + 失败回退
  提示（不自动回退到 CF 路径——大文件回 CF 必死，明确报错）。
- **P3 评估**：灰度数据（成功率/回拉时延/成本）→ 决定是否放开全部注册用户 + 是否
  下调双路径阈值统一走 OSS。

## 8. 测试计划

- 单测：STS policy 模板（前缀注入/过期）、签发限频 fail-closed、complete 校验矩阵
  （size 超限/key 越前缀/对象不存在）、回拉幂等（重复 complete 只拉一次）。
- 集成：fakes 跑全链（签发→mock OSS→回拉→落盘→job 创建 ref 可用）。
- e2e（用户显式触发，真实流量费 ~¥1）：真传一个 >100MB 视频走完整漏斗到 job 创建。
- 守卫：前端 bundle 无长期凭证模式；回拉 worker 不被 pipeline import（路径契约隔离）。

## 9. 开放问题（外审请给意见）

1. bucket region：cn-beijing（与现有一致）vs cn-shanghai/就近——用户上传体验差异小，
   建议沿用 cn-beijing 降低运维面。
2. 回拉失败重试策略：建议 3 次指数退避后置 failed + 用户可重新触发 complete，
   不自动无限重试（跨境链路抖动场景）。
3. 匿名档未来是否接入：倾向永不（滥用面/成本面均不划算），免费档保持 ≤95MB。
4. 是否同时给武汉 CosyVoice worker 的样本中转切到同一 bucket 体系——不在本方案范围，
   但 RAM 角色设计预留命名空间（`uploads/` vs `cosyvoice/` 前缀已天然隔离）。

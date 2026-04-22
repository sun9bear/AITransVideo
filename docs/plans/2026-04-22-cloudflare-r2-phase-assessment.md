# Cloudflare + R2 方案落实评估报告

- 评估日期：2026-04-22
- 评估范围：
  - [2026-04-21-cloudflare-r2-deployment-plan.md](../plans/2026-04-21-cloudflare-r2-deployment-plan.md)
  - [2026-04-21-phase01-implementation-checklist.md](../plans/2026-04-21-phase01-implementation-checklist.md)
  - [2026-04-22-phase01-rollout-notes.md](../plans/2026-04-22-phase01-rollout-notes.md)
- 核实维度：
  - 仓库文档与配置
  - 当前生产入口 `https://aitrans.video`
  - 美国主机 `5.78.122.220` 运行态

## 1. 执行摘要

结论分三层：

1. `Phase 1` 已经基本落地，`aitrans.video` 当前确实通过 Cloudflare Tunnel 对外提供服务，US 主机公网入站也已经被 `ufw` 收紧到只留 `22/tcp`。
2. `Phase 1` 的实际落地口径与计划文档存在明显漂移，最重要的是：
   - 计划主域名口径是 `app.yourdomain.com`，实际生产口径是 `aitrans.video`
   - 计划里 v4 倾向 `cloudflared -> 3000/8880` 直连 upstream，实际落地是 `cloudflared -> https://localhost:443 -> Caddy -> Next/Gateway`
3. `Phase 0` 没有形成完整、可放行的探针闭环；`Phase 2 / 3 / 5` 中的 R2 下载、R2 上传、R2 备份、TTL 清理、监控等主体能力仍停留在方案层，没有形成对应的生产代码和运行态。

一句话判断：

- 这份方案目前的真实完成度是：`Phase 1 已落地但需要补硬化`，`Phase 0 证据不足`，`R2 主体方案尚未进入实装阶段`。

## 2. 核实方法

本次评估实际核对了以下内容：

- 阅读方案、检查清单和 rollout 记录。
- 抽查仓库中的 [docker-compose.yml](../../docker-compose.yml)、[Caddyfile](../../Caddyfile)、[RUN_ENVIRONMENT.md](./RUN_ENVIRONMENT.md)、[scripts/phase0_probes](../../scripts/phase0_probes)。
- 实测 DNS：
  - `aitrans.video` 解析到 Cloudflare 边缘 IP
  - `api.aitransvideo.com` 仍解析到 `expired.hichina.com`
- 实测线上入口：
  - `GET https://aitrans.video/gateway/health` 返回 `{"status":"ok","auth_required":true}`
- SSH 抽查 US 主机：
  - `docker ps`
  - `ufw status verbose`
  - `ss -ltnp`
  - `curl http://127.0.0.1:20241/config`
  - `caddy validate`
  - `crontab -l`
  - `/opt/aivideotrans/scripts/pg_backup.sh`
  - `/opt/aivideotrans/caddy/phase14_rollback.sh`

## 3. 计划项落实状态

### 3.1 总体状态表

| 项目 | 计划口径 | 当前状态 | 结论 |
|---|---|---|---|
| Phase 0 探针 | 三类探针都要完成并写入基线 | 只有脚本与部分基线文本，未形成完整放行证据 | 部分完成 |
| Phase 1 Tunnel 切流 | 通过 Cloudflare Tunnel 隐藏 US 源站 | 已落地 | 基本完成 |
| Caddy 降级 | 仅 loopback + `tls internal` | 已落地 | 完成 |
| UFW 收口 | 仅保留 SSH | 已落地 | 完成 |
| Defense in depth | 服务本身改绑 `127.0.0.1`，metrics 改 loopback | 未完成 | 未完成 |
| R2 下载链路 | 302 到 R2 预签名 URL | 未发现生产实现 | 未开始 |
| R2 上传链路 | 浏览器 multipart 直传 R2 | 未发现生产实现 | 未开始 |
| R2 备份 | `pg_dump -> gzip -> R2` | 仅本地磁盘备份 | 部分完成 |
| 监控 | Uptime Kuma + 告警 | 未落地 | 未完成 |
| 回滚预案 | 有脚本且能回退 | 脚本存在，源文件也在 | 基本完成 |

### 3.2 Phase 0：探针与基线

已落实的部分：

- 仓库里已经有完整探针脚本目录：
  - [scripts/phase0_probes/README.md](../../scripts/phase0_probes/README.md)
  - [scripts/phase0_probes/probe1_baseline.sh](../../scripts/phase0_probes/probe1_baseline.sh)
  - [scripts/phase0_probes/probe2_r2_download.sh](../../scripts/phase0_probes/probe2_r2_download.sh)
  - [scripts/phase0_probes/probe3_r2_upload.sh](../../scripts/phase0_probes/probe3_r2_upload.sh)
  - [scripts/phase0_probes/generate_download_url.py](../../scripts/phase0_probes/generate_download_url.py)
- 方案的 `§15` 已填入一部分基线数据，至少包含：
  - 中国电信直连基线
  - R2 原生域名的部分下载稳定性样本
  - `17ce.com` 覆盖测试补充说明

未落实或证据不足的部分：

- `联通 / 移动` 两链路的数据仍未在核查到的文档中完整补齐。
- 没有发现“探针执行完成”的统一记录页，也没有一个最终的 Phase 0 放行结论页。
- 生产环境 `.env` 中没有任何 `R2_*` 或 `AVT_STORAGE_BACKEND` 标记，说明 Phase 0 探针数据并没有自然过渡成后续 R2 上线配置。

判断：

- `Phase 0` 不是完全没做。
- 但它目前更像“脚本和部分样本已准备/已做”，还不是“足够支撑进入 Phase 2 的闭环证据”。

### 3.3 Phase 1：Tunnel 切流与源站隐藏

已核实为完成的事项：

- `aitrans.video` 当前通过 Cloudflare 边缘提供服务。
- 美国主机上存在并运行：
  - `aivideotrans-cloudflared-us`
  - `aivideotrans-caddy`
  - `aivideotrans-next`
  - `aivideotrans-gateway`
  - `aivideotrans-postgres`
- `ufw` 已启用，默认策略是：
  - `deny incoming`
  - `allow outgoing`
  - 仅放行 `22/tcp`
- 实测监听：
  - `127.0.0.1:443` 为 Caddy
  - `0.0.0.0:3000` 为 Next
  - `0.0.0.0:8880` 为 Gateway
  - `0.0.0.0:5432` 为 Postgres
  - `*:20241` 为 cloudflared metrics
- `cloudflared` 当前 ingress 实际配置为：
  - `hostname = aitrans.video`
  - `service = https://localhost:443`
  - `originServerName = localhost`
  - `noTLSVerify = true`
- Caddy 当前配置确实是：
  - `127.0.0.1:443, localhost:443, aitrans.video:443`
  - `bind 127.0.0.1`
  - `tls internal`

判断：

- `Phase 1` 在“业务可用、源站对公网隐藏”这个核心目标上已经达成。

### 3.4 Phase 1 的计划漂移

这里是最需要纠偏的部分。

计划文档的主要口径：

- `app.yourdomain.com` 作为单域名入口
- `status.yourdomain.com` 作为监控入口
- `cloudflared` 直连 `localhost:3000 / localhost:8880`
- 后续 Phase 2/3 围绕这一口径继续展开

实际落地口径：

- 对外主域名使用的是 `aitrans.video`
- `cloudflared` 只发布了一个 `aitrans.video` route
- 当前回源不是直连 `3000/8880`，而是经 `https://localhost:443`
- `status` 域名和 `3001` 监控入口没有落地

这意味着：

- 当前生产是“Plan v4 的一个变体”，不是原文照做。
- 如果后续仍按原方案直接推进，容易继续把 `app.yourdomain.com`、`status.yourdomain.com`、`直连 upstream` 当成已存在前提，造成误操作。

### 3.5 Defense in depth：只做了半层

当前实际风险点：

- `Next` 仍监听 `0.0.0.0:3000`
- `Gateway` 仍监听 `0.0.0.0:8880`
- `Postgres` 仍监听 `0.0.0.0:5432`
- `cloudflared metrics` 仍监听 `*:20241`

虽然 `ufw` 现在挡住了公网，但这仍然属于“防火墙外层保护”，不是服务自身最小暴露。

如果哪天：

- `ufw` 被误关
- 云厂商安全组策略变更
- 新增了额外端口放行

这些进程会立即暴露。

因此 rollout notes 里把这部分列为“下一个专项任务”是对的，这部分不能再拖。

### 3.6 回滚能力

已经核实存在：

- `/opt/aivideotrans/caddy/phase14_rollback.sh`
- `/tmp/Caddyfile.original`

这意味着回滚资产基本在位。

但仍有一个明显不足：

- `/tmp/Caddyfile.original` 是临时目录文件，主机重启后可能消失。

所以当前状态应判定为：

- `回滚脚本基本完成`
- `回滚源仍需持久化一份到 /opt/aivideotrans/caddy/`

## 4. R2 相关能力落实情况

### 4.1 当前结论

R2 方案主体能力还没有进入生产代码。

### 4.2 证据

仓库内已经存在：

- 方案文档中大量 R2 设计
- `scripts/phase0_probes/*` 探针脚本

但没有在运行主路径中看到这些关键实现的实际落地痕迹：

- `AVT_STORAGE_BACKEND`
- `publish_artifacts_to_r2`
- `backfill_legacy_artifact`
- R2 预签名下载主链路
- `force_local=1` 运维开关

生产 `.env` 抽查结果也没有出现：

- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_*_BUCKET`

因此当前不能把方案说成“Cloudflare + R2 已经上线”，准确说法应是：

- `Cloudflare Tunnel 已上线`
- `R2 方案仍在文档和探针阶段`

## 5. 备份与监控

### 5.1 备份

已存在能力：

- 仓库有 [scripts/pg_backup.sh](../../scripts/pg_backup.sh)
- US 主机 crontab 存在每日 03:00 任务：
  - `/opt/aivideotrans/scripts/pg_backup.sh >> /opt/aivideotrans/backups/backup.log 2>&1`
- US 主机 `/opt/aivideotrans/backups/` 下已有连续多日的 `.sql.gz`

未达到计划口径的地方：

- 当前备份仅保存在主机本地目录
- 没有推送到 R2
- 没有看到异地/异机保留

判断：

- 备份不是空白
- 但它是“本地备份已做”，不是计划里的“R2 异地备份已落地”

### 5.2 监控

计划口径：

- `Uptime Kuma`
- `status.yourdomain.com`
- 告警推送

实际核查：

- 当前 `docker ps` 中没有 `uptime-kuma`
- 也没有 `3001` 监听
- `cloudflared` 只有 `aitrans.video` 这一个 ingress route

判断：

- 监控项目前基本未落实

## 6. 额外发现

### 6.1 `api.aitransvideo.com` 仍是失效状态

当前 DNS：

- `api.aitransvideo.com -> expired.hichina.com`

这不是本次 Cloudflare + R2 计划的主路径，但它是一个明显的对外脏状态，容易带来：

- 运维误判
- 第三方回调误填
- 文档和真实入口混淆

建议：

- 要么恢复它并纳入 Cloudflare/Tunnel 体系
- 要么彻底停止在文档和后台配置中引用它

### 6.2 线上营销站整改已生效，但与本方案是并行事项

实测 `https://aitrans.video/contact`、`/pricing` 可见：

- 新主体 `武汉市江岸区鑫鑫图文服务部`
- 新地址 `武汉市江岸区二七街黄家墩江站80户1-1-2`
- 支付/购买说明

这对支付宝审核有帮助，但它不是 Cloudflare + R2 计划本身的完成证据，需要与基础设施推进分开管理。

## 7. 最终评估

### 7.1 完成度判断

按计划阶段拆分：

- `Phase 0`：`40%`
  - 有探针脚本和部分基线
  - 但没有完整三网结果、没有形成放行文档闭环
- `Phase 1`：`75%`
  - 核心链路已经跑通
  - 但与计划有口径漂移，且硬化任务未完成
- `Phase 2+`：`0%~10%`
  - 主要停留在文档设计
  - 尚未进入完整代码实现和生产配置

### 7.2 是否可以说“方案已完成”

不能。

准确说法应该是：

- `Cloudflare Tunnel 切流 + Caddy loopback + UFW 收口` 已经完成
- `Cloudflare + R2 整体方案` 尚未完成

## 8. 建议与下一步推进方式

### 8.1 第一优先级：先做 Phase 1.5 硬化，不要直接跳 Phase 2

建议先补这 5 项：

1. 把 `Next/Gateway/Postgres` 改绑到 `127.0.0.1`
2. 把 `cloudflared --metrics` 改成 `127.0.0.1:20241`
3. 把 `/tmp/Caddyfile.original` 持久化到 `/opt/aivideotrans/caddy/Caddyfile.git-head`
4. 明确写死当前生产口径：
   - 主域名就是 `aitrans.video`
   - 当前 tunnel 走 `localhost:443 -> Caddy`
5. 处理 `api.aitransvideo.com`
   - 恢复纳管，或彻底弃用

这一步完成后，Phase 1 才能算真正稳定。

### 8.2 第二优先级：把 Phase 0 补成可决策证据

建议输出一份单独的“探针结果归档”文档，至少包含：

- 电信 / 联通 / 移动 三链路完整数据
- Tunnel 访问结果
- R2 下载稳定性
- R2 multipart 上传结果
- 进入 Phase 2 / 3 的放行结论

否则 Phase 2 会变成“没有基线门槛的继续施工”。

### 8.3 第三优先级：把 R2 方案拆成最小闭环，不要大包推进

建议 Phase 2 只做以下最小闭环：

1. `r2_client` 与生产 `.env` 接线
2. `AVT_STORAGE_BACKEND=r2|local`
3. 成品下载 302 到 R2 预签名 URL
4. `force_local=1` 紧急兜底
5. 只覆盖最核心下载路径，不同时做上传直传、TTL、回填、备份全套

原因：

- 当前 Phase 1 还没完全收尾
- Phase 0 也没有完整证据
- 一口气推进 Phase 2/3/5，风险会叠在一起

### 8.4 第四优先级：监控与备份补齐

建议顺序：

1. 先上最小可用监控
   - `Uptime Kuma` 或等价探活
   - tunnel / health / checkout / webhook 至少 4 个监控点
2. 再把 PG 备份从“本地目录”升级到“异地保存”
   - 首选 R2
   - 保留本地 7 天 + R2 30 天

## 9. 推荐推进路线

推荐下一步按这个顺序推进：

1. `Phase 1.5 硬化专项`
2. `Phase 0 探针结果归档 + 放行结论`
3. `Phase 2 最小闭环：R2 下载 302`
4. `Phase 2b / Phase 3` 是否继续，等真实探针数据再定
5. `监控 + 异地备份`

如果只选一件事现在就做，优先做：

- `Phase 1.5 硬化专项`

因为它同时降低现网风险，并且不会把后续 R2 方案建立在不够稳的基础上。

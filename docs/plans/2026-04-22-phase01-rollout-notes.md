# Phase 1 §13-§15 落地实录（2026-04-22）

- 关联方案：[2026-04-21-phase01-implementation-checklist.md](2026-04-21-phase01-implementation-checklist.md) §13 §14 §15
- 目标：把 Caddy 从"公网 ACME LE 直服"降级为"只在 loopback 讲 tls internal"，Cloudflare Tunnel 做公网入口，顺带修掉 US 上的若干 0.0.0.0 裸奔端口
- 结果：`https://aitrans.video` 仍 200，US 公网只剩 22/tcp 可达，撤退脚本就位
- 涉及 commit：`0abdc08` phase1(§14) 的 Caddyfile

---

## 最终拓扑（本次落地后）

```
Client
  └─ CF Edge (aitrans.video, LE 证书 CF 全托管)
      └─ cloudflared-us tunnel (outbound only, network_mode: host)
          └─ Caddy @ 127.0.0.1:443 (tls internal, Caddy Local Authority)
              ├─ Next.js :3000
              └─ Gateway :8880  → Postgres :5432
```

US 公网 (5.78.122.220) 只有 22/tcp 可达；80/443/3000/5432/8880 全部被 ufw DROP。

---

## cloudflared Public Hostname 必需配置

在 CF Zero Trust → Networks → Tunnels → ATV-us → Published application routes → `aitrans.video` → Edit：

| 字段 | 值 | 不能乱改 |
|---|---|---|
| Hostname | `aitrans.video` | 和生产域名一致 |
| Service Type | `HTTPS` | Caddy loopback 发 cert |
| URL | `localhost:443` | 必须用 `localhost`，对应 Caddy site key |
| Origin Server Name | `localhost` | 对应 Caddy 内部 CA 签的 cert SAN |
| No TLS Verify | **ON** | 内部 CA 不被公网信任，必须关校验 |
| Match SNI to Host | OFF | 用上面 Origin Server Name 做 SNI |
| HTTP2 connection | OFF | Caddy 默认 HTTP/1.1 够用 |

验证值已生效（从容器内拿）：`curl -s http://127.0.0.1:20241/config` 里看 `config.ingress[0]`。

---

## Caddyfile 落地后的样子（关键摘录）

```
{
    admin off
    # 不要加 auto_https off —— trap v1
}

127.0.0.1:443, localhost:443, aitrans.video:443 {
    bind 127.0.0.1
    tls internal
    ...
}
```

三个 site-block key 各自为：
- `127.0.0.1:443` —— Caddy 绑 loopback
- `localhost:443` —— cloudflared 的 SNI 命中
- `aitrans.video:443` —— cloudflared 的 HTTP Host header 命中（trap v2）

`bind 127.0.0.1` 强制 loopback 绑定，即使 `aitrans.video` 的 DNS 解析到 CF 边缘 IP 也不会尝试去那里监听。

`tls internal` 让 Caddy Local Authority 给上面三个名字都签自签证书，cloudflared `noTLSVerify=true` 收下即可。

---

## 踩过的两个坑（trap v1 + trap v2）

### trap v1: `auto_https off` 会同时关掉 internal CA

最初以为 `tls internal` 配合 `auto_https off` 更干净（反正不做 ACME 了），结果 Caddy 启动后 loopback TLS 握手直接 `internal error`，tunnel 502。

原因：Caddy v2 的 `auto_https off` 是**总开关**，同时关闭了 ACME 和 internal CA 的证书管理。`tls internal` 指定发行者，但拿不到证书（CA 不 mint）。

修法：**删掉 `auto_https off`**。Caddy 对纯 loopback/本地域名（`127.0.0.1`, `localhost`）不会尝试 ACME，这部分可以靠默认的 safety check 自动过滤，不需要手工关 auto_https。

### trap v2: Host=aitrans.video 不 match 任何 site block → 空 200

删掉 `auto_https off` 后 Caddy 起来了，Caddy loopback 自测 200，但 tunnel 过来依然异常：**HTTP 200 with Content-Length: 0**（空响应）。

原因：Caddy v2 site-block matcher 按 Host header 精确匹配。我原本只写了：
```
127.0.0.1:443, localhost:443 { ... }
```
cloudflared 忠实转发公网来的 `Host: aitrans.video`。Caddy 匹配不到任何 block，走 Caddy v2 的"**no site configured for this host**" 默认分支 —— 返回空 200（不是 404、不是 421）。

修法：把 `aitrans.video:443` 加到 site key 里（见上面 Caddyfile 摘录）。`tls internal` 会自动给 `aitrans.video` 也签一张内部 CA 证书，cloudflared `noTLSVerify=true` 收下。

### 排查这两个 trap 的可复用手法

从 US host 侧，把 cloudflared 要发的请求自己模拟一遍：

```bash
# 确认 SNI 能握手
curl -sk --resolve localhost:443:127.0.0.1 \
    -o /dev/null -w 'sni_localhost=%{http_code}\n' \
    https://localhost/

# 确认 Host header 能匹配 site block（trap v2 探针）
curl -sk -H 'Host: aitrans.video' \
    https://127.0.0.1:443/ \
    -o /tmp/probe.body -w 'code=%{http_code} size=%{size_download}\n'
```

如果 `size=0` 而 `code=200`，就是 trap v2；如果 code=000（握手失败），就是 trap v1 或证书不匹配。

---

## Phase §13 变体：ufw defense-in-depth（比原计划多解决 3 个问题）

原计划 §13 只说 "ufw close 80/443"。实际落地时发现：

1. US 上 `ufw status = inactive` —— 根本没启用过 firewall
2. Caddy 切到 tls internal 后本来就只绑 127.0.0.1，公网 80/443 "关闭"是自动附带的
3. **postgres:5432 / gateway:8880 / next:3000 全在 0.0.0.0 裸奔**（docker 容器虽然用 `network_mode: host`，进程 bind 就是主机 0.0.0.0，没有 DNAT 但 INPUT 仍然会过 firewall）

所以 §13 升级成：

```
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw --force enable
```

这样把上面 4 个端口一次性全挡掉。

### 安全启用的 self-heal 模式

`ufw enable` 的最大风险是把自己 SSH 踢出来。本次用 `at` 做 10 分钟自愈：

```bash
# before enable
echo "/usr/sbin/ufw --force disable" | at now + 10 minutes
# → 保存 job id，如 /tmp/ufw_selfheal.at.id

# enable + 验证（新 SSH 会话，而不是复用当前连接）
ufw --force enable

# 新会话验证 22 仍通、tunnel 仍 200 → cancel self-heal
atrm <job-id>
```

如果 `ufw enable` 之后新 SSH 连不上，10 分钟后 atd 会自动 `ufw disable` 自愈。实操完 4 分钟冗余取消 self-heal 正好。

### 为什么 docker 容器能被 ufw 挡住

经典 ufw-docker 问题（docker 用 DOCKER 链 DNAT 绕过 INPUT 链）在这个项目**不存在**，因为所有容器都用 `network_mode: host`：

```bash
$ for c in $(docker ps --format '{{.Names}}'); do
    echo "$c: $(docker inspect -f '{{.HostConfig.NetworkMode}}' $c)"
  done
aivideotrans-next: host
aivideotrans-gateway: host
aivideotrans-app: host
aivideotrans-cloudflared-us: host
aivideotrans-caddy: host
aivideotrans-postgres: host
```

`network_mode: host` 意味着：
- 容器内进程直接 bind 主机网络栈
- 没有 docker 的 `-p 5432:5432` 端口发布
- iptables 的 DOCKER / DOCKER-USER 链对这类流量空转
- 所有外部入站都老实走 INPUT 链 → 过 ufw 规则

确认方式：`iptables -t nat -L DOCKER -n` 应该为空；`iptables -L DOCKER-USER -n` 也应该为空。

---

## §15 撤退预案（已验证就绪）

### 撤退条件

出现以下任一情况立即撤退：
- cloudflared tunnel 持续 >5 分钟不可达且 CF 侧无异常
- CF 账号被封 / 被 DDoS 清算
- Phase 1 后性能不能接受（回退测基线对比）

### 撤退操作（US 侧）

```bash
sudo bash /opt/aivideotrans/caddy/phase14_rollback.sh
```

脚本做 4 件事：
1. 备份当前 tls internal 版本到 `Caddyfile.pre-rollback-<timestamp>`
2. 从 `/tmp/Caddyfile.original`（git HEAD 版，73 行）还原回来
3. `ufw allow 80/tcp; ufw allow 443/tcp`（保留 default deny，只放行这两个）
4. `docker restart aivideotrans-caddy`

### 撤退操作（CF 侧，手工）

1. Zero Trust → Networks → Tunnels → ATV-us → Published application routes → `aitrans.video` → 删除这条 public hostname（或设 `noTLSVerify=false` 不影响正确性）
2. DNS → `aitrans.video` proxied (橙云) → **DNS only (灰云)**，或把 CNAME `aitrans.video → <tunnel-id>.cfargotunnel.com` 改回 A record `aitrans.video → 5.78.122.220`

### 撤退耗时预估

- Caddy 重启 ≤ 10s
- CF DNS 改动传播 ≤ 60s（CF 自家 DNS 很快）
- LE 证书 —— 原 Caddyfile 已经有过 LE 证书，storage 在 `/data/caddy/caddy/certificates/acme-v02.api.letsencrypt.org-directory/aitrans.video/`，重用无需等新签发

总计 **≤ 2 分钟**，假设 DNS TTL 已经设短（CF 默认 Auto ≈ 5 分钟，撤退前可以提前 1 小时调到 1 分钟降低后续传播延迟）。

### 撤退预案的 dry-run 验证记录

```
=== 2026-04-22T10:07:09Z rollback dry-run check ===
/tmp/Caddyfile.original  md5=98cc32134bcce49629b5536dc6c6c49d  lines=73
caddy validate (with env injected) → Valid configuration
.env 里 AUTODUB_PUBLIC_HOST=aitrans.video, CADDY_EMAIL=<SET>
phase14_rollback.sh 已安装在 /opt/aivideotrans/caddy/ 下可执行
```

### 什么时候不要跑撤退脚本

- 单纯 tunnel 抖动 < 5 分钟 —— 等 CF 自愈
- cloudflared 容器 healthy 但 502 —— 先查 ingress `originServerName` 是否还是 `localhost`（CF dashboard 可能被别人误改）
- Caddy logs 有 `tls: no cert` —— 检查 site key 是否少了 `aitrans.video:443`（trap v2 回来）

---

## 剩余 defense-in-depth（Phase 1.5 专项）

[CodeX 阶段评估 §8.1](2026-04-22-cloudflare-r2-phase-assessment.md) 明确列为"推 Phase 2 之前必做"的硬化项。

1. **postgres / next / uvicorn bind 127.0.0.1**（修 `docker-compose.yml`）：
   - Postgres 在 docker-compose.yml 里 `command: postgres` 后加 `-c listen_addresses=127.0.0.1,::1`，或在 `environment:` 里用 `POSTGRES_HOST_AUTH_METHOD` + bind
   - Next.js 的 `CMD ["node", "server.js"]` 改成 `node server.js --hostname 127.0.0.1`（或 env `HOSTNAME=127.0.0.1`）
   - Gateway uvicorn 启动命令 `--host 0.0.0.0` 改成 `--host 127.0.0.1`
   - 修完后 ufw 即便失效也不会泄露 —— 真正的 defense in depth
   - 前置：全项目 grep 确认没有容器间跨主机调用，应该都走 `127.0.0.1` 或 `localhost`

2. **cloudflared 的 metrics 端口 20241**：目前 bind `*:20241`，如果 ufw 失效也会泄露（会泄漏 tunnel 配置！），应在 docker-compose.yml 里 `--metrics 127.0.0.1:20241`。

3. ~~**`/tmp/Caddyfile.original` 持久化**~~ — ✅ 已完成（见下方"§15.4 撤退演练结果"）

4. **DNS TTL** 调到 1 分钟（CF dashboard → DNS → aitrans.video 那行的 TTL 下拉），撤退时生效更快。

5. ~~**`api.aitransvideo.com` 处理**~~ — **这个子域不是本项目拥有的域名**。
   用户本人澄清：从未持有 `aitransvideo.com`（.com 系列），本项目唯一域名是
   `aitrans.video`（.video TLD）。CodeX 阶段评估 §6.1 的观察是对一个**与本项目
   无关的外部域名**的描述，不属于我方任何配置 / 回调 / ingress / 文档口径，
   无需处理。所有后续提到"清理 api.aitransvideo.com"的条目均可忽略。

6. **监控**：CF dashboard → Analytics → Traffic 设一个"tunnel 故障告警"；或 US 本地 cron 每分钟 curl tunnel 失败 3 次发 webhook。

---

## 关键产物清单（US 上的稳定路径）

| 路径 | 作用 |
|---|---|
| `/opt/aivideotrans/caddy/Caddyfile` | 当前运行版（tls internal） |
| `/opt/aivideotrans/caddy/Caddyfile.bak-pre-v14-*` | 每次 deploy 自动 snapshot |
| `/opt/aivideotrans/caddy/Caddyfile.pre-rollback-*` | 撤退演练 / 真实撤退前的自动 snapshot（rollback.sh 第 1 步产物） |
| `/opt/aivideotrans/caddy/Caddyfile.git-head` | **撤退源（持久化）** — 重启保留，rollback.sh v2 首选路径 |
| `/opt/aivideotrans/caddy/phase14_rollback.sh` | 一键撤退脚本（v2：优先 `.git-head`，fallback `/tmp/Caddyfile.original`） |
| `/tmp/Caddyfile.original` | 撤退源 fallback（重启会丢） |

---

## §15.4 撤退演练结果（2026-04-22）

方案 MVP 放行硬指标 ③（"15 min 内能回滚到 Phase 0"）实测数据。

**策略**：纯服务器侧演练（不动 CF DNS），利用 Caddy `/data` 里 2026-03-26 获取的 LE 证书缓存（notAfter=2026-06-24，63 天有效）避免触发 ACME rate limit，production 流量全程走 tunnel 不中断。

**执行脚本**：`/tmp/drill_phase14.sh`（演练完即留档）

| 阶段 | 动作 | 耗时 | 验证结果 |
|---|---|---|---|
| A | 快照稳态 | – | ✅ Caddyfile md5=b07a2747，ufw 只 22/tcp，tunnel 200 |
| B | `phase14_rollback.sh` 全流程 | **2s** | ✅ Caddyfile 恢复 LE / ufw 开 80,443 / Caddy 缓存 LE 证书加载 / loopback 200 / US IP 公网 443 = 200 |
| B.6 | tunnel 路径 | – | ⚠️ **502**（预期）：rollback 后 Caddy site block 只匹配 `aitrans.video` SNI，而 cloudflared 发 SNI=localhost → 真实撤退时 CF DNS 必须**同步**切 A 记录到 5.78.122.220 |
| C | 滚回 Phase 1 | **2s** | ✅ Caddyfile 恢复 tls internal / ufw 关 80,443 / Caddy 仅 Local Authority / US IP 公网 refused / tunnel 恢复 200 |
| **总计** | – | **4s** | ✅ **PASS**（硬指标 ≤ 900s，实际 4s） |

**演练暴露的两个改进点**：

1. `/tmp/Caddyfile.original` 重启就丢 — 已落实持久化到 `/opt/aivideotrans/caddy/Caddyfile.git-head`，`phase14_rollback.sh` 升 v2 优先读持久化路径
2. tunnel 侧 502 确认："服务器侧撤退 + CF DNS 切换"**必须同步**；单做任一侧会 502 —— [撤退 SOP](#撤退-sop) 要强调 DNS 操作和脚本并行

**撤退 SOP**（真实撤退时，≤5min 完成）：

```
T0   CF Dashboard → DNS → aitrans.video：CNAME 改 A 记录 5.78.122.220，取消 🟧 Proxied（30s）
T0   US 同步执行：sudo bash /opt/aivideotrans/caddy/phase14_rollback.sh（2s）
T+30 curl -I https://aitrans.video/gateway/health → 预期 200（走 US 直连 LE cert）
T+60 docker logs --tail 20 aivideotrans-caddy 确认无报错
```

实测数据已证：15min 指标有 225× 的裕度，瓶颈在 CF DNS 传播而非服务器侧。

---

## CodeX 阶段评估 §8 硬化路线图（Phase 1.5）

独立评估报告 [2026-04-22-cloudflare-r2-phase-assessment.md](2026-04-22-cloudflare-r2-phase-assessment.md) 给 Phase 1 打 75% 完成度。在推 Phase 2（R2 下载 302）之前**必须**补这 5 项：

| # | 任务 | 本次已完成 | 剩余 |
|---|---|---|---|
| 1 | next / gateway / postgres 改绑 127.0.0.1 | ✅ 本次（docker-compose.yml + gateway/Dockerfile 双修） | – |
| 2 | cloudflared `TUNNEL_METRICS=127.0.0.1:20241` | ✅ 本次 | – |
| 3 | `/tmp/Caddyfile.original` 持久化到 `.git-head` | ✅ 本次 | – |
| 4 | 生产口径写死：主域名 = `aitrans.video`；tunnel → `localhost:443` → Caddy | ✅ 本次（rollout notes 里已全部对齐真实口径） | – |
| 5 | ~~`api.aitransvideo.com` 处理~~ | — 用户澄清此子域不是本项目域名，无需处理（见上 §CodeX §8 §5） | – |

5/5 完成。Phase 2（R2 下载 302 最小闭环）可在 Phase 0 联通/移动探针数据补齐后启动。

---

## Phase 1.5 硬化执行结果（2026-04-22）

### 修改面

| 文件 | 改动 | 目的 |
|---|---|---|
| `docker-compose.yml` (postgres) | 加 `command: ["postgres","-c","listen_addresses=127.0.0.1,::1"]` | PG 只监听 loopback，ufw 失效也不泄露 |
| `docker-compose.yml` (next) | `HOSTNAME: "0.0.0.0"` → `"127.0.0.1"` | Next standalone 只绑 loopback |
| `docker-compose.yml` (gateway) | `AVT_GATEWAY_HOST: "0.0.0.0"` → `"127.0.0.1"` + **新增 `command: ["uvicorn","main:app","--host","127.0.0.1",…]`** 覆盖镜像 CMD | Gateway uvicorn 只绑 loopback |
| `docker-compose.yml` (cloudflared-us) | 加 `TUNNEL_METRICS=127.0.0.1:20241` | metrics 端口只监听 loopback，避免 tunnel 配置/token 信息泄露 |
| `gateway/Dockerfile` | `CMD ["uvicorn",…,"--host","0.0.0.0",…]` → `CMD ["python","main.py"]` | 让镜像走 `main.py` 入口读 `settings.gateway_host`（长期修复，避免下次 rebuild 又回到 0.0.0.0） |

> 发现的真正 bug：`gateway/Dockerfile:18` 的 CMD 硬编码 `--host 0.0.0.0`，**绕过** `main.py:325` 的 `uvicorn.run(host=settings.gateway_host, …)`。单改 `docker-compose.yml` 的 `environment.AVT_GATEWAY_HOST` 没用。本次采用"compose `command:` 覆盖（立刻生效）+ Dockerfile 改走 `python main.py`（下次 rebuild 后不需 compose 覆盖）"双修。

### 验证（`ss -ltnp`，US 上真机）

```
LISTEN 0 200  127.0.0.1:5432   postgres
LISTEN 0 2048 127.0.0.1:8880   uvicorn  (gateway)
LISTEN 0 5    127.0.0.1:8877   python   (job-api，本来就 loopback)
LISTEN 0 4096 127.0.0.1:20241  cloudflared (metrics)
LISTEN 0 511  127.0.0.1:3000   next-server
LISTEN 0 200       [::1]:5432  postgres (IPv6 loopback)
LISTEN 0 4096 127.0.0.1:443    caddy    (tunnel ingress，Phase 1 §14 已落)
LISTEN 0 4096 127.0.0.1:80     caddy    (Phase 1 §14)
```

非 loopback 上只剩 `0.0.0.0:22` (sshd) + `127.0.0.53:53` (systemd-resolved 本机专用)，符合预期。

### 公网冒烟

| 项 | 结果 |
|---|---|
| `curl http://127.0.0.1:8880/gateway/health` (本机直连) | HTTP 200, 1.2ms |
| `curl https://aitrans.video/gateway/health` (tunnel → Caddy → Gateway) | HTTP 200, 282ms |
| `curl https://aitrans.video/` (Next.js 首页) | HTTP 200, 54ms |
| `curl http://5.78.122.220:8880/...` (公网 IP 直连 Gateway) | connect refused ✅ |
| `curl http://5.78.122.220:20241/...` (公网 IP 直连 metrics) | connect refused ✅ |
| ufw 状态 | active, 仅放行 22/tcp |

### 部署路径修正（副产物）

执行中发现 US 上 compose 文件曾**分裂成两份**：
- `/opt/aivideotrans/app/docker-compose.yml`（管 app / gateway / next / postgres）
- `/opt/aivideotrans/docker-compose.yml`（管 cloudflared-us / caddy）

这是 Phase 1 §14 cloudflared-us 加入时从不同目录 `docker compose up` 导致的容器 label 分裂。本次一并修复：两条路径写入同一份（md5=6315d834…）硬化版本，新建容器的 `com.docker.compose.project.config_files` label 统一。备份保留在 `*.pre-phase15-*` 和 `*.pre-gwfix-*`。

---

## 参考 git commit

- `0abdc08` phase1(§14): Caddy 降级到 tls internal，仅 loopback + aitrans.video SNI 匹配
- `f01c763` docs(phase1): 记录 §13-§15 部署踩坑 + cloudflared origin 必需值

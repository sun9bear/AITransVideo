# Phase 0 + Phase 1 实施手册（按时间顺序照做）

- 关联方案：[2026-04-21-cloudflare-r2-deployment-plan.md](2026-04-21-cloudflare-r2-deployment-plan.md)（v4 终版）
- 范围：本手册只覆盖 Phase 0（探针）和 Phase 1（Tunnel 切流），共 **~3 天**
- 目标：Phase 1 结束后大陆用户不用代理能访问 `app.yourdomain.com`，US IP 隐藏；探针数据决定 Phase 2/3 路径
- 读法：按章节顺序照做；每一步都有**完成标志**（checkbox），勾完才下一步。遇到阻塞回到方案对应章节深读

---

## 前置清单（动手之前确认）

- [ ] 读完方案 v4 **§ 0 / § 1 决策表 / § 2 架构图**（大约 20 min）
- [ ] 读完方案 **§ Phase 0 / § Phase 1 / § 11.6 撤退预案**（大约 10 min）
- [ ] 手里有 Cloudflare 账号（或准备好邮箱 + 支付方式注册）
- [ ] 手里有域名（假设是 `aitrans.video`；下文都以此为例，实际替换成你的）
- [ ] US 节点 SSH 可连通（通过 `SSH-US-Via-154.cmd`）
- [ ] 至少有一个国内朋友或能开国内云机器（Phase 0 探针需要）

---

# Day 1：Phase 0 前置准备 + 三探针

预计耗时 ~6-8 小时（包括等探针数据回来）。

## 步骤 1：Cloudflare 账号 + R2 开通（海外，30 min）

- [ ] 1.1 访问 https://dash.cloudflare.com/sign-up，注册账号（用工作邮箱）
- [ ] 1.2 **开启 2FA**（Security → Two-Factor Authentication），强烈建议
- [ ] 1.3 Dashboard → R2 → Overview → 点 **Purchase R2 Plan**
- [ ] 1.4 按提示绑定信用卡或 PayPal（首 10GB 免费，绑卡是 R2 必需步骤）
- [ ] 1.5 R2 → Create bucket：建 **3 个 bucket**
  - [ ] `avt-uploads`
  - [ ] `avt-artifacts`
  - [ ] `avt-backups`
  - Location hint 选 **APAC**（离大陆近）
  - **不**勾 "Allow public access"（保持私有）
- [ ] 1.6 R2 → Overview → 右上角记下你的 **Account ID**（32 位 hex，Phase 1 会用）
- [ ] 1.7 R2 → Manage R2 API Tokens → Create API Token
  - Name: `avt-prod-rw`
  - Permissions: **Object Read & Write**
  - Specify bucket(s): 勾选上面 3 个
  - TTL: Forever（Phase 1 长期用）
  - 创建后**立刻保存** Access Key ID + Secret Access Key 到 1Password 或密码管理器（这是唯一能看到 secret 的机会）

**完成标志**：你有 Account ID + 一对 API key + 3 个空 bucket。

## 步骤 2：域名 NS 迁移 CF（海外，30 min + 6-24h 传播）

**风险提示**：这一步会触发 DNS 全球传播，期间网站可能有 1-60 min 抖动。如果当前有用户在跑任务，等深夜或约个窗口。

- [ ] 2.1 Cloudflare Dashboard → Add a site → 输入 `yourdomain.com` → Free plan
- [ ] 2.2 CF 会扫你的现有 DNS 记录，**仔细核对**每条记录都在（尤其 MX / TXT / 当前指 US IP 的 A 记录）
- [ ] 2.3 每条记录的 🟧/⚪ 按钮：
  - `aitrans.video` 的 A → 保持 🟧 Proxied（暂时指 US IP，Phase 1 换成 Tunnel CNAME）
  - MX / TXT / 邮箱相关 → 保持 ⚪ DNS only
- [ ] 2.4 CF 会给两个 NS 地址（形如 `xxx.ns.cloudflare.com`）
- [ ] 2.5 去你的域名注册商（Namecheap / GoDaddy / 阿里等），**改 NS** 为这两个地址
- [ ] 2.6 等传播。检查命令（每 5-10 min 跑一次）：
  ```bash
  dig +short NS yourdomain.com
  # 直到出现 cloudflare 字样 → 完成
  ```

**完成标志**：`dig NS yourdomain.com` 返回 `*.ns.cloudflare.com`，CF Dashboard 里域名状态 "Active"。

## 步骤 3：Phase 0 探针预置文件（海外，20 min）

给探针 ① / ② 准备测试样本。

- [ ] 3.1 本地生成 100MB 样本：
  ```bash
  dd if=/dev/urandom of=/tmp/sample_100mb.bin bs=1M count=100
  ```
- [ ] 3.2 上传到 R2（给探针 ②）：
  ```bash
  export R2_ENDPOINT='https://<account-id>.r2.cloudflarestorage.com'
  export AWS_ACCESS_KEY_ID='<步骤 1.7 的 key>'
  export AWS_SECRET_ACCESS_KEY='<步骤 1.7 的 secret>'

  aws s3 cp /tmp/sample_100mb.bin s3://avt-artifacts/probe/sample_100mb.bin \
      --endpoint-url="$R2_ENDPOINT" --region=auto
  ```
- [ ] 3.3 SSH 到 US 节点，放一个给探针 ① 用的样本：
  ```bash
  ssh us-host  # 用你的 Via-154 脚本
  cd /opt/aivideotrans/data/projects
  mkdir -p _probe
  dd if=/dev/urandom of=_probe/sample_100mb.bin bs=1M count=100

  # 让它通过 /probe/ 可访问:
  # 方案 A: 改 Caddyfile 加一条 file_server 规则
  # 方案 B: 借用某个已完成任务的 download URL(差不多大即可)
  # 方案 C: 临时在 next 容器里放 public/probe/,重启 next
  #
  # 最简单:用现有某个成品 final.mp4 作代替(测出来也是"US 源站下载速度",够用)
  # 记下 URL 给探针 ① 用
  ```
- [ ] 3.4 验证两个样本 URL 在海外都可访问：
  ```bash
  # 探针 ②(签 URL)
  cd <repo>/scripts/phase0_probes
  export R2_ARTIFACTS_BUCKET=avt-artifacts
  python3 generate_download_url.py | tee /tmp/probe_url.txt
  # 拿到 export PRESIGNED_URL=... 行, 在海外 curl 一下验通
  source /tmp/probe_url.txt
  curl -I "$PRESIGNED_URL"   # 应返回 HTTP 200

  # 探针 ①
  curl -I https://aitrans.video/probe/sample_100mb.bin   # 200
  # 如果 404, 检查步骤 3.3 的 Caddy/Next 配置
  ```

**完成标志**：两个样本都 `curl -I` 返回 200。

## 步骤 4：创建探针专用 R2 Token（海外，10 min）

探针 ③ 要发给国内朋友，**不能用**步骤 1.7 的生产 token（权限太大）。

- [ ] 4.1 R2 → Manage R2 API Tokens → Create API Token
  - Name: `avt-probe-temp`
  - Permissions: **Object Read & Write**
  - Specify bucket(s): **只勾** `avt-uploads`
  - TTL: **1 day**（24h 自动失效）
- [ ] 4.2 记下 Access Key + Secret（稍后发给测试者）

**完成标志**：你有一对 1 天有效的 R2 探针凭据。

## 步骤 5：分发探针脚本给国内测试者（20 min）

- [ ] 5.1 把 [`scripts/phase0_probes/`](../../scripts/phase0_probes/) 整个目录打包成 zip，或把三个 `.sh` 文件 + README 通过微信/飞书发给测试者
- [ ] 5.2 生成一个有效期 6 小时的探针 ② 签名 URL（步骤 3.4 的方式）
- [ ] 5.3 汇总发给测试者的指令模板：

  ```
  你好!帮忙跑 Phase 0 探针,三网各跑一遍,每网约 5-8 分钟.

  ① 下载脚本包: <scripts/phase0_probes 的 zip 或 github 链接>

  ② 安装 AWS CLI v2(探针 ③ 用,如已装跳过):
     curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o awscli.zip
     unzip awscli.zip && sudo ./aws/install

  ③ 生成 2GB 测试样本(~1-2 min):
     dd if=/dev/urandom of=./sample_2gb.bin bs=1M count=2048

  ④ 跑探针 ①(无凭据):
     bash probe1_baseline.sh 电信   # 或联通/移动

  ⑤ 跑探针 ②(需要 URL):
     export PRESIGNED_URL='<步骤 5.2 给的 URL>'
     bash probe2_r2_download.sh 电信

  ⑥ 跑探针 ③(需要凭据):
     export AWS_ACCESS_KEY_ID='<步骤 4.2 Access Key>'
     export AWS_SECRET_ACCESS_KEY='<步骤 4.2 Secret>'
     export R2_ENDPOINT='https://<account>.r2.cloudflarestorage.com'
     export R2_BUCKET='avt-uploads'
     bash probe3_r2_upload.sh 电信 ./sample_2gb.bin

  每个脚本输出末尾有一行 Markdown 表格, 把 9 行(三网 × 三探针)全部复制给我.
  ```

- [ ] 5.4 确认测试者收到 + 大致理解

**完成标志**：测试者开始跑。等 1-3 小时（取决于他们的网速和耐心）。

## 步骤 6：收集数据填方案 § 15（海外，等 + 15 min）

- [ ] 6.1 收到 9 行 Markdown 表格后，按探针号填到方案 § 15.2 / 15.3 / 15.4
- [ ] 6.2 根据方案 § 11.3 / D38 / D39 判定：

  **探针 ② 判据（R2 下载）**：
  - 三网都 ≥ 1 MB/s 且成功率 ≥ 90% → ✅ 继续 Phase 1
  - 任一运营商不达标 → ⚠️ Phase 1 仍可以推进，但 Phase 2 上线后需要启 Phase 2b 备胎（见方案 § Phase 2b）

  **探针 ③ 判据（R2 上传）**：
  - 三网成功率 ≥ 80% → Phase 3 路径 α（4.5d）
  - 60-80% → Phase 3 路径 β（5.5d UI 灰度）
  - < 60% → Phase 3 路径 γ（暂缓重评审，方案 § Phase 3 路径 γ）

- [ ] 6.3 如果探针 ② 三网都不达标 → 考虑暂缓 Phase 2 / 直接上 Phase 2b / 改方向（方案 § 11.6）

**完成标志**：方案 § 15 填完，知道要走哪条路。

## 步骤 7：清理探针残留（海外，5 min）

- [ ] 7.1 CF Dashboard → R2 → Manage API Tokens → **Revoke** 步骤 4 的 `avt-probe-temp`
- [ ] 7.2 清理 R2 和 US 节点的测试对象：
  ```bash
  # R2
  aws s3 rm s3://avt-artifacts/probe/sample_100mb.bin \
      --endpoint-url="$R2_ENDPOINT" --region=auto
  aws s3 ls s3://avt-uploads/probe/ --endpoint-url="$R2_ENDPOINT" --region=auto
  # 如有残留:
  aws s3 rm s3://avt-uploads/probe/ --recursive \
      --endpoint-url="$R2_ENDPOINT" --region=auto

  # US 节点
  ssh us-host
  rm -rf /opt/aivideotrans/data/projects/_probe
  ```

**完成标志**：R2 和 US 都没有探针测试残留。

---

# Day 2：Phase 1 Tunnel 切流（1.5 天）

## 步骤 8：US 节点建 Cloudflare Tunnel（海外，20 min）

- [ ] 8.1 CF Dashboard → Zero Trust → Networks → Tunnels → Create a tunnel
  - Connector: **Cloudflared**
  - Name: `avt-us`
- [ ] 8.2 页面给一个 `TUNNEL_TOKEN`（形如 `eyJ...`，很长），**保存**
- [ ] 8.3 Add Public Hostname（两条）：
  - Subdomain: `app`, Domain: `yourdomain.com`, Service: `HTTP://localhost:3000`
  - Subdomain: `status`, Domain: `yourdomain.com`, Service: `HTTP://localhost:3001`
  （这里先用 UI 配，也可改 config.yml 方式；config.yml 方式用 path-based 路由见方案 § 4.3）
- [ ] 8.4 **不要**立即切 DNS，先完成步骤 9 部署 cloudflared 容器

**完成标志**：CF 里有 tunnel `avt-us`（暂时离线），DNS 记录 `app.yourdomain.com` / `status.yourdomain.com` 自动 CNAME 到 `<uuid>.cfargotunnel.com`（🟧 Proxied）。

## 步骤 9：US 节点部署 cloudflared 容器（海外，30 min）

- [ ] 9.1 SSH 到 US，在 `/opt/aivideotrans/config/.env` 追加：
  ```bash
  CLOUDFLARED_TOKEN_US=<步骤 8.2 保存的 token>
  ```
- [ ] 9.2 **本地** 编辑 `docker-compose.yml`（仓库根），在末尾 `services:` 下追加：
  ```yaml
    cloudflared-us:
      image: cloudflare/cloudflared:latest
      restart: unless-stopped
      command: tunnel --no-autoupdate run
      environment:
        - TUNNEL_TOKEN=${CLOUDFLARED_TOKEN_US}
      network_mode: host
      depends_on:
        - gateway
        - next
  ```
- [ ] 9.3 提交改动到 main（方案 v4 说不用建分支）：
  ```bash
  git add docker-compose.yml
  git commit -m "phase1: add cloudflared-us tunnel container"
  ```
- [ ] 9.4 用 `Deploy-US-Via-154.cmd` 把 `docker-compose.yml` 传到 US 节点
- [ ] 9.5 US 节点重启服务：
  ```bash
  ssh us-host
  cd /opt/aivideotrans
  docker compose up -d cloudflared-us
  docker logs -f cloudflared-us
  # 看到 "Registered tunnel connection" × 2-4 → 成功
  ```
- [ ] 9.6 CF Dashboard → Zero Trust → Networks → Tunnels → `avt-us` 应显示 **Active**

**完成标志**：tunnel Active；cloudflared 容器稳定运行。

## 步骤 10：侧通验证（不切流，10 min）

- [ ] 10.1 海外本地 curl（通过 tunnel 访问）：
  ```bash
  curl -H "Host: app.yourdomain.com" https://app.yourdomain.com/gateway/health
  # 预期: {"status":"ok",...}
  # 如果超时或 502, cloudflared 容器日志查为什么
  ```
- [ ] 10.2 此时 `app.yourdomain.com` 已经通过 Tunnel 回源 US，但原 `aitrans.video` A 记录还指 US IP，**两条链路并存**

**完成标志**：新域名 `app.yourdomain.com` 通 tunnel 可访问。原域名 `aitrans.video` 还是老路径。

## 步骤 11：跑 MVP 前基线对照（海外 + 国内测试者，30 min）

这一步是为了 **Phase 1 完成后的对比**。现在新旧两条路径都通，要收集"通过 Tunnel 访问"的三网数据。

- [ ] 11.1 让国内测试者重跑探针 ①，目标 URL 改为 `https://app.yourdomain.com`（Tunnel 路径）：
  ```bash
  export APP_URL='https://app.yourdomain.com'
  export SAMPLE_URL='https://app.yourdomain.com/probe/sample_100mb.bin'
  export HEALTH_URL='https://app.yourdomain.com/gateway/health'
  bash probe1_baseline.sh 电信    # 联通、移动同
  ```
- [ ] 11.2 收集三网数据填 方案 § 15.6 "Phase 1" 行
- [ ] 11.3 对比 Phase 0 基线（§ 15.2），验证方案 § 11.3 硬指标 ②：三网下载速度 ±10% 内（不求加速，只求不劣化）

**完成标志**：方案 § 15.6 "Phase 1" 行填完。如果严重劣化（下降 >30%），**不要切流**，回到方案 § 11.6 撤退预案讨论。

## 步骤 12：切流量（决定性动作，5 min + 观察 30 min）

这一步把主流量从 US IP 直连切到 Tunnel。**建议深夜或用户活跃低谷做**。

- [ ] 12.1 CF Dashboard → DNS → 找 `aitrans.video` 的 A 记录
- [ ] 12.2 改这条记录：
  - Type: `A` → **`CNAME`**
  - Target: `<old-us-ip>` → **`<tunnel-uuid>.cfargotunnel.com`**（和 `app.*` 一样的 target）
  - Proxy: 保持 🟧 Proxied
- [ ] 12.3 DNS 传播（CF 代理记录通常 <1 min 生效）：
  ```bash
  dig +short aitrans.video
  # 应该返回 Cloudflare IP (104.21.x.x 或 172.67.x.x)
  ```
- [ ] 12.4 海外 + 国内测试者验证：
  ```bash
  curl -I https://aitrans.video/gateway/health
  # 200 OK
  ```
- [ ] 12.5 观察 30 min：
  - CF Dashboard → Analytics 看流量有没有异常
  - US 节点 `docker logs -f cloudflared-us` 看有没有错误
  - 国内测试者用浏览器打开 `aitrans.video` 确认界面正常、登录可用

**完成标志**：`aitrans.video` 大陆不用代理可访问；现有用户登录 / 列表 / 详情 / 下载全部正常。

## 步骤 13：关闭 US 公网入口（海外，10 min）

验证 tunnel 稳定 30 min 后再做这一步。

- [ ] 13.1 SSH 到 US：
  ```bash
  ssh us-host
  ```
- [ ] 13.2 查看当前防火墙规则：
  ```bash
  sudo ufw status verbose
  ```
- [ ] 13.3 **先备份现状**（方便撤退）：
  ```bash
  sudo ufw status > /tmp/ufw-backup-$(date +%Y%m%d).txt
  ```
- [ ] 13.4 关闭公网入口：
  ```bash
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow 22/tcp      # SSH 保留
  sudo ufw delete allow 80/tcp  2>/dev/null || true
  sudo ufw delete allow 443/tcp 2>/dev/null || true
  sudo ufw --force enable
  ```
- [ ] 13.5 确认从公网看 80/443 已关闭（从海外另一台机器跑）：
  ```bash
  nmap -Pn -p 80,443,3000,8877,8880 5.78.122.220
  # 期望全是 filtered
  ```
- [ ] 13.6 再次验证 tunnel 仍然工作：
  ```bash
  curl -I https://aitrans.video/gateway/health   # 仍应 200
  ```

**完成标志**：源站 IP 所有 HTTP 端口不可达；tunnel 依然提供服务。

## 步骤 14：Caddy 降级（海外，15 min）

方案 § 11.6 撤退要求 Caddy 不删只降级。

- [ ] 14.1 本地编辑 `Caddyfile`（或先 SSH 到 US 上改再拉回来），让 Caddy 只监听 `127.0.0.1:443`：
  ```
  # 开头加一行:
  {
      auto_https off
  }

  # 原 {$AUTODUB_PUBLIC_HOST}:443 {...} 改为:
  127.0.0.1:443, localhost:443 {
      tls internal
      # 原 reverse_proxy 配置不动
      ...
  }
  ```
- [ ] 14.2 docker-compose.yml 里 caddy 容器的 ports 从 `80:80, 443:443` 改为 `127.0.0.1:443:443`（或直接删 ports 字段，host 网络模式下会继承）
- [ ] 14.3 部署并重启：
  ```bash
  git commit -m "phase1: caddy downgrade to 127.0.0.1 tls internal"
  # Deploy-US-Via-154.cmd 上传 Caddyfile 和 docker-compose.yml
  ssh us-host
  cd /opt/aivideotrans
  docker compose up -d caddy
  docker logs caddy --tail=30   # 看 "serving HTTPS on 127.0.0.1:443" 类日志
  ```

**完成标志**：Caddy 只内网监听；tunnel 仍工作；`aitrans.video` 从大陆访问正常。

## 步骤 15：Phase 1 收尾验证（30 min）

- [ ] 15.1 Smoke test 全链路（方案 § 11.4 Phase 1 列）：
  - [ ] 未登录打开首页 → 200
  - [ ] 登录 / 登出 / 会话保持 → OK
  - [ ] 创建一个 YouTube 测试 Job（短视频即可） → 能看到进度 → 完成
  - [ ] 下载这个 Job 的 final_video → 下载成功（此时还是老本地 FileResponse 路径）
  - [ ] `<video>` 在线播放 → OK
- [ ] 15.2 三网再跑一次 probe1（`APP_URL=https://aitrans.video`），写入方案 § 15.6 "Phase 1" 最终行
- [ ] 15.3 从海外 + 大陆确认：`nmap -Pn 5.78.122.220` 无 HTTP 端口暴露
- [ ] 15.4 **撤退演练**（方案硬指标 ③）：在非生产流量时段，试一次完整撤退（步骤 12 / 13 / 14 反过来），能在 15 min 内恢复到 Phase 0 状态。**然后再切回 Phase 1**。演练数据记录到方案 § 15.6。

**完成标志（Phase 1 放行）**：
- [ ] 三网访问 `aitrans.video` 无需代理
- [ ] 源站 IP 公网不可达
- [ ] Smoke test 全过
- [ ] 撤退演练 ≤ 15 min 完成

---

# 到这里：Phase 0 + Phase 1 完成

**下一步决策树**：

```
Phase 0 探针 ② 数据
├─ 三网 ≥ 1MB/s 且成功率 ≥ 90%  →  Phase 2 按 v4 主路径(方案 § Phase 2, ~3.5d)
└─ 任一不达标                     →  Phase 2 启 Phase 2b 备胎(方案 § Phase 2b, +1.5d)

Phase 0 探针 ③ 数据(独立判据)
├─ 三网 ≥ 80%      →  Phase 3 路径 α (4.5d)
├─ 60-80%           →  Phase 3 路径 β (5.5d UI 灰度)
└─ < 60%            →  Phase 3 路径 γ (暂缓)

Phase 1 MVP 硬指标
├─ 全过             →  继续 Phase 2
└─ 撤退演练失败     →  不进 Phase 2, 优先修演练链路
```

## 撤退紧急预案（需要时再翻，方案 § 11.6）

在 Phase 1 上线后发现严重问题时：

```bash
# 1. DNS 切回 US IP (CF Dashboard, 5 min 内)
#    aitrans.video: CNAME tunnel-uuid.cfargotunnel.com → A 5.78.122.220
#    取消 🟧 Proxied

# 2. US 重开 80/443
ssh us-host
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 3. Caddy 恢复公网模式
# 改 Caddyfile 回 {$AUTODUB_PUBLIC_HOST}:443 {...}
# 改 docker-compose.yml caddy ports 回 "80:80", "443:443"
docker compose up -d caddy
docker logs -f caddy   # 等 "certificate obtained successfully"

# 4. 停 cloudflared (可选, 先保留观察 1 天再下)
docker compose stop cloudflared-us

# 5. 验证
curl -I https://aitrans.video/gateway/health  # 走 US 直连
```

---

## 进入 Phase 2 前的准备

不是今天的事，但可以提前通知开发：

- [ ] 读方案 **§ 5.1.2 r2_client.py** + **§ 5.2.3 / § 5.2.5 api.py 改造** + **§ 6.1 后端改造清单**
- [ ] 本地 clone 仓库 + 跑 `python -m pytest tests/` 确保测试套可运行
- [ ] Phase 2 预计 3.5d 后端 + 联调

---

**本手册到此结束**。所有步骤均有对应的方案章节 / 代码文件 / 撤退路径。遇到未覆盖的问题回到 [2026-04-21-cloudflare-r2-deployment-plan.md](2026-04-21-cloudflare-r2-deployment-plan.md) 搜具体决策编号（D1-D40）。

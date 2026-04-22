# Phase 0 探针脚本

对齐 [docs/plans/2026-04-21-cloudflare-r2-deployment-plan.md](../../docs/plans/2026-04-21-cloudflare-r2-deployment-plan.md) § Phase 0（D37）的三类必测探针。数据会填进方案 § 15.2 / 15.3 / 15.4，直接决定 MVP 是按主路径走还是启 Phase 2b 备胎。

---

## 前置准备

### A. 项目方（你，在海外）

**A1. 在 R2 里放一个 100MB 测试样本**（给探针 ② 用）

```bash
# 本地生成 100MB 随机数据
dd if=/dev/urandom of=./sample_100mb.bin bs=1M count=100

# 上传到 R2 (环境变量换成你自己的)
aws s3 cp ./sample_100mb.bin s3://avt-artifacts/probe/sample_100mb.bin \
    --endpoint-url="$R2_ENDPOINT" --region=auto
```

**A2. 在 US 节点放一个 100MB 样本**（给探针 ① 用，测试现网直连下载速度）

```bash
# US 节点 SSH 上去
cd /opt/aivideotrans/data/projects
mkdir -p _probe && cd _probe
dd if=/dev/urandom of=./sample_100mb.bin bs=1M count=100

# 让 Caddy 或 Next 能 serve 到 https://aitrans.video/probe/sample_100mb.bin
# 最简单:放到 Next public 目录或直接让 Caddy 加一条静态规则
# 或者:用现有任何一个已完成任务的 final_video 下载 URL 代替(差不多大)
```

**A3. 创建一个探针专用的 R2 API Token**（给探针 ③ 用，**测完必须 revoke**）

- Cloudflare Dashboard → R2 → Manage R2 API Tokens → Create API Token
- Permissions: **Object Read & Write**
- Bucket Scope: **仅勾选 `avt-uploads`**（不给其他 bucket 权限）
- 保存生成的 Access Key ID + Secret Access Key

### B. 测试者（国内朋友 / 云机器）

- Linux 或 macOS 终端（Windows 用 WSL 或 git bash）
- `curl`（系统自带）
- `bash` 4+
- 至少 **3 GB 可用磁盘空间**（生成 2GB 样本）
- **探针 ③ 需要 AWS CLI v2**，安装：
  ```bash
  curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscli.zip
  unzip awscli.zip && sudo ./aws/install
  ```
  或用 Homebrew / 云厂商镜像版都行

---

## 运行三探针

### 探针 ①：当前 US 直连基线

**测试者本地跑**（不需要任何凭据）：

```bash
bash probe1_baseline.sh 电信
bash probe1_baseline.sh 联通
bash probe1_baseline.sh 移动
```

默认目标 `https://aitrans.video`，如需改：

```bash
export APP_URL='https://aitrans.video'
export SAMPLE_URL='https://aitrans.video/probe/sample_100mb.bin'
export HEALTH_URL='https://aitrans.video/gateway/health'
bash probe1_baseline.sh 电信
```

脚本末尾会输出一行 Markdown 表格，直接发给项目方贴回 § 15.2。

### 探针 ②：R2 原生域名下载稳定性

**项目方先签 URL**（本地跑，需要 boto3）：

```bash
pip install boto3
export R2_ENDPOINT='https://<account>.r2.cloudflarestorage.com'
export R2_ACCESS_KEY_ID='...'
export R2_SECRET_ACCESS_KEY='...'
export R2_ARTIFACTS_BUCKET='avt-artifacts'
export R2_TEST_KEY='probe/sample_100mb.bin'
export EXPIRES=21600   # 6 小时,够测试者从容跑完三网

python3 generate_download_url.py
# 输出形如: export PRESIGNED_URL='https://...'
```

把输出的 `export PRESIGNED_URL=...` 这一行发给测试者。

**测试者跑**：

```bash
export PRESIGNED_URL='<项目方给的>'
bash probe2_r2_download.sh 电信
bash probe2_r2_download.sh 联通
bash probe2_r2_download.sh 移动
```

末尾的 Markdown 表格行发给项目方贴回 § 15.3。

### 探针 ③：R2 真实 multipart 上传

**测试者本地准备样本**（只需做一次）：

```bash
dd if=/dev/urandom of=./sample_2gb.bin bs=1M count=2048
# 耗时 ~1-2 分钟, 占 2GB 磁盘
```

**项目方发凭据**给测试者（A3 生成的受限 token）：

```bash
export AWS_ACCESS_KEY_ID='<R2 Access Key>'
export AWS_SECRET_ACCESS_KEY='<R2 Secret>'
export R2_ENDPOINT='https://<account>.r2.cloudflarestorage.com'
export R2_BUCKET='avt-uploads'
```

**测试者跑**：

```bash
bash probe3_r2_upload.sh 电信 ./sample_2gb.bin
bash probe3_r2_upload.sh 联通 ./sample_2gb.bin
bash probe3_r2_upload.sh 移动 ./sample_2gb.bin
```

末尾的 Markdown 表格行发给项目方贴回 § 15.4。

---

## 数据收集

三网 × 三探针 = **9 行数据**。建议用一份 Google Sheet / 飞书文档收集：

| 运营商 | 探针 ① LCP/TTFB | 探针 ① API P50 | 探针 ① 下载 | 探针 ② 平均 | 探针 ② 成功率 | 探针 ③ 耗时 | 探针 ③ 状态 |
|--------|----------------|----------------|-------------|-------------|---------------|-------------|-------------|
| 电信 | | | | | | | |
| 联通 | | | | | | | |
| 移动 | | | | | | | |

数据出来后对照方案 § 11.3 放行判据：

- **探针 ② 三网都 ≥ 1 MB/s 且成功率 ≥ 90%** → MVP 走 v4 主路径
- **任一运营商 < 1 MB/s 或成功率 < 90%** → 启动 Phase 2b 备胎（Worker HMAC + public custom domain）
- **探针 ③ 三网成功率 ≥ 80%** → Phase 3 路径 α（4.5d）
- **60-80%** → Phase 3 路径 β（5.5d UI 灰度）
- **< 60%** → Phase 3 路径 γ（暂缓重评审）

---

## 测完清理

项目方**必做**：

1. Revoke A3 生成的 R2 探针 token（Dashboard → R2 → API Tokens → Revoke）
2. 删除 R2 测试对象：
   ```bash
   aws s3 rm s3://avt-artifacts/probe/sample_100mb.bin --endpoint-url="$R2_ENDPOINT"
   # probe3 的上传对象脚本会自动删, 保险起见再扫一次:
   aws s3 ls s3://avt-uploads/probe/ --endpoint-url="$R2_ENDPOINT"
   aws s3 rm s3://avt-uploads/probe/ --recursive --endpoint-url="$R2_ENDPOINT"
   ```
3. 删除 US 节点 `/opt/aivideotrans/data/projects/_probe/`

测试者可保留本地 `sample_2gb.bin`（下次探针或 Phase 3 灰度还能用），或直接 `rm sample_2gb.bin sample_100mb.bin`。

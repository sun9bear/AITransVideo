# TU-02 · 部署 / 构建卫生

- **目标**：清掉生产镜像里的隐患与漂移源——开发期 bind-mount、未使用的 Deno、浮动镜像 tag、缺失的 env 文档、旁路安装的依赖。全 S 级、不改运行时业务逻辑。
- **关联发现**：DEP-04（dev bind-mount）· DEP-06（Deno）· DEP-07（cloudflared:latest）· DEP-02（.env.example 缺 36 项）· DEP-05（pyJianYingDraft 旁路安装）
- **前置依赖**：无；与所有单元独立，可并行派发。
- **建议分支**：`quality/build-hygiene`
- **预估工时**：A（本地清理 Step 1/2/5）=S；B（生产配置确认 Step 3/4）=需项目主输入版本/变量清单，S→M

> **建议两段式 PR**：**A 本地构建清理**（Step 1/2/5：删 bind-mount、删 Deno、pyJianYingDraft 进 pyproject——纯机械、可立即合）与 **B 生产配置确认**（Step 3/4：cloudflared 生产版本 pin、`.env.example` 逐项判断——需项目主输入）拆成两个 PR，别把「需人工判断」混进「纯清理」。
>
> **命令环境**：本文验收命令默认 **Git Bash / CI Linux**；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`test -f`→`Test-Path`、避免 `comm`/`<(...)` 进程替换）。

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **bind-mount 分层删除**：仅删除 `app` 服务的 3 个「开发期代码热更新」bind-mount（`src/`、`main.py`、`scripts/`）；data/config/jobs/model_cache 等持久化挂载保留；gateway 对 `app/src` 的只读挂载暂不动。
- **Deno 安装删除**：删除 Dockerfile 中 `curl -fsSL https://deno.land/install.sh | sh` 管道安装段，消除供应链风险与无用体积。
- **cloudflared pin 策略**：pin 到「当前生产已验证的 digest」，不盲选最新版 tag；pin 前须项目主确认生产在用 digest（执行时前置动作，不阻塞 A PR 合入，属 B PR 范围）。
- **`.env.example` 原则**：只补变量名、空值占位和说明；不写任何真实 secret；内部派生变量注明"运行时派生，无需在 .env 配置"即可。
- **两段式 PR 保持**：A（Step 1/2/5 本地清理）可立即合；B（Step 3/4 cloudflared pin + .env.example 补全）需项目主确认版本/变量后合。

## 不在本单元范围

- 依赖锁定全面切 `uv sync --frozen`（DEP-01/09）、双路配置收口（DEP-03）属 Phase 1/后续，不在此。
- 这里只做「清理 + 文档补全 + pin」，不重构 Dockerfile 分层。

## 必守不变量

- **改 docker-compose 必须落到 root 入口那份**（见 [memory 教训 feedback_apf_deploy_incident]：只改 `app/` 那份是 Known Bad Pattern）。
- 删 bind-mount 等于切回「镜像不可变」模式——确认 CLAUDE.md「项目接近完成时切回镜像不可变」的前提成立；若仍在热更新开发期，本步可标注「待项目主确认时机」再执行（文档先就位）。
- `.env.example` 只写**变量名 + 说明 + 占位**，**绝不写真实 secret**。

---

## Step 0 · 确认现状

```bash
git switch -c quality/build-hygiene
grep -n "开发期代码热更新" docker-compose.yml          # 定位 3 个 dev bind-mount
grep -n "deno\|Deno" Dockerfile                         # 定位 Deno 安装
grep -n "cloudflared:latest" docker-compose.yml         # 定位浮动 tag
grep -n "pyJianYingDraft" Dockerfile pyproject.toml     # 定位旁路安装
```

## Step 1 · 删开发期 code bind-mount（DEP-04）

`docker-compose.yml` 中标注「开发期代码热更新 bind mount」的 3 条（`src/`、`main.py`、`scripts/`）删除，切回镜像不可变。**仅删这 3 条**：data/config/jobs/model_cache 等持久化挂载保留；gateway 服务对 `app/src` 的只读挂载暂不动（✅ 已决策（CodeX 2026-06-25）：分层删除，只动 app 服务的开发期热更新挂载，其余挂载全部保留）。

**验收**：
```bash
grep -c "开发期代码热更新" docker-compose.yml   # 0
docker compose config >/dev/null && echo "compose 语法 OK"   # 可解析
```
> 执行时前置动作（已定方向）：部署侧需 `docker compose build app` + `up -d app`（不再是 restart 热更新）；本单元只改文件，部署由项目主执行。

## Step 2 · 删未使用的 Deno（DEP-06）

`Dockerfile` 中 `curl -fsSL https://deno.land/install.sh | ... sh` 一段删除（运行时从不调用 Deno；同时去掉供应链风险与 ~100MB）。

**验收**：
```bash
grep -ci "deno" Dockerfile        # 0
grep -rni "deno" src/ gateway/ main.py scripts/   # 0（确认确无运行时引用）
```

## Step 3 · pin cloudflared（DEP-07）

✅ 已决策（CodeX 2026-06-25）：pin 到「当前生产已验证的 digest」，不盲选最新版 tag（如 `:latest`）。

> 执行时前置动作（已定方向）：**pin 前须项目主确认生产在用 digest**（`docker inspect cloudflared --format '{{.Image}}'` 或 Cloudflare Dashboard 查当前隧道版本），不要凭空写版本号（pin 错版本可能换掉正在工作的隧道镜像）。

`docker-compose.yml` 的 `cloudflare/cloudflared:latest` 改为固定 digest 或具体版本 tag（取当前生产在用值）。

**验收**：
```bash
grep -n "cloudflared:" docker-compose.yml   # 不含 :latest，为具体版本或 sha256 digest
```

## Step 4 · 补 .env.example 缺失变量（DEP-02）

✅ 已决策（CodeX 2026-06-25）：`.env.example` 只补变量名、空值占位和说明，不写任何真实 secret；内部派生/运行时变量在文件内注明"运行时派生，无需在 .env 配置"。

> 执行时前置动作（已定方向）：36 项差集中部分是运行时派生/内部变量，**执行时逐项判断**是否需进 example；方向已定（只写变量名+空占位+说明），判断范围收窄为"是否是外部配置项"，不再是开放决策。

扫出「代码读取但 `.env.example` 未列」的 env，补齐（名 + 注释 + 安全占位）。生成清单：
```bash
# 代码中引用的 env 名
grep -rhoE "os\.environ(\.get)?\(['\"][A-Z0-9_]+|getenv\(['\"][A-Z0-9_]+|settings\.[a-z_]+" src/ gateway/ \
  | grep -oE "[A-Z0-9_]{4,}" | sort -u > /tmp/used_env.txt
# .env.example 已列的
grep -oE "^[A-Z0-9_]{4,}" .env.example | sort -u > /tmp/doc_env.txt
comm -23 /tmp/used_env.txt /tmp/doc_env.txt        # 差集 = 需补的（核对后写入，含所有 *_API_KEY / *_SECRET）
```
逐个补到 `.env.example`，敏感项写 `CHANGEME` / 空占位 + 注释说明用途。

**验收**：
```bash
# 重新跑差集应显著收敛（保留确属运行时派生/内部的少数例外，并在文件内注明）
comm -23 /tmp/used_env.txt <(grep -oE "^[A-Z0-9_]{4,}" .env.example | sort -u) | wc -l   # 大幅下降
grep -ciE "API_KEY|SECRET|TOKEN" .env.example   # 关键密钥类均有占位
```

## Step 5 · pyJianYingDraft 纳入 pyproject（DEP-05）

把 `Dockerfile` 里旁路 `pip install pyJianYingDraft` 改为写进 `pyproject.toml` 依赖（带版本下界），Dockerfile 不再单独装。

**验收**：
```bash
grep -n "pyJianYingDraft" pyproject.toml   # 出现在依赖
grep -ci "pip install.*pyJianYingDraft" Dockerfile   # 0
```

## 测试计划

- `docker compose config` 可解析；本地 `docker compose build app`（条件允许时）成功。
- 不触碰 Python 运行时逻辑，无需新单测；CI 既有 job 不应受影响。

## 回滚方案

每步一个 commit（显式 pathspec）。bind-mount 删除若影响开发体验，单独 revert Step 1 即可。

## 完成定义（DoD）

- [ ] 3 个 dev bind-mount 删除（落 root 入口的 compose，仅删 app 服务热更新挂载）；data/config/jobs/model_cache 持久化挂载保留；gateway 只读挂载保留；`compose config` 通过。
- [ ] Deno 安装段删除（`curl|sh` 管道安装整段移除）；全仓无运行时 Deno 引用。
- [ ] cloudflared pin 到生产已验证 digest 或具体版本 tag，无 `:latest`；pin 前已获项目主确认在用 digest。
- [ ] `.env.example` 补全缺失变量（尤其全部 API Key/Secret 占位），差集大幅收敛；运行时派生项已在文件内注明；`.env.example` 不含任何真实 secret，敏感项均为空值或 `CHANGEME` 占位。
- [ ] pyJianYingDraft 进 pyproject，Dockerfile 不再旁路安装。
- [ ] 各步独立 commit，显式 pathspec，未 `git add .`；`.env.example` 无真实 secret。

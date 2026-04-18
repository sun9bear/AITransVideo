# 旧架构迁移收尾 & 清理方案

> **Status:** ✅ **COMPLETED & DEPLOYED**（2026-04-17）
> **Last updated:** 2026-04-18（执行记录追加）
> **Depends-on:** 2026-04-17 迁移债务批次 T1-T8（commits `8e8b896` ~ `d3039b0`，已部署 US）
> **Goal:** 把单机→Web SaaS 迁移**彻底收尾**，让项目对后续开发零包袱、运行更安全稳定高效。
> **执行说明：** 每 Task 独立可回滚，按 Phase 顺序跑，Phase 内可并行。
>
> **完成摘要（详见 §13 执行记录）：**
> - 12 个清理 commit（`5ac5eaf` ~ `fb84c8f`）+ 1 个 doc 收尾 commit（`7408022`）= **13 commits** 已合入 `main` 并推送 `origin/main`
> - **US 生产已部署**，`aitrans.video` 全链路健康（5/5 容器 healthy，全部 smoke test 通过）
> - **永久 CI 守卫**：`tests/test_legacy_cleanup_guards.py`（10 条契约级断言）常驻
> - **推迟项**：T3.1b（audit 证实无多线程访问，premature optimization）+ /opt/aivideotrans 长尾 13 处（非语义 env，非重要路径）
>
> **修订历史：**
> - **v1（初稿）**：14 Task / 4 Phase
> - **v2**：三审（Claude session / Codex / Gemini 3.1 Pro）吸收后重大修订
> - **v2.1（本版）**：Codex 二审吸收——T1.6a 从"整删 test_web_ui.py"改为"按 A/B 类拆分只删 server/handler 专属测试"，保留 retained library（~50 个）测试覆盖
> 
> **v2 相对 v1 的重大修订：**
>   - **T2.3 重写**：取消 `AIVIDEOTRANS_ROOT` 单根抽象，改用仓库**已有的语义 env vars**（`AIVIDEOTRANS_CONFIG_DIR`、`AIVIDEOTRANS_JOBS_DIR`、`AIVIDEOTRANS_PROJECTS_DIR`、`AIVIDEOTRANS_RUNTIME_LOGS_DIR`）
>   - **T1.6 拆分**：低估了 `web_ui/server.py` + `handler.py` 的依赖面（`__init__.py` re-export、`models.py`、`tests/test_web_ui.py` 都引用），拆成"先迁依赖"+"再删文件"两步
>   - **T1.3 修正**：`build/` 下有 2 个历史部署 tar 归档，改 mv-then-rm 不直接删
>   - **T2.1 补齐第 5 个文件**：`voice_catalog_service.py:273` 也有 `_APP_INTERNAL_URL` 硬编码
>   - **T4.1 重写为契约级守卫**：字符串 grep 容易因注释/deprecation 文案误报，改成"跑 `main.py --help` 检查输出"、"AST 扫 import 检查废弃模块引用"等行为断言
>   - **T1.5 补齐触点**：`main.py` 的 `web-ui` 引用不止 1822-1830，还有 933/940/1341
>   - **新增 T1.1.5**：同步清理 `docs/` 里对旧 `frontend/src/` 的死链引用
>   - **T3.1 加并发触发点审计前置**：若无多线程调用证据则降级推迟
>   - **决策点锁定**：D1 文件锁 / D2 用已有语义 env / D3 加 X-Internal-Key

---

## 1. 背景

迁移债务审核走了三轮：
- **一审**（2026-04-17 上午）列出 15+ 条疑似问题
- **修复批次**（10 commits）解决其中 3 条 + 一批邻近安全问题
- **三审复核**（本方案前置）发现一审的若干条属于**误报**，实际状态已修或不构成威胁

本方案基于**复核后的真实状态**，只修真正的遗留，不做无用功。

### 1.1 复核后确认的"已修"项（不在本方案范围）

| 一审列过的项 | 实际状态 | 证据 |
|-------------|---------|------|
| `job_paths.build_workspace_dir` `user_id=None` 回退 | ✅ **已修** | [job_paths.py:14-23](../../src/services/job_paths.py) 无默认值 |
| `user_id` payload 可选读取 | ✅ **已修** | Gateway [intercept_create_job:430-431](../../gateway/job_intercept.py) 强制注入 |
| `avt:avt` DB fallback | ✅ **已修** | 上一批 T3 (commit `8e8b896`) |
| credits shadow_* 并发锁 | ✅ **已修** | 上一批 T1 (commit `a1484dc`) |
| `continue_job` 双 spawn | ✅ **已修** | 上一批 T2 (commit `f7a3053`) |
| `jobs.project_dir` 系统性 NULL | ✅ **已修** | 今天 commit `444c406` (list_jobs sync + backfill) |
| Caddy `/api/internal/*` 公网暴露 | ✅ **已修** | 上一批 T4 (commit `60c8a44`) + Caddy TLS (`a77230e`) |

### 1.2 复核后确认的"误报"（明确排除出 scope）

| 一审说是多用户漏洞 | 实际情况 | 为什么不改 |
|------------------|---------|-----------|
| `voice_registry.json` 全局文件 | 它是**项目级内置音色表**，用户 clone 的音色走 [UserVoice 表](../../gateway/models.py)（line 525-561），已有 per-user 隔离 | 没有用户数据流经，改动反而引入风险 |
| `voice_bank/` 全局目录 | **Job pipeline 完全不用它**，只被 `src/services/control_panel.py`（本地 workbench）访问，docker-compose 没挂载 | 生产多租户不涉及 |
| admin 路由直连 `localhost:8877` | `_require_admin()` 门禁 + Caddy `/api/internal/*` 404 + 8877 不对公网 = 三层防御 | 不是跨用户越权，但 config 一致性可优化（纳入 Phase 2） |
| docker-compose.yml `127.0.0.1:8877` | `network_mode: host` 所有服务共享 host 网络，127.0.0.1 就是对的（Agent 3 误报） | 只有迁 bridge 网络才需要改 |

### 1.3 复核后确认"仍需保留不能删"（明确排除出删除 scope）

| 一审说该删 | 实际还在用 | 证据 |
|-----------|-----------|------|
| `src/services/control_panel.py` ThreadingHTTPServer | **本地开发工具**，`python main.py control-panel` 启动 | `scripts/start_remote_workbench.ps1` 仍用 |
| `main.py` 的 `control-panel` + `job-api` 子命令 | 同上，Windows 本地 workbench 入口 | docker-compose 没调用（安全）|
| `src/services/web_ui/project_resolver.py` + `voice_library.py` | 被 Job API import | `src/services/jobs/api.py` + `review_actions.py` 多处引用 |
| `scripts/start_remote_workbench.ps1` | Windows 本地开发启动器 | `run_remote_workbench_service.py` 依赖 |

---

## 2. 本方案范围（v2：15 个 Task，分 4 Phase）

**Phase 1 — 死代码清理**（低风险速赢，并行可做）
- T1.1 删 `frontend/` 旧 Vite 项目
- T1.1.5 **[NEW]** 同步修正 `docs/` 里对 `frontend/src/...` 的死链引用
- T1.2 删 `tmp_local_video_repro/` 临时调试数据
- T1.3 删 `build/`（先迁走 2 个历史部署 tar，再删 PyInstaller 残留）
- T1.4 删根 `projects/` 空目录
- T1.5 删 `main.py` 的 `web-ui` 子命令 + 函数 + help text（4 处触点：933/940/1341/1828-1829）
- T1.6a **[RESTRUCTURED]** 迁 `web_ui.server/.handler` 依赖：修 `__init__.py` re-export、`models.py`、`tests/test_web_ui.py`、`constants.py`
- T1.6b **[RESTRUCTURED]** 真正删除 `server.py` + `handler.py`（T1.6a 全绿后才做）

**Phase 2 — 配置 / API 层一致性**（中低风险，有回归守卫）
- T2.1 Gateway `JOB_API_BASE` + `_APP_INTERNAL_URL` 硬编码 × **5 文件**（补齐 `voice_catalog_service.py:273`）统一到 `settings.job_api_upstream`
- T2.2 Admin 路由调 Job API 加 `X-Internal-Key`
- T2.3 **[REWRITTEN]** `/opt/aivideotrans` × 32 处硬编码→用仓库**已有**语义 env（`AIVIDEOTRANS_JOBS_DIR` 等），**不**新建 `AIVIDEOTRANS_ROOT` 抽象

**Phase 3 — 并发正确性**（中风险，需测试）
- T3.1a **[NEW 前置]** `llm_registry._cache` 多线程访问审计——若不存在多线程调用，T3.1b 推迟出本方案
- T3.1b `llm_registry._cache` 加 `threading.RLock`（仅当 T3.1a 证实有多线程访问）
- T3.2 `VoiceRegistry` 对 read-modify-write 序列加文件锁（`save()` 本身已有 `os.replace` atomic I/O，锁是叠加层）

**Phase 4 — 回归守卫**（低风险，建档用）
- T4.1 `tests/test_legacy_cleanup_guards.py` **[REWRITTEN]** —— **契约级守卫**为主（跑 `main.py --help`、AST 扫 import 引用、文件存在性），仅少量必要的字符串 grep 作辅助

**预计总工作量**：10-13 小时（含测试 + 本地验证，不含部署；比 v1 多 2-3h 主因 T1.6 拆分 + T2.3 改精细）

---

## 3. 决策点（v2 已锁定）

> 三审（尤其 Gemini 3.1 Pro）和用户皆认可默认推荐。以下决策在 v2 已定，Task 实施按此执行。

### ✅ 决策 1（已定 = A）— VoiceRegistry 并发锁策略（T3.2）

| 选项 | 工作量 | 副作用 |
|------|--------|--------|
| **A. 文件锁（fcntl/msvcrt）** | 2h | 兼容现有格式；跨平台有条件 `if os.name == "nt": msvcrt.locking(...) else: fcntl.flock(...)` |
| B. 完全迁 `UserVoice` DB 表 | 6-8h | 彻底消除文件锁问题；需要 data migration 脚本把现有 voice_registry.json 内容导入 DB；改动面大 |

**决定：文件锁（fcntl / msvcrt 跨平台）**。当前 `voice_registry.json` 是项目级内置音色表，并发写入场景罕见（admin 补录），文件锁够用。完全迁 DB 属 Batch 2 级别大改，不在本方案。

---

### ✅ 决策 2（已定 = A 的**修正版**）— 路径 env 变量策略（T2.3）

**v1 原推荐**：单一 `AIVIDEOTRANS_ROOT` 派生所有路径。
**v2 修正**（**Codex P1 否决**）：repo **已有** 4 个语义 env 在用：

```yaml
# docker-compose.yml 实证：
AIVIDEOTRANS_CONFIG_DIR: /opt/aivideotrans/config
AIVIDEOTRANS_JOBS_DIR: /opt/aivideotrans/app/jobs
AIVIDEOTRANS_PROJECTS_DIR: /opt/aivideotrans/app/projects
AIVIDEOTRANS_RUNTIME_LOGS_DIR: /opt/aivideotrans/data/runtime_logs
```

而且 `src/services/web_ui/handler.py:1050` / `cleanup.py` / `gateway/upload.py` 已经按 `os.environ.get("AIVIDEOTRANS_JOBS_DIR", "/opt/...")` 模式读取。

**决定：不新建 `AIVIDEOTRANS_ROOT`**。把散落的字面量映射到**已有的对应语义 env**。无语义匹配的长尾（如 `/opt/aivideotrans/config/admin_settings.json` → 用 `AIVIDEOTRANS_CONFIG_DIR`）也先尝试套 CONFIG_DIR，实在套不上再看情况。详见 T2.3 重写后的步骤。

---

### ✅ 决策 3（已定 = A）— `X-Internal-Key` 加到 admin 路由（T2.2）

**决定：加**。和 T4 内部路由防护一致，`_internal_headers()` helper 复用写一次就行。Defense-in-depth，成本 30min 收益大。

---

## 4. 文件触及总览（v2）

| 文件 | Task | 改动类型 |
|------|------|---------|
| `frontend/`（整个目录）| T1.1 | 删除 |
| `docs/graphs/*.md` + `docs/plans/*.md` 里引用 `frontend/src/...` 的 | T1.1.5 | 修改：改成 `frontend-next/src/...` 或标注已删除 |
| `tmp_local_video_repro/` | T1.2 | 删除 |
| `build/*-deploy-*.tar.gz` | T1.3 | **归档到 `D:/Claude/temp/deploy-archive/`** |
| `build/`（其余 PyInstaller 残留） | T1.3 | 删除 |
| `projects/`（根目录，空）| T1.4 | 删除 |
| `main.py:933, 940, 1341, 1828-1829` | T1.5 | 删除 `run_web_ui_command` 函数 + 废弃提示文本 + help text 那一行 + argparse 分支 |
| `src/services/web_ui/__init__.py:36` + `__all__` | T1.6a | 移除 `.server` re-export 和对应 `__all__` 条目 |
| `src/services/web_ui/models.py:5-10` | T1.6a | 决策：删 `WebUICommandArgs` dataclass（只用于已废弃 CLI）或内联 port 默认值——二选一，别保留僵尸 |
| `tests/test_web_ui.py` | T1.6a | **拆分（v2.1）**：删 A 类（server/handler 专属）测试函数；保留 B 类（retained library）测试 ~50 个 |
| `src/services/web_ui/constants.py` | T1.6a | 删 `WEB_UI_DEFAULT_PORT`（仅在 models.py 用，而 models.py 也改了） |
| `src/services/web_ui/server.py` + `handler.py` | T1.6b | 删除（必须 T1.6a 全绿后） |
| `gateway/admin_job_monitor_api.py:21` | T2.1 | 删 `JOB_API_BASE`，改引用 `settings.job_api_upstream` |
| `gateway/admin_settings.py:778` | T2.1 | 同上 |
| `gateway/s2_monitor_api.py:24` | T2.1 | 同上 |
| `gateway/voice_catalog_api.py:683` | T2.1 | 删 `_APP_INTERNAL_URL`，改引用 `settings.job_api_upstream` |
| `gateway/voice_catalog_service.py:273` | T2.1 | **同上（v1 漏了，v2 补齐）** |
| 5 个文件的 HTTPX 调用 | T2.2 | 加 `X-Internal-Key` header（复用 `internal_auth.py` helper） |
| `gateway/internal_auth.py`（新建） | T2.2 | 从 `voice_catalog_api.py:687` 挪出 `_internal_headers()` |
| `src/` + `gateway/` 共 32 处 `/opt/aivideotrans` 字面量 | T2.3 | 按语义映射到**已有** env vars（见 T2.3 重写版步骤） |
| `.env.example` | T2.3 | 记录 4 个路径 env 的 Windows dev 覆盖示例 |
| `src/services/llm_registry.py:115-141` | T3.1b | 加 `threading.RLock`（若 T3.1a 审计证实需要） |
| `src/services/voice_registry.py:208-244` | T3.2 | 在 read-modify-write 序列外围加文件锁（叠加于现有 `os.replace` atomic I/O） |
| `src/services/_file_lock.py`（新建） | T3.2 | 跨平台 `file_lock()` context manager |
| `tests/test_legacy_cleanup_guards.py` | T4.1 | 新建：**契约级**守卫（跑 `main.py --help`、AST 扫 import、文件存在性等） |

---

## 5. 任务分解

### Phase 1: 死代码清理

#### Task 1.1: 删 `frontend/`（旧 Vite 项目）

**Files:**
- Delete: `frontend/`（整个目录 + `frontend/node_modules/` 如有）
- Verify: `docker-compose.yml`, `Caddyfile`, `package.json`（根目录）无引用

---

- [ ] **T1.1.1: 零引用检查**

```bash
grep -rn "frontend/" docker-compose.yml Caddyfile .github/ scripts/ 2>/dev/null
grep -rn "\"./frontend\"\|'./frontend'" frontend-next/ gateway/ src/ 2>/dev/null
```

预期：**无输出**（仅 `frontend-next/` 相关命中是正常的）。若有命中，停止此 Task，升级到 DONE_WITH_CONCERNS。

- [ ] **T1.1.2: 删除目录**

```bash
rm -rf frontend/
```

- [ ] **T1.1.3: 确认 `.gitignore` 无变更需要**

已有 `.gitignore` 一般会 ignore `node_modules/` 和 `dist/`。本 Task 不新增 ignore 规则。

- [ ] **T1.1.4: 提交**

```bash
git add -A
git commit -m "chore: remove legacy frontend/ (Vite) — fully replaced by frontend-next/"
```

---

#### Task 1.1.5: 同步 `docs/` 里对旧 `frontend/` 路径的死链引用（v2 新增）

**背景：** 删掉 `frontend/` 后，docs 里若还有形如 `frontend/src/routes/...` 的 file reference，未来 AI 会话按 CLAUDE.md 指引先读 graphs，碰到死链会浪费时间。必须同步修或标注。

**Files:**
- Modify: `docs/graphs/*.md` 里所有 `frontend/src/` → 改 `frontend-next/src/` 或明确标"2026-04-17 已删除"
- Modify: `docs/plans/*.md` 里同理
- Modify: 任何根目录 Markdown（README.md / CONTRIBUTING.md / HANDOFF.md 等）里的引用

---

- [ ] **T1.1.5.1: 枚举所有死链**

```bash
grep -rn "frontend/src/\|frontend/vite\.\|frontend/package\.json" docs/ *.md 2>/dev/null | grep -v "frontend-next/"
```

期望：列出具体 file:line 引用的（大概十几条）。

- [ ] **T1.1.5.2: 逐条修正**

对每条引用判断：
- 若同样文件在 `frontend-next/src/` 也存在（routes 结构大致保留）→ 直接改路径前缀
- 若已无对应文件（如旧 Vite 独有的 `frontend/vite.config.ts`）→ 改成 `~~frontend/...~~ (2026-04-17 已随旧 Vite 前端一起删除)`

不要批量 sed 替换 —— 逐条判断。

- [ ] **T1.1.5.3: 再次 grep 验证零残留**

```bash
grep -rn "frontend/src/" docs/ 2>/dev/null | grep -v "frontend-next/"
```

期望：无输出，或所有剩余行都带 "已删除" 标注。

- [ ] **T1.1.5.4: 提交**

```bash
git commit -am "docs: fix dead-link references to legacy frontend/ (removed in T1.1)"
```

---

#### Task 1.2: 删 `tmp_local_video_repro/`

**Files:**
- Delete: `tmp_local_video_repro/`
- Verify: 代码无硬编码引用

---

- [ ] **T1.2.1: 零引用检查**

```bash
grep -rn "tmp_local_video_repro" src/ gateway/ tests/ frontend-next/ --include="*.py" --include="*.ts" --include="*.tsx"
```

预期：**无输出**。

- [ ] **T1.2.2: 删除 + 确认 `.gitignore`**

```bash
rm -rf tmp_local_video_repro/
# 确认 .gitignore 有 `tmp_*/` 或 `tmp_local_*/`
grep -E "^tmp_|^tmp/" .gitignore || echo 'tmp_*/' >> .gitignore
```

- [ ] **T1.2.3: 提交**

```bash
git add -A
git commit -m "chore: remove tmp_local_video_repro/ debug fixtures"
```

---

#### Task 1.3: 清理 `build/`（v2：先归档部署 tar 再删）

**背景修正：** v1 以为 `build/` 全是 PyInstaller 产物。实测内容：
```
build/
├── admin-pricing-copy-deploy-20260409-151357.tar.gz   ← 历史部署归档（不是 PyInstaller）
├── pricing-admin-deploy-20260409-133308.tar.gz         ← 历史部署归档
├── bdist.win-amd64/                                    ← PyInstaller
└── lib/                                                ← PyInstaller
```

**Files:**
- Move: `build/*-deploy-*.tar.gz` → `D:/Claude/temp/deploy-archive/`（保留部署记录）
- Delete: `build/bdist.win-amd64/`, `build/lib/`, 及 `build/` 整目录

---

- [ ] **T1.3.1: 零引用检查**

```bash
grep -rn "^build/\|\"build/\"\|'build/'" docker-compose.yml Caddyfile Dockerfile* .github/ 2>/dev/null | grep -v node_modules
```

预期：无关键引用（frontend-next 的 `build` 是 npm script，不是目录引用）。

- [ ] **T1.3.2: 归档历史部署 tar**

```bash
mkdir -p "D:/Claude/temp/deploy-archive/"
mv build/*-deploy-*.tar.gz "D:/Claude/temp/deploy-archive/"
ls "D:/Claude/temp/deploy-archive/"   # 验证归档成功
```

- [ ] **T1.3.3: 删除 PyInstaller 残留**

```bash
rm -rf build/
```

- [ ] **T1.3.4: 确认 `.gitignore`**

```bash
grep "^build/" .gitignore || echo "build/" >> .gitignore
```

- [ ] **T1.3.5: 提交**

```bash
git commit -am "chore: remove build/ PyInstaller residue (deploy tars archived to local temp)"
```

---

#### Task 1.4: 删根 `projects/`

**Files:**
- Delete: `projects/`（根目录空目录）

---

- [ ] **T1.4.1: 确认真空 + 无引用**

```bash
ls -la projects/  # 应该是空或只有 .gitkeep
grep -rn "\"./projects\"\|'./projects'\|(PROJECT_ROOT.*\"projects\"" src/ gateway/ --include="*.py" | grep -v "data/projects"
```

- [ ] **T1.4.2: 删除 + 提交**

注意：不要动 `data/projects/`（真实数据目录）。

```bash
rm -rf projects/
git commit -am "chore: remove empty root projects/ dir (data lives in data/projects/)"
```

---

#### Task 1.5: 删 `main.py` 的 `web-ui` 子命令（v2：4 处触点）

**Files:**
- Modify: `main.py` 共 4 处：
  - **Line 933**: `def run_web_ui_command(argv: list[str]) -> None:` —— 整个函数删
  - **Line 940**: `"web-ui 命令已废弃..."` 废弃提示字符串（在 run_web_ui_command 函数体内，随函数一起走）
  - **Line 1341**: help text 里 `"  python main.py web-ui          # (deprecated — prints deprecation notice)"` 那一行 —— 删
  - **Line 1828-1829**: `if command == "web-ui": run_web_ui_command(sys.argv)` —— 删整条 if 分支

---

- [ ] **T1.5.1: 定位 `web-ui` 触点**

```bash
grep -n "web-ui\|web_ui_command\|run_web_ui" main.py
```

预期看到 5 行（函数定义、函数体里的废弃文案、help text、if 分支、函数调用）。

- [ ] **T1.5.2: 逐行删除**

**顺序很关键：从大到小删，避免行号漂移破坏后续定位。**

1. 先删 `run_web_ui_command()` 函数整体（line 933 起一段）
2. 删 `if command == "web-ui":` 分支（line 1828-1829）
3. 删 help text 那行（line 1341）

注意 `main.py:940` 的废弃提示文本在 run_web_ui_command 函数体内，随函数一起删了，无需单独操作。

**保留**：
- `control-panel` 子命令 + `run_control_panel_server` 调用
- `job-api` 子命令

- [ ] **T1.5.3: 本地验证**

```bash
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe main.py --help
```

确认 subcommands 列表**不再有 `web-ui`**（包括 help text 也不提），但 `control-panel` 和 `job-api` 仍在。

再做一次 grep 零残留：

```bash
grep -n "web-ui\|web_ui_command\|run_web_ui" main.py
# 预期 0 输出
```

- [ ] **T1.5.4: 提交**

```bash
git commit -am "chore(main.py): remove deprecated web-ui subcommand — 4 touchpoints cleared"
```

---

#### Task 1.6a: 迁 `web_ui.server` / `handler` 依赖（v2 新拆——先迁依赖）

**背景修正：** v1 以为 `web_ui/server.py + handler.py` 是 web-ui 子命令唯一引用。**三审（Codex + Claude R1）实测证伪**：

```
src/services/web_ui/__init__.py:36  from .server import create_web_ui_server, run_web_ui_server
src/services/web_ui/models.py:5     from .constants import WEB_UI_DEFAULT_PORT
                           :10      port: int = WEB_UI_DEFAULT_PORT  # WebUICommandArgs dataclass
tests/test_web_ui.py:17            import services.web_ui.voice_library as voice_library_module
tests/test_web_ui.py:18            from services.web_ui import (...)
tests/test_web_ui.py:1488          from services.web_ui.handler import _normalize_optional_text
```

**必须先处理这些依赖，否则 T1.6b 直接炸测试 + 包导出。**

**Files:**
- Modify: `src/services/web_ui/__init__.py`（去除 `.server` re-export 条目和 `__all__` 项）
- Modify: `src/services/web_ui/models.py`（删 `WebUICommandArgs` dataclass——随 web-ui 子命令一起退役）或内联 port 字面量
- Delete: `tests/test_web_ui.py`（整个文件都在测 server + handler，随功能走）
- Modify: `src/services/web_ui/constants.py`（删 `WEB_UI_DEFAULT_PORT`）

---

- [ ] **T1.6a.1: 分析 `_normalize_optional_text` 的真定义位置**

```bash
grep -rn "def _normalize_optional_text" src/
```

若只在 `handler.py` 里定义，**T1.6a.1.5：** 把它挪到独立工具模块（比如 `src/services/web_ui/text_utils.py`）或者 `src/services/_text_utils.py`，然后 handler.py 和其他用它的地方都改 import。

若已经在别处定义，handler.py 只是 re-export —— 则 test_web_ui.py:1488 的 import 路径改成指向真定义即可，handler.py 删除无影响。

- [ ] **T1.6a.2: 拆分 `tests/test_web_ui.py`——不整删（v2.1 Codex 二审修正）**

**v2 的误判：** 原 v2 方案要"整个删 test_web_ui.py"。Codex 二审实证这文件的 56 个测试中**大多数测的是保留中的 library 代码**（`build_web_ui_snapshot` / `save_web_ui_settings` / `build_provider_key_options` / `voice_review_*` / `project_resolver` / `ProcessJobManager` / `JobAPIBackedJobManager` 等），整删会让仍 live 的 library（`src/services/jobs/api.py:788` 仍在 import `voice_library`）失去大量回归覆盖。

**正确做法：** 把测试分成两类，**只**删 server/handler 专属那一小部分。

- [ ] **T1.6a.2.1: 逐函数分类 `test_web_ui.py` 里 56 个 test**

读文件，每个 `def test_xxx` 和 `class TestXxx` 按测试目标分类：

| 类别 | 判据 | 处理 |
|------|------|------|
| **A. server/handler 专属**（随 T1.6b 退役）| 函数名/断言直接测 `create_web_ui_server`、`run_web_ui_server`、Web UI HTTP 端点、handler 请求解析 | 随测试文件拆分一起走（下面 T1.6a.2.3）|
| **B. retained library 测试**（必须保留）| 测 `build_web_ui_snapshot`、`save_web_ui_settings`、`build_provider_key_options`、`build_translation_model_options`、`set_translation_primary_model`、`voice_review_*`、`voice_library_*`、`_resolve_authoritative_review_project_dir`、`JobAPIBackedJobManager`、`ProcessJobManager` | 全部保留 |

从文件初步扫描预期分类：
- **A 类**：`test_create_web_ui_server_uses_job_api_manager_by_default`、`test_web_ui_result_download_endpoint_allows_only_public_whitelist`（HTTP endpoint 测），以及其他直接调 server.py 内部或 handler.py 独有函数的少量测试
- **B 类**：其余大部分（~50+ 个），测的是 `config_helpers.py` / `snapshot.py` / `voice_review.py` / `project_resolver.py` / `job_managers.py` 等 library 模块

手工过一遍分类结果，列出 A 类测试函数名的清单（预期 ≤10 个），作 T1.6a.2.3 的输入。

- [ ] **T1.6a.2.2: 处理 `_normalize_optional_text` import（line 1488）**

```bash
grep -rn "def _normalize_optional_text" src/
```

- 若它定义在 handler.py 且**被 A 类测试专用** → 随 handler.py 一起退役，对应测试也走 A 类
- 若它被 B 类 library 代码使用 → 挪到独立工具模块（如 `src/services/web_ui/text_utils.py`），handler.py 和测试都改 import 指向新位置
- 若它在 handler.py 但实际是 B 类工具 → 同上，挪到 text_utils.py

- [ ] **T1.6a.2.3: 拆文件**

两种做法二选一：

**方案 α（最小改动）：** 保留 `tests/test_web_ui.py` 文件名，只**删掉 A 类测试函数**。优点：最少 diff；缺点：文件名误导（叫 test_web_ui 但里面都是 library 测试）。

**方案 β（cosmetic 改名）：** 把 B 类函数移到新文件 `tests/test_web_ui_library.py`（或按 module 细分：`test_web_ui_snapshot.py` / `test_web_ui_voice_review.py` / `test_web_ui_job_managers.py`），然后删原 `test_web_ui.py`（此时它只剩 A 类会随 T1.6b 退役）。

**推荐：方案 α**——改动小，名字 cosmetic 问题未来有空再改。重点是别丢测试。

- [ ] **T1.6a.2.4: 验证分类正确**

```bash
cd "D:/Claude/AIVideoTrans_Codex_web_mvp"
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/test_web_ui.py -v --tb=short --timeout 60
```

预期：**所有 B 类测试 PASS**（A 类已删），无 import 报错。若有 import 报错，T1.6a.2.2 的 `_normalize_optional_text` 处理漏了 —— 回去补。

**若此时 pytest 仍然会因为 T1.6b 未执行而无法删 server.py：没关系，T1.6a 只处理测试和依赖**，server.py+handler.py 文件还在，只是不被 A 类测试引用了，B 类测试不依赖 server/handler。

- [ ] **T1.6a.3: 改 `src/services/web_ui/__init__.py`**

移除两处：

```python
# 删这一行（line 36 附近）：
from .server import create_web_ui_server, run_web_ui_server

# 并在 __all__ 里删对应条目：
"create_web_ui_server",
"run_web_ui_server",
```

- [ ] **T1.6a.4: 处理 `models.py` 的 `WebUICommandArgs`**

读 `src/services/web_ui/models.py`，确认 `WebUICommandArgs` 只在 web-ui CLI 里用。

grep 验证：
```bash
grep -rn "WebUICommandArgs" src/ tests/ gateway/
```

- 若只在 `__init__.py` re-export + 已删的 test_web_ui.py 里用 → 删整个 `WebUICommandArgs` class + `__init__.py` 里对应 re-export + `__all__` 条目
- 若还有业务代码引用 → 改 `port: int = 8876`（内联字面量），并删 `from .constants import WEB_UI_DEFAULT_PORT`

- [ ] **T1.6a.5: 删 `constants.py` 里 `WEB_UI_DEFAULT_PORT`**

确认 T1.6a.4 处理完后，`WEB_UI_DEFAULT_PORT` 已无引用：

```bash
grep -rn "WEB_UI_DEFAULT_PORT" src/ gateway/ tests/
# 期望 0 命中（或只在 constants.py 定义处）
```

然后删 `src/services/web_ui/constants.py` 里的 `WEB_UI_DEFAULT_PORT = 8876` 一行。

- [ ] **T1.6a.6: 跑测试验证**

```bash
cd "D:/Claude/AIVideoTrans_Codex_web_mvp"
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/ -x --tb=short -k "not postgres" --timeout 120
```

预期：**全绿**。如果这一步失败，**不要进 T1.6b**——先调试 T1.6a 的迁移漏了什么。

- [ ] **T1.6a.7: 提交**

```bash
git commit -am "refactor(web_ui): migrate server/handler dependents before deletion (T1.6a prep)"
```

---

#### Task 1.6b: 真正删除 `server.py` + `handler.py`（v2 新拆——后删）

**前置：T1.6a 全绿**。若 T1.6a 某步失败，T1.6b 绝不启动。

**Files:**
- Delete: `src/services/web_ui/server.py`
- Delete: `src/services/web_ui/handler.py`

---

- [ ] **T1.6b.1: 再次全仓验证零引用**

```bash
grep -rn "from services.web_ui.server\|from services.web_ui.handler\|import services.web_ui.server\|import services.web_ui.handler\|services\.web_ui\.server\|services\.web_ui\.handler" --include="*.py" --include="*.toml" --include="*.json" .
```

预期：**0 命中**。若有命中说明 T1.6a 漏迁移，回 T1.6a 补。

- [ ] **T1.6b.2: 删除文件**

```bash
rm src/services/web_ui/server.py src/services/web_ui/handler.py
```

- [ ] **T1.6b.3: 跑全量测试**

```bash
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/ -x --tb=short -k "not postgres" --timeout 120
```

预期：**全绿**。

- [ ] **T1.6b.4: 提交**

```bash
git commit -am "chore(web_ui): delete retired server.py + handler.py (Phase 4 downline complete)"
```

---

### Phase 2: 配置 / API 层一致性

#### Task 2.1: Gateway `JOB_API_BASE` 统一到 `settings.job_api_upstream`

**Files（v2：补齐第 5 个文件）：**
- Modify: `gateway/admin_job_monitor_api.py:21`（删 `JOB_API_BASE = "http://localhost:8877"`）
- Modify: `gateway/admin_settings.py:778`（同上）
- Modify: `gateway/s2_monitor_api.py:24`（同上）
- Modify: `gateway/voice_catalog_api.py:683`（`_APP_INTERNAL_URL = "http://127.0.0.1:8877"` 同理统一）
- Modify: **`gateway/voice_catalog_service.py:273`**（**v1 漏了的第 5 个文件**，也是 `_APP_INTERNAL_URL = "http://127.0.0.1:8877"`）
- Test: `tests/test_legacy_cleanup_guards.py` 加断言

**确认的 config 属性名：** `settings.job_api_upstream`（[gateway/config.py:12](../../gateway/config.py)，env var `AVT_JOB_API_UPSTREAM`）。下面替换用这个名字。

---

- [ ] **T2.1.1: 逐文件替换（5 个文件）**

每个文件里：

```python
# 删除模块顶层常量：
JOB_API_BASE = "http://localhost:8877"          # admin_* / s2_monitor
_APP_INTERNAL_URL = "http://127.0.0.1:8877"     # voice_catalog_api

# 改为（顶部 import 一次，其他地方引用 settings.job_api_upstream）：
from config import settings
# ... 使用时：
url = f"{settings.job_api_upstream}/jobs/{job_id}"
```

- [ ] **T2.1.2: 本地 grep 确认无残留硬编码**

```bash
# 只扫 gateway/ 下的 .py 文件（docker-compose.yml 在仓根不在 gateway/ 下，不会被误匹配）
grep -rn "http://localhost:8877\|http://127.0.0.1:8877" gateway/ --include="*.py"
```

预期：**仅在 `gateway/config.py` default 处出现一次**（line 12 的 `job_api_upstream: str = "http://127.0.0.1:8877"`），其他业务 .py 文件 0 命中。

- [ ] **T2.1.3: 跑测试 + 提交**

```bash
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/test_gateway_*.py -v
```

```bash
git commit -am "refactor(gateway): unify JOB_API_BASE to settings.job_api_upstream"
```

---

#### Task 2.2: Admin 路由调 Job API 加 `X-Internal-Key`

**Files:**
- Modify: `gateway/admin_job_monitor_api.py` 所有 httpx 调用
- Modify: `gateway/admin_settings.py` 所有 httpx 调用
- Modify: `gateway/s2_monitor_api.py` 所有 httpx 调用
- Test: `tests/test_internal_voice_catalog_access.py` 扩展（admin 路由也测）

---

- [ ] **T2.2.1: 复用 `_internal_headers()` helper**

现有 `gateway/voice_catalog_api.py:687-692` 有 `_internal_headers()`。把它挪到共享位置（比如 `gateway/internal_auth.py` 新建），然后 3 个 admin 文件 import 用。

```python
# gateway/internal_auth.py (新建)
import os

def internal_headers() -> dict[str, str]:
    """Shared helper for gateway → Job API internal calls.

    Reads AVT_INTERNAL_API_KEY at call time (not module-import time) so
    monkeypatch works in tests. The previous T4 (prior batch, commit
    60c8a44) added `validate_internal_api_key()` to gateway/main.py
    startup block — it raises RuntimeError if the env var is unset in
    production, so this function will never be called with an empty key
    on a successfully-started gateway.

    Defense-in-depth: if somehow called with empty key (e.g. dev env
    without the var), the X-Internal-Key header is simply omitted and
    Job API's internal-path check will 403. Fail-closed, not fail-open.
    """
    key = os.environ.get("AVT_INTERNAL_API_KEY", "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["X-Internal-Key"] = key
    return h
```

- [ ] **T2.2.2: 3 个文件替换所有 httpx 调用**

```python
# 改前：
async with httpx.AsyncClient() as client:
    resp = await client.get(f"{settings.job_api_upstream}/jobs/{jid}")

# 改后：
from internal_auth import internal_headers
async with httpx.AsyncClient() as client:
    resp = await client.get(f"{settings.job_api_upstream}/jobs/{jid}", headers=internal_headers())
```

- [ ] **T2.2.3: 扩展测试**

在 `tests/test_internal_voice_catalog_access.py` 加一个断言：admin 路由对 Job API 的调用也带 `X-Internal-Key` header（用 `httpx.MockTransport` 拦截检查）。

- [ ] **T2.2.4: 跑测试 + 提交**

---

#### Task 2.3: `/opt/aivideotrans` 硬编码 → 用已有语义 env vars（v2 重写）

**v1 取消的抽象：** 单一 `AIVIDEOTRANS_ROOT` + `_paths.py` 派生所有子路径。
**v2 修正（Codex P1 确认必要）：** repo 已在用 4 个**语义 env vars**，新建单根抽象会盖住 / 冲突现有的：

| 现有语义 env | 默认值 | 谁在用 |
|-------------|--------|--------|
| `AIVIDEOTRANS_CONFIG_DIR` | `/opt/aivideotrans/config` | docker-compose.yml + gateway 多处 |
| `AIVIDEOTRANS_JOBS_DIR` | `/opt/aivideotrans/app/jobs` | handler.py:1050, cleanup.py |
| `AIVIDEOTRANS_PROJECTS_DIR` | `/opt/aivideotrans/app/projects` | handler.py:1100, upload.py |
| `AIVIDEOTRANS_RUNTIME_LOGS_DIR` | `/opt/aivideotrans/data/runtime_logs` | docker-compose.yml（未必代码读）|

**策略：** 把散落的 `/opt/aivideotrans/...` 字面量**映射到对应的已有 env**，沿用 `os.environ.get(NAME, DEFAULT)` 模式。**不**新建 root 抽象。**不**试图统一不同语义下的路径（app 的 `/opt/aivideotrans/app/*` vs gateway 的 `/opt/aivideotrans/data/*`）。

**Files:**
- Modify: src/ + gateway/ 下的 `/opt/aivideotrans` 字面量（实测 32 处）—— 按映射表逐个改
- Modify: `.env.example` 加 4 个 path env + Windows dev override 说明
- Test: `tests/test_legacy_cleanup_guards.py` 加断言

---

- [ ] **T2.3.1: 枚举所有 32 处硬编码，按语义分类**

```bash
cd "D:/Claude/AIVideoTrans_Codex_web_mvp"
grep -rn "/opt/aivideotrans" src/ gateway/ --include="*.py" > "D:/Claude/temp/hardcoded_paths.txt"
wc -l "D:/Claude/temp/hardcoded_paths.txt"   # 期望 32
```

手工过一遍文件，每条标注所属语义：

| 路径片段 | 语义 env |
|---------|---------|
| `/opt/aivideotrans/config/...` | `AIVIDEOTRANS_CONFIG_DIR` |
| `/opt/aivideotrans/app/jobs/...` | `AIVIDEOTRANS_JOBS_DIR` |
| `/opt/aivideotrans/app/projects/...` | `AIVIDEOTRANS_PROJECTS_DIR` |
| `/opt/aivideotrans/data/runtime_logs/...` | `AIVIDEOTRANS_RUNTIME_LOGS_DIR` |
| `/opt/aivideotrans/data/<其他>` | 原样保留（无语义匹配）|

- [ ] **T2.3.2: 逐文件替换，沿用 `os.environ.get(..., DEFAULT)` 模式**

**示例模式**（跟 handler.py:1050 既有风格完全一致）：

```python
# 改前：
_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")

# 改后：
import os
from pathlib import Path
_SETTINGS_PATH = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "admin_settings.json"
```

**不新建 `_paths.py` helper**。每个模块就地读 env，重复几行 `os.environ.get` 比错误的全局抽象好。

**顺便 clean**：
- [llm_registry.py:114](../../src/services/llm_registry.py)（R1 指出）用 `AIVIDEOTRANS_CONFIG_DIR` 拼 `admin_settings.json` 路径
- 其他 31 处按映射表类推

**无语义匹配的留着不动**（如 `/opt/aivideotrans/data/other-thing`），注释说明原因。

- [ ] **T2.3.3: 更新 `.env.example`**

```bash
# 在 .env.example 合适位置加：

# --- Application path layout ---
# Production defaults (match docker-compose.yml). Override for Windows local dev:
#   AIVIDEOTRANS_CONFIG_DIR=D:/Claude/AIVideoTrans_Codex_web_mvp/data/config
#   AIVIDEOTRANS_JOBS_DIR=D:/Claude/AIVideoTrans_Codex_web_mvp/data/jobs
#   AIVIDEOTRANS_PROJECTS_DIR=D:/Claude/AIVideoTrans_Codex_web_mvp/data/projects
#   AIVIDEOTRANS_RUNTIME_LOGS_DIR=D:/Claude/AIVideoTrans_Codex_web_mvp/data/runtime_logs
#
# AIVIDEOTRANS_CONFIG_DIR=/opt/aivideotrans/config
# AIVIDEOTRANS_JOBS_DIR=/opt/aivideotrans/app/jobs
# AIVIDEOTRANS_PROJECTS_DIR=/opt/aivideotrans/app/projects
# AIVIDEOTRANS_RUNTIME_LOGS_DIR=/opt/aivideotrans/data/runtime_logs
```

**不在 .env.example 里启用**这 4 行（注释保留默认值的信息），让默认行为和生产一致；Windows 开发者需要时自己取消注释+改值。

- [ ] **T2.3.4: grep 确认替换覆盖度**

```bash
grep -rn "/opt/aivideotrans" src/ gateway/ --include="*.py" | grep -v "os\.environ\.get"
```

预期：**只剩 `os.environ.get(..., "/opt/...")` 模式里的 default 值**（这些是合法的 fallback，不是硬编码）。若还有裸字面量命中 —— 要么本轮补上，要么明确记录推迟理由。

- [ ] **T2.3.5: 跑全量测试 + 提交**

```bash
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/ -x --tb=short -k "not postgres"
```

```bash
git commit -am "refactor(paths): map /opt/aivideotrans literals to existing semantic env vars"
```

---

### Phase 3: 并发正确性

#### Task 3.1a: 并发触发点审计（v2 新增前置——若无多线程访问则推迟 T3.1b）

**背景（R1 ⚠️）：** 加锁前必须证实存在**多线程并发**读写 `_cache`。FastAPI/uvicorn 单进程纯 async 场景下，async 协程不需要 `threading.RLock`——用 `asyncio.Lock` 或直接不用（单线程不会有 race）。v1 未论证，属 premature optimization 嫌疑。

---

- [ ] **T3.1a.1: grep `_load_settings` / `_cache` 调用路径**

```bash
grep -rn "llm_registry\.\|_load_settings\|llm_registry._cache\|from llm_registry import\|import llm_registry" src/ gateway/ --include="*.py"
```

检查每个 caller 的执行环境：
- 纯 async 协程（FastAPI handler）→ 单线程，不需要 threading lock
- `concurrent.futures.ThreadPoolExecutor` 提交的 worker → 多线程，需要 lock
- `multiprocessing` → 多进程（lock 也救不了，需其他机制）

- [ ] **T3.1a.2: 跟踪 pipeline 关键调用栈**

特别检查：
- `src/pipeline/process.py`（S1/S2/S3/S4 阶段）是否在 ThreadPoolExecutor 里调 LLM？
- TTS 并行生成（`tts_generator.py` 有 `_PARALLEL_WORKERS = 3`，已确认多线程）是否会间接命中 `llm_registry`？

grep 示例：
```bash
grep -rn "ThreadPoolExecutor\|ProcessPoolExecutor\|concurrent\.futures" src/ --include="*.py" | grep -v test_
```

- [ ] **T3.1a.3: 下结论**

根据 T3.1a.1/T3.1a.2 结果：

| 结论 | 走向 |
|------|------|
| **有多线程访问 `_cache`** | 继续 T3.1b（加 `threading.RLock`） |
| 只有 async 协程，无多线程 | **T3.1b 推迟**出本方案，移到 §11 明确推迟项；本方案 Phase 3 只剩 T3.2 |
| 模糊（找不到确凿证据也不能完全排除）| 保守加 `threading.RLock`（开销极小），但 commit message 注明"defensive"，不是证实需要 |

---

#### Task 3.1b: `llm_registry._cache` 加 `threading.RLock`（仅在 T3.1a 证实必要时执行）

**Files:**
- Modify: `src/services/llm_registry.py:115-141`
- Test: `tests/test_llm_registry_concurrency.py`（新建）

---

- [ ] **T3.1b.1: 读当前实现**

读 `src/services/llm_registry.py:100-160`，确认 `_cache` 和 `_cache_ts` 的读写点。

- [ ] **T3.1b.2: 加锁**

```python
import threading

_cache: dict | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0
_cache_lock = threading.RLock()   # NEW: RLock 允许同线程嵌套


def _load_settings() -> dict:
    global _cache, _cache_ts
    with _cache_lock:
        now = time.time()
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
        # miss: reload from disk
        _cache = _reload_from_disk()   # 原有逻辑
        _cache_ts = now
        return _cache
```

- [ ] **T3.1b.3: 测试**

```python
# tests/test_llm_registry_concurrency.py
def test_concurrent_load_settings_no_partial_cache(monkeypatch, tmp_path):
    """10 threads concurrently calling _load_settings must all see a fully
    initialized cache — no partial dict reads."""
    # ... use ThreadPoolExecutor(10), assert all results are equal
```

- [ ] **T3.1b.4: 跑测试 + 提交**

---

#### Task 3.2: `VoiceRegistry` 的 read-modify-write 序列加文件锁（v2 措辞修正）

**背景（R1 💡）：** v1 措辞易误导。实际上 [voice_registry.py:227-239](../../src/services/voice_registry.py) 的 `save()` 方法**已经**用 `NamedTemporaryFile` + `os.replace` 做了**原子写**（save 本身不坏）。真正的并发问题是 `register_voice()` / `update_voice()` 执行的 **read → modify → save** 序列中间没有互斥 —— 两个并发写操作都读到相同旧状态，最后一个 save 覆盖掉另一个的修改（经典 TOCTOU）。

**本 Task 加的文件锁是叠加在现有 atomic rename 之上的**逻辑层互斥，不是替换现有 I/O。

**Files:**
- Create: `src/services/_file_lock.py`（跨平台 `file_lock()` context manager）
- Modify: `src/services/voice_registry.py`（`register_voice` / `update_voice` 外围包 lock，不改 `save()` 本身）
- Test: `tests/test_voice_registry_concurrency.py`（新建）

---

- [ ] **T3.2.1: 实现跨平台文件锁 helper**

```python
# src/services/_file_lock.py
import os
import sys
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
    @contextmanager
    def file_lock(path: Path):
        """Windows exclusive lock via msvcrt.locking.

        Note: `touch()` is not atomic on Windows — two concurrent processes
        could race here. Accepted trade-off because voice registry writes
        are rare (admin-triggered only). If this ever becomes hot-path,
        migrate to asyncio.Lock in-process + DB-backed registry.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.touch(exist_ok=True)   # idempotent, exist_ok silences race
        fd = os.open(lock_path, os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
else:
    import fcntl
    @contextmanager
    def file_lock(path: Path):
        """POSIX exclusive lock via fcntl.flock."""
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.touch(exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
```

- [ ] **T3.2.2: 修改 `register_voice` / `update_voice`（**不**改 `save()`）**

把 "load → modify → save" 整个序列包在 `with file_lock(...)` 里。`save()` 方法本身已经是原子（`NamedTemporaryFile` + `os.replace`），**不要动它**：

```python
def register_voice(self, voice_data: dict) -> str:
    with file_lock(self.registry_path):   # 叠加层互斥
        current = self.load()
        # ... modify ...
        self.save(current)                # 现有的 atomic rename 保持不变
```

这样两层保护叠加：文件锁防止逻辑 race，现有 `os.replace` 防止落盘 crash 时撕裂。

- [ ] **T3.2.3: 测试**

```python
# tests/test_voice_registry_concurrency.py
def test_concurrent_register_voice_no_lost_write(tmp_path):
    """Two concurrent register_voice calls must both succeed;
    final registry contains both entries."""
    # ... use 2 threads, check len(voices) == 2
```

- [ ] **T3.2.4: 跑测试 + 提交**

---

### Phase 4: 回归守卫

#### Task 4.1: `tests/test_legacy_cleanup_guards.py`

**Files:**
- Create: `tests/test_legacy_cleanup_guards.py`

---

- [ ] **T4.1.1: 写回归守卫**

```python
"""Contract-level regression guards for legacy cleanup (2026-04-17 plan v2).

v1 used string-grep guards (e.g. ban the literal "web-ui" from main.py).
Those failed on comments, deprecation messages, help text — high noise,
low signal. v2 replaces them with contract-level assertions that test
observable behavior and structural invariants:

  - `main.py --help` output must not advertise a retired subcommand
  - AST-level: no module imports a deleted symbol/module
  - File existence: deleted files must stay gone
  - Narrow business-scoped source scans (not whole-file greps)

Implementation notes:
  - Uses `sys.executable` + subprocess to run `main.py --help` so it works
    identically on Windows dev (Python from uv) and Linux CI.
  - Uses `ast.parse` + `ast.walk` for import-graph checks, not regex.
  - No `subprocess.check_output(["grep", ...])` — that would break on
    Windows without GNU grep in PATH.
"""
import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories to skip during whole-repo scans (vendored deps / build artifacts)
_SKIP_DIRS = {
    "node_modules", ".git", "build", ".venv", "venv",
    ".pytest_cache", "__pycache__", "frontend",  # frontend/ should be gone
}


def _iter_py_files(root: Path):
    """Yield *.py files under root, skipping vendored / build dirs."""
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(REPO).parts):
            continue
        yield p


def _imports_of(py_path: Path) -> set[str]:
    """Return the set of fully-qualified module names this file imports.

    Uses ast — ignores comments, strings, and docstrings. Safer than grep.
    """
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # Also capture "from X import Y" where Y could be a module
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


# ---------------------------------------------------------------------------
# Phase 1: structural invariants (deleted files stay gone)
# ---------------------------------------------------------------------------

def test_no_legacy_frontend_dir():
    assert not (REPO / "frontend").exists(), "Legacy Vite frontend/ was recreated"


def test_no_tmp_local_video_repro():
    assert not (REPO / "tmp_local_video_repro").exists()


def test_no_root_projects_dir():
    # data/projects/ is OK, root projects/ is not
    assert not (REPO / "projects").exists(), \
        "Root projects/ came back (real data is at data/projects/)"


def test_no_build_dir():
    assert not (REPO / "build").exists(), "build/ residue is back"


def test_no_web_ui_server_files():
    assert not (REPO / "src/services/web_ui/server.py").exists()
    assert not (REPO / "src/services/web_ui/handler.py").exists()


# ---------------------------------------------------------------------------
# Contract: main.py --help must not advertise `web-ui`
# ---------------------------------------------------------------------------

def test_main_help_does_not_advertise_web_ui_subcommand():
    """Behavioral contract: run main.py --help and verify web-ui is gone
    from the *visible CLI surface*. Tolerates the word appearing in
    comments/docstrings elsewhere (v1 guard caught those, was noisy)."""
    result = subprocess.run(
        [sys.executable, str(REPO / "main.py"), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"main.py --help exited {result.returncode}: {result.stderr}"
    combined = (result.stdout + result.stderr).lower()
    # Help text should not list web-ui as a subcommand. This catches both
    # argparse subparser listings and manually-written usage text.
    assert "web-ui" not in combined, \
        f"main.py --help still advertises web-ui:\n{result.stdout}"


# ---------------------------------------------------------------------------
# Contract: no module imports deleted symbols/modules
# ---------------------------------------------------------------------------

_DELETED_IMPORT_TARGETS = {
    # Module paths that T1.6b removes; nothing in src/, gateway/, or tests/
    # should ever import these after cleanup lands.
    "services.web_ui.server",
    "services.web_ui.handler",
}


def test_no_imports_of_deleted_web_ui_modules():
    """Structural invariant: after T1.6b, no .py file imports server/handler
    or their submembers. AST-level check — catches `from ... import X`,
    `import ...`, nested attribute access via ImportFrom, etc."""
    offenders = []
    for scan_root in (REPO / "src", REPO / "gateway", REPO / "tests"):
        if not scan_root.exists():
            continue
        for py in _iter_py_files(scan_root):
            imports = _imports_of(py)
            for bad in _DELETED_IMPORT_TARGETS:
                if any(i == bad or i.startswith(bad + ".") for i in imports):
                    offenders.append(f"{py.relative_to(REPO)} imports {bad}")
    assert offenders == [], \
        "Deleted module still imported:\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# Narrow business-scope contract: gateway business modules don't hardcode
# the Job API upstream URL.
# ---------------------------------------------------------------------------

# Files that legitimately hold the default or the env resolution — allowed
# to contain the literal. Keep this list tiny.
_JOB_API_URL_ALLOWLIST = {
    REPO / "gateway" / "config.py",           # default value
    REPO / "gateway" / "internal_auth.py",    # if it references config; scanned defensively
}


def test_gateway_business_modules_no_hardcoded_job_api_url():
    """Scan ONLY gateway business files (not tests, not config) for hardcoded
    Job API URLs. Much narrower than a whole-file grep — false positive
    rate is near zero."""
    business = [p for p in _iter_py_files(REPO / "gateway")
                if p not in _JOB_API_URL_ALLOWLIST
                and "test" not in p.name]
    bad_literals = ("http://localhost:8877", "http://127.0.0.1:8877")
    offenders = []
    for py in business:
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Contract-level: literal must not appear as a string assignment.
        # We accept it in comments/docstrings (low-signal for regression).
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in bad_literals:
                    offenders.append(
                        f"{py.relative_to(REPO)}:{node.lineno}: {node.value}"
                    )
    assert offenders == [], \
        "gateway business module hardcodes Job API URL:\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# Lightweight: Caddy has @internal_block (already in live config; guard keeps it)
# ---------------------------------------------------------------------------

def test_caddyfile_has_internal_block():
    """Defense-in-depth: Caddy must still block /api/internal/* publicly.
    (Set by prior T4 batch — this guard catches accidental rollback.)"""
    caddy = REPO / "Caddyfile"
    if not caddy.exists():
        return  # dev env without Caddy is fine
    src = caddy.read_text(encoding="utf-8")
    assert "@internal_block" in src and "/api/internal/*" in src, \
        "Caddyfile lost the @internal_block rule — /api/internal/* would be public"
```

**注意取舍：**
- 不再为 `WEB_UI_DEFAULT_PORT` 常量专门做守卫——T1.6a.5 已删，若回退会被 `test_no_imports_of_deleted_web_ui_modules`（间接）或单元测试捕获
- 不再为 `/opt/aivideotrans` 计数阈值做守卫——T2.3 用 `os.environ.get` 模式替换后，硬编码默认值就是合法的 fallback，计数守卫会误报
- 契约级守卫专注于"**行为**不回退"（CLI 表面、文件结构、import graph、narrow 业务扫描），不做"字符串在不在源码里"这种脆弱检查

- [ ] **T4.1.2: 跑测试 + 提交**

---

## 6. 执行顺序 & 依赖（v2）

```
Phase 1（大部分并行，T1.5→T1.6a→T1.6b 必须串行）
  T1.1   frontend/ 删           ────┐
  T1.1.5 docs/ 死链同步            │  （依赖 T1.1）
  T1.2   tmp_local_video_repro/ 删 │   并行
  T1.3   build/ 归档 + 删            │
  T1.4   根 projects/ 删            │
  T1.5   main.py web-ui 子命令删  ──┐
  T1.6a  迁移 web_ui 依赖          │  （T1.6a 依赖 T1.5 完成）
  T1.6b  删 server.py + handler.py │  （T1.6b 依赖 T1.6a 全绿）
                                    │
Phase 2                             │
  T2.1   JOB_API_BASE 统一 × 5      │
  T2.2   X-Internal-Key 加到 admin  │
  T2.3   /opt/aivideotrans → 语义 env │
                                    │
Phase 3                             │
  T3.1a  llm_registry 多线程审计    │（先跑审计）
  T3.1b  加 RLock（仅 3.1a 证实时）│
  T3.2   VoiceRegistry 文件锁       │
                                    │
Phase 4                             │
  T4.1   契约级回归守卫（放最后） <─┘
```

**推荐顺序：**
1. Phase 1 并行跑 T1.1 / T1.2 / T1.3 / T1.4
2. T1.1.5 紧跟 T1.1（不等其他）
3. T1.5 → T1.6a → T1.6b 严格串行（T1.6a 绿才进 T1.6b）
4. Phase 2：T2.1 → T2.2 → T2.3
5. Phase 3：T3.1a 审计 → T3.1b（条件执行）→ T3.2
6. Phase 4：T4.1 最后做，所有 Phase 绿后写守卫

---

## 7. 测试策略

每个 Task 的单元测试必须跑通。所有 Task 完成后跑全量：

```bash
cd "D:/Claude/AIVideoTrans_Codex_web_mvp"
/c/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest tests/ -x --tb=short -k "not postgres" --timeout 120
```

**回归风险热区：**
- Phase 1 删文件：`main.py --help` 输出能跑；`python -m pytest tests/test_job_api_*.py` 不炸（因为 web_ui library 文件保留了）
- Phase 2 config 改动：`test_gateway_*.py` 全绿，特别是涉及 admin 路由的
- Phase 3 并发：新加的 concurrency 测试在 CI 和本地都应稳定 pass（不是 flaky）

---

## 8. 回滚方案

每个 Task 独立 commit，单独 `git revert` 即可。具体：

| Task | 回滚代价 |
|------|---------|
| T1.1-T1.4 删目录 | `git revert` 恢复文件 |
| T1.5-T1.6 删代码 | `git revert` |
| T2.1-T2.3 配置改动 | `git revert` + docker restart gateway |
| T3.1-T3.2 加锁 | `git revert` + app restart |
| T4.1 回归守卫 | 删除测试文件即可，不影响生产 |

**整批回滚**：`git revert <first-commit>..<last-commit>` 一次性撤。

---

## 9. 部署计划

本方案改动大多是 **gateway 层 + src/ 层**。按既定部署流程：

1. 本地 commit 完全部 14 个 Task
2. 全量 pytest 绿
3. 打 tar：`tar czf deploy-cleanup.tar.gz <changed files list>`
4. `Deploy-Via-154.cmd us ...` 上传 + 解压
5. 重建 gateway + next 镜像（`docker compose build gateway next`）
6. 重启 app（bind mount）: `docker compose restart app`
7. 重建上线：`docker compose up -d --force-recreate gateway next`
8. 健康检查 + 烟测

**新增 env 变量**（`AIVIDEOTRANS_ROOT`）—— 因为默认值就是 `/opt/aivideotrans`，**.env 里不设也不会坏**，无 operator 动作。

---

## 10. 上线检查单

- [ ] 所有 15 个 Task（v2 含 T1.1.5 + T1.6 拆 T1.6a/T1.6b + T3.1 拆 T3.1a/T3.1b）的单元测试 PASS
- [ ] `python -m pytest tests/ -x` 全量 PASS（或只 PG-mark 测试跳过）
- [ ] 前端 `cd frontend-next && npm run lint` 通过
- [ ] `main.py --help` 输出不再有 `web-ui` 子命令，但 `control-panel` 和 `job-api` 还在
- [ ] Gateway admin 路由调用 Job API 带 `X-Internal-Key`（`docker logs aivideotrans-gateway | grep X-Internal-Key` 能看到）
- [ ] 部署后手工验证全链路：
  - [ ] 登录
  - [ ] 创建任务
  - [ ] S2 审核翻译 / 说话人
  - [ ] "生成视频"（走新 task API 链路，验证 Phase 2 的 X-Internal-Key 注入）
  - [ ] "素材包" 下载
  - [ ] 音色克隆（**用户显式触发** —— 一次就好，验证 Phase 2 admin 调 Job API 那条链路没挂）
  - [ ] 观察 24h gateway logs 无 `Invalid or missing X-Internal-Key` 报错
  - [ ] shadow credits 扣点在预期范围（对比一次任务前后的 bucket 状态）
- [ ] `test_legacy_cleanup_guards.py` 全绿

---

## 11. 明确推迟（不在本方案）

以下是三审发现但暂缓的项，写明理由避免后人再报成 bug：

| 推迟项 | 理由 | 将来触发条件 |
|-------|------|-------------|
| `llm_registry._cache` `threading.RLock` (T3.1b) | **T3.1a 审计（2026-04-17）证实无多线程并发访问路径**：pipeline 全单线程执行；Gateway FastAPI handlers 单线程 async event loop；TTS ThreadPoolExecutor 调用链与 llm_registry 无交集。加锁属 premature optimization | 若未来在 llm_registry 调用链里引入 ThreadPoolExecutor 或 threading.Thread 并行访问，重新启动 T3.1b |
| `voice_registry.json` 迁 DB | 当前是项目级内置音色表，用户 clone 走 `UserVoice` 表，没有跨用户泄漏风险。迁 DB 是纯架构优化 | 若未来要支持"用户共享自己的音色给其他用户"功能时再做 |
| `voice_bank/` 改 per-user | Job pipeline 不用它，只被 local workbench 访问 | 若生产 pipeline 开始用 voice_bank 作用户样本存储时再做 |
| `src/services/control_panel.py` 删除 | 仍是本地开发工具（remote workbench 启动） | 若团队彻底切换到容器化本地开发时再删 |
| `main.py control-panel / job-api` 子命令删除 | 同上 | 同上 |
| bridge 网络迁移 | 当前 `network_mode: host` 工作得好好的 | 若未来需要容器网络隔离或多副本时再评估 |
| `/opt/aivideotrans` 硬编码剩余（非常量型，行内字符串的） | T2.3 抓住常量型的大头，剩下零散行内的是长尾，成本收益不划算 | 若下次迁移到非标准安装路径时再补 |

---

## 12. 成功判定（v2）

本方案上线后：
- 🟢 `frontend/`、`tmp_local_video_repro/`、`build/`、根 `projects/` 在仓里不存在（契约级 file existence 守卫）
- 🟢 `main.py --help` 输出中不出现 `web-ui`（契约级 CLI 表面守卫）
- 🟢 `services.web_ui.server` / `.handler` 不被任何 `.py` 文件 import（AST 级 import-graph 守卫）
- 🟢 Gateway 业务模块（非 config、非测试）源码里**字符串字面量**不含 `http://localhost:8877` 或 `http://127.0.0.1:8877`（AST 级字符串字面量扫描）
- 🟢 Caddyfile 保留 `@internal_block` 规则（契约级 defense-in-depth 守卫）
- 🟢 `VoiceRegistry` 并发测试稳定 pass；`llm_registry` 若 T3.1a 证实需要也绿
- 🟢 admin 路由日志里可见 `X-Internal-Key` header 被透传到 Job API
- 🟢 生产运行无 regression（部署后 24h 观察期无异常）
- 🟢 `test_legacy_cleanup_guards.py` 作为永久回归守卫纳入 CI

**v2 相比 v1 的成功判据变化：** 不再以"代码仓 grep 不到 X 字符串"作为成功标志（那是 v1 的脆弱路径，会被注释/文档正常误伤）。改为**契约级 + AST 级**的结构性不回退证明。达到以上后，本项目的**单机→Web 迁移正式完成**，后续开发不用再背历史包袱。

---

## 13. 执行记录（2026-04-17 完成）

本节在方案执行完后追加。每个 Phase 列：实际动作、commit SHA、审核轮次、偏离本方案的地方、验证证据。

### Phase 1 — 死代码清理 ✅

| Task | Commit | 审核 | 备注 |
|------|--------|------|------|
| T1.1 frontend/ 删 | `5ac5eaf` | Codex 审核 staging → 批准 | 删除 58 files / -11,139 LoC，合并 T1.1.5（docs 死链同步） |
| T1.2 tmp_local_video_repro/ 删 | 合并入 `5ac5eaf` 的 commit message（untracked，无 index 痕迹） | - | 本地零引用 verified |
| T1.3 build/ 归档后删 | 同上（untracked） | - | 2 个历史 deploy tar 归档到 `%USERPROFILE%\Desktop\deploy-archive\` 后删 |
| T1.4 根 projects/ 删 | 同上（untracked） | - | 空目录，.gitignore 已覆盖 |
| T1.5 main.py 的 web-ui 子命令删 | `f1dd24e` | Codex 放行 | 实际 4 处触点全清（933 / 940 / 1341 / 1828-1829） |
| T1.6a test_web_ui.py 拆分 + 依赖迁移 | `cea5045` | Codex 二审→`test_web_ui.py` 保留 50+ library test（只删 6 个 server/handler 专属） | 避免"整删 test_web_ui.py 丢 50+ library 测试覆盖" |
| T1.6b server.py + handler.py 真删 | `286fea3` | Codex 放行 | 1,262 LoC 退役 |
| Plan v2.1 doc | `0bb456f` | - | 方案文档本身 commit |
| verify_deploy.sh（上一批遗留） | `b6b6d03` | - | 顺带 commit，方便下次部署 |

**Phase 1 commit 清单：** `5ac5eaf` `f1dd24e` `cea5045` `286fea3` `0bb456f` `b6b6d03`（6 commits）

### Phase 2 — 配置 / API 一致性 ✅

| Task | Commit | 审核 | 备注 |
|------|--------|------|------|
| T2.2 internal_auth.py helper + guard tests | `3e6ab92` | Codex 审核 staging → 批准 | 新文件 295 lines + 6 focused tests（admin_job_monitor / admin_settings / s2_monitor 各 1） |
| T2.1 + T2.2 合并（5 shared files） | `46887cb` | Codex 放行 | 物理层面不可分（同 line 改 import + 同 hunk 改 `{JOB_API_BASE}` + `headers=internal_headers()`）。commit message 明说两个 Task 合并 |
| T2.3 路径语义映射（`AIVIDEOTRANS_CONFIG_DIR`） | `da9d0d7` | Codex 放行 | 15 处 `admin_settings.json` / `pricing_runtime.json` / `.env` / `review_prompt_history.json` 字面量收口；13 处长尾推迟（§11）|

**Phase 2 commit 清单：** `3e6ab92` `46887cb` `da9d0d7`（3 commits）

**技术细节偏差**：
- v2.1 方案 §4 中 admin_settings.py 的 T2.3 hunk 原计划单独 commit 在 T2.3，实际通过 "save 全状态 → 还原 HEAD → 手工重建 T2.1+T2.2 → commit 2 → 恢复完整状态 → commit 3" 的手法精确拆分（因为 git add -p 无法拆解物理上同 line 的 hunk）

### Phase 3 — 并发正确性 ✅

| Task | Commit | 审核 | 备注 |
|------|--------|------|------|
| T3.1a 并发触发点审计 | `6af4d27`（plan 追加 §11 条目）| - | **结论：T3.1b 推迟**。证据：pipeline 零 ThreadPoolExecutor；TTS ThreadPoolExecutor 调用链与 llm_registry 零交集；Gateway async handlers 单线程 |
| T3.1b 加 threading.RLock | — | - | **推迟**（§11 has reasoning + 触发条件） |
| T3.2 VoiceRegistry file_lock | `a8759a7` | Codex 放行 | `_file_lock.py` 跨平台 reentrant（threading.RLock + fcntl/msvcrt）+ 5 并发测试（含 reentrancy / serialization 断言）|

**Phase 3 commit 清单：** `6af4d27` `a8759a7`（2 commits）

### Phase 4 — 契约级回归守卫 ✅

| Task | Commit | 审核 | 备注 |
|------|--------|------|------|
| T4.1 tests/test_legacy_cleanup_guards.py | `fb84c8f` | Codex 放行（2 failure 修复：①远端一过时 projects/ 空目录清掉；②main.py --help exit=1 是 argparse-free dispatcher 的合法行为，守卫改成不检查 exit code）| 10 个契约级测试：文件存在性 × 6 + CLI 行为 × 1 + AST import graph × 1 + AST 字面量 × 1 + Caddyfile 结构 × 1 |

**Phase 4 commit 清单：** `fb84c8f`（1 commit）

### 部署 & 收尾

| 步骤 | 结果 |
|------|------|
| Pre-flight SSH 检查 US | 4 个 `AIVIDEOTRANS_*_DIR` env 都在 docker-compose.yml，`AVT_INTERNAL_API_KEY` / `PG_PASSWORD` / `CADDY_EMAIL` 都已设 |
| 打包 Phase 1-4（23 文件）| `deploy-legacy-cleanup.tar.gz` 180KB |
| 部署到 US（`Deploy-Via-154.cmd`）| tar 上传 + 解压 + 删除 remote 上的 `frontend/` / 根 `projects/` / `server.py` / `handler.py` |
| gateway 镜像 rebuild | `docker compose build gateway` 完成 |
| 容器刷新 | `restart app`（bind mount src/）+ `up -d --force-recreate gateway` |
| 生产验证 | 5 容器全 healthy，smoke test 全绿（`aitrans.video/` 200、`/api/internal/*` 404、`/job-api/jobs` 401、Let's Encrypt cert 有效） |
| verify_phase1_4.sh（定制脚本）| 所有 Phase 1-4 契约通过（frontend 删、web_ui server/handler 删、internal_auth 导入 OK、5 gateway 模块用上 internal_headers、llm_registry `_SETTINGS_PATH` env 解析、VoiceRegistry.register_voice 含 file_lock 等）|
| 合并到 `main` + push | 12 个 commit fast-forward 到 `main`，`git push origin main` 成功 |
| 分支清理 | `codex/review-guidelines` 本地 + 远端均已删除（CLAUDE.md 纪律：单人单分支） |
| 2026-04-18 文档收尾 | `7408022 docs: update CLAUDE.md + README + QUICKSTART` —— 集中记录新模块 / env / 禁忌 / 守卫测试入口（见 CLAUDE.md 新增"§2026-04-17 Legacy Migration Cleanup 遗产"整节） |

### 最终度量

| 维度 | 数值 |
|------|------|
| 总 commit（含 doc 收尾）| **13** |
| 代码删除 | `-12,914` lines（frontend 143MB + web_ui server/handler + main.py web-ui 等）|
| 代码新增 | `+437` lines（`internal_auth.py` / `_file_lock.py` / 3 个新测试文件 + 各种 env 映射 wrap）|
| 新增测试 | 21（admin X-Internal-Key × 6 + voice_registry 并发 × 5 + legacy cleanup guards × 10）|
| 本批次生产停机窗口 | ~15 秒（gateway 容器 force-recreate 间隙）|
| 本批次生产事故 | 0 |
| 最长连续 green CI 路径 | Phase 2 窄 scope 180 passed；Phase 3 广 scope 73 passed；Phase 4 + 其他 194 passed |

### 永久回归保障（CI 稳态）

`tests/test_legacy_cleanup_guards.py` 10 条守卫长期 live，任何下面的回归会立刻红：
1. frontend/ / build/ / tmp_local_video_repro/ / 根 projects/ 任一复活
2. web_ui/server.py 或 handler.py 被复活
3. 任何 .py 文件 import `services.web_ui.server` / `.handler`（AST 级）
4. `main.py --help` 输出再次出现 "web-ui"
5. gateway 业务模块（非 config.py 白名单）AST 常量出现 `http://localhost:8877` / `127.0.0.1:8877`
6. Caddyfile 丢掉 `@internal_block` 或 `/api/internal/*` 匹配

以及 `tests/test_admin_internal_key_headers.py` 6 条 + `tests/test_voice_registry_concurrency.py` 5 条共 11 条额外行为级守卫。

**总计：21 条专为本次清理设计的回归守卫长驻 CI。**

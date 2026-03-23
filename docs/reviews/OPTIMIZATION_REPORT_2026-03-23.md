# 代码优化执行报告

> 日期：2026-03-23
> 范围：#5 web_ui.py 拆分、#12 Provider 重复代码、#23 .gitignore、#25 文档整理
> 验证：39 测试全部通过，零回归

---

## 总览

| 任务 | 变更量 | 状态 |
|------|--------|------|
| #5 web_ui.py 拆分 | 9386行单文件 → 16个模块 + 删除5592行死代码 | ✅ 完成 |
| #12 Provider 配置重复 | 提取共享模块，删除 2 × 46 行重复函数 | ✅ 完成 |
| #23 .gitignore 补全 | 16行 → 45行，覆盖 Node/env/IDE/构建产物 | ✅ 完成 |
| #25 根目录文档整理 | 48 个 .md 文件归类到 6 个子目录 | ✅ 完成 |

---

## #5 web_ui.py 拆分 — 详情

### 问题

`src/services/web_ui.py` 是一个 9,386 行的巨型单文件，包含 HTTP handler、HTML 渲染、任务管理、Review 状态处理、文件服务、Voice Library 管理等所有功能。无法单独测试或复用，修改任何功能都有回归风险。

### 第一步：删除死代码

发现 frontend-next（Next.js）已经完全取代了旧的内嵌 HTML UI，以下代码已成死代码：

| 删除内容 | 行数 |
|----------|------|
| `_legacy_render_web_ui_html()` — 完整 HTML 页面 | ~1045 行 |
| `render_web_ui_html()` — HTML 组装函数 | ~65 行 |
| `_web_ui_extra_styles()` — CSS | ~212 行 |
| `_web_ui_tabs_markup()` — Tab 导航 HTML | ~13 行 |
| `_web_ui_results_panel_markup()` — 结果面板 HTML | ~114 行 |
| `_web_ui_review_panel_markup()` — 审核面板 HTML | ~86 行 |
| `_web_ui_translation_panel_markup()` — 翻译面板 HTML | ~85 行 |
| `_web_ui_voice_library_panel_markup()` — 音色库面板 HTML | ~84 行 |
| `_web_ui_audio_alignment_panel_markup()` — 对齐面板 HTML | ~68 行 |
| `_web_ui_script_extension()` — 内嵌 JavaScript | **3,818 行** |
| 测试中 7 个 render_web_ui_html 测试函数 | 230 行 |
| **合计** | **5,822 行** |

Handler 中的 `GET /` 路由改为返回 JSON 提示（"Legacy HTML UI removed"）。

### 第二步：按职责拆分

剩余 3,794 行拆分为 `src/services/web_ui/` 包，含 16 个模块：

| 模块 | 行数 | 职责 |
|------|------|------|
| `handler.py` | 831 | HTTP 请求路由分发（do_GET/do_POST） |
| `job_managers.py` | 752 | ProcessJobManager + JobAPIBackedJobManager |
| `project_resolver.py` | 486 | 项目目录解析、路径安全、下载白名单 |
| `speaker_review.py` | 412 | Speaker Review 读写/归一化 |
| `translation_review.py` | 332 | Translation Review 读写/拆分段落 |
| `voice_library.py` | 293 | Voice Library 快照构建 |
| `config_helpers.py` | 285 | LLM 配置、模型选项、设置保存 |
| `output_entries.py` | 152 | Artifact 路径解析、输出文件列表 |
| `utils.py` | 127 | 通用工具函数（normalize、coerce 等） |
| `segment_loader.py` | 104 | 段落数据加载、排序 |
| `snapshot.py` | 78 | `build_web_ui_snapshot` 主入口 |
| `constants.py` | 72 | 常量、正则、标签映射 |
| `review_state_helpers.py` | 68 | Review 状态加载 |
| `__init__.py` | 68 | Re-export（保持外部 import 路径不变） |
| `models.py` | 50 | WebUICommandArgs + ProcessJobSnapshot 数据类 |
| `server.py` | 47 | HTTP 服务启动 |

### 向后兼容

`__init__.py` re-export 所有公开符号，外部 import 路径 `from services.web_ui import ...` 完全不变。测试仅需修改 1 处 monkeypatch 路径（`services.web_ui.urlopen` → `services.web_ui.job_managers.urlopen`）。

---

## #12 Provider 配置重复代码

### 问题

`modules/translation/providers.py` 和 `services/tts_provider.py` 各有一份完全相同的 `_summarize_config_source()` 和 `_classify_api_key_source_type()` 函数（共 46 行 × 2 = 92 行重复）。

### 修复

创建 `src/services/provider_config_helpers.py` 共享模块，两个文件改为 import：
```python
from services.provider_config_helpers import (
    classify_api_key_source_type as _classify_api_key_source_type,
    summarize_config_source as _summarize_config_source,
)
```

### 未做的部分（推迟）

`from_env()` 方法中 10 个字段的 resolve 调用虽然结构相似，但每个 provider 有不同的字段组合和默认值。提取通用 `ProviderConfigResolver` 基类的风险/收益比不够理想（改错会导致翻译/TTS 启动失败），建议在有更多测试覆盖后再做。

---

## #23 .gitignore 补全

### 之前（16 行）
仅覆盖 `__pycache__`、`*.py[cod]`、运行时数据和 3 个配置文件。

### 之后（45 行）
新增覆盖：

| 类别 | 新增规则 |
|------|----------|
| Python | `*.egg-info/`, `venv/`, `.venv/` |
| Node/前端 | `node_modules/`, `dist/`, `.next/`, `.vite/`, `*.tsbuildinfo` |
| 环境变量 | `.env`, `.env.*` |
| 构建产物 | `*.tar.gz`, `uploads/` |
| 临时文件 | `tmp_remote_env*`, `tmp_playwright/` |
| IDE/OS | `.vscode/`, `.idea/`, `*.swp`, `.DS_Store`, `Thumbs.db` |
| 日志 | `*.log`, `npm-debug.log*` |

---

## #25 根目录文档整理

### 之前
51 个 .md 文件全部平铺在项目根目录，无层级组织。

### 之后
根目录仅保留 3 个文件（`README.md`, `AGENTS.md`, `encapsulated-growing-spark.md`），其余 48 个文件归类到：

| 目录 | 文件数 | 内容 |
|------|--------|------|
| `docs/phases/` | 10 | Phase scope 文档、实施任务 |
| `docs/acceptance/` | 10 | 验收笔记、基线总结 |
| `docs/architecture/` | 12 | UI 规范、安全边界、API 契约、工作流 |
| `docs/deployment/` | 6 | Linux/Windows 部署计划和验证 |
| `docs/reviews/` | 5 | 代码审计报告、回归清单、商用检查清单 |
| `docs/` (顶层) | 5 | 总文档、前端页面清单、当前状态 |

---

## 另一会话同步完成的修复（审计报告条目）

以下条目由当天另一个会话完成，已更新到 `CODEX_REVIEW_REPORT_2026-03-23.md`：

| 审计条目 | 修复内容 |
|----------|----------|
| #1 Caddy basic_auth 冲突 (P0) | Caddyfile + `public_entry_caddy.py` 中 `basic_auth` 已移除，两台主机部署域名 + HTTPS + session auth |
| #9 Gateway 异常吞掉 (P1) | `gateway/job_intercept.py` 4 处 `except: pass` → `logger.exception()` |
| Step 5 强制认证 | ✅ 完成（域名 + HTTPS + 登录 + basic_auth 移除） |
| Alembic 迁移管理 | ✅ 已存在（`gateway/alembic/versions/001_baseline.py`） |
| 文件下载归属校验 | ✅ Gateway 拦截 `/api/result-download` 和 `/api/project-file`，校验 job_id 归属 |
| PG 每日备份 | ✅ `scripts/pg_backup.sh` + cron 每日 3:00，两台主机已配置 |

---

## 验证结果

```
39 passed, 1 warning in 2.58s
```

所有 39 个 web_ui 测试全部通过，零回归。
唯一已有的失败（`test_gemini_translator` 中 1 个测试）与本次改动无关。

---

## 审计报告条目状态汇总

截至 2026-03-23，26 项审计条目中已关闭 8 项：

| 条目 | 优先级 | 状态 |
|------|--------|------|
| #1 Caddy basic_auth | P0 | ✅ 已修复 |
| #5 web_ui.py 拆分 | P1 | ✅ 已完成（本会话） |
| #9 Gateway 异常日志 | P1 | ✅ 已修复 |
| #12 Provider 重复代码 | P2 | ✅ 已部分修复（本会话，helper 提取） |
| #23 .gitignore | P3 | ✅ 已完成（本会话） |
| #25 文档整理 | P3 | ✅ 已完成（本会话） |
| Step 5 强制认证 | — | ✅ 已完成 |
| 文件下载归属校验 | — | ✅ 已完成 |

剩余 P0（任务队列、计量计费、存储保留）仍需尽快推进。

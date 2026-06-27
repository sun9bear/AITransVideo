# 代码质量优化方案 — 准确进度快照

> 日期：2026-06-27
> 基准：`origin/main`（commit `0212ed16`，已逐条 `git show origin/main:<file>` 实测）
> 母方案：[`docs/plans/2026-06-24-code-quality-optimization-plan-MERGED.md`](../plans/2026-06-24-code-quality-optimization-plan-MERGED.md)
> 实施轨道：[`docs/plans/code-quality-tasks/TU-00-INDEX.md`](../plans/code-quality-tasks/TU-00-INDEX.md)（ship-unit 任务单元）
>
> **为何重写**：同日 Deepseek 审计（`2026-06-27-code-quality-plan-audit.md`）对照的是 **TU-03 合并前的过期代码快照**，且**完全不知情 ship-unit 实施轨道（PR #40–#55）**，系统性低估了真实进度——其头号结论"CI 未激活、激活 CI 是最高优先级"与实际 main 矛盾（CI 三个 job 18 天前就由 TU-03 激活并阻断）。本快照逐条以 `origin/main` 实测纠正。§8 列关键分歧。

---

## 1. 一句话结论

**Wave A + Wave B 全 9 个 ship-unit 单元已全部合并 `main`**：止血已清、**护栏已激活并阻断**、低风险标准化（原子写 / admin 鉴权 / coerce+error payload / 类型契约 / 计费日志 / 构建卫生）已收口。**结构债大头（Wave C 热点拆分 / Wave D process.py Option B 收敛）尚未启动**——这是真实的下一步，不是"激活 CI"。

---

## 2. 实施轨道：ship-unit 单元（全部已合并 main）

| 单元 | 内容 | PR | squash | 状态 |
|---|---|---|---|---|
| TU-01 | 止血四修（cost 静默归零 / regenerate cancel 竞争 / editing fsync / aligner en_text）| [#40](https://github.com/sun9bear/AITransVideo/pull/40) | — | ✅ |
| TU-03 | 质量护栏脚手架（ruff/mypy/pytest 配置 + file-size ratchet + pre-commit + **CI 3 job**）| [#41](https://github.com/sun9bear/AITransVideo/pull/41) | `dc12c071` | ✅ |
| TU-04 | 统一 JSON 原子写 helper（`utils/atomic_io.py`，收口 7 处）| [#42](https://github.com/sun9bear/AITransVideo/pull/42) | `3f8508f3` | ✅ |
| TU-05 | 统一 admin 鉴权依赖（`gateway/admin_auth.py`，13 文件副本收口）| [#43](https://github.com/sun9bear/AITransVideo/pull/43) | `592dc226` | ✅ |
| TU-06 | coerce/normalize + JSON helpers + 统一 error payload 契约 | [#44](https://github.com/sun9bear/AITransVideo/pull/44) | `0e81e21a` | ✅ |
| TU-07 | 类型契约硬化（getattr 架空清理 + JobPolicy TypedDict + job_record Protocol + mypy 窄域）| [#47](https://github.com/sun9bear/AITransVideo/pull/47) | `436a0d82` | ✅ |
| TU-08 | 计费&付费路径结构化日志（print→logger）| [#51](https://github.com/sun9bear/AITransVideo/pull/51) | `8a016072` | ✅ |
| TU-02 PR-A | 构建卫生（删开发期 bind-mount 切镜像不可变 + 删 Deno + pyJianYingDraft→pyproject + uv.lock + CLAUDE.md 部署节）| [#52](https://github.com/sun9bear/AITransVideo/pull/52) | `814769e6` | ✅ |
| TU-02 PR-B | 生产配置（cloudflared `:latest`→digest + .env.example 补 53 个 compose 插值变量）| [#55](https://github.com/sun9bear/AITransVideo/pull/55) | `0212ed16` | ✅ |
| （前置）| file-size 基线修复 billing.py 1617→1996（修 PayPal 直推 main 遗留的 guard 预存红）| [#54](https://github.com/sun9bear/AITransVideo/pull/54) | `e44999d3` | ✅ |

每单元走 ship-unit：隔离 worktree → 分步 commit → 对抗式多 lens + CodeX CLI + @codex bot 复核 → 按严重度收敛 → squash-merge → 同步 TU-00-INDEX LOG。

---

## 3. 护栏（CI）实际状态：**已激活并阻断**（纠正审计 §3.2/§7/§9.1）

实测 [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) = **220 行，7 个 job**（非审计所称"212 行、无 lint job"）：

| job | 行 | 作用 | 阻断? |
|---|---|---|---|
| `backend` | 15 | 手挑守卫测试（快） | 阻断 |
| `backend-pg-integration` | 53 | 需真实 PG 的测试 | 阻断 |
| `frontend` | 86 | 前端 lint + typecheck + build | 阻断 |
| **`python-lint`** | 118 | `ruff check`+`ruff format --check`（**仅阻断新增 .py**）/ 改动既有 report-only / `mypy` 窄域 | 阻断 |
| **`backend-full-suite`** | 164 | `pytest -q -n auto --timeout=120` 全量 | 非阻断（continue-on-error，~250 预存债 + CI-env ffprobe 缺失）|
| **`file-size-guard`** | 183 | 读 **base ref** 基线（防同 PR grow+bump 自助绕过），新文件 ≤800、白名单不许超基线 | 阻断 |

- pytest 配置 **存在**于 [`pyproject.toml:86`](../../pyproject.toml)（`[tool.pytest.ini_options]` + `asyncio_mode=auto` + `addopts -m 'not slow and not real_provider and not benchmark'` + `markers` 注册）——非审计所称"无此段"。
- `.pre-commit-config.yaml`、`requirements-dev.txt` 已存在。
- `tools/file_size_baseline.json` = **42 条白名单，全部 >800**——guard 把这 42 个巨石**冻结在当前行数**，任何增长撞红（见 §5 多数文件 0~低余量）。

> 关键认知：护栏不是"配置就绪、激活待做"，而是**已激活、已阻断、已在 #40–#55 每个 PR 上实跑**。本会话亲历：#52/#54/#55 的 `gh pr checks` 真实输出上述 6 个检查的 pass/fail。

---

## 4. §4 止血项：**全部已修**（逐条 `origin/main` file:line 验证；纠正审计"H3 待确认"）

| # | 问题 | 验证（origin/main） | 状态 |
|---|---|---|---|
| H2 | cost_management 异常财务静默归零 | `gateway/cost_management.py:855` `logger.exception(...)` + `:834/:860` `ZERO_CREDITS_SUSPECT`（charged 模式才告警，free 合法 0 过滤）| ✅ |
| H3 | regenerate_all_async cancel 竞争 | `src/services/jobs/regenerate_all_async.py:187-197` 显式注释+逻辑保护"in-flight cancel survives"（非终态写不回写 `cancel_requested=False`）| ✅（审计"待确认"系未细看）|
| H4 | editing 写盘漏 fsync | `src/services/jobs/editing_segments.py:174` `_atomic_write_json(..., fsync=True)` → 委托 TU-04 canonical `utils/atomic_io.atomic_write_json`（保 sort_keys=False + 末尾换行 + fsync）| ✅（审计路径 `web_ui/...` 已过期，现在 `jobs/...`）|
| H5 | aligner getattr 读不存在 en_text | `src/services/alignment/aligner.py` `getattr.*en_text` **0 命中**（TU-01 全归零 + TU-07 源码守卫）| ✅ |

---

## 5. 现状快照：行数（含口径澄清）

> ⚠️ **口径校准（关键，本快照早期版本在此处自身有误，已更正）**：Deepseek 审计全程用**非空行**计数，本快照与 **file-size-guard 用总行数**（`wc -l` = `sum(1 for _ in open())`）。已逐文件实测：审计每个数字**精确等于**该文件非空行数（process.py 非空行=12,491、job_intercept=6,312、…六个全吻合）。两者都对，只是口径不同；file-size-guard 按**总行数**冻结，故下表以总行数为准、并列非空行。

| 文件 | 母方案(总行,6-24) | 审计(非空行) | **真实总行** | 真实非空行 | 基线 cap(总行) | 余量 |
|---|---|---|---|---|---|---|
| `src/pipeline/process.py` | 12,806 | 12,491 | **13,335** | 12,491 | 13,341 | 6 |
| `gateway/job_intercept.py` | 6,880 | 6,312 | **6,936** | 6,312 | 6,936 | **0** |
| `src/services/transcript_reviewer.py` | 4,173 | 3,769 | **4,296** | 3,769 | 4,296 | **0** |
| `src/services/gemini/translator.py` | 2,825 | 2,743 | **3,059** | 2,743 | 3,090 | 31 |
| `src/services/jobs/api.py` | 2,645 | 2,526 | **2,654** | 2,526 | 2,654 | **0** |
| `src/services/jobs/service.py` | — | — | **1,902** | — | 1,902 | **0** |
| `main.py` | 1,868 | 1,675 | **1,868** | 1,675 | 1,868 | **0** |
| `frontend .../edit/page.tsx` | — | ~1,975 | **1,976** | — | (前端不进 guard) | — |

要点：
- **审计的行数不是错误也不是"低估"**——是非空行口径，每个数字与实测非空行精确吻合；审计 §5 自己标注"方法差异"判断正确。（本快照初稿曾把两口径混比、误称审计"低估/方向反了"，特此更正——这正是我先前苛责 Deepseek 的同类毛病。校准锚点：`main.py` 母方案 1,868 = 真实总行 1,868，证明母方案用总行口径。）
- **file-size-guard 用总行**，工程上相关的是总行：process.py 总行 12,806(6-24)→**13,335**(现)，确有 +529 增长；现 ratchet 冻结于 13,341 cap、仅 6 行余量。
- **多数热点已被 guard 冻结在 0 余量**（job_intercept / transcript_reviewer / jobs-api / jobs-service / main.py）——已无法再长大，迫使后续改动"先拆再加"。
- process.py 仍是头号巨石：**13,335 总行 ≈ 16.7× 800 目标**（非空行 12,491 ≈ 15.6×），待 Option B 收敛（TU-14）。

---

## 6. 已完成 vs 未启动（按 Wave）

**✅ Wave A（高 ROI 低风险）+ Wave B（低风险标准化）= 全 9 单元完成**
止血 / 护栏激活 / 原子写统一 / admin 鉴权统一 / coerce+error payload / 类型契约+mypy 窄域 / 计费日志 / 构建&生产配置卫生。

**☐ Wave C（热点深挖）— 未启动**
- TU-09 `job_intercept.py`(6,936) route family 拆分
- TU-10 前端编辑页(1,976) route shell 化
- TU-11 前端语音选择共享模块
- TU-12 `jobs/api.py`(2,654) dispatch table 化
- TU-13 JobService post-edit 模块抽取

**☐ Wave D（收敛+性能）— 未启动**
- **TU-14 process.py Option B 输出收敛第一刀**（ADR `PROCESS_WORKFLOW_CONVERGENCE.md`，让 process.py 退成兼容壳消费 `ProjectWorkflow`/`OutputDispatcher`）
- TU-15 性能有界优化 · TU-16 DB 卫生 · TU-17 logs/events cursor + benchmark harness

**☐ Wave E（决策门）— 未启动**
- TU-18 治理决策（Job API→FastAPI / JSON→DB / OpenAPI→TS / 全仓阻断升级）

---

## 7. 真实的下一步优先级

不是"激活 CI"（已做），而是：

1. **TU-09 拆 `job_intercept.py`**（已 0 余量、被 guard 顶住，最该先拆；从 post-edit route family 起建立拆分模式）。
2. **TU-14 process.py Option B 收敛第一刀**（13,335 行头号巨石；配合 guard 阻止其继续长大）。
3. Wave C 其余（TU-10/11/12/13）可并行派发。

Wave C/D 启动节奏由项目主拍板。护栏与止血已为重构提供回归网（guard 冻结 + ruff/mypy 窄域 + 全量 set-diff 流程）。

---

## 8. 与 Deepseek 审计的关键分歧

| 审计声明 | 实际（origin/main 实测）|
|---|---|
| "CI 未激活、无 lint/full-suite/file-size-guard job"（§3.2/§7）| **3 个 job 全在 ci.yml**（118/164/183 行），18 天前由 TU-03(#41) 激活并阻断 |
| "pyproject 无 `[tool.pytest.ini_options]`"（§3.2）| **存在**于 pyproject.toml:86 |
| "CI 仍 212 行" / job 名 `frontend-lint`/`pytest-postgres` | ci.yml **220 行**；真名 `frontend`/`python-lint`/`backend-pg-integration`（审计 job 名是假的）|
| process.py 12,491 + "行数略高于实际(方法差异)" | **审计此项无误**——它用非空行(12,491)、file-size-guard 用总行(13,335)，同一文件两口径。本快照初稿误判为"低估/增长方向反了"，已更正（口径混比是我的错，非审计的错）|
| §6.4 规范标准化"未启动" | **TU-06(#44) 已做**（coerce/json_helpers/ErrorPayload/voice_reranker 去重）|
| §6.3 数据一致性"只 H4 fsync" | **TU-04(#42)** 统一原子写收口 7 处 |
| （未提）| TU-05/07/08/02 + TU-00-INDEX 整条 ship-unit 轨道全不知情 |
| "激活 CI 是最高优先级"（§10）| 该工作已完成；真实最高优先级是 **Wave C/D 结构拆分/收敛** |

审计对得上的：止血项已修（大体对）、结构优化未启动（对，但给错优先级理由）、process.py 是头号巨石且量级排序正确、file-size-guard 是关键创新、母方案质量高。

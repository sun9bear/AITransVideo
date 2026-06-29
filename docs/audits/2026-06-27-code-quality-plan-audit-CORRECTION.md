# 审计更正：2026-06-27 代码质量方案审计 — Claude Code 反驳核验

> 原审计：`docs/audits/2026-06-27-code-quality-plan-audit.md`
> Claude Code 反驳链：`docs/plans/code-quality-tasks/TU-00-INDEX.md` + `ci.yml` + `pyproject.toml`
> 核验日期：2026-06-27
> 核验方法：直接读取当前仓库文件 vs Claude Code 声称 vs 原审计声称

---

## 结论：原审计有 4 处事实错误。Claude Code 的反驳 3/4 成立。

---

## 错误一：CI jobs "未激活" — 错误 ❌

**原审计 §3.2 声称**：`python-lint`、`backend-full-suite`、`file-size-guard` 三个 CI job "未添加"。

**实际状态**：`.github/workflows/ci.yml` 第 118-220 行明确包含全部三个 job：

```yaml
# ci.yml:118 → python-lint job（ruff + mypy）
# ci.yml:164 → backend-full-suite job（全量测试 + coverage）
# ci.yml:183 → file-size-guard job（文件行数 ratchet）
```

加上原有的 `backend`、`backend-pg-integration`、`frontend`，CI 共 **7 个 job**。TU-03（PR #41）已于 2026-06-25 将此脚手架合并到 main。

**根因**：审计时确实读了 ci.yml 完整 220 行，但结论写错了——将"ruff/mypy/pytest-timeout 工具已安装"与"CI job 不存在"两个独立事实混淆。工具在 pyproject.toml，CI job 在 ci.yml，两者都已落地。

## 错误二：pyproject.toml 无 `[tool.pytest.ini_options]` — 错误 ❌

**原审计 §3.2 声称**：pyproject.toml "无 `[tool.pytest.ini_options]` 段"。

**实际状态**：`pyproject.toml:83-99` 明确包含完整配置：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-p no:cacheprovider -m 'not slow and not real_provider and not benchmark'"
markers = [
    "unit: 纯函数/小模块",
    "contract: API/schema/status/path parity 契约",
    "guard: 架构守卫，禁止回退",
    ...
]
```

TU-03（PR #41）已将此配置写入。

## 错误三：code-quality-tasks/ 目录 — 完全遗漏 ❌

**原审计**：全文零提及 `docs/plans/code-quality-tasks/`。

**实际状态**：该目录存在，包含 **20 个文件**，将母方案的 123 条缺陷清单拆为 18 个可独立执行的 Task Unit（TU-01 至 TU-18），每个有独立文档 + CodeX 审核决策 + 分步验收标准。

当前进度：

| Wave | 单元 | 状态 | PR |
|---|---|---|---|
| A 止血 | TU-01 四修 | ✅ | #40 已合并 |
| A 构建 | TU-02 build hygiene | ☐ | 待执行 |
| A 护栏 | TU-03 质量脚手架 | ✅ | #41 已合并 |
| B 标准化 | TU-04 原子写 | ✅ | #42 已合并 |
| B 标准化 | TU-05 admin 鉴权 | ✅ | #43 已合并 |
| B 标准化 | TU-06 共享 helper | ✅ | #44 已合并 |
| B 标准化 | TU-07 类型契约 | ✅ | #47 已合并 |
| B 标准化 | TU-08 计费日志 | ✅ | #51 已合并 |
| C 热点 | TU-09 job_intercept 拆分 | ☐ | 待执行 |
| C 热点 | TU-10 前端编辑页 | ☐ | 待执行 |
| C 热点 | TU-11 前端语音选择 | ☐ | 待执行 |
| C 热点 | TU-12 jobs/api dispatch | ☐ | 待执行 |
| C 热点 | TU-13 JobService 模块 | ☐ | 待执行 |
| D 收敛 | TU-14 process 收敛 | ☐ | 待执行 |
| D 收敛 | TU-15 性能优化 | ☐ | 待执行 |
| D 收敛 | TU-16 DB 卫生 | ☐ | 待执行 |
| D 收敛 | TU-17 events/benchmark | ☐ | 待执行 |
| E 治理 | TU-18 治理门 | ☐ | 待执行 |

**Wave A + Wave B 共 7/8 已完成合并**。这是审计最大的盲区。

## 错误四：行数 // Claude Code 也错了

| 文件 | 原审计 | Claude Code 声称 | 本次实测 |
|---|---|---|---|
| `process.py` | 12,491 | 13,335 | **12,491** |
| `job_intercept.py` | 6,312 | 6,936 | **6,312** |

**Claude Code 的行数在这次 checkout 上不成立。** 12,491 不是"低估"，是实际测量值。可能 Claude Code 在含有未合并 TU 变更的分支上测量。

原审计 §5 中引用方案文档的行数（12,806 / 6,880）与实测（12,491 / 6,312）的偏差，原审计已标注为"方案行数略高于实际（方法差异）"——这是准确的。

---

## 更正后的正确状态

| 方面 | 原审计结论 | 更正后结论 |
|---|---|---|
| CI 护栏 | "未激活" | **已激活**——7 job 全部在线，TU-03 PR #41 已于 6/25 合并 |
| pytest 配置 | "无 ini_options" | **已配置**——pyproject.toml:83-99，含 asyncio_mode/addopts/markers |
| Wave A+B 止血+护栏 | "H2/H4/H5 已修，其余未动" | **8 个 TU 中 7 个已完成并合并 main**——TU-01/03/04/05/06/07/08 全部 ✅ |
| Wave C+D 结构优化 | "全部未启动" | **正确**——TU-09 至 TU-18 全部 ☐。job_intercept 仍 6,312 行，process.py 仍 12,491 行 |
| 实施追踪体系 | "未发现" | **完整存在**——`docs/plans/code-quality-tasks/` 含 18 个 TU + CodeX 审核 |
| process.py 行数 | 12,491 | 12,491（Claude Code 的 13,335 在本次 checkout 不成立） |

---

## 原审计中仍然正确的部分

1. **方案质量评价**：合并策略合理、123 条缺陷清单可执行、file-size-guard 是关键创新——评得中肯
2. **结构优化确实未启动**：TU-09 至 TU-18 全部待执行——job_intercept 拆分、process.py 收敛、前端编辑页拆分均未开始
3. **行数数量级和排序**：正确
4. **process.py 是头号巨石**：12,491 行 = 15.6× 800 行上限，核心论据成立

---

## 更正后的建议

**废弃原审计 §9.1 "激活 CI" 的建议**——它已由 TU-03 在 6月25日完成。正确的下一步是：

1. **执行 TU-02**（构建卫生：删 dev bind-mount、pin cloudflared、补 .env.example）
2. **执行 Wave C**（TU-09 `job_intercept` 拆分是最高 ROI 的热点深挖）
3. **维护 file-size-guard 基线**——process.py 的 12,491 行冻结基线已就位，任何新功能不得继续拉大这个数字

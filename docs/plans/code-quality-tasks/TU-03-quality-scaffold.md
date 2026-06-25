# TU-03 · 质量护栏脚手架（ruff / pytest 配置 / file-size guard / pre-commit / CI）

- **目标**：给整条 Python 代码库装上「可度量、可渐进、首周不阻断」的质量护栏。这是所有后续重构的回归网与防债加速的闸门，ROI 最高。
- **关联发现**：TOOL-01（无 ruff）· TOOL-03（CI 只跑 14/8474）· TOOL-04（无 pre-commit）· TEST-01..06（无 pytest 配置/超时/cov）· STRUCT-01/02（文件失控需 file-size 棘轮）
- **前置依赖**：无（建议早做——它是 TU-07 mypy 门、以及所有重构回归验证的基础）。
- **建议分支**：`quality/quality-scaffold`
- **预估工时**：S–M
- **配置真源**：所有片段以母方案 [`../2026-06-24-code-quality-optimization-plan-MERGED.md`](../2026-06-24-code-quality-optimization-plan-MERGED.md) §10 为准（已按 CodeX review 校准为 report-only/nightly/changed-files 形态）。
- **命令环境**：本文验收命令默认 **Git Bash / CI Linux**（仓库已配 Bash 工具）。PowerShell 执行者改用等价命令（`$env:TEMP` 代 `/tmp`、`Select-Object -Last N` 代 `tail`、`Test-Path` 代 `test -f`、避免 `<(...)` 进程替换）。

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **uv.lock 口径**：首选同步运行 `uv lock` 并提交锁文件；若本阶段仅用 pip extras 不动锁，须在 PR 描述显式注明——二选一，不留口径不一致。
- **首周 full suite 保持 report-only / continue-on-error**：全仓 ruff 与 backend-full-suite job 首周均 `continue-on-error: true`（或 nightly）；**不设 `--cov-fail-under` 覆盖率硬门**；阻断只针对 changed files + 新增 guard。
- **ruff 阻断范围**：CI `python-lint` job 仅对 PR 改动文件阻断；全仓扫描保持 report-only（`--exit-zero`）。
- **pre-commit 配置可提交，但不强制开发者本地安装**：`.pre-commit-config.yaml` 进仓库，CI 通过 `pre-commit run` 验证；不在文档/DoD 中要求所有贡献者必须 `pre-commit install`。
- **分支名**：`quality/quality-scaffold`（与 Step 0 一致，固定此名）。
- **coverage 策略**：本单元不设任何 `--cov-fail-under` 门槛；覆盖率收集仅用于基线可见性，硬门留给 Phase 2+。
- **窄域 mypy**：本单元 mypy 仅窄域扫描（见 pre-commit + CI python-lint），不做全仓 mypy 严格化，不批量写 `# type: ignore`。

---

## 不在本单元范围

- 不在本单元修任何 lint/type **报错**（只装工具 + 建基线 + report-only）。真正清理交给各重构单元。
- mypy 严格化、全仓阻断属 Phase 2+（本单元只装窄域 mypy + report-only ruff）。

## 必守不变量

- **首周一律不阻断 PR**：ruff 全仓 report-only（仅改动文件阻断）、full-suite nightly/`continue-on-error`、coverage 先不设 `--cov-fail-under`。避免「质量治理第一步=CI 大爆炸」。
- **fail-closed 顺序**：先装 `pytest-timeout` 再开全量 job，否则挂死测试会让 CI 永久 hang（当前 `@pytest.mark.timeout` 是 no-op）。

---

## Step 0 · 确认现状

```bash
git switch -c quality/quality-scaffold
python -m pytest --collect-only -q 2>&1 | tail -1     # 记录基线：应约 8,474 tests
python -m pip show ruff mypy pytest-timeout pytest-cov pytest-xdist 2>/dev/null   # 记录哪些未装
test -f pyproject.toml && grep -c "tool.pytest\|tool.ruff\|tool.mypy" pyproject.toml   # 预期 0
```

## Step 1 · 装 dev 依赖

按 §10.1 把 `ruff>=0.5.0` / `mypy>=1.11` / `pytest-cov>=5.0` / `pytest-timeout>=2.3` / `pytest-xdist>=3.6` 加入 `pyproject.toml` 的 dev extras，并本地安装。

✅ 已决策（CodeX 2026-06-25）：dev 工具写进 `pyproject.toml` 后**首选同步运行 `uv lock`** 更新锁文件；若本阶段决定只用 pip extras、不动锁，须在 PR 描述显式注明。二选一，不留口径不一致。

**验收**：`python -m pip show ruff mypy pytest-timeout pytest-cov pytest-xdist` 全部有 Version；`git status --short uv.lock` 反映所选口径（已更新 / 故意未动）。

## Step 2 · 加 `[tool.ruff]` + 建 report-only 基线

按 §10.2 写入 `[tool.ruff]`（select `E/W/F/I/UP`，per-file-ignores 含 alembic/tests/scripts）。

**验收**：
```bash
ruff check src/ gateway/ --exit-zero --output-format=github > /tmp/ruff_report.txt; echo "report 行数:"; wc -l < /tmp/ruff_report.txt   # 出报告、退出码 0
ruff format src/ gateway/ --diff > /tmp/fmt.diff; echo "format diff 已生成（不写盘）"
```
记录基线问题数到 PR 描述（debt baseline）。**不**用 `--add-noqa` 全仓压制。

## Step 3 · 加 `[tool.pytest.ini_options]`（修 TEST-02/03/05/06）

按 §10.4 写入：`asyncio_mode="auto"`、`addopts="-p no:cacheprovider -m 'not slow and not real_provider and not benchmark'"`、注册 markers（`postgres/timeout/slow/real_provider/guard/contract/integration/unit/benchmark`）。

**验收**：
```bash
python -m pytest --collect-only -q 2>&1 | grep -ci "PytestUnknownMarkWarning"   # 0（mark 已注册）
python -m pytest tests/test_process_runner_watchdog.py -q --timeout=30 -p no:cacheprovider 2>&1 | tail -3   # timeout 插件生效（不再是 no-op）
```

## Step 4 · file-size ratchet（基线 + guard，防 STRUCT-01/02 继续长大）

按 §10.6：先用脚本生成 `tools/file_size_baseline.json` 并提交，再放 guard 脚本（白名单文件只许变小、新文件 ≤800）。

**验收**：
```bash
python <<'PY'  # 生成基线（见 §10.6 完整脚本）
import json,pathlib
b={}
for p in list(pathlib.Path("src").rglob("*.py"))+list(pathlib.Path("gateway").rglob("*.py"))+[pathlib.Path("main.py")]:
    n=sum(1 for _ in open(p,encoding="utf-8",errors="ignore"))
    if n>800: b[str(p).replace("\\","/")]=n
pathlib.Path("tools").mkdir(exist_ok=True)
json.dump(dict(sorted(b.items(),key=lambda kv:-kv[1])),open("tools/file_size_baseline.json","w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("baseline entries:",len(b))
PY
test -f tools/file_size_baseline.json && echo "baseline 存在"   # 应约 42 条
# guard 当前应通过（baseline==现状）：
python -c "import json,pathlib; B=json.load(open('tools/file_size_baseline.json')); \
v=[(str(p).replace('\\\\','/'),sum(1 for _ in open(p,encoding='utf-8',errors='ignore'))) for p in list(pathlib.Path('src').rglob('*.py'))+list(pathlib.Path('gateway').rglob('*.py'))+[pathlib.Path('main.py')]]; \
bad=[(f,n) for f,n in v if n>B.get(f,800)]; print('violations:',bad); assert not bad"
```

## Step 5 · `.pre-commit-config.yaml`（TOOL-04）

按 §10.5 写入（ruff + ruff-format + 基础卫生 hooks + 窄域 mypy）。

✅ 已决策（CodeX 2026-06-25）：`.pre-commit-config.yaml` 提交进仓库，CI 通过 `pre-commit run` 统一验证；**不强制每个开发者本地安装**（不在 README/onboarding 中设为必须步骤）。

**验收**：
```bash
pre-commit run --all-files 2>&1 | tail -20   # 能跑（历史问题报告，不要求全绿）
# 注：本地 install 非强制；CI job 会统一执行
```

## Step 6 · CI 三个 job（TOOL-03 + TEST-01）

✅ 已决策（CodeX 2026-06-25）：首周 CI 策略如下——

- `python-lint`：ruff **changed-files 阻断**（PR 改动文件不达标则 fail）+ **全仓 report-only**（`--exit-zero`，仅输出基线报告）+ 窄域 mypy（不批量 `# type: ignore`）。
- `backend-full-suite`：`continue-on-error: true`（或 nightly schedule）+ `--timeout=120 -n auto`，**不设 `--cov-fail-under` 覆盖率硬门**（覆盖率仅收集可见，不阻断 PR）。
- `file-size-guard`：读 `tools/file_size_baseline.json`，新文件 ≤800 行 / 白名单文件只许变小，阻断 PR。

按 §10.6 的 YAML 写入 `.github/workflows/ci.yml`。

**验收**：
```bash
python -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci.yml')); \
j=d['jobs']; assert {'python-lint','backend-full-suite','file-size-guard'} <= set(j), set(j); \
assert j['backend-full-suite'].get('continue-on-error') is True; \
lint_steps=str(j['python-lint']); assert '--exit-zero' in lint_steps or 'exit-zero' in lint_steps or 'report' in lint_steps.lower(), 'full-suite ruff must be report-only'; \
print('CI jobs OK')"
```
（可选）推分支触发一次 CI，确认三 job 出现且不因历史问题（全仓 ruff / full suite）阻断 PR。

## Step 7 · `scripts/check_quality.*` 一键本地门

提供一条命令跑「核心守卫 + ruff report + 窄域 mypy + changed-files lint」，供本地与各重构单元复用。

**验收**：`bash scripts/check_quality.sh`（或 `.ps1`）可运行并打印各项结果，退出码反映 changed-files 是否干净。

## 测试计划

- 不改业务代码，主要验证「配置/CI 自身可解析、工具可运行、首周不阻断」。
- `pytest --collect-only` 数量不回退；`PytestUnknownMarkWarning` 归零；既有 14 守卫测试仍绿。

## 回滚方案

纯新增配置文件 + CI job，逐个文件独立 commit。任何一项有副作用单独 revert；CI job 可先设 `continue-on-error` 观察再转硬门。

## 完成定义（DoD）

- [ ] 5 个 dev 工具装好（ruff/mypy/pytest-timeout/pytest-cov/pytest-xdist）。
- [ ] `[tool.ruff]` 就位，全仓 report-only 出基线、无 `--add-noqa` 全仓压制。
- [ ] `[tool.pytest.ini_options]` 就位，marks 注册（warning 归零）、timeout 插件生效、默认排除 slow/real_provider/benchmark。
- [ ] `tools/file_size_baseline.json` 生成并提交，guard 通过。
- [ ] `.pre-commit-config.yaml` 就位可运行（**不要求每位开发者本地 install**，CI 统一跑）。
- [ ] CI 增 3 job，首周均不阻断 PR（changed-files / continue-on-error / **不设 `--cov-fail-under` 覆盖率硬门**）。
- [ ] `scripts/check_quality.*` 可一键运行。
- [ ] 各项独立 commit，显式 pathspec。
- [ ] uv.lock 口径明确（已同步 `uv lock` 或 PR 描述注明"仅 pip extras 不动锁"）。
- [ ] mypy 仅窄域扫描，无批量 `# type: ignore` 新增。

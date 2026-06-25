# TU-01 · 止血四修（财务可见性 / 取消竞争 / 写盘持久化 / 数据契约）

- **目标**：修掉 4 个「现在就坏、但纯战略视角会漏」的隐患，全部 S 级、不改业务语义、高 ROI。
- **关联发现**：H2(EH-003 + 同源 EH-004/005) · H3(ASYNC-06) · H4ᵐⁱⁿ(DRY-02 的最小子集) · H5(TS-01)
- **前置依赖**：无（可立即开工）。
- **建议分支**：`quality/hotfix-stabilize`
- **预估工时**：S（半天内 4 个修复 + 测试）

> 注：H1（根 `projects/` 红灯守卫）已于 2026-06-24 清理、守卫通过，不在本单元——保持守卫防回归即可。
>
> **命令环境**：本文验收命令默认 **Git Bash / CI Linux**（仓库已配 Bash 工具）。PowerShell 执行者改用等价命令（`grep`→`Select-String`、`test -f`→`Test-Path`）。

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **分支名确定**：使用 `quality/hotfix-stabilize`（已定，可直接执行 Step 0 中的 `git switch -c quality/hotfix-stabilize`）。
- **H2 日志策略**：只加结构化日志（`logger.exception` / `logger.error` / `logger.warning`），**不改 `return 0` 兜底语义**，不影响计费金额计算路径。
- **H3 helper 覆盖范围**：所有 `running` 写入（初始快照 + 每段进度快照）**均须走 `_write_running_status` helper**；进度快照中硬编码的 `"cancel_requested": False` 必须删除，由 helper 统一决定；终态写入保持原样。
- **H5 字段约束**：**禁止新增 `en_text` 字段**；aligner 四处 `getattr(segment, "en_text", "")` 统一改读既有 `segment.source_text`，不引入双事实源。
- **范围边界确认**：H4 只补 `_atomic_write_json` 的 fsync（最小修），其余原子写收口留 TU-04；H5 字段改名留 TU-07。
- **本单元可直接执行**，无需进一步确认。

## 不在本单元范围（out-of-scope）

- H4 这里只给 `editing_segments._atomic_write_json` 补 `fsync`（最小修）；**6 处原子写收口为单一 helper 是 TU-04**。
- H5 这里只把 aligner 4 处 `getattr` 改读既有 `source_text`（**不新增字段**）；**tts_generator 其余 ~60 处 getattr + `en_text` 命名整改 + mypy 窄域是 TU-07**。
- print→logger 大迁移、付费 TTS 事件日志是 **TU-08**；本单元只补 H2 这三处计费静默点。

## 必守不变量

- **付费 API 红线**：本单元不新增任何付费调用；H2 只加日志、不改 `return 0` 的兜底语义（避免影响计费金额）。
- 修复以「让失败可见」为原则，不改对外行为。

---

## Step 0 · 确认现状

```bash
git switch -c quality/hotfix-stabilize
python -m pytest tests/test_legacy_cleanup_guards.py::test_no_root_projects_dir -q   # 应 1 passed（确认基线干净）
```
逐条复核下列 `file:line` 与本文一致（多 agent 仓库行号可能漂移）。

---

## Step 1 · H2 计费静默归零 → 加可见性（不改金额语义）

**位置**：`gateway/cost_management.py:851-852`（`_derive_credits_from_minutes` 的 `except Exception: return 0`）；同源 `gateway/billing.py`（`ensure_subscription_bucket` 失败处）、`gateway/credits_service.py:121/137/146`（pricing fallback）。

**改法**：
1. `cost_management.py` 顶部确保有模块 logger（`import logging; logger = logging.getLogger(__name__)`）。
2. `_derive_credits_from_minutes` 的 except 改为：
   ```python
   except Exception:
       logger.exception("derive_credits_failed job=%s minutes=%s", getattr(job, "job_id", "?"), minutes)
       return 0
   ```
3. 在 `return 0` 前加「可疑欠费」告警（`minutes` 有值却算出 0）：
   ```python
   credits = estimate_credits(...)
   if credits == 0 and minutes:
       logger.error("ZERO_CREDITS_SUSPECT job=%s minutes=%s mode=%s", getattr(job, "job_id", "?"), minutes, job.service_mode)
   return credits
   ```
4. `billing.py` bucket 失败的 `except` 加 `exc_info=True`；`credits_service.py:121/137/146` 三处 fallback 加 `logger.warning("pricing_fallback ...")`。

**该步验收**：
```bash
grep -nE "logger\.(exception|error|warning)" gateway/cost_management.py | grep -E "derive_credits|ZERO_CREDITS" # 命中 ≥2
grep -n "exc_info=True" gateway/billing.py    # 命中 ≥1
grep -cn "pricing_fallback" gateway/credits_service.py  # ≥1（或等价 warning）
```
+ 新增单测 `tests/test_cost_management_silent_zero.py`：monkeypatch `estimate_credits` 抛异常 → 用 `caplog` 断言记录了 `derive_credits_failed` 且函数返回 0（语义不变）。`pytest tests/test_cost_management_silent_zero.py -q` → passed。

---

## Step 2 · H3 regenerate 取消竞争 → 所有 running 写入保留 cancel

**位置**：`src/services/jobs/regenerate_all_async.py` 的**两处** running 写入——初始快照 `:371-380`，**以及每段进度快照 `:395-411`（硬编码 `"cancel_requested": False`）**。

**根因**：`cancel_requested` 是「外部取消端点（`:237-241` RMW 写 True）」与「批次进度写入」共享的字段。批次每段循环里先读状态（`:389`）再写进度快照（`:395`），而进度快照**硬编码 `cancel_requested: False`**。取消请求若夹在「读取」与「下一发进度写入」之间，会被覆盖回 False → 取消被静默吞掉（对应测试间歇红）。**只修初始快照不够**——必须让所有 running 写入都保留已存在的取消标记。

**改法**：加一个「保留取消」helper，所有 running 写入走它（终态写入——cancelled 摘要 `:447/461`、final `:479`——保持原样）：
```python
def _write_running_status(project_dir: Path, task_id: str, payload: dict[str, Any]) -> None:
    """写 running 进度时，绝不把已存在的 cancel_requested=True 覆盖回 False。"""
    existing = _read_status_raw(project_dir, task_id) or {}
    if existing.get("cancel_requested") is True:
        payload = {**payload, "cancel_requested": True}
    _write_status(project_dir, payload)
```
把初始快照（`:372`）与每段进度快照（`:395`）两处 `_write_status(...)` 改为 `_write_running_status(project_dir, task_id, {...})`，并**删掉进度快照里硬编码的 `"cancel_requested": False`**（交由 helper 决定）。

> 残留 TOCTOU（诚实标注）：helper 仍是「读-改-写」，与取消端点之间存在极小窗口。本止血把窗口从「整段 TTS 时长（2–5s）」缩到「两次相邻 JSON 写之间（μs 级）」，足够。彻底消除需让取消端点与进度写入共享同一文件锁做 RMW——留给后续，本单元不扩面。

**该步验收**（命令默认 Git Bash / CI Linux）：
```bash
grep -c '"cancel_requested": False' src/services/jobs/regenerate_all_async.py   # 应为 1（仅 _initial_status 默认值）；进度快照硬编码已移除
grep -c "_write_running_status(" src/services/jobs/regenerate_all_async.py      # ≥2（初始 + 每段进度都走 helper）
```
+ 新增确定性回归 `tests/test_regenerate_cancel_race.py`：①预写 `cancel_requested=True` → 跑 `_run_batch` → 断言 0 段处理、终态 cancelled；②mid-batch：第 1 段后置位取消 → 断言第 2 段不再处理。`pytest tests/test_regenerate_cancel_race.py -q` → passed（修复前 FAIL）。

---

## Step 3 · H4 editing 写盘漏 fsync（最小修）

**位置**：`src/services/jobs/editing_segments.py:183-187`（`_atomic_write_json`：`json.dump` 后直接 `tmp_path.replace(path)`，中间无 flush/fsync）。

**风险**：`os.replace` 只保证 rename 原子，不保证文件**内容**已落盘；断电时可能 rename 已持久但数据未刷 → `segments.json`/`voice_map.json` 变空/截断。

**改法**：在 `with open(...)` 块内、replace 前补 flush+fsync：
```python
with open(tmp_fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())   # 修 H4：保证内容落盘后再原子 rename
# os.replace is atomic on both POSIX and Windows (ReplaceFileW).
tmp_path.replace(path)
```
确认文件已 `import os`（未导入则补）。

**该步验收**：
```bash
grep -n "os.fsync" src/services/jobs/editing_segments.py   # 命中 1（在 _atomic_write_json 内）
python -m pytest tests/ -q -k "editing_segment" -p no:cacheprovider   # 现有 editing 测试仍全绿
```

---

## Step 4 · H5 en_text 数据契约 bug

**位置**：`src/services/alignment/aligner.py:361/542/591/778`（`en_text=getattr(segment, "en_text", "")` 恒返回空串——因为 `DubbingSegment` 无 `en_text` 字段）。

**改法（不新增字段，避免双事实源）**：`DubbingSegment` **已有** `source_text` 字段（`src/services/gemini/translator.py:260`，存源语言文本）。把 aligner 四处 `getattr(segment, "en_text", "")` 改为 `segment.source_text`，让 `AlignedSegment.en_text` 直接取自既有源文本，**不**新增重复的 `en_text` 字段去维护同步。

> 命名说明：`AlignedSegment.en_text` 实际语义是「源语言字幕行」（英文源时即英文），并非强绑 English；字段改名属 TU-07 的类型/命名整改，本单元只修「恒空」这个 bug。

**该步验收**（命令默认 Git Bash / CI Linux）：
```bash
grep -c 'getattr(segment, "en_text"' src/services/alignment/aligner.py   # 0
grep -c "segment.source_text" src/services/alignment/aligner.py          # ≥4（四处改为直接取 source_text）
python -m pytest tests/ -q -k "align" -p no:cacheprovider                # 对齐相关测试仍绿
```
+ 新增/扩展测试锁住：构造带非空 `source_text` 的 `DubbingSegment` → 跑对齐 → 断言 `AlignedSegment.en_text == source_text`（修复前为 `""`）。

> 影响边界（诚实）：`AlignedSegment.en_text` 下游主要落 deprecated 的 `editor_package_writer._write_srt_from_segments`，今天活影响有限——但它是 TS-02（65+ getattr 架空 slots）与「引入 mypy」的实证锚点，TU-07 会接着扩面。

---

## 测试计划

- 新增：`test_cost_management_silent_zero.py`、`test_regenerate_cancel_race.py`。
- 回归：`pytest -q -k "editing_segment or align or regenerate" -p no:cacheprovider` 全绿。
- 守卫：`pytest tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py -q` 全绿（不破坏既有契约）。

## 回滚方案

四步互相独立、各自一个 commit（`git commit -- <file>`）。任一步出问题 `git revert <该步 commit>` 即可，不影响其余三步。

## 完成定义（DoD）

- [ ] H2：3 处计费静默点均有结构化日志；`ZERO_CREDITS_SUSPECT` 告警就位；新单测绿；`return 0` 语义**严格不变**（✅ 已决策 CodeX 2026-06-25：只加日志，不改兜底金额语义）。
- [ ] H3：**所有 running 写入**（初始 + 每段进度）经 `_write_running_status` 保留 `cancel_requested`（✅ 已决策 CodeX 2026-06-25：初始 + 每段进度均走同一 helper）；进度快照硬编码 `"cancel_requested": False` 已删；确定性竞争测试（含 mid-batch）由红转绿。
- [ ] H4：`_atomic_write_json` 含 `os.fsync`；editing 测试全绿。
- [ ] H5：aligner 4 处改读 `segment.source_text`（getattr 清零、**禁止新增 `en_text` 字段**）（✅ 已决策 CodeX 2026-06-25：改读既有 `source_text`，不引入双事实源）；测试锁住 `en_text==source_text`；对齐测试绿。
- [ ] 全程未引入任何自动付费调用；守卫测试全绿。
- [ ] 4 个独立 commit，用显式 pathspec 提交，未 `git add .`。

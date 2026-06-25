# TU-13 · JobService post-edit 模块抽取

- **目标 / 价值**：`src/services/jobs/service.py` 当前 **1,902 行**（是 800 行上限的 2.4 倍），其中从 `:444`（`enter_editing`）到 `:1765`（`clear_editing_voice_override` 结束）的约 **1,320 行**全是 post-edit 工作流逻辑——editing 生命周期、文本段落 CRUD、TTS 再生、voice map 覆盖、批量 TTS、拆分、建议拆分、source audio 预览、审计事件等混入同一个类。这些方法与 `submit_job` / `continue_job` / `get_job` 等核心方法共享同一个 `JobService` 实例，无法在不构造完整服务的情况下对 post-edit 流程做单元测试。本单元通过将上述约 1,320 行抽取为独立的 `EditingApplicationModule`，提供窄接口（只需 `JobStore` + 可 fake 的依赖），实现：单独测试不依赖 `ProcessJobRunner`；`service.py` 降至 **700 行以下**；`phase1 guards` 全绿（AST 守卫不改动）。
- **关联发现**：STRUCT-07（`jobs/service.py` 1,902 行；editing 工作流 + TTS regen + voice map + 清理混合）
- **前置依赖**：建议在 TU-09（`gateway/job_intercept.py` 拆分）之后执行，避免在 post-edit route policy 层与 service 层同时改动造成合并冲突；TU-03 质量脚手架（ruff / mypy / file-size guard）建议先落，但不是硬前置。
- **建议分支**：`quality/jobservice-postedit`
- **预估工时**：M（约 3–4 天；分 5 步迁移，含 contract 测试编写；不含 TU-09 耗时）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令：`grep` → `Select-String`、`wc -l` → `(Get-Content file | Measure-Object -Line).Lines`、`test -f` → `Test-Path`、`tail -n` → `Select-Object -Last N`；避免 `<(...)` 进程替换。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **测试文件只扩展不覆盖**：若 `tests/test_editing_application_module.py` 执行前已存在，只向其追加 / 扩展用例，不得覆盖重写；Step 1 的"红灯先行"测试也须以 append 而非 overwrite 方式写入。
- **`suggest_split_for_segment` 只可 user-initiated**：该方法调用付费 LLM（S2 Pass 1），迁入 `EditingApplicationModule` 后，模块内部绝不新增对该方法的自动、批量或后台调用路径；DoD 增加对应 AST 守卫检查要求。
- **regen async/thread 生命周期先留在 `JobService` 薄层**：`regenerate_selected_dirty_segments_async` / `regenerate_all_dirty_segments_async` 若间接依赖 `service.runner`（`ProcessJobRunner`）或外部 status_store Adapter，不强制迁入 `EditingApplicationModule`——保留在 `service.py` 薄层中处理线程启动和取消逻辑；仅当 `EditingApplicationModule` 显式接收 runner/status_store Adapter **且**有取消令牌测试覆盖时，才可将完整实现迁入。
- **回滚优先 `git revert`**：`reset --hard` 仅限"本地尚未 push 的 feature 分支"场景，且须项目主明确确认后执行；`branch -D` 同样须项目主确认；这两条在回滚方案中已明文写定，不再作为开放决策。

---

## 不在本单元范围（out-of-scope）

- 修复 editing 相关逻辑 bug（本单元只做等价搬迁，不改行为）
- 拆分 `editing_commit.py`（当前 1,267 行，属独立后续任务）
- 拆分 `editing_segments.py`（当前 1,942 行，属独立后续任务）
- `JobService` 中非 post-edit 部分的重构（`submit_job`、`continue_job`、`get_job` 等，≈ 580 行，不在本单元）
- `process.py` 任何改动（守卫明确要求走 Option B，本单元不触碰）
- 修改 `gateway/job_intercept.py` 中的 post-edit policy 路由（TU-09 范围）
- 向 `EditingApplicationModule` 注入真实 TTS provider（runtime_wiring.py 的 `_segment_tts_caller` 注入逻辑保持不变）
- 前端路由变更

---

## 必守不变量

以下不变量在本单元每次 commit / PR 中必须保持：

1. **commit 管线永不调用 `tts_generator.*`**：`editing_commit.py` 的 alignment / publish 阶段代码不得引用 `tts_generator`。`tests/test_phase1_guards.py::test_alignment_modules_do_not_call_tts_generator`、`test_publish_modules_do_not_call_tts_generator`、`test_editing_commit_pipeline_does_not_call_tts_generator` 三个 AST 守卫必须全绿（见 `tests/test_phase1_guards.py:95–123`）。迁移不得新增 `tts_generator` 导入路径。
2. **re-TTS 只在 user-initiated 端点触发**：`_not_wired_tts_caller`（`editing_tts.py:155`）作为默认占位（抛 `TtsNotWiredError` / HTTP 501），只有通过 `runtime_wiring.apply_runtime_wiring` 注入真实 caller 后才激活。`EditingApplicationModule` 不得绑定真实 TTS provider；保持 `getattr(self, "_segment_tts_caller", None)` 的 DI 模式，允许测试注入 fake caller。
3. **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / LLM / ASR 绝不在 fallback / except / retry / batch 中自动触发。迁移后 AST 扫描不得在 `EditingApplicationModule` 内出现 `tts_generator`、`minimax_tts`、`voice_clone` 等受控符号的自动调用。
4. **公开接口不变**：`JobService` 对外的 post-edit 方法签名（`enter_editing`、`cancel_editing`、`commit_editing`、`regenerate_segment_tts`、`set_editing_voice_override` 等）仍作为转发薄层存在于 `service.py` 中（委托到 `EditingApplicationModule`），确保 `src/services/jobs/api.py` 与 `tests/` 的所有现有导入路径零改动。
5. **`__init__.py` 公开符号不变**：`src/services/jobs/__init__.py` 当前导出的符号不得减少；`EditingApplicationModule` 只作为实现细节，不强制导出到包级别（除非测试需要 fake 实例化）。
6. **默认测试不接真实外部服务**：迁移后所有测试继续 mock JobStore / TTS caller，不新增真实网络调用或真实文件系统依赖（除 `tmp_path` fixture）。
7. **process.py 走 Option B**：本单元只改 `src/services/jobs/` 层，不触碰 `src/pipeline/process.py`。
8. **gateway 侧不 import `services.jobs`**：`gateway/` 目录不得因本单元改动而新增对 `services.jobs.service` 或 `EditingApplicationModule` 的直接 import（gateway 容器不装 pydub 等传染依赖，见 `CLAUDE.md`）。

---

## Step 0 · 确认现状

```bash
# 0-a. 建分支
git switch -c quality/jobservice-postedit

# 0-b. 确认 service.py 行数（spec 值：1,902；行号可能漂移，以实测为准）
wc -l src/services/jobs/service.py
# 预期：1902（±20 因近期提交）

# 0-c. 确认关键方法的实际行号（spec 给出预期值，以本命令输出为准）
grep -n "def enter_editing\|def cancel_editing\|def commit_editing\|def revert_unsynced_text_segments\|def get_editing_segments\|def patch_editing_segment\|def preview_bulk_replace_terms\|def apply_bulk_replace_terms\|def split_editing_segment_many\|def suggest_split_for_segment\|def get_suggest_split_quota\|def split_editing_segment\b\|def preview_editing_segment\|def prepare_preview_source\|def mark_editing_segment\|def regenerate_segment_tts\|def accept_segment_draft_tts\|def discard_segment_draft_tts\|def regenerate_all_dirty\|def regenerate_selected_dirty\|def regenerate_all_dirty_segments_async\|def get_regenerate_all_status\|def request_regenerate_all_cancel\|def get_editing_voice_map\|def set_editing_voice_override\|def clear_editing_voice_override" src/services/jobs/service.py
# 预期（main 分支实测值；行号如漂移以本命令输出为准）：
#   enter_editing                   :444
#   cancel_editing                  :564
#   commit_editing                  :631
#   revert_unsynced_text_segments   :673
#   get_editing_segments            :803
#   patch_editing_segment           :817
#   preview_bulk_replace_terms      :863
#   apply_bulk_replace_terms        :886
#   split_editing_segment           :1025
#   suggest_split_for_segment       :1086
#   get_suggest_split_quota         :1119
#   split_editing_segment_many      :1127
#   preview_editing_segment_source_audio :1217
#   prepare_preview_source_cache    :1242
#   mark_editing_segment_status     :1267
#   regenerate_segment_tts          :1286
#   accept_segment_draft_tts        :1371
#   discard_segment_draft_tts       :1392
#   regenerate_all_dirty_segments   :1456
#   regenerate_selected_dirty_segments_async :1484
#   regenerate_all_dirty_segments_async :1529
#   get_regenerate_all_status       :1557
#   request_regenerate_all_cancel   :1576
#   get_editing_voice_map           :1597
#   set_editing_voice_override      :1603
#   clear_editing_voice_override    :1696

# 0-d. 确认关键私有辅助方法行号
grep -n "def _require_editing\|def _emit_user_edit_event\|def _emit_editing_session_started\|def _summarize_editing_baseline\|def _emit_post_edit_cancelled\|def _emit_post_edit_committed\|def _emit_post_edit_segment_patch_audit\|def _emit_post_edit_split_audit\|def _emit_post_edit_tts_regenerated\|def _emit_post_edit_draft_tts_event\|def _emit_post_edit_voice_override_audit\|def _editor_tts_segments_was_empty" src/services/jobs/service.py
# 预期：
#   _emit_user_edit_event           :115
#   _editor_tts_segments_was_empty  :470
#   _emit_editing_session_started   :494
#   _summarize_editing_baseline     :530
#   _emit_post_edit_cancelled       :602
#   _emit_post_edit_committed       :697
#   _require_editing                :790
#   _emit_post_edit_segment_patch_audit :944
#   _emit_post_edit_split_audit     :1169
#   _emit_post_edit_tts_regenerated :1330
#   _emit_post_edit_draft_tts_event :1413
#   _emit_post_edit_voice_override_audit :1736

# 0-e. 确认现有子模块结构
ls src/services/jobs/
# 应看到：editing.py editing_tts.py editing_voice_map.py editing_commit.py
#         editing_batch.py editing_segments.py editing_bulk_replace.py
#         editing_split_suggest.py editing_speakers.py editing_voice_profile.py
#         runtime_wiring.py user_edit_audit.py 等

# 0-f. 确认现有测试文件（这些测试是迁移前后的基线）
ls tests/ | grep -E "editing|phase1|job_service"
# 应包含：test_editing_endpoints.py test_editing_tts.py test_editing_batch_and_voice_map.py
#         test_editing_segments.py test_editing_commit.py test_phase1_guards.py test_job_service.py 等

# 0-g. 全量 post-edit 测试绿灯基线（必须全 passed 才能继续）
python -m pytest tests/test_editing_endpoints.py tests/test_editing_tts.py tests/test_editing_batch_and_voice_map.py tests/test_editing_segments.py tests/test_editing_commit.py tests/test_phase1_guards.py tests/test_job_service.py -q
# 预期：全 passed，0 failed（若已有预存失败，记录 baseline set-diff 供对照）

# 0-h. 记录目标文件（若存在则停止，不要覆盖）
test -f src/services/jobs/editing_application.py && echo "ALREADY_EXISTS_STOP" || echo "OK_TO_CREATE"
```

**⚠️ 注意**：项目有多个 `.codex_worktrees/` 目录；所有命令在主工作树的 `quality/jobservice-postedit` 分支执行，不要在 worktree 子目录里操作。

---

## Step 1 · 编写 contract 测试（红灯先行）

**动作**：在 `tests/test_editing_application_module.py`（新文件）中编写 `EditingApplicationModule` 的 contract 测试，在模块存在之前就定义它的窄接口约束。此时测试必然失败（`ImportError`），这是预期的"红灯"。

**具体改法**：

```python
# tests/test_editing_application_module.py  ← 新文件
"""Contract tests for EditingApplicationModule.

These tests verify that EditingApplicationModule can be instantiated with
only a JobStore (+ optional audit_observer + optional tts_caller),
without a ProcessJobRunner.  They also verify the paid-API guard:
no tts_generator import leaks into the module.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---- contract: module exists and is importable ----
def test_editing_application_module_importable():
    from services.jobs.editing_application import EditingApplicationModule  # noqa: F401


# ---- contract: instantiable with only JobStore (no ProcessJobRunner) ----
def test_editing_application_module_instantiable_with_fake_store(tmp_path):
    from services.jobs.editing_application import EditingApplicationModule
    from services.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    module = EditingApplicationModule(store=store)
    assert module is not None


# ---- contract: enter_editing / cancel_editing are callable on module ----
def test_editing_application_module_has_enter_editing(tmp_path):
    from services.jobs.editing_application import EditingApplicationModule
    from services.jobs.store import JobStore

    store = JobStore(tmp_path / "jobs")
    module = EditingApplicationModule(store=store)
    assert callable(getattr(module, "enter_editing", None))


# ---- paid-API guard: editing_application.py must not reference tts_generator ----
def test_editing_application_module_no_tts_generator_import():
    src = Path("src/services/jobs/editing_application.py").read_text(encoding="utf-8")
    assert "tts_generator" not in src, (
        "editing_application.py must not reference tts_generator — "
        "re-TTS is user-initiated only (see tests/test_phase1_guards.py)"
    )


# ---- contract: JobService still exposes enter_editing (delegation intact) ----
def test_job_service_still_exposes_enter_editing(tmp_path):
    from services.jobs.service import JobService
    from services.jobs.store import JobStore
    from services.jobs.process_runner import ProcessJobRunner

    store = JobStore(tmp_path / "jobs")
    runner = ProcessJobRunner.__new__(ProcessJobRunner)  # skip __init__
    svc = JobService.__new__(JobService)
    svc.store = store
    svc.runner = runner
    svc._audit_observer = None
    assert callable(getattr(svc, "enter_editing", None))
```

**该步验收**：

```bash
# 1-a. 运行新测试 — 必须看到 ImportError（红灯），不是其他错误
python -m pytest tests/test_editing_application_module.py -v 2>&1 | grep -E "FAILED|ERROR|ImportError|ModuleNotFoundError"
# 预期：test_editing_application_module_importable FAILED 含 ImportError 或 ModuleNotFoundError
#       其余 test 也 FAILED（因为第一个 import 就挂），共 5 FAILED

# 1-b. 确认 test_phase1_guards 仍全绿（contract 测试文件本身不能破坏守卫）
python -m pytest tests/test_phase1_guards.py -q
# 预期：全 passed，0 failed
```

✅ 已决策（CodeX 2026-06-25）：若现有 `tests/` 中已有 `test_editing_application_module.py`（排查 0-h 步骤），**只扩展追加，不覆盖重写**；直接在文件末尾 append 新用例，保留原有内容。

---

## Step 2 · 创建 EditingApplicationModule 骨架（最小可通过 contract 测试）

**动作**：新建 `src/services/jobs/editing_application.py`，只包含类定义和 `__init__` 签名，不迁移任何业务逻辑。此步让 Step 1 的 contract 测试从红灯变绿灯（仅 contract 层通过），而 `service.py` 的行为完全不变。

**文件**：`src/services/jobs/editing_application.py`（新建）

**具体改法**：

```python
"""EditingApplicationModule — post-edit workflow 的窄接口应用模块。

职责：管理任务从 succeeded → editing → succeeded/running 的生命周期，
包括段落 CRUD、TTS 再生、voice map 覆盖、批量 TTS、split/suggest、
source audio 预览、审计事件。

设计约束：
- 只依赖 JobStore + 可 fake 的 audit_observer + 可 fake 的 tts_caller。
- 不依赖 ProcessJobRunner。
- 不直接调用 tts_generator（付费 API 红线）。
- 不被 gateway/ 直接 import。

迁移进度（TU-13）：骨架阶段，业务方法待在 Step 3–5 迁入。
"""
from __future__ import annotations

from typing import Any


class EditingApplicationModule:
    """Post-edit 工作流应用模块，可用 fake JobStore 单测。"""

    def __init__(
        self,
        *,
        store: Any,
        audit_observer: object | None = None,
        segment_tts_caller: object | None = None,
    ) -> None:
        self.store = store
        self._audit_observer = audit_observer
        self._segment_tts_caller = segment_tts_caller
```

**该步验收**：

```bash
# 2-a. contract 测试全绿（从红转绿）
python -m pytest tests/test_editing_application_module.py -v
# 预期：5 passed，0 failed

# 2-b. phase1 guards 仍全绿
python -m pytest tests/test_phase1_guards.py -q
# 预期：全 passed，0 failed

# 2-c. service.py 行数不变（骨架阶段未迁移任何方法）
wc -l src/services/jobs/service.py
# 预期：与 Step 0-b 实测值相同（约 1902）

# 2-d. 新文件无 tts_generator 引用（paid-API guard）
grep -c "tts_generator" src/services/jobs/editing_application.py
# 预期：0

# 2-e. 现有全量 post-edit 测试仍全绿（无回归）
python -m pytest tests/test_editing_endpoints.py tests/test_editing_tts.py tests/test_editing_batch_and_voice_map.py tests/test_editing_segments.py tests/test_editing_commit.py tests/test_job_service.py -q
# 预期：全 passed，0 failed（仅允许与 Step 0-g 相同的预存失败集合）

# 2-f. 独立 commit（显式 pathspec）
git add src/services/jobs/editing_application.py tests/test_editing_application_module.py
git commit -- src/services/jobs/editing_application.py tests/test_editing_application_module.py \
  -m "feat: add EditingApplicationModule skeleton (TU-13 Step 2)"
```

---

## Step 3 · 迁移 editing 生命周期方法（Batch A）

**动作**：将 `service.py` 中的 editing 生命周期相关方法迁入 `EditingApplicationModule`；在 `service.py` 保留同名**转发薄层**（一行 `return self._editing.method(...)` 形式）。每次只迁一组，迁移前后跑相同测试。

**Batch A 方法清单（service.py 中的行号以 Step 0-c 实测值为准）**：

| 方法 | 预期行号 | 分类 |
|------|---------|------|
| `_emit_user_edit_event` | :115 | 私有辅助（审计） |
| `_editor_tts_segments_was_empty` | :470 | 私有辅助 |
| `_emit_editing_session_started` | :494 | 私有辅助（审计） |
| `_summarize_editing_baseline` | :530 | 私有静态辅助 |
| `enter_editing` | :444 | 生命周期 |
| `cancel_editing` | :564 | 生命周期 |
| `_emit_post_edit_cancelled` | :602 | 私有辅助（审计） |
| `commit_editing` | :631 | 生命周期 |
| `revert_unsynced_text_segments` | :673 | 生命周期辅助 |
| `_emit_post_edit_committed` | :697 | 私有辅助（审计） |
| `_require_editing` | :790 | 私有校验辅助 |

**具体改法**：

1. 将上述方法的完整实现从 `service.py` 剪切，粘贴到 `editing_application.py` 的 `EditingApplicationModule` 类体中，行为**零改动**。
2. 迁移时把所有 `self.store` / `self._audit_observer` / `self._emit_user_edit_event` 引用保持不变（它们在 `EditingApplicationModule` 上同名存在）。
3. 在 `service.py` 中将这些方法替换为转发薄层，形式如下（以 `enter_editing` 为例）：
   ```python
   def enter_editing(self, job_id: str) -> JobRecord:
       return self._editing.enter_editing(job_id)
   ```
4. 在 `JobService.__init__` 中添加一行初始化（`:85–:103` 区域）：
   ```python
   # service.py __init__ 末尾（:103 附近）新增
   from services.jobs.editing_application import EditingApplicationModule
   self._editing = EditingApplicationModule(
       store=self.store,
       audit_observer=self._audit_observer,
   )
   ```

**⚠️ 注意**：`_emit_user_edit_event` 在 `service.py` 中还被 `review_actions.py` 通过 `from services.jobs.service import JobConflictError, JobService` 间接使用；迁移后确认 `review_actions.py` 的调用路径是否通过 `service.py` 薄层还是直接调用（见 `src/services/jobs/review_actions.py`），若有直接调用须同步修改。

**该步验收**：

```bash
# 3-a. 迁移后跑相同基线测试（与 Step 0-g 对比 set-diff）
python -m pytest tests/test_editing_endpoints.py tests/test_editing_tts.py tests/test_editing_batch_and_voice_map.py tests/test_editing_segments.py tests/test_editing_commit.py tests/test_job_service.py tests/test_editing_application_module.py tests/test_phase1_guards.py -q
# 预期：全 passed，0 新增失败（允许与 Step 0-g 相同的预存失败集合）

# 3-b. service.py 行数已下降（Batch A 约 250 行业务逻辑迁出，转发薄层约 20 行）
wc -l src/services/jobs/service.py
# 预期：比 Step 0-b 实测值减少约 230 行（实际值以本命令输出为准）

# 3-c. editing_application.py 无 tts_generator 引用
grep -c "tts_generator" src/services/jobs/editing_application.py
# 预期：0

# 3-d. 独立 commit（显式 pathspec）
git commit -- src/services/jobs/service.py src/services/jobs/editing_application.py \
  -m "refactor: migrate editing lifecycle methods to EditingApplicationModule (TU-13 Batch A)"
```

---

## Step 4 · 迁移段落 CRUD + split + source audio + TTS 方法（Batch B）

**动作**：将 `service.py` 中段落操作、split/suggest、source audio 预览、TTS 再生相关方法迁入 `EditingApplicationModule`；同样在 `service.py` 保留转发薄层。

**Batch B 方法清单（行号以 Step 0-c 实测值为准）**：

| 方法 | 预期行号 | 分类 |
|------|---------|------|
| `get_editing_segments` | :803 | 段落读取 |
| `patch_editing_segment` | :817 | 段落 CRUD |
| `preview_bulk_replace_terms` | :863 | 批量替换预览 |
| `apply_bulk_replace_terms` | :886 | 批量替换应用 |
| `_emit_post_edit_segment_patch_audit` | :944 | 私有辅助（审计） |
| `split_editing_segment` | :1025 | split |
| `suggest_split_for_segment` | :1086 | split 建议（付费 LLM，user-initiated） |
| `get_suggest_split_quota` | :1119 | split 配额读取 |
| `split_editing_segment_many` | :1127 | split（多切点） |
| `_emit_post_edit_split_audit` | :1169 | 私有辅助（审计） |
| `preview_editing_segment_source_audio` | :1217 | source audio 预览 |
| `prepare_preview_source_cache` | :1242 | source audio 缓存 |
| `mark_editing_segment_status` | :1267 | 状态标记 |
| `regenerate_segment_tts` | :1286 | TTS 再生（user-initiated） |
| `_emit_post_edit_tts_regenerated` | :1330 | 私有辅助（审计） |
| `accept_segment_draft_tts` | :1371 | draft 接受 |
| `discard_segment_draft_tts` | :1392 | draft 丢弃 |
| `_emit_post_edit_draft_tts_event` | :1413 | 私有辅助（审计） |

**具体改法**：

与 Batch A 步骤相同：剪切粘贴实现到 `EditingApplicationModule`，`service.py` 保留一行转发薄层。重点注意：

- `regenerate_segment_tts`（`:1286–:1369`）使用 `getattr(self, "_segment_tts_caller", None)` 的 DI 模式，迁移后确认 `EditingApplicationModule.__init__` 接受 `segment_tts_caller` 参数（Step 2 已预留），并在薄层中透传：
  ```python
  # service.py 中的薄层需在迁移后注入 tts_caller
  def regenerate_segment_tts(self, job_id, segment_id, *, tts_caller=None, **kwargs):
      return self._editing.regenerate_segment_tts(
          job_id, segment_id, tts_caller=tts_caller, **kwargs
      )
  ```
- `suggest_split_for_segment`（`:1086`）调用 LLM（S2 Pass 1 模型复用），属 user-initiated 路径，不违反付费 API 红线——但迁移后 `EditingApplicationModule` 内部不得新增对该方法的自动调用（只允许被外部显式调用）。

**该步验收**：

```bash
# 4-a. 迁移后跑全量 post-edit 基线测试
python -m pytest tests/test_editing_endpoints.py tests/test_editing_tts.py tests/test_editing_batch_and_voice_map.py tests/test_editing_segments.py tests/test_editing_commit.py tests/test_job_service.py tests/test_editing_application_module.py tests/test_phase1_guards.py -q
# 预期：全 passed，0 新增失败

# 4-b. service.py 行数继续下降（Batch B 约 550 行业务逻辑迁出）
wc -l src/services/jobs/service.py
# 预期：比 Step 3-b 再减少约 530 行（约 600–700 行区间；实际值以本命令为准）

# 4-c. editing_application.py 无 tts_generator 引用
grep -c "tts_generator" src/services/jobs/editing_application.py
# 预期：0

# 4-d. phase1 guards 全绿（关键：Batch B 含 TTS 相关方法，最易触碰守卫）
python -m pytest tests/test_phase1_guards.py -v
# 预期：全 passed，0 failed

# 4-e. 独立 commit
git commit -- src/services/jobs/service.py src/services/jobs/editing_application.py \
  -m "refactor: migrate segment CRUD + split + TTS regen methods to EditingApplicationModule (TU-13 Batch B)"
```

✅ 已决策（CodeX 2026-06-25）：`suggest_split_for_segment` 迁入 `EditingApplicationModule` 后，必须严格保持 user-initiated-only 接口——模块内部绝不新增对该方法的自动、批量或后台调用路径（付费 API 红线）。迁移完成后执行前置动作（已定方向）：确认 `tests/test_editing_split_suggest*.py`（若存在）仍覆盖该路径，且无测试通过后台模拟路径绕过 user-initiated 保护。

---

## Step 5 · 迁移批量 TTS + voice map 方法（Batch C），更新 runtime_wiring，补充 contract 测试

**动作**：迁移剩余方法（批量 TTS + voice map），更新 `runtime_wiring.py` 中的 `_segment_tts_caller` 注入目标，在 `service.py` 中删除已被完全替换为薄层的私有辅助方法（若无薄层需求），并补充 `test_editing_application_module.py` 中的独立单元测试覆盖。

**Batch C 方法清单（行号以 Step 0-c 实测值为准）**：

| 方法 | 预期行号 | 分类 |
|------|---------|------|
| `regenerate_all_dirty_segments` | :1456 | 批量 TTS（user-initiated） |
| `regenerate_selected_dirty_segments_async` | :1484 | 批量 TTS 异步（线程管理先留薄层，见决策记录） |
| `regenerate_all_dirty_segments_async` | :1529 | 批量 TTS 异步（线程管理先留薄层，见决策记录） |
| `get_regenerate_all_status` | :1557 | 批量 TTS 状态查询 |
| `request_regenerate_all_cancel` | :1576 | 批量 TTS 取消 |
| `get_editing_voice_map` | :1597 | voice map 读取 |
| `set_editing_voice_override` | :1603 | voice map 写入 |
| `clear_editing_voice_override` | :1696 | voice map 清除 |
| `_emit_post_edit_voice_override_audit` | :1736 | 私有辅助（审计） |

**具体改法**：

1. 剪切粘贴 Batch C 方法到 `EditingApplicationModule`，`service.py` 保留转发薄层（同前）。**例外**：`regenerate_selected_dirty_segments_async` 和 `regenerate_all_dirty_segments_async` 若依赖 `self.runner`（`ProcessJobRunner`）或外部 status_store，则线程启动与取消令牌逻辑留在 `service.py`，只将纯 store 读写委托给 `EditingApplicationModule`（已决策，见决策记录）。

2. 更新 `src/services/jobs/runtime_wiring.py`（`:34`，`apply_runtime_wiring` 函数）：注入 `_segment_tts_caller` 时，除写 `service._segment_tts_caller` 外，**同步写入** `service._editing._segment_tts_caller`，确保 `EditingApplicationModule` 的 DI 绑定一致：
   ```python
   # runtime_wiring.py apply_runtime_wiring 内（在原有赋值行后追加）
   # service._segment_tts_caller = caller   ← 原有行保留（薄层读取路径兼容）
   if hasattr(service, "_editing"):
       service._editing._segment_tts_caller = caller
   ```

3. 在 `tests/test_editing_application_module.py` 中补充独立单元测试（无需 `ProcessJobRunner`）：
   - `test_get_editing_voice_map_returns_empty_for_nonexistent_project`：fake `JobStore` 返回已进入 editing 的 `JobRecord`，调用 `get_editing_voice_map` 后验证返回 `dict`。
   - `test_regenerate_all_dirty_segments_requires_tts_caller_raises_when_not_wired`：注入 `segment_tts_caller=None` 的 `EditingApplicationModule`，调用 `regenerate_all_dirty_segments` 验证抛出 `TtsNotWiredError`（或等价异常），不做真实 TTS 调用。

**该步验收**：

```bash
# 5-a. service.py 最终行数（目标：700 行以下）
wc -l src/services/jobs/service.py
# 预期：< 700（迁移了 ~1,320 行业务逻辑，薄层约 60 行，净减少 ~1,260 行，
#        加上保留的 submit_job/get_job 等约 580 行，合计 ≈ 640 行）

# 5-b. editing_application.py 无 tts_generator 引用（终态校验）
grep -c "tts_generator" src/services/jobs/editing_application.py
# 预期：0

# 5-c. 全量 post-edit 基线测试 + contract 测试全绿
python -m pytest tests/test_editing_endpoints.py tests/test_editing_tts.py tests/test_editing_batch_and_voice_map.py tests/test_editing_segments.py tests/test_editing_commit.py tests/test_job_service.py tests/test_editing_application_module.py tests/test_idle_scanner_integration.py tests/test_phase1_guards.py -q
# 预期：全 passed，0 新增失败

# 5-d. 补充的独立单元测试可在不启动 ProcessJobRunner 的情况下通过
python -m pytest tests/test_editing_application_module.py -v
# 预期：全 passed；无 ProcessJobRunner、无真实 TTS、无真实网络调用

# 5-e. runtime_wiring 守卫（phase1 guards 中已覆盖 apply_runtime_wiring 调用；此处额外确认）
python -m pytest tests/test_phase1_guards.py -v
# 预期：全 passed，0 failed

# 5-f. 确认 gateway/ 未新增对 editing_application 的 import
grep -r "editing_application\|EditingApplicationModule" gateway/ --include="*.py"
# 预期：0 条匹配（gateway 侧不得直接 import）

# 5-g. 独立 commit
git commit -- src/services/jobs/service.py src/services/jobs/editing_application.py \
              src/services/jobs/runtime_wiring.py tests/test_editing_application_module.py \
  -m "refactor: migrate batch TTS + voice map to EditingApplicationModule; update runtime_wiring (TU-13 Batch C)"
```

✅ 已决策（CodeX 2026-06-25）：`regenerate_selected_dirty_segments_async`（`:1484`）和 `regenerate_all_dirty_segments_async`（`:1529`）的线程启动与取消逻辑**先留在 `service.py` 的薄层中**，不强制迁入 `EditingApplicationModule`。仅当后续满足以下两个条件时才可完整迁入：① `EditingApplicationModule` 显式接收 `runner` / `status_store` Adapter 参数；② 有取消令牌（cancel token）的专项测试覆盖。执行时前置动作（已定方向）：在 Batch C 迁移时，逐一核查 `regenerate_*_async` 是否依赖 `self.runner`——若依赖则保留线程管理部分在 `service.py`，只将纯 store 操作部分委托给 `EditingApplicationModule`。

---

## 测试计划（新增 / 回归）

### 新增测试（`tests/test_editing_application_module.py`）

| 测试 | 验证点 |
|------|--------|
| `test_editing_application_module_importable` | 模块可导入（contract） |
| `test_editing_application_module_instantiable_with_fake_store` | 只需 `JobStore`，不需要 `ProcessJobRunner` |
| `test_editing_application_module_has_enter_editing` | 公开接口存在 |
| `test_editing_application_module_no_tts_generator_import` | 付费 API 红线（AST 文本扫描） |
| `test_job_service_still_exposes_enter_editing` | `JobService` 薄层保持公开接口 |
| `test_get_editing_voice_map_returns_empty_for_nonexistent_project` | voice map 独立单测（Step 5） |
| `test_regenerate_all_dirty_segments_requires_tts_caller_raises_when_not_wired` | TTS DI 正确：未注入时抛 `TtsNotWiredError` |

### 回归测试（必须全绿，集合与 Step 0-g 基线相同）

- `tests/test_editing_endpoints.py` — 通过 `JobService` 的 HTTP 层路径
- `tests/test_editing_tts.py` — TTS 再生、draft accept/discard
- `tests/test_editing_batch_and_voice_map.py` — 批量 TTS + voice map
- `tests/test_editing_segments.py` — 段落 CRUD
- `tests/test_editing_commit.py` — commit / overwrite / copy_as_new
- `tests/test_job_service.py` — `JobService` 核心（submit / continue / get）
- `tests/test_phase1_guards.py` — AST 守卫（commit 管线不调 tts_generator）
- `tests/test_idle_scanner_integration.py` — editing idle cancel 集成

### 验收命令（可直接粘贴到 CI）

```bash
python -m pytest \
  tests/test_editing_endpoints.py \
  tests/test_editing_tts.py \
  tests/test_editing_batch_and_voice_map.py \
  tests/test_editing_segments.py \
  tests/test_editing_commit.py \
  tests/test_job_service.py \
  tests/test_editing_application_module.py \
  tests/test_idle_scanner_integration.py \
  tests/test_phase1_guards.py \
  -q --tb=short
# 最终预期：0 new failures；service.py 行数 < 700（grep 验收）：
wc -l src/services/jobs/service.py
```

---

## 回滚方案

本单元每步独立 commit，可精确回滚到任意检查点：

| Commit | 可回滚到 | 影响文件 |
|--------|---------|---------|
| Step 2 commit | 骨架创建前 | `src/services/jobs/editing_application.py`（删除）、`tests/test_editing_application_module.py`（删除） |
| Step 3 commit | Batch A 迁移前 | `src/services/jobs/service.py`（恢复）、`src/services/jobs/editing_application.py`（回退） |
| Step 4 commit | Batch B 迁移前 | 同上 |
| Step 5 commit | Batch C 迁移 + runtime_wiring 前 | `src/services/jobs/service.py`（恢复）、`src/services/jobs/editing_application.py`（回退）、`src/services/jobs/runtime_wiring.py`（恢复） |

**完整回滚**（回到分支起点）：

```bash
# 查看本分支的 commit 列表
git log quality/jobservice-postedit --oneline

# 优先（任何情况均适用）：逐个 revert（安全，适用于已 push / 共享分支）
git revert <各 commit>   # 按逆序 revert

# reset --hard：仅限"本地尚未 push 的 feature 分支"，且须项目主明确确认后执行
# ✅ 已决策（CodeX 2026-06-25）：reset --hard 使用条件已锁定，非上述条件禁止使用
git reset --hard <质量分支 base commit hash>

# branch -D：须项目主明确确认后执行
# ✅ 已决策（CodeX 2026-06-25）：branch -D 须项目主确认，条件已锁定
git checkout main
git branch -D quality/jobservice-postedit
```

**关键文件边界**：

- `src/services/jobs/service.py` — 每步 commit 前通过 `git diff HEAD -- src/services/jobs/service.py` 确认只有薄层替换，无逻辑变更。
- `src/services/jobs/editing_application.py` — 新文件，回滚直接删除。
- `src/services/jobs/runtime_wiring.py` — 仅在 Step 5 有 2 行增量（`if hasattr(service, "_editing"): ...`），可独立 revert。
- 测试文件 — 只新增，不修改现有测试；回滚删除 `tests/test_editing_application_module.py` 即可。

---

## 完成定义（DoD）

- [ ] `src/services/jobs/editing_application.py` 已创建，`EditingApplicationModule` 包含全部 post-edit 方法实现（Batch A + B + C）。
- [ ] `src/services/jobs/service.py` 行数 **< 700 行**（以 `wc -l src/services/jobs/service.py` 输出为准）。
- [ ] `service.py` 中每个 post-edit 公开方法保留一行转发薄层，签名与原方法完全一致。
- [ ] `editing_application.py` 内不含 `tts_generator` 字符串（`grep -c "tts_generator" src/services/jobs/editing_application.py` 输出为 `0`）。
- [ ] `tests/test_editing_application_module.py` 中全部 7 个 contract / 单元测试通过，且无 `ProcessJobRunner` 实例化。
- [ ] 所有回归测试通过，新增失败数为 0（以与 Step 0-g 基线的 set-diff 验证）：`tests/test_editing_endpoints.py`、`tests/test_editing_tts.py`、`tests/test_editing_batch_and_voice_map.py`、`tests/test_editing_segments.py`、`tests/test_editing_commit.py`、`tests/test_job_service.py`、`tests/test_idle_scanner_integration.py`。
- [ ] `tests/test_phase1_guards.py` 全绿（AST 守卫：commit 管线不调 `tts_generator`）。
- [ ] `gateway/` 目录下无对 `EditingApplicationModule` 的直接 import（`grep -r "editing_application" gateway/` 输出为空）。
- [ ] `src/services/jobs/runtime_wiring.py` 更新，`apply_runtime_wiring` 同步注入 `service._editing._segment_tts_caller`。
- [ ] `editing_application.py` 内不存在对 `suggest_split_for_segment` 的自动/批量/后台调用路径（只暴露 user-initiated 接口，付费 API 红线）；`grep -n "suggest_split_for_segment" src/services/jobs/editing_application.py` 仅含方法定义行，无内部自调用。
- [ ] `regenerate_selected_dirty_segments_async` / `regenerate_all_dirty_segments_async` 的线程启动与取消令牌逻辑已确认归属（留在 `service.py` 薄层或已满足"显式接收 runner/status_store Adapter + 取消测试覆盖"条件后迁入），不引入无守卫的后台 TTS 路径。
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未使用 `git add .`。

# TU-12 · jobs/api.py dispatch table 化

- **目标 / 价值**：`src/services/jobs/api.py` 当前 **2,645 行**（是 800 行上限的 3.3 倍），核心问题是 `_build_job_api_handler`（`:210`）内嵌了一个 ~2,200 行的 `JobAPIHandler` 巨型内部类，其中 `do_GET`（`:212`–`:974`）约 763 行、`do_POST`（`:976`–`:2208`）约 1,233 行、`do_DELETE`（`:2209`）约 15 行、`do_PATCH`（`:2224`）约 37 行，每个方法是一条超长 `if/elif` 链。路由逻辑、业务逻辑、权限 gate 全部混在同一块 `if len(path_parts) == N and path_parts[2] == "xxx"` 结构里，新增路由只能在末尾追加分支，审查时需上下翻 1,000+ 行才能找到相关代码。本单元通过引入 **dispatch table**（`dict[tuple, Callable]`）并将各路由的处理逻辑提取为**独立顶层 handler 函数**，把 `do_GET` / `do_POST` 各自压缩到 ~50 行的 dispatch 分发壳，辅助函数保留在 `api.py` 内（不跨文件拆包，避免循环导入）。不替换 HTTP 框架（stdlib `BaseHTTPRequestHandler`），不迁 FastAPI——那是 TU-18 的决策门。
- **关联发现**：STRUCT-05（`jobs/api.py` 2,645 行；`_build_job_api_handler` 内嵌 ~2,200 行路由 dispatch 内部类）
- **前置依赖**：建议在 TU-09（`job_intercept.py` 拆分）之后执行，避免 gateway/jobs api 两侧同时大改造成合并冲突；TU-03（pytest 配置 / file-size guard）建议先落，以便量化收尾指标。本单元本身不依赖 TU-03 / TU-09 完成，可独立执行。
- **建议分支**：`quality/jobsapi-dispatch`
- **预估工时**：M（分 4 批提取，每批约 S；含 contract 测试编写时间）

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **D1 · dispatch 层级**：采用一级 dispatch table + 族 handler 内少量 if/elif。二级全量 table 化留给后续（工时 L，当前不做）。
- **D2 · X-Internal-Key 统一前置 guard**：在 dispatch 壳中对所有 `path_parts[0]=="internal"` 的 key 做统一鉴权前置（而非嵌入各 internal handler）是可接受的，**但必须有 contract 测试证明：未带 key 仍返回 403、带正确 key 的路径状态码不变**（两个用例已在 Step 1 列出）。
- **D3 · 接受低净减行**：净减约 200 行是合理预期，不以大幅减行为目标；收益指标改为 **Locality / 路由可定位性**（`_get_table_GET` 一屏看全所有 GET 路由）。
- **D4 · 不替换 HTTP 框架**：stdlib `BaseHTTPRequestHandler` 保持不动；迁 FastAPI 是 TU-18 的决策门，本单元范围之外。
- **D5 · DoD 行数阈值保留但非核心指标**：`<= 2,450` 行的 DoD 约束保留作安全网（防退化），但主指标为 `do_GET`/`do_POST` 壳 ≤ 40 行（Locality 指标）。
- **D6 · 族路由归一 handler**：`editing`、`segments`、`review` 等同一 `path_parts[2]` 有多个子分支的路由，保持为一个族 handler 函数内部 if/elif，不再拆细。
- **D7 · contract 测试覆盖鉴权行为**：Step 1 新增的 `test_internal_missing_key_returns_403` 与 `test_internal_correct_key_passes_gate` 是本单元的安全契约门，必须在迁移前通过、迁移后仍通过。

---

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令：`grep` → `Select-String`、`wc -l` → `(Get-Content file | Measure-Object -Line).Lines`、`tail -n` → `Select-Object -Last N`、`test -f` → `Test-Path`、避免 `<(...)` 进程替换。

---

## 不在本单元范围（out-of-scope）

- 替换 HTTP 框架（stdlib `BaseHTTPRequestHandler` → FastAPI）——TU-18 决策门
- 修改任何业务逻辑（review gate / 付费 API gate / policy 判断逻辑）
- 跨文件拆包（不新建 `jobs/api_handlers/` 子包，本单元只在 `api.py` 内重组）
- `JobService` 内部 post-edit 模块化（STRUCT-07，独立任务）
- `_require_project_dir` / `_require_language_pair_capability` 等工具函数的类型改造（TS-*，独立任务）
- 任何性能优化（chunked write / range streaming 逻辑不变）
- 前端路由变更
- `control_panel.py` 的 `do_GET` / `do_POST`（独立文件，STRUCT-08）

---

## 必守不变量

以下不变量在本单元每次 commit / PR 中必须保持：

1. **HTTP 语义不变**：每个路由的 HTTP 方法、URL 路径匹配规则、状态码、响应 JSON shape 与迁移前一致。dispatch table 的键（method + path_parts pattern）不得重新诠释原有路由逻辑。
2. **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / LLM / ASR 绝不在 fallback / except / retry / batch 路径自动触发。提取后的 handler 函数内不得新增任何付费 API 调用点；原有内联 `except Exception` 兜底只调 `self._send_sanitized_error(exc)`，保持不变。
3. **policy gate 逻辑不拆不移**：`_policy_mode_for(record)` 判断和 `EXPRESS_ALLOWED_*` 过滤逻辑所在的每一个 `if` 分支，必须整块（含 gate + 业务 + return）迁入对应 handler 函数，不得拆成 gate 留原处、业务迁新处的拆分。
4. **内部端点鉴权 gate 不漏**：`/internal/*` 的 `X-Internal-Key` 校验（`:1769`）必须在 dispatch 前执行，迁移后保持"先校验 key、再 dispatch 到具体 handler"的顺序不变。
5. **`_write_json` / `_send_sanitized_error` / `_read_json_payload` 等 handler 工具方法继续属于 `JobAPIHandler` 类**：dispatch table 内的 handler 函数须能通过闭包或参数获得 `self`（handler 实例），不要把这些方法提升为顶层函数。
6. **`build_job_api_server` 签名不变**：唯一公开入口的函数签名 `build_job_api_server(*, service, host, port, jianying_runner)` 保持不变；`_validate_internal_api_key`（`:142`）的调用位置不变。
7. **Alignment DSP-first**：`do_POST` 路由中触发 `editing/commit`（`:1620`）的 alignment 路径不改变，dispatch table 化后整块逻辑无修改地迁移。
8. **默认测试不接真实外部服务**：新增 contract 测试只用 `FakePopenFactory` / `FakeJobStore` 等已有 fake，不新增真实网络或文件系统 I/O。

---

## Step 0 · 确认现状

```bash
# 0-a. 建分支
git switch -c quality/jobsapi-dispatch

# 0-b. 确认 api.py 行数（spec 值：2,645）
wc -l src/services/jobs/api.py
# 预期：2645

# 0-c. 确认关键方法行号（以下预期基于 main 分支实测，行号如漂移以实际输出为准）
grep -n "def do_GET\|def do_POST\|def do_DELETE\|def do_PATCH\|def _build_job_api_handler\|class JobAPIHandler" src/services/jobs/api.py
# 预期（已核实）：
#   _build_job_api_handler  :210
#   class JobAPIHandler     :211
#   do_GET                  :212
#   do_POST                 :976
#   do_DELETE               :2209
#   do_PATCH                :2224

# 0-d. 统计 do_GET / do_POST 内路由分支数（path_parts 匹配条件）
grep -n "path_parts\[0\] == \|path_parts\[2\] == \|path_parts\[3\] ==" src/services/jobs/api.py | wc -l
# 预期：约 104 条（即路由密度，每条代表一个 path_parts 匹配子条件）

# 0-e. 枚举 do_POST 下 path_parts[2] 的唯一值（路由族名称）
grep -n "path_parts\[2\] ==" src/services/jobs/api.py | awk -F'"' '{print $2}' | sort -u
# 预期（已核实，共 16 个）：
#   editing  enter-edit  generate-jianying-draft  generate-video
#   jianying-draft-status  jobs  materials-availability  regenerate-all-tts
#   regenerate-selected-tts  reports  review  segments
#   smart-quality-report  speaker-audio  stream  suggest-split-quota

# 0-f. 确认 helper 函数位于 JobAPIHandler 外部（_build_job_api_handler 之后）
grep -n "^def _require_project_dir\|^def _build_global_voice_library\|^def _require_language_pair_capability\|^def _gate_pair_post_edit\|^def _gate_pair_suggest_split\|^def _require_waiting_for_review\|^def _require_review_gate" src/services/jobs/api.py
# 预期：
#   _require_language_pair_capability  :2426（在 return JobAPIHandler 的 :2419 之后）
#   _gate_pair_post_edit               :2455
#   _gate_pair_suggest_split           :2462
#   _require_waiting_for_review        :2469
#   _require_waiting_for_review_or_editing :2478
#   _require_review_gate               :2492
#   _require_project_dir               :2511
#   _build_global_voice_library        :2634

# 0-g. 现有 Job API 测试基线（必须全绿才能继续）
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_express_filter.py tests/test_job_api_phase1.py tests/test_job_api_phase2.py -q
# 预期：全 passed，0 failed

# 0-h. 收集关联测试文件名（迁移前后对比用）
python -m pytest tests/ --collect-only -q -k "job_api" 2>&1 | grep "test session starts" -A 5
# 预期：约 105 个 job_api 相关测试
```

**⚠️ 注意**：项目有多个 `.codex_worktrees/` 分支；上述命令必须在主工作树的 `quality/jobsapi-dispatch` 分支上执行，不要在 worktree 目录里操作。

---

## Step 1 · 补 handler 行为 contract 测试（先测后迁的前提）

**目标**：在移动任何代码之前，建立一套 **route contract 测试**，覆盖路由 dispatch 的关键行为约束。这些测试在迁移前和迁移后都必须通过（"迁移前已绿、迁移后仍绿"的不变量性质，而非"因重构才绿"的实现测试）。

**背景**：现有 `tests/test_job_api.py`（222 行）、`tests/test_job_api_express_filter.py`（约 380 行）等文件通过 `build_job_api_server` + 真实 HTTP 调用测试行为，但缺少以下 contract 层面的验证：
- dispatch 覆盖性：每个已注册的路由 pattern 至少有一个 HTTP 测试
- policy gate 顺序：`_policy_mode_for` gate 在 express/anonymous_preview 路由上先于业务逻辑执行
- 内部端点鉴权顺序：`/internal/*` 路由必须在业务逻辑之前校验 `X-Internal-Key`

**动作**：

1. 新建测试文件 `tests/test_job_api_dispatch_contracts.py`，使用与 `tests/test_job_api_express_filter.py` 相同的 `_build_test_server` fixture 模式（`FakePopenFactory` + `JobStore` + `build_job_api_server(port=0)`）。

2. 实现以下 contract 用例（不依赖实现细节，当前就对 `api.py` 内联结构测试）：

   - **`test_unknown_get_route_returns_404`**：对 `GET /jobs/{id}/unknown-subresource-xyz` 返回 404，而非 500。
   - **`test_unknown_post_route_returns_404`**：对 `POST /jobs/{id}/unknown-action-xyz` 返回 404，而非 500。
   - **`test_internal_missing_key_returns_403`**：设置 `AVT_INTERNAL_API_KEY=testkey16chars`，对 `POST /internal/voice-verify/cosyvoice` 不带 `X-Internal-Key` header 返回 403。
   - **`test_internal_correct_key_passes_gate`**：同上，带正确 `X-Internal-Key` header 不返回 403（返回 200 或 400，取决于 payload）。
   - **`test_anonymous_preview_artifacts_returns_empty_list`**：创建带 `anonymous_preview=True` 的 job，`GET /jobs/{id}/artifacts` 的 `artifacts` 字段为 `[]`。
   - **`test_dispatch_route_count_baseline`**：用 `grep -c` 统计 `src/services/jobs/api.py` 中 `path_parts[2] ==` 的行数，断言 `>= 16`（防止迁移时路由静默丢失）。用 `subprocess.run` 在测试内执行 grep，把计数存到 `assert count >= 16`。

3. 测试文件顶部继承已有 `sys.modules["database"]` stub 约定（参考 `tests/test_job_api_express_filter.py` 顶部 8 行）。

**文件**：
- `tests/test_job_api_dispatch_contracts.py`（新建）

**该步验收**：

```bash
# contract 测试在迁移前必须全部通过
python -m pytest tests/test_job_api_dispatch_contracts.py -v
# 预期：全 passed，0 failed，0 error

# 确认测试文件存在
test -f tests/test_job_api_dispatch_contracts.py && echo "OK"
```

**commit 边界**：

```bash
git commit -- tests/test_job_api_dispatch_contracts.py \
  -m "test: add route dispatch contract tests for jobs/api.py (pre-refactor)"
```

---

## Step 2 · 设计 dispatch table 骨架（仅结构，不迁逻辑）

**目标**：在 `_build_job_api_handler` 内部、`JobAPIHandler` 类定义之上，确立 dispatch table 的数据结构和 `do_GET` / `do_POST` 的 dispatch 壳模式，让后续每步只需把 `if` 分支剪切进对应 handler 函数即可，不需要改结构。

**设计决策**：

dispatch key 选用 `tuple[int, str]`，其中 `int` 是 `len(path_parts)`，`str` 是 `path_parts[2]`（最具辨别力的位置）。对于 `len==2`（无 subresource）和 `len==1`（`/jobs` 列表）使用特殊 sentinel 字符串（`""` 表示无 subresource，`"$list"` 表示 `/jobs`）。

示例骨架（在 `JobAPIHandler` 类体内部，method 内引用 `_get_dispatch_table` 方法）：

```python
def _get_table(self) -> dict[tuple, object]:
    # 在 Step 3-6 中逐批填入；此处先返回空 dict 作占位
    return {}
```

`do_GET` 壳（约 25 行）：

```python
def do_GET(self) -> None:  # noqa: N802
    parsed_path = urlparse(self.path)
    path_parts = [p for p in parsed_path.path.strip("/").split("/") if p]
    try:
        key = _route_key(path_parts)
        table = self._get_table_GET()
        if key in table:
            table[key](self, path_parts, parsed_path)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
    except JobNotFoundError as exc:
        self._write_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
    except UnsupportedJobRequestError as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    except JobConflictError as exc:
        self._write_json(HTTPStatus.CONFLICT, {"error": str(exc)})
    except ValueError as exc:
        self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    except Exception as exc:  # pragma: no cover
        self._send_sanitized_error(exc)
```

其中 `_route_key` 是模块顶层工具函数（不在 `JobAPIHandler` 类内），以便 contract 测试可独立测试路由键计算：

```python
def _route_key(path_parts: list[str]) -> tuple[int, str]:
    n = len(path_parts)
    if n == 0:
        return (0, "")
    if n == 1:
        return (1, path_parts[0])        # e.g. (1, "jobs") → GET /jobs
    if n == 2:
        return (2, path_parts[0])        # e.g. (2, "jobs") → GET /jobs/{id}
    return (n, path_parts[2])            # e.g. (3, "logs") → GET /jobs/{id}/logs
```

✅ 已决策（CodeX 2026-06-25）：`path_parts[2]` 作为 dispatch key 对 97% 的路由唯一。对于同一 `path_parts[2]` 有多个子分支的路由——`editing`（下含 `cancel`/`commit`/`voice-map`/`segments`/`speakers` 等）、`segments`（下含多种 action）、`review`——这些"族路由"在 Step 3–6 中保持为**一个族 handler 函数内部 if/elif**，不拆成更细粒度的 dispatch。二级全量 table 化（工时 L）留给后续单元，本单元范围外。

**动作**：

1. 在 `api.py` 的 `_build_job_api_handler` 函数体内、`class JobAPIHandler` 定义之前，插入 `_route_key` 工具函数（模块级，或 `_build_job_api_handler` 内局部——推荐模块级以便独立测试）。
2. 在 `JobAPIHandler` 类内添加空的 `_get_table_GET(self)` / `_get_table_POST(self)` 方法（返回空 `dict`），及新版 `do_GET` / `do_POST` dispatch 壳（暂时在 dispatch table 未命中时 fallback 到原 `if` 链，让迁移可增量进行）。

**文件**：
- `src/services/jobs/api.py`（改：新增 `_route_key` 函数 + `JobAPIHandler._get_table_GET/POST` 骨架 + `do_GET`/`do_POST` dispatch 壳；原 `if` 链暂作 fallback）

**该步验收**：

```bash
# 所有现有测试通过（行为不变）
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_express_filter.py tests/test_job_api_dispatch_contracts.py -q
# 预期：全 passed，0 failed

# _route_key 函数存在（后续 contract 测试可直接 import）
python -c "from services.jobs.api import _route_key; print(_route_key(['jobs','abc','logs']))"
# 预期：(3, 'logs')

# api.py 行数此时可能略增（加了骨架代码）但不超过 2700
wc -l src/services/jobs/api.py
# 预期：<= 2700
```

**commit 边界**：

```bash
git commit -- src/services/jobs/api.py \
  -m "refactor: add _route_key + dispatch table skeleton to JobAPIHandler (no logic moved yet)"
```

---

## Step 3 · 迁移第一批：GET 只读族（`/jobs` 列表 + `/jobs/{id}` 单取 + `/jobs/{id}/logs` + `/jobs/{id}/result-summary` + `/jobs/{id}/artifacts`）

**目标**：把 `do_GET` 内最简单的 5 个纯读路由提取为 handler 函数，并注册进 `_get_table_GET`。提取后从原 `if` 链的 fallback 中删除对应 `if` 块。

**涉及路由**（已核实行号）：
- `path_parts == ["jobs"]`（`:216`）→ `_handle_GET_jobs_list`
- `len==2 and path_parts[0]=="jobs"`（`:232`）→ `_handle_GET_job_single`
- `len==3 and path_parts[2]=="logs"`（`:236`）→ `_handle_GET_job_logs`
- `len==3 and path_parts[2]=="result-summary"`（`:247`）→ `_handle_GET_job_result_summary`
- `len==3 and path_parts[2]=="artifacts"`（`:253`）→ `_handle_GET_job_artifacts`（**含 `_policy_mode_for` gate 和 express 过滤，必须整块迁移**）

**具体做法**：

1. 在 `_build_job_api_handler` 内、`class JobAPIHandler` 定义处，新建 5 个嵌套函数（或 `JobAPIHandler` 的方法）如 `_handle_GET_jobs_list(self, path_parts, parsed_path)`，将原 `if` 分支的**完整代码块**剪切进去（含所有 `return`）。
2. 在 `_get_table_GET` 返回的 dict 中注册：
   ```python
   {
       (1, "jobs"):           self._handle_GET_jobs_list,
       (2, "jobs"):           self._handle_GET_job_single,
       (3, "logs"):           self._handle_GET_job_logs,
       (3, "result-summary"): self._handle_GET_job_result_summary,
       (3, "artifacts"):      self._handle_GET_job_artifacts,
   }
   ```
3. 从 `do_GET` 的 fallback `if` 链中删除这 5 个 `if` 块（此时 dispatch table 已接管）。

**文件**：
- `src/services/jobs/api.py`（改：新增 5 个 handler 方法，更新 `_get_table_GET`，删除 fallback 中对应 `if` 块）

**该步验收**：

```bash
# 所有现有测试必须全绿（行为不变）
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_express_filter.py tests/test_job_api_dispatch_contracts.py -q
# 预期：全 passed，0 failed

# dispatch table 中已注册 5 个键（可通过 grep 间接确认）
grep -c "_handle_GET_" src/services/jobs/api.py
# 预期：>= 5（每个 handler 函数定义 + 注册各一行）

# artifacts route 包含 _policy_mode_for 调用（安全：gate 逻辑没丢失）
grep -n "_policy_mode_for" src/services/jobs/api.py
# 预期：有至少 1 行在 _handle_GET_job_artifacts 函数内
```

**commit 边界**：

```bash
git commit -- src/services/jobs/api.py \
  -m "refactor: extract GET read-only routes to dispatch table handlers (jobs list/single/logs/artifacts)"
```

---

## Step 4 · 迁移第二批：GET 媒体/资源族（stream、download、tts-segments-zip、speaker-audio、reports、smart-quality-report 等）

**目标**：把 `do_GET` 剩余的媒体流和资源读取路由（约 11 个路由分支，`:337`–`:964`）提取为对应 handler 函数并注册进 dispatch table，清空 fallback `if` 链。

**涉及路由**（以实际行号为准，spec 值已核实）：
- `len==5 and path_parts[2]=="segments" and path_parts[4]=="draft-audio"`（`:337`）→ `_handle_GET_segment_draft_audio`
- `len==4 and path_parts[2]=="editing" and path_parts[3]=="segments"`（`:290`）→ `_handle_GET_editing_segments`
- `len==4 and path_parts[2]=="editing" and path_parts[3]=="voice-map"`（`:296`）→ `_handle_GET_editing_voice_map`
- `len==4 and path_parts[2]=="editing" and path_parts[3]=="speakers"`（`:307`）→ `_handle_GET_editing_speakers`
- `len==4 and path_parts[2]=="regenerate-all-tts" and path_parts[3]=="status"`（`:392`）→ `_handle_GET_regen_all_tts_status`
- `len==3 and path_parts[2]=="smart-quality-report"`（`:424`区段）→ `_handle_GET_smart_quality_report`
- `len==3 and path_parts[2]=="reports"` + `len==4 and path_parts[2]=="reports"`（`:503`/`:519`）→ `_handle_GET_reports`（含两种 len 的子判断）
- `len==3 and path_parts[2]=="review-state"`（`:566`）→ `_handle_GET_review_state`
- `len==4 and path_parts[2]=="download"`（`:584`）→ `_handle_GET_download`
- `len==3 and path_parts[2]=="tts-segments-zip"`（`:628`）→ `_handle_GET_tts_segments_zip`
- `len==3 and path_parts[2]=="suggest-split-quota"`（`:672`区段）→ `_handle_GET_suggest_split_quota`
- `len==5 and path_parts[2]=="segments" and path_parts[4]=="preview-source-audio"`（`:690`区段）→ `_handle_GET_segment_preview_source_audio`
- `len==3 and path_parts[2]=="stream"`（`:712`区段）→ `_handle_GET_stream`
- `len==3 and path_parts[2]=="materials-availability"`（`:770`区段）→ `_handle_GET_materials_availability`
- `len==3 and path_parts[2]=="generate-video"`（`:807`区段）→ `_handle_GET_generate_video_status`
- `path_parts == ["voice-library"]`（`:823`）→ `_handle_GET_voice_library`
- `len==4 and path_parts[2]=="speaker-audio"`（`:830`）→ `_handle_GET_speaker_audio_list`（含 `_policy_mode_for` gate）
- `len==5 and path_parts[2]=="speaker-audio"`（`:856`）→ `_handle_GET_speaker_audio_segment`（含 `_policy_mode_for` gate）
- `len==3 and path_parts[2]=="jianying-draft-status"`（`:893`）→ `_handle_GET_jianying_draft_status`

**⚠️ 注意**：`_route_key` 对 `len==3` 和 `len==4` + `len==5` 的不同 `path_parts[2]` 值已可唯一映射大多数路由。对于同一 `(n, subresource)` 键下有多个子分支的路由（例如 `path_parts[3]` 或 `path_parts[4]` 再区分），handler 函数内部保留 `if/elif` 子分支，不再拆 dispatch 层级。

**具体做法**：参考 Step 3，逐个将 `if` 块剪切为 method，注册进 `_get_table_GET`，删除 fallback 中对应 `if` 块。每次提取一个路由族（stream/speaker-audio/reports 等），确保行为不变后再提取下一个。

**文件**：
- `src/services/jobs/api.py`（改：新增约 15–19 个 handler 方法，完成 `_get_table_GET`，清空 fallback `if` 链）

**该步验收**：

```bash
# 全量 job_api 测试绿
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_express_filter.py tests/test_job_api_phase1.py tests/test_job_api_phase2.py tests/test_job_api_dispatch_contracts.py -q
# 预期：全 passed，0 failed

# do_GET 内 fallback if 链清空（行数极少）
awk '/def do_GET/,/def do_POST/' src/services/jobs/api.py | grep -c "if path_parts"
# 预期：0（所有 if path_parts 分支已迁走）

# _policy_mode_for 仍存在于 api.py（gate 逻辑未丢失）
grep -c "_policy_mode_for" src/services/jobs/api.py
# 预期：>= 2（artifacts 和 speaker-audio 至少各 1 次调用）
```

**commit 边界**：

```bash
git commit -- src/services/jobs/api.py \
  -m "refactor: extract GET media/resource routes to dispatch table handlers (stream/speaker-audio/reports/jianying)"
```

---

## Step 5 · 迁移第三批：POST 修改族（create + review + editing + segments + internal）

**目标**：将 `do_POST`（`:976`–`:2208`，约 1,233 行）内所有 `if` 分支提取为 handler 函数并注册进 `_get_table_POST`，完成 POST dispatch table。

**涉及路由族**（按关注点分组，每组作为 1 个 handler 函数；以实际行号为准）：
- `path_parts == ["jobs"]`（`:980`）→ `_handle_POST_create_job`（~80 行，含所有参数解析）
- `len==3 and path_parts[2]=="continue"`（`:1074`）→ `_handle_POST_continue_job`
- `len==4 and path_parts[2]=="speaker-audio" and path_parts[3]=="reassign"`（`:1080`）→ `_handle_POST_speaker_audio_reassign`
- `len==4 and path_parts[2]=="speaker-audio" and path_parts[3]=="dubbing-mode"`（`:1104`）→ `_handle_POST_speaker_audio_dubbing_mode`
- `len==3 and path_parts[2]=="enter-edit"`（`:1128`）→ `_handle_POST_enter_edit`
- `len==4 and path_parts[2]=="editing" and path_parts[3]=="cancel"`（`:1135`）→ `_handle_POST_editing_cancel`
- `len==5 and path_parts[2]=="editing" and path_parts[3]=="bulk-replace" and path_parts[4]=="preview"` / `"apply"`（`:1148`/`:1161`）→ `_handle_POST_editing_bulk_replace`（合并为一个函数，内部子判断）
- segments 族（`:1180`–`:1380`，多个 `path_parts[4]` 子分支）→ `_handle_POST_segment_action`（一个函数，内部 if/elif path_parts[4]）
- `len==3 and path_parts[2]=="regenerate-all-tts"`（`:1382`）→ `_handle_POST_regen_all_tts`
- `len==3 and path_parts[2]=="regenerate-selected-tts"`（`:1397`）→ `_handle_POST_regen_selected_tts`
- `len==4 and path_parts[2]=="regenerate-all-tts" and path_parts[3]=="cancel"`（`:1415`）→ `_handle_POST_regen_all_tts_cancel`
- editing voice-map / speakers / revert-unsynced-text / commit（`:1430`–`:1619`）→ `_handle_POST_editing_action`（一个函数，内部 if/elif path_parts[3]；或按 path_parts[3] 再拆 4 个函数——选前者较保守）
- `len>=4 and path_parts[2]=="review"`（`:1644`）→ `_handle_POST_review_action`（含 review subpath 子分支）
- internal 族（`:1769`–`:2139`）→ `_handle_POST_internal_action`（**必须保持 `X-Internal-Key` 校验在 dispatch 之前**，见不变量 §4）
- `len==3 and path_parts[2]=="generate-video"`（`:1883`）→ 含在 internal 族或单独 handler，按实际行号确认
- `len==3 and path_parts[2]=="generate-jianying-draft"`（`:1970`）→ 同上
- `len==3 and path_parts[2]=="cancel"`（`:2140`）→ `_handle_POST_cancel_job`

✅ 已决策（CodeX 2026-06-25）：`do_POST` dispatch 壳中对所有 `path_parts[0]=="internal"` 的 key 做**统一前置鉴权**（dispatch 前校验 `X-Internal-Key`），而非嵌入各 internal handler。此方案可接受，行为等价。**执行时前置动作（已定方向）**：确认迁移后 Step 1 的两个 contract 用例（`test_internal_missing_key_returns_403` / `test_internal_correct_key_passes_gate`）仍全绿，以此作为行为等价的机器验证证据。

**具体做法**：参考 Step 3/4，逐族剪切，每族注册一个 key，删除 fallback 块。建议顺序：先 create job（最重要），再 review/editing（有 gate），再 internal（有鉴权）。

**文件**：
- `src/services/jobs/api.py`（改：新增约 12–16 个 POST handler 方法，完成 `_get_table_POST`，清空 fallback `if` 链）

**该步验收**：

```bash
# 全量 job_api 测试绿（含 phase1 / phase2 / jianying / voice_label 等）
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_phase1.py tests/test_job_api_phase2.py tests/test_job_api_jianying_generate.py tests/test_job_api_jianying_status.py tests/test_job_api_voice_label.py tests/test_job_api_dispatch_contracts.py -q
# 预期：全 passed，0 failed

# do_POST 内 fallback if 链清空
awk '/def do_POST/,/def do_DELETE/' src/services/jobs/api.py | grep -c "if path_parts"
# 预期：0

# X-Internal-Key 校验逻辑仍存在（未丢失）
grep -c "X-Internal-Key" src/services/jobs/api.py
# 预期：>= 2（do_GET jianying-draft-status 处 1 次 + do_POST internal 处 1 次）
```

**commit 边界**：

```bash
git commit -- src/services/jobs/api.py \
  -m "refactor: extract POST mutation routes to dispatch table handlers (create/review/editing/segments/internal)"
```

---

## Step 6 · 收尾：DELETE/PATCH dispatch + 量化行数收益验证

**目标**：将 `do_DELETE`（`:2209`，约 15 行）和 `do_PATCH`（`:2224`，约 37 行）也改为 dispatch 形式（体量小，直接改成 dispatch table 或保留一层简单 `if` 均可），然后量化全部收益、更新 `file-size-guard` 基线。

**动作**：

1. `do_DELETE` 当前只有 1 个路由分支（`len==2 and path_parts[0]=="jobs"` → 删任务）。可提取为 `_handle_DELETE_job` 方法或直接在 `do_DELETE` 壳内调用（体量小，直接提取为方法即可，不必单独 table）。
2. `do_PATCH` 当前只有 1 个路由分支（`len==2 and path_parts[0]=="jobs"` → 改名）。同 `do_DELETE` 处理方式。
3. 删除 `_get_table_GET` / `_get_table_POST` 的 fallback 分支（如 Step 3–5 已清空 `if` 链，则此时 fallback 应为空，可删）。
4. 运行 `wc -l src/services/jobs/api.py` 确认行数下降幅度（预期：2,645 → 约 2,300–2,450，即 `do_GET` + `do_POST` 从 ~2,000 行压缩到 dispatch 壳约 60 行 + handler 函数合计约 1,800 行；净减约 200 行，主要来自去除重复的 `path_parts` 解析和嵌套缩进）。
5. 若 TU-03（file-size guard）已落地，更新 `tools/file_size_baseline.json` 中 `src/services/jobs/api.py` 的基线行数为新值，并注释变更原因。

✅ 已决策（CodeX 2026-06-25）：净减约 200 行是合理预期，本单元**不以大幅减行为目标**。核心收益指标已调整为 **Locality / 路由可定位性**——`_get_table_GET` / `_get_table_POST` 一屏看全所有路由注册，`do_GET` / `do_POST` 壳 ≤ 40 行。行数 `<= 2,450` 的 DoD 项保留作安全网（防退化），但不作主要成功标准。若要进一步压缩 `api.py` 总行数，需把 handler 实现迁入 `JobService` / `src/services/jobs/editing*.py`（STRUCT-07 / TU-18 范围）。

**文件**：
- `src/services/jobs/api.py`（改：`do_DELETE`/`do_PATCH` handler 提取，删 fallback 清理）
- `tools/file_size_baseline.json`（改：如 TU-03 已落地则更新基线；否则跳过）

**该步验收**：

```bash
# 全量 job_api 测试绿（终态验收）
python -m pytest tests/test_job_api.py tests/test_job_api_error_handling.py tests/test_job_api_express_filter.py tests/test_job_api_phase1.py tests/test_job_api_phase2.py tests/test_job_api_jianying_generate.py tests/test_job_api_jianying_status.py tests/test_job_api_rename.py tests/test_job_api_reports.py tests/test_job_api_smart_quality_report.py tests/test_job_api_voice_label.py tests/test_job_api_dispatch_contracts.py -q
# 预期：全 passed，0 failed

# ★ 主指标：do_GET 壳不超过 40 行（Locality / 路由可定位性）
awk '/def do_GET/,/def do_POST/' src/services/jobs/api.py | wc -l
# 预期：<= 40

# ★ 主指标：do_POST 壳不超过 40 行（Locality / 路由可定位性）
awk '/def do_POST/,/def do_DELETE/' src/services/jobs/api.py | wc -l
# 预期：<= 40

# 安全网：行数退化检查（非主指标，防止意外增行）
wc -l src/services/jobs/api.py
# 预期：<= 2450（比 2645 减少 >= 195 行）

# dispatch table 可独立导入（不依赖 handler 实例，_route_key 函数可用）
python -c "from services.jobs.api import _route_key, build_job_api_server; print('OK')"
# 预期：OK

# contract 测试的路由计数断言仍通过（路由未丢失）
python -m pytest tests/test_job_api_dispatch_contracts.py::test_dispatch_route_count_baseline -v
# 预期：passed

# 鉴权 contract 仍通过（内部端点前置 guard 行为等价确认）
python -m pytest tests/test_job_api_dispatch_contracts.py::test_internal_missing_key_returns_403 tests/test_job_api_dispatch_contracts.py::test_internal_correct_key_passes_gate -v
# 预期：全 passed
```

**commit 边界**：

```bash
git commit -- src/services/jobs/api.py \
  -m "refactor: complete dispatch table for DELETE/PATCH; do_GET/do_POST now pure dispatch shells"
```

---

## 测试计划（新增 / 回归）

### 新增（Step 1）

| 文件 | 用例 | 类型 |
|---|---|---|
| `tests/test_job_api_dispatch_contracts.py` | `test_unknown_get_route_returns_404` | contract |
| | `test_unknown_post_route_returns_404` | contract |
| | `test_internal_missing_key_returns_403` | contract |
| | `test_internal_correct_key_passes_gate` | contract |
| | `test_anonymous_preview_artifacts_returns_empty_list` | contract |
| | `test_dispatch_route_count_baseline` | contract（基线守卫） |

### 回归（每步必跑）

以下测试文件在 Step 3 / 4 / 5 / 6 的每次 commit 前都必须全绿：

```bash
python -m pytest \
  tests/test_job_api.py \
  tests/test_job_api_error_handling.py \
  tests/test_job_api_express_filter.py \
  tests/test_job_api_phase1.py \
  tests/test_job_api_phase2.py \
  tests/test_job_api_jianying_generate.py \
  tests/test_job_api_jianying_status.py \
  tests/test_job_api_rename.py \
  tests/test_job_api_reports.py \
  tests/test_job_api_smart_quality_report.py \
  tests/test_job_api_voice_label.py \
  tests/test_job_api_dispatch_contracts.py \
  tests/test_jobapi_cleanup_delegate.py \
  -q
# 预期：全 passed，0 failed
```

### 新增后的 dispatch 可测试性验证

dispatch table 化完成后，`_route_key` 可被独立单测（不需要启动 HTTP server）：

```python
# 可在 test_job_api_dispatch_contracts.py 中追加
from services.jobs.api import _route_key

def test_route_key_jobs_list():
    assert _route_key(["jobs"]) == (1, "jobs")

def test_route_key_job_single():
    assert _route_key(["jobs", "abc123"]) == (2, "jobs")

def test_route_key_job_logs():
    assert _route_key(["jobs", "abc123", "logs"]) == (3, "logs")
```

---

## 回滚方案

本单元分 6 步增量提交，每步独立可回滚：

- **Step 1（仅测试）**：`git revert <commit>`，删除 `tests/test_job_api_dispatch_contracts.py`。对生产代码无影响。
- **Step 2（骨架）**：`git revert <commit>`，回到无 dispatch table 骨架的原始状态。
- **Step 3–5（路由迁移）**：每步独立 commit，`git revert <commit>` 可精确回滚到迁移前一步。回滚后原 `if` 链恢复，测试仍绿。
- **Step 6（收尾）**：`git revert <commit>`，`file_size_baseline.json` 回滚到旧值。

**文件边界**：本单元只涉及 `src/services/jobs/api.py` 和 `tests/test_job_api_dispatch_contracts.py`（以及可选的 `tools/file_size_baseline.json`）。不触碰其他任何文件，回滚风险极低。

---

## 完成定义（DoD）

- [ ] `tests/test_job_api_dispatch_contracts.py` 新增并通过（Step 1）
- [ ] `_route_key(path_parts)` 函数在模块顶层可独立导入（Step 2）
- [ ] **★ 主指标** `do_GET` 函数体（`def do_GET` 至下一个 `def`）不超过 40 行——Locality 指标（Step 4）
- [ ] **★ 主指标** `do_POST` 函数体（`def do_POST` 至下一个 `def`）不超过 40 行——Locality 指标（Step 5）
- [ ] `do_GET` 和 `do_POST` 内不再有 `if path_parts[2] ==` 条件分支（Step 4/5）
- [ ] 以下全量测试全绿（Step 6 终态）：`test_job_api*.py`、`test_jobapi_cleanup_delegate.py`、`test_job_api_dispatch_contracts.py`（`python -m pytest tests/ -k "job_api" -q` 全 passed）
- [ ] **鉴权 contract**：`test_internal_missing_key_returns_403` 与 `test_internal_correct_key_passes_gate` 在迁移前后均通过（D2 / D7）
- [ ] **安全网** `api.py` 总行数 <= 2,450（防退化；非主指标——净减 ~200 行是合理预期，不以大幅减行为目标）
- [ ] `_policy_mode_for` 和 `EXPRESS_ALLOWED_*` 过滤逻辑在 handler 函数内完整保留（`grep -c "_policy_mode_for" src/services/jobs/api.py` >= 2）
- [ ] `X-Internal-Key` 鉴权校验在 `do_POST` dispatch 壳的 internal 前置 guard 处保留（`grep -c "X-Internal-Key" src/services/jobs/api.py` >= 2）
- [ ] `build_job_api_server` 签名无变化（`grep "^def build_job_api_server" src/services/jobs/api.py` 返回原始签名）
- [ ] 不替换 HTTP 框架（`BaseHTTPRequestHandler` 保持不动；FastAPI 迁移是 TU-18 范围）
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`

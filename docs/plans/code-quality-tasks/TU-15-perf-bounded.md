# TU-15 · 性能有界优化（缓存 + to_thread + 查询）

- **目标 / 价值**：消除 FastAPI 异步事件循环中的三类高频阻塞点——(1) 热路径同步 I/O 调用（`load_settings` 每次读文件、`_load_minimax_pool` 每次 HTTP、`build_disk_overview` 磁盘扫描、`assemble_sample_from_job_segments` transcript 读取），(2) `intercept_list_jobs` 的全表 `SELECT job_id` 冗余扫描，(3) 废弃 API `asyncio.get_event_loop()`（Python 3.10+ deprecation，Python 3.12 行为已变）。改动均在现有调用点上做最小包裹，不改业务逻辑、不降准确性、不触碰付费 API 路径。
- **关联发现**：PERF-001（minimax pool 无缓存）、PERF-002（load_settings 无缓存）、PERF-003（intercept_list_jobs 全表扫）、PERF-004（build_disk_overview 同步磁盘扫描阻塞事件循环）、PERF-005（admin_disk_api urllib 同步）、PERF-006（sample_assembler 同步 transcript 读）、PERF-007（pan/auth exchange_code 同步）、ASYNC-01（file_lock 同步调用在 async 函数）、ASYNC-02（voice_selection_api get_event_loop）、ASYNC-03（voice_calibration_inflight get_event_loop）
- **前置依赖**：无（可与其他 Wave D 单元并行；与 TU-14 的 process.py 内点不重叠）
- **建议分支**：`quality/perf-bounded`
- **预估工时**：M（约 3–5 个开发日，各子步骤独立可分批 commit）

> **命令环境**：默认 Git Bash / CI Linux；PowerShell 执行者改用等价命令（`grep` → `Select-String`、`wc -l` → `(Get-Content ...).Count`、`test -f` → `Test-Path`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **缓存写后 invalidate**：`load_settings` 采用"写后立即 invalidate"策略（`save_settings` 末尾调用 `invalidate_settings_cache()`），保证管理配置读写立即一致，不依赖 TTL 自然过期。
- **孤立任务对账先测量**：`intercept_list_jobs` 的孤立任务对账改为按需单条查后，不预先引入 LRU 缓存或后台任务；如后续测量发现实际触发频率高再专项处理。
- **同步阻塞 helper 用 `asyncio.to_thread` + `asyncio.wait_for`**：所有同步磁盘扫描（`_scan_disk_jobs_sync`）和同步 HTTP helper（`_check_resize_helper_status` 等）均用 `asyncio.to_thread` 包裹；对可能慢的 HTTP helper 额外加 `asyncio.wait_for` 设超时上限，防止线程池线程长期占用。
- **不引入自定义线程池**：统一沿用 Python 默认线程池（`asyncio.to_thread` 底层），不单独创建 `ThreadPoolExecutor`。
- **`load_settings` 调用点数以仓库实测为准（27 处）**：spec 文档所说"61 调用点"含 worktree 副本，gateway 仓库根目录实测值为 27，Step 0 核实命令结果以此为准。
- **不碰付费调用点**：本单元所有改动均不涉及 MiniMax 付费克隆 / 付费 TTS / LLM / ASR 路径。

---

## 不在本单元范围（out-of-scope）

- `src/pipeline/process.py` 内任何改动（属 TU-14 专属）
- 付费 TTS / LLM / ASR / MiniMax clone 调用路径——**绝对不碰**
- `pan/baidu_pan_client.py` 的 `requests.*` 调用（已在 `backup_executor` 的 `asyncio.to_thread` 中调用，不属于本单元）
- 引入新的外部依赖（`cachetools`、`aiofiles` 等）——统一用 stdlib（`threading.Lock` + `time.monotonic()`，或直接 `asyncio.to_thread`）
- Gateway DB schema / Alembic migration
- DSP 对齐算法、retiming 数学逻辑
- 前端代码

---

## 必守不变量

1. **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / LLM / ASR 绝不在 fallback / except / retry / batch 路径自动触发；`_load_minimax_pool` 的缓存只缓存声音目录列表，不影响克隆调用路径。
2. **Alignment DSP-first**：不迁 LLM，不改 retiming 数学确定性。
3. **剪映 draft 为主交付物**：本单元不涉及 publish/delivery 路径。
4. **Gateway 是 plan / pricing / entitlement 唯一事实源**：不改 entitlement 判断逻辑。
5. **默认测试不接真实外部服务**：新增测试必须 mock 外部 HTTP / 文件系统，不依赖真实 Gateway 或 Baidu Pan。
6. **process.py 走 Option B**：本单元完全不涉及 process.py 结构。
7. **缓存不降准确性**：TTL 缓存只用于"最终一致即可"的管理配置读取（5 s TTL）和声音目录（120 s TTL）；实时业务决策（用户余额、任务状态）不缓存。

---

## Step 0 · 确认现状

```bash
git switch -c quality/perf-bounded

# 确认 load_settings 位置（spec 说 :860）
grep -n "^def load_settings" gateway/admin_settings.py

# 确认 intercept_list_jobs 位置（spec 说 :1385）
grep -n "^async def intercept_list_jobs" gateway/job_intercept.py

# 确认全表扫描行（spec 说 :1385，实测 :1385）
grep -n "select(Job\.job_id)" gateway/job_intercept.py

# 确认 voice_selection_api get_event_loop（spec 说 :741）
grep -n "get_event_loop()" gateway/voice_selection_api.py

# 确认 voice_calibration_inflight get_event_loop（spec 说 :143）
grep -n "get_event_loop()" gateway/voice_calibration_inflight.py

# 确认 build_disk_overview 位置（spec 说 :531）
grep -n "^async def build_disk_overview" gateway/admin_disk_api.py

# 确认 urllib.request 同步调用在 admin_disk_api（spec 说 :268 区域）
grep -n "urllib.request.urlopen\|urllib.request.Request" gateway/admin_disk_api.py

# 确认 sample_assembler assemble_sample_from_job_segments 位置（spec 说 :132）
grep -n "^async def assemble_sample_from_job_segments" gateway/cosyvoice_clone/sample_assembler.py

# 确认 pan/auth exchange_code 位置（spec 说 :224）
grep -n "client\.exchange_code" gateway/pan/auth.py

# 确认 _load_minimax_pool 位置（spec 说 :63）
grep -n "^def _load_minimax_pool" src/services/tts/minimax_voice_selector.py

# 统计 load_settings() 调用点数（参考值约 27 处，spec 说 61 个，以实测为准）
grep -rn "load_settings()" gateway/ --include="*.py" | wc -l
```

**行号核实结果（写文档时已 grep 确认）**：

| 符号 | spec 行号 | 实测行号 |
|---|---|---|
| `load_settings` | :860 | **:860** ✓ |
| `intercept_list_jobs` | :1385 | **:1360**（函数定义）/ 全表扫在 **:1385** ✓ |
| `voice_selection_api` `get_event_loop` | :741 | **:741** ✓ |
| `voice_calibration_inflight` `get_event_loop` | — | **:143** |
| `build_disk_overview` | :531 | **:531** ✓ |
| `_load_minimax_pool` | :63 | **:63** ✓ |
| `assemble_sample_from_job_segments` | :132（spec 位置）| **:132** ✓ |
| `pan/auth` `exchange_code` | :224 | **:224** ✓ |

> ✅ 已决策（CodeX 2026-06-25）：`load_settings` 调用点实测为 27 处（`grep -rn "load_settings()" gateway/ --include="*.py" | wc -l`）；spec 所说"61 调用点"含 worktree 副本，以仓库根目录实测值 27 为准。

---

## Step 1 · `load_settings` 加 5 s TTL 缓存（PERF-002）

**背景**：`load_settings()` 是纯同步函数，每次调用都从磁盘读取 JSON 文件（`SETTINGS_FILE.read_text()`）。该函数在 27 个 gateway 调用点（含请求热路径）被直接调用，每次请求都会触发一次 `stat` + 磁盘读。

**文件**：`gateway/admin_settings.py:860`

**改法**：在 `load_settings` 函数上方添加线程安全的 TTL 缓存包装器，用 stdlib `threading.Lock` + `time.monotonic()`，不引入第三方库。

```python
# 在 gateway/admin_settings.py 中，load_settings 函数定义之前（约第 858 行）插入：

import threading as _threading
import time as _time

_settings_cache: "AdminSettings | None" = None
_settings_cache_expires: float = 0.0
_settings_cache_lock = _threading.Lock()
_SETTINGS_CACHE_TTL = 5.0  # seconds


def load_settings() -> "AdminSettings":
    """Load settings from JSON file, returning defaults if missing.

    结果缓存 5 s（TTL）——admin 操作写入后通过 ``invalidate_settings_cache()``
    强制失效，保证写后读一致性。
    """
    global _settings_cache, _settings_cache_expires
    now = _time.monotonic()
    with _settings_cache_lock:
        if _settings_cache is not None and now < _settings_cache_expires:
            return _settings_cache
        result = _load_settings_from_disk()
        _settings_cache = result
        _settings_cache_expires = now + _SETTINGS_CACHE_TTL
        return result


def _load_settings_from_disk() -> "AdminSettings":
    """原始磁盘读取逻辑，由 load_settings 调用。"""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return AdminSettings(**data)
        except Exception:
            logger.warning("Failed to parse %s, using defaults", SETTINGS_FILE)
    return AdminSettings()


def invalidate_settings_cache() -> None:
    """强制失效缓存。在 save_settings / 任何写操作后调用。"""
    global _settings_cache_expires
    with _settings_cache_lock:
        _settings_cache_expires = 0.0
```

在 `save_settings` 函数末尾（`with file_lock(SETTINGS_FILE):` 块结束后）追加 `invalidate_settings_cache()` 调用，确保写后读一致性。

**注意**：`load_settings` 被同步调用（非 async），不需要 `asyncio.to_thread` 包装。TTL 设 5 s 意味着最坏情况下 admin 写入后 5 s 内各节点仍读旧值，可接受（现有行为是每次都读文件）。

✅ 已决策（CodeX 2026-06-25）：采用写后立即 invalidate 策略——`save_settings` 末尾调用 `invalidate_settings_cache()`，保证管理配置读写立即一致，不依赖 5 s TTL 自然过期。

**该步验收**：

```bash
# 1. 原有函数签名不变（API 兼容）
grep -n "^def load_settings\|^def _load_settings_from_disk\|^def invalidate_settings_cache" \
  gateway/admin_settings.py
# 期望：三行均出现

# 2. 回归测试不新增失败
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20

# 3. 若有专项测试可单独跑
python -m pytest tests/test_legacy_cleanup_guards.py -v
```

---

## Step 2 · `_load_minimax_pool` 加 120 s TTL 缓存（PERF-001）

**背景**：`_load_minimax_pool()` 每次调用都向 `http://127.0.0.1:8880/api/internal/voice-catalog` 发起一次同步 `requests.get`（含 3 s timeout）。该函数在 `select_minimax_voice_match` 内部调用，后者在每次音色匹配时触发。MiniMax 声音目录（604 条）内容稳定，120 s TTL 完全足够。

**文件**：`src/services/tts/minimax_voice_selector.py:63`

**改法**：在模块级添加缓存变量，用 stdlib `threading.Lock` + `time.monotonic()`。

```python
# 在 minimax_voice_selector.py 文件顶部（import 之后，_LANGUAGE_ALIASES 之前）插入：

import threading as _threading
import time as _time

_pool_cache: "list[dict] | None" = None
_pool_cache_expires: float = 0.0
_pool_cache_lock = _threading.Lock()
_POOL_CACHE_TTL = 120.0  # seconds — 声音目录变化频率极低


def _get_cached_minimax_pool() -> list[dict]:
    """线程安全的 120 s TTL 缓存包装。仅缓存目录列表，不影响克隆路径。"""
    global _pool_cache, _pool_cache_expires
    now = _time.monotonic()
    with _pool_cache_lock:
        if _pool_cache is not None and now < _pool_cache_expires:
            return _pool_cache
        result = _load_minimax_pool()
        _pool_cache = result
        _pool_cache_expires = now + _POOL_CACHE_TTL
        return result
```

然后将 `select_minimax_voice_match` 函数内对 `_load_minimax_pool()` 的直接调用替换为 `_get_cached_minimax_pool()`。

**该步验收**：

```bash
# 1. 调用点已替换
grep -n "_load_minimax_pool\|_get_cached_minimax_pool" \
  src/services/tts/minimax_voice_selector.py
# 期望：_get_cached_minimax_pool 出现在 select_minimax_voice_match 内
# _load_minimax_pool 仍存在（被 _get_cached_minimax_pool 调用）但不再被 select 直接调用

# 2. 回归测试
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 3 · `intercept_list_jobs` 去全表 `SELECT job_id` 冗余扫描（PERF-003）

**背景**：`intercept_list_jobs` 在认证路径下执行两次查询：
- 第 1 次（`gateway/job_intercept.py:1385`）：`SELECT job_id FROM jobs`——全表扫描，仅用于孤立任务对账（reconciliation），不涉及结果过滤。
- 第 2 次（`:1391`）：`SELECT * FROM jobs WHERE user_id = ?`——按用户过滤，是真正需要的数据。

全表扫描代价随任务数量线性增长，生产中任务数达数千时每次列表请求都触发一次全表扫。

**文件**：`gateway/job_intercept.py:1384–1386`

**改法**：将全表扫改为"只有当 user_jobs 里出现上游 JSON 有而 DB 无的 job_id 时才按需查"的惰性策略。具体：

1. 删除第 1385 行的 `result_all = await db.execute(select(Job.job_id))` 及 `all_db_job_ids` 赋值。
2. 将原来使用 `all_db_job_ids` 的孤立任务对账逻辑改为：在遍历到某个 `jid` 不在 `user_job_ids` 时，按需执行单条 `SELECT EXISTS(SELECT 1 FROM jobs WHERE job_id = ?)` 查询。
3. 避免在高频路径（每次列表请求）执行全表扫。

```python
# gateway/job_intercept.py — 原来在 :1384–1386 的：
#   result_all = await db.execute(select(Job.job_id))
#   all_db_job_ids = {row[0] for row in result_all.all()}
# 替换为：（删除这两行，改用惰性查）

# 在孤立任务判断处（原来用 all_db_job_ids 的地方）改为：
from sqlalchemy import exists, literal

async def _job_exists_in_db(db: AsyncSession, job_id: str) -> bool:
    """按需单条查，避免全表扫。"""
    result = await db.execute(
        select(literal(1)).where(Job.job_id == job_id).limit(1)
    )
    return result.scalar() is not None
```

> ✅ 已决策（CodeX 2026-06-25）：孤立任务对账先实施按需单条查，不预先引入 LRU 缓存或异步后台任务。如后续测量发现对账触发频率显著影响性能，再专项评估。

**该步验收**：

```bash
# 1. 全表扫已删除
grep -n "select(Job\.job_id)" gateway/job_intercept.py
# 期望：该行不再出现（或只在注释中）

# 2. 孤立对账逻辑仍存在（改为按需查）
grep -n "all_db_job_ids\|_job_exists_in_db" gateway/job_intercept.py

# 3. 回归测试
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 4 · `voice_selection_api` 和 `voice_calibration_inflight` 替换废弃 `get_event_loop()`（ASYNC-02/03）

**背景**：Python 3.10+ 中 `asyncio.get_event_loop()` 在没有运行中事件循环时会 DeprecationWarning，Python 3.12 中若在非主线程且无事件循环时会 RuntimeError。项目使用 Python 3.12（pyproject.toml: `requires-python = ">=3.12,<3.13"`）。

**文件 A**：`gateway/voice_selection_api.py:741`

原代码：
```python
loop = asyncio.get_event_loop()
...
concat_path = await loop.run_in_executor(None, concat_segments_to_wav, ...)
...
clone_result = await loop.run_in_executor(None, _clone_via_minimax, ...)
```

改法：直接用 `asyncio.to_thread`（Python 3.9+）替换 `loop.run_in_executor(None, ...)` 模式：

```python
# 删除 loop = asyncio.get_event_loop() 这一行
# 将 loop.run_in_executor(None, func, *args) 替换为 asyncio.to_thread(func, *args)
concat_path = await asyncio.to_thread(
    concat_segments_to_wav,
    source_audio,
    selected_segments,
    project_dir,
    speaker_id,
)
```

> 注意：`asyncio.to_thread` 只能传 `*args`，不支持 `**kwargs`。若调用处有关键字参数，需用 `functools.partial` 包装。检查实际参数形式后按需调整。

**文件 B**：`gateway/voice_calibration_inflight.py:143`

原代码：
```python
future: asyncio.Future = asyncio.get_event_loop().create_future()
```

改法：
```python
future: asyncio.Future = asyncio.get_running_loop().create_future()
```

`asyncio.get_running_loop()` 在有运行中事件循环时等价，在无运行中事件循环时抛 `RuntimeError`（比 `get_event_loop` 的静默降级行为更早暴露问题）。该函数只在 `async with self._lock` 内部调用（已在协程上下文），所以 `get_running_loop()` 总是成功。

**该步验收**：

```bash
# 1. 废弃 API 已清除
grep -rn "get_event_loop()" gateway/ --include="*.py"
# 期望：0 行（或仅存在于 alembic/env.py 等非业务文件）

# 2. 替换为 to_thread / get_running_loop
grep -n "asyncio\.to_thread\|get_running_loop" \
  gateway/voice_selection_api.py gateway/voice_calibration_inflight.py

# 3. 回归测试
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 5 · `build_disk_overview` 同步磁盘扫描用 `asyncio.to_thread` 包裹（PERF-004）

**背景**：`gateway/admin_disk_api.py:531` 的 `build_disk_overview` 是 `async def`，但内部的 `_iter_disk_job_dirs`（含 `os.walk`）和 `_directory_size_bytes`（含 `os.walk` / `subprocess.check_output`）是纯同步磁盘 I/O，在 async 函数中直接调用会阻塞事件循环。管理后台操作，低频但扫描时间可能较长（生产有数千任务目录）。

**文件**：`gateway/admin_disk_api.py:531`

**改法**：将同步磁盘扫描主循环抽成独立同步函数 `_scan_disk_jobs_sync(root, jobs, now)`，然后在 `build_disk_overview` 中用 `asyncio.to_thread` 调用：

```python
# 新增纯同步扫描函数（放在 build_disk_overview 之前）
def _scan_disk_jobs_sync(
    root: Path,
    jobs: dict,
    now: datetime,
) -> tuple[list[dict], dict[str, list[dict]]]:
    """纯同步磁盘扫描，供 asyncio.to_thread 调用。
    返回 (all_rows, buckets)。
    """
    buckets: dict[str, list[dict]] = {
        "orphan_dirs": [],
        "expired_dirs": [],
        "protected_expired_dirs": [],
        "failed_dirs": [],
        "active_largest_dirs": [],
    }
    all_rows: list[dict] = []
    for user_id, job_dir in _iter_disk_job_dirs(root):
        # ... 原 build_disk_overview 的 for 循环体搬来 ...
    return all_rows, buckets


async def build_disk_overview(
    db: AsyncSession,
    *,
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    root = project_root or _resolve_scan_root()

    result = await db.execute(select(Job))
    jobs = {job.job_id: job for job in result.scalars().all()}

    # 磁盘扫描在线程池中执行，不阻塞事件循环
    all_rows, buckets = await asyncio.to_thread(_scan_disk_jobs_sync, root, jobs, now)

    # 后续统计逻辑不变 ...
```

**执行时前置动作（已定方向）**：实施前确认 `_iter_disk_job_dirs` + `_directory_size_bytes` 仍为纯同步（无 SQLAlchemy session / 无 async 上下文依赖），确认后再提交。方向已定：只要纯同步即可安全 `asyncio.to_thread`；若发现依赖 async 上下文则先重构剥离。

**该步验收**：

```bash
# 1. 同步扫描已抽出
grep -n "_scan_disk_jobs_sync\|asyncio\.to_thread.*_scan_disk" \
  gateway/admin_disk_api.py
# 期望：两行均出现

# 2. build_disk_overview 内 for 循环已迁移到 _scan_disk_jobs_sync
grep -n "for user_id, job_dir in _iter_disk_job_dirs" gateway/admin_disk_api.py
# 期望：出现在 _scan_disk_jobs_sync 内，不再在 build_disk_overview 内直接出现

# 3. 回归测试
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 6 · `admin_disk_api` `urllib.request.urlopen` 同步调用用 `asyncio.to_thread` 包裹（PERF-005）

**背景**：`gateway/admin_disk_api.py` 中的磁盘扩容 helper 调用（`_check_resize_helper_status`、`_do_resize_helper_call` 等函数）使用 `urllib.request.urlopen`（同步阻塞），在 async endpoint 中直接调用会阻塞事件循环。这些是管理后台操作，但仍需修正。

**文件**：`gateway/admin_disk_api.py`（具体行：`grep -n "urllib.request.urlopen" gateway/admin_disk_api.py` 确认，写文档时确认在第 268 行附近）

**改法**：将 `_check_resize_helper_status` 函数及相关 `urllib.request.urlopen` 调用包装为纯同步函数（如已是同步函数则直接用），在 async endpoint 中通过 `asyncio.to_thread` 调用：

```python
# 若 _check_resize_helper_status 目前已是普通 def（非 async），
# 在调用侧（async endpoint 或 async 函数）改为：
status = await asyncio.to_thread(_check_resize_helper_status)
# 若原来是 async def 但内部用 urllib（混用），将其改为纯 def，
# 外层 async caller 用 asyncio.to_thread 包裹。
```

✅ 已决策（CodeX 2026-06-25）：对可能慢的 HTTP helper 加 `asyncio.wait_for` 超时上限（建议与 `urlopen` 自身 timeout 对齐，如 `asyncio.wait_for(..., timeout=10.0)`），防止线程池线程长期占用。不引入自定义线程池，沿用 Python 默认线程池。

```python
# 推荐写法（调用侧）：
status = await asyncio.wait_for(
    asyncio.to_thread(_check_resize_helper_status),
    timeout=10.0,
)
```

**该步验收**：

```bash
# 1. async 函数中无直接 urllib.request.urlopen 调用
#    （允许在纯 def 函数中使用，由 to_thread 包裹）
python -c "
import ast, pathlib, sys
src = pathlib.Path('gateway/admin_disk_api.py').read_text()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef):
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Attribute) and fn.attr == 'urlopen':
                    print(f'FAIL: urlopen in async def at line {child.lineno}')
                    sys.exit(1)
print('OK')
"

# 2. 可能慢的 HTTP helper 已加 asyncio.wait_for 包裹
grep -n "asyncio\.wait_for" gateway/admin_disk_api.py
# 期望：出现至少一处（对应 _check_resize_helper_status 或等价 helper）

# 3. 回归测试
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 7 · `assemble_sample_from_job_segments` transcript 同步读取用 `asyncio.to_thread`（PERF-006）

**背景**：`gateway/cosyvoice_clone/sample_assembler.py:200` 中，`async def assemble_sample_from_job_segments` 在异步上下文中直接调用 `transcript_path.read_text(encoding="utf-8")`（同步文件读）及后续的 `concat_segments_to_wav`（已是同步的 ffmpeg 调用）。

**文件**：`gateway/cosyvoice_clone/sample_assembler.py:132–260`

**改法**：将从 transcript 读取到 `concat_segments_to_wav` 调用的同步 I/O 部分抽成纯同步辅助函数 `_build_concat_from_transcript_sync(transcript_path, segment_ids, speaker_id, project_dir, source_audio)`，在 `async def` 中用 `asyncio.to_thread` 包裹：

```python
def _build_concat_from_transcript_sync(
    transcript_path: Path,
    segment_ids: list[int],
    speaker_id: str,
    project_dir: Path,
    source_audio: Path,
) -> Path:
    """Layer 3-6 同步执行：读 transcript → 验证 → concat。
    不含 DB 查询（Layer 1-2 在 async 侧完成）。
    """
    try:
        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptParseError(f"failed to parse transcript: {exc}") from exc
    # ... 原 Layer 3-6 逻辑 ...


# 在 assemble_sample_from_job_segments 中替换：
output_path = await asyncio.to_thread(
    _build_concat_from_transcript_sync,
    transcript_path,
    segment_ids,
    speaker_id,
    project_dir,
    source_audio,
)
```

> 注意：`concat_segments_to_wav` 调用 ffmpeg（同步阻塞，可能耗时数秒），也应包含在 `to_thread` 内，不要单独分开调用。

**该步验收**：

```bash
# 1. async def assemble_sample_from_job_segments 内不再直接调用 read_text
python -c "
import ast, pathlib, sys
src = pathlib.Path('gateway/cosyvoice_clone/sample_assembler.py').read_text()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'assemble_sample_from_job_segments':
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Attribute) and fn.attr == 'read_text':
                    print(f'FAIL: read_text in async def at line {child.lineno}')
                    sys.exit(1)
print('OK')
"

# 2. 守卫测试
python -m pytest tests/test_cosyvoice_clone_sample_assembler.py -v

# 3. 回归
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## Step 8 · `pan/auth` `exchange_code` 同步调用用 `asyncio.to_thread`（PERF-007）

**背景**：`gateway/pan/auth.py:224` 中，`async def _pan_callback_impl` 直接调用 `client.exchange_code(code, ...)` （`BaiduPanClient.exchange_code` 内部用 `requests.post`，同步阻塞），注释说"OAuth bandwidth dominates over thread overhead anyway"——这个判断在低并发时合理，但阻塞事件循环时间可达数秒（网络延迟），应修正。

**文件**：`gateway/pan/auth.py:224`（确认当前行号：`grep -n "client\.exchange_code" gateway/pan/auth.py`）

**改法**：

```python
# 原来（:224）:
tokens = client.exchange_code(code, settings.baidu_pan_redirect_uri)

# 改为：
tokens = await asyncio.to_thread(
    client.exchange_code, code, settings.baidu_pan_redirect_uri
)
```

同时删除或更新注释："backup_executor's asyncio.to_thread pattern needs to apply here it's a one-shot"——现在已经应用了。

**该步验收**：

```bash
# 1. 已改为 to_thread
grep -n "asyncio\.to_thread.*exchange_code\|exchange_code" gateway/pan/auth.py
# 期望：exchange_code 调用处在 asyncio.to_thread 内

# 2. auth 路径回归
python -m pytest tests/ -k "pan" -v --tb=short 2>&1 | tail -20

# 3. 全量回归
python -m pytest tests/ -x -q --tb=short 2>&1 | tail -20
```

---

## 测试计划（新增 / 回归）

### 新增测试

**文件**：`tests/test_tu15_perf_bounded.py`（新建）

```python
"""TU-15 回归：缓存 + to_thread + 查询优化验收测试。"""
import asyncio
import threading
import time
import types
import unittest.mock as mock


# ---------- PERF-002: load_settings TTL 缓存 ----------

def test_load_settings_returns_cached_result_within_ttl(tmp_path, monkeypatch):
    """两次调用在 TTL 内，磁盘读只发生一次。"""
    import gateway.admin_settings as mod
    call_count = {"n": 0}
    orig = mod._load_settings_from_disk
    def counted_load():
        call_count["n"] += 1
        return orig()
    monkeypatch.setattr(mod, "_load_settings_from_disk", counted_load)
    # 强制失效缓存
    mod.invalidate_settings_cache()
    mod.load_settings()
    mod.load_settings()
    assert call_count["n"] == 1, "TTL 内应只读一次磁盘"


def test_load_settings_re_reads_after_invalidate(monkeypatch):
    """invalidate_settings_cache 后下次调用重新读磁盘。"""
    import gateway.admin_settings as mod
    call_count = {"n": 0}
    orig = mod._load_settings_from_disk
    def counted_load():
        call_count["n"] += 1
        return orig()
    monkeypatch.setattr(mod, "_load_settings_from_disk", counted_load)
    mod.invalidate_settings_cache()
    mod.load_settings()
    mod.invalidate_settings_cache()
    mod.load_settings()
    assert call_count["n"] == 2, "invalidate 后应重新读磁盘"


def test_load_settings_thread_safe():
    """多线程并发调用不引发异常。"""
    import gateway.admin_settings as mod
    errors = []
    def worker():
        try:
            mod.load_settings()
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发调用异常: {errors}"


# ---------- PERF-001: minimax pool 缓存 ----------

def test_minimax_pool_cached_within_ttl(monkeypatch):
    """TTL 内 _load_minimax_pool 只被调用一次。"""
    import src.services.tts.minimax_voice_selector as mod
    call_count = {"n": 0}
    orig = mod._load_minimax_pool
    def counted():
        call_count["n"] += 1
        return []
    monkeypatch.setattr(mod, "_load_minimax_pool", counted)
    mod._pool_cache_expires = 0.0  # 强制失效
    mod._get_cached_minimax_pool()
    mod._get_cached_minimax_pool()
    assert call_count["n"] == 1


# ---------- ASYNC-02: get_event_loop 已替换 ----------

def test_no_get_event_loop_in_gateway_business_files():
    """gateway 业务文件中不再使用废弃 get_event_loop()。"""
    import ast
    from pathlib import Path
    skip = {"alembic", "__pycache__"}
    gateway_dir = Path("gateway")
    violations = []
    for py_file in gateway_dir.rglob("*.py"):
        if any(part in skip for part in py_file.parts):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_event_loop"
            ):
                violations.append(f"{py_file}:{node.lineno}")
    assert not violations, f"发现废弃 get_event_loop(): {violations}"
```

### 回归测试（每步完成后必须全绿）

```bash
python -m pytest tests/ -x -q --tb=short
```

关键守卫测试（必须保持绿色）：

```bash
python -m pytest tests/test_legacy_cleanup_guards.py -v
python -m pytest tests/test_cosyvoice_clone_sample_assembler.py -v
python -m pytest tests/test_phase1_guards.py -v
python -m pytest tests/test_phase2_download_backend.py -v
```

---

## 回滚方案

各步骤独立 commit，按需 `git revert` 对应 commit 即可。

| 步骤 | 涉及文件 | 回滚方式 |
|---|---|---|
| Step 1 | `gateway/admin_settings.py` | `git revert <step1-commit>` |
| Step 2 | `src/services/tts/minimax_voice_selector.py` | `git revert <step2-commit>` |
| Step 3 | `gateway/job_intercept.py` | `git revert <step3-commit>`；⚠️ 注意孤立对账逻辑需同步回退 |
| Step 4 | `gateway/voice_selection_api.py`、`gateway/voice_calibration_inflight.py` | `git revert <step4-commit>` |
| Step 5 | `gateway/admin_disk_api.py` | `git revert <step5-commit>` |
| Step 6 | `gateway/admin_disk_api.py` | `git revert <step6-commit>`（与 Step 5 同文件，注意顺序）|
| Step 7 | `gateway/cosyvoice_clone/sample_assembler.py` | `git revert <step7-commit>` |
| Step 8 | `gateway/pan/auth.py` | `git revert <step8-commit>` |
| 测试 | `tests/test_tu15_perf_bounded.py` | 保留（回归守卫） |

---

## 完成定义（DoD）

- [ ] Step 0：`git switch -c quality/perf-bounded`；所有 grep 核对命令输出与本文档"行号核实结果"表一致（或已标注差异）
- [ ] Step 1：`load_settings` 加 5 s TTL 缓存；`invalidate_settings_cache` 在 `save_settings` 末尾**必须调用**（写后立即一致，已决策）；`python -m pytest tests/ -x -q` 全绿
- [ ] Step 2：`_load_minimax_pool` 加 120 s TTL 缓存；`_get_cached_minimax_pool` 替换直接调用；`python -m pytest tests/ -x -q` 全绿
- [ ] Step 3：`intercept_list_jobs` 全表 `select(Job.job_id)` 已删除；孤立对账改为按需单条查；未预加 LRU 缓存或后台任务（先测量再优化，已决策）；`python -m pytest tests/ -x -q` 全绿
- [ ] Step 4：`voice_selection_api.py` 和 `voice_calibration_inflight.py` 中 `get_event_loop()` 已替换；`grep -rn "get_event_loop()" gateway/ --include="*.py"` 返回 0 行（业务文件）
- [ ] Step 5：`build_disk_overview` 磁盘扫描已移入 `_scan_disk_jobs_sync`，通过 `asyncio.to_thread` 调用；`python -m pytest tests/ -x -q` 全绿
- [ ] Step 6：`admin_disk_api` async 函数内 `urllib.request.urlopen` 已移出（通过 `to_thread` 或已在纯 def 中）；AST 检查脚本返回 `OK`；可能慢的 HTTP helper 已加 `asyncio.wait_for` 超时包裹；未引入自定义 `ThreadPoolExecutor`
- [ ] Step 7：`assemble_sample_from_job_segments` transcript 读取及 `concat_segments_to_wav` 已移入 `asyncio.to_thread`；`tests/test_cosyvoice_clone_sample_assembler.py` 全绿
- [ ] Step 8：`pan/auth.py:224` `exchange_code` 改为 `asyncio.to_thread` 包裹；`python -m pytest tests/ -k "pan" -v` 全绿
- [ ] 新增测试文件 `tests/test_tu15_perf_bounded.py` 已提交且全绿
- [ ] 所有守卫测试全绿：`test_legacy_cleanup_guards.py`、`test_cosyvoice_clone_sample_assembler.py`、`test_phase1_guards.py`、`test_phase2_download_backend.py`
- [ ] 付费 API 路径（MiniMax 克隆 / 付费 TTS / LLM / ASR）未被修改（`git diff quality/perf-bounded -- src/services/tts/tts_generator.py src/services/transcript_reviewer.py` 无相关改动）
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`

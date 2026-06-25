# TU-04 · 统一 JSON 原子写 helper

- **目标 / 价值**：将代码库中 8 处独立的原子 JSON 写实现收口为单一 canonical helper（`src/utils/atomic_io.py`），消除 DRY-02 发现。canonical helper 升级为支持 `Path | str`、`dict | list | object`、`fsync` 可选参数、同目录临时文件 + `os.replace`。逐个调用点迁移到 helper、删除重复实现。收口后：任何原子写语义变更（如 fsync 策略）只需改一处。
- **关联发现**：DRY-02（含 H4 完整版）
- **前置依赖**：TU-01（H4 最小修 — `editing_segments._atomic_write_json` 已补 fsync），本单元做完整收口
- **建议分支**：`quality/atomic-write`
- **预估工时**：M（分 8 个调用点逐一迁移，每处 15–30 分钟；含新增测试）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）。PowerShell 执行者改用等价命令（`grep`→`Select-String`、`tail`→`Select-Object -Last`、`test -f`→`Test-Path`、避免 `<(...)`）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **sort_keys=False（editing_segments / editing_voice_map）**：这两处原实现无 `sort_keys`，迁移时必须传 `sort_keys=False`，保持现有 JSON 字节顺序，避免 diff 噪声（防"字节语义变化被包装成重构"）。其余调用点（store / manifest / draft）原已有 `sort_keys=True`，保持不变。
- **业务状态写入 fsync=True 为默认**：`editing_voice_map` / `review_actions` 原实现无显式 fsync；迁移后 wrapper 默认 `fsync=True` 已定，无需再向项目主确认性能问题。若后续性能数据证明有问题，再按调用点降级。
- **目录 fsync 不要求本单元完成**：目录级 fsync 作为后续增强任务，本单元不引入。
- **sidecar / backfill 确认不迁移**：`sidecar_emitter._atomic_write_json`（持有 file_lock 并发语义）和 `gateway/cost_summary_backfill._atomic_write_json`（import 边界约束）两处已在 out-of-scope，决策确认维持现状，仅补 docstring 注释。

---

## 不在本单元范围（out-of-scope）

- `src/services/smart/sidecar_emitter._atomic_write_json`：该函数额外持有 `file_lock()`（threading + fcntl 跨平台 reentrant lock，来自 `_file_lock.py`），是并发安全函数，**不等同于纯原子写**。本单元不合并它（合并会强制所有调用方拿锁，行为变化）；保留为独立实现，在函数 docstring 注明"同见 `utils.atomic_io.atomic_write_json`"。
- `gateway/cost_summary_backfill._atomic_write_json`：位于 gateway 进程，设计上不得 import pipeline 模块（含 `src/`）。该文件 docstring 已注明"Mirrors the pattern in services.smart.sidecar_emitter… Gateway-side (we don't import pipeline modules)"。本单元同样不合并（import 边界约束），保留为 gateway 内独立实现，在其 docstring 注明"同见 `src/utils/atomic_io.atomic_write_json`"。
- `_write_json`（assemblyai/gemini）：这两处是**非原子**直接写（`path.write_text(…)`），属 DRY-03，不在本单元。
- `_write_json_atomic` → `JobStore._write_json_atomic`（`store.py`）的 `fsync=False` group-commit 快速路径：Step 5 合并时须保留 `fsync` 参数语义（含 False 分支），不丢失性能优化。
- process.py 架构改造：本单元不触及 Option B 收敛（属 TU-14）。
- 付费 API 调用点：本单元不涉及。

---

## 必守不变量

- **付费 API 硬约束**：本单元只迁移原子写 helper，不新增、不触发任何付费调用（MiniMax 克隆 / 付费 TTS / 付费 LLM / 付费 ASR）。
- **剪映 draft 为主交付物**：`draft_writer._write_json_atomic` 写剪映草稿 JSON，迁移时必须保留 `DraftError` 异常包装（`except OSError → raise DraftError`），不改写业务错误语义。
- **Gateway 是 plan/pricing/entitlement 唯一事实源**：gateway 进程不 import `src/` pipeline 模块（含 `src/utils/`），本单元严格遵守 import 边界——`gateway/cost_summary_backfill.py` 不迁移。
- **默认测试不接真实外部服务**：新增/回归测试用 `tmp_path` fixture，不接文件系统外的任何外部服务。
- **process.py 走 Option B**：本单元不改 `process.py` 结构。
- **迁移原则**：先补 contract/回归测试再动调用点；每次只迁一处（一 commit）；迁移前后字节语义等价（见该步验收）。

---

## Step 0 · 确认现状

```bash
# 建分支
git switch -c quality/atomic-write

# 确认 canonical helper 当前签名（spec 称 str 入参，需升级为 Path|str）
grep -n "def atomic_write" src/utils/atomic_io.py
# 期望：
#   5: def atomic_write_bytes(target_path: str, data: bytes) -> None:
#  14: def atomic_write_json(target_path: str, data: dict) -> None:

# 确认 8 个待迁移调用点（main tree，排除 .codex_worktrees）
grep -rn "_atomic_write_json\|_write_json_atomic" \
  src/services/jobs/editing_segments.py \
  src/services/jobs/editing_voice_map.py \
  src/services/jobs/review_actions.py \
  src/services/jobs/store.py \
  src/modules/output/manifest_writer.py \
  src/modules/draft/draft_writer.py \
  | head -20
# 期望行号（以实际输出为准，行号可能因并行改动漂移）：
#   editing_segments.py:172  → def _atomic_write_json
#   editing_voice_map.py:87  → def _atomic_write_json
#   review_actions.py:1068   → def _atomic_write_json
#   store.py:439             → def _write_json_atomic  (static method, fsync 参数)
#   manifest_writer.py:149   → def _write_json_atomic  (static method)
#   draft_writer.py:250      → def _write_json_atomic  (method, DraftError wrap)

# 确认 sidecar / backfill 不迁移（仅记录位置）
grep -n "def _atomic_write_json" \
  src/services/smart/sidecar_emitter.py \
  gateway/cost_summary_backfill.py
# 期望：sidecar_emitter.py:208, cost_summary_backfill.py:70

# 现有 atomic_io 测试基线
python -m pytest tests/test_atomic_io.py -q
# 期望：9 passed
```

> 若任何 `file:line` 与上述期望不同，以 grep 实际输出为准，在该步执行时注明。

---

## Step 1 · 升级 canonical helper — 支持 `Path`、`list`、`object`、`fsync` 参数

**动作**：扩展 `src/utils/atomic_io.py:14` 的 `atomic_write_json`，使其签名和行为覆盖所有待迁移调用点的公约数。

**文件**：`src/utils/atomic_io.py`（当前 31 行）

**具体改法**：

```python
"""原子写入工具。写入 .tmp 文件后原子重命名，防止半写入。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(target_path: str | Path, data: bytes) -> None:
    """将字节数据原子写入目标文件（tempfile + os.replace）。"""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    target_path: str | Path,
    data: Any,
    *,
    fsync: bool = True,
    sort_keys: bool = True,
    trailing_newline: bool = False,
) -> None:
    """将 JSON 可序列化对象原子写入目标文件。

    参数
    ----
    target_path : str | Path
        目标文件路径（父目录不存在时自动创建）。
    data : Any
        可 json.dumps 的对象（dict / list / 其他 JSON 类型）。
    fsync : bool
        True（默认）：rename 前先 fsync，保证内容落盘——适用于业务状态文件
        （segments.json / voice_map.json / manifest 等）。
        False：跳过 fsync，仅保证 rename 原子性——适用于 JobStore group-commit
        快速路径（见 store.py _write_json_atomic 注释）。
    sort_keys : bool
        True（默认）：键排序，利于 diff / 调试。
        editing_segments / editing_voice_map 迁移时必须传 False，保持原有字节顺序。
    trailing_newline : bool
        False（默认）。editing_voice_map / review_actions 原实现末尾有 \\n，
        迁移时按调用点需要传 True（内容等价）。
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys)
    if trailing_newline:
        serialized += "\n"
    encoded = serialized.encode("utf-8")
    fd, tmp_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def is_valid_output(path: str | Path) -> bool:
    """检查文件是否存在且非空（用于 checkpoint 判断）"""
    p = str(path)
    return os.path.isfile(p) and os.path.getsize(p) > 0


def cleanup_tmp_files(directory: str | Path) -> int:
    """清理目录下所有 .tmp 文件，返回清理数量"""
    count = 0
    for root, _, files in os.walk(str(directory)):
        for f in files:
            if f.endswith(".tmp"):
                os.remove(os.path.join(root, f))
                count += 1
    return count
```

> **设计说明**：
> - `atomic_write_bytes` 同步重构为同目录 mkstemp，消除旧版 `path + ".tmp"` 的固定后缀（旧版在并发写同一文件时可能产生临时文件名冲突）。
> - `trailing_newline` 参数：`editing_voice_map._atomic_write_json` 和 `editing_segments._atomic_write_json` 均在 json.dump 后追加 `"\n"`；该参数使迁移后字节输出与原始实现保持等价（影响字节比较验收）。
> - `sort_keys` 默认 True，与 `store.py`/`manifest_writer.py`/`draft_writer.py` 等已有行为一致；`editing_voice_map` / `editing_segments` 原实现无 sort，迁移时**必须传 `sort_keys=False`** 以保证字节一致、避免 diff 噪声（✅ 已决策（CodeX 2026-06-25）：传 sort_keys=False，保持现有 JSON 字节顺序，不引入 key 顺序变化）。

**该步验收**：
```bash
python -c "
from pathlib import Path
import tempfile, json
from src.utils.atomic_io import atomic_write_json, atomic_write_bytes, is_valid_output, cleanup_tmp_files
# 签名检查：不抛 TypeError 即通过
import inspect
sig = inspect.signature(atomic_write_json)
assert 'fsync' in sig.parameters, 'fsync param missing'
assert 'sort_keys' in sig.parameters, 'sort_keys param missing'
assert 'trailing_newline' in sig.parameters, 'trailing_newline param missing'
print('签名检查 OK')
"

# 全量现有 atomic_io 测试仍绿
python -m pytest tests/test_atomic_io.py -q
# 期望：9 passed（原有 9 个测试；新增测试在 Step 2）
```

---

## Step 2 · 新增契约测试，锁定升级后 helper 的全部语义

**动作**：在 `tests/test_atomic_io.py` 追加覆盖升级参数的契约测试。**先补测试（本步），后迁移调用点（Step 3–7）**。

**文件**：`tests/test_atomic_io.py`

**新增测试清单（追加到文件末尾）**：

```python
import os, json, tempfile
from pathlib import Path
from src.utils.atomic_io import atomic_write_json, atomic_write_bytes, is_valid_output


def test_atomic_write_json_accepts_path_object():
    """Path 对象也能写入（以前只接受 str）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        atomic_write_json(p, {"k": 1})
        assert json.loads(p.read_text()) == {"k": 1}


def test_atomic_write_json_accepts_list():
    """list 也是合法 data（以前签名只接受 dict）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "arr.json"
        atomic_write_json(p, [1, 2, 3])
        assert json.loads(p.read_text()) == [1, 2, 3]


def test_atomic_write_json_fsync_false_still_writes():
    """fsync=False 仍然成功写入内容（跳 fsync 不影响功能）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fast.json"
        atomic_write_json(p, {"fast": True}, fsync=False)
        assert json.loads(p.read_text()) == {"fast": True}


def test_atomic_write_json_trailing_newline():
    """trailing_newline=True 时文件末尾有换行符。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nl.json"
        atomic_write_json(p, {"x": 1}, trailing_newline=True)
        raw = p.read_bytes()
        assert raw.endswith(b"\n"), f"期望末尾有 \\n，实际: {raw[-3:]!r}"


def test_atomic_write_json_no_trailing_newline_by_default():
    """默认不追加换行符。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "no_nl.json"
        atomic_write_json(p, {"x": 1})
        raw = p.read_bytes()
        # json.dumps indent=2 末尾是 "}" 不是 "\n"（sort_keys 影响顺序，不影响末尾）
        assert not raw.rstrip(b" ").endswith(b"\n"), f"期望无末尾 \\n，实际: {raw[-5:]!r}"


def test_atomic_write_json_no_tmp_residue_on_success():
    """成功写入后临时文件不残留。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "out.json"
        atomic_write_json(p, {"ok": True})
        tmps = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert tmps == [], f"残留临时文件: {tmps}"


def test_atomic_write_json_creates_nested_dirs():
    """自动创建父目录。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a" / "b" / "c.json"
        atomic_write_json(p, {"deep": True})
        assert p.exists()


def test_atomic_write_bytes_accepts_path_object():
    """atomic_write_bytes 也接受 Path 对象。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audio.wav"
        atomic_write_bytes(p, b"\x00\x01\x02")
        assert p.read_bytes() == b"\x00\x01\x02"
```

**该步验收**：
```bash
python -m pytest tests/test_atomic_io.py -q
# 期望：全部通过（原 9 + 新增 8 = 17 passed）
```

---

## Step 3 · 迁移 `editing_segments._atomic_write_json`（`src/services/jobs/editing_segments.py:172`）

**背景**：TU-01 Step 3 已给此处补了 `os.fsync`（H4 最小修），本步做完整收口——删除局部函数、改用 canonical helper。

**文件**：`src/services/jobs/editing_segments.py`

**改法**：
1. 在文件顶部 import 区补（或确认已有）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除 `def _atomic_write_json(path: Path, payload: object) -> None:` 整个函数体（约 `editing_segments.py:172–193`，实际以 grep 核对）。
3. 在文件顶部或删除处附近添加 module-level 别名（使所有现有 `_atomic_write_json(...)` 调用点零改动）：
   ```python
   def _atomic_write_json(path: Path, payload: object, *, fsync: bool = True) -> None:
       """Thin wrapper → utils.atomic_io.atomic_write_json（DRY-02 收口，TU-04）。"""
       _atomic_write_json_helper(path, payload, fsync=fsync, sort_keys=False, trailing_newline=True)
   ```
   > `trailing_newline=True`：原实现 `handle.write("\n")` 追加换行；迁移后保持字节等价。
   > `sort_keys=False`：✅ 已决策（CodeX 2026-06-25）：原实现无 sort，迁移时传 sort_keys=False，保持现有 JSON 字节顺序，避免 diff 噪声。

**该步验收**：
```bash
# 确认局部 def 已删除
grep -n "^def _atomic_write_json" src/services/jobs/editing_segments.py
# 期望：0 行（已删除）

# 确认 fsync 在 helper 内就位（通过 canonical helper 间接有效，不在此文件直出）
python -c "
import tempfile, json
from pathlib import Path
# 补丁：让 sys.path 含 src/
import sys; sys.path.insert(0, 'src')
from services.jobs.editing_segments import _atomic_write_json
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'segs.json'
    _atomic_write_json(p, [{'id': 1}])
    assert json.loads(p.read_text()) == [{'id': 1}]
print('editing_segments._atomic_write_json 迁移后写入正常')
"

# editing_segments 相关测试全绿
python -m pytest tests/test_editing_segments.py tests/test_split_many_kernel.py -q
```

---

## Step 4 · 迁移 `editing_voice_map._atomic_write_json`（`src/services/jobs/editing_voice_map.py:87`）

**文件**：`src/services/jobs/editing_voice_map.py`

**改法**：
1. 顶部 import 补（若无）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除 `def _atomic_write_json(path: Path, payload: object) -> None:` 整个函数体（约 `editing_voice_map.py:87–103`）。
3. 替换为 thin wrapper（保留原有 `trailing_newline=True`；✅ 已决策（CodeX 2026-06-25）：业务状态写入默认 fsync=True，若后续性能数据证明有问题再按调用点降级）：
   ```python
   def _atomic_write_json(path: Path, payload: object, *, fsync: bool = True) -> None:
       """Thin wrapper → utils.atomic_io.atomic_write_json（DRY-02 收口，TU-04）。"""
       _atomic_write_json_helper(path, payload, fsync=fsync, sort_keys=False, trailing_newline=True)
   ```
   > `sort_keys=False`：✅ 已决策（CodeX 2026-06-25）：原实现无 sort，传 sort_keys=False 保持现有字节顺序。

**该步验收**：
```bash
grep -n "^def _atomic_write_json" src/services/jobs/editing_voice_map.py
# 期望：0 行

python -m pytest tests/test_editing_batch_and_voice_map.py -q
# 期望：全绿
```

---

## Step 5 · 迁移 `review_actions._atomic_write_json`（`src/services/jobs/review_actions.py:1068`）

**文件**：`src/services/jobs/review_actions.py`

**改法**：
1. 顶部 import 补（若无）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除 `def _atomic_write_json(path: Path, payload: object) -> None:` 整个函数体（约 `review_actions.py:1068–1087`）。
3. 替换为 thin wrapper（`trailing_newline=True`：原实现末尾有 `"\n"`；fsync：✅ 已决策（CodeX 2026-06-25）：业务状态写入默认 fsync=True）：
   ```python
   def _atomic_write_json(path: Path, payload: object, *, fsync: bool = True) -> None:
       """Thin wrapper → utils.atomic_io.atomic_write_json（DRY-02 收口，TU-04）。"""
       _atomic_write_json_helper(path, payload, fsync=fsync, trailing_newline=True)
   ```

**该步验收**：
```bash
grep -n "^def _atomic_write_json" src/services/jobs/review_actions.py
# 期望：0 行

python -m pytest tests/ -q -k "review" -p no:cacheprovider
# 期望：全绿（含 test_transcript_reviewer.py 等）
```

---

## Step 6 · 迁移 `JobStore._write_json_atomic`（`src/services/jobs/store.py:439`）

**背景**：此处已有 `fsync: bool = True/False` 两条路径（`fsync=False` 是 group-commit 快速路径，注释有完整解释），canonical helper 的 `fsync` 参数与其语义完全对应。

**文件**：`src/services/jobs/store.py`

**改法**：
1. 顶部 import 补（若无）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除 `@staticmethod def _write_json_atomic(...)` 整个函数体（约 `store.py:438–481`）。
3. 在 `JobStore` 类内替换为：
   ```python
   @staticmethod
   def _write_json_atomic(
       output_path: Path,
       payload: dict[str, object],
       *,
       fsync: bool = True,
   ) -> None:
       """Atomic temp + rename JSON write.

       ``fsync=True``（默认）：内容落盘后 rename，防断电后截断。
       ``fsync=False``（group-commit 快速路径）：跳 fsync，仅保证 rename 原子性。

       DRY-02 收口（TU-04）：委托 utils.atomic_io.atomic_write_json 实现。
       """
       _atomic_write_json_helper(output_path, payload, fsync=fsync, sort_keys=True)
   ```
   > `sort_keys=True`：原实现已有 `sort_keys=True`，保持一致。

**该步验收**：
```bash
grep -n "_write_json_atomic" src/services/jobs/store.py | head -5
# 期望：薄 wrapper 定义 1 行 + 若干调用点（无完整实现体）

# store 相关测试（包括 group-commit）
python -m pytest tests/test_job_api_phase1.py tests/test_job_model_snapshot.py -q
# 期望：全绿
```

---

## Step 7 · 迁移 `manifest_writer._write_json_atomic`（`src/modules/output/manifest_writer.py:149`）

**文件**：`src/modules/output/manifest_writer.py`

**改法**：
1. 顶部 import 补（若无）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除 `@staticmethod def _write_json_atomic(...)` 整个函数体（约 `manifest_writer.py:149–168`）。
3. 替换为：
   ```python
   @staticmethod
   def _write_json_atomic(output_path: Path, payload: dict[str, object]) -> None:
       """DRY-02 收口（TU-04）：委托 utils.atomic_io.atomic_write_json 实现。"""
       _atomic_write_json_helper(output_path, payload, sort_keys=True)
   ```
   > 原实现有 `sort_keys=True` + fsync，canonical helper 默认 `fsync=True`，行为等价。

**该步验收**：
```bash
grep -n "def _write_json_atomic" src/modules/output/manifest_writer.py
# 期望：1 行（薄 wrapper，无完整实现体）

python -m pytest tests/ -q -k "manifest or output" -p no:cacheprovider
# 期望：全绿
```

---

## Step 8 · 迁移 `draft_writer._write_json_atomic`（`src/modules/draft/draft_writer.py:250`）

**特殊性**：该方法在 `except OSError` 时抛 `DraftError`（业务异常包装），**不能丢失**。canonical helper 会把 `OSError` 原样 re-raise；`DraftError` 包装必须保留在 wrapper 层。

**文件**：`src/modules/draft/draft_writer.py`

**改法**：
1. 顶部 import 补（若无）：
   ```python
   from utils.atomic_io import atomic_write_json as _atomic_write_json_helper
   ```
2. 删除原有 `def _write_json_atomic(self, output_path: Path, payload: dict[str, object]) -> None:` 整个函数体（约 `draft_writer.py:250–271`）。
3. 替换为：
   ```python
   def _write_json_atomic(self, output_path: Path, payload: dict[str, object]) -> None:
       """DRY-02 收口（TU-04）：委托 utils.atomic_io.atomic_write_json，
       保留 DraftError 包装（调用方依赖此异常类型）。"""
       try:
           _atomic_write_json_helper(output_path, payload, sort_keys=True)
       except OSError as exc:
           raise DraftError(f"Failed to write draft JSON: {output_path}") from exc
   ```
   > 原实现同样是 `except OSError → raise DraftError`；`finally` 里的 tmp 清理由 canonical helper 内部处理，无需保留。

**该步验收**：
```bash
grep -n "def _write_json_atomic" src/modules/draft/draft_writer.py
# 期望：1 行（wrapper，含 DraftError 包装）

grep -n "DraftError" src/modules/draft/draft_writer.py
# 期望：wrapper 中仍有 DraftError（≥1 行）

python -m pytest tests/ -q -k "draft" -p no:cacheprovider
# 期望：全绿
```

---

## Step 9 · 在 sidecar / backfill 不迁移处补注释，新增整体收口验收

**文件**：`src/services/smart/sidecar_emitter.py:208`、`gateway/cost_summary_backfill.py:70`

**改法**（仅 docstring 补充，不改实现）：

`sidecar_emitter.py` 的 `_atomic_write_json` docstring 末尾追加：
```
注：同见 utils.atomic_io.atomic_write_json（canonical helper，TU-04）。
本函数保留独立实现：持有 file_lock() 并发安全语义，合并会改调用方行为。
```

`gateway/cost_summary_backfill.py` 的 `_atomic_write_json` docstring 末尾追加：
```
注：同见 src/utils/atomic_io.atomic_write_json（canonical helper，TU-04）。
本函数保留独立实现：gateway 进程不 import src/ pipeline 模块（import 边界约束）。
```

**该步验收（整体收口）**：
```bash
# 确认 main tree（排除 worktrees）内独立实现数量已降至 2（sidecar + backfill）
grep -rn "^def _atomic_write_json\|^    def _write_json_atomic\|^    @staticmethod" \
  src/ gateway/ \
  --include="*.py" \
  | grep -v ".codex_worktrees" \
  | grep -E "_atomic_write_json|_write_json_atomic" \
  | grep -v "def.*wrapper\|#\|TU-04"
# 期望：仅剩 sidecar_emitter.py 和 cost_summary_backfill.py 两处完整实现
# （store/manifest/draft/editing_* 只剩 thin wrapper 定义行）

# 全量回归
python -m pytest tests/test_atomic_io.py \
  tests/test_editing_segments.py \
  tests/test_split_many_kernel.py \
  tests/test_editing_batch_and_voice_map.py \
  tests/test_editing_commit.py \
  tests/test_copy_service.py \
  tests/test_job_api_phase1.py \
  tests/test_voice_registry.py \
  tests/test_legacy_cleanup_guards.py \
  -q
# 期望：全绿
```

---

## 测试计划（新增 / 回归）

### 新增测试

- `tests/test_atomic_io.py`（Step 2 追加）：8 个契约测试，覆盖 `Path` 入参、`list` data、`fsync=False`、`trailing_newline`、无残留 tmp、自动建目录、`atomic_write_bytes` Path 入参。

### 回归测试

| 步骤 | 验证命令 | 期望结果 |
|---|---|---|
| Step 1–2 | `pytest tests/test_atomic_io.py -q` | 17 passed |
| Step 3 | `pytest tests/test_editing_segments.py tests/test_split_many_kernel.py -q` | 全绿 |
| Step 4 | `pytest tests/test_editing_batch_and_voice_map.py -q` | 全绿 |
| Step 5 | `pytest tests/ -q -k "review"` | 全绿 |
| Step 6 | `pytest tests/test_job_api_phase1.py tests/test_job_model_snapshot.py -q` | 全绿 |
| Step 7 | `pytest tests/ -q -k "manifest or output"` | 全绿 |
| Step 8 | `pytest tests/ -q -k "draft"` | 全绿 |
| 整体 | `pytest tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py -q` | 全绿（守卫不退） |

---

## 回滚方案

9 步各自独立 commit（显式 pathspec）：

| 步骤 | 改动文件 | commit 说明 |
|---|---|---|
| Step 1 | `src/utils/atomic_io.py` | `refactor: upgrade atomic_write_json helper — Path/Any/fsync/trailing_newline` |
| Step 2 | `tests/test_atomic_io.py` | `test: add contract tests for upgraded atomic_write_json` |
| Step 3 | `src/services/jobs/editing_segments.py` | `refactor: editing_segments._atomic_write_json → atomic_io helper (DRY-02)` |
| Step 4 | `src/services/jobs/editing_voice_map.py` | `refactor: editing_voice_map._atomic_write_json → atomic_io helper (DRY-02)` |
| Step 5 | `src/services/jobs/review_actions.py` | `refactor: review_actions._atomic_write_json → atomic_io helper (DRY-02)` |
| Step 6 | `src/services/jobs/store.py` | `refactor: JobStore._write_json_atomic → atomic_io helper (DRY-02)` |
| Step 7 | `src/modules/output/manifest_writer.py` | `refactor: manifest_writer._write_json_atomic → atomic_io helper (DRY-02)` |
| Step 8 | `src/modules/draft/draft_writer.py` | `refactor: draft_writer._write_json_atomic → atomic_io helper (DRY-02)` |
| Step 9 | `src/services/smart/sidecar_emitter.py`, `gateway/cost_summary_backfill.py` | `docs: annotate non-migrated atomic write fns with canonical reference (DRY-02)` |

任一步出问题：`git revert <该步 commit>` 即回滚，不影响其余步骤。

---

## 完成定义（DoD）

- [ ] **canonical helper 升级**：`src/utils/atomic_io.py` 的 `atomic_write_json` 支持 `str | Path`、任意 JSON-serializable data、`fsync` 参数、`sort_keys`、`trailing_newline`；`atomic_write_bytes` 同样支持 `str | Path`。
- [ ] **契约测试**：`tests/test_atomic_io.py` 有 ≥17 个 passed，覆盖 Step 2 全部 8 个新场景。
- [ ] **editing_segments**：局部 `_atomic_write_json` 函数体已删，thin wrapper 调用 canonical helper，`tests/test_editing_segments.py` 全绿。
- [ ] **editing_voice_map**：局部 `_atomic_write_json` 函数体已删，thin wrapper 调用 canonical helper，`tests/test_editing_batch_and_voice_map.py` 全绿。
- [ ] **review_actions**：局部 `_atomic_write_json` 函数体已删，thin wrapper 调用 canonical helper，review 相关测试全绿。
- [ ] **JobStore._write_json_atomic**：完整实现体已删，thin wrapper 保留 `fsync=False` 分支语义，`tests/test_job_api_phase1.py` 全绿。
- [ ] **manifest_writer._write_json_atomic**：完整实现体已删，thin wrapper 保留 `sort_keys=True`，manifest 相关测试全绿。
- [ ] **draft_writer._write_json_atomic**：完整实现体已删，thin wrapper 保留 `DraftError` 包装，draft 相关测试全绿。
- [ ] **注释标注**：`sidecar_emitter._atomic_write_json` 和 `cost_summary_backfill._atomic_write_json` 的 docstring 均注明"同见 canonical helper + 保留原因"。
- [ ] **量化收口指标**：`grep -rn "def _atomic_write_json\|def _write_json_atomic" src/ gateway/ --include="*.py" | grep -v ".codex_worktrees" | grep -v "wrapper"` 输出行数 ≤2（仅 sidecar + backfill）。
- [ ] **守卫测试全绿**：`pytest tests/test_legacy_cleanup_guards.py tests/test_phase1_guards.py -q` 通过，无守卫退步。
- [x] **✅ 已决策（CodeX 2026-06-25）** `sort_keys=False`（editing_segments / editing_voice_map）：这两处迁移时**必须传 `sort_keys=False`**，保持现有 JSON 字节顺序，不引入 key 排序变化（Step 3/4 wrapper 已按此落地）。
- [x] **✅ 已决策（CodeX 2026-06-25）** `editing_voice_map` / `review_actions` fsync：业务状态写入默认 `fsync=True` 已定；若后续性能数据证明有问题再按调用点降级；目录 fsync 为后续增强、本单元不引入。
- [ ] **sort_keys 约束**：`editing_segments` / `editing_voice_map` 的 wrapper 显式传 `sort_keys=False`；字节比较验收（diff before/after）输出仅有 `_atomic_write_json` 定义行差异，无 key 顺序变化行。
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`。

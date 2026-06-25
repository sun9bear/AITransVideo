# TU-06 · 统一 coerce/normalize + error payload

- **目标 / 价值**：消灭跨模块重复的微型工具函数（`_normalize_optional_text` 出现 18 处、`_coerce_int`/`_coerce_bool` 出现 12+ 处、`_write_json`+`_to_jsonable` 各有 2 份独立副本、`_AGE_*`+`_resolve_age_bucket` 在两个 TTS 文件里各自定义），并为 API 错误响应建立可测试的结构化载荷标准；减少认知负荷、让未来的维护只改一处。
- **关联发现**：DRY-03 (`_write_json/_to_jsonable` 重复)、DRY-04 (`_AGE_*` 常量与 `_resolve_age_bucket` 重复)、DRY-06 (三个 selector 的 rerank 结果提取样板)、错误载荷标准。
- **前置依赖**：无（可与 Wave B 其他单元并行）。
- **建议分支**：`quality/shared-helpers`
- **预估工时**：M（约 2–3 天；Step 1–3 日均可独立合并，Step 4–5 跟进）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令：`grep -n`→`Select-String -n`、`test -f`→`Test-Path`、`wc -l`→`(Get-Content <f>).Count`、避免 `<(...)` 进程替换。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **错误响应双写，不硬替换字段名**：Gateway `_error_response` 的 JSON body 必须同时保留旧 `error` 字段与新 `error_code` 字段，至少一个版本周期后才可弃用旧字段；Step 5 的 gateway diff 已按双写形态修订。
- **`src/utils/error_payload.py` 不被 gateway import**：gateway 保留自己的本地 `_error_response` Adapter；两套只做形状对齐，不合并 import 路径（与原不变量 3 一致，已在 Step 5 明确）。
- **`select_voice` 先保留兼容 shim**：Step 3 中 `cosyvoice_voice_selector.py` 的 legacy `select_voice` 函数不立刻删除，确认无外部直接调用后再收口；执行时须先跑 grep 核查调用者。
- **新增 `retryable` / `user_action` 字段向后兼容**：Gateway `_error_response` 新增两个可选参数（默认值保持旧行为），现有调用方不改也正常。
- **`ErrorPayload.to_dict()` 作为测试断言基准**：`src/utils/error_payload.py` 是后端 schema 文档和测试断言的单一来源，仅供 Job API 侧与测试使用；gateway 侧对齐 shape 但不 import 该模块。

---

## 不在本单元范围（out-of-scope）

- **全量替换**所有 18 处 `_normalize_optional_text` 调用点——本单元只建 helper + 迁移示范调用点（各 3 处），全量替换另排 Wave C 单独 PR 或保留内联副本（副本数量不增长即达标）。
- `_write_json_atomic`（`draft_writer.py`、`manifest_writer.py`、`config_loader.py`、`jobs/store.py`）——使用了 atomic write 语义，与非原子版 `_write_json` 不同，属 TU-04 范围。
- `process.py` 的 Option B 重构——属 TU-14，本单元不触碰 `process.py`。
- 前端 error payload 消费侧适配——Gateway JSON 格式已有约定（`job_intercept.py:488`），本单元只标准化后端生产侧。
- TTS 音色选择逻辑行为变更——DRY-06 只收口"结果提取"样板，不改评分算法。

---

## 必守不变量

以下红线在本单元各步均适用；任何 Step 引入风险时须回头核验：

1. **付费 API 红线**：MiniMax 付费克隆 / 付费 TTS / LLM 推理 / ASR 转录，绝不在 `except`/`fallback`/`retry`/`batch` 中自动触发。`_write_json`、`coerce_*`、`normalize_optional_text` 均为纯 CPU 工具，不涉及外部调用——迁移时确认替换后的调用点没有悄悄添加外部 IO。
2. **测试不接真实外部服务**：新建的 `tests/test_shared_helpers.py` 严禁 import 或 mock 任何付费 provider；helper 测试应全部是纯 Python 单元测试，无网络 / 无文件系统副作用（`tmp_path` fixture 例外）。
3. **Gateway 不 import `services.*` 重型模块**：`src/utils/` 下的新 helper 文件若被 gateway 侧引用，须确认不会传染拉入 pydub / `services.jobs` / `services.tts` 等重型包（参见 `gateway/storage/event_log.py` 的分离先例）。本单元的 `error_payload` helper 若放在 `src/utils/`，gateway 侧**不 import** 它，gateway 保留自己的 `_error_response`（`gateway/job_intercept.py:488`）；两套只是形状对齐，不合并 import 路径。
4. **`_resolve_age_bucket` 行为不变**：DRY-04 迁移后函数签名、返回值集合（`"young"` / `"middle"` / `"elderly"` / `""`）、别名集合必须与 `voice_reranker.py:56` 的 `resolve_age_bucket` 完全一致；现有 `tests/test_voice_reranker.py` 中的 `TestResolveAgeBucket` 测试组在迁移前后均须绿。
5. **DRY-06 样板只收口结果提取，不改 scoring 逻辑**：三个 selector 的 `scored = combined_rerank(...)` 调用之后的 `scored[0][0]`、`scored[0][1]`、`scored[1:6]` 模式可抽为 helper，但 `combined_rerank` 本身不动，`load_profiles` 调用不合并。

---

## Step 0 · 确认现状

```bash
# 0-a. 建分支
git switch -c quality/shared-helpers

# 0-b. 核对 DRY-03：_write_json / _to_jsonable 在 src 下的位置
grep -rn "^def _write_json\|^def _to_jsonable" src/
# 预期：
#   src/services/assemblyai/transcriber.py:828  _write_json
#   src/services/assemblyai/transcriber.py:836  _to_jsonable
#   src/services/gemini/translator.py:2683       _write_json
#   src/services/gemini/translator.py:2713       _to_jsonable
# （jobs/api.py 的 _write_json 是方法不是函数，不在本单元范围）

# 0-c. 核对 DRY-04：_AGE_* 常量 + 两个 age 解析函数位置
grep -n "_AGE_ELDERLY\|_AGE_YOUNG\|_AGE_MIDDLE" src/services/tts/cosyvoice_voice_selector.py | head -5
grep -n "^def _resolve_age_bucket\|^def resolve_age_bucket" \
    src/services/tts/cosyvoice_voice_selector.py \
    src/services/tts/voice_reranker.py
# 预期：
#   cosyvoice_voice_selector.py:110-112  _AGE_* sets (plain set，非 frozenset)
#   cosyvoice_voice_selector.py:199      _resolve_age_bucket
#   voice_reranker.py:51-53              _AGE_* frozensets（Final[frozenset]）
#   voice_reranker.py:56                 resolve_age_bucket（已有公开名）

# 0-d. 核对 DRY-06：三个 selector 结果提取样板
grep -n "scored\[0\]\[0\]\|scored\[0\]\[1\]\|scored\[1:6\]" \
    src/services/tts/volcengine_voice_selector.py \
    src/services/tts/minimax_voice_selector.py \
    src/services/tts/cosyvoice_voice_selector.py
# 预期：三个文件各有一处 scored[0][0], scored[0][1], scored[1:6]

# 0-e. 统计 _normalize_optional_text 当前副本数（建立基线）
grep -rn "^def _normalize_optional_text" src/ | wc -l
# 预期：18（或稍有出入——记录实际数字，作为 DoD 对比基准）

# 0-f. 核对 _coerce_* 在 src 下位置
grep -rn "^def _coerce_bool\|^def _coerce_int\|^def _coerce_optional_int" src/ | grep -v "\.codex_worktrees"
# 预期覆盖：assemblyai/transcriber.py, gemini/translator.py, pipeline/process.py,
#            services/llm/router.py, services/tts/tts_generator.py, services/usage_meter.py 等

# 0-g. 确认 src/utils/__init__.py 存在（已有目录，内容为空单行）
test -f src/utils/__init__.py && echo "EXISTS" || echo "MISSING"

# 0-h. 确认 gateway error_response 当前位置（只看，不改）
grep -n "def _error_response" gateway/job_intercept.py
# 预期：gateway/job_intercept.py:488
```

> ⚠️ 若任何 `grep` 输出行号与上方预期不符，以实际行号为准更新后续步骤；若文件位置整体移动，在该步注明差异后继续。

---

## Step 1 · 建 `src/utils/coerce.py`（coerce/normalize helpers）

**动作**：新建文件 `src/utils/coerce.py`，把跨文件重复的 coerce / normalize 微工具收口到单一位置，附完整 docstring + 类型注解。

**改法（新文件内容骨架）**：

```python
# src/utils/coerce.py
"""Shared type-coercion and text-normalization helpers.

All functions are pure (no I/O, no external calls).
"""
from __future__ import annotations

__all__ = [
    "normalize_optional_text",
    "coerce_bool",
    "coerce_int",
    "coerce_optional_int",
]


def normalize_optional_text(value: object) -> str | None:
    """Strip *value* to str; return ``None`` if empty/None."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def coerce_bool(value: object, *, default: bool) -> bool:
    """Coerce a loose value to bool.

    Truthy strings: ``"1"``, ``"true"``, ``"yes"``, ``"on"``.
    Falsy  strings: ``"0"``, ``"false"``, ``"no"``, ``"off"``.
    Anything else returns *default*.
    """
    if isinstance(value, bool):
        return value
    normalized = normalize_optional_text(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def coerce_optional_int(value: object) -> int | None:
    """Return ``int(value)`` or ``None`` on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def coerce_int(value: object, *, default: int) -> int:
    """Return ``int(value)`` or *default* on failure."""
    coerced = coerce_optional_int(value)
    return default if coerced is None else coerced
```

**示范迁移（3 处调用点）**：

- `src/services/assemblyai/transcriber.py:852–884`：删除本地 `_normalize_optional_text`、`_coerce_bool`、`_coerce_optional_int`、`_coerce_int`，改为：
  ```python
  from utils.coerce import normalize_optional_text, coerce_bool, coerce_int, coerce_optional_int
  ```
  并将所有内部调用名替换（`_normalize_optional_text` → `normalize_optional_text`，等）。
- `src/services/tts/tts_generator.py`（`_normalize_optional_text:1832`、`_coerce_int:1852`）：同法替换。
- `src/services/llm/router.py`（`_normalize_optional_text:402`、`_coerce_int:416`）：同法替换。

> 注意：`src/services/gemini/translator.py:2729–2752` 同类副本，Step 2 中随 `_to_jsonable` 一起迁移，避免改同一文件两次。

**该步验收**：

```bash
# 文件存在且非空
test -f src/utils/coerce.py && echo "OK" || echo "MISSING"

# 新文件通过 Python 语法检查
python -m py_compile src/utils/coerce.py && echo "SYNTAX_OK"

# 示范迁移的三个文件不再含本地定义
grep -n "^def _normalize_optional_text\|^def _coerce_bool\|^def _coerce_int\|^def _coerce_optional_int" \
    src/services/assemblyai/transcriber.py \
    src/services/tts/tts_generator.py \
    src/services/llm/router.py
# 预期：无输出

# 示范迁移文件正确 import 新 helper
grep -n "from utils.coerce import" \
    src/services/assemblyai/transcriber.py \
    src/services/tts/tts_generator.py \
    src/services/llm/router.py
# 预期：每个文件各有 1 行 import

# 回归：现有 assemblyai + tts_generator 相关测试通过（smoke）
python -m pytest tests/ -q -x \
    -k "transcriber or tts_generator or voice_reranker" \
    --no-header 2>&1 | tail -5
# 预期：collected N items ... passed（无 FAILED / ERROR）
```

---

## Step 2 · 建 `src/utils/json_helpers.py`（DRY-03 `_write_json`+`_to_jsonable`）

**动作**：新建 `src/utils/json_helpers.py`，收口两处独立的 `_write_json` + `_to_jsonable` 副本（`assemblyai/transcriber.py:828–849` 和 `gemini/translator.py:2683–2726`）。

**确认函数体一致性**（执行前先 diff）：

```bash
# 提取两个文件的函数体，肉眼或 diff 核对
sed -n '828,849p' src/services/assemblyai/transcriber.py
sed -n '2683,2726p' src/services/gemini/translator.py
# 两者语义一致（均处理 Path / dataclass / dict / list/tuple/set / __dict__），
# 唯一差异是 translator.py 的 _to_jsonable 在 836–849 行之间与 transcriber 完全相同。
# 若执行时发现差异，合并时取行为更完整的版本，并在 commit message 注明。
```

**改法（新文件内容骨架）**：

```python
# src/utils/json_helpers.py
"""Shared JSON serialization helpers (non-atomic write).

For *atomic* writes (used in draft_writer, manifest_writer, config_loader,
jobs/store) see src/utils/atomic_io.py — those are NOT in scope here.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

__all__ = ["to_jsonable", "write_json"]


def to_jsonable(value: Any) -> Any:
    """Recursively convert *value* to a JSON-serializable type."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {k: to_jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    """Write *payload* as pretty-printed UTF-8 JSON to *path* (non-atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

**示范迁移（2 个调用点）**：

- `src/services/assemblyai/transcriber.py:828–849`：删除本地 `_write_json`、`_to_jsonable`；添加：
  ```python
  from utils.json_helpers import to_jsonable, write_json as _write_json  # noqa: F401 — keeps call-site names
  ```
  若调用点使用 `_write_json(path, payload)` 形式不变，alias import 可维持原名。
- `src/services/gemini/translator.py:2683–2726`：同法，删除本地两个函数，替换 import。

**该步验收**：

```bash
# 文件存在
test -f src/utils/json_helpers.py && echo "OK"

# 语法检查
python -m py_compile src/utils/json_helpers.py && echo "SYNTAX_OK"

# 两个示范文件不再含本地定义
grep -n "^def _write_json\|^def _to_jsonable" \
    src/services/assemblyai/transcriber.py \
    src/services/gemini/translator.py
# 预期：无输出

# 两个文件含 from utils.json_helpers import
grep -n "from utils.json_helpers import" \
    src/services/assemblyai/transcriber.py \
    src/services/gemini/translator.py
# 预期：每个文件各 1 行

# 回归：assemblyai + translator 相关测试通过
python -m pytest tests/ -q -x \
    -k "transcriber or translator or assemblyai" \
    --no-header 2>&1 | tail -5
# 预期：passed（无 FAILED / ERROR）
```

---

## Step 3 · 收口 DRY-04：`_AGE_*`+`_resolve_age_bucket` 去重

**动作**：`src/services/tts/cosyvoice_voice_selector.py` 里有自己的 `_AGE_*` 常量（`plain set`，L110–112）和 `_resolve_age_bucket`（L199–207），与 `voice_reranker.py:51–65` 的 `resolve_age_bucket`（已有公开名）逻辑完全等价。策略：cosyvoice selector 直接 import 并复用 `voice_reranker.resolve_age_bucket`，删除本地重复定义。

**确认差异**：

```bash
# cosyvoice_voice_selector.py 中 _AGE_* 是 plain set（可变），voice_reranker.py 是 frozenset；
# 两者成员相同。本步统一到 frozenset（复用 voice_reranker 的定义）。
grep -n "_AGE_ELDERLY\|_AGE_YOUNG\|_AGE_MIDDLE" \
    src/services/tts/cosyvoice_voice_selector.py
# L110: _AGE_ELDERLY = {"elderly", "old", "senior"}
# L111: _AGE_YOUNG = {"young", "youth"}
# L112: _AGE_MIDDLE = {"middle", "adult", "mature"}
grep -n "_AGE_ELDERLY\|_AGE_YOUNG\|_AGE_MIDDLE" \
    src/services/tts/voice_reranker.py
# L51: _AGE_ELDERLY: Final[frozenset[str]] = frozenset({"elderly", "old", "senior"})
# L52: _AGE_YOUNG:   Final[frozenset[str]] = frozenset({"young", "youth"})
# L53: _AGE_MIDDLE:  Final[frozenset[str]] = frozenset({"middle", "adult", "mature"})
```

**改法**：

1. 在 `src/services/tts/cosyvoice_voice_selector.py` 头部，已有的 import 块（`from services.tts.voice_reranker import ...`，在 `select_cosyvoice_voice_match` 函数内 lazy import，L463）改为文件级 import：
   ```python
   # cosyvoice_voice_selector.py 顶部（文件已有 from __future__ import annotations 等）
   from services.tts.voice_reranker import resolve_age_bucket
   ```
   注意：`select_cosyvoice_voice_match` 内部的 lazy import（L463）仍引入 `combined_rerank`、`load_profiles`、`score_to_confidence`，只需把 `resolve_age_bucket` 从 lazy import 提到文件级即可（或直接加到 L463 的 lazy import 列表亦可，选其一保持一致）。

2. 删除 L110–112 的 `_AGE_*` 常量定义。

3. 在 `select_voice`（legacy 函数，L115 起）和 `_resolve_age_bucket`（L199）中，将对 `_AGE_*` 的引用替换为对 `voice_reranker` 模块导出常量的引用，或直接调用 `resolve_age_bucket(age_group)`：
   - `select_voice` 内（L138–145）的 age bucket 解析逻辑替换为 `age_bucket = resolve_age_bucket(age_group)`。
   - 删除本地 `_resolve_age_bucket` 函数（L199–207）；所有对 `_resolve_age_bucket` 的调用点改为 `resolve_age_bucket`。

✅ **已决策（CodeX 2026-06-25）**：`select_voice` legacy 函数先保留为兼容 shim，不在本次 PR 中删除。执行时须先跑下方 grep 核查有无外部直接调用者；确认无调用后再收口（另排后续 PR）。此步仅完成 `_resolve_age_bucket` / `_AGE_*` 的去重，`select_voice` 函数体保持不变。

```bash
# 执行时前置动作（已定方向）：确认 select_voice 调用者范围后再决定收口时机
grep -rn "select_voice\b" src/ gateway/ | grep -v "cosyvoice_voice_selector\|select_voice_match\|select_volcengine\|select_minimax"
```

**该步验收**：

```bash
# cosyvoice_voice_selector.py 不再含本地 _AGE_* 常量定义
grep -n "^_AGE_ELDERLY\|^_AGE_YOUNG\|^_AGE_MIDDLE" \
    src/services/tts/cosyvoice_voice_selector.py
# 预期：无输出

# cosyvoice_voice_selector.py 不再含本地 _resolve_age_bucket 定义
grep -n "^def _resolve_age_bucket" src/services/tts/cosyvoice_voice_selector.py
# 预期：无输出

# cosyvoice_voice_selector.py 引用了 resolve_age_bucket
grep -n "resolve_age_bucket" src/services/tts/cosyvoice_voice_selector.py
# 预期：至少 1 行（import + 调用）

# 回归：voice_reranker 和 cosyvoice_voice_selector 相关测试全通
python -m pytest tests/ -q -x \
    -k "voice_reranker or cosyvoice" \
    --no-header 2>&1 | tail -5
# 预期：passed（无 FAILED / ERROR）
# 特别检查：TestResolveAgeBucket 必须全绿
python -m pytest tests/test_voice_reranker.py::TestResolveAgeBucket -v \
    --no-header 2>&1 | tail -10
# 预期：5 passed
```

---

## Step 4 · 抽取 DRY-06：rerank 结果提取辅助函数

**动作**：三个 selector 在调用 `combined_rerank(...)` 后的结果提取模式完全一致：

```python
best_vid = scored[0][0]
best_score = scored[0][1]
remaining = tuple(vid for vid, _ in scored[1:6])
confidence = score_to_confidence(best_score)
```

对应位置：
- `volcengine_voice_selector.py:115–118`
- `minimax_voice_selector.py:209–212`
- `cosyvoice_voice_selector.py:553–556`

策略：在 `src/services/tts/voice_reranker.py` 中新增公开辅助函数 `unpack_rerank_result`（避免建第四个小文件），使 voice_reranker 成为 rerank 结果解包的单一来源。

**改法**：

在 `voice_reranker.py` 的 `score_to_confidence` 函数之后（约 L345 后），添加：

```python
def unpack_rerank_result(
    scored: list[tuple[str, float]],
    *,
    backup_limit: int = 5,
) -> tuple[str, float, tuple[str, ...], str]:
    """Extract best voice, score, backup pool, and confidence from rerank output.

    Returns ``(best_voice_id, best_score, backup_voices, confidence)``.
    ``backup_voices`` contains up to *backup_limit* runner-up voice IDs.
    """
    best_vid = scored[0][0]
    best_score = scored[0][1]
    remaining: tuple[str, ...] = tuple(vid for vid, _ in scored[1 : 1 + backup_limit])
    confidence = score_to_confidence(best_score)
    return best_vid, best_score, remaining, confidence
```

然后在三个 selector 中替换各自的 4 行模式为：

```python
from services.tts.voice_reranker import (
    combined_rerank,
    load_profiles,
    resolve_age_bucket,
    score_to_confidence,
    unpack_rerank_result,   # 新增
)
# ...
best_vid, best_score, remaining, confidence = unpack_rerank_result(scored)
```

**该步验收**：

```bash
# voice_reranker.py 含新函数
grep -n "^def unpack_rerank_result" src/services/tts/voice_reranker.py
# 预期：1 行

# 三个 selector 不再含手写的 scored[0][0] 提取模式
grep -n "scored\[0\]\[0\]\|scored\[0\]\[1\]\|scored\[1:6\]" \
    src/services/tts/volcengine_voice_selector.py \
    src/services/tts/minimax_voice_selector.py \
    src/services/tts/cosyvoice_voice_selector.py
# 预期：无输出（已替换为 unpack_rerank_result 调用）

# 三个 selector 均 import unpack_rerank_result
grep -n "unpack_rerank_result" \
    src/services/tts/volcengine_voice_selector.py \
    src/services/tts/minimax_voice_selector.py \
    src/services/tts/cosyvoice_voice_selector.py
# 预期：每个文件各有 import + 调用行

# 回归：voice match 相关全量测试通过
python -m pytest tests/ -q -x \
    -k "voice_reranker or volcengine or minimax or cosyvoice" \
    --no-header 2>&1 | tail -5
# 预期：passed（无 FAILED / ERROR）
```

---

## Step 5 · 定义统一 API error payload 标准 + 示范对齐

**动作**：为后端 HTTP 错误响应定义统一载荷形状，建立可测试的 schema 文档 + 轻量 Python dataclass，并让现有 Gateway `_error_response`（`gateway/job_intercept.py:488`）的行为与标准对齐（**只补缺失字段，不改调用接口**）。

**标准形状定义**（新建 `src/utils/error_payload.py`）：

```python
# src/utils/error_payload.py
"""Canonical API error payload shape for backend HTTP responses.

Gateway side (gateway/job_intercept.py) produces JSON from _error_response().
Job API side (src/services/jobs/api.py) uses similar patterns.
Both MUST match this schema — this module is the single source of truth
for documentation and test assertions.

NOTE: Gateway must NOT import this module to avoid pulling in src/services
into the gateway Python process.  Gateway keeps its own _error_response()
helper; this module defines the contract shape for testing and documentation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ErrorPayload", "RETRYABLE_CODES", "NON_RETRYABLE_CODES"]


@dataclass(frozen=True)
class ErrorPayload:
    """Canonical error response shape.

    Fields
    ------
    error_code : str
        Stable machine-readable English identifier (snake_case).
        Frontend and tests key on this — must never change once shipped.
        Examples: ``"job_not_found"``, ``"credit_insufficient"``,
        ``"voice_clone_consent_required"``.
    message : str
        Human-readable Chinese description shown in the UI.
    detail : dict[str, Any]
        Optional structured diagnostic (no secrets, no PII).
        Default: empty dict.
    retryable : bool
        Whether the client should offer a retry action.
    user_action : str
        Suggested next step for the user (Chinese, one sentence).
        Empty string means no action needed.
    """

    error_code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    user_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
            "user_action": self.user_action,
        }


# Well-known stable error codes (extend as needed; NEVER rename existing ones)
RETRYABLE_CODES: frozenset[str] = frozenset({
    "db_write_failed",
    "upstream_timeout",
    "worker_unavailable",
})

NON_RETRYABLE_CODES: frozenset[str] = frozenset({
    "job_not_found",
    "job_not_owned",
    "credit_insufficient",
    "voice_clone_consent_required",
    "plan_upgrade_required",
    "invalid_request",
})
```

**Gateway `_error_response` 对齐（不改 gateway 函数签名，仅补字段）**：

当前 `gateway/job_intercept.py:488–502` 的 `body` 只含 `{"error": error_code, "message": message}` + 可选 `detail`；缺少 `retryable` 和 `user_action`。本步**只在已有调用 `_error_response` 的地方逐步补 `retryable` keyword arg**——gateway 函数签名新增可选参数 `retryable: bool = False`、`user_action: str = ""`，调用方不改也仍然正常（默认值向后兼容）。

✅ **已决策（CodeX 2026-06-25）：双写过渡，不硬替换字段名。** 旧 `error` 字段必须保留，同时新增 `error_code`；至少一个版本周期后再考虑弃用旧字段。**不得**把 `"error"` 改写为 `"error_code"` 后就删掉 `"error"`——这会造成兼容性断裂。

具体改法（`gateway/job_intercept.py:488–502`）：

```python
def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    detail: dict | None = None,
    retryable: bool = False,          # 新增，默认向后兼容
    user_action: str = "",            # 新增，默认向后兼容
) -> Response:
    """Return a JSON error with structured error_code for frontend consumption.

    Dual-write transition: both 'error' (legacy) and 'error_code' (new) are
    emitted until the frontend has fully migrated to 'error_code'.
    """
    body: dict = {
        "error": error_code,          # 旧字段保留（兼容现有前端消费）
        "error_code": error_code,     # 新字段新增（标准 shape，与 ErrorPayload 对齐）
        "message": message,
        "retryable": retryable,
        "user_action": user_action,
    }
    if detail:
        body["detail"] = detail
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        headers={"content-type": "application/json"},
    )
```

执行时前置动作（已定方向）：可用下方 grep 核查前端当前消费哪个字段，以便评估旧字段弃用时机（不影响本步执行）：

```bash
grep -rn '"error"\|\.error\b' frontend-next/src/ | grep -v "console\|// \|error_code" | head -20
```

**该步验收**：

```bash
# src/utils/error_payload.py 存在且语法正常
python -m py_compile src/utils/error_payload.py && echo "SYNTAX_OK"

# ErrorPayload dataclass 可实例化且 to_dict() 含全部 5 个字段
python -c "
from utils.error_payload import ErrorPayload
p = ErrorPayload('job_not_found', '任务不存在')
d = p.to_dict()
assert set(d) == {'error_code','message','retryable','detail','user_action'}, d.keys()
print('PAYLOAD_SHAPE_OK')
"

# gateway/job_intercept.py 双写：同时含旧 error 字段和新 error_code 字段
grep -n '"error"\|"error_code"\|retryable' gateway/job_intercept.py | head -10
# 预期：body 中同时含 "error" 和 "error_code" 两行，以及 retryable: bool = False

# gateway 测试（smoke）通过
python -m pytest tests/ -q -x \
    -k "intercept or job_intercept" \
    --no-header 2>&1 | tail -5
# 预期：passed（无 FAILED / ERROR）
```

---

## 测试计划（新增 / 回归）

### 新增测试文件：`tests/test_shared_helpers.py`

**覆盖范围**（所有 case 均为纯单元测试，无网络/无外部服务）：

```
TestNormalizeOptionalText
  test_none_returns_none
  test_empty_string_returns_none
  test_whitespace_only_returns_none
  test_strips_and_returns
  test_non_string_coerced_via_str

TestCoerceBool
  test_true_literals          # "true", "True", "1", "yes", "on"
  test_false_literals         # "false", "False", "0", "no", "off"
  test_bool_passthrough
  test_unknown_returns_default
  test_none_returns_default

TestCoerceInt
  test_int_passthrough
  test_str_int
  test_float_truncated
  test_invalid_returns_default

TestCoerceOptionalInt
  test_none_returns_none
  test_valid_returns_int
  test_invalid_returns_none

TestToJsonable
  test_primitives_passthrough
  test_path_to_str
  test_nested_dict
  test_list_and_tuple
  test_dataclass_via_asdict
  test_object_with_dunder_dict
  test_private_attrs_excluded

TestWriteJson  (使用 tmp_path fixture)
  test_roundtrip_utf8
  test_creates_parent_dirs
  test_path_values_serialized_as_str

TestErrorPayload
  test_to_dict_has_all_fields
  test_defaults_are_safe
  test_frozen_immutability
  test_retryable_codes_disjoint_from_non_retryable
```

运行命令：

```bash
python -m pytest tests/test_shared_helpers.py -v --no-header 2>&1 | tail -20
# 预期：全部 case passed
```

### 回归测试

迁移前后需通过以下测试组（每个 Step 的验收命令已涵盖，集中列出便于最终全量验证）：

```bash
# 完整回归（全量 — 排除已知与本单元无关的慢速 E2E 测试）
python -m pytest tests/ -q \
    -k "not benchmark and not e2e" \
    --no-header 2>&1 | tail -10
# 通过率应与本单元开始前基线相同（用 Step 0 运行的结果作基线）
```

---

## 回滚方案

- **每个 Step 是独立 commit**（见 DoD），回滚粒度 = 单 Step。
- Step 1（coerce.py）：`git revert <commit>`；恢复 assemblyai/transcriber.py、tts_generator.py、llm/router.py 的本地定义（各文件旧版存 git 历史）。
- Step 2（json_helpers.py）：同法 revert；两个示范文件历史中有旧版本可还原。
- Step 3（DRY-04）：revert 单 commit；cosyvoice_voice_selector.py 恢复 _AGE_* + _resolve_age_bucket 定义。
- Step 4（DRY-06）：revert voice_reranker.py + 三个 selector 的对应 commit。
- Step 5（error_payload）：revert gateway/job_intercept.py 中 `retryable`/`user_action` 参数与 `error_code` 双写字段的变更；`src/utils/error_payload.py` 本身纯文档性质，留着无害。注意：回滚后 body 恢复为仅含旧 `error` 字段（双写已删除），不影响前端消费。
- **无数据库变更、无迁移文件**，回滚不需要 alembic downgrade。

---

## 完成定义（DoD）

- [ ] `src/utils/coerce.py` 已创建，含 `normalize_optional_text`、`coerce_bool`、`coerce_int`、`coerce_optional_int`，通过 `python -m py_compile`。
- [ ] `src/utils/json_helpers.py` 已创建，含 `to_jsonable`、`write_json`，通过 `python -m py_compile`。
- [ ] `src/utils/error_payload.py` 已创建，含 `ErrorPayload` dataclass + `RETRYABLE_CODES` / `NON_RETRYABLE_CODES`，`to_dict()` 含全部 5 个字段。
- [ ] 示范迁移：`assemblyai/transcriber.py`、`tts_generator.py`、`llm/router.py` 已迁移到 `utils.coerce`（本地定义已删）。
- [ ] 示范迁移：`assemblyai/transcriber.py`、`gemini/translator.py` 已迁移到 `utils.json_helpers`（本地 `_write_json`/`_to_jsonable` 已删）。
- [ ] `cosyvoice_voice_selector.py` 已删除 `_AGE_*` 常量和 `_resolve_age_bucket`，改用 `voice_reranker.resolve_age_bucket`。
- [ ] `voice_reranker.py` 已新增 `unpack_rerank_result`；三个 selector 已替换 4 行重复提取模式。
- [ ] `gateway/job_intercept.py:_error_response` 已新增 `retryable`、`user_action` 可选参数（向后兼容）；body 同时包含旧 `error` 字段（保留兼容）和新 `error_code` 字段（双写过渡，不删旧字段）。
- [ ] `gateway/` 下任何文件均未 import `src/utils/error_payload`（gateway 保留自己的本地 Adapter）；验证：`grep -rn "from utils.error_payload\|import error_payload" gateway/` 无输出。
- [ ] `tests/test_shared_helpers.py` 已新建，所有 case passed。
- [ ] `tests/test_voice_reranker.py::TestResolveAgeBucket` 全 5 条 passed（回归）。
- [ ] `grep -rn "^def _normalize_optional_text" src/ | wc -l` 输出 ≤ 示范迁移前基线（本单元不要求全量清零，但**不得增加**）。
- [ ] 全量回归通过：`python -m pytest tests/ -q -k "not benchmark and not e2e" 2>&1 | tail -3` 无 FAILED / ERROR。
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未执行 `git add .`。

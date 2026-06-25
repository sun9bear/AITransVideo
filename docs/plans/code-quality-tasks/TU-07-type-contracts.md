# TU-07 · 类型契约硬化 + mypy 窄域纳入

- **目标 / 价值**：消除 `DubbingSegment(slots=True)` 上的 `getattr` 架空——slots dataclass 上的 `getattr(seg, "x", default)` 在字段存在时完全正确，但当字段**不存在**时静默返回 default 而非在构建期报错，彻底绕过了 slots 的类型安全意义（H5 已证实 `en_text` 就是这类缺字段）。本单元把 tts_generator.py 52 处 + aligner.py 9 处（4 处 `en_text` 由 TU-01 处理）+ process.py 中剩余相关的 `getattr(segment, …)` 改为直接属性访问；给 `compute_job_policy` 的返回值加 `TypedDict`；把 `_read_job_field` 的 `job_record: Any` 收窄；最后把这三个模块逐步纳入 TU-03 的 mypy 窄域 override，让构建期能捕获同类回归。
- **关联发现**：TS-01（`en_text` 缺字段，TU-01 修）· TS-02（65+ getattr 架空）· TS-05（`compute_job_policy` 裸 dict）· TS-07（`job_record: Any`）· TS-10（mypy 窄域纳入）
- **前置依赖**：TU-03（mypy 窄域门就位，`pyproject.toml [tool.mypy]` + `[[tool.mypy.overrides]]` 已合入）；TU-01（aligner.py 4 处 `en_text` → `source_text` 已改，TU-07 不重复处理这 4 处）
- **建议分支**：`quality/type-contracts`
- **预估工时**：M（分 Step 拆批改，含新增回归测试，预估 2–3 天）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`tail -1`→`Select-Object -Last 1`、`test -f`→`Test-Path`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **禁止批量 `type: ignore`**：本 PR 不得批量为现有代码加 `# type: ignore[...]` 换取表面绿，每处 ignore 必须有具体原因且仅限于无法修改的第三方/外部契约代码。
- **mypy 窄域只纳入 tts_generator + aligner**：`pyproject.toml` 的 `[[tool.mypy.overrides]]` 本单元只添加这两个模块；`gateway.job_intercept` 不纳入（体量 6,880+ 行，纳入会产生大量 unrelated 阻断错误）。
- **`compute_job_policy` TypedDict 为局部范围**：可给 `compute_job_policy` 加 `JobPolicy` TypedDict 并补局部测试，但 `gateway/job_intercept.py` 整体不成为本 PR 的 mypy 阻断目标；`job_intercept` 纳入 mypy 延后至 TU-09 拆分后专项处理。
- **`target_language` 保留防御式 `getattr`**：若 `target_language` 不是 `DubbingSegment` 声明字段，不强行删除 getattr，改为保留并加注释说明原因（兼容 i18n 临时扩展，字段声明待 TU-09）；不加 `# type: ignore`，不强行删除。
- **Step 6 pyproject.toml 只写两个模块**：`[[tool.mypy.overrides]]` 中的 `module` 列表移除 `"gateway.job_intercept"`，仅保留 `"src.services.tts.tts_generator"` 和 `"src.services.alignment.aligner"`。
- **DoD 补"无批量 type:ignore"勾选项**：完成定义须核查 PR diff 中无批量 ignore 出现。

---

## 不在本单元范围（out-of-scope）

- aligner.py 4 处 `getattr(segment, "en_text", "")` → `segment.source_text`：**属 TU-01 H5**，本单元不重复处理。
- aligner.py 其余 `getattr(segment, …)` 中涉及 DSP 字段（`pre_tts_contradiction`、`pre_tts_rewrite_direction` 等）的清理：本单元一并处理，但**不改 DSP-first 的任何业务逻辑**。
- process.py 超过 800 行的结构性拆分：**属 TU-06（Option B 收敛）**，本单元只删其中 `getattr(segment, …)` 用法。
- `job_record: Any` 在 Gateway 侧（`gateway/` 路由函数）的 Any 清理：属 TU-05 Gateway route family，不在此处。
- `compute_job_policy` 内部逻辑变更（新增 service_mode、修改 policy 字段）：本单元**只加类型注解**，不改 policy 逻辑。
- 全仓 strict mypy（`disallow_any_generics` 等）：Phase 4+，本单元只做 `check_untyped_defs + warn_return_any` 的窄域 override。

---

## 必守不变量

- **付费 API 红线**：本单元不新增任何付费 API 调用点；`getattr` 改直接访问不改任何 fallback 语义；`compute_job_policy` 的 TypedDict 是注解，不改 policy 值。
- **Alignment DSP-first**：aligner.py 的 getattr 清理**只改调用形式**，不改 DSP 分支条件（`force_dsp_severity`、`dsp_speed_ratio_used` 等字段）的任何判断逻辑。
- **rewrite loop 是 fallback**：`_rewrite_segment_with_constraints` 的调用路径及其结果处理完全不动，只清理外层 getattr。
- **剪映 draft 为主交付物**：本单元不触碰 `OutputDispatcher` / `_build_jianying_draft` 等输出路径。
- **Gateway 是 plan/pricing/entitlement 唯一事实源**：给 `compute_job_policy` 加 TypedDict 是收窄类型，不向前端暴露任何新信息，不改 Gateway 单一事实源地位。
- **默认测试不接真实外部服务**：新增测试全部用 mock / `SimpleNamespace` / `dataclasses.replace`，不调用 TTS / LLM / MiniMax 真实 endpoint。
- **process.py 走 Option B**：本单元不在 process.py 新增任何业务逻辑，只删 getattr 调用；不新建 stages/ 子包。
- **先补测试再动代码**：每个子任务先确认现有测试通过，再改代码，再验证回归。

---

## Step 0 · 确认现状

```bash
git switch -c quality/type-contracts

# 1. 确认 DubbingSegment 定义位置
grep -n "^class DubbingSegment" src/services/gemini/translator.py
# 预期：252:class DubbingSegment  （若漂移按实际行号注明）

# 2. 确认 _read_job_field 位置
grep -n "^def _read_job_field" src/services/tts/tts_generator.py
# 预期：130:def _read_job_field

# 3. 确认 compute_job_policy 签名
grep -n "^def compute_job_policy" gateway/job_intercept.py
# 预期：798:def compute_job_policy(user, service_mode: str) -> dict:

# 4. 统计当前 getattr 计数（建立 baseline）
echo "=== tts_generator.py getattr(segment) ==="
grep -c "getattr(segment" src/services/tts/tts_generator.py
# 预期：52

echo "=== aligner.py getattr(segment) — 去除 en_text 4 处（TU-01 处理）==="
grep -c "getattr(segment" src/services/alignment/aligner.py
# 预期：13（含 TU-01 的 4 处 en_text；TU-01 完成后应为 9）

echo "=== process.py getattr(segment) ==="
grep -c "getattr(segment" src/pipeline/process.py
# 预期：99（本单元仅清理其中与 DubbingSegment 直接字段的 getattr）

# 5. 确认 DubbingSegment 所有字段名（供后续判断哪些 getattr 可安全去除）
python - <<'PY'
import ast, pathlib
src = pathlib.Path("src/services/gemini/translator.py").read_text(encoding="utf-8")
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "DubbingSegment":
        fields = [n.target.id for n in ast.walk(node)
                  if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
        print(f"DubbingSegment fields ({len(fields)}):", fields)
PY

# 6. 确认现有回归测试基线
python -m pytest tests/test_gateway_job_policy.py tests/test_volcengine_voice_selector.py -q 2>&1 | tail -3
# 预期：全绿（no failures）

# 7. 确认 TU-03 的 mypy overrides 是否已在 pyproject.toml
grep -A5 "tool.mypy.overrides" pyproject.toml | head -20
# 若 TU-03 未完成：本单元 Step 5 会自行补充 overrides 片段（TU-03 是建议前置，不是硬阻断）
```

**关键行号差异记录**（本单元执行前须填写）：

| 符号 | spec 行号 | 实际行号 | 差异备注 |
|---|---|---|---|
| `DubbingSegment` | 252 | \_\_\_\_ | |
| `_read_job_field` | 130 | \_\_\_\_ | |
| `TTSGenerator.__init__` job_record 参数 | 174 | \_\_\_\_ | |
| `compute_job_policy` | 798 | \_\_\_\_ | |

---

## Step 1 · 为 DubbingSegment 的 getattr 清理建契约测试

**目的**：在改任何调用代码之前，先固化"字段必须存在"这一不变量，防止后续回滚。

**动作**：新建 `tests/test_type_contracts_dubbing_segment.py`，覆盖：

1. `DubbingSegment` 的所有字段均可直接属性访问（不需要 getattr 默认值），slots 不允许注解外字段。
2. `tts_generator._read_job_field` 的两路（dict / object）均返回预期值且不返回 `Any`。
3. `compute_job_policy` 返回值包含所有预期键（`service_mode / tts_provider / tts_model / requires_review / voice_clone_enabled / voice_strategy / plan_code_snapshot / role_snapshot / quality_tier`）且值类型与 TypedDict 预期一致。

```python
# tests/test_type_contracts_dubbing_segment.py
"""契约测试：DubbingSegment 字段直接访问 + TypedDict 形状不变量。"""
from __future__ import annotations

import sys, types, dataclasses
from types import SimpleNamespace
import pytest

# --- stub database for gateway import ---
_gw = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gw not in sys.path:
    sys.path.insert(0, _gw)
_fake_db = types.ModuleType("database")
_fake_db.get_db = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
_fake_db.engine = _fake_db.get_db
_fake_db.async_session = _fake_db.get_db
sys.modules.setdefault("database", _fake_db)

from src.services.gemini.translator import DubbingSegment  # noqa: E402
from src.services.tts.tts_generator import _read_job_field   # noqa: E402
from job_intercept import compute_job_policy                  # noqa: E402


def _minimal_segment(**overrides) -> DubbingSegment:
    """最小合法 DubbingSegment（必填字段）。"""
    defaults = dict(
        segment_id=1, speaker_id="A", display_name="Speaker A",
        voice_id="", start_ms=0, end_ms=1000, target_duration_ms=1000,
        source_text="hello", cn_text="你好",
    )
    defaults.update(overrides)
    return DubbingSegment(**defaults)


def _make_user(role="user", plan_code="free"):
    return SimpleNamespace(id="u1", email="t@t.com", display_name="T",
                           role=role, plan_code=plan_code,
                           free_jobs_quota_total=5, free_jobs_quota_used=0)


# ── 1. DubbingSegment slots 契约 ──────────────────────────────────────

class TestDubbingSegmentSlots:
    def test_slots_rejects_unknown_attribute(self):
        """slots=True: 设置未声明字段必须 AttributeError（不能静默存储）。"""
        seg = _minimal_segment()
        with pytest.raises(AttributeError):
            seg.en_text = "should fail"  # en_text 不是 DubbingSegment 字段

    def test_known_fields_directly_accessible(self):
        """所有在 DubbingSegment 声明的字段可直接访问，不需 getattr 默认值。"""
        seg = _minimal_segment()
        # 抽查高频字段
        for attr in ("voice_id", "gender", "age_group", "persona_style",
                     "energy_level", "dubbing_mode", "requires_worker",
                     "worker_target_model", "tts_provider", "tts_model_key",
                     "target_chars_per_second", "target_duration_ms",
                     "first_pass_cn_text", "tts_audio_path",
                     "pre_tts_contradiction", "pre_tts_rewrite_direction",
                     "voiceclone_reference_path"):
            # 直接访问不抛 AttributeError
            _ = getattr(seg, attr)  # 此处用 getattr 是测试工具，非被测代码

    def test_dataclass_fields_match_expected_set(self):
        """确认 DubbingSegment 字段集包含本单元清理涉及的所有字段。"""
        field_names = {f.name for f in dataclasses.fields(DubbingSegment)}
        required = {
            "voice_id", "gender", "age_group", "persona_style",
            "energy_level", "dubbing_mode", "requires_worker",
            "worker_target_model", "tts_provider", "tts_model_key",
            "target_chars_per_second", "target_duration_ms",
            "first_pass_cn_text", "tts_audio_path",
            "pre_tts_contradiction", "pre_tts_rewrite_direction",
            "voiceclone_reference_path",
        }
        missing = required - field_names
        assert not missing, f"DubbingSegment 缺字段（回归红线）: {missing}"


# ── 2. _read_job_field 双路契约 ───────────────────────────────────────

class TestReadJobField:
    def test_dict_path(self):
        assert _read_job_field({"tts_model": "speech-2.8-hd"}, "tts_model") == "speech-2.8-hd"

    def test_dict_missing_key_returns_none(self):
        assert _read_job_field({}, "tts_model") is None

    def test_object_path(self):
        rec = SimpleNamespace(tts_model="speech-2.8-turbo")
        assert _read_job_field(rec, "tts_model") == "speech-2.8-turbo"

    def test_object_missing_attr_returns_none(self):
        assert _read_job_field(SimpleNamespace(), "tts_model") is None


# ── 3. compute_job_policy 键集 + 类型形状契约 ─────────────────────────

_EXPECTED_POLICY_KEYS = frozenset({
    "service_mode", "tts_provider", "tts_model",
    "requires_review", "voice_clone_enabled",
    "voice_strategy", "plan_code_snapshot",
    "role_snapshot", "quality_tier",
})


class TestJobPolicyShape:
    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_all_modes_return_expected_keys(self, mode):
        p = compute_job_policy(_make_user(), mode)
        missing = _EXPECTED_POLICY_KEYS - set(p.keys())
        assert not missing, f"mode={mode} 缺键: {missing}"

    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_bool_fields_are_bool(self, mode):
        p = compute_job_policy(_make_user(), mode)
        assert isinstance(p["requires_review"], bool), f"mode={mode}"
        assert isinstance(p["voice_clone_enabled"], bool), f"mode={mode}"

    @pytest.mark.parametrize("mode", ["express", "studio", "smart", "free"])
    def test_str_fields_are_str(self, mode):
        p = compute_job_policy(_make_user(), mode)
        for key in ("service_mode", "tts_provider", "voice_strategy",
                    "plan_code_snapshot", "role_snapshot", "quality_tier"):
            assert isinstance(p[key], str), f"mode={mode} key={key}"
```

**该步验收**：

```bash
python -m pytest tests/test_type_contracts_dubbing_segment.py -v 2>&1 | tail -10
# 预期：所有测试通过（no failures / no errors）
# 注意：test_slots_rejects_unknown_attribute 若红，说明 DubbingSegment 未用 slots=True —— 这本身是回归。
```

独立 commit：

```bash
git add tests/test_type_contracts_dubbing_segment.py
git commit -- tests/test_type_contracts_dubbing_segment.py \
  -m "test: type contract guards for DubbingSegment slots, _read_job_field, job policy shape (TU-07 Step 1)"
```

---

## Step 2 · aligner.py：getattr → 直接属性访问（TU-01 完成后执行）

**前提**：TU-01 已完成，aligner.py 的 4 处 `getattr(segment, "en_text", "")` 已改为 `segment.source_text`。

**位置**：`src/services/alignment/aligner.py`

当前 13 处 `getattr(segment, …)`，去除 TU-01 的 4 处，剩余 9 处全部指向 `DubbingSegment` 的已声明字段，可安全改为直接访问。以实际行号为准（Step 0 中记录）：

| 原代码 | 替换为 | 字段存在验证 |
|---|---|---|
| `getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)` | `segment.dubbing_mode` | 字段在 translator.py:358 |
| `getattr(segment, "first_pass_cn_text", "")` | `segment.first_pass_cn_text` | 字段在 translator.py:281 |
| `getattr(segment, "tts_audio_path", None)` | `segment.tts_audio_path` | 字段在 translator.py:262 |
| `getattr(segment, "pre_tts_rewrite_direction", "")` | `segment.pre_tts_rewrite_direction` | 字段在 translator.py:305 |
| `getattr(segment, "pre_tts_contradiction", False)` | `segment.pre_tts_contradiction` | 字段在 translator.py:311 |

> ⚠️ 注意：`DubbingSegment.tts_audio_path` 类型为 `str | None`（默认 `None`），原 getattr 默认值也是 `None`，语义等价；`dubbing_mode` 类型为 `str`，默认值 `DUBBING_MODE_DUB`，直接访问语义完全一致。

**改法（以 dubbing_mode 为例）**：

```python
# 改前：
if is_keep_original_dubbing_mode(getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)):
# 改后：
if is_keep_original_dubbing_mode(segment.dubbing_mode):
```

对 `tts_audio_path`（类型 `str | None`）：

```python
# 改前：
raw_path_value = (getattr(segment, "tts_audio_path", None) or "").strip()
# 改后：
raw_path_value = (segment.tts_audio_path or "").strip()
```

**该步验收**：

```bash
# 1. 清理后计数下降
grep -c "getattr(segment" src/services/alignment/aligner.py
# TU-01 未完成时预期 4（仅剩 en_text）；TU-01 完成后预期 0

# 2. 回归：aligner 相关测试
python -m pytest tests/ -k "aligner or align" -q 2>&1 | tail -5
# 预期：no failures

# 3. 契约测试仍绿
python -m pytest tests/test_type_contracts_dubbing_segment.py -q 2>&1 | tail -3
# 预期：all passed
```

独立 commit：

```bash
git add src/services/alignment/aligner.py
git commit -- src/services/alignment/aligner.py \
  -m "refactor: remove getattr(segment) in aligner.py — DubbingSegment fields are declared (TU-07 Step 2)"
```

---

## Step 3 · tts_generator.py：getattr(segment, …) 批量改直接访问

**位置**：`src/services/tts/tts_generator.py`（52 处 `getattr(segment, …)`）

**策略**：分两批改，每批 commit，避免单次大改难以 review。

### 批次 A：高频、逻辑简单的字段（约 30 处）

以下字段均在 `DubbingSegment` 声明，直接替换无歧义：

| 原 getattr | 替换为 |
|---|---|
| `getattr(segment, "voice_id", None)` | `segment.voice_id or None`（类型 str，空串视为无值，行为等价） |
| `getattr(segment, "gender", None)` | `segment.gender or None` |
| `getattr(segment, "age_group", None)` | `segment.age_group or None` |
| `getattr(segment, "persona_style", None)` | `segment.persona_style or None` |
| `getattr(segment, "energy_level", None)` | `segment.energy_level or None` |
| `getattr(segment, "voice_description", "")` | `segment.voice_description` |
| `getattr(segment, "target_chars_per_second", 0.0)` | `segment.target_chars_per_second` |
| `getattr(segment, "target_duration_ms", 0)` | `segment.target_duration_ms` |
| `getattr(segment, "tts_input_cn_text", None)` | `segment.tts_input_cn_text or None` |
| `getattr(segment, "selected_voice", "")` | `segment.selected_voice` |
| `getattr(segment, "match_confidence", "")` | `segment.match_confidence` |
| `getattr(segment, "tts_provider", None)` | `segment.tts_provider or None` |
| `getattr(segment, "tts_model_key", None)` | `segment.tts_model_key or None` |
| `getattr(segment, "requires_worker", False)` | `segment.requires_worker` |
| `getattr(segment, "worker_target_model", None)` | `segment.worker_target_model or None` |
| `getattr(segment, "voiceclone_reference_path", None)` | `segment.voiceclone_reference_path` （类型 str \| None） |
| `getattr(segment, "target_language", None)` | 见注意事项 |

> ✅ **已决策（CodeX 2026-06-25）：`target_language` 保留防御式 `getattr`**：若 `DubbingSegment` 未声明 `target_language` 字段，此处 getattr 是合理的防御式访问（兼容 i18n 临时扩展），**保留 getattr 调用不改动**，并在其后添加注释说明原因（示例：`# target_language 非 DubbingSegment 声明字段；兼容 i18n 扩展，待 TU-09 声明后可改为直接访问`）。**不加 `# type: ignore`，不强行删除。** 字段正式声明归属 TU-09（i18n）。
>
> ⚠️ **`voice_id` 的 `or None` 语义**：原 `_normalize_optional_text(getattr(segment, "voice_id", None))` 中 getattr 返回 None（段未设置时），改后 `segment.voice_id` 返回 `""`（默认值），`_normalize_optional_text("")` 应同样返回 None（确认 `_normalize_optional_text` 对空串的处理后再改）。

### 批次 B：剩余带逻辑判断的（约 22 处）

包含 `int(getattr(segment, "target_duration_ms", 0) or 0)`、`float(getattr(segment, "target_chars_per_second", 0.0)) or None` 等含 int/float 转换的 getattr。直接改为：

```python
# 改前：
target_duration_ms=int(getattr(segment, "target_duration_ms", 0) or 0),
# 改后：
target_duration_ms=int(segment.target_duration_ms or 0),

# 改前：
float(getattr(segment, "target_chars_per_second", 0.0)) or None
# 改后：
float(segment.target_chars_per_second) or None
```

**该步验收**：

```bash
# 批次 A commit 后验证
echo "=== getattr(segment) 剩余计数（批次 A 后应约减少 30） ==="
grep -c "getattr(segment" src/services/tts/tts_generator.py

# 批次 B commit 后验证
echo "=== getattr(segment) 最终计数（应为 0 或仅剩 target_language 1 处） ==="
grep -c "getattr(segment" src/services/tts/tts_generator.py

# 回归：tts_generator 相关测试
python -m pytest tests/ -k "tts or volcengine_voice or cosyvoice_voice" -q 2>&1 | tail -5
# 预期：no failures

# 契约测试仍绿
python -m pytest tests/test_type_contracts_dubbing_segment.py -q 2>&1 | tail -3
```

独立 commit（两批分别 commit）：

```bash
# 批次 A
git add src/services/tts/tts_generator.py
git commit -- src/services/tts/tts_generator.py \
  -m "refactor: remove getattr(segment) batch-A in tts_generator.py (TU-07 Step 3-A)"

# 批次 B
git add src/services/tts/tts_generator.py
git commit -- src/services/tts/tts_generator.py \
  -m "refactor: remove getattr(segment) batch-B in tts_generator.py (TU-07 Step 3-B)"
```

---

## Step 4 · compute_job_policy：加 TypedDict 返回类型

**位置**：`gateway/job_intercept.py:798`

**目的**：让调用侧（目前只有 `gateway/job_intercept.py:2333`）能获得构建期类型检查，消除 TS-05 裸 dict。

**改法**：在 `job_intercept.py` 顶部导入区后加入 TypedDict（Python 3.12 支持 `from typing import TypedDict`）：

```python
# 在 job_intercept.py 适当位置新增（已有的 from __future__ import annotations 之后）：
from typing import TypedDict

class JobPolicy(TypedDict, total=True):
    service_mode: str
    tts_provider: str
    tts_model: str | None      # studio/volcengine 时可为 None
    requires_review: bool
    voice_clone_enabled: bool
    voice_strategy: str
    plan_code_snapshot: str
    role_snapshot: str
    quality_tier: str
```

然后修改 `compute_job_policy` 签名：

```python
# 改前：
def compute_job_policy(user, service_mode: str) -> dict:
# 改后：
def compute_job_policy(user, service_mode: str) -> JobPolicy:
```

> ⚠️ **`tts_model` 的 Optional**：当 `service_mode="studio"` 且 `tts_provider="volcengine"` 时，`tts_model=None`（见 `job_intercept.py:902`）。因此 TypedDict 中 `tts_model: str | None`，不是 `str`。调用侧读取 `tts_model` 时必须做 `if policy["tts_model"] is not None:` 检查——这正是类型注解的价值所在。
>
> ⚠️ **不要新增 `total=False` 字段**：`JobPolicy` 是一个完整的 flat dict；若未来需要扩展键（如 `anonymous_preview` 标记），必须先更新 TypedDict 定义，避免散落的 `.get("new_key")` 绕过类型检查。

**该步验收**：

```bash
# 1. 签名已更新
grep -n "def compute_job_policy.*-> " gateway/job_intercept.py
# 预期：798:def compute_job_policy(user, service_mode: str) -> JobPolicy:

# 2. JobPolicy TypedDict 已定义
grep -n "class JobPolicy" gateway/job_intercept.py
# 预期：命中 1 行

# 3. 回归：job_policy 测试全绿
python -m pytest tests/test_gateway_job_policy.py -q 2>&1 | tail -3
# 预期：all passed

# 4. 契约测试仍绿
python -m pytest tests/test_type_contracts_dubbing_segment.py::TestJobPolicyShape -q 2>&1 | tail -3
```

独立 commit：

```bash
git add gateway/job_intercept.py
git commit -- gateway/job_intercept.py \
  -m "feat: add JobPolicy TypedDict to compute_job_policy return type (TU-07 Step 4)"
```

---

## Step 5 · _read_job_field / TTSGenerator.job_record：缩窄 Any 类型

**位置**：`src/services/tts/tts_generator.py:130`（`_read_job_field`）、`src/services/tts/tts_generator.py:174`（`TTSGenerator.__init__` 的 `job_record: Any`）

**目的（TS-07）**：`_read_job_field` 接受 `Any` 是因为 job_record 在流水线中以两种形态传入——`dict`（从 JSON 快照反序列化）或 `JobRecord`（runtime ORM 对象）。本步不改运行时行为，只让注解更精确。

**改法**：

1. 在 `tts_generator.py` 顶部加 Protocol 定义：

```python
from typing import Protocol, Union, runtime_checkable

@runtime_checkable
class JobRecordLike(Protocol):
    """tts_generator 访问的 job_record 字段子集的结构型 Protocol。"""
    job_id: str
    tts_model: str | None
    tts_provider: str
    service_mode: str
    # 注：dict 不实现此 Protocol，但 isinstance(obj, JobRecordLike) 对
    # 实际的 SQLAlchemy JobRecord 对象会返回 True（runtime_checkable）。
```

2. 修改 `_read_job_field` 注解（保持实现不变）：

```python
# 改前：
def _read_job_field(job_record: Any, key: str) -> Any:
# 改后：
def _read_job_field(job_record: Union[dict, "JobRecordLike", None], key: str) -> object:
    """读取 job_record（dict 或 ORM 对象）的某个字段，缺失时返回 None。"""
    if job_record is None:
        return None
    if isinstance(job_record, dict):
        return job_record.get(key)
    return getattr(job_record, key, None)
```

3. `TTSGenerator.__init__` 的 `job_record` 参数：

```python
# 改前：
def __init__(self, config: TTSConfig, *, job_record: Any = None):
# 改后：
def __init__(self, config: TTSConfig, *, job_record: Union[dict, "JobRecordLike", None] = None):
```

> ⚠️ **不要引入具体的 `JobRecord` import**：tts_generator 位于 `src/services/tts/`，不能 import `gateway/models.py` 里的 ORM 类（会产生循环依赖）。Protocol（结构化子类型）是正确解法——调用侧只需满足 Protocol 的字段集，不需要继承。
>
> ⚠️ **Protocol 字段仅列 _read_job_field 实际访问的键**：`job_id`、`tts_model`、`tts_provider`、`service_mode`。不要过度约束，否则未来 Job 对象新增字段时 Protocol 变成障碍。

**该步验收**：

```bash
# 1. 签名更新确认
grep -n "def _read_job_field" src/services/tts/tts_generator.py
# 预期命中 1 行，含 Union[dict

grep -n "class JobRecordLike" src/services/tts/tts_generator.py
# 预期命中 1 行

# 2. 回归：契约测试中的 _read_job_field 测试
python -m pytest tests/test_type_contracts_dubbing_segment.py::TestReadJobField -q 2>&1 | tail -3
# 预期：all passed

# 3. 全量 tts 相关测试
python -m pytest tests/ -k "tts" -q 2>&1 | tail -5
# 预期：no new failures
```

独立 commit：

```bash
git add src/services/tts/tts_generator.py
git commit -- src/services/tts/tts_generator.py \
  -m "refactor: narrow job_record Any to Protocol in tts_generator (TU-07 Step 5)"
```

---

## Step 6 · 纳入 mypy 窄域 override + 验收 exit 0

**目的（TS-10）**：把 `src.services.tts.tts_generator` 和 `src.services.alignment.aligner` 纳入 TU-03 已建立的 mypy `[[tool.mypy.overrides]]`，使构建期能捕获本单元清理后的任何 Any 回归。

> ✅ **已决策（CodeX 2026-06-25）：`gateway.job_intercept` 不纳入本单元 mypy override。** job_intercept.py 行数 6,880+，纳入会产生大量与本单元无关的阻断错误，禁止用批量 `# type: ignore` 换取表面通过。`job_intercept` 的 mypy 纳入延后至 TU-09 拆分后专项处理，本单元 pyproject.toml 片段中不写该模块。`compute_job_policy` 的 TypedDict 注解与局部测试照常在本 PR 落地，但不触发整个文件的 mypy 检查。

**前提**：TU-03 已在 `pyproject.toml` 写入 `[tool.mypy]` 基础配置（`python_version`, `ignore_missing_imports`, `check_untyped_defs`）。若 TU-03 未完成，本步需先手动补充 `[tool.mypy]` 基础段（见母方案 §10.3）。

**改法**：在 `pyproject.toml` 的 `[[tool.mypy.overrides]]` 追加（仅两个模块，不含 `gateway.job_intercept`）：

```toml
[[tool.mypy.overrides]]
module = [
    "src.services.tts.tts_generator",
    "src.services.alignment.aligner",
]
disallow_untyped_defs = true
warn_return_any = true
# 注：不加 disallow_any_generics / strict_equality，避免过度约束现有代码库
# gateway.job_intercept 不在此处纳入——延后至 TU-09 拆分后专项处理（体量 6880+ 行，需专项处理）
```

**该步验收**：

```bash
# 1. 对清理后的模块单独跑 mypy（指定模块路径）
python -m mypy src/services/tts/tts_generator.py \
    --ignore-missing-imports --check-untyped-defs --warn-return-any \
    --exclude "\.codex_worktrees|\.codex_tmp|\.claude/worktrees" 2>&1 | tail -10
# 目标：与本单元改动相关的符号（_read_job_field, TTSGenerator.__init__,
#         tts_generator 中直接访问 segment 属性处）无 error。
# 允许其他预存 error 暂存（不得用批量 type: ignore 压住，需记录为后续工作）。

python -m mypy src/services/alignment/aligner.py \
    --ignore-missing-imports --check-untyped-defs --warn-return-any \
    --exclude "\.codex_worktrees|\.codex_tmp|\.claude/worktrees" 2>&1 | tail -10
# 目标：TU-01 + TU-07 改动处无 error。

# 2. 确认 pyproject.toml override 段已写入且不含 gateway.job_intercept
grep -A6 "src.services.tts.tts_generator" pyproject.toml
# 预期：命中 [[tool.mypy.overrides]] 段，module 列表中无 gateway.job_intercept

grep "gateway.job_intercept" pyproject.toml
# 预期：无输出（gateway.job_intercept 未纳入本单元 override）

# 3. 回归：所有相关测试
python -m pytest tests/test_type_contracts_dubbing_segment.py \
    tests/test_gateway_job_policy.py \
    tests/test_volcengine_voice_selector.py \
    -q 2>&1 | tail -5
# 预期：all passed

# 4. 统计 getattr(segment) 总量（量化收尾指标）
echo "=== 清理后 getattr(segment) 总计（目标：tts_generator+aligner 合计 ≤1，process.py 有降） ==="
echo -n "tts_generator: "; grep -c "getattr(segment" src/services/tts/tts_generator.py
echo -n "aligner:       "; grep -c "getattr(segment" src/services/alignment/aligner.py
echo -n "process.py:    "; grep -c "getattr(segment" src/pipeline/process.py
# tts_generator 目标：0（或仅剩 target_language 1 处若字段未声明）
# aligner 目标：0（TU-01 完成后）
# process.py 目标：比 baseline(99) 有所下降（本单元只清理与 DubbingSegment 完全匹配的字段）
```

独立 commit：

```bash
git add pyproject.toml
git commit -- pyproject.toml \
  -m "chore: add mypy narrow-domain overrides for tts_generator + aligner (TU-07 Step 6)"
```

---

## 测试计划（新增 / 回归）

### 新增测试

| 测试文件 | 覆盖内容 | 类型 |
|---|---|---|
| `tests/test_type_contracts_dubbing_segment.py` | DubbingSegment slots 契约、`_read_job_field` 双路、`compute_job_policy` 键集+类型形状 | contract |

### 回归验证清单

```bash
# 全量相关测试（每个 Step 完成后均需跑）
python -m pytest \
    tests/test_type_contracts_dubbing_segment.py \
    tests/test_gateway_job_policy.py \
    tests/test_volcengine_voice_selector.py \
    tests/test_anonymous_express_t3_policy_fail_closed.py \
    -v 2>&1 | tail -20
# 预期：all passed，no new failures

# 守卫测试不回退
python -m pytest tests/test_legacy_cleanup_guards.py -q 2>&1 | tail -3
# 预期：all passed
```

### 可量化收尾指标

本单元完成时，以下指标必须可验证：

| 指标 | baseline | 目标 |
|---|---|---|
| `getattr(segment` in `tts_generator.py` | 52 | 0（或 ≤1 仅剩 target_language） |
| `getattr(segment` in `aligner.py`（TU-01 后） | 9 | 0 |
| `compute_job_policy` 返回类型 | `-> dict` | `-> JobPolicy` |
| `_read_job_field` 第一参数类型 | `Any` | `Union[dict, JobRecordLike, None]` |
| mypy 对 `tts_generator.py` 的本单元改动处 | 无检查 | 无 error |

> **process.py `getattr(segment, …)` 清理移交 [TU-14](TU-14-process-converge-1.md)**：本单元 goal 行 + Step 0 曾提及 process.py `getattr(segment,…)`「比 baseline 有所下降」，但 DoD 清单**无 process.py 验收项**、out-of-scope 又把 process.py 结构性收敛划归 TU-14。process.py 是 13.3k 行、在 ADR Option B 收敛轨上的最高风险文件，本单元**零触碰**（与 main 字节一致，90 处 `getattr(segment,…)` 不变），该清理移交 TU-14 在输出收敛触碰相关代码时一并做（字节等价，同本单元做法）。此处显式记录以免该指标被静默丢弃（对抗式审查 completeness lens，2026-06-25）。

---

## 回滚方案

| 范围 | 回滚方式 |
|---|---|
| `tests/test_type_contracts_dubbing_segment.py`（Step 1） | `git revert <commit>` 或直接删除文件——不影响任何业务代码 |
| `aligner.py` getattr 清理（Step 2） | `git revert <commit>`；TU-01 的 `en_text→source_text` 不受影响 |
| `tts_generator.py` getattr 清理（Step 3-A/3-B） | 两次独立 commit，分别 revert；不影响 `_read_job_field` 和 Protocol 改动 |
| `compute_job_policy` TypedDict（Step 4） | `git revert <commit>`；只影响类型注解，不改 dict 结构，调用侧无 runtime 变化 |
| `_read_job_field` Protocol 化（Step 5） | `git revert <commit>`；Protocol 是纯类型信息，不影响 runtime |
| `pyproject.toml` override（Step 6） | 删除新增的 `[[tool.mypy.overrides]]` 片段并 commit |

**回滚判据**：任何 Step 的回归测试出现新的 failures 且无法在当次 PR 内修复，立即回滚该 Step，不跨 Step 带病合并。

---

## 完成定义（DoD）

- [ ] Step 0：关键 `file:line` 已核对，行号差异已记录（上表已填写）。
- [ ] Step 1：`tests/test_type_contracts_dubbing_segment.py` 全绿，独立 commit 已完成。
- [ ] Step 2：`aligner.py` `getattr(segment` 计数为 0（TU-01 完成后），回归无新 failures，独立 commit。
- [ ] Step 3-A/3-B：`tts_generator.py` `getattr(segment` 计数 ≤1（仅 `target_language` 暂留），两次独立 commit，回归无新 failures。
- [ ] Step 4：`compute_job_policy` 返回类型为 `-> JobPolicy`，`tests/test_gateway_job_policy.py` 全绿，独立 commit。
- [ ] Step 5：`_read_job_field` 第一参数类型已缩窄，`TestReadJobField` 全绿，独立 commit。
- [ ] Step 6：`pyproject.toml` mypy override 已写入，`tts_generator` + `aligner` 的改动处 mypy 无 error，独立 commit。
- [ ] 可量化收尾指标全部达标（见测试计划表格）。
- [ ] 全部相关守卫测试（`test_legacy_cleanup_guards`、`test_gateway_job_policy`、`test_volcengine_voice_selector`）回归绿。
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、**未 `git add .`**。
- [ ] PR diff 中无批量 `# type: ignore[...]`：每处 ignore 须有具体行内注释说明原因，且仅用于无法修改的外部/第三方契约代码；不得以批量 ignore 换取 mypy 表面通过（CodeX 2026-06-25）。
- [ ] `pyproject.toml` mypy override 的 `module` 列表中**不含** `gateway.job_intercept`（job_intercept 纳入延后至 TU-09 拆分后专项处理）。
- [ ] `target_language` getattr（若字段未在 DubbingSegment 声明）已保留并加注释，**未被强行删除也未加 `# type: ignore`**。

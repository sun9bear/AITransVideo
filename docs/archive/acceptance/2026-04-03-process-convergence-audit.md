# Process → Workflow 收敛审计 — 2026-04-03 (rev.2)

> 本文档回答：`process.py` 里还剩哪些 canonical-shape 逻辑值得迁移到 shared helpers、
> 哪些应保留在 process 中、下一步是否应继续动生产代码。
>
> rev.2：补充 `_ProcessOutputAlignedBlock` 与 `LocalizedProject` 类型契约不一致的审计。

## 1. 当前已收敛的边界

以下 canonical-shape 逻辑已经从 `process.py` 迁出到 shared helpers，`process.py` 作为调用方正确使用了它们：

| 已收敛功能 | shared helper | process.py 调用点 |
|-----------|--------------|-------------------|
| source_info 规范化 | `project_shape_helpers.build_canonical_source_info()` | `_build_process_source_info()` (L2460) 直接委托 |
| 核心媒体 artifact 注册 | `project_shape_helpers.build_core_media_artifact_entries()` | `_build_process_artifact_index()` (L2427) 直接委托 |
| artifact index 构建 | `ProjectBuilder.build_artifact_index()` | `_build_process_artifact_index()` (L2444) 直接委托 |
| project model 组装 | `ProjectBuilder.build_result()` | `_build_and_dispatch_output()` (L2385) 直接委托 |
| 输出分发 | `OutputDispatcher.dispatch()` | `_dispatch_process_output_bundle()` (L2405) 直接委托 |

这一部分结论**不变**。

---

## 2. 仍在 `process.py` 的逻辑分类

### 2A. process-only 适配逻辑（应保留，无争议）

| 逻辑 | 位置 | 保留理由 |
|------|------|---------|
| `_build_process_source_info()` 的 `source_type` 路径分支 | L2456-2459 | process 特有的多 source 类型路径解析 |
| `_build_process_artifact_index()` 中 3 个 process-only artifact 追加 | L2438-2443 | `download_metadata`、`review_state`、`project_state` 是 process 运行时产物 |
| `_build_process_output_captions()` | L2479-2495 | `DubbingSegment` → `SubtitleLine` 映射，process 特有 |
| `_resolve_process_output_block_status()` | L2673-2678 | 3 行 status 推导 |
| 6 个 `_build_*_stage_payload()` 方法 | L2525-2672 | process 运行时的 stage 进度记录 |

### 2B. 未完成的 shared-boundary 收敛：aligned_blocks 类型契约不一致

`process.py` 将 process-private 类型 `_ProcessOutputAlignedBlock` 注入到声明为 `list[SemanticBlock]` 的 canonical 字段中。`OutputDispatcher` 消费这些对象时，依赖了不存在于 `SemanticBlock` 的属性。

**数据链路：**

```
process.py _build_process_output_blocks()
  → 生成 list[_ProcessOutputAlignedBlock]
  → 存入 stage_outputs["aligned_blocks"]
  → ProjectBuilder.build() 将其赋值给 LocalizedProject.aligned_blocks
  → OutputDispatcher._build_aligned_segments() 消费
```

**类型契约不一致的代码证据：**

`LocalizedProject`（`src/core/project_model.py` L17）声明：
```python
aligned_blocks: list[SemanticBlock] = field(default_factory=list)
```

`_ProcessOutputAlignedBlock`（`src/pipeline/process.py` L306-326）是一个完全独立的 dataclass，不继承 `SemanticBlock`，但与其有大量字段重叠。

三个关键字段只存在于 `_ProcessOutputAlignedBlock`，不存在于 `SemanticBlock`：

| 字段 | `SemanticBlock` | `_ProcessOutputAlignedBlock` | OutputDispatcher 消费方式 |
|------|----------------|-------------------------------|--------------------------|
| `segment_id: int` | 不存在 | L307 | `_resolve_segment_id()` L169: `getattr(block, "segment_id", fallback)` |
| `alignment_method: str` | 不存在 | L321 | `_resolve_alignment_method()` L179: `getattr(block, "alignment_method", "")` |
| `needs_review: bool` | 不存在 | L322 | `_resolve_needs_review()` L189: `getattr(block, "needs_review", None)` |

`OutputDispatcher` 的三个 `_resolve_*` 方法（L169-194）全部使用 `getattr` + fallback 模式，这意味着：
- 传入 `SemanticBlock` 时：`getattr` 拿不到 `segment_id`/`alignment_method`/`needs_review`，走 fallback（从 `status` 字段推导，或用 `index` 替代）
- 传入 `_ProcessOutputAlignedBlock` 时：`getattr` 直接拿到值，不走 fallback

**这不是"故意的 duck-typing 设计"。** 这是一个功能上可运行但类型上不一致的状态：
- `LocalizedProject.aligned_blocks` 声明接受 `list[SemanticBlock]`
- 但 process.py 实际注入的是 `list[_ProcessOutputAlignedBlock]`
- `OutputDispatcher` 被迫用 `getattr` fallback 来兼容两种类型，而非通过正式的 shared interface

---

## 3. 候选迁移点评估

### 候选 1（修订）：将 `alignment_method`、`needs_review` 提升到 `SemanticBlock`

**当前状态：** 这两个字段是对齐阶段的核心输出信息。`OutputDispatcher` 需要用它们来决定 `AlignedSegment.alignment_method` 和 `AlignedSegment.needs_review`。当前 `SemanticBlock` 没有它们，`OutputDispatcher` 被迫从 `status` 字符串反推。

**收益：**
- 消除 `LocalizedProject.aligned_blocks` 的类型契约违反
- `OutputDispatcher._resolve_alignment_method()` 和 `_resolve_needs_review()` 不再需要 `status` 反推 fallback
- `process.py` 可以直接构造 `SemanticBlock` 而非 `_ProcessOutputAlignedBlock`（至少在对齐相关字段上）
- 未来新增的 pipeline 不需要重新发明相同的私有 block 类型

**风险：**
- 低。在 `SemanticBlock` 上增加两个带默认值的 optional 字段不会破坏现有调用方
- `OutputDispatcher` 的 `getattr` fallback 逻辑可以保留作为向后兼容，不需要立即删除
- 需要验证 workflow 路径（非 process 路径）构造 `SemanticBlock` 的地方不会被影响

**结论：** 值得进入 Task 3B，但需要同时处理 `segment_id` 的问题。

### 候选 2（修订）：`segment_id` 字段

**当前状态：** `SemanticBlock` 使用 `block_id: str`（如 `"segment_001"`），`_ProcessOutputAlignedBlock` 使用 `segment_id: int`（如 `1`）。`OutputDispatcher._resolve_segment_id()` 通过 `getattr` 读 `segment_id`，找不到时用 `index` fallback。

**收益：** 消除一个 `getattr` fallback。

**风险：** 中等。`block_id`（str）和 `segment_id`（int）的语义不完全相同。在 `SemanticBlock` 上新增 `segment_id: int` 字段可能导致两个 ID 概念混淆。

**结论：** 不建议在本轮处理。`block_id` 是 `SemanticBlock` 的 canonical identifier，`segment_id` 是 process pipeline 的 legacy 概念。`OutputDispatcher` 的 fallback 已经正确处理了这个差异。

### 候选 3（不变）：process-only artifact 追加

**结论不变：** 不应迁移。

---

## 4. 最终结论：GO — 最小切片

### 理由

`aligned_blocks` 的类型契约不一致是一个尚未完成的 shared-boundary 收敛。`LocalizedProject.aligned_blocks` 声明 `list[SemanticBlock]`（`project_model.py` L17），但 `process.py` 的 `_build_process_stage_outputs()`（L2471-2477）通过 `_build_process_output_blocks()`（L2497-2523）实际注入的是 `list[_ProcessOutputAlignedBlock]`。只要 process.py 仍然产出私有类型，类型契约违反就仍然存在，不论 `SemanticBlock` 上加了多少字段。

### 为什么"只给 SemanticBlock 增字段"不够

如果只在 `SemanticBlock` 上增加 `alignment_method` 和 `needs_review`，但 `process.py` 的 `_build_process_output_blocks()` 仍然构造 `_ProcessOutputAlignedBlock` 并塞入 `stage_outputs["aligned_blocks"]`，那么：

- `LocalizedProject.aligned_blocks` 里的对象仍然是 `_ProcessOutputAlignedBlock`，不是 `SemanticBlock`
- 类型契约违反完全没有消除——`SemanticBlock` 有了新字段，但没有人用它
- `OutputDispatcher` 仍然在消费一个它声明上不认识的类型

这只是"缩小字段差距"，不是"解决类型契约问题"。

### 推荐最小切片（Task 3B）

两步合为一个切片，缺一不可：

**Step A：在 `SemanticBlock` 上增加 `alignment_method` 和 `needs_review`**

- 修改 `src/core/models.py`
- `alignment_method: str = "direct"`
- `needs_review: bool = False`
- 带默认值，不破坏任何现有 `SemanticBlock` 调用方

**Step B：让 `process.py` 的 aligned block 构建路径直接产出 `SemanticBlock`**

- 修改 `src/pipeline/process.py` 的 `_build_process_output_blocks()`（L2497-2523）
- 将其从构造 `_ProcessOutputAlignedBlock` 改为构造 `SemanticBlock`
- `_ProcessOutputAlignedBlock` 上的 `segment_id: int` 字段不迁入 `SemanticBlock`（见下方说明），改为不传或者用 `block_id` 携带
- `_build_process_output_captions()` 不在本切片范围内（它产出 `SubtitleLine`，类型已经正确）

**不在本切片范围内：**

- **`segment_id`：** `SemanticBlock` 的 canonical ID 是 `block_id: str`。`segment_id: int` 是 process pipeline 的 legacy 概念。`OutputDispatcher._resolve_segment_id(block, fallback=index)`（L169-175）已经有稳定的 fallback：当 `getattr(block, "segment_id", fallback)` 找不到属性时，用 `enumerate` 的 `index` 作为 fallback，行为正确。将 `segment_id` 硬塞进 `SemanticBlock` 会引入 `block_id` 和 `segment_id` 两套 ID 的混淆，风险高于收益。
- **`OutputDispatcher` 的 `getattr` fallback：** 保留。Step B 完成后，`aligned_blocks` 里的 `SemanticBlock` 会携带 `alignment_method` 和 `needs_review`，`getattr` 会直接命中真实字段而非走 fallback。但不删除 fallback 逻辑，保持向后兼容。
- **`_ProcessOutputAlignedBlock` 类本身：** Step B 完成后如果没有其他引用，可以删除；如果还有其他引用则保留。由实现者判断。

### 为什么这个切片是最小且安全的

1. **Step A 单独不会破坏任何东西** — 只增加带默认值的字段
2. **Step B 单独也不会破坏任何东西** — `SemanticBlock` 是 `_ProcessOutputAlignedBlock` 的超集（除 `segment_id` 外），`OutputDispatcher` 通过 `getattr` 消费，不依赖具体类型
3. **两步一起才消除 drift** — Step A 让 `SemanticBlock` 能携带对齐信息，Step B 让 `process.py` 真正产出声明类型
4. **`segment_id` 不进入本切片** — 有稳定 fallback，语义不同，单独处理更安全

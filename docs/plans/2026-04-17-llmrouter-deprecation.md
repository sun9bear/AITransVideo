# LLMRouter 废弃观察期计划

> **Status:** active (observation period **重启**)
> **Last updated:** 2026-05-02
> **Observation window:** ~~2026-04-17 → 2026-05-01~~ **重启 2026-05-02 → 2026-05-16**
> **Decision node:** 2026-05-16 — 根据观察结果决定清理 or 延期
> **Related:** `docs/plans/2026-04-09-prompt-model-management-plan.md` §5.4 + §0 Gap 4

## 0. 重启原因（2026-05-02）

第一轮观察（2026-04-17~2026-05-01）失败，证据无法验证：

1. **观察源选错**：plan §2 依赖 `docker logs aivideotrans-app`。docker 日志驱动是 `json-file` 且未配 max-size/max-file rotation，但 `docker rm` 时 json log 文件随容器删除。`aivideotrans-app` 容器在 **2026-05-01 06:17:38** 被 recreate（新 container created time 早于决策日 2026-05-01），观察窗口内的 docker logs 全部丢失。
2. **Plan §1 论断不全**：`prompt_key_map` 漏映射 `s2_review`（[translator.py:901](../../src/services/gemini/translator.py:901) call site 在 [process.py:_legacy_speaker_inference_and_review](../../src/pipeline/process.py:4943) 的 2-speaker review fallback 里）。任何"新 S2 三轮 review 失败 + 2 speaker"的 job 都会触发 `[LLM-ROUTER-LEGACY]`，意味着 legacy path 不是死代码而是**rare-path live code**。

第二轮修复（2026-05-02 部署）：

- 给 `prompt_key_map` 补 `"s2_review": "translate"` 映射，让 fallback 路径走 llm_registry。
- legacy path 的 `print(...)` 之外**追加持久化 audit log** 写到 `${AIVIDEOTRANS_RUNTIME_LOGS_DIR}/llm-router-legacy.log`（默认 `/opt/aivideotrans/data/runtime_logs/`，bind mount，container recreate 不丢）。
- 观察窗重启 2 周。

---

## 1. 背景

`src/services/llm/router.py` 的**路由决策层**（`LLMRouter.get_route` / `generate_via_alias` / `DEFAULT_LLM_MODELS` 等）已被 `src/services/llm_registry.py` 取代（2026-04-09 方案 §5.4 立项、2026-04 批次实施）。但 translator 的 `_call_task_with_fallback` 保留了 legacy path 作为"unmapped task"兜底，router 实例仍在 `process.py:578` 被构造和注入。

生产代码证据表明 legacy path 理论上是死代码：
- `translator.py:1006` 条件 `if prompt_key and mode is not None` — 生产 job 总会设 `_service_mode`（`process.py:820`）
- `prompt_key_map` 覆盖 `s3_translate` / `s5_rewrite` / `s2_infer` 三个 task，这是 translator 实际被调的全部 task

"理论死代码"不等于"确认死代码"。B 方案（加 warning + 观察 → 清理）比直接删稳妥，本文档是这一轮观察的执行计划。

## 2. 观察期设置

### 2.1 已落地的标记

- `src/services/gemini/translator.py` 的 legacy path 入口加了 print 标签：

  ```python
  print(
      f"[LLM-ROUTER-LEGACY] hit task={task} prompt_key={prompt_key!r} "
      f"service_mode={mode!r} has_router={self.llm_router is not None}",
      flush=True,
  )
  ```

- `src/services/llm/router.py` 顶部加了 DEPRECATED module docstring，说明废弃计划 + 模块剩余真实用户。

### 2.2 观察期间每周检查

每周（建议周一）在生产日志里 grep：

```bash
# 主源（持久化 audit log，container recreate 不丢；2026-05-02 起启用）
SSH-US-Via-154.cmd "wc -l /opt/aivideotrans/data/runtime_logs/llm-router-legacy.log 2>/dev/null || echo 0 zero-hits"

# 辅助源（docker logs 仅当前 container lifetime 内有效）
SSH-US-Via-154.cmd "docker logs aivideotrans-app 2>&1 | grep -c '\[LLM-ROUTER-LEGACY\]'"
```

- **期望结果**：零命中（每周都是 0）
- **若命中**：记录命中的 task、上下文（prompt_key / service_mode / has_router），分析原因：
  - 是不是有新 task 没加到 `prompt_key_map`？→ 补 map 后重启观察期
  - 是不是某个测试/脚本入口没设 `_service_mode`？→ 修调用点
  - 是不是生产数据有 `service_mode=None` 的异常 job？→ 查 Gateway job snapshot 写入逻辑

### 2.3 每周记录

在本文件 §6 追加一行（格式见下）。

## 3. 清理条件

2026-05-01 决策节点，**同时满足**以下全部条件，才执行清理：

- [ ] 观察期内 `[LLM-ROUTER-LEGACY]` 零命中（连续 2 周每周 grep = 0）
- [ ] `process.py:2158-2160` 的 `model_configs` 读取用途已查清（metering / pricing / 日志？），并且已迁到 `llm_registry.MODEL_REGISTRY` 或确认可删
- [ ] 下线计划已通过一次 eng review（参考 §5 清理清单）

**任一不满足**：延期到 2026-05-15 再评估。

## 4. 观察期内不做的事

- 不动 `router.py` 模块本体（除已加的 DEPRECATED 注释）
- 不动 `process.py:578 / 807 / 2158` 的 router 构造和注入
- 不动测试文件（`test_llm_router.py` / `test_gemini_translator.py` 的 FakeRouter）
- 不改 `web_ui/config_helpers.py` 的 import 源

观察期只做**标记 + 记录**，不做结构改动，保留回滚余地。

## 5. 清理清单（观察期结束后）

一次 commit 一个改动，按序提交：

| Step | 动作 | 文件 | 回滚难度 |
|------|------|------|----------|
| 1 | 确认 `model_configs` 用途，迁或删 `process.py:2158-2160` | `src/pipeline/process.py` | 低（独立改动） |
| 2 | 删 translator 的 legacy path（含 `[LLM-ROUTER-LEGACY]` print） | `src/services/gemini/translator.py:1028-1068` | 低 |
| 3 | 删 translator 构造参数 `llm_router` | `src/services/gemini/translator.py:263, 277` | 中（测试 fixture 会改）|
| 4 | 删 `process.py:578 / 807` 的 router 构造和 segmenter 注入 | `src/pipeline/process.py` | 低 |
| 5 | 删 `process.py:73` import `LLMRouter, load_llm_fallback_config` | `src/pipeline/process.py` | 低 |
| 6 | 迁 `web_ui/config_helpers.py:17-18, 240-245` 的 `DEFAULT_AUTODUB_LOCAL_CONFIG_PATH` import 源到 `services.config_loader` | `src/services/web_ui/config_helpers.py` | 低 |
| 7 | 删 `src/services/llm/__init__.py` 对 `LLMRouter` / `load_llm_fallback_config` 的导出 | `src/services/llm/__init__.py` | 低 |
| 8 | 整组删除 `tests/test_llm_router.py`（12+ 测试） | `tests/test_llm_router.py` | 低 |
| 9 | 改造 `tests/test_gemini_translator.py` 的 `FakeRouter` fixture — 改 mock `llm_registry.get_prompt_model` | `tests/test_gemini_translator.py` | 中 |
| 10 | 改造 `tests/test_process_pipeline.py` 的 `llm_router=None / is not None` 断言 — 改查 `_service_mode` | `tests/test_process_pipeline.py` | 中 |
| 11 | 归档 `src/services/llm/router.py` 本体（git rm） | `src/services/llm/router.py` | 低（有 git history 可回复） |

预估 CC 执行工时：1-1.5 小时（含验证测试绿）。

## 6. 观察记录

| 检查日期 | 命中次数 | grep 命令输出 | 备注 |
|----------|---------|--------------|------|
| 2026-04-17 | — | _（观察起点，标记刚加，需要下次部署后生效）_ | 部署 commit SHA：857cb46 |
| 2026-04-24 | — | _（漏检）_ | 单人开发，观察日志 §6 未维护 |
| 2026-05-01 | **N/A** | container 2026-05-01 06:17 recreate → docker logs 丢失 | **第一轮观察作废**；plan §1 漏映射 s2_review 一并发现 |
| 2026-05-02 | — | _（第二轮观察起点；audit log 落 runtime_logs/llm-router-legacy.log，bind mount 持久）_ | 部署：translator.py 加 s2_review 映射 + persistent audit log |
| 2026-05-09 | ? | ? | ? |
| 2026-05-16 | ? | ? | 决策节点（第二轮） |

## 7. 非目标

- 不改翻译/审校的正确性逻辑（观察期零行为改动）
- 不提前做 Gap 2 / Gap 3 的修复（见 `2026-04-09-prompt-model-management-plan.md` §0）
- 不新开"LLMRouter 替代方案"——已经有 `llm_registry` 完成替代

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 标签没接入部署环境就做了 grep | 部署后先跑 1 个测试 job（任意 service_mode），确认日志里能看到 `[LLM] ... using ...`（llm_registry 新路径）或 `[LLM-ROUTER-LEGACY]`（legacy 命中）中任一其一——说明 logging 通路 OK |
| 观察期跨越真人放假 / 低流量期 | 2026-04-17 → 2026-05-01 含五一假期前 3 天，流量可能偏低。若命中数据疑似"假阴性"（流量太低），延期 1 周继续观察 |
| 某个 task 命中后修 map 但再次忘记 / 误删 | Step 9/10 的测试改造中加一条 assert：所有 translator 被调用的 task 都在 `prompt_key_map` 里。相当于"永久回归守卫" |
| `model_configs` 读取实为 metering 的一部分（影响账单口径） | Step 1 单独做，查完用途再动。不和路由下线混在一个 commit |

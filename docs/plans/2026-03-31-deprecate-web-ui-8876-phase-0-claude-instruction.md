# Web UI API (8876) 迁移 Phase 0 Claude 指令

> **用途：** 这是当前轮次可直接发给 Claude 的 Phase 0 执行指令。  
> **对应计划：** [2026-03-31-deprecate-web-ui-8876-migration-plan.md](/D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-03-31-deprecate-web-ui-8876-migration-plan.md)  
> **注意：** 仅适用于 Phase 0。本文件不约束后续阶段；后续阶段指令需根据本轮执行结果重新编写。

## 可直接发送给 Claude 的内容

```md
请只执行迁移计划中的 Phase 0。

计划文档：
`D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-03-31-deprecate-web-ui-8876-migration-plan.md`

本阶段目标：
- 冻结本次迁移的真实活跃 surface
- 清理会直接阻塞迁移推进的测试基线问题

本阶段只允许做这些事：
1. 核对 8876 当前真实活跃 surface，必要时更新计划文档中的盘点表与基线说明。
2. 只处理会阻塞本迁移推进的基线测试债，至少覆盖：
   - `tests/test_main_cli.py` 中对 `_shutdown_cli_tts_runtimes` 的过时预期
   - `tests/test_job_api.py::test_job_api_continue_reuses_existing_review_semantics` 的 flaky 或稳定性问题
3. 在计划文档中写清楚：
   - 冻结的预存失败列表
   - 后续阶段统一使用的验证命令

本阶段明确禁止：
- 不新增任何新的业务端点
- 不改前端 API 调用路径
- 不动 Gateway 路由
- 不开始清理 8876 runtime wiring
- 不进入 Phase 1

执行要求：
- 以最小改动为原则
- 如果发现新的真实活跃调用，可以补充到计划盘点，但不要直接实现新的迁移范围
- 完成后必须停止

请严格按下面格式回传：

## Phase 0 Completion Report

### 1. Scope Completed
- ...

### 2. Files Changed
- `path/to/file`: 一句话说明改动目的

### 3. Baseline Decisions
- Frozen failures:
  - ...
- Shared verification commands:
  - `command`

### 4. Verification
- `command`
  - result
- `command`
  - result

### 5. Explicitly Deferred
- ...

### 6. Risks / Open Questions
- ...

### 7. Ready For Review
- `READY_FOR_REVIEW`
- Suggested next phase: `Phase 1`

如果被阻塞，则改用：

## Phase 0 Blocked Report

### Blocker
- ...

### Evidence
- `path/to/file`
- `command`
  - result

### Options
1. ...
2. ...

### Recommendation
- ...
```

# AI Workgroup Protocol

> 状态：当前启用  
> 作用：为 `Trae`、`Claude Code`、`CodeX` 与 `项目开发者` 提供统一的异步文件协作协议。  
> 范围：仅用于方案、指令、汇报、阻塞与审批沟通；不替代仓库中的正式计划文档与代码评审流程。

## 1. 目标

本协议的目标是把“多个 AI 工具之间的来回转述”收敛为一个可审计、可追踪、可人工介入的文件通信系统。

核心原则：

- 以 `CodeX` 为中枢，采用星型拓扑。
- 以 Markdown 为消息载体，便于人工检查与长期留档。
- 任何进入仓库的最终代码，仍由 `Claude Code` 负责实现或收口。
- 任何涉及业务边界、价格、Trial、支付、迁移的事项，仍需 `项目开发者` 拍板。

---

## 2. 角色定义

- `Trae`：负责页面结构、产品文案、Stitch prompt、营销和前端表达层建议。
- `Claude Code`：负责最终代码实现、测试、迁移、接口与阶段汇报。
- `CodeX`：负责审核边界、核验结果、拦截跑偏、转发任务与生成下一步指令。
- `项目开发者`：负责价格、Trial、风控、支付顺序、优先级与是否进入下一阶段的最终决定。

一句话：

**Trae 提建议，项目开发者拍板，Claude Code 写代码，CodeX 控节奏。**

---

## 3. 目录结构

```text
AI-workgroup/
  00-protocol.md
  01-index.md
  inbox/
    CodeX/
    Claude-Code/
    Trae/
    Human/
  working/
    CodeX/
    Claude-Code/
    Trae/
  done/
  archive/
  shared/
```

目录职责：

- `inbox/<role>/`：发给某一方、等待处理的消息。
- `working/<role>/`：该方已领取、正在处理的消息。
- `done/`：已完成、待归档的消息。
- `archive/`：已关闭的历史消息。
- `shared/`：公共参考资料、模板、上下文文档。
- `01-index.md`：人工总览页，记录当前活跃任务与最新状态。

---

## 4. 拓扑规则

默认采用星型拓扑：

- `Trae -> CodeX`
- `Claude Code -> CodeX`
- `CodeX -> Trae`
- `CodeX -> Claude Code`
- `CodeX -> Human`
- `Human -> CodeX`

不建议直接通信：

- `Trae <-> Claude Code`
- `Trae -> Human` 的执行性要求
- `Claude Code -> Trae` 的直接分派

理由：

- 所有跨代理沟通都经过 `CodeX` 审核。
- 统一边界，减少 scope 漂移。
- 任何需要 `项目开发者` 确认的事项统一汇总到 `Human` 收件箱。

---

## 5. 文件命名规范

所有消息文件必须使用以下格式：

```text
YYYY-MM-DD_HHMMSS_from-<sender>_to-<receiver>_type-<type>_task-<taskid>_<slug>.md
```

示例：

```text
2026-04-05_213000_from-CodeX_to-Claude-Code_type-instruction_task-T0_plan-catalog.md
2026-04-05_214500_from-Claude-Code_to-CodeX_type-report_task-T0_plan-catalog.md
2026-04-05_215000_from-CodeX_to-Human_type-decision_task-T0_public-api-scope.md
```

字段约束：

- `sender` / `receiver`：`CodeX`、`Claude-Code`、`Trae`、`Human`
- `type`：`instruction`、`report`、`review`、`decision`、`blocker`、`ack`
- `taskid`：如 `T0`、`T1`、`M-A-T2`
- `slug`：2-6 个英文单词，用短横线连接

---

## 6. Front Matter 规范

每个消息文件开头都应包含 front matter：

```md
---
id: T0-msg-003
task: T0
from: CodeX
to: Claude-Code
type: instruction
status: ready
priority: high
reply_to: T0-msg-001
requires_human: false
created_at: 2026-04-05 21:30 Asia/Shanghai
---
```

推荐字段：

- `id`：消息唯一 ID
- `task`：所属任务 ID
- `from`：发送者
- `to`：接收者
- `type`：消息类型
- `status`：消息状态
- `priority`：`high` / `medium` / `low`
- `reply_to`：回复哪条消息；没有则留空
- `requires_human`：是否需要项目开发者确认
- `created_at`：时间戳，使用 `Asia/Shanghai`

---

## 7. 状态流转规则

推荐状态枚举：

- `ready`：新消息，待处理
- `claimed`：已领取，处理中
- `waiting-human`：等待项目开发者拍板
- `done`：已完成
- `archived`：已归档

标准流转：

1. 发送方把文件写入 `inbox/<receiver>/`
2. 接收方开始处理时，将其移动到 `working/<receiver>/`，并将 `status` 更新为 `claimed`
3. 处理结束后，产出回复文件到 `inbox/<next-receiver>/`
4. 原文件可移动到 `done/`
5. 周期性或阶段性整理时，再移动到 `archive/`

---

## 8. 内容结构规范

消息正文建议采用固定结构：

```md
# 标题

## 背景
- ...

## 请求 / 结论
- ...

## 约束
- ...

## 需要回复的点
1. ...
2. ...

## 附件 / 参考
- ...
```

如果是不同类型消息，至少满足：

- `instruction`：要写清任务、范围、禁止项、验证方式
- `report`：要写清执行结果、测试结果、风险、是否停止
- `decision`：要写清需要项目开发者确认的具体问题
- `blocker`：要写清当前阻塞、已做尝试、建议选项

---

## 9. 批准与升级规则

以下事项必须升级到 `Human`：

- 最终价格变更
- Trial 时长与额度
- Free / Plus / Pro 的对外口径
- 风控阈值
- 支付渠道顺序
- 微信自动续费是否进入本阶段
- Team / Enterprise 是否进入主线

以下事项必须先经过 `CodeX`，不能由其他代理自行决定：

- 计划边界调整
- API 路径变更
- 迁移顺序调整
- 测试基线口径调整

---

## 10. 推荐工作流

标准流程：

1. `Trae` 产出建议，写入 `inbox/CodeX/`
2. `CodeX` 审核后：
   - 转发给 `Claude Code`
   - 打回 `Trae`
   - 或升级到 `Human`
3. `Claude Code` 执行后，把汇报写入 `inbox/CodeX/`
4. `CodeX` 审核后决定：
   - 放行
   - 修订
   - 升级到 `Human`

---

## 11. 自动化建议

当前协议默认按**半自动**方式运行：

- AI 工具在被明确提示时读取目录并处理消息
- `项目开发者` 或 `CodeX` 负责触发下一步

注意：

- 本协议本身不等于“自动常驻多 agent 系统”
- 是否支持定时轮询，取决于具体 AI 工具
- 在不确定其他工具是否支持后台监听前，建议先按“手动触发 + 文件协议”运行

---

## 12. 启用规则

启用本协议时，所有参与方都应遵守：

- 不擅自跳过 `CodeX`
- 不擅自修改文件命名规则
- 不把草案建议直接当成已批准执行稿
- 不把消息目录当成正式计划文档的替代品

本协议的用途是：

**让协作更顺畅，而不是绕过审核。**


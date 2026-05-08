# 客服坐席 Playbook（P1 / P2 邮箱工单阶段）

P1 阶段没有部署 Chatwoot。所有人工工单通过运营邮箱接收（默认
`sxz999@proton.me`，可在 admin/support 修改）。

## 上线开关清单（重要）

`/admin/support` 页面有 3 个独立开关，按需组合：

| 场景 | 启用客服系统 | 允许未登录访客咨询 | 启用真实 AI |
| --- | --- | --- | --- |
| 完全关闭客服 | ❌ | ❌ | ❌ |
| 只给登录用户用客服（保守上线） | ✅ | ❌ | ❌ |
| **完整售前售后**（推荐生产配置） | ✅ | ✅ | ❌ |
| 同上，且开启真实 AI（需确认预算） | ✅ | ✅ | ✅ |

**售前用户咨询必须同时打开前两个开关**：

- 只开「启用客服系统」、不开「允许未登录访客咨询」时，营销页（首页 / 定价页 / 联系页）
  对未登录访客**不显示客服浮窗**——这是 Codex round-3 加的 UX 保护，避免访客点开
  浮窗发消息后才吃 401。
- 已登录用户始终看到浮窗，不受匿名开关影响。
- 默认全部 OFF（Codex P2-1）。新部署需要运营进 admin 显式打开。

## 真实 AI 开关说明

打开「启用真实 AI」+ 选中模型（默认 deepseek）**当前不会真的调用 DeepSeek**：
P1 的 `_IMPLEMENTED_REAL_PROVIDERS` 集合为空，所有真实 provider 路径会安全降级
到 fake provider。这是为了避免在 wiring 没审完前误触发付费 API。

要真正接入 DeepSeek：

1. 实现 `gateway/support_ai.py:DeepseekProvider.reply()`（HTTP 调用 + 结构化输出）。
2. 把 `"deepseek"` 加入 `_IMPLEMENTED_REAL_PROVIDERS`。
3. 同时更新 `tests/test_support_codex_round2.py::test_implemented_real_providers_is_empty_in_p1`。
4. 在 admin 重新打开「启用真实 AI」并确认月度预算。

## 接到工单

工单从两个地方入：

1. **运行日志**：`runtime_logs/support_handoff_email.log`（gateway 容器内 bind-mount 到主机）。每条 JSONL 一行，包含会话 ID、用户上下文、最近 N 条消息。
2. **运营邮箱**：每次 handoff 创建会发一封邮件（P1 阶段先写日志，待 SMTP 接入后真实发送）。

## 处理步骤

1. 在 admin/support 页面「人工工单」找到对应行。
2. 阅读 `summary` + 最近 5 条消息，理解用户问题。
3. 通过邮件直接回复用户（用户邮箱在工单 `provider_payload` 里）。
4. 处理完成后在 admin/support 点「关闭」 → 写入 `support_handoff_requests.status='closed'`。
5. 用户下次进入站内会看到「客服已回复」通知（前提：用户登录态）。

## 不要做

- 不要在邮件里贴出 `project_dir` / `manifest_path` 等内部路径。
- 不要承诺超过自己权限的事（例如「24h 内一定退款」）。
- 不要把别的用户的任务 ID 写到回复里。

## 升级路径

- 退款 / 发票相关：转给负责账务的同事。
- 版权 / 侵权：转给法务或暂时挂起，等法务回复。
- 用户情绪激烈：先冷静回应，避免在邮件里争论；必要时先承认问题再细化解决方案。

## P3 时切换到 Chatwoot

- 部署 Chatwoot 自托管。
- 把 `AVT_CHATWOOT_*` env 填好。
- 在 admin/support 把「默认人工通道」从 email 切到 chatwoot。
- 历史邮件工单不迁移，新工单走 Chatwoot。
- 详细切换流程见 `chatwoot-handoff.md`（待 P3 落地时编写）。

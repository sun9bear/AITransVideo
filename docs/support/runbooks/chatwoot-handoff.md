# Chatwoot Handoff Runbook（P3 stub）

本文档在 P3 部署 Chatwoot 时填实。当前 P1 阶段仅记录待办：

- [ ] 部署 Chatwoot 自托管（独立 Postgres / Redis）。
- [ ] 配置 Caddy 反代到 `support.{public_host}`。
- [ ] 创建 inbox / 坐席 / 标签 / 自动回复。
- [ ] 生成 API token，写入 admin_settings.json 的 support 段。
- [ ] 把 `gateway/support_adapters/chatwoot.py` 从 stub 替换成真实 httpx 调用。
- [ ] 把 admin/support 页面的「默认人工通道」改成 chatwoot 时验证 fallback 仍指向 email。
- [ ] 配置 webhook 接收 Chatwoot 关闭会话事件，同步 `handoff_state=closed`。
- [ ] 测试 R2 / 用户上下文 / AI 摘要在 Chatwoot 后台可读。

参考：plan §9.2 / §13 P3。

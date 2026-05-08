# 微信客服接入 Runbook（P4 / P5 stub）

P4 范围（轻入口）：

- [ ] 在企业微信申请微信客服账号。
- [ ] 拿到客服账号链接 / 二维码。
- [ ] 在 admin/support 配置 `support_wechat_kf_url`（P4 字段，P1 暂未实现）。
- [ ] 在联系页 / 页脚 / SupportWidget 的「人工」按钮里展示该链接。
- [ ] 通过 scene 参数携带来源（`source=pricing` / `source=billing` / `job_id_hash=...`）。
- [ ] 工作时间 / 预期响应方式说明。

P5 范围（API adapter）：

- [ ] 实现回调验签 + 消息加解密（参考企业微信 / 微信客服文档）。
- [ ] access token 管理（缓存 + 刷新）。
- [ ] 客服账号链接生成接口。
- [ ] 消息收发 adapter，把微信消息映射到 `support_conversations`。
- [ ] 决定主后台：微信会话最终归口到 Chatwoot 还是邮箱工单？P0 决策应已锁定。
- [ ] 把 `gateway/support_adapters/wechat_kf.py` 从 stub 替换成真实实现。

风险：

- PC 网站跳微信链路会中断站内上下文；展示二维码时附 scene 参数，让坐席能识别来源。
- 微信 API 变更频繁；接入后需要订阅企业微信变更日志。

参考：plan §9.4 / §13 P4 / §13 P5。

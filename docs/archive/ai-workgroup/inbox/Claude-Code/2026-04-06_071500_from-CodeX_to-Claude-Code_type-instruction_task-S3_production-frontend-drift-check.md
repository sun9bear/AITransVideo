---
id: S3-msg-001
task: S3
from: CodeX
to: Claude-Code
type: instruction
status: pending
priority: high
reply_to: S2
requires_human: false
created_at: 2026-04-06 07:15 Asia/Shanghai
---

# S3 Production frontend drift check

## 1. 背景

CodeX 直接核查线上 `https://aitrans.video/` 后发现：

- `/` 仍自动跳转到 `/auth/login?from=%2F`
- 页面仍显示旧版邮箱登录页视觉
- 与当前仓库已完成并放行的 `T1 / T2 / P1` 前端基线不一致

按当前仓库基线，生产首页应为 marketing 首页，不应默认跳登录。

因此，本次任务不是实现新功能，而是确认 **production frontend drift**：

- 生产前端是否仍停在 pre-T1 / pre-T2 构建
- 是否有旧 redirect / proxy / nginx / caddy / middleware 规则覆盖了当前前端路由
- 是否存在部署同步不完整、前后端版本不一致的问题

## 2. 本次任务目标

只做生产前端漂移排查，不做大范围修复。

你需要确认：

1. `aitrans.video` 生产前端当前运行的是哪一版代码
2. 为什么 `/` 仍跳到 `/auth/login`
3. 是代码未部署、容器未更新、构建未替换，还是代理层仍有旧规则
4. 如果问题已明确，给出最小修复建议

## 3. 明确范围

### 允许做的事

- 只读检查生产环境前端部署状态
- 检查：
  - 前端容器/镜像/构建版本
  - Next 构建产物
  - 反向代理（Caddy / nginx / compose）对 `/` 的处理
  - 是否存在旧静态产物、旧容器、旧 redirect 规则
- 如有必要，可在生产环境执行 **只读命令**
- 如有必要，可在本地对照当前仓库：
  - `frontend-next/src/app/page.tsx`
  - `frontend-next/src/app/(marketing)/page.tsx`
  - `frontend-next/src/middleware.ts`
  - 生产部署相关 compose / caddy 配置

### 本次禁止

- 不要直接改生产代码
- 不要直接重启或重建生产容器
- 不要直接改 Caddy / nginx / compose
- 不要直接做生产发布
- 不要顺手处理 staging
- 不要扩展到短信、支付、billing、gateway 其他问题

如果你确认了原因，也先停在“诊断 + 建议”，不要直接修。

## 4. 必读文件

1. `D:/Claude/AIVideoTrans_Codex_web_mvp/AGENTS.md`
2. `D:/Claude/AIVideoTrans_Codex_web_mvp/docs/plans/2026-04-03-frontend-auth-billing-pricing-implementation-plan-v2.md`
3. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/page.tsx`
4. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/page.tsx`
5. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(marketing)/layout.tsx`
6. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/app/(auth)/auth/login/page.tsx`
7. `D:/Claude/AIVideoTrans_Codex_web_mvp/frontend-next/src/middleware.ts`
8. `D:/Claude/AIVideoTrans_Codex_web_mvp/Caddyfile`

## 5. 建议排查路径

建议按这个顺序查：

1. 先确认当前仓库预期行为
   - `/` 应该渲染什么
   - 哪些路径应公开可访问
   - 是否还有任何仓库内 redirect 会把 `/` 导向 `/auth/login`

2. 再查生产环境
   - 生产前端容器/镜像时间戳
   - 是否仍在运行旧构建产物
   - 生产 Caddy / nginx / compose 是否有旧 rewrite / redirect
   - 是否 front container / gateway container 版本错位

3. 最后给出归因
   - 代码未部署
   - build 未替换
   - proxy 层旧规则
   - 多实例/多容器版本不一致
   - 其他

## 6. 输出要求

请写回 `inbox/CodeX` 一封 report，重点包含：

1. 生产 `/` 当前为何跳登录
2. 生产 `/auth/login` 为什么仍是旧视觉
3. 漂移发生在：
   - 代码
   - 构建产物
   - 容器/镜像
   - 代理层
   - 或其组合
4. 你是否确认生产还没部署 `T1 / T2 / P1`
5. 下一步最小修复建议
6. 你本次是否对生产环境做了任何写操作

## 7. 成功标准

本次任务成功，不是“线上修好”，而是：

- 已明确 production drift 的根因
- 已区分是前端部署漂移还是代理层旧规则
- 已给出下一步最小修复建议
- 未越界直接改生产

## 8. 停止条件

如果你已经能明确回答：

- 为什么 `/` 跳 `/auth/login`
- 为什么线上还是旧 login 页
- 最小修复动作是什么

就停止，并等待 CodeX 审核。

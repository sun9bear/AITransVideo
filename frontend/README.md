# AIVideoTrans Frontend Shell (已废弃 — 归档参考)

> **⚠️ 此目录已废弃，不应再运行。**
>
> - 当前活跃前端为 **`frontend-next/`**（Next.js 16 + React 19）
> - Web UI 独立服务 (8876) 和 `/web-ui-api/*` 路径已在 Phase 4 下线
> - 所有 API 请求现通过 Gateway (8880) 路由到 Job API (8877)
> - 上传走 `/gateway/upload-video`（Gateway-native）
>
> **请勿使用此目录中的命令或配置启动服务。**

---

以下内容仅作历史参考，记录旧 Vite 前端的原始结构。

## 原始目录结构

```text
src/
  app/          # 入口层、路由装配、全局布局
  routes/       # 页面路由模块
  components/   # 共享 UI 骨架组件
  features/     # 页面级占位数据与 feature 组装层
  lib/api/      # fetch client、endpoint 包装、API -> domain mapper
  types/        # API 类型与受控 domain 类型
  styles/       # Tailwind 入口与全局样式
```

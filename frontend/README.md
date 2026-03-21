# AIVideoTrans Frontend Shell

单用户 Web Console MVP 的前端工程壳，当前只覆盖首批 3 个页面的结构准备：

- 新建翻译
- 当前任务
- 项目详情

## Commands

```bash
npm install
npm run dev
```

默认开发地址：

- `http://127.0.0.1:4173`

生产构建：

```bash
npm run build
```

静态检查：

```bash
npm run lint
npm run typecheck
```

## Environment

复制或参考 [`.env.example`](./.env.example)：

```bash
VITE_APP_BASE_PATH=/
VITE_JOB_API_BASE_URL=/job-api
VITE_WEB_UI_BASE_URL=/web-ui-api
```

本地人工试用时建议同时启动当前基线后端：

```bash
python main.py job-api
python main.py web-ui
```

开发态默认通过 Vite 代理：

- `/job-api/*` -> `http://127.0.0.1:8877/*`
- `/web-ui-api/*` -> `http://127.0.0.1:8876/*`

## Structure

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

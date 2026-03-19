# LINUX_MIGRATION_SCOPE.md

# AIVideoTrans Linux 迁移阶段范围定义

## 1. 阶段唯一主题

本阶段唯一主题是：

**Linux 单机迁移与部署标准化**

目标不是继续扩张产品功能，也不是推进前端产品化，而是把当前已经收口的 **Windows 单机自用远程网页工作台稳定基线**，迁移成一个：

- Linux 单机
- 长期在线
- 可标准化部署
- 可重复启动/停止/恢复
- 可继续承载后续商业化演进

的运行基线。

---

## 2. 当前迁移前提

当前已经完成并冻结的稳定基线是：

- Windows 单机
- 自用远程网页工作台
- HTTPS + Basic Auth + Web UI 公网入口
- Job API 仅本机 / 内网
- `youtube_url -> review continue -> result-summary -> 白名单下载` 真实闭环已验收通过
- 当前仍是：
  - `single-active-job`
  - `youtube_url only`
  - `process-backed`
  - 无 `cancel`
  - 非 Linux 完成态
  - 非多用户
  - 非完整 Web MVP
  - 非商业化完成态

Linux 迁移必须尽量保持这些外部行为不变。

---

## 3. 本阶段目标

本阶段要完成的事情只有这些：

1. 固定 Linux 目标 OS 与部署边界
2. 形成标准化运行方式
3. 固定服务拓扑与入口边界
4. 在 Linux 目标机上复现当前最小闭环
5. 形成 Linux 环境下的可运维最小基线

---

## 4. 本阶段明确做什么

### 4.1 部署基线
- 确定 Linux 目标发行版
- 确定目录结构
- 确定端口边界
- 确定日志目录
- 确定环境变量契约

### 4.2 服务标准化
- 明确 app / caddy 服务职责
- 明确启动 / 停止 / 重启方式
- 明确是否使用 Docker / Compose / systemd
- 明确最小健康检查路径

### 4.3 真实验收
- 在 Linux 主机上跑通一条真实 `youtube_url` 任务
- 验证 review continue
- 验证 result-summary
- 验证白名单下载
- 验证 Web UI 公网入口
- 验证 Job API 仍不公网暴露

---

## 5. 本阶段明确不做什么

### 5.1 不做产品扩张
- 不做前端产品化改版
- 不做 Web MVP
- 不做 Skill
- 不做商业化
- 不做多用户

### 5.2 不做执行底座重构
- 不做 workflow-backed official execution
- 不做大范围 process 重构
- 不做 review / voice_review 语义重写

### 5.3 不做基础设施扩张
- 不做 Kubernetes
- 不做多机部署
- 不做数据库
- 不做多 worker
- 不做队列系统

### 5.4 不做功能扩张
- 不做 `cancel`
- 不扩 source type
- 不做完整结果中心
- 不做完整目录浏览

---

## 6. 阶段验收标准

只有同时满足以下条件，才算 Linux 迁移阶段完成：

1. Linux 主机可重复部署
2. 服务可标准启动 / 停止 / 重启
3. Web UI 可通过 HTTPS 访问
4. Job API 仍不公网暴露
5. 真实 `youtube_url` 闭环可跑通
6. review continue 可用
7. result-summary 可用
8. 白名单下载可用
9. 有日志与最小恢复路径
10. 不依赖手工临时操作才能长期运行

---

## 7. 与当前稳定基线的关系

Linux 迁移阶段不是推翻当前 Windows 稳定基线，而是：

**以当前 Windows 稳定基线为行为参照，构建 Linux 上的等价运行基线。**

迁移阶段优先级高于前端产品化。  
在 Linux 迁移完成前，不建议继续投入大量前端开发。

---

## 8. 一句话边界

**本阶段只做 Linux 单机迁移与部署标准化，不做功能扩张，不做产品化，不做执行内核重构。**

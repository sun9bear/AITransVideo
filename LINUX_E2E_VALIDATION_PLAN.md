# LINUX_E2E_VALIDATION_PLAN.md

# AIVideoTrans Linux 端到端验收计划

## 1. 验收目标

本计划用于验证：

当前已在 Windows 上收口的最小闭环，是否能够在 Linux 目标机上以标准化部署方式真实跑通。

本阶段只验收：

- Linux 单机
- Web UI HTTPS 入口
- Job API 内部访问
- 一条真实 `youtube_url` 闭环
- review continue
- result-summary
- 白名单下载

---

## 2. 验收前置条件

在开始真实验收前，必须满足：

1. Linux 主机已完成基础部署
2. app service 已可启动
3. caddy service 已可启动
4. HTTPS 入口可访问
5. Basic Auth（如保留）可生效
6. Job API 未直接公网暴露
7. 数据目录、日志目录可写
8. 必要环境变量已配置
9. YouTube 访问链路可用
10. 目标主机具备运行当前任务的依赖环境

---

## 3. 验收路径

## Step 1：入口验证
- 访问 HTTPS 入口
- 未认证访问应得到认证挑战（如保留 Basic Auth）
- 认证后应进入 Web UI 首页

## Step 2：任务提交
- 提交一条真实 `youtube_url`
- 记录任务创建时间与任务标识
- 确认当前任务页可看到状态变化

## Step 3：运行中观察
- 查看状态流转
- 查看关键日志
- 记录是否进入 review gate

## Step 4：review continue
如果任务进入 review：
- 在 Web UI 中完成一次真实 approve / continue
- 确认任务沿同一 job/project 语义继续
- 确认不会错误分叉成新任务

## Step 5：结果验证
任务成功后，确认：
- result-summary 可读
- manifest 可用
- outputs/artifacts 摘要可见

## Step 6：白名单下载验证
至少验证 1~2 个关键产物可下载，例如：
- `manifest.file`
- `translation.segments`
- `editor.subtitles`
- `publish.dubbed_video`（如存在）

---

## 4. 关键验收点

必须重点确认：

### 4.1 入口边界
- Web UI 可通过 HTTPS 访问
- Job API 未被公网暴露

### 4.2 任务闭环
- 真实任务可提交
- 状态可读
- 日志可读
- review continue 可用
- 成功后可查看结果

### 4.3 结果边界
- 白名单下载可用
- 不出现任意路径下载
- 结果仍来自 manifest-derived surface

### 4.4 日志与诊断
- runtime logs 可查看
- access logs 可查看
- 失败时能定位服务/任务问题

---

## 5. 失败时必须记录的信息

如果任一环节失败，必须至少记录：

- 失败发生的步骤
- 失败时间
- 相关服务日志位置
- 相关 access log 片段
- 当前任务状态
- 当前 project_dir / job_id（如可获得）
- 是否与入口、任务、review、结果或下载相关

---

## 6. 通过标准

只有以下都满足，才算 Linux 验收通过：

1. HTTPS 入口可用
2. Job API 不公网暴露
3. 真实 `youtube_url` 提交成功
4. 状态与日志可读
5. 至少一次真实 review continue 成功（如果该任务触发了 review）
6. result-summary 可读
7. 至少 1~2 个白名单产物下载成功
8. runtime/access logs 可用于诊断
9. 无需手工临时修补才能持续运行

---

## 7. 明确不做什么

- 不在验收中顺手扩张功能
- 不做前端改版
- 不做 Linux/Windows 双活
- 不做多用户
- 不做 Skill
- 不做商业化
- 不做完整结果中心
- 不做执行底座重构

---

## 8. 一句话验收标准

**Linux 迁移通过 = 在 Linux 单机上，以标准化部署方式，真实跑通一条 `youtube_url -> review continue -> result-summary -> 白名单下载` 闭环。**

# Web Console 文案与命名口径

## 页面标题

- `/translations/new`：新建翻译
- `/tasks/current`：当前任务
- `/projects`：我的项目
- `/projects/:jobId`：项目详情
- `/reviews/:jobId/speaker`：说话人审核
- `/reviews/:jobId/translation`：翻译审核
- `/reviews/:jobId/voice`：音色确认
- `/settings`：设置说明

## 状态 / 阶段 / 审核文案

- 状态统一为：待开始、处理中、等待审核、已完成、已失败、已取消
- 阶段统一为：输入准备、媒体理解、说话人审核、翻译审核、音色确认、草稿与配音、输出完成、处理失败
- 审核相关统一为：请先处理审核、确认并继续、审核已提交、打开旧版审核页
- 空态 / 错误态尽量直接说明当前页面是否“没有内容”或“无法打开”，不再混用工程提示

## 下载命名

- 成品视频
- 完整配音音频
- 字幕文件
- 翻译分段（JSON）
- 项目清单（JSON）

## 按钮文案

- 新建翻译
- 查看当前任务
- 返回当前任务
- 查看项目详情
- 确认并继续
- 确认音色并继续
- 下载文件
- 暂不可下载
- 打开旧版审核页

## 不再直接展示给最终用户的内部术语

- `job_id`
- `project_dir`
- `manifest_path`
- `legacy_process_output`
- `review_gate`
- `fallback_summary`
- 直接把旧 `Web UI` 作为主入口提示

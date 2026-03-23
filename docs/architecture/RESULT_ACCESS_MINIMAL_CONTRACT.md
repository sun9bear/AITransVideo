# RESULT_ACCESS_MINIMAL_CONTRACT

Last updated: 2026-03-18

## 结果摘要最小结构

下一阶段的最小结果摘要应继续对齐当前 A3 的 manifest-derived 语义，至少包含：

```json
{
  "job_id": "...",
  "status": "...",
  "project_dir": "...",
  "manifest_path": "...",
  "review_gate": {},
  "error_summary": {},
  "fallback_summary": {},
  "manifest": {
    "available": true,
    "artifact_count": 0
  },
  "outputs": [],
  "artifacts": {
    "total_count": 0,
    "existing_count": 0,
    "categories": []
  }
}
```

说明：

- `result-summary` 继续作为轻量摘要层。
- `artifacts` 继续作为 manifest-derived listing。
- 结果真相源继续是 `manifest.json`，不是 `JobRecord`。

## 允许下载的关键产物范围

当前阶段只建议开放少数白名单对象：

- `manifest.file` -> `manifest.json`
- `translation.segments`
- `editor.subtitles`
- `editor.dubbed_audio_complete`
- `publish.dubbed_video`

补充约束：

- 只有当对象在 manifest 中可解析且文件实际存在时，才允许下载。
- 不是每个任务都会同时拥有这些对象。

## 下载对象如何由 manifest-derived stable keys 决定

- 下载资格必须来自 `manifest.json` 的 artifact index。
- 应以稳定 key 白名单作为唯一入口，而不是以前端传任意路径。
- 解析顺序应保持为：
  - 先读取 manifest
  - 再从稳定 key 找到目标 artifact
  - 再解析到本机文件路径
  - 最后确认文件存在
- `JobRecord` 只提供 `project_dir` / `manifest_path` 等定位信息，不提供新的下载真相源。

## 为什么不做完整目录浏览

- 当前项目目录里既有最终产物，也有大量中间文件和审校状态文件。
- 这些内容对公网远程工作台来说既不稳定，也不是当前阶段必须暴露的体验。
- 当前仓库也没有为“安全目录浏览器”做专门边界设计。

## 为什么不做任意路径下载

- 当前路径是 Windows 主机上的真实本地路径。
- 如果允许任意路径下载，就会把“结果访问”变成“主机文件读取”风险。
- 对这个仓库来说，最安全也最可控的方式就是：
  - 只允许 manifest-derived
  - 只允许 stable key 白名单
  - 只允许少数关键产物

## 明确不做什么

- 不做完整项目目录浏览器
- 不做任意路径下载
- 不做结果中心大页面
- 不做打包所有产物的一键归档下载
- 不暴露 `state.project`
- 不暴露 `state.review`
- 不把 artifacts / result-summary 持久化进 `JobRecord`

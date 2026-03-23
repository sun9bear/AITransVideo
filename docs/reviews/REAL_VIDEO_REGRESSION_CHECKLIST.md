# 真实视频回归检查表

建议每跑完一个真实视频，就按这张表检查一次。

建议使用 UTF-8 编码打开本文件。

## 一、运行信息

- 样本名称：
- 日期：
- 输入类型：`YouTube / local_audio / local_video`
- 使用入口：`web-ui / process / local-audio-demo / local-video-demo`
- 运行模式：`mock / real`
- 是否修改过自定义 prompt：`是 / 否`
- 备注：

## 二、是否跑通

- [ ] 已生成项目目录
- [ ] 主流程没有异常中断
- [ ] 已生成 `project_state.json`
- [ ] 核心 stage 状态符合预期

重点检查这些 stage：

- `ingestion`
- `media_understanding`
- `translation`
- `chunking`
- `alignment`
- `draft`

## 三、核心产物检查

确认以下文件存在且可打开：

- [ ] `output/dubbed_audio_complete.wav`
- [ ] `output/subtitles.srt`
- [ ] `output/alignment_report.md`
- [ ] `output/segments/`
- [ ] `draft/draft_content.json`
- [ ] `draft/draft_meta_info.json`
- [ ] `draft/jianying_like_export.json`

如果本次启用了 publish，再额外确认：

- [ ] `publish/dubbed_video.mp4`

## 四、音频质量检查

- [ ] `dubbed_audio_complete.wav` 可以正常播放
- [ ] 没有大段静音
- [ ] 没有明显爆音、破音、截断
- [ ] 整体时序大致跟原视频一致
- [ ] 整体结果适合继续人工精修

音频主观评价：

- `好 / 可用待修 / 不可用`

## 五、字幕检查

- [ ] `subtitles.srt` 可以正常打开
- [ ] 字幕时间轴整体跟配音一致
- [ ] 没有大面积提前或滞后
- [ ] 没有明显空字幕、重复字幕、长时间不切换
- [ ] 字幕文本整体接近最终说出口的中文

字幕时序评价：

- `正常 / 轻微偏移 / 明显异常`

## 六、分段结果检查

- [ ] 分段目录结构正常
- [ ] 分段数量大致合理
- [ ] 没有大量缺失文件
- [ ] 没有大量 0KB 或异常短音频
- [ ] 随机抽查 3 到 5 段，结果正常

## 七、对齐检查

查看 `output/alignment_report.md`：

- [ ] `needs_review` 数量可接受
- [ ] 问题集中在少数段落，而不是系统性失配
- [ ] 没有大面积时长对不齐

需要人工复核的体感等级：

- `低 / 中 / 高`

## 八、Prompt 与配置检查

- [ ] 实际使用的是预期 provider mode
- [ ] 实际使用的是预期 prompt 版本
- [ ] 如果通过 Web UI 修改了 prompt，确认只影响新任务
- [ ] 没有出现意外的配置或 provider 错配

## 九、当前阶段哪些现象算正常

以下情况目前不自动算失败：

- 少量 `needs_review` 段落
- editor 路径不直接产出完美最终 MP4
- `local-audio-demo` 不能实际完成 publish
- `local-video-demo` 仍可能暴露当前 extractor 边界
- real provider 未配置完整时出现清晰报错

## 十、哪些现象算异常

以下情况应视为回归候选：

- [ ] 主流程崩溃
- [ ] `alignment` 失败
- [ ] `draft` 失败
- [ ] `dubbed_audio_complete.wav` 缺失
- [ ] `subtitles.srt` 缺失或基本不可用
- [ ] 大量 segment 文件缺失
- [ ] 大面积时序错位
- [ ] 同一样本里反复出现 speaker 混乱

## 十一、最终结论

- 本次是否通过：`是 / 否`
- 主要问题总结：
- 是否需要继续跟进：
- 建议下一步：

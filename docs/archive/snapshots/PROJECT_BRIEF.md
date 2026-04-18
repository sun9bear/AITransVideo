# AIVideoTrans - 项目说明

## 核心目标
YouTube URL → 全自动生成对齐好的配音音频 + SRT字幕
输入一个链接，等待几分钟，拿到完整配音音频，在剪映里5分钟导入即可精修发布。

## 技术栈
- 下载：yt-dlp（已完成 T1.4）
- 转录+说话人识别：AssemblyAI
- 翻译+文本重写：Gemini API
- TTS+音色克隆：MiniMax（国内版，api.minimaxi.com，speech-2.8-turbo）
- 时间轴对齐：pydub/ffmpeg DSP变速 + Gemini重写
- 输出合成：pydub

## 输出格式（不是剪映草稿）
- dubbed_audio_complete.wav — 完整配音音频（与原视频等长，间隙填静音）
- segments/ — 按说话人分目录的单独音频片段（方便单段替换）
- subtitles.srt — 中文字幕
- background_sounds.txt — 背景声检测报告
- alignment_report.txt — 对齐质量报告

## 主命令
python main.py process <youtube_url>

## 开发规则
1. 新功能以新增模块为主，最小化修改现有代码
2. 现有demo命令、voice-registry、control-panel保持不变
3. 所有API Key通过env或autodub.local.json加载，不硬编码
4. 每个Stage完成后状态写入project_state.json，支持断点续跑
5. MiniMax音频解码使用bytes.fromhex（不是base64）
6. src/modules/draft/ 目录已搁置，不修改不扩展

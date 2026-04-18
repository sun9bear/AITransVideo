# REVIEW_FRONTENDIZATION_PHASE1_ACCEPTANCE_NOTE.md

## 1. 本阶段完成了什么

本阶段已将 `speaker_review` 与 `translation_review` 的主处理路径搬进新前端。
当前任务页与项目详情页在发现这两类 review 时，主入口已优先进入新前端原生 review 页面。

## 2. 当前两类 review 已具备的真实能力

- 可从当前任务页 / 项目详情页发现 review
- 可进入新前端原生 `speaker_review` 页面
- 可进入新前端原生 `translation_review` 页面
- 可展示真实 review 内容，而不是前端伪造状态
- 可在新前端完成真实 approve
- approve 后任务状态会按现有后端语义真实前进

## 3. 当前明确边界

- 当前只完成 `speaker_review` 与 `translation_review` 原生前端化
- 当前不包含 `voice_review` 原生前端化
- 当前不包含第二批页面
- 当前不重构 review 系统
- 当前不改后端核心 review 语义，除非后续重新定义范围

## 4. 旧 Web UI fallback 的当前定位

旧 Web UI 在当前阶段继续保留为 fallback。
它不再是 `speaker_review` 与 `translation_review` 的主入口，只用于异常、未覆盖能力或排障时兜底。

## 5. 后续推进前的前提

如果后续还要继续推进 review 原生前端化或扩张页面范围，必须先重新定义下一阶段范围，再开始开发。

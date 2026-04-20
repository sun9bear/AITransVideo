# Backlog

轻量级技术债 / follow-up 跟踪。每项独立可做，不互相阻塞。新 session 启动
时扫一眼本文件，判断有没有合适的专项可以拿起。

格式约定：每项标题 = 一句话概括；正文 = 背景 / 动机 / 工作量估计 / 关联
commit。完成后从本文件删除（git history 自然保留）。

---

## [DSP] 主流程 `aligner._dsp_stretch` 迁移到 `utils/audio_fit.fit_audio_to_slot`

**背景**：`dade959` 已把 γ publish-only resume 路径的 DSP（编辑后的
单段 TTS → slot）抽成 `src/utils/audio_fit.py`，策略是 smart-trim +
clamped atempo + silence-pad / truncate。

主流程（`services/alignment/aligner.py::SegmentAligner._dsp_stretch`）
还在用旧的"无脑 atempo 到 exact target"策略。四处调用点（aligner.py
line 196 / 216 / 229 / 356 / 372）分布在 force_dsp / force_dsp_user /
decision="dsp" / rewrite_loop success / rewrite_loop fallback 等分
支，对 return 值 + error 语义有紧耦合（`aligned_duration_ms` +
`alignment_method` 字段需要回写到 `DubbingSegment`）。

**迁移会带来的用户可感知提升**：

- Gemini rewrite 失败（ratio 超出 [0.85, 1.15] 且 budget 耗尽）时，
  最终 `force_dsp` 分支的极端比例不再鸭子音；clamp + pad 后是"前段
  正常语速 + 末尾静音"
- 短段 TTS 中部的 silence 不再被 smart trim 误删（旧版对无 alignment
  rewrite 成功的情况无 trim，倒是 OK，但迁移后行为统一，一致性更好）

**工作量**：~1-2 小时

1. 新增 thin wrapper `_dsp_stretch_v2(self, input_path, target_ms, output_path)`
   调用 `fit_audio_to_slot(input_path, target_ms, output_path=output_path,
   policy=FitPolicy(atempo_min=..., atempo_max=..., ...))`
2. 选好主流程 policy 参数（比 γ 可以略宽松，比如 atempo [0.7, 1.7]，
   因为主流程已有 rewrite loop 兜底，DSP 只是最后一公里）
3. 替换 4 处调用点，留下 return 值语义（`aligned_duration_ms` 从
   `FitResult.final_duration_ms` 取）
4. 删除 `aligner._build_atempo_filter` / `_format_atempo_factor` /
   `_dsp_stretch` 本体（和 util 里重复）
5. 跑 `tests/test_aligner*` + 完整回归，确保 alignment 精度测试没
   退化（可能需要放宽一些 tolerance）

**关联 commit**:
- `dade959` — audio_fit 模块本体
- `0ab76a1` — 最早把 γ 的硬裁改 atempo
- `74613bc` — 短段静音裁切
- aligner.py 现状见 [src/services/alignment/aligner.py:501-550](../../src/services/alignment/aligner.py:501)（`_dsp_stretch` 原始实现）

**触发条件**：γ 路径的 audio_fit 稳定 1 周以上，用户/admin 无投诉音
质问题 → 可以启动迁移。之前不要贸然切（主流程是付费 API 下游，DSP
出问题会让昂贵的 TTS 白跑）。

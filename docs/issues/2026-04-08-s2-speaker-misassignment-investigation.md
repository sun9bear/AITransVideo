# S2 审校阶段说话人误分配调查

> 日期：2026-04-08
> 状态：排查中，根因未 100% 确认
> 严重性：P1（直接影响配音质量，用户可见）
> 关联任务：job_2fa5bd6d1ec64379a56a2e1999d6e265

---

## 1. 现象

Studio 任务翻译审核页面，片段 1（"All right, we have some other news to tell you about, too..."）被标注为 Speaker B，但该片段内容明显是主持人贝基·奎克在说话，应为 Speaker A。

该问题在删除旧任务、重新提交后仍然复现，排除缓存因素。

## 2. 排查数据

### 2.1 ASR 原始输出（正确）

```
utt 0: ASR=A (0.6s-35.6s)   "All right, we have some other news..."    ← 贝基
utt 1: ASR=B (38.2s-107.5s) "Everything, uh, will be the same..."      ← 巴菲特
utt 6: ASR=A (180.9s-202.6s) "Hm. So does Warren Buffett..."           ← 贝基
utt 7: ASR=C (202.6s-245.8s) "Absolutely. I mean, so it's..."          ← 第三人
```

ASR 标注完全正确，3 个说话人 A/B/C 识别准确。

### 2.2 speaker_id 映射（正确）

`_speaker_id_from_label()` 是确定性的：
- ASR speaker "A" → `speaker_a`
- ASR speaker "B" → `speaker_b`
- ASR speaker "C" → `speaker_c`

不依赖出场顺序，纯字母转换，不会错位。

### 2.3 S2 审校后（错误）

```
idx | ASR      | S2 后      | 变化
 0  | speaker_a | speaker_b  |  <<<  被改错
 1  | speaker_b | speaker_b  |
 2  | speaker_a | speaker_a  |
 3  | speaker_b | speaker_b  |
...其余 9 条均未变化
```

**仅 idx 0 一条被改错**，从 speaker_a（贝基）变成了 speaker_b（巴菲特）。

### 2.4 Pipeline 日志

```
[S2] Running unified LLM transcript review (audio + text)...
[S2] Speaker identity result:
  Speaker A → 贝基·奎克                  ← Gemini 正确识别了贝基
[S2] Applied 1 correction(s).
  #7 (03:00, 21.7s) speaker_a → speaker_b: "Hm. So does Warren Buffett..."
[S2] Lines: 13 (was 13)
```

日志显示：
1. Gemini 正确识别了 Speaker A = 贝基·奎克
2. 只应用了 1 个 correction
3. 该 correction 的目标是 `#7`（idx=7，即 utt 6，"Hm. So does Warren Buffett..."）

### 2.5 矛盾点

日志说改的是 #7（utt 6），但最终结果里 utt 6 没变（仍为 speaker_a），utt 0 却被改了。这意味着**日志和实际结果不一致**，中间有一步操作没被日志记录。

## 3. 候选根因（2 个，未 100% 确认）

### 候选 A：`_apply_interview_sanity_check` 在 3-speaker 视频上被错误触发

**链条：**
1. Gemini `correct_speaker` correction 把 line 6（idx=7）从 speaker_a 改为 speaker_b → `_apply_corrections` applied=1
2. Gemini 的 `speakers` dict 只返回了 2 个 key（speaker_a + speaker_b，省略了 speaker_c）
3. `_resolve_interview_roles(speakers)` 检查 `len(speakers) == 2` → True → 返回 host/guest 映射
4. `_apply_interview_sanity_check` 遍历所有 line：
   - line 0（贝基 35s 长叙述）被某条规则匹配（如 `_is_first_person_answer`），改为 guest=speaker_b → sanity_applied +1
   - line 6（被 correction 改成 speaker_b）被另一条规则改回 speaker_a → sanity_applied -1（或 +1）
5. 净效果：line 0 被改错，line 6 被改回，`corrections_applied` 总数仍为 1

**支持证据：**
- Gemini prompt 模板里 speaker_c 是可选的（"如有第三位及更多说话人，都要列出"），Gemini 可能确实省略了
- `_resolve_interview_roles` 只在 `len(speakers) == 2` 时生效
- sanity check 里 `_is_first_person_answer` 会匹配长句并分配给 guest

**反对证据：**
- 无法确认 Gemini 实际返回了几个 speaker key（没有原始 JSON 输出日志）
- 如果 sanity check 改了 2 条（line 0 和 line 6），`sanity_applied` 应该是 2，总数应该是 3 而非 1

### 候选 B：Gemini correction 实际目标是 index=1（不是 index=7）

**链条：**
1. Gemini 输出的 correction 是 `{"action": "correct_speaker", "index": 1, "to": "speaker_b"}`
2. `_apply_corrections` 用 `index_map[1]` 找到 line 0（index=1，1-based） → 改为 speaker_b → applied=1
3. line 0 从 speaker_a 变为 speaker_b ← 这直接解释了最终结果

**但日志显示的是 #7？**

日志打印逻辑（process.py line 755-764）是在 `review_result.lines` 返回后，逐条比较原始 vs 审校后的 lines：

```python
for orig, rev in zip(transcript_result.lines, review_result.lines):
    if orig.speaker_id != rev.speaker_id:
        print(f"#{orig.index} ...")
```

如果 `_enforce_max_duration` 或 `_apply_interview_sanity_check` 之后做了 re-index（line 500-501），那 `orig.index` 和 `rev.index` 的编号可能错位——导致日志打印了错误的 index。

**支持证据：**
- re-index 确实在 line 500-501 执行：`for i, line in enumerate(final_lines): line.index = i + 1`
- 如果 review 过程中有 split/merge 导致行数变化，zip 会错位
- 但这个 job 行数没变（13→13），所以不应该错位

**反对证据：**
- 行数没变，re-index 不会改变顺序
- 如果 correction index=1，日志应该打印 `#1 (00:00, 35.0s)`，不会打 `#7 (03:00, 21.7s)`

## 4. 已确认的相关代码问题（2-speaker 硬编码残留）

排查过程中发现 3 处 hardcoded 2-speaker 限制，虽然不一定是本次 bug 的直接原因，但属于同类风险：

### 4.1 transcript_reviewer.py line 951

```python
speaker = c.get("speaker", first.speaker_id)
if speaker not in {"speaker_a", "speaker_b"}:
    speaker = first.speaker_id
```

merge 操作中 speaker_c 会被强制覆盖为第一段的 speaker。

### 4.2 process.py line 1576

```python
if str(speaker_id).strip() in {"speaker_a", "speaker_b"}
```

reviewed_speaker_map 中 speaker_c 及以上的映射会被静默丢弃。

### 4.3 process.py line 2368

```python
for spk_id, spk_name in [("speaker_a", speaker_name_a), ("speaker_b", speaker_name_b)]:
```

speaker_styles 只给 speaker_a/speaker_b 生成，speaker_c 没有。

## 5. 旧 job 为什么没这个问题？

旧 job（job_46eaf09b3af54f5580117c04d2b5382d）的 ASR 和 S2 结果完全一致——S2 没有应用任何 speaker correction。这是 LLM 输出的非确定性：同样的输入，Gemini 有时会发出不同的 correction 指令。

## 6. 复现条件

- 3+ 说话人的采访视频
- Gemini S2 审校发出 `correct_speaker` correction
- 或 Gemini speakers dict 只返回 2 个 key → 触发 sanity check

## 7. 建议修复方向

### 短期（止血）

1. `_apply_interview_sanity_check` 应该检查 **transcript 中实际出现的 speaker 数量**，而非 Gemini speakers dict 的 key 数量：

```python
actual_speakers = {line.speaker_id for line in lines}
if len(actual_speakers) > 2:
    return list(lines), 0  # 3+ speaker → 不做 sanity check
```

2. 修复 3 处 hardcoded 2-speaker 限制（4.1-4.3）

### 中期

3. 在 S2 prompt 里加入每个 speaker 的**首次出现时间码和音频时长**，给 Gemini 明确锚点
4. `_apply_corrections` 后、`_apply_interview_sanity_check` 前加一行日志，记录 corrections 改了哪些行
5. 保存 Gemini 原始 JSON 响应到文件，便于事后排查

### 长期

6. 考虑对 `correct_speaker` correction 增加置信度阈值——只有 Gemini 明确表示"音色明显不同"时才应用

## 8. 排查工具命令

```bash
# 查 ASR 原始 vs S2 结果对比
python3 -c "
import json
raw = json.load(open('transcript/raw_assemblyai.json'))
t = json.load(open('transcript/transcript.json'))
for i, (u, l) in enumerate(zip(raw['utterances'], t['lines'])):
    asr = f'speaker_{u[\"speaker\"].lower()}'
    s2 = l['speaker_id']
    flag = '  <<<' if asr != s2 else ''
    print(f'{i}: ASR={asr} S2={s2} {flag} {u[\"text\"][:60]}')
"
```

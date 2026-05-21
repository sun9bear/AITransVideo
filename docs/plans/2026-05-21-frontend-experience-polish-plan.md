# 前端体验打磨与优化方案（实测收敛执行版）

**日期**：2026-05-21
**状态**：方案已对齐，进入执行阶段
**作用范围**：`frontend-next/`（任务创建、视频修改工作台、草稿交付组件）
**关联文档**：`docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md`, `DESIGN.md`

---

## 1. 背景与对齐目标

本方案在吸收 Claude Code 4.6 与 CodeX 评审意见的基础上，**经过了针对本地代码文件的严密物理核对与实测校验**。前两轮评审中包含多处静态分析产生的幻觉与误读，本方案坚决贯彻"以代码事实为根基"的原则，去伪存真，制定最务实、高性价比的前端打磨清单。

### 核心设计与工程共识

1. **坚守 Web 沙盒与安全红线**：浏览器环境严禁读取或操控本地文件目录。所有针对剪映目录的交互定位为"一键复制路径模板 + 高质量图文步骤导引"，严禁编写任何越界移动文件的本地助手。
2. **Gateway 单一计费真源**：前端不自行编写任何带有 `duration * billing_rate` 倾向的客户端估算公式。所有点数预估必须由 Gateway 接口返回值驱动。
3. **彻底铲除客户端硬编码计费公式**：历史遗留且从未被调用的 `frontend-next/src/lib/cost/estimator.ts`（包含硬编码 API 单价和费率计算）已执行**物理删除**，彻底斩断客户端计费逻辑与后端脱钩的隐患。配套加回归守卫防止退化。
4. **拒绝无中生有的幻觉工作项**：
   * 不存在的 Bug：`SegmentRow.tsx` 中的 `draftRatio` 百分比换算写法已是 `((draftRatio - 1) * 100).toFixed(0)`，括号优先级正确，无语法 Bug。
   * 不存在的动效：`SegmentRow.tsx` 活动态是高效的 solid border（`border-l-2 border-l-primary + bg-primary/[0.06]`），不存在 `blur` 或 `backdrop-filter` 重绘动画。
   * 不存在的依赖：项目从未引入 `mermaid` 组件。
   * 不存在的路径泄露：`compatibility_report_path` 前端从不渲染，无安全风险。为类型严谨执行清理。
5. **高性能微秒级动效（硬约束）**：本方案**禁止引入**任何 `@keyframes` 关键帧动画来操纵 `background-image` / `filter: blur` / `backdrop-filter` / `radial-gradient` 等无法 promote 到 compositor 层的 CSS 属性。这些属性在工作台长列表（30+ 段落）下会触发软件 paint，破坏滚动与状态切换的流畅度。仅使用 solid border / background-color / transform / opacity 这类 compositor-friendly 的视觉手段。
6. **拒绝 scope creep**：本方案只做 4-5 个已审计文件的小修改。任何超出范围的重设计（如空状态重做、品牌印章、产品文案改写）都属于独立设计任务，需另行走 `/plan-design-review` 或 `/design-consultation`，不纳入本方案。

---

## 2. 代码实测事实与采纳决策

| 模块名称 | 评审声称 | 代码实测事实 | 本方案最终执行决策 |
| :--- | :--- | :--- | :--- |
| **客户端计费文件** | 未提及 | `estimator.ts` 硬编码 MiniMax / AssemblyAI / Gemini / Deepseek 费率，全局零引用。 | **删除该文件**（已完成）+ 加 `test_legacy_cleanup_guards.py` 回归守卫防止退化。 |
| **百分比 JS Bug** | 声称存在 `100.toFixed(0)` 运算符优先级 Bug。 | 代码已正确使用括号 `((draftRatio - 1) * 100).toFixed(0)`，无 Bug。 | 跳过：不修复不存在的 Bug。 |
| **SegmentRow blur 动画** | 声称卡片活动态在用 blur 动画。 | 实际无 blur 属性，当前为 solid border + 轻底色，性能极佳。 | 跳过：保持当前结构。 |
| **Mermaid 依赖膨胀** | 声称需警惕 Mermaid 引入 bundle。 | 全局零匹配，无人引入。 | 跳过：假想问题。 |
| **路径泄露漏洞** | 声称 `compatibility_report_path` 暴露绝对路径。 | 前端从未解构或渲染该字段，默认值为 null，UI 不显示。 | 类型清理（Hygiene）：从 API 响应类型与默认 state 中剔除该字段，消除误读源头。**不构成 security 工作**。 |
| **额度预估预算卡** | 声称需引入 pre-flight credits 预算估算。 | submit 前 probe 视频时长需要新增 backend endpoint 与改造前端 upload 流程，工期不可控。`TranslationForm` 已经在调 `getCreditsEstimate` 拿每分钟点率。 | 推后：只在 submit 之后基于后端返回的 `source_duration_minutes` 渲染总额，本方案不做。 |
| **焦虑消除（重打包不扣点）** | 建议增加重新打包的免费提示。 | 页面已有过期重打包按钮 + 24h 提示，但未明确"不再扣点"。 | 轻量文案优化：三处补"不再扣点 / 不额外扣点"措辞。**不用"免费"**——任务本身已扣过点，"免费"语义松散且涉嫌商业承诺。 |
| **张力指示器** | 提议引入 5%-20% 灰 / >20% 朱砂的双色分阶。 | 现有 `hasDraftMismatch` 是单行文本警告（仅 >20% 触发）。同卡片还有 `tts_length_guidance` 与 `isAnomalous` 警告，已三层。 | **替换式落地**：删除 `hasDraftMismatch` 文本块，换成简单的双色 progress bar；`isAnomalous`（force_dsp）保留不动。 |
| **圆角收敛** | 提议把主 CTA 的 `rounded-full` 改为 `rounded-[var(--radius)]`。 | `TranslationForm` line 598 主提交按钮 + line 656 `ConcurrencyActionLink primary` 是同一组视觉规格，都用 `rounded-full bg-gradient-to-r`。徽章 / 状态点 / 复选 dot 不受影响。 | 一并收敛 line 598 与 line 656，避免同表单两枚主 CTA 视觉分叉。徽章保持 `rounded-full`。 |

---

## 3. 具体修改内容与实现策略

### 3.1 物理删除客户端硬编码计费文件（已完成）

* **删除目标**：`frontend-next/src/lib/cost/estimator.ts`（已物理删除，git status `D`）
* **顺手清理**：同时删除空目录 `frontend-next/src/lib/cost/`
* **回归守卫**：在 `tests/test_legacy_cleanup_guards.py` 追加一条测试：
  ```python
  def test_frontend_no_client_side_billing_estimator():
      """estimator.ts deleted 2026-05-21; prevent silent revival.
      Re-introducing it would violate "Gateway is sole billing
      truth source" invariant (CLAUDE.md §硬编码计费红线)."""
      repo_root = Path(__file__).resolve().parent.parent
      # 1. 文件不存在
      assert not (repo_root / "frontend-next/src/lib/cost/estimator.ts").exists()
      # 2. 同级目录也不存在（防止被换个文件名复活）
      assert not (repo_root / "frontend-next/src/lib/cost").exists()
      # 3. 别名 import + 相对路径 import 双向扫描
      forbidden_patterns = ("@/lib/cost", "lib/cost/estimator", "/cost/estimator")
      for ts in (repo_root / "frontend-next/src").rglob("*.ts*"):
          text = ts.read_text(encoding="utf-8")
          for pat in forbidden_patterns:
              assert pat not in text, f"forbidden import {pat!r} in {ts}"
  ```

### 3.2 TranslationForm.tsx 圆角同步收敛

* **目标文件**：`frontend-next/src/components/workspace/TranslationForm.tsx`
* **修改范围**：
  * line 598 创建任务 `<button type="submit">` 的 `rounded-full` → `rounded-[var(--radius)]`
  * line 656 `ConcurrencyActionLink` （`variant="primary"`）的 `rounded-full` → `rounded-[var(--radius)]`
* **不动**：plan-card 内的徽章（line 366、398、420、454、476）、check 圆点（line 378、410、466）、灰底"即将开放"标签（line 431、487）—— 这些是徽章/状态点，需要纯圆。

### 3.3 SegmentRow.tsx 张力指示器（双色 progress bar 替换式落地）

* **目标文件**：`frontend-next/src/components/workspace/edit/SegmentRow.tsx`
* **删除范围（精确）**：
  * 当前 line 516 的外层条件 `{(isAnomalous || hasDraftMismatch) && (...)}` 改为 `{isAnomalous && (...)}`
  * 块内仅保留 `isAnomalous` 分支（line 524-531 的"⚠ 时长异常"），删除 `hasDraftMismatch` 分支（line 532-537）
  * 注意：`isAnomalous`（force_dsp 警告）是独立信号，必须保留
  * **同时删除 line 286-292 的两个变量声明**：`hasDraftMismatch` 和 `draftMismatchSeverity` ——它们仅服务于即将删除的文本分支，留下会被 `@typescript-eslint/no-unused-vars` 卡 lint。保留 `target` / `draft` / `draftRatio`，后两个新计算要用。
* **新增预计算**（放在 `draftRatio` 计算之后，替代删除的两行）：
  ```typescript
  const deviationPct =
    draftRatio !== null ? Math.round((draftRatio - 1) * 100) : 0
  // ARIA spec 要求 aria-valuenow 落在 [valuemin, valuemax] 区间内。
  // 视觉宽度也按 ±20% clamp（>20% 时条带触底，由颜色表达"超限"语义）。
  // 文本仍显示真实 deviationPct（屏幕阅读器会读到文本节点）。
  const clampedDeviationPct = Math.max(-20, Math.min(20, deviationPct))
  const showTensionBar = draftRatio !== null && Math.abs(deviationPct) >= 5
  const isSevere = Math.abs(deviationPct) > 20
  ```
* **进度条 JSX**（放在 force_dsp 警告之后、draft 面板之前）：
  ```tsx
  {showTensionBar && (
    <div className="flex items-center gap-2 mt-1">
      <div
        className="relative h-[3px] flex-1 rounded-full bg-muted/50 overflow-hidden"
        role="meter"
        aria-valuenow={clampedDeviationPct}
        aria-valuemin={-20}
        aria-valuemax={20}
        aria-label="新 TTS 时长偏差（百分比）"
      >
        {/* 居中起点的条带：偏正方向往右、偏负方向往左。
            视觉用 clamp 值，超限时条带触底由颜色表达。 */}
        <div
          className={`absolute top-0 h-full ${
            isSevere ? "bg-[color:var(--cinnabar)]" : "bg-[color:var(--ink-gray-3)]"
          }`}
          style={{
            left: clampedDeviationPct >= 0 ? "50%" : `${50 + clampedDeviationPct * 2.5}%`,
            width: `${Math.abs(clampedDeviationPct) * 2.5}%`,
          }}
        />
        {/* 中线刻度（0% 完美对齐参考线） */}
        <div className="absolute top-0 left-1/2 h-full w-px bg-border" />
      </div>
      <span
        className={`text-[10px] tabular-nums ${
          isSevere ? "text-[color:var(--cinnabar)]" : "text-muted-foreground"
        }`}
        title="新 TTS 与目标时长的偏差"
      >
        {deviationPct > 0 ? "+" : ""}{deviationPct}%
      </span>
    </div>
  )}
  ```
* **视觉语义**：
  * 偏差 < 5% → 不显示（避免噪声）
  * 5% ≤ |偏差| ≤ 20% → 灰色条带 + 灰色数字（非警示，纯指示）
  * |偏差| > 20% → 朱砂色条带 + 朱砂色数字（警示）
* **禁止项**：本节**不引入**任何 `@keyframes` 动画、`backdrop-filter`、`background-image: radial-gradient` 等无法 GPU 合成的属性，遵守 §1.5 硬约束。

### 3.4 JianyingDraftPathDialog.tsx & ResultMediaCard.tsx（交付区微打磨与类型清理）

#### 一键复制路径
* **目标文件**：`frontend-next/src/components/workspace/JianyingDraftPathDialog.tsx`
* **新增 import**：`import { toast } from "sonner"` —— 当前文件只有 React / Dialog / Input / Button 的 import，缺这条会编译失败。
* **布局**：当前 Windows / Mac 路径用 `<code className="block break-all">` 渲染，路径很长会换行。**不要**把复制按钮塞到 code block 同一行右侧（会被 `break-all` 挤压）。改用 **flex 容器，按钮在 code block 上方右对齐**，或者按钮作为 code block 后面的独立一行（小尺寸 ghost button）。
* **实现要点**：
  ```tsx
  async function copyToClipboard(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`已复制 ${label} 路径`)
    } catch {
      // 非 HTTPS 上下文 / 权限被拒 / 老 webview fallback
      toast.error("复制失败，请手动选中复制")
    }
  }

  // JSX 示例（Windows 块，Mac 块同理）
  <div className="space-y-1">
    <div className="flex items-center justify-between">
      <p className="font-medium text-foreground/70">Windows 默认路径：</p>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-6 px-2 text-[10px]"
        onClick={() => copyToClipboard(WINDOWS_PATH, "Windows")}
        aria-label="复制 Windows 路径"
      >
        复制
      </Button>
    </div>
    <code className="block break-all font-mono text-[11px] text-foreground/60">
      {WINDOWS_PATH}
    </code>
  </div>
  ```

#### 焦虑消除文案（三处明确"不额外扣点"）
* **目标文件**：`frontend-next/src/components/workspace/ResultMediaCard.tsx`
* **措辞原则**：避免"免费"二字——任务本身已经扣过点，"免费"在点数体系下语义松散且涉嫌商业承诺。采用"不再扣点"或"不额外扣点"更精确，且不会和未来商业策略冲突。
* line 292 区附近（保留窗口提示）：`超时后可重新打包。` → `超时后可重新打包，不额外扣点。`
* line 363 区附近 title：`素材包已过保留期（24 小时）被自动清理，请重新打包` → `素材包已过保留期（24 小时）被自动清理，重新打包不再扣点`
* line 367 按钮文案：`素材包已过期 · 重新打包` → `素材包已过期 · 重新打包不扣点`

#### 接口类型清理
* **目标文件**：`frontend-next/src/lib/api/jobs.ts`、`frontend-next/src/components/workspace/ResultMediaCard.tsx`
* `jobs.ts:220` 的 `JianyingDraftStatusResponse` 接口删除 `compatibility_report_path: string | null` 字段
* `ResultMediaCard.tsx:475` 的 `JIANYING_DEFAULT_STATE` 默认对象删除 `compatibility_report_path: null` 属性
* 后端如果继续返回该字段也无伤——TypeScript 端不再期望它，前端不会泄露

### 3.5 globals.css 噪点参数细腻化

* **目标文件**：`frontend-next/src/app/globals.css` line 280-282
* `baseFrequency='0.85'` → `baseFrequency='0.95'`（颗粒更细）
* 外层 `opacity: 0.07` → `opacity: 0.045`（更克制）
* **本节不引入任何新 `@keyframes` 与动画类**，遵守 §1.5 硬约束。

---

## 4. 实施顺序与分期计划

工期 **1-2 天**，全部为低风险前端打磨。

### Stage 1 — 删除、清理与圆角（第 1 天）

* [x] 物理删除 `frontend-next/src/lib/cost/estimator.ts`
* [ ] 删除空目录 `frontend-next/src/lib/cost/`
* [ ] 追加 `tests/test_legacy_cleanup_guards.py::test_frontend_no_client_side_billing_estimator` 回归守卫
* [ ] 清理 `JianyingDraftStatusResponse` 与 `JIANYING_DEFAULT_STATE` 中的 `compatibility_report_path`
* [ ] `globals.css` 噪点参数微调（opacity 0.07→0.045 / baseFrequency 0.85→0.95）
* [ ] `TranslationForm.tsx` 圆角同步收敛（line 598 + line 656）
* [ ] 阶段提交前跑 `npm run lint && npm run build` + `pytest tests/test_legacy_cleanup_guards.py`

### Stage 2 — 张力条与交付区打磨（第 2 天）

* [ ] `SegmentRow.tsx`：把外层 `&&` 条件改为只看 `isAnomalous`，删除 `hasDraftMismatch` 文本分支
* [ ] `SegmentRow.tsx`：在 force_dsp 警告之后追加双色 progress bar 张力条
* [ ] `JianyingDraftPathDialog.tsx`：Windows / Mac 路径旁加一键复制按钮（含 try/catch fallback + toast）
* [ ] `ResultMediaCard.tsx`：三处文案补"不再扣点 / 不额外扣点"（line 292 / line 363 title / line 367 按钮）
* [ ] 阶段提交前跑 `npm run lint && npm run build`
* [ ] 起本地 dev server（`cd frontend-next && npm run dev`）目视核对：张力条在 5% / 12% / 22% 三档下的视觉是否符合双色分阶设计

---

## 5. 编译守卫与回归测试

```bash
cd frontend-next
npm run lint
npm run build
```

后端运行：
```bash
python -m pytest tests/test_legacy_cleanup_guards.py
```

确保 `estimator.ts` 不会被静默恢复、`@/lib/cost` import 不出现在前端代码中。

---

## 6. 显式不做清单（Non-goals）

* ❌ **不重写 `SplitSegmentDialog.tsx`** —— `CutTextBlockMulti` 已成熟，保留不动
* ❌ **不引入任何 `@keyframes` 动画** —— 包括但不限于 `ink-splash`、水墨晕染、状态切换淡入淡出（§1.5 硬约束）
* ❌ **不引入声学正弦波 / 竹简格栅 / 15-21 根像素条等复杂张力可视化结构** —— 一个双色 progress bar 满足需求
* ❌ **不重做 `projects/page.tsx` 的空状态** —— 属于独立设计任务，需走 `/plan-design-review`
* ❌ **不修改任何品牌元素**（朱砂印章、篆字、空状态文案） —— 不在本方案 scope
* ❌ **不在 submit 之前 probe YouTube 时长** —— 后端 endpoint 不存在；只在 submit 之后基于后端返回时长渲染
* ❌ **不引入第三方图表库**（Mermaid / Chart.js 等）—— 保持 bundle 体积稳定
* ❌ **不做任何浏览器外的本地文件读写助手** —— Web 沙盒红线

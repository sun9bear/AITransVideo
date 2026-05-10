# AIVideoTrans 全面代码审查报告

> 日期：2026-05-10
> 审查范围：全仓库当前工作区快照（`git ls-files`: 1397；`rg --files`: 1375；`tests/*.py`: 294；`docs/graphs/*.md`: 11，其中架构子图 10 张；`docs/plans/**/*.md`: 166；Next.js 前端 + Python pipeline/gateway）
> 审查方式：逐层阅读——架构图 → core → pipeline → modules → services → gateway → frontend → tests → docs → devops
> 二次核验：2026-05-10 依据当前工作区重新核对可证伪事实；本报告中的行数和统计以该次核验为准。
> 统计口径：`Any` 计数使用 `rg -n "\bAny\b" src/services/gemini/translator.py`；`/job-api` 运行时代码/配置计数使用 `rg -n "/job-api|job-api" gateway src frontend-next Caddyfile docker-compose.yml`，测试另用 `tests`；日志计数使用 `rg -n "\bprint\(" src gateway main.py` 与 `rg -n "\blogging\b|logger|getLogger\(" src gateway main.py`。

---

## 目录

1. [总体评价](#1-总体评价)
2. [逐维度评分](#2-逐维度评分)
3. [项目亮点](#3-项目亮点)
4. [代码质量问题](#4-代码质量问题)
5. [编码规范问题](#5-编码规范问题)
6. [架构优化建议](#6-架构优化建议)
7. [可扩展性问题](#7-可扩展性问题)
8. [运维与可观测性](#8-运维与可观测性)
9. [测试覆盖分析](#9-测试覆盖分析)
10. [Bug 与风险](#10-bug-与风险)
11. [综合改进优先级](#11-综合改进优先级)

---

## 1. 总体评价

这是一个**单人开发但已达到准生产级商业化 SaaS 规模**的项目。架构严谨性、测试覆盖度和商业完备性远超大多数个人项目。不是"快速出 Demo"的风格，而是每个 subsystem 都被当作正式产品面来设计——voice calibration 有三入口控制平面，R2 有 proactive publish + terminal mirror + edit_generation 版本控制，editing 有独立的 speaker registry + profile inference + hard sync gate。

核心评价：**工程纪律罕见的个人项目，但系统复杂性的增长正在接近单文件单类组织方式的承载上限。后续最优路线不是大爆炸重写，而是先补可观测性、CI 与薄切片测试，再分阶段收敛大文件/大类。**

---

## 2. 逐维度评分

| 维度 | 评分 | 说明 |
|---|---|---|
| 架构设计 | 8.5/10 | 分层清晰、不变量被强制执行、防御性设计无处不在。扣分：双重管道并行、process.py 过于庞大 |
| Pipeline 成熟度 | 9/10 | Pre-TTS rewrite 逻辑极其精细，speaker 结构分析、三层对齐策略均为生产级 |
| 商业化完备性 | 8.5/10 | 完整的 auth + billing + trial + credits + admin + support 体系。未含 team seats、auto-renew |
| 代码质量 | 6.5/10 | 逻辑正确但结构问题明显：巨型函数、God Class、getattr 滥用、类型黑洞 |
| 编码规范 | 6/10 | Provider 注册 4 种模式、配置 4 种机制、API 无版本化、中文字符串散落 |
| 测试与质量保障 | 8/10 | 294 个 Python 测试文件覆盖广、契约级守卫测试成熟。扣分：部分核心函数只做窄单测、缺少 gateway + job-api 跨进程契约测试 |
| 前端设计 | 7.5/10 | 组件分层合理、后端消费模式正确。扣分：无 React Query/SWR、4s 轮询 |
| DevOps | 8/10 | Docker Compose 6 服务清晰、安全加固到位。扣分：无 CI/CD 配置入仓 |
| 文档体系 | 9/10 | 11 个 graph md（10 张架构子图 + README）+ 166 个 plans md + 核心约束文档，覆盖全面但需要区分当前计划与历史计划 |
| 可扩展性 | 6/10 | 新增 TTS provider 需改 5+ 文件，新增套餐需改 4 文件，无注册表/策略模式 |
| 可观测性 | 5.5/10 | 有 UsageMeter、event JSONL 和部分 logging，但 `print()` 仍大量存在，结构化日志不统一，启动恢复错误仍有静默吞掉路径 |

---

## 3. 项目亮点

### 3.1 架构设计

**清晰的三层分离**：Gateway（认证/路由/商业规则）→ Job API（任务 CRUD）→ Pipeline（纯处理），各层职责明确，没有跨层泄漏。

**强制的架构不变量**，且被测试守卫：
- TTS 单元是 `SemanticBlock`，不是 subtitle line
- 对齐策略是 DSP-first，LLM rewrite 是 fallback
- 字幕重定时是数学计算，不是 LLM 驱动
- Gateway 是 plan/pricing/trial 的唯一真源

**防御性设计**层层叠加：R2 异常 → fallback local；whisper 对齐失败 → fallback proportional；校准失败 → 不阻断审核提交；Feature flag + kill switch 覆盖所有关键路径。

**源码真源原则严格**：Gateway 模块不允许硬编码 Job API URL（有 AST 级回归守卫）；内部 API key 有 16 字符最小长度启动校验；前端消费商业事实而非自建真源。

### 3.2 Pipeline 成熟度

**Pre-TTS rewrite**：超过 30 个常量控制不同场景的文本缩减/扩展决策——普通 overshoot、短段 compact、中等段 undershoot risk、长段 overshoot、high shrink risk——每类各有一套 guardrail。积累了可观的领域知识。

**Speaker 结构分析**：自动将说话人分为 primary/incidental/fragmented/non_speech 四类，基于 duration share、short segment rate、non-speech markers 等指标。

**三层对齐策略**：direct (≤5%) → DSP (≤15~20%) → rewrite loop (max 2 retries) → force_dsp review。每一步有明确阈值和 fallback。

**Post-edit 工作流**：overwrite/copy_as_new 两种提交策略，编辑态独立文件目录，text-audio sync hard gate，idle scanner 自动回收。

**Voice calibration 三入口 sidecar**：手动校准 → clone 后自动校准 → review 提交前预热校准，汇入同一套 inflight dedupe + merge 语义。

### 3.3 测试质量

**契约级守卫测试**（phase1_guards, legacy_cleanup_guards）：AST 扫描确保付费 API 不被误调、模块结构不变、前端后端 path 对齐——测试的是不变量不会漂移。

**辅助工具设计合理**：`FakeProcess`, `RecordingRunner`, `write_process_project` 使测试不依赖真实外部服务。

---

## 4. 代码质量问题

### 4.1 巨型函数（God Functions）

| 函数 | 文件 | 行数 | 核心问题 |
|---|---|---|---|
| `_report_job_metering()` | `src/pipeline/process.py:454-905` | **约 452 行** | 计算 30+ 类指标，含真实网络调用；已有窄单测，但主体仍难以按指标维度独立覆盖 |
| `intercept_create_job()` | `gateway/job_intercept.py:710-1085` | **约 376 行** | 混入多种职责：鉴权、配额、plan gate、YouTube 探测、display name 生成、上游转发、PG 写入、回滚补偿 |
| `_align_one()` | `src/services/alignment/aligner.py:565-750` | **约 186 行** | 4 层嵌套条件，force_dsp/dsp/rewrite 三条路径纠缠在一个方法里 |
| `_attempt_rewrite_loop_unguarded()` | `src/services/alignment/aligner.py:807-950` | **约 144 行** | 5 层嵌套（`for → if → if → try → if`） |

**建议**：拆分方法，每个方法只做一件事。对齐决策树的 direct/dsp/rewrite 三个分支应该是独立的 `_try_direct()`, `_try_dsp()`, `_try_rewrite()` 方法。

### 4.2 `getattr` 滥用

在 `aligner.py` 中对 `DubbingSegment`（一个 `slots=True` 的固定字段 dataclass）频繁使用 `getattr`：

```
aligner.py:107   getattr(segment, "first_pass_cn_text", "")
aligner.py:304   getattr(segment, "dubbing_mode", DUBBING_MODE_DUB)
aligner.py:327   getattr(segment, "en_text", "")
aligner.py:526   getattr(segment, "tts_audio_path", None)
aligner.py:602   getattr(segment, "pre_tts_rewrite_direction", "")
```

当前核验约有 13 处 `getattr(segment, ...)`。

**后果**：字段名拼写错误被静默吞掉（返回默认值），不会报 `AttributeError`。在已知 dataclass 上使用 `getattr` 在 90% 的场景下是多余的防御。

**建议**：替换为直接字段访问 `segment.first_pass_cn_text`。

### 4.3 God Class：`GeminiTranslator`

`src/services/gemini/translator.py` — 当前 **2,731 行**；`GeminiTranslator` 类内约 **30 个方法**，全文件约 **85 个函数/方法定义**。它承担三种不同职责：

| 职责 | 代表方法 | 应归属 |
|---|---|---|
| 翻译 | `translate()`, `translate_probe()`, `_build_prompt()`, `_parse_response()` | `TranslationProvider` 实现 |
| 说话人推断 | `infer_speaker_names()`, `review_speaker_labels()` | `SpeakerAttributionService` |
| LLM 调用分发 | `_call_by_model()`, `_call_mimo_text()`, `_call_openai_compatible()`, `_call_task_with_fallback()` | `LLMRouter`（部分已抽取） |

此外 `Any` 类型使用仍偏多（当前按 `\bAny\b` 核验有 17 处提及，包含 `self.client: Any`, `self._usage_meter: Any`, `payload: Any` 等），至少一部分可以用 `Protocol`、TypedDict 或更窄的结构类型替换。

**建议**：拆为三个独立类：`GeminiTranslationProvider`, `GeminiSpeakerAttributionService`, `LLMClientRouter`。

### 4.4 God Module：`gateway/job_intercept.py`

**3,300 行**单一文件，包含 **8 种不同关注点**：

1. 任务生命周期（create/list/get/delete）
2. 配额与额度（reserve/commit/release/compensate）
3. YouTube 探测（duration/title 提取）
4. Display name 生成（5 分支决策树）
5. Post-edit 工作流（enter/cancel/commit/segments/voice-map）
6. 日志脱敏（admin-only redacted logs）
7. R2 下载路由（proactive redirect + lazy fallback）
8. 音色质量同步（voice selection approve + quality tier aggregation）

**建议**：拆为 `jobs_lifecycle.py`, `jobs_quota.py`, `jobs_display_name.py`, `jobs_post_edit.py`, `jobs_download.py` 等子模块，由主 `job_intercept.py` 做薄路由。

### 4.5 `Any` 类型过度使用

```python
# translator.py
self.client: Any | None = None        # :418
self._usage_meter: Any | None = None  # :422 — UsageMeter 有已知类型
payload: Any                           # :2611 — 总是 dict

# tts_generator.py
job_record: object                     # 通篇是 dict|object 双重人格

# job_intercept.py
"# type: ignore[arg-type]"            # :401, :824 — yt-dlp 返回 object
```

**建议**：
- `self._usage_meter` → `UsageMeter | None`
- `job_record` → 定义 `JobSnapshot` dataclass/TypedDict，在入口处统一转换
- `payload: Any` → `payload: dict[str, object]`
- yt-dlp 返回值 → 在解析点做 `assert isinstance()` 收窄类型

### 4.6 危险的异常捕获

```python
# src/services/alignment/aligner.py:465
except BaseException:  # ← 会捕获 KeyboardInterrupt, SystemExit, GeneratorExit
```

`BaseException` 是 Python 异常体系根，包含了不应该被业务代码轻易捕获的系统异常。这里需要重新审视异常边界和清理语义。

二次核验补充：当前代码在捕获后会 `stop_event.set()`、取消 pending futures，并立即 `raise` 重新抛出，因此它不是“吞掉 Ctrl+C”的确认 bug；更准确的问题是业务清理逻辑依赖捕获系统异常，语义需要显式测试或改成更窄的异常路径加 `finally` 清理。

**建议**：不要机械替换为 `except Exception`。先补一个并发对齐失败/中断清理测试，确认 pending futures 和 paid work guard 的语义；再决定是否改为 `except Exception` + `finally`。

### 4.7 错误分类的脆弱性

```python
# src/services/gemini/translator.py:209-231
def classify_llm_error(exc):
    if "rate limit" in str(exc).lower():
        return "rate_limit_error"
    elif "quota" in str(exc).lower():
        return "quota_error"
```

靠字符串匹配判断错误类型。如果提供商改了措辞，分类静默退化到 `unknown_error`，无任何告警。

---

## 5. 编码规范问题

### 5.1 Provider 注册模式：四种不同风格

| 文件 | 注册方式 | 分发方式 |
|---|---|---|
| `src/services/tts_provider.py` | 无注册表 | `if/elif` 硬编码 |
| `src/services/llm_registry.py` | 静态 dict | key lookup（最佳实践） |
| `gateway/payment_providers.py` | dict + `get_provider()` | key lookup |
| `gateway/sms_provider.py` | 无注册表 | `if/elif` 硬编码 |

同一文件 `payment_providers.py` 内，`StripeProvider` 和 `WechatPayProvider` 继承 `_StubProvider`，但 `AlipayProvider` 和 `FakeProvider` 不继承——基类使用不一致。

**建议**：统一为 `_PROVIDERS: dict[str, ProviderProtocol] = {}` + `get_provider(name) → ProviderProtocol` 模式。

### 5.2 配置机制：四种不同模式

| 机制 | 使用位置 | 配置源 |
|---|---|---|
| `config_loader.py` + `autodub.local.json` | pipeline 模块（tts_provider 等） | JSON 文件 |
| `admin_settings.json` + TTL 缓存 | `llm_registry.py` | JSON 文件（不同路径） |
| Pydantic `settings` 对象 | Gateway 全部模块 | 环境变量 |
| 直接 `os.environ` 读取 | `sms_provider.py`, `AlipayProvider` | 环境变量（无封装） |

**建议**：定义统一的 `AppConfig` 协议，所有模块通过 `get_config()` 读取，具体来源（env/json/admin）由适配层决定。

### 5.3 路由注册：机械重复

`gateway/main.py:278-307`（闭区间）— 22 个 `app.include_router(xxx_router)` 调用。应改为列表 + 循环。

### 5.4 API 无版本化

所有 `/job-api/*` 端点没有版本前缀（如 `/v1/job-api/`）。移动端 App、第三方 API 接入、灰度发布场景下会出问题。

**建议**：不要直接重命名现有 `/job-api/*`。当前 `/job-api` 相关引用约 119 处，其中运行时代码/配置约 71 处（Gateway、frontend、pipeline callback、Caddy 等），测试约 48 处；文档引用另计。更稳妥的路径是先增加兼容 alias 或 Caddy rewrite，补路由契约测试，再逐步把客户端 base URL 配置化。

### 5.5 中文字符串散落全代码库

UI 文案、错误消息、审核提示文本与业务逻辑混在一起，散落在 `process.py`、`gateway/` 各文件中。

**建议**：提取 `src/messages/zh_CN.py`，将所有用户可见字符串集中管理。

---

## 6. 架构优化建议

### 6.1 `process.py` 拆分为 Stage 模块

当前 8,430 行单文件。建议：

```
src/pipeline/
├── process.py                    # ProcessPipeline 类 + run() 编排 (~500行)
├── stages/
│   ├── s0_ingestion.py           # YouTube下载、本地源接入
│   ├── s1_transcription.py       # ASR、音频分离、speaker检测
│   ├── s2_review.py              # 内容合规、3-pass审核、voice profiling
│   ├── s3_translation.py         # Gemini 翻译 + probe translation
│   ├── s4_tts_prep.py            # TTS生成、pre-TTS rewrite、short merge
│   ├── s5_alignment.py           # 对齐编排 + post-alignment修复
│   └── s6_synthesis.py           # 输出调度、metering上报
├── _constants.py                 # 所有阈值常量集中管理
├── _rewrite_policy.py            # Pre-TTS rewrite 策略定义
└── _helpers.py                   # 共享辅助函数
```

每个 stage 模块暴露 `def run_sX(ctx: PipelineContext) -> PipelineContext`，`process.py` 做线性序列调用。

### 6.2 加速双管道收敛

当前 `process.py` 和 `ProjectWorkflow` 两套路径并行。收敛路径：

- **第一步**：S6（synthesis/publish）统一走 `OutputDispatcher`
- **第二步**：S3（translation）和 S4（TTS）复用 `ProjectWorkflow` 的 StageRunner
- **第三步**：process.py 退化为 `ProjectWorkflow` 的薄 adapter

### 6.3 Pre-TTS Rewrite 策略对象化

当前 30+ 常量散落在 process.py，决策逻辑嵌入方法深处。建议：

```python
@dataclass(frozen=True)
class RewritePolicy:
    min_target_ms: int
    overshoot_ratio: float
    undershoot_ratio: float
    max_change_ratio: float

REWRITE_POLICIES = {
    "default": RewritePolicy(min_target_ms=8_000, ...),
    "short_content": RewritePolicy(min_target_ms=2_000, ...),
    "high_shrink_risk": RewritePolicy(...),
    "mid_undershoot_risk": RewritePolicy(...),
    "long_undershoot_risk": RewritePolicy(...),
}
```

好处：策略可在测试中独立验证，新增场景只需加一个 entry。

### 6.4 Gateway 中间件链重构

`intercept_create_job` 的 18 步操作拆为独立中间件：

```python
INTERCEPT_CHAIN = [
    validate_service_mode,
    check_concurrency_limit,
    check_credits,
    probe_source_metadata,
    enforce_duration_limit,
    compute_display_name,
    inject_policy_snapshot,
    forward_to_upstream,
    record_in_postgres,
]
```

每个中间件接收 `(ctx: InterceptContext) -> InterceptContext`，独立可测试，可按 plan 选择性激活。

### 6.5 依赖方向违规修复

```python
# gateway/job_intercept.py:24-31 — 运行时路径 hack
sys.path.insert(0, "src")

# gateway/job_intercept.py:354 — gateway 容器 import pipeline 层代码
from services import config_loader
```

**解决方案**：将共享类型定义和配置 schema 抽取到 `src/shared/` 或独立 `common/` 包，gateway 和 pipeline 都依赖它，消除跨层 import。

### 6.6 `job_record` 类型统一

当前 `job_record` 在 TTSGenerator、SegmentAligner、JianyingDraftRunner 中是 `object | dict | Any` 三重人格：

```python
def _read_job_field(job_record, field, default=None):
    if isinstance(job_record, dict):
        return job_record.get(field, default)
    return getattr(job_record, field, default)
```

**建议**：定义 `JobSnapshot` frozen dataclass，在 Gateway 转交时统一转换，消除所有 `_read_job_field` 和类型分发。

### 6.7 Metering Payload 加 Schema

`_report_job_metering` 构建 50+ 字段的 dict，无任何 schema。Gateway 隐式接受任意 JSON。

**建议**：定义 `JobMeteringPayload` Pydantic model，两端共享。Gateway 做输入验证，pipeline 做构建。

### 6.8 Pipeline Stage 显式化

当前 pipeline 的 S0→S1→...→S6 是 process.py 中硬编码的线性序列。建议：

```python
class PipelineStage(Protocol):
    name: str
    def run(self, ctx: PipelineContext) -> PipelineContext: ...

STAGES = [S0_Ingestion(), S1_Transcription(), ...]
for stage in STAGES:
    ctx = stage.run(ctx)
```

插入新 stage 只需在列表中加一行。

---

## 7. 可扩展性问题

### 7.1 TTS Provider 新增成本

当前新增一个 TTS 提供商需改 **5+ 个文件**：

1. `job_intercept.py` — 加白名单
2. `tts_strategy.py` — 改选择逻辑
3. `tts_generator.py` — 加 `if provider == "new":` 分支
4. `tts_generator.py` — 实现 `_generate_one_new()` 方法（~100 行）
5. `voice_match_resolver.py` — 加分发

各 provider 方法重复相同的 voice-resolution → speed-decision → synthesize → audit 模式（~80-100 行），billed_chars 乘数硬编码而非表驱动。

**建议**：改为注册表模式（见 [5.1](#51-provider-注册模式四种不同风格)），新增只需实现一个类 + 一行注册。

### 7.2 订阅套餐新增成本

新增 `enterprise` 套餐需改：

1. `plan_catalog.py` — 加 `PLANS` 条目
2. `billing.py` — 加 `valid_target_plan_codes()`
3. `job_intercept.py` — 改 plan rank 比较 + `POST_EDIT_LIMITS` dict
4. `pricing_schema.py` — 加字段

Plan rank 当前是隐式推断（`free=0 < plus=1`），应改为显式 `PLANS["plus"].rank` 字段。

### 7.3 数据库查询重复

`job_intercept.py` 中同一模式重复 10+ 次：

```python
await db.execute(select(Job).where(Job.job_id == job_id))
```

**建议**：提取 `JobStore` 类，封装 `get_by_id()`, `get_by_id_for_update()`, `update_status()` 等方法。

### 7.4 前端数据层

当前 4s 轮询 + 手写 fetch。随 Job 数量增长会出现缓存失效、重复请求、竞态条件问题。建议引入 **TanStack Query**（与现有 fetch-based API client 兼容）。

---

## 8. 运维与可观测性

### 8.1 结构化日志不统一

代码库同时存在 `logging` 和大量 `print()` + `flush=True`。当前核验约有 451 处 `print(`，也有约 797 处 logging/logger 相关引用；问题不是“logging 用得少”，而是两套日志形态大量并存、上下文字段不稳定：

```python
print(f"[S1] 视频时长 {actual_minutes:.1f} 分钟...", flush=True)
print(f"[metering] Warning: failed to report job metering: {e}", flush=True)
```

**问题**：
- 无法按级别过滤（INFO/WARNING/ERROR）
- 无法按 job_id 或 stage 做结构化查询
- 无法接入日志聚合系统

**建议**：以 pipeline/gateway 关键路径为先，把用户任务级日志迁移到 `logging`，每个模块注入 `LoggerAdapter` 或等价封装，稳定携带 `{"job_id": ..., "stage": "S4"}` 上下文。低价值的本地调试 `print` 可以后置清理。

### 8.2 Gateway 启动错误静默吞掉

```python
# gateway/main.py:109-119
try:
    await recover_stale_tasks(db)
except Exception:
    pass  # ← 丢失 stale label task 恢复失败信号

# gateway/main.py:121-130
try:
    await background_task_queue.recover_stale(db)
except Exception:
    pass  # ← 丢失 stale background task 恢复失败信号
```

这些失败可能意味着任务永久停留在中间状态，但注释也说明部分异常来自 migration 前表不存在。应区分预期异常与真实恢复失败：表不存在/未迁移可降级为 warning 或 debug，其余异常至少打 ERROR/CRITICAL 日志，并在 health check 或 admin ops 面暴露最近一次恢复状态。

### 8.3 Gateway lifespan 内联代码

`gateway/main.py:79-231` 的 `lifespan` 中有 3 个内联 `async def`（pack_cleanup, project_cleanup, r2_sweeper），各 ~20-25 行。应抽取为独立函数。

### 8.4 CI/CD 配置缺失

当前仓库无 `.github/workflows/` 目录。建议添加最低限度的 CI：

```yaml
# .github/workflows/ci.yml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: python -m pytest -q
      - run: python -m pytest tests/test_phase1_guards.py tests/test_legacy_cleanup_guards.py -q
      - run: cd frontend-next && npm run lint && npx tsc --noEmit
```

---

## 9. 测试覆盖分析

### 9.1 覆盖盲区

| 模块 | 问题 |
|---|---|
| `GeminiTranslator`（2,731 行） | 测试文件存在但未覆盖全部路径（翻译、推断、审核、fallback routing 应各自独立测试） |
| `_report_job_metering`（约 452 行） | 已有发送 internal key 的窄单测（`tests/test_process_pipeline.py:162`、`tests/test_job_metering_writeback.py:376` 起），但 50+ 字段构建、glossary 指标、异常降级等分支仍缺少参数化单测 |
| Post-edit 限额逻辑 | commit 次数/试用 copy_as_new 已有覆盖（`tests/test_gateway_editing_commit_sync.py:426` 起）；`_consume_post_edit_tts_usage` 的 TTS 字数、segment 数、batch 分支仍应补直接单测 |
| Gateway 启动恢复 | `recover_stale_tasks` / `background_task_queue.recover_stale` 失败路径无测试；成功路径可参考 `tests/test_background_task_queue.py:351` |
| Pre-TTS rewrite 决策 | 30+ 常量的决策树——拆分后应对 `_select_rewrite_policy` 做参数化测试 |
| Speaker 结构分析 | `_build_speaker_structure_profiles` 应做纯函数测试 |

### 9.2 缺失的跨进程测试

现有测试以单进程/函数级/AST contract 为主，缺少验证 gateway + job-api 两个服务真实交互的轻量集成测试。Contract test 能守住路由/不变量漂移，但不能替代一次最小双服务 happy path。

---

## 10. Bug 与风险

### 10.1 确认的问题

**Gateway 启动恢复静默失败**（`gateway/main.py:109-130`）：`recover_stale_tasks` 和 `background_task_queue.recover_stale` 恢复失败被 `pass` 吞掉。虽然部分异常可能只是 migration 前表不存在，但真实恢复失败同样会无信号消失。

### 10.2 需确认的风险

**`except BaseException` 清理语义**（`src/services/alignment/aligner.py:465`）：该行捕获 `BaseException`，但当前会取消 pending futures 并重新抛出。它不是确认的 Ctrl+C 吞掉 bug；风险在于清理语义过宽且缺少回归测试。

### 10.3 潜在风险

| 风险 | 严重度 | 说明 |
|---|---|---|
| 单人总线 | 高 | 所有架构知识集中在一个人 |
| 双重管道维护 | 中 | process 和 ProjectWorkflow 并行，新人混淆 |
| process.py 文件过大 | 中 | 8,430 行单文件，变更风险高 |
| 无 CI/CD | 中 | 架构不变量无自动化守卫 |
| LLM 错误分类脆弱 | 低 | 靠字符串匹配，提供商改措辞则失效 |
| API 无版本化 | 中 | 当前运行时代码/配置约 71 处、测试约 48 处引用 `/job-api`，未来移动端/第三方接入时需要兼容迁移 |

---

## 11. 综合改进优先级

| 优先级 | # | 改进项 | 类别 | 预期收益 | 改动风险 |
|---|---|---|---|---|---|
| **P0** | 1 | Gateway 启动恢复失败不静默：区分表不存在与真实恢复失败，补日志/health/admin 状态 | 运维 | 避免后台任务恢复失败无信号 | 极低 |
| **P0** | 2 | 添加最小 `.github/workflows/ci.yml`：pytest 守卫 + frontend type/lint | DevOps | 让架构不变量自动化运行 | 低 |
| **P0** | 3 | `_report_job_metering` 字段构建抽纯函数 + 参数化测试 | 代码质量 | 不跑完整 pipeline 也能覆盖关键计量字段 | 低 |
| **P0** | 4 | 补 `_consume_post_edit_tts_usage` / batch / chars / segments 限额直接单测 | 测试 | 覆盖商业限额边界 | 低 |
| **P0** | 5 | 为 `except BaseException` 清理语义补回归测试，再决定是否改成 `Exception + finally` | 稳定性 | 避免误改并发取消/paid work guard | 低 |
| **P1** | 6 | Metering payload 加 Pydantic/TypedDict schema，Gateway 只接受白名单字段 | 架构 | 契约清晰、减少隐式 JSON 漂移 | 中 |
| **P1** | 7 | `/job-api` 版本化做兼容迁移：alias/rewrite + base URL 配置 + contract tests | 可扩展性 | 未来移动端/第三方接入可演进 | 中 |
| **P1** | 8 | Plan rank 显式化到 Gateway plan catalog/runtime truth | 可扩展性 | 新增套餐少改代码，减少硬编码 rank | 低 |
| **P1** | 9 | TTS Provider 注册表模式 | 可扩展性 | 新增 provider 从多处分发变成单类注册 | 中 |
| **P1** | 10 | Pipeline/Gateway 关键路径日志统一到 logging + job_id/stage 上下文 | 运维 | 可观测性明显提升 | 中 |
| **P2** | 11 | `intercept_create_job` 按鉴权/配额/source probe/上游转发/PG 写入做薄切片抽取 | 架构 | 降低单函数复杂度 | 中 |
| **P2** | 12 | `process.py` 先抽 `_rewrite_policy` / metering / output dispatch 小模块，再逐步 stage 化 | 架构 | 可维护、可导航，避免大爆炸重构 | 中 |
| **P2** | 13 | `GeminiTranslator` 按 translation / speaker attribution / LLM routing 分阶段拆分 | 代码质量 | 单一职责 | 高 |
| **P2** | 14 | `job_record` 统一为 `JobSnapshot` dataclass/TypedDict | 编码规范 | 类型安全 | 中 |
| **P2** | 15 | Pre-TTS rewrite 策略对象化 | 代码质量 | 可测试、可配置 | 中 |
| **P2** | 16 | 前端引入 TanStack Query 或等价数据层，先覆盖 job detail/projects/support 高频轮询 | 前端 | 缓存、去重、竞态控制 | 中 |
| **P3** | 17 | `getattr(segment, ...)` 逐步替换为直接字段访问或显式兼容 adapter | 代码质量 | 类型安全 | 低 |
| **P3** | 18 | Router 批量注册 | 代码质量 | 减少样板 | 极低 |
| **P3** | 19 | 数据库查询提取 `JobStore` 类 | 代码质量 | 减少重复 | 低 |
| **P3** | 20 | Gateway lifespan 内联代码抽取 | 代码质量 | 可读性 | 极低 |
| **P3** | 21 | 用户可见中文文案集中管理，先覆盖营销/支付/审核错误文案 | 编码规范 | 可维护 | 中 |
| **P3** | 22 | 统一配置读取机制，先定义边界和迁移表，不立即替换所有入口 | 编码规范 | 一致性 | 高 |

---

## 附录：已阅读的核心文件清单

### 架构图（docs/graphs/）
- `GITNEXUS_PROJECT_GRAPH.md` — 项目总图
- `GITNEXUS_WORKFLOW_CORE_GRAPH.md` — 工作流内核
- `GITNEXUS_COMMERCIALIZATION_GRAPH.md` — 商业化
- `GITNEXUS_REVIEW_GRAPH.md` — 审核流
- `GITNEXUS_EDITING_POST_EDIT_GRAPH.md` — 编辑/后处理
- `GITNEXUS_STORAGE_DELIVERY_R2_GRAPH.md` — 存储与交付
- `GITNEXUS_ADMIN_OPS_CALIBRATION_GRAPH.md` — Admin/Ops/校准

### 核心代码
- `main.py`（完整阅读）
- `src/core/enums.py`, `exceptions.py`, `models.py`, `project_model.py`, `retry.py`, `artifact_index.py`
- `src/pipeline/process.py`（完整阅读，8,430 行）
- `src/services/alignment/aligner.py`
- `src/services/gemini/translator.py`
- `src/services/tts/tts_generator.py`, `voice_reranker.py`
- `src/services/jobs/editing_commit.py`, `jianying_draft_runner.py`
- `src/services/subtitles/ensure_whisper_alignment.py`
- `src/services/usage_meter.py`, `review_state.py`, `state_manager.py`
- `src/services/config_loader.py`, `tts_provider.py`, `llm_registry.py`
- `src/modules/workflow/project_workflow.py`
- `src/modules/alignment/alignment_orchestrator.py`
- `src/modules/subtitles/cue_pipeline.py`
- `src/modules/draft/draft_writer.py`
- `src/modules/output/output_dispatcher.py`
- `src/modules/translation/translator.py`
- `src/utils/atomic_io.py`

### Gateway
- `gateway/main.py`, `config.py`, `auth.py`, `auth_phone.py`
- `gateway/billing.py`, `plan_catalog.py`, `pricing_runtime.py`, `pricing_schema.py`
- `gateway/job_intercept.py`（部分阅读，3,300 行）
- `gateway/payment_providers.py`, `sms_provider.py`

### 前端
- `frontend-next/src/app/(marketing)/page.tsx`
- `frontend-next/src/app/(app)/workspace/[jobId]/page.tsx`
- `frontend-next/src/app/(app)/workspace/[jobId]/edit/page.tsx`
- `frontend-next/src/lib/api/client.ts`, `config.ts`, `jobs.ts`, `editing.ts`
- `frontend-next/src/components/` 和 `src/features/` 结构

### 测试
- `tests/test_pipeline_duration_check.py`
- `tests/test_aligner.py`
- `tests/test_phase1_guards.py`
- `tests/test_editing_commit.py`
- `tests/job_test_helpers.py`
- `tests/conftest.py`

### DevOps
- `docker-compose.yml`, `Dockerfile`, `Caddyfile`
- `scripts/run_remote_workbench_service.py`

### 文档
- `README.md`, `AGENTS.md`, `CLAUDE.md`, `DESIGN.md`
- `docs/QUICKSTART.md`
- `docs/plans/`（扫描 45 个文件，重点阅读 2026-05-09 两个计划）

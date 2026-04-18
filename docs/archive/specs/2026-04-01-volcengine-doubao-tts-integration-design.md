# 豆包语音合成模型 2.0 集成实施计划（V3 / API Key 版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将火山引擎「豆包语音合成模型 2.0」作为第四个 TTS provider 集成到项目中，并确保管理员在后台切换后，新建任务真正使用该 provider。

**Architecture:** 本次不再使用 V1 一次性 HTTP 接口，改为接入官方推荐的 **V3 HTTP Chunked 单向流式接口**。Provider 负责发起 V3 流式请求、累计返回的 PCM 音频帧、一次性封装成 WAV 后交给现有 `TTSGenerator`，从而不改下游产物与缓存命名。同时补齐 Gateway policy 生效链路和 `frontend-next` 设置页选项。

**Tech Stack:** Python 3.12 / requests / FastAPI / Next.js 16 / 火山引擎豆包语音 V3 HTTP Chunked API

> **修订说明**：本版替换此前的 V1 过渡方案。当前工作区里若已存在 `src/services/tts/volcengine_tts_provider.py` 与对应测试的 V1 草稿，应按本计划**原地改写为 V3**，不再保留 `Authorization: Bearer;{token}`、`appid/token/cluster`、`1024 字节 V1 guard` 这套实现假设。

---

## 0. 协议决策

火山引擎豆包语音当前常见接入方式：

| 协议 | 说明 | 官方口径 | 本次结论 |
|------|------|---------|---------|
| V1 一次性 HTTP | 同步返回完整音频 | 不推荐 | 不使用 |
| V3 HTTP Chunked 单向流式 | 服务端流式推送音频帧 | 推荐 | **采用** |
| V3 SSE 单向流式 | 更偏浏览器消费 | 推荐 | 暂不采用 |
| V3 WebSocket | 适合实时对话/双向交互 | 推荐 | 暂不采用 |

**本次决策：采用 `POST https://openspeech.bytedance.com/api/v3/tts/unidirectional`。**

理由：
1. 这是官方推荐的 V3 路径，避免继续在“不推荐”的 V1 上追加代码。
2. 我们当前场景是服务端逐段合成，不需要 WebSocket 双向能力。
3. `HTTP Chunked` 在 Python 后端里比 SSE 更直接，便于我们按块解析并累计音频。
4. V3 不再受 V1 的 `1024` 字节短文本硬限制约束，后续扩展空间更大。
5. 控制台已开通服务，且 V3 文档与当前控制台的 API Key/项目体系匹配。

**补充决策：**
- 本期仍然保持 `TTSGenerator` 对下游输出 `.wav` 文件这一行为不变。
- 因 V3 流式接口不应直接请求 `wav`，Provider 将请求 `pcm`，然后在本地一次性封装为 WAV。

---

## 1. 背景与决策依据

### 1.1 为什么集成豆包 TTS

| 对比维度 | CosyVoice（当前快捷版） | 豆包 2.0（新增） | MiniMax（当前工作台版） |
|---------|----------------------|---------------|---------------------|
| 单价 | ¥2/万字符 | **¥3/万字符**（资源包 ¥2.8） | ¥7/万汉字（1 汉字=2 字符） |
| 预置音色 | 68 个 | **300+** | 100+ |
| 音质 | 良好 | **很好（大模型驱动）** | 极好 |
| 情感表达 | 无 | **支持上下文语义驱动** | 情感标签手动控制 |
| 首包延迟 | 0.4-0.5s | **V3 流式更适合低延迟场景** | <0.25s |
| API 协议 | WebSocket only | **HTTP Chunked / SSE / WebSocket** | HTTP |
| 声音克隆 | 免费复刻 | 赠送试用 | ¥9.9/音色解锁 |

### 1.2 定位

- **CosyVoice**：免费/快捷版默认，成本最低。
- **豆包 2.0**：快捷版升级选项，兼顾音色丰富度与音质。
- **MiniMax**：工作台版/高端选项，质量最高但成本最高。
- **MiMo**：实验性/限时免费。

### 1.3 前期投入

| 项目 | 费用 |
|------|------|
| 注册 + 实名认证 | ¥0 |
| 试用额度 | ¥0（20,000 字符 / 半年） |
| 最小资源包 | ¥28（10 万字/年） |
| 开发集成 | ~1-2 天工作量 |

---

## 2. 火山引擎 V3 API 技术规格

### 2.1 端点

本计划采用：

```text
POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
```

说明：
- 官方同页还提供 SSE 路径，但本期后端服务端消费更适合 `unidirectional` 的 chunked 返回。
- 文档中也出现 `/api/v3/tts/bidirection`，但那是另一组流式协议说明；本期不需要双向交互。

### 2.2 认证方式

V3 不再使用 V1 的 `Authorization: Bearer;{token}`。

**请求头：**

```text
X-Api-App-Id: <APP ID>
X-Api-Access-Key: <Access Token>
X-Api-Resource-Id: seed-tts-2.0
X-Api-Request-Id: <uuid>
Content-Type: application/json
```

关键说明：
1. 控制台页面虽然叫“API Key 管理”，但 V3 文档里的真实请求头仍是 `APP ID + Access Key + Resource ID` 组合。
2. `X-Api-Access-Key` 对应控制台里的服务访问密钥/Access Token，不是 V1 的 `Authorization` header。
3. `X-Api-Resource-Id` 本期固定按官方示例使用 `seed-tts-2.0`，部署前再做一次控制台核对。

### 2.3 请求格式

官方示例核心结构如下：

```json
{
  "user": {
    "uid": "388808087185088"
  },
  "req_params": {
    "speaker": "zh_female_shuangkuaisisi_moon_bigtts",
    "text": "需要合成的文本。",
    "audio_params": {
      "format": "pcm"
    }
  }
}
```

本项目约定：
1. `speaker` 使用 `segment.voice_id`，为空时回退到默认公有音色。
2. `text` 直接使用 `tts_text`。
3. `audio_params.format` 固定为 `pcm`，避免流式 `wav` 重复返回 header。
4. `user.uid` 固定使用稳定字符串，例如 `aivideotrans`。
5. 本期不启用 `additions.context_texts`，保持最小可用集成。

### 2.4 响应与错误模型

官方文档给出的关键返回语义：

- `code = 0`：普通音频帧，`data` 字段为 base64 音频分片，应累计。
- `code = 20000000`：结束帧，表示本次流式返回完成。
- `code = 40402003`：`TTSExceededTextLimit`，文本超出服务端限制。
- `code = 45000000`：客户端参数/权限/资源错误。
- `code = 55000000`：服务端内部错误。

Provider 约束：
1. 只要收到 `code = 0` 且含 `data`，就解码并累计音频 bytes。
2. 必须收到结束帧 `20000000` 才算成功；没有 finish 视为协议失败。
3. 出现 `40402003 / 45000000 / 55000000` 时，抛出明确的 `VolcEngineTTSError`。
4. 保留/记录 `X-Tt-Logid` 便于排障。

### 2.5 输出格式策略

这是本次 V3 方案里最关键的实现约束：

1. **不要在流式模式下请求 `wav`**。
2. Provider 请求 `pcm`，把所有音频帧按顺序拼接。
3. 使用 Python 标准库 `wave` 一次性包装成单个 WAV 文件。
4. `tts_generator.py` 和现有缓存路径仍写出 `.wav` 文件，不改下游逻辑。

推荐本期固定参数：

```text
format = pcm
sample_rate = 24000
channels = 1
sample_width = 2
```

### 2.6 环境变量

本期标准化为：

```bash
VOLCENGINE_TTS_APP_ID=<APP ID>
VOLCENGINE_TTS_ACCESS_KEY=<Access Token>
VOLCENGINE_TTS_RESOURCE_ID=seed-tts-2.0
VOLCENGINE_TTS_DEFAULT_SPEAKER=zh_female_shuangkuaisisi_moon_bigtts
```

迁移兼容建议：
- 代码可以临时兼容读取旧变量 `VOLCENGINE_TTS_APPID` / `VOLCENGINE_TTS_ACCESS_TOKEN`，但文档、部署和最终实现都应以新变量名为准。

---

## 3. 文件结构

> 当前工作区若已存在 V1 草稿文件，均按下列职责**重写/改写**，不要在旧实现上继续追加 V1 逻辑。

| 文件 | 职责 |
|------|------|
| `src/services/tts/volcengine_tts_provider.py` | V3 HTTP Chunked Provider；负责凭据解析、请求头构造、流式 JSON 解析、PCM 聚合、WAV 包装、错误映射 |
| `tests/test_volcengine_tts_provider.py` | Provider 单元测试；覆盖 headers、chunked 流、finish 事件、业务错误、PCM→WAV |
| `src/services/tts/tts_strategy.py` | 注册 `volcengine` provider、RPM、fallback |
| `tests/test_tts_strategy.py` | provider 注册与 fallback 测试 |
| `src/services/tts/tts_generator.py` | dispatch 到 VolcEngine provider；保持产出 `.wav` 不变 |
| `tests/test_tts_generator.py` | VolcEngine dispatch 成功与异常包装测试 |
| `gateway/job_intercept.py` | `compute_job_policy()` 改为读取后台设置，真正让新任务快照使用管理员选中的 provider |
| `tests/test_gateway_job_policy.py` | Gateway policy 层生效测试 |
| `tests/test_gateway_create_job.py` | 创建任务时快照 `tts_provider` 写入测试 |
| `frontend-next/src/app/admin/settings/page.tsx` | 管理后台 TTS 选项加 `volcengine` |

---

## 4. 实施任务

### Task 1: 将 VolcEngine provider 从 V1 重写为 V3 Chunked

**Files:**
- Modify: `src/services/tts/volcengine_tts_provider.py`
- Modify: `tests/test_volcengine_tts_provider.py`

- [ ] **Step 1: 先写 V3 provider 失败测试**

测试至少覆盖：
1. 空文本直接报错。
2. 请求头使用 `X-Api-App-Id / X-Api-Access-Key / X-Api-Resource-Id`。
3. 解析多个 chunk 音频帧并合成单个 WAV。
4. 收到业务错误码时抛出明确异常。
5. 没有 finish 帧时抛错。
6. 不再触发 V1 的 `1024` 字节本地 guard。

建议测试骨架：

```python
def test_synthesize_uses_v3_headers(monkeypatch):
    ...
    assert headers["X-Api-App-Id"] == "app-id"
    assert headers["X-Api-Access-Key"] == "access-key"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"

def test_synthesize_accumulates_pcm_chunks_and_returns_single_wav(monkeypatch):
    ...
    assert result[:4] == b"RIFF"

def test_synthesize_raises_when_finish_event_missing(monkeypatch):
    with pytest.raises(VolcEngineTTSError, match="finish"):
        synthesize("测试文本")

def test_long_text_is_not_rejected_by_v1_guard(monkeypatch):
    long_text = "测" * 500
    ...
    with pytest.raises(VolcEngineTTSError) as exc:
        synthesize(long_text)
    assert "1024" not in str(exc.value)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_volcengine_tts_provider.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 V3 provider**

实现要点：
1. `_resolve_credentials()` 读取 `APP_ID / ACCESS_KEY / RESOURCE_ID`，并可临时兼容旧变量名。
2. 使用 `requests.Session().post(..., stream=True)`。
3. `iter_lines()` 逐行解析每个 JSON chunk。
4. `code == 0` 时累计 `data` 中的 base64 音频。
5. `code == 20000000` 时标记 finish。
6. 使用 `wave` 把累计的 PCM 封装成 WAV bytes。
7. 将 `40402003 / 45000000 / 55000000` 转成可读错误。

推荐函数拆分：

```python
def _resolve_credentials() -> tuple[str, str, str]: ...
def _build_headers(...) -> dict[str, str]: ...
def _iter_chunk_events(response) -> Iterator[dict[str, Any]]: ...
def _pcm_to_wav(pcm_bytes: bytes, *, sample_rate: int = 24000) -> bytes: ...
def synthesize(text: str, voice_id: str = DEFAULT_SPEAKER, ...) -> bytes: ...
```

- [ ] **Step 4: 运行 provider 测试**

Run: `python -m pytest tests/test_volcengine_tts_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/tts/volcengine_tts_provider.py tests/test_volcengine_tts_provider.py
git commit -m "feat: switch volcengine TTS provider to V3 chunked API"
```

---

### Task 2: 对齐 strategy 与 generator 到 V3 语义

**Files:**
- Modify: `src/services/tts/tts_strategy.py`
- Modify: `tests/test_tts_strategy.py`
- Modify: `src/services/tts/tts_generator.py`
- Modify: `tests/test_tts_generator.py`

- [ ] **Step 1: 保留/补齐 strategy 测试**

校验点：
1. `volcengine` 在 `VALID_PROVIDERS` 中。
2. `get_tts_rpm("volcengine") == 60`。
3. `get_fallback_provider("volcengine") == "cosyvoice"`。

- [ ] **Step 2: 调整 VolcEngine dispatch 细节**

约束：
1. `_generate_one_volcengine()` 继续返回 `.wav` 路径。
2. 默认音色改为 V3 公有 speaker，例如 `zh_female_shuangkuaisisi_moon_bigtts`。
3. `selected_voice` 写入实际使用的 speaker。
4. 不在 `tts_generator.py` 里保留任何 V1 `voice_type` 假设。

示意：

```python
voice_id = _normalize_optional_text(getattr(segment, "voice_id", None)) or DEFAULT_SPEAKER
audio_bytes = vc_synthesize(text=tts_text, voice_id=voice_id)
atomic_write_bytes(str(output_path), audio_bytes)
```

- [ ] **Step 3: 补充 generator 测试**

至少覆盖：
1. `provider="volcengine"` 会调用新 provider。
2. provider 抛普通异常时，被包装成 `TTSGenerationError("VolcEngine: ...")`。
3. 写出的仍是 `.wav` 文件。

- [ ] **Step 4: 运行 strategy / generator 测试**

Run:

```bash
python -m pytest tests/test_tts_strategy.py -v
python -m pytest tests/test_tts_generator.py -v -k "volcengine or strategy"
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/tts/tts_strategy.py tests/test_tts_strategy.py src/services/tts/tts_generator.py tests/test_tts_generator.py
git commit -m "feat: wire volcengine V3 provider into TTS routing"
```

---

### Task 3: 打通 Gateway policy 生效链路

**Files:**
- Modify: `gateway/job_intercept.py`
- Modify: `tests/test_gateway_job_policy.py`
- Modify: `tests/test_gateway_create_job.py`

- [ ] **Step 1: 先写 Gateway 生效测试**

至少覆盖：
1. `express` 模式下读取 `settings.express_tts_provider`。
2. `studio` 模式下读取 `settings.studio_tts_provider`。
3. 创建任务后，DB 快照中的 `tts_provider` 等于 `volcengine`。

- [ ] **Step 2: 修改 `compute_job_policy()`**

约束：
1. 从 `admin_settings` 读取 provider，不再硬编码 `express -> cosyvoice / studio -> minimax`。
2. 不要从 `src/services/tts/tts_strategy.py` 跨层 import `VALID_PROVIDERS`；Gateway 层本地定义允许值或本地 fallback。
3. 当配置值无效时，`express` 回退到 `cosyvoice`，`studio` 回退到 `minimax`。

- [ ] **Step 3: 运行 Gateway 测试**

Run:

```bash
python -m pytest tests/test_gateway_job_policy.py -v
python -m pytest tests/test_gateway_create_job.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add gateway/job_intercept.py tests/test_gateway_job_policy.py tests/test_gateway_create_job.py
git commit -m "feat: make gateway honor admin-selected TTS provider"
```

---

### Task 4: 更新 `frontend-next` 管理后台

**Files:**
- Modify: `frontend-next/src/app/admin/settings/page.tsx`

- [ ] **Step 1: 增加 `volcengine` 选项**

```typescript
{ 
  value: "volcengine",
  label: "豆包语音合成 2.0（V3）",
  description: "火山引擎 V3 流式接口，音色丰富，适合作为快捷版升级选项"
}
```

- [ ] **Step 2: 确保只改 `frontend-next`**

不修改旧 `frontend` 目录。

- [ ] **Step 3: 前端检查**

Run:

```bash
cd frontend-next
npm run lint
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend-next/src/app/admin/settings/page.tsx
git commit -m "feat: expose volcengine V3 option in admin settings"
```

---

### Task 5: 回归测试与真实 smoke 验证

- [ ] **Step 1: 运行关键回归**

```bash
python -m pytest tests/test_volcengine_tts_provider.py \
  tests/test_tts_strategy.py \
  tests/test_tts_generator.py \
  tests/test_gateway_job_policy.py \
  tests/test_gateway_create_job.py \
  tests/test_tts_routing_invariants.py -v --tb=short
```

Expected: 全部通过

- [ ] **Step 2: 本机真实 smoke test（可选，但建议）**

前提：已配置真实 V3 环境变量。

验证目标：
1. 能成功请求 V3 接口。
2. 返回的 chunk 可累计成可播放 WAV。
3. 日志里能看到 finish 帧和 `X-Tt-Logid`。

- [ ] **Step 3: Commit**

```bash
git add .
git commit -m "test: verify volcengine V3 integration end to end"
```

---

## 5. 部署检查清单

### 5.1 远程部署步骤

1. 在远程 `.env` 写入：

   ```bash
   VOLCENGINE_TTS_APP_ID=<APP ID>
   VOLCENGINE_TTS_ACCESS_KEY=<Access Token>
   VOLCENGINE_TTS_RESOURCE_ID=seed-tts-2.0
   VOLCENGINE_TTS_DEFAULT_SPEAKER=zh_female_shuangkuaisisi_moon_bigtts
   ```

2. 部署修改后的 Python 服务代码并重启应用容器。
3. 部署 `gateway` 代码并重建镜像。
4. 部署 `frontend-next` 并重建镜像。
5. 后台设置页把 `express` 或 `studio` 的 TTS provider 切换到 `volcengine`。
6. 新建测试 job 验证。

### 5.2 验证步骤

1. 后台设置页能看到「豆包语音合成 2.0（V3）」选项。
2. 切换为 `volcengine` 后，新建任务的 job record 中 `tts_provider = volcengine`。
3. 运行日志显示 `[VolcEngine]` 且有 finish/成功日志。
4. 产出的音频文件是单个可播放 WAV，而不是坏掉的流式 wav 片段。
5. 切回 `cosyvoice` 或 `minimax` 后无回归。

### 5.3 回滚

1. 后台把 TTS provider 切回 `cosyvoice` 或 `minimax`。
2. 如需回滚代码，优先回滚 `volcengine_tts_provider.py`、`tts_generator.py`、`gateway/job_intercept.py`、`frontend-next` 设置页。
3. 保留数据库中的历史 `tts_provider` 快照，不做破坏性清理。

---

## 6. 待确认事项（V3 版）

在正式执行前需确认：

1. [x] 已完成火山引擎账号注册、实名认证，并开通豆包语音相关服务。
2. [ ] 已在目标项目下实际拿到并验证 `APP ID / Access Token / Resource ID(seed-tts-2.0)`。
3. [x] 本次协议已确定为 **V3 HTTP Chunked 单向流式**，不再走 V1。
4. [x] V3 请求头格式已核实：`X-Api-App-Id / X-Api-Access-Key / X-Api-Resource-Id`。
5. [ ] 默认公有 speaker 需在当前项目权限下再核对一次；本计划暂定 `zh_female_shuangkuaisisi_moon_bigtts`。
6. [x] 官方试用额度已核实：`20,000 字符 / 半年`。
7. [ ] 结合本次真实 smoke test 与回归范围，确认试用额度是否足够。
8. [ ] 本期是否启用 `context_texts`；若不启用，先保持最小请求体。
9. [x] Gateway policy 生效链路已纳入实施范围。
10. [x] 输出格式策略已确定为 `pcm -> 单个 WAV`，不直接请求流式 `wav`。

---

## 7. 后续扩展（不在本次范围）

- 改接 SSE 路径供浏览器或调试工具直接消费。
- 升级到 WebSocket 或双向流式协议。
- 对接豆包音色 catalog 与后台动态音色选择。
- 启用 `context_texts`、字幕时间戳、usage token 回传等高级能力。
- 集成声音克隆 / 角色映射。
- 补齐 `src/services/tts_provider.py` 的统一 provider 边界。

---

## 8. 官方技术文档链接

- [HTTP Chunked/SSE 单向流式 V3](https://www.volcengine.com/docs/6561/1598757?lang=zh)
- [控制台使用 FAQ](https://www.volcengine.com/docs/6561/196768?lang=zh)
- [API Key 使用](https://www.volcengine.com/docs/6561/1816214?lang=zh)
- [大模型音色列表接口](https://www.volcengine.com/docs/6561/2160690?lang=zh)
- [豆包语音合成 2.0 能力介绍](https://www.volcengine.com/docs/6561/1871062?lang=zh)
- [计费概述](https://www.volcengine.com/docs/6561/1359369?lang=zh)
- [计费说明](https://www.volcengine.com/docs/6561/1359370?lang=zh)
- [QPS/并发查询接口](https://www.volcengine.com/docs/6561/1476626?lang=zh)


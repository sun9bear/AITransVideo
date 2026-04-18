# 豆包 TTS 集成计划审阅备注

对应原计划文档：
- `docs/specs/2026-04-01-volcengine-doubao-tts-integration-design.md`

审阅日期：
- 2026-04-01

审阅范围：
- 对照当前仓库代码结构，评估该计划是否能真正落地
- 重点核实原计划第 6 节“待确认事项”
- 必要时对照火山引擎官方文档核实会随时间变化的事实

---

## 1. 总体结论

这份计划可以作为一次探索性接入草案，但在正式实施前还需要先修正几个关键问题：

1. 计划目前只覆盖了旧的 `ProcessPipeline -> TTSGenerator` 链路，没有覆盖仓库里已经存在的统一真实 TTS provider 边界。
2. 计划宣称“供管理员在后台按需切换使用”，但当前新任务实际使用哪个 provider，是由 Gateway 的 `compute_job_policy()` 在创建任务时硬编码写入快照决定的；只改后台页面和 `tts_strategy.py` 并不能让新任务真正切到 `volcengine`。
3. 计划选用的是火山引擎当前仍提供、但官方已标注为“不推荐”的 V1 HTTP 一次性接口；如果要做正式生产接入，应先明确是“V1 快速接入验证”还是“V3 正式接入”。
4. 计划没有处理 V1 单次短文本接口的长度限制，直接把整段文本送出会有运行时失败风险。
5. 计划中的鉴权 header 写法前后不一致，且示例代码里写成了 `Bearer;{token}`，需要明确这是 V1 的正式写法，而不是继续保留为待确认项。

结论上，我建议把这份计划先修订后再开工，而不是直接照文档执行。

---

## 2. 与当前代码结构的对照结论

### 2.1 当前仓库中实际存在两套 TTS 接入面

当前仓库至少有两条与 TTS 相关的主路径：

1. 旧链路 / web-console 兼容链路
   - `src/pipeline/process.py`
   - `src/services/tts/tts_generator.py`

2. 模块化工作流 / 统一真实 provider 边界
   - `main.py`
   - `src/services/tts_provider.py`

如果本次目标只是给 web-console 兼容链路追加一个 provider，那么原计划写法基本匹配。

如果本次目标是“项目的第四个 TTS provider”，那么当前计划范围偏窄，因为它没有把 `src/services/tts_provider.py` 这条正式边界一起纳入。

### 2.2 “管理员后台可切换”目前并不会生效

当前任务策略是在创建任务时由 Gateway 写入快照：

- `gateway/job_intercept.py` 中 `compute_job_policy()` 目前固定返回：
  - `express -> cosyvoice`
  - `studio -> minimax`

这意味着：

1. 即使后台设置页出现了 `volcengine` 选项
2. 即使 `tts_strategy.py` 接受 `volcengine`
3. 新任务也依然会被 Gateway 固定写成 `cosyvoice` 或 `minimax`

因此，若目标是“管理员按需切换”，至少还需要把以下内容纳入实施范围：

- `gateway/job_intercept.py`
- `tests/test_gateway_job_policy.py`
- 与 admin settings 读取逻辑相关的测试

### 2.3 `gateway/admin_settings.py` 也不应写成“无需修改”

原计划写法是“`gateway/admin_settings.py` 无需改（tts_provider 是 str，volcengine 作为合法值由 tts_strategy 校验）”。

这在类型上是成立的，但在产品行为上不够：

1. 后台保存了什么值，不等于新建任务就会消费这个值
2. 真正的任务快照来自 `compute_job_policy()`
3. 如果不把 admin settings 与 policy 计算关联起来，后台选择项就只是“可保存但不生效”的配置

因此，这一条建议从“无需改”改成：

- 是否修改 `gateway/admin_settings.py` 取决于最终决策：
  - 如果仍维持策略硬编码，则 UI 不应暴露无效选项
  - 如果允许管理员切换，则 Gateway policy 层必须读取对应配置

---

## 3. 原计划中的主要问题

### 3.1 V1 接口已被官方标注为“不推荐”

原计划第 2 节和 Task 1 均基于：

- `POST https://openspeech.bytedance.com/api/v1/tts`

但火山引擎文档当前已明确在“豆包语音 -> 语音合成大模型”下同时列出：

- `HTTP Chunked/SSE单向流式-V3`
- `WebSocket 单向流式-V3`
- `HTTP 一次性合成/非流式接口-V1（不推荐）`

这不代表 V1 不能用，但代表它更适合：

- 快速验证
- 低风险试接
- 临时兼容接入

而不适合直接作为长期正式方案的默认协议。

建议：

1. 在计划最前面增加一个协议决策说明
2. 明确本次是：
   - 方案 A：先接 V1 作为低成本验证
   - 方案 B：直接做 V3 正式接入
3. 如果走方案 A，文档里要清楚写明“这是过渡方案”

### 3.2 V1 短文本接口存在长度限制，计划未覆盖

原计划第 2.1 节已经写了：

- 短文本在线合成，单次 `<=1024` 字节

但 Task 1 和 Task 3 里没有任何以下内容：

- 文本长度校验
- 超长文本失败测试
- 按句拆分策略
- 超长段 fallback 处理

而当前 `src/services/tts/tts_generator.py` 的 dispatch 方式是直接取整段 `tts_text` 发送。

如果不加 deterministic guard，长段或中文多字节段会在运行时直接失败。

建议：

1. 在 provider 层补长度校验
2. 新增最少两个测试：
   - 超长文本触发明确错误
   - 边界值文本可以成功
3. 如果不做自动拆分，就要明确写成“V1 仅支持短段；超限即失败”

### 3.3 鉴权 header 写法在文档内自相矛盾

原计划中存在三种状态：

1. 第 2.2 节写的是：
   - `Authorization: Bearer {access_token}`

2. 第 6 节第 6 条又把它列为待确认：
   - `Bearer;{token}` vs `Bearer {token}`

3. Task 1 的示例代码实际写成：
   - `Authorization: f"Bearer;{access_token}"`

根据当前官方 V1 文档，短文本一次性 HTTP 接口写法是：

- `Authorization: "Bearer;${token}"`

并且文档特别说明：

- `Bearer` 和 `token` 使用分号 `;` 分隔

因此这项不应继续悬而未决，而应直接在原计划中改为：

- 对于 V1，一次性 HTTP 接口的 header 写法确认使用 `Bearer;{token}`

### 3.4 环境变量设计与现有项目配置体系脱节

原计划新增的是：

- `VOLCENGINE_TTS_APPID`
- `VOLCENGINE_TTS_ACCESS_TOKEN`
- `VOLCENGINE_TTS_CLUSTER`

这套变量本身没问题，但当前项目的真实 provider 体系主要围绕：

- `AUTODUB_TTS_*`
- `autodub.local.json`

如果继续新增一整套完全独立的 `VOLCENGINE_*` 变量，需要在文档里明确：

1. 这是仅供旧链路使用
2. 还是会通过 `AUTODUB_TTS_PROVIDER_NAME=volcengine` 等方式统一接入

否则后续会出现：

- 一个 provider 在旧链路用一套配置
- 在统一 provider 层又是另一套配置

这会提高运维和排障成本。

### 3.5 RPM/QPS 数值建议目前缺少直接文档依据

原计划在 `tts_strategy.py` 里建议：

- `volcengine: 100` RPM

我本次核查到的官方资料中，确认存在：

- `QPS/并发查询接口说明`
- 计费说明中提到“豆包语音合成模型2.0 正式版默认支持 10 并发，超出部分按需增购”

但原计划里的“100 RPM”并没有在本次审阅中被官方文档直接确认。

建议：

1. 把该值标记为“初始保守值/待压测修正”
2. 或者在计划里把它改成“先使用更保守的配置，后续根据控制台并发/QPS能力调整”

---

## 4. 第 6 节待确认事项核实结果

以下结论基于 2026-04-01 查阅火山引擎官方文档。

### 4.1 事项 1：火山引擎账号已注册并完成实名认证

结论：
- 仍然需要人工确认
- 但这项是明确的前置条件，不只是“建议”

官方“创建应用”文档里明确列出了：

1. 注册账号
2. 登录并完成实名认证
3. 获取访问密钥
4. 进入音频技术控制台
5. 创建应用并开通服务

建议改写为：

- [ ] 已完成火山引擎账号注册、实名认证，并可登录音频技术控制台

### 4.2 事项 2：已创建应用并获取 appid / access_token / cluster

结论：
- 对 V1 接口而言，这项仍需要人工确认
- 但技术口径基本可以认为已确认

官方 V1 文档与“创建应用”文档都支持以下事实：

- 需要 `AppID`
- 需要 `Token/AccessToken`
- 需要 `Cluster`

建议改写为：

- [ ] 已在控制台完成应用创建、服务开通与接入应用，并实际拿到 `appid / token / cluster`

同时补一句：

- 若后续改走 V3，应重新确认是否改用 API Key 体系，不应默认沿用 V1 的凭据模型

### 4.3 事项 3：确认 API endpoint 和认证格式

结论：
- 对 V1 已可确认
- 真正未决的是“是否继续选 V1”

已确认内容：

- Endpoint: `POST https://openspeech.bytedance.com/api/v1/tts`
- 认证：Bearer Token
- Header 写法：`Authorization: Bearer;{token}`

建议把原待确认项拆成两条：

1. [ ] 确认本次是否接受使用 V1（不推荐）作为过渡方案
2. [x] 若使用 V1，endpoint 与认证格式已核实

### 4.4 事项 4：确认免费试用额度是否足够初期测试

结论：
- 官方当前资料可确认“豆包语音合成模型2.0”的试用额度是 `20000字符 / 半年`
- 是否“足够”取决于测试范围

如果只是：

- smoke test
- 单元测试
- 少量手工验证

通常是足够的。

如果要做：

- 多轮真实样本回归
- 长视频验证
- 多音色 A/B 验证

则很快会不够。

建议改写为：

- [x] 官方试用额度已核实：`20000字符 / 半年`
- [ ] 需要项目侧确认本次测试范围是否会超出该额度

### 4.5 事项 5：确认音色列表 API 或文档

结论：
- 官方已存在音色列表相关文档与接口
- 这项不构成本次最小集成的阻塞项

本次已确认存在：

- `ListSpeakers - 大模型音色列表(新接口)`
- `ListBigModelTTSTimbres - 大模型音色列表`
- `豆包语音合成2.0能力介绍`

建议改写为：

- [x] 官方已存在音色列表/音色查询相关接口与文档
- [ ] 本次是否接 catalog 能力需单独决策；若不接，可先固定 1~2 个验证音色

### 4.6 事项 6：确认 Authorization header 格式

结论：
- 对 V1 已确认
- 不建议继续保留为待确认项

官方 V1 文档明确写的是：

- `Authorization: "Bearer;${token}"`

并明确强调：

- `Bearer` 与 `token` 使用分号 `;` 分隔

建议直接在原计划里改成：

- [x] V1 一次性 HTTP 接口鉴权 header 已核实，使用 `Bearer;{token}`

---

## 5. 建议对原计划做的修订

### 5.1 建议新增一个“协议决策”前置章节

建议在原计划第 2 节之前插入：

## 0. 协议决策

- 本次若以“快速接通、验证业务可行性”为目标，可先接 V1 一次性 HTTP 接口
- 本次若以“长期正式方案”为目标，应优先评估 V3
- 无论选择哪一种，都需要把该结论写死在计划里，不要边实施边临时切换

### 5.2 建议扩大文件清单

如果仍然要保留“管理员后台可切换使用”这个目标，建议把以下文件加入范围：

- `gateway/job_intercept.py`
- `tests/test_gateway_job_policy.py`
- 可能还包括与 admin settings 生效链路有关的测试

否则建议把目标收窄为：

- “在旧 TTS 链路中增加 volcengine provider，但不在本次实现后台策略切换”

### 5.3 建议新增一个长度限制任务

建议增加一个单独 Task，例如：

- Task X: 为 V1 provider 增加短文本长度 guard

最少包括：

1. 超长文本单测
2. provider 抛出明确错误
3. 文档写明此限制

### 5.4 建议明确本次是否只做旧链路

建议在原计划“Architecture”部分改写成二选一：

方案 A：
- 本次仅接入 `src/services/tts/tts_generator.py` 旧链路，供 web-console 兼容流程使用

方案 B：
- 本次同时补齐 `src/services/tts_provider.py` 统一真实 provider 边界

如果不做这一步，后续会出现“同一个 provider 在不同入口能力不一致”的问题。

---

## 6. 建议替换后的第 6 节草案

下面是一版可直接替换到原计划中的“待确认事项”草案。

```md
## 6. 待确认事项（修订建议）

在正式实施前需确认：

1. [ ] 已完成火山引擎账号注册、实名认证，并能进入音频技术控制台
2. [ ] 已完成服务开通、应用创建和接入应用，并实际拿到 `appid / token / cluster`
3. [ ] 确认本次采用 V1 作为临时验证方案，还是改为直接接 V3 正式协议
4. [x] 若采用 V1，一次性 HTTP 接口 endpoint 与鉴权格式已核实：
   - `POST https://openspeech.bytedance.com/api/v1/tts`
   - `Authorization: Bearer;{token}`
5. [x] 官方试用额度已核实：豆包语音合成模型2.0 为 `20000字符 / 半年`
6. [ ] 结合本次测试范围，确认试用额度是否足够
7. [x] 官方已存在音色列表/音色查询相关接口与文档
8. [ ] 确认本次是否实现音色 catalog；若不实现，则固定 1~2 个验证音色
9. [ ] 确认“管理员后台可切换”是否纳入本次范围；若纳入，需补 Gateway policy 生效链路
10. [ ] 若继续使用 V1，需明确本次如何处理单次短文本长度限制
```

---

## 7. 官方技术文档链接

以下链接均为本次审阅使用的官方文档或官方搜索结果落地页。

### 7.1 火山引擎 / 豆包语音官方文档

- V1 一次性 HTTP 接口（当前计划使用）
  - https://www.volcengine.com/docs/6561/79820?lang=zh

- 豆包语音合成 2.0 能力介绍
  - https://www.volcengine.com/docs/6561/1871062

- 大模型音色列表新接口
  - https://www.volcengine.com/docs/6561/2160690?lang=zh

- 计费概述
  - https://www.volcengine.com/docs/6561/1359369?lang=zh

- 计费说明
  - https://www.volcengine.com/docs/6561/1359370?lang=zh

### 7.2 火山引擎控制台 / 开通流程相关文档

- 创建应用
  - https://www.volcengine.com/docs/6489/75565?lang=zh

---

## 8. 关键核实点摘要

本次可以直接视为已核实的事实：

1. V1 endpoint 当前是 `https://openspeech.bytedance.com/api/v1/tts`
2. V1 header 写法当前是 `Authorization: Bearer;{token}`
3. V1 在官方文档中属于“不推荐”接口
4. 豆包语音合成模型 2.0 当前资源包价格可对应到 `10万字 / 28 元`
5. 豆包语音合成模型 2.0 当前试用额度为 `20000字符 / 半年`
6. 官方已存在音色列表文档/接口
7. 账号注册、实名认证、服务开通、应用创建这些前置动作仍然需要人工完成

本次仍然需要项目侧做决策的事项：

1. 是否接受 V1 作为临时实现
2. 是否把“管理员后台可切换”真正纳入本次范围
3. 是否只接旧链路，还是同时补统一 provider 边界
4. 如何处理 V1 的单次短文本长度限制

---

## 9. 最终建议

建议把原计划修订为“先做一次范围收敛，再进入编码”：

1. 先决定协议：V1 过渡 or V3 正式
2. 先决定范围：仅旧链路 or 同时补统一 provider
3. 先决定产品口径：后台可见选项是否必须真正生效
4. 只有以上三点确定后，再开始 Task 1~Task 6

否则很容易出现：

- 文档里说能切换，实际切不了
- 测试能过，但真实长段跑不通
- 旧链路能用，统一 provider 层却没有同步能力


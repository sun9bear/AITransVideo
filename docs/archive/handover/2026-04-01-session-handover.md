# 会话交接文档 — 2026-04-01

> 本文档记录本次长会话（从 CosyVoice 系统到 VolcEngine 豆包 TTS 集成）的全部工作成果、当前状态、已知问题和待执行计划，供新会话无缝衔接。

---

## 一、本次会话完成的任务总览

### 1. CosyVoice 系统收尾（会话早期，已提交）

以下工作在 commit `860406d` 中：

- **B1 Baseline Voice Matcher**: 81声音目录、71个可匹配、10个国际端点可用
- **B2 离线 Gemini Voice Profiling**: 59 voice 全量 profiling（gemini-3.1-pro-preview）
- **端点切换配置**: intl/mainland 双端点，Admin 面板集成
- **`_rerank_with_profiles()`**: 4维评分 rerank（pitch_level, texture_tags, maturity, childlike）
- **routing invariants 测试**: 全部通过，无 xfail

### 2. VolcEngine 豆包 TTS V3 集成（主要工作，未提交）

从零实现了 VolcEngine 作为第4个 TTS provider（之前有 minimax/cosyvoice/mimo）。

#### 2a. 实现过程（重要背景）

- **最初按 V1 API 设计**，经用户提供的 Codex 评审文档指出应该用 V3
- **完整重写为 V3 HTTP Chunked API**（`POST /api/v3/tts/unidirectional`）
- V3 认证用 `X-Api-App-Id` + `X-Api-Access-Key` + `X-Api-Resource-Id` 三个 header（不是 V1 的 `Authorization: Bearer;{token}`）
- V3 返回流式 JSON lines：`code==0` 是 PCM 音频块（base64），`code==20000000` 是结束，`code>0` 其他值是错误
- Provider 本地累积 PCM 块后用 Python `wave` 模块封装为 WAV（24000Hz, mono, 16-bit）

#### 2b. 改动的文件清单（全部未提交，在 working tree 中）

**新文件：**

| 文件 | 说明 |
|------|------|
| `src/services/tts/volcengine_tts_provider.py` | V3 provider，函数式 API（不是 class），核心函数 `synthesize()` |
| `tests/test_volcengine_tts_provider.py` | 10个测试：空文本、缺凭据、V3 header、PCM 累积、finish 事件、错误码、长文本、legacy env |
| `docs/specs/2026-04-01-volcengine-doubao-tts-integration-design.md` | 原始设计文档（V1→V3 多次修订） |
| `docs/specs/2026-04-01-volcengine-doubao-tts-integration-design-review.md` | Codex 评审意见 |

**修改的文件：**

| 文件 | 改动 |
|------|------|
| `src/services/tts/tts_strategy.py` | `VALID_PROVIDERS` 加 `"volcengine"`，RPM=60，fallback → cosyvoice |
| `src/services/tts/tts_generator.py` | 新增 `_generate_one_volcengine()` 方法 + dispatch 分支 |
| `gateway/job_intercept.py` | 新增 `_VALID_EXPRESS_PROVIDERS` / `_VALID_STUDIO_PROVIDERS` 含 volcengine，`compute_job_policy()` 读 admin settings |
| `frontend-next/src/app/admin/settings/page.tsx` | TTS_OPTIONS / EXPRESS / STUDIO 加 volcengine 选项 |
| `tests/test_tts_strategy.py` | +4 tests (valid provider, RPM, fallback) |
| `tests/test_tts_generator.py` | +3 tests (dispatch, default speaker, exception wrapping) |
| `tests/test_gateway_job_policy.py` | +4 tests (express/studio volcengine, invalid fallback) |
| `tests/test_gateway_create_job.py` | +2 tests (express/studio volcengine injected) |

**本地测试状态：74 passed, 0 failed**

### 3. 远程部署（已完成，US 主机可用）

#### US 主机 (5.78.122.220)

- 所有代码已通过 tar.gz 打包 + `Deploy-Via-154.cmd` 部署
- `.env` 已写入 VolcEngine 凭据：
  ```
  VOLCENGINE_TTS_APP_ID=2678837730
  VOLCENGINE_TTS_ACCESS_KEY=CdQIZvmJWfLKukzjKlf21h3pQsME-HQL
  VOLCENGINE_TTS_RESOURCE_ID=volc.service_type.10029
  VOLCENGINE_TTS_DEFAULT_SPEAKER=zh_female_shuangkuaisisi_moon_bigtts
  ```
- 容器已用 `docker compose up -d app` 重建，环境变量已加载确认
- **Smoke test 通过**：`synthesize()` 成功生成 245,328 bytes WAV，5.11秒时长

#### SG 主机 (5.223.84.82)

- 代码已部署，但 **gateway 持续重启**（疑似 DB migration 问题，类似 US 主机之前遇到的缺列问题）
- SSH 连接不稳定，未完成修复
- VolcEngine 凭据尚未写入

---

## 二、关键技术发现（Resource ID 与音色的对应关系）

这是本次会话最重要的发现，直接影响后续改造：

### Resource ID 决定模型版本

| Resource ID | 模型 | 音色后缀 | 音色数量 |
|---|---|---|---|
| `seed-tts-1.0` 或 `volc.service_type.10029` | 豆包1.0 | `_moon_bigtts` | 100+ |
| `seed-tts-2.0` | 豆包2.0 | `_uranus_bigtts` | 20+ |

### 验证结果（在 US 主机容器内实测）

| 组合 | 结果 |
|------|------|
| `seed-tts-1.0` + `_moon_bigtts` 音色 | ✅ 成功 |
| `seed-tts-2.0` + `_uranus_bigtts` 音色 | ✅ 成功 |
| `seed-tts-2.0` + `_moon_bigtts` 音色 | ❌ 55000000 mismatch |
| `seed-tts-1.0` + `_uranus_bigtts` 音色 | ❌ 未测（预期 mismatch） |

### 当前代码的问题

当前 `volcengine_tts_provider.py` 的默认值是：
- `DEFAULT_RESOURCE_ID = "volc.service_type.10029"`（即 1.0）
- `DEFAULT_SPEAKER = "zh_female_shuangkuaisisi_moon_bigtts"`（1.0 音色）

**这意味着当前部署实际用的是豆包1.0，不是2.0。** 用户以为开的是2.0服务，但 speaker 是1.0的。

### 已验证的2.0音色列表（部分，来自官方文档截图）

```
zh_female_vv_uranus_bigtts              — Vivi 2.0（多语种）
zh_female_xiaohe_uranus_bigtts          — 小何 2.0
zh_male_m191_uranus_bigtts              — 云舟 2.0
zh_male_taocheng_uranus_bigtts          — 小天 2.0
zh_male_liufei_uranus_bigtts            — 刘飞 2.0
zh_male_sophie_uranus_bigtts            — 魅力苏菲 2.0
zh_female_qingxinnvsheng_uranus_bigtts  — 清新女声 2.0
zh_female_cancan_uranus_bigtts          — 知性姗姗 2.0
zh_female_sajiaoxuemei_uranus_bigtts    — 撒娇学妹 2.0
zh_female_tianmeixiaoyuan_uranus_bigtts — 甜美小源 2.0
zh_female_tianmeitaozi_uranus_bigtts    — 甜美桃子 2.0
zh_female_shuangkuaisisi_uranus_bigtts  — 爽快思思 2.0
zh_female_peiqi_uranus_bigtts           — 佩奇猪 2.0
zh_female_linjianvhai_uranus_bigtts     — 邻家女孩 2.0
zh_male_shaonianzixin_uranus_bigtts     — 少年梓辛 2.0
zh_male_sunwukong_uranus_bigtts         — 猴哥 2.0
zh_female_yingyujiaoxue_uranus_bigtts   — Tina老师 2.0
zh_female_kefunvsheng_uranus_bigtts     — 暖阳女声 2.0
```

### 豆包2.0 特有能力

- **情绪指令**: 在文本中插入 `[#用吵架的语气说]` 等自然语言指令
- **引用上文**: 前置对话上下文，模型推断情绪延续
- **语音标签**: `[旁白，语调惊恐]`（早期访问）
- **自动情感推断**: 根据文本语义自动调整语气（无需参数）

### 豆包1.0 vs 2.0 定价

| 模型 | 按量价格 |
|------|---------|
| 1.0 | ¥5/万字符 |
| 2.0 | ¥3/万字符 |

---

## 三、待执行的改造计划

计划文件位于: `C:\Users\Administrator\.claude\plans\melodic-puzzling-fiddle.md`

### 目标

将单一 `volcengine` provider 改造为 1.0/2.0 双模式：
- **快捷版 express** → 豆包1.0（`seed-tts-1.0`，100+ `_moon_bigtts` 音色）
- **工作台版 studio** → 豆包2.0（`seed-tts-2.0`，20+ `_uranus_bigtts` 音色）

### 核心设计

保持 `volcengine` 单一 provider 名称。Gateway 根据 `service_mode` 自动设置 `tts_model`：

```
Admin 选 volcengine → Gateway compute_job_policy()
  express: tts_model="seed-tts-1.0"  /  studio: tts_model="seed-tts-2.0"
    → Job snapshot (PostgreSQL)
      → TTSGenerator._generate_one_volcengine()
        → 读 tts_model → 传 resource_id 给 synthesize()
          → X-Api-Resource-Id header
```

### 4 个阶段（TDD）

**阶段1: Provider 层** — `src/services/tts/volcengine_tts_provider.py`
- 新增常量: `RESOURCE_ID_1_0`, `RESOURCE_ID_2_0`, `DEFAULT_SPEAKER_1_0`, `DEFAULT_SPEAKER_2_0`
- 新增: `default_speaker_for_resource(resource_id) -> str`
- `synthesize()` 增加 `resource_id: str | None = None` 关键字参数
- 4个新测试

**阶段2: Gateway 层** — `gateway/job_intercept.py`
- `compute_job_policy()` studio volcengine → `tts_model="seed-tts-2.0"`, `voice_clone_enabled=False`
- express volcengine → `tts_model="seed-tts-1.0"`
- 3个新测试

**阶段3: Generator 层** — `src/services/tts/tts_generator.py`
- `_generate_one_volcengine()` 从 job_record 读 `tts_model` 作为 `resource_id`
- 用 `default_speaker_for_resource()` 替代硬编码默认音色
- 4个新测试

**阶段4: 前端标签** — `frontend-next/src/app/admin/settings/page.tsx`
- EXPRESS: "豆包1.0（seed-tts-1.0）"
- STUDIO: "豆包2.0（seed-tts-2.0）"

### 用户确认的决策

- 工作台版选豆包2.0时 `voice_clone_enabled=False`（豆包2.0不支持克隆）
- 后期可以用豆包语音复刻模型2.0（`seed-icl-2.0`）实现克隆，那是另外的计划

### 不做的事

- 不拆分为两个 provider（共用同一个 V3 端点和代码）
- 不做豆包音色自动匹配（后续计划）
- 不做豆包语音复刻（后续计划）
- 不改 RPM 限制（1.0/2.0 共享配额）
- 不改 fallback 链（volcengine → cosyvoice 保持不变）

---

## 四、CosyVoice 音色匹配系统完整说明

### 4.1 匹配算法概览

匹配入口: `select_voice_match()` in `cosyvoice_voice_selector.py`

采用 **4级回退层次**，从最精确到最粗糙：

| 级别 | 条件 | 分数 | 置信度 | 示例 |
|------|------|------|--------|------|
| Tier 1: Style Override | gender + age_bucket + persona_style 三者都匹配 | 0.85 | HIGH | 女+中年+professional → longyingjing_v3 |
| Tier 2: Base Map + Age | gender + age_bucket 匹配 | 0.60 | MEDIUM | 男+middle → longanzhi_v3 |
| Tier 3: Gender-Only | 仅 gender 匹配 | 0.40 | LOW | 男 → longanyang |
| Tier 4: Fallback | 无匹配 | 0.20 | LOW | → longanyang（兜底） |

### 4.2 数据流：Segment → Matcher → Voice

```
DubbingSegment 属性:
├── segment.gender              → 传给 matcher 的 gender 参数
├── segment.age_group           → 传给 matcher 的 age_group 参数
├── segment.persona_style       → 传给 matcher 的 persona_style 参数
├── segment.energy_level        → 传给 matcher 的 energy_level 参数
├── segment.voice_description   → 用于推断 persona_style（如果未直接提供）
└── segment.voice_id            → 如果是合法的 builtin voice，直接使用跳过匹配
```

调用链: `_generate_one_cosyvoice()` → `enhance_voice_selection()` → `select_voice_match()`

### 4.3 完整匹配流程

```
1. 检查 segment.voice_id 是否为合法 builtin voice
   ├─ 是: 直接使用，confidence="high"，跳过后续所有步骤
   └─ 否: 进入人口统计学匹配

2. 判断 is_childlike（关键词: 童声/儿童/小孩/boy/girl/child）
   ├─ 是: effective_gender 强制为 "child"
   └─ 否: 使用原始 gender

3. 年龄桶归一化
   "elderly"/"old"/"senior" → "elderly"
   "young"/"youth"          → "young"
   "middle"/"adult"/"mature" → "middle"
   其他/空                   → ""

4. Tier 1 尝试: _STYLE_OVERRIDES[(gender, age_bucket, persona_style)]
   ├─ 命中: score=0.85, confidence="high"
   └─ 未命中: → Tier 2

5. Tier 2 尝试: _BASE_MAP[f"{gender}_{age_bucket}"]
   ├─ 命中: score=0.60, confidence="medium"
   └─ 未命中: → Tier 3

6. Tier 3 尝试: _BASE_MAP[gender]
   ├─ 命中: score=0.40, confidence="low"
   └─ 未命中: → Tier 4 fallback ("longanyang", score=0.20)

7. 端点可用性检查 (_ensure_available)
   ├─ 音色在当前端点可用: 保持
   └─ 不可用: 从同性别端点安全池中选替代，score -0.15

8. 选取 backup_voices（同性别，最多2个，优先 known-good 集合）

9. B2 Profile Rerank（仅 confidence 为 low/medium 且有 backup 时）
   ├─ 对 primary + backups 打分（4维）:
   │   ├─ maturity 匹配 (0.3): speaker age 与 voice maturity 对齐
   │   ├─ childlike 匹配 (0.2): child speaker 需要 childlike voice
   │   ├─ pitch_level 匹配 (0.3): male→low/mid, female→mid/high, child→high
   │   └─ texture_tags 匹配 (0.2): persona→texture 对齐
   ├─ 按分数排序，最高分成为新 primary
   └─ 如果改变了: reason 追加 "+reranked", score +0.05
```

### 4.4 关键映射表

**Style Overrides (Tier 1):**

| Gender | Age | Persona | Voice | 名称 |
|--------|-----|---------|-------|------|
| female | middle | professional | longyingjing_v3 | 沉稳从容主持人 |
| female | middle | warm | longanwen_v3 | 优雅知性 |
| female | middle | energetic | longanhuan | 欢脱元气 |
| female | middle | serious | longxiaoxia_v3 | 沉稳权威 |
| male | middle | professional | longanzhi_v3 | 睿智轻熟 |
| male | middle | serious | longanzhi_v3 | 睿智沉稳 |
| male | middle | warm | longanyun_v3 | 居家暖男 |
| male | young | energetic | longanyang | 阳光大男孩 |
| male | young | serious | longcheng_v3 | 睿智少年 |

**Base Map (Tier 2 & 3):**

| Key | Voice | 名称 |
|-----|-------|------|
| male | longanyang | 阳光大男孩 |
| female | longanhuan | 欢脱元气 |
| child | longhuhu_v3 | 呼呼 |
| male_elderly | longlaobo_v3 | 老伯 |
| male_young | longanyang | 阳光大男孩 |
| male_middle | longanzhi_v3 | 睿智轻熟 |
| female_young | longanhuan | 欢脱元气 |
| female_middle | longyingjing_v3 | 沉稳从容 |
| female_elderly | longlaoyi_v3 | 老奶奶 |
| child_young | longhuhu_v3 | 呼呼 |
| child_middle | longjielidou_v3 | 杰力豆 |

### 4.5 端点可用性

| 端点 | 可用音色数 | 说明 |
|------|-----------|------|
| International | 10 | longanyang, longanhuan, longhuhu_v3, longanzhi_v3 等 |
| Mainland | 100+ | 所有 matchable 音色 |

当前生产默认用 international 端点，只有 10 个音色可用。

### 4.6 Voice Profile 数据结构

```
VoiceProfile:
├── primary: {pitch_level, warmth, authority, intimacy}
└── secondary: {energy_level, brightness, maturity, delivery_style, texture_tags, childlike}
```

59 个 voice 已通过 Gemini profiling（gemini-3.1-pro-preview）。
Profile JSON 位于: `/opt/aivideotrans/data/b2_voice_profiles_final.json`

Rerank 只用 4 个维度（pitch_level, texture_tags, maturity, childlike），其余维度权重为 0（A/B 验证趋同）。

### 4.7 用户反馈的 Bug

用户测试视频时发现：
1. **2个男性说话者被匹配到女声**
2. **2个男性说话者被匹配到同一个声音**

### 4.8 Bug 可能原因

1. **性别检测不准**: Pipeline S2/S3 阶段输出的 `speaker_gender` 可能不准。B1 matcher 完全依赖这个字段做首轮筛选，性别判断错 → 从错误性别池选择。

2. **International 端点可用声音池太小**: 10 个音色中可用男声只有约 3-4 个。不同男性说话者很容易撞到同一个音色。如果切换到 mainland 端点（27+音色），碰撞概率大幅降低。

3. **Rerank 未生效**: 如果匹配走了 high confidence 路径（Tier 1），rerank 不会执行。但 Tier 1 需要 persona_style + age_bucket 都有值才触发，如果 segment 缺这些字段，实际应该走 Tier 2/3。

4. **同一 speaker_id 的多个 segment 没有缓存声音选择**: 如果 A 说话者的第1段选了 voice X，但第2段的 demographics 略有不同，可能选了 voice Y。反之，如果两个不同说话者的 demographics 完全相同，会选到同一个 voice。

### 4.9 Bug 排查步骤

1. **查 job 日志**: 过滤 `[CosyVoice]` 行，看每个 segment 的 gender / age / persona / selected_voice / confidence
2. **检查 speaker_gender**: 是 "male" 还是误判为 "female"？
3. **检查端点模式**: `grep cosyvoice_runtime_endpoint_mode /opt/aivideotrans/config/admin_settings.json`
4. **尝试切 mainland 端点**: 通过 admin settings 切换，可用音色从 10 扩大到 100+
5. **检查 speaker_id 一致性**: 同一 speaker_id 的所有 segment 是否选了同一 voice

### 4.10 相关文件

| 文件 | 用途 |
|------|------|
| `src/services/tts/cosyvoice_voice_selector.py` | 核心匹配: `select_voice_match()`, `_rerank_with_profiles()` |
| `src/services/tts/cosyvoice_voice_catalog.py` | 81声音目录 + endpoint availability |
| `src/services/tts/cosyvoice_voice_profile_catalog.py` | B2 profile 数据加载 |
| `src/services/tts/cosyvoice_endpoint_config.py` | intl/mainland 端点切换 |
| `src/services/tts/cosyvoice_instruction_enhancer.py` | 包装层，当前 instruct 功能关闭 |
| `src/services/tts/tts_generator.py` | `_generate_one_cosyvoice()` 调用入口 |

---

## 五、远程部署注意事项

### 部署脚本

**只允许使用：**
- `D:\daili\scripts\Upload-Via-154.cmd <target> <local> <remote>` — 上传
- `D:\daili\scripts\Deploy-Via-154.cmd <target> <local> <remote> "<cmd>"` — 上传+执行（但本会话发现此脚本已改为 Deploy-US-Via-154.cmd 格式）
- `D:\daili\scripts\SSH-US-Via-154.cmd "<cmd>"` / `SSH-SG-Via-154.cmd "<cmd>"` — 执行远端命令

**实际可用的 PowerShell 调用方式（本会话验证通过）：**
```bash
# SSH 执行命令
powershell.exe -ExecutionPolicy Bypass -Command "& 'D:\daili\scripts\Invoke-SSH-Via-154-Test-Proxy.ps1' -TargetHost 5.78.122.220 -TargetPort 22 '<远端命令>'"

# SCP 上传文件
powershell.exe -ExecutionPolicy Bypass -Command "& 'D:\daili\scripts\Invoke-SCP-Via-154-Test-Proxy.ps1' -TargetHost 5.78.122.220 -TargetPort 22 -LocalPath '<本地路径>' -RemotePath '<远端路径>'"

# 上传+执行
powershell.exe -ExecutionPolicy Bypass -Command "& 'D:\daili\scripts\Invoke-Deploy-Via-154-Test-Proxy.ps1' -TargetHost 5.78.122.220 -TargetPort 22 -LocalPath '<本地路径>' -RemotePath '<远端路径>' -RemoteCommand '<命令>'"
```

### 关键踩坑

1. **MSYS 路径转换**: 远端路径 `/tmp/` 会被 Git Bash 转成 Windows 路径。用 `//tmp/` 双斜线。但 PowerShell 调用不需要
2. **SFTP 不创建目录**: 先 SSH `mkdir -p` 再上传
3. **`docker compose restart` 不重新读 .env**: 必须用 `docker compose up -d` 重建
4. **docker compose stderr**: PowerShell 会把 docker compose 的 stderr 输出（如 "Container Recreate"）当作错误，实际操作已成功
5. **嵌套引号**: PowerShell + SSH + 远端命令的多层引号容易出问题。复杂命令写成脚本文件上传后执行

### US 主机当前状态

```
aivideotrans-app       — Up (healthy)
aivideotrans-gateway   — Up (healthy)
aivideotrans-next      — Up
aivideotrans-postgres  — Up
```

VolcEngine 环境变量已加载到 app 容器。

### SG 主机待修复

- Gateway 重启循环（可能需要 DB migration，参考 US 修复方法）
- VolcEngine 凭据未写入
- SSH 不稳定

---

## 六、VolcEngine 凭据

```
APP_ID:      2678837730
ACCESS_KEY:  CdQIZvmJWfLKukzjKlf21h3pQsME-HQL
```

用户已开通 1.0 和 2.0 两个服务。

---

## 七、工作树未提交文件的完整 diff 说明

所有 VolcEngine 相关改动**都未提交**，在 `codex/review-guidelines` 分支上。

需要注意：`volcengine_tts_provider.py` 第28行 `DEFAULT_RESOURCE_ID` 当前值是 `"volc.service_type.10029"`，在双模式改造中应改为 `"seed-tts-1.0"`。

建议改造完成后一起提交，而不是先提交当前版本再改。

---

## 八、本会话未涉及但新会话可能需要知道的

1. **CosyVoice rerank 还有个旧计划未执行**: 之前的 plan 文件包含 "补全剩余17 voice 的 mainland profiling" + "接入 rerank 到生产" + "评估 mainland runtime"。这个计划被豆包集成打断了。CosyVoice 音色匹配问题可能和这个有关——rerank 可能还没真正接入生产路径。

2. **前端 RadioGroup name 冲突**: 之前修过一个 bug——admin settings 页面所有 RadioGroup 共享按钮名称导致互斥。已修复（每个 RadioGroup 加了 unique name prop），包含在未提交的改动中。

3. **US 主机 DB Migration**: 之前在 US 主机上手动补了3个缺失列（`estimated_duration_seconds`, `create_idempotency_key`, `quota_state`），通过 gateway 容器内 Python ALTER TABLE。SG 主机可能也需要同样的 migration。

4. **YouTube Cookies**: yt-dlp 下载失败时会覆盖 cookies 文件为无效内容。如果视频下载失败，检查 cookies 文件大小。

5. **`aivideotrans-app` 容器的代码部署**: 开发期用 bind mount（src/main.py/scripts），修改主机文件后 `docker restart` 即生效。但 gateway 和 next 是 Docker 镜像构建的，改代码后必须 `docker compose build gateway next && docker compose up -d gateway next`。

6. **用户偏好**: 用户是中文沟通，所有 UI 文本和交流用中文。用户倾向 TDD 开发方式。用户不喜欢不必要的确认，倾向让 AI 自主完成。

---

## 九、推荐的新会话第一步

1. 读取计划文件 `C:\Users\Administrator\.claude\plans\melodic-puzzling-fiddle.md`
2. 读取本交接文档
3. 确认 working tree 状态（`git status` + `git diff --stat`）
4. 按计划4阶段 TDD 执行豆包1.0/2.0双模式改造
5. 全量回归测试
6. 提交
7. 部署到 US 主机并验证
8. 修复 SG 主机

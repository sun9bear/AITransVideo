# CosyVoice 端点切换设置 — 上线与回滚说明

## 1. 当前结论

端点切换设置已完成开发与远程验证（审核节点 14 通过）。

推荐默认值：
- **runtime** = `international`（express/CosyVoice 生产调用）
- **offline** = `mainland`（B2 calibration / profiling / 离线建库）

## 2. 配置项说明

### 后台设置（admin_settings.json）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `cosyvoice_runtime_endpoint_mode` | `international` | 运行时端点，影响 express 作业的 CosyVoice TTS 调用 |
| `cosyvoice_offline_endpoint_mode` | `mainland` | 离线端点，影响 B2 calibration sample 生成和 profiling |

合法值：`international` / `mainland`

可通过以下方式修改：
1. Next 管理后台 → 系统设置 → CosyVoice 端点配置
2. `POST /api/admin/settings`
3. 直接编辑 `/opt/aivideotrans/config/admin_settings.json`

优先级：环境变量 > admin_settings.json > 硬编码默认值

### 环境变量（.env）

| 变量 | 用途 |
|------|------|
| `DASHSCOPE_INTERNATIONAL_API_KEY` | 国际端点专用 API key |
| `DASHSCOPE_MAINLAND_API_KEY` | 国内端点专用 API key |
| `DASHSCOPE_API_KEY` | 通用 fallback key（如果专用 key 不存在则使用） |

Key 选择规则：
- runtime=international → 优先 `DASHSCOPE_INTERNATIONAL_API_KEY` → fallback `DASHSCOPE_API_KEY`
- offline=mainland → 优先 `DASHSCOPE_MAINLAND_API_KEY` → fallback `DASHSCOPE_API_KEY`

### 端点可用音色差异

| 端点 | 可用音色数 | 说明 |
|------|-----------|------|
| international | 10/59 | 延迟低（1-2s），但音色覆盖有限 |
| mainland | 59/59 | 延迟较高（3-7s from SG），音色覆盖完整 |

runtime=international 时，B1 selector 自动启用 endpoint-safe voice pool，不会选出 intl 不可用的音色。

## 3. 上线步骤

### 3.1 文件部署

需要同步以下文件到远程主机 `/opt/aivideotrans/app/`：

**Python 后端**（bind mount，sync 后 restart app）：
- `src/services/tts/cosyvoice_endpoint_config.py`
- `src/services/tts/cosyvoice_provider.py`
- `src/services/tts/cosyvoice_voice_catalog.py`
- `src/services/tts/cosyvoice_voice_selector.py`
- `src/services/tts/cosyvoice_instruction_enhancer.py`
- `src/services/tts/cosyvoice_voice_profile_catalog.py`
- `src/services/tts/tts_generator.py`
- `src/services/gemini/translator.py`
- `src/pipeline/process.py`
- `scripts/cosyvoice_tts_helper.py`
- `scripts/b2_calibration_sample_builder.py`

**Gateway**（需要 docker compose build + recreate）：
- `gateway/admin_settings.py`

**Frontend**（需要 docker compose build + recreate）：
- `frontend-next/src/app/admin/settings/page.tsx`

### 3.2 环境变量

确认 `/opt/aivideotrans/config/.env` 包含：
```
DASHSCOPE_API_KEY=<existing intl key>
DASHSCOPE_INTERNATIONAL_API_KEY=<intl key>
DASHSCOPE_MAINLAND_API_KEY=<mainland key>
```

### 3.3 容器重建/重启

```bash
cd /opt/aivideotrans/app
source /opt/aivideotrans/config/.env
export PG_PASSWORD AIVIDEOTRANS_ROOT=/opt/aivideotrans

# App container (bind mount, restart only)
docker restart aivideotrans-app

# Gateway (rebuild image)
docker compose build gateway
docker stop aivideotrans-gateway && docker rm aivideotrans-gateway
docker compose up -d gateway

# Frontend (rebuild image)
docker compose build next
docker stop aivideotrans-next && docker rm aivideotrans-next
docker compose up -d next
```

### 3.4 默认值检查

```bash
cat /opt/aivideotrans/config/admin_settings.json | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("runtime:", d.get("cosyvoice_runtime_endpoint_mode", "MISSING"))
print("offline:", d.get("cosyvoice_offline_endpoint_mode", "MISSING"))
'
```

期望输出：
```
runtime: international
offline: mainland
```

## 4. 验证步骤

### 4.1 API 检查

```bash
# Login
COOKIE=$(curl -s -D /tmp/lh -X POST 'http://localhost:8880/auth/login' \
  -H 'Content-Type: application/json' \
  -d '{"email":"<admin_email>","password":"<admin_password>"}' \
  > /dev/null && grep -oP 'avt_session=[^;]+' /tmp/lh | head -1)

# GET settings
curl -s 'http://localhost:8880/api/admin/settings' -H "Cookie: $COOKIE" | python3 -m json.tool
```

确认返回 `settings.cosyvoice_runtime_endpoint_mode` = `international`。

### 4.2 App 侧读取检查

```bash
docker exec aivideotrans-app python3 -c '
from services.tts.cosyvoice_endpoint_config import get_runtime_endpoint_mode, get_offline_endpoint_mode
print("runtime:", get_runtime_endpoint_mode())
print("offline:", get_offline_endpoint_mode())
'
```

### 4.3 Runtime sanity check

提交一个最小 express job，确认 CosyVoice 日志显示 `source=enhancer(...)` 且 job 成功：

```bash
curl -s -X POST http://localhost:8877/jobs -H 'Content-Type: application/json' \
  -d '{"source":{"type":"youtube_url","value":"https://www.youtube.com/watch?v=jNQXAC9IVRw"},"speakers":"1","service_mode":"express","tts_provider":"cosyvoice","plan_code_snapshot":"free","role_snapshot":"admin","requires_review":false}'
```

### 4.4 Offline sanity check（可选）

```bash
docker exec aivideotrans-app python3 scripts/b2_calibration_sample_builder.py \
  --output-dir /tmp/offline_test --voices longanyang,longshuo_v3 --endpoint-mode mainland
```

`longshuo_v3` 在 intl 上 418，在 mainland 上成功 → 证明 offline mainland 生效。

## 5. 回滚说明

### 5.1 只回滚配置（不回滚代码）

如果端点设置导致异常，恢复默认值：

```bash
# 方式 A：通过 API
curl -s -X POST 'http://localhost:8880/api/admin/settings' \
  -H 'Content-Type: application/json' -H "Cookie: $COOKIE" \
  -d '{"tts_provider":"minimax","review_model":"gemini","translation_model":"deepseek","skip_translation_config_for_users":true,"skip_all_reviews_for_free_users":true,"free_user_max_duration_minutes":10.0,"enable_pre_tts_rewrite":true,"express_tts_provider":"cosyvoice","studio_tts_provider":"minimax","cosyvoice_runtime_endpoint_mode":"international","cosyvoice_offline_endpoint_mode":"mainland"}'

# 方式 B：直接编辑文件
python3 -c '
import json
path = "/opt/aivideotrans/config/admin_settings.json"
d = json.load(open(path))
d["cosyvoice_runtime_endpoint_mode"] = "international"
d["cosyvoice_offline_endpoint_mode"] = "mainland"
open(path, "w").write(json.dumps(d, indent=2, ensure_ascii=False))
print("restored")
'
```

不需要重启容器 — app 每次请求都重新读取配置文件。

### 5.2 回滚到完全无端点切换能力

如果需要彻底移除端点切换功能：

1. 从 `admin_settings.json` 删除 `cosyvoice_runtime_endpoint_mode` 和 `cosyvoice_offline_endpoint_mode` 字段
2. `cosyvoice_endpoint_config.py` 的 `get_runtime_endpoint_mode()` 会 fallback 到默认值 `international`
3. Helper 的 `endpoint_mode` 字段为空时回退到 env `DASHSCOPE_DEPLOYMENT_MODE` → 默认行为
4. 不需要回滚 Python 代码 — 所有新逻辑都有安全 fallback

### 5.3 代码级最小回滚范围

如果确需代码回滚（极端情况）：

| 文件 | 回滚影响 |
|------|---------|
| `cosyvoice_endpoint_config.py` | 删除整个文件，provider 中的 `_resolve_*` 需要同步删除 |
| `cosyvoice_provider.py` | 删除 `_resolve_deployment_mode()` / `_resolve_ws_url()`，request.json 删除 `endpoint_mode` 字段 |
| `cosyvoice_tts_helper.py` | 恢复单 key + 单端点逻辑 |
| `gateway/admin_settings.py` | 从 AdminSettings 删除两个字段 |
| `frontend-next/settings/page.tsx` | 删除 CosyVoice 端点配置区块 |

## 6. 已知限制

1. **intl 音色覆盖有限**：国际端点只支持 10/59 个 matchable 音色。Runtime=international 时 selector 自动降级到可用 voice，但匹配精度受限。
2. **instruction 保持 gated off**：`INSTRUCT_ENABLED=False`，CosyVoice v3-flash 引擎不支持 instruction 参数（B0 spike 已验证）。
3. **B2 rerank 未接生产**：离线 voice profile 数据已生成（10 voices），但尚未接入生产 selector 的 rerank 逻辑。
4. **SG→mainland 延迟**：如果将 runtime 切到 mainland，TTS 延迟会从 1-2s 增至 3-7s。
5. **INTL_AVAILABLE_VOICES 是静态数据**：基于 2026-03-30 审计结果。DashScope 后续如果在 intl 上线更多 voice，需要手动更新 `cosyvoice_endpoint_config.py` 中的 `INTL_AVAILABLE_VOICES` 集合。

# TU-05 · 统一 admin 鉴权依赖

- **目标 / 价值**：将分散在 gateway 中 13 个文件里的独立 `_require_admin` / `_is_admin` 副本收归为单一 `gateway/admin_auth.py`，提供一个权威实现；所有 router 改用它。消除已发现的行为分叉（返回类型 `None` vs `User`；`role` 判断 `!= "admin"` vs `(... or "user") != "admin"` 两种写法混用），杜绝未来 admin 鉴权静默绕过的安全维护风险。
- **关联发现**：DRY-01
- **前置依赖**：无（可与其他 quality/Wave B 单元并行）
- **建议分支**：`quality/admin-auth-dep`
- **预估工时**：M（约半天；Step 1 新文件 + Step 2 逐文件替换 + Step 3 更新 gate-coverage 测试）

> **命令环境**：默认 Git Bash / CI Linux（仓库已配 Bash 工具）；PowerShell 执行者改用等价命令（`grep`→`Select-String`、`test -f`→`Test-Path`、避免 `<(...)` 进程替换）。

---

## 决策记录（CodeX 审核 2026-06-25，已采纳）

- **第一 PR 范围**：仅收口 `gateway/` 顶层 13 个重复 `_require_admin` / `_is_admin` 副本；`gateway/pan/` 子包**不在本单元 PR 范围内**。
- **pan/ 保留为显式例外**：`pan/auth.py` 与 `pan/admin_api.py` 的本地副本继续保留，原因是 pan 子包有独立的认证上下文，语义等价性尚未完整确认；须在 `admin_auth.py` 文件头注释和 pan/auth.py 内注明此例外，说明其认证上下文独立。
- **pan/ 后续迁移**：待项目主另行确认 pan/ 与顶层 `admin_auth` 语义完全一致后，由单独 PR 迁移；不作为本单元 DoD 必须项。
- **admin gate coverage 测试必跑**：`test_admin_gate_coverage.py` 每次 commit 后必须通过（2 passed）；每个 admin 路由在迁移前后均须 403 拒绝非 admin 用户，安全不变量不放松。
- **Step 2-M 跳过执行**：本 PR 不对 `gateway/pan/` 做任何代码改动，Step 2-M 作为"已决策跳过（pan/ 例外）"标记，执行者不需等待额外确认即可跳过。
- **Step 5 收尾指标调整**：`gateway/pan/*.py` 中剩余 2 行本地副本是预期结果（pan/ 显式例外），不是遗漏；DoD 相应调整为 `gateway/*.py`（顶层）0 行、`pan/` 2 行并有注释说明。
- **DoD 去除 pan/ 迁移勾选项**：原 DoD 最后一项"pan/ 迁移决定已获项目主确认"调整为仅要求 pan/ 已有注释说明，迁移本身不计入本单元 DoD。

---

## 不在本单元范围（out-of-scope）

- `gateway/job_intercept.py:4834` 的 `_is_admin_user`（语义不同——只用于 "是否在响应里展示 infra 细节"，不抛 HTTP 异常，不是路由 gate）；本单元不触碰。
- `gateway/pan/auth.py` 里的 `_require_admin`：pan 子包有独立的认证上下文，其 `_require_admin` 供 `pan/admin_api.py` 内部使用。✅ 已决策（CodeX 2026-06-25）：pan/ 保留为显式例外，不在本单元 PR 迁移；待后续单独 PR 在确认语义等价后迁移。`pan/auth.py` 内应加注释说明此例外理由。
- 对任何路由功能逻辑的修改——本单元只替换 `_require_admin(user)` 调用源，**不改任何路由的业务代码**。
- FastAPI `Depends(require_admin)` 风格（路由签名注入）的迁移——当前代码全部是函数体内首行调用模式；本单元维持该调用模式，仅统一实现位置；依赖注入风格重构留作后续可选任务。

---

## 必守不变量

- **安全**：任何 admin 路由在迁移前后必须都 **403 拒绝非 admin 用户**；401 拒绝未登录用户。不允许出现迁移期间 gate 缺失的窗口。
- **付费 API 红线**：admin 鉴权本身不涉及任何付费外部 API，但 admin 路由下有触发 TTS/LLM 批量任务的端点——本单元只动 `_require_admin` 调用点，不触碰路由业务逻辑，红线天然隔离。
- **`test_admin_gate_coverage.py` 必须始终通过**：`_ADMIN_FILES` 列表和 `_GATE_MARKERS` 元组必须随本单元同步更新，确保 AST 扫描器识别新的 import 路径；每步 commit 后立跑该测试。
- **不破坏已有 monkeypatch 模式**：测试文件里有 `monkeypatch.setattr("admin_settings._require_admin", ...)` 形式的 mock；迁移后这些 mock 路径需同步更新到 `admin_auth._require_admin`，否则 mock 失效、测试静默通过但实际未打桩。
- **Gateway 不 import `services.jobs`**：`admin_auth.py` 只能 import `fastapi`、`models`，绝对不能间接拉入 pydub 或 `services.jobs`（见 CLAUDE.md 部署约束）。

---

## Step 0 · 确认现状

```bash
git switch -c quality/admin-auth-dep
```

运行 gate coverage 基线确认当前干净：

```bash
python -m pytest tests/test_admin_gate_coverage.py -q
# 期望：2 passed
```

用 grep 核对下列 `file:line`（多 agent 仓库行号可能漂移，以实际输出为准）：

```bash
grep -n "def _require_admin\|def _is_admin\|def _is_admin_user" gateway/*.py gateway/pan/*.py
```

实际核查结果（2026-06-25 核实，行号供参考，执行者应以 grep 输出为准）：

| 文件 | 符号 | 行（参考） | 返回类型 | 行为差异备注 |
|---|---|---|---|---|
| `gateway/admin_disk_api.py` | `_is_admin` + `_require_admin` | 89 / 93 | `User` | 标准 `(... or "user") == "admin"` |
| `gateway/admin_settings.py` | `_is_admin` + `_require_admin` | 818 / 827 | `User` | 含 docstring，是当前「事实最权威」版本 |
| `gateway/admin_job_monitor_api.py` | `_require_admin` | 37 | **`None`** | ⚠️ 行为分叉：`!= "admin"` 不含 `or "user"` 兜底；返回 None（调用方丢弃返回值） |
| `gateway/admin_smart_analytics_api.py` | `_require_admin` | 75 | `User` | `(... or "user") != "admin"` |
| `gateway/admin_support_api.py` | `_is_admin` + `_require_admin` | 109 / 113 | `User` | 标准版 |
| `gateway/cost_management.py` | `_require_admin` | 274 | `User` | `(... or "user") != "admin"` |
| `gateway/credits_observability.py` | `_require_admin` | 255 | `User` | `(... or "user") != "admin"` |
| `gateway/pricing_admin.py` | `_is_admin` + `_require_admin` | 33 / 37 | `User` | 标准版 |
| `gateway/s2_monitor_api.py` | `_require_admin` | 60 | **`None`** | ⚠️ 行为分叉：`!= "admin"` 不含 `or "user"` 兜底；返回 None |
| `gateway/traffic_analytics.py` | `_is_admin` + `_require_admin` | 155 / 159 | `User` | `bool(user and ...)` 写法（`_is_admin` 接受 `None`） |
| `gateway/voice_catalog_api.py` | `_require_admin` | 54 | `User` | `(... or "user") != "admin"` |
| `gateway/pan/admin_api.py` | `_require_admin` | 73 | `User` | 标准版（pan 子包） |
| `gateway/pan/auth.py` | `_is_admin` + `_require_admin` | 85 / 89 | `User` | 标准版（pan 子包） |
| `gateway/job_intercept.py` | `_is_admin_user` | 4834 | `bool` | **本单元不触碰**（语义不同，非路由 gate） |

已有 import 关系（4 个文件已提前从 `admin_settings` 导入 `_require_admin`）：

```bash
grep -rn "from admin_settings import.*_require_admin" gateway/
# 期望 4 行：admin_billing_api.py、admin_cost_api.py、admin_cosyvoice_control_api.py、mainland_voice_worker.py
```

确认 `gateway/admin_auth.py` 尚不存在：

```bash
test -f gateway/admin_auth.py && echo "EXISTS - STOP" || echo "OK - not exists"
```

检查测试文件中已有的 monkeypatch 路径（迁移后需同步）：

```bash
grep -rn "monkeypatch.*_require_admin\|monkeypatch.*_is_admin\|patch.*_require_admin\|patch.*_is_admin" tests/
```

---

## Step 1 · 创建 `gateway/admin_auth.py`（权威实现）

**动作**：新建 `gateway/admin_auth.py`，内容如下。

**改法**：

```python
# gateway/admin_auth.py
"""Shared admin authentication helpers for all gateway routers.

BACKGROUND
----------
Before this module, every admin router file contained its own copy of
``_require_admin`` / ``_is_admin``.  The copies had drifted in subtle ways:
- Return type: some returned ``User``, two returned ``None`` (monitor APIs)
- Role check: some used ``getattr(user, "role", None) != "admin"`` (no
  sentinel for missing-field), others used ``(getattr(...) or "user") != "admin"``

This single source of truth normalises behaviour: missing or falsy role is
treated as ``"user"`` (the `or "user"` sentinel), and the function always
returns the authenticated ``User`` object so callers that need it can use
the return value.

USAGE
-----
In any gateway router that needs an admin gate::

    from admin_auth import require_admin

    @router.get("/api/admin/something")
    async def my_handler(
        user: User | None = Depends(get_current_user),
    ) -> ...:
        require_admin(user)  # raises 401 / 403 for non-admin
        ...

The function is intentionally a **plain call** (not ``Depends``) so that
the existing ``test_admin_gate_coverage.py`` AST scan continues to work
unchanged (it keys on the string ``"_require_admin("`` in the route body).
A future step may migrate to ``Depends`` style; this module makes that
trivial since there is now only one place to change.

WHAT IS CHECKED
---------------
``user.role == "admin"`` via the ``role`` column added in Alembic migration
002.  To bootstrap the first admin:
    UPDATE users SET role='admin' WHERE email='your-admin@example.com';
"""
from __future__ import annotations

from fastapi import HTTPException
from models import User


def is_admin(user: User) -> bool:
    """Return True iff the user has the admin role.

    Uses ``(getattr(user, "role", None) or "user")`` so that a missing or
    falsy role field is treated as the default ``"user"`` role — consistent
    with Alembic 002 which gives all pre-existing users ``role='user'``.
    """
    return (getattr(user, "role", None) or "user") == "admin"


def require_admin(user: User | None) -> User:
    """Assert that *user* is an authenticated admin.

    Raises:
        HTTPException 401: if ``user`` is None (not logged in).
        HTTPException 403: if ``user`` is logged in but not admin.

    Returns:
        The same ``user`` object (so callers that need it can chain:
        ``admin = require_admin(user)``).
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# Backward-compat aliases used by the gate-coverage AST scanner
# (tests/test_admin_gate_coverage.py keys on "_require_admin(")
# ---------------------------------------------------------------------------
_is_admin = is_admin
_require_admin = require_admin
```

> **设计说明**
> - 暴露 `require_admin` / `is_admin`（公开命名）作为规范 API。
> - 同时提供 `_require_admin = require_admin` 别名，确保 `test_admin_gate_coverage.py` 的 AST 扫描器继续识别 `"_require_admin("` 标记——无需修改该测试。
> - 不 import 任何 `services.*` 模块，符合 CLAUDE.md gateway 容器约束。

**该步验收**：

```bash
# 文件存在
test -f gateway/admin_auth.py && echo "OK" || echo "FAIL: file missing"

# 只 import fastapi 和 models（不拉入 services.jobs）
python -c "
import sys, ast, pathlib
src = pathlib.Path('gateway/admin_auth.py').read_text()
tree = ast.parse(src)
imports = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
bad = [i for i in imports if 'services' in i or 'pydub' in i]
assert not bad, f'Forbidden imports: {bad}'
print('OK: no forbidden imports')
"

# 模块可 import（需 gateway/ 在 sys.path）
cd gateway && python -c "from admin_auth import require_admin, is_admin, _require_admin, _is_admin; print('OK')" && cd ..
```

**commit**（本步骤独立 commit）：

```bash
git add gateway/admin_auth.py
git commit -- gateway/admin_auth.py -m "feat: add gateway/admin_auth.py — unified admin gate helper"
```

---

## Step 2 · 逐文件替换：删除本地副本，改用 `admin_auth`

⚠️ **安全敏感改动**——每个文件改完后立即用 pytest 验证该文件对应的测试（见"测试计划"），不要一次改完再跑。

每个文件的改法模式相同：
1. 删除 `_is_admin` 和 `_require_admin` 本地定义。
2. 在文件顶部 import 行加 `from admin_auth import _require_admin` (若有 `_is_admin` 用法则同时加 `_is_admin`)。
3. 若该文件原来有 `from admin_settings import _require_admin`，删除该 import 并改为 `from admin_auth import _require_admin`。

> **注意**：`admin_job_monitor_api.py` 和 `s2_monitor_api.py` 原版返回 `None`，调用方丢弃返回值（`_require_admin(user)` 无赋值）。迁移后改为从 `admin_auth` import 的 `_require_admin`（返回 `User`），**调用方代码无需修改**——Python 允许函数返回值被丢弃，行为语义不变，只是修复了 role 判断的 `or "user"` 兜底缺失。

### 2-A · `gateway/admin_settings.py`（818-832 行，定义 `_is_admin` + `_require_admin`）

```diff
-def _is_admin(user: User) -> bool:
-    """Check admin via role field only.
-    ...
-    """
-    return (getattr(user, "role", None) or "user") == "admin"
-
-
-def _require_admin(user: User | None) -> User:
-    if user is None:
-        raise HTTPException(status_code=401, detail="未登录")
-    if not _is_admin(user):
-        raise HTTPException(status_code=403, detail="需要管理员权限")
-    return user
```

在文件顶部 imports 区加（与其他 from-imports 排在一起）：

```python
from admin_auth import _is_admin, _require_admin
```

**该步验收**：

```bash
# 本地定义已消失
grep -n "^def _require_admin\|^def _is_admin" gateway/admin_settings.py
# 期望：0 行

# import 存在
grep -n "from admin_auth import" gateway/admin_settings.py
# 期望：1 行

python -m pytest tests/test_admin_settings.py -q 2>&1 | tail -3
```

### 2-B · `gateway/admin_disk_api.py`（89-98 行）

删除本地 `_is_admin` / `_require_admin`，在顶部加：

```python
from admin_auth import _is_admin, _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin\|^def _is_admin" gateway/admin_disk_api.py
# 期望：0 行
python -m pytest tests/test_admin_disk_api.py -q 2>&1 | tail -3
```

### 2-C · `gateway/admin_job_monitor_api.py`（37-41 行）

删除本地 `_require_admin`（注意：原版返回 `None`，新版返回 `User`，调用方不使用返回值，无影响），在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/admin_job_monitor_api.py
# 期望：0 行
python -m pytest tests/ -k "admin_job_monitor or monitor_api" -q 2>&1 | tail -3
```

### 2-D · `gateway/admin_smart_analytics_api.py`（75-80 行）

删除本地 `_require_admin`，在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/admin_smart_analytics_api.py
# 期望：0 行
python -m pytest tests/test_admin_smart_analytics.py tests/test_admin_smart_analytics_voice_reuse.py -q 2>&1 | tail -3
```

### 2-E · `gateway/admin_support_api.py`（109-118 行）

删除本地 `_is_admin` + `_require_admin`，在顶部加：

```python
from admin_auth import _is_admin, _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin\|^def _is_admin" gateway/admin_support_api.py
# 期望：0 行
python -m pytest tests/test_support_admin_save_roundtrip.py -q 2>&1 | tail -3
```

### 2-F · `gateway/cost_management.py`（274-279 行）

删除本地 `_require_admin`，在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/cost_management.py
# 期望：0 行
python -m pytest tests/test_smart_admin_cost_endpoint.py -q 2>&1 | tail -3
```

### 2-G · `gateway/credits_observability.py`（255-260 行）

删除本地 `_require_admin`，在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/credits_observability.py
# 期望：0 行
python -m pytest tests/test_credits_observability.py -q 2>&1 | tail -3
```

### 2-H · `gateway/pricing_admin.py`（33-42 行）

删除本地 `_is_admin` + `_require_admin`，在顶部加：

```python
from admin_auth import _is_admin, _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin\|^def _is_admin" gateway/pricing_admin.py
# 期望：0 行
python -m pytest tests/test_pricing_admin.py -q 2>&1 | tail -3
```

### 2-I · `gateway/s2_monitor_api.py`（60-64 行）

删除本地 `_require_admin`（原版返回 `None`，新版返回 `User`，调用方丢弃返回值，无影响），在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/s2_monitor_api.py
# 期望：0 行
python -m pytest tests/ -k "s2_monitor" -q 2>&1 | tail -3
```

### 2-J · `gateway/traffic_analytics.py`（155-164 行）

删除本地 `_is_admin` + `_require_admin`，在顶部加：

```python
from admin_auth import _is_admin, _require_admin
```

> 注意：`traffic_analytics._is_admin` 原版接受 `User | None`（`bool(user and ...)`），而 `admin_auth.is_admin` 签名是 `user: User`。迁移后所有调用点均已通过 `_require_admin` 确认 `user is not None`，`_is_admin` 不会以 `None` 直接调用，签名变化无运行时影响。

**该步验收**：

```bash
grep -n "^def _require_admin\|^def _is_admin" gateway/traffic_analytics.py
# 期望：0 行
python -m pytest tests/test_traffic_analytics.py -q 2>&1 | tail -3
```

### 2-K · `gateway/voice_catalog_api.py`（54-59 行）

删除本地 `_require_admin`，在顶部加：

```python
from admin_auth import _require_admin
```

**该步验收**：

```bash
grep -n "^def _require_admin" gateway/voice_catalog_api.py
# 期望：0 行
python -m pytest tests/test_voice_catalog_api.py -q 2>&1 | tail -3
```

### 2-L · 4 个已从 `admin_settings` 导入的文件（admin_billing_api.py、admin_cost_api.py、admin_cosyvoice_control_api.py、mainland_voice_worker.py）

这 4 个文件自身没有本地定义，只有 `from admin_settings import _require_admin`；改为 `from admin_auth import _require_admin`。

```bash
# 查找并确认 4 个文件
grep -rn "from admin_settings import.*_require_admin" gateway/
```

对每个文件，将：

```python
from admin_settings import _require_admin
```

替换为：

```python
from admin_auth import _require_admin
```

（`admin_cosyvoice_control_api.py` 同时 import `AdminSettings, load_settings, save_settings`，保留那些 import，只将 `_require_admin` 来源改为 `admin_auth`）

**该步验收**：

```bash
# admin_settings 中不再有任何文件导入 _require_admin
grep -rn "from admin_settings import.*_require_admin" gateway/
# 期望：0 行

python -m pytest tests/test_admin_cosyvoice_control.py tests/test_mainland_voice_worker_gateway.py -q 2>&1 | tail -3
```

### 2-M · `gateway/pan/` 子包（✅ 已决策（CodeX 2026-06-25）：本单元跳过，pan/ 保留为显式例外）

`gateway/pan/auth.py:89` 和 `gateway/pan/admin_api.py:73` 各有一份 `_require_admin`。pan 是独立子包，有自己的认证上下文；其语义等价性尚未完整确认。

✅ 已决策（CodeX 2026-06-25）：**本 PR 不迁移 pan/ 子包**。pan/ 的本地副本作为显式例外保留。执行者**无需等待任何额外确认，直接跳过本子步骤**。

需要在代码中体现此例外（执行时前置动作——已定方向）：
1. 在 `gateway/admin_auth.py` 文件头 `BACKGROUND` 注释里加一段说明 pan/ 例外：
   ```python
   # NOTE: gateway/pan/auth.py has its own _require_admin / _is_admin because
   # pan/ has an independent authentication context.  Its semantic equivalence
   # with this module has not been fully verified.  Migrate pan/ in a separate
   # PR once equivalence is confirmed.
   ```
2. 在 `gateway/pan/auth.py` 对应函数上方加注释：
   ```python
   # Explicit exception: kept local to pan/ (independent auth context).
   # See gateway/admin_auth.py BACKGROUND note for migration plan.
   ```
3. **不对 `pan/admin_api.py` 或 `pan/auth.py` 做任何代码逻辑改动**。

后续迁移：由项目主在确认 pan/ 与顶层 `admin_auth` 语义完全一致后，开单独 PR 完成。

---

## Step 3 · 更新 `test_admin_gate_coverage.py`

**文件**：`tests/test_admin_gate_coverage.py`

**动作**：

1. 将 `admin_support_api.py` 补入 `_ADMIN_FILES` 元组（当前该文件有注释说 "WIP，未纳入"，但本次代码已稳定落地应纳入）。
2. 在 `_GATE_MARKERS` 里确认 `"_require_admin("` 仍在（无需改动，`admin_auth.py` 的别名 `_require_admin = require_admin` 保证 AST 扫描器仍能找到 `_require_admin(user)` 调用）。
3. 将 `test_admin_route_count_baseline` 的 `baseline` 数值更新为实际扫描结果（加入 `admin_support_api.py` 后路由数会增加）。

**具体改法**：

```diff
 _ADMIN_FILES = (
     "gateway/admin_settings.py",
     "gateway/admin_disk_api.py",
     "gateway/admin_cosyvoice_control_api.py",
     "gateway/admin_job_monitor_api.py",
     "gateway/admin_smart_analytics_api.py",
     "gateway/cost_management.py",
     "gateway/credits_observability.py",
     "gateway/pricing_admin.py",
     "gateway/s2_monitor_api.py",
     "gateway/traffic_analytics.py",
     "gateway/voice_catalog_api.py",
+    "gateway/admin_support_api.py",  # WIP 已稳定，TU-05 纳入
 )
```

更新 baseline 数值：先跑一次扫描取实际值，再写入。

```bash
python -c "
import ast
from pathlib import Path

ADMIN_FILES = [
    'gateway/admin_settings.py',
    'gateway/admin_disk_api.py',
    'gateway/admin_cosyvoice_control_api.py',
    'gateway/admin_job_monitor_api.py',
    'gateway/admin_smart_analytics_api.py',
    'gateway/cost_management.py',
    'gateway/credits_observability.py',
    'gateway/pricing_admin.py',
    'gateway/s2_monitor_api.py',
    'gateway/traffic_analytics.py',
    'gateway/voice_catalog_api.py',
    'gateway/admin_support_api.py',
]

def count_routes(src):
    tree = ast.parse(src)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if isinstance(dec.func.value, ast.Name) and dec.func.value.id in ('router', 'internal_router'):
                    count += 1
                    break
    return count

total = sum(count_routes(Path(f).read_text(encoding='utf-8')) for f in ADMIN_FILES)
print('Total admin routes:', total)
"
```

将输出的数字填入 `baseline = <N>`。

**该步验收**：

```bash
python -m pytest tests/test_admin_gate_coverage.py -v 2>&1 | tail -10
# 期望：2 passed（test_every_admin_route_has_gate_call + test_admin_route_count_baseline）
```

---

## Step 4 · 更新 monkeypatch 路径（测试一致性）

**动作**：搜索所有测试文件中对 `admin_settings._require_admin`、`admin_settings._is_admin` 的 monkeypatch，将模块路径改为 `admin_auth`。

```bash
grep -rn "monkeypatch.*admin_settings.*_require_admin\|monkeypatch.*admin_settings.*_is_admin\|patch.*admin_settings.*_require_admin" tests/
```

对每个命中行，将 `"admin_settings._require_admin"` → `"admin_auth._require_admin"`，`"admin_settings._is_admin"` → `"admin_auth._is_admin"`。

> 注意：各 router 文件内部调用 `_require_admin(user)` 是直接调用本地已 import 的名称，而非通过 `admin_settings._require_admin` 引用；因此 monkeypatch 的 target 要跟 **import 时绑定的模块**一致——迁移后是 `admin_auth`。

**该步验收**：

```bash
# 不再有 admin_settings._require_admin 的 patch 路径
grep -rn "admin_settings.*_require_admin\|admin_settings.*_is_admin" tests/
# 期望：0 行（或仅剩功能性 import，无 monkeypatch）

python -m pytest tests/ -q --tb=no 2>&1 | tail -5
# 期望：无新增 FAILED
```

---

## Step 5 · 全量回归 + 定量收尾指标

```bash
# 确认顶层副本已消失，pan/ 保留 2 行（显式例外，已决策）
grep -rn "^def _require_admin\|^def _is_admin" gateway/*.py
# 期望：0 行（顶层 gateway/*.py 全部迁走）

grep -rn "^def _require_admin\|^def _is_admin" gateway/pan/*.py
# 期望：2 行（pan/ 显式例外，已决策保留，并有注释说明）

# 全量副本计数（顶层）
COPIES=$(grep -rn "^def _require_admin\|^def _is_admin" gateway/*.py | grep -v ".codex_worktrees" | wc -l)
echo "Remaining local copies in gateway/*.py: $COPIES"
# 迁移完成目标：$COPIES == 0（pan/ 的 2 行不计入，属于显式例外）

# gate coverage 测试
python -m pytest tests/test_admin_gate_coverage.py -v

# admin 相关测试套件
python -m pytest tests/test_admin_settings.py tests/test_admin_disk_api.py tests/test_admin_smart_analytics.py tests/test_admin_smart_analytics_voice_reuse.py tests/test_pricing_admin.py tests/test_voice_catalog_api.py tests/test_credits_observability.py tests/test_traffic_analytics.py tests/test_admin_cosyvoice_control.py tests/test_mainland_voice_worker_gateway.py tests/test_support_admin_save_roundtrip.py tests/test_smart_admin_cost_endpoint.py tests/test_pan_admin_api.py -q 2>&1 | tail -5
```

**定量收尾指标**（可量化、机器可验证）：

| 指标 | 目标 |
|---|---|
| `gateway/*.py`（顶层）中 `def _require_admin` 行数 | 0（全部删除，逻辑移至 `admin_auth.py`） |
| `gateway/*.py`（顶层）中 `def _is_admin` 行数 | 0（同上） |
| `gateway/pan/*.py` 中本地副本行数 | 2（显式例外，已决策保留，有注释说明） |
| `test_admin_gate_coverage.py` | 2 passed |
| `gateway/admin_auth.py` 存在 | `test -f gateway/admin_auth.py` → 0 exit code |
| `from admin_settings import.*_require_admin` | 0 行（全部改为 `admin_auth`） |

---

## 测试计划（新增 / 回归）

### 新增测试

在 `tests/test_admin_auth.py` 中新增针对 `admin_auth.py` 的单元测试（Step 1 后即可写，不依赖任何路由）：

```python
# tests/test_admin_auth.py
"""Unit tests for gateway/admin_auth.py — the shared admin gate helper."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))

import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException

from admin_auth import is_admin, require_admin, _require_admin, _is_admin


def _make_user(role):
    u = MagicMock()
    u.role = role
    return u


class TestIsAdmin:
    def test_admin_role_returns_true(self):
        assert is_admin(_make_user("admin")) is True

    def test_user_role_returns_false(self):
        assert is_admin(_make_user("user")) is False

    def test_none_role_returns_false(self):
        u = MagicMock(); del u.role  # ensure getattr returns None
        u = _make_user(None)
        assert is_admin(u) is False

    def test_empty_string_role_returns_false(self):
        assert is_admin(_make_user("")) is False


class TestRequireAdmin:
    def test_none_user_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(None)
        assert exc.value.status_code == 401

    def test_non_admin_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(_make_user("user"))
        assert exc.value.status_code == 403

    def test_admin_returns_user(self):
        u = _make_user("admin")
        result = require_admin(u)
        assert result is u

    def test_alias_require_admin_same_as_require_admin(self):
        """_require_admin alias must behave identically."""
        u = _make_user("admin")
        assert _require_admin(u) is u

    def test_alias_is_admin_same_as_is_admin(self):
        assert _is_admin(_make_user("admin")) is True
        assert _is_admin(_make_user("user")) is False
```

**新增测试验收**：

```bash
python -m pytest tests/test_admin_auth.py -v 2>&1 | tail -10
# 期望：9 passed
```

### 回归测试

- `tests/test_admin_gate_coverage.py` — 每次 commit 后必跑（2 passed）
- 各文件迁移后对应的测试套件（见 Step 2 各子步骤验收）
- Step 5 全量 admin 测试套件

---

## 回滚方案

**哪些文件**：`gateway/admin_auth.py`（新建）+ 所有被修改的 `gateway/*.py` 和 `gateway/pan/*.py`（删除本地定义 + 新增 import）+ `tests/test_admin_gate_coverage.py` + `tests/test_admin_auth.py`（新建）。

**commit 边界**：

- Step 1 独立 commit：仅 `gateway/admin_auth.py`（新文件，现有代码不受影响，可随时 revert 而不破坏任何功能）。
- Step 2 每个子步骤可单独 commit（`git commit -- gateway/<file>.py`），出问题只 revert 该文件。
- Step 3 / Step 4 / Step 5 各自独立 commit。

**回滚操作**：

```bash
# 回滚整个分支（若未合入 main）
git switch main
git branch -D quality/admin-auth-dep

# 回滚单个文件
git checkout main -- gateway/admin_disk_api.py   # 举例
```

---

## 完成定义（DoD）

- [ ] `gateway/admin_auth.py` 存在，`test -f gateway/admin_auth.py` 返回 exit code 0
- [ ] `grep -rn "^def _require_admin\|^def _is_admin" gateway/*.py` 输出 0 行（顶层全部迁走）
- [ ] `grep -rn "^def _require_admin\|^def _is_admin" gateway/pan/*.py` 输出 2 行（pan/ 显式例外，有注释说明独立认证上下文）
- [ ] `grep -rn "from admin_settings import.*_require_admin" gateway/` 输出 0 行
- [ ] `python -m pytest tests/test_admin_gate_coverage.py -q` → 2 passed
- [ ] `python -m pytest tests/test_admin_auth.py -v` → 所有测试通过（≥ 9 passed）
- [ ] 各被迁移文件对应的测试套件（Step 2 各子步骤列出的测试命令）均无新增 FAILED
- [ ] Step 5 全量 admin 测试套件无新增 FAILED
- [ ] `tests/test_admin_gate_coverage.py` 的 `_ADMIN_FILES` 含 `admin_support_api.py`，`baseline` 已更新为实测值
- [ ] `tests/` 中无 `admin_settings._require_admin` monkeypatch 路径残留
- [ ] `gateway/admin_auth.py` 文件头 `BACKGROUND` 注释中说明 pan/ 例外；`gateway/pan/auth.py` 对应函数上有注释说明独立认证上下文及迁移计划
- [ ] 各步独立 commit、显式 pathspec（`git commit -- <files>`）、未 `git add .`

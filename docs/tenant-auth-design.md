# 租户登录认证与隔离系统设计

## 1. 需求概述

### 1.1 背景

当前 `wetty-mcp-terminal` 作为 AI 可控的 SSH 终端管理服务，存在以下安全问题：

| 问题 | 现状 | 风险 |
|------|------|------|
| **无用户概念** | 全局只有一个 API Token，所有使用者共享 | 无法区分操作者身份 |
| **无会话隔离** | `TerminalManager._sessions` 全局字典，任何人可操作任何会话 | A 用户可以看到/操控 B 用户的终端 |
| **WebSocket 无认证** | `/ws/terminal/{session_id}` 不经过 `auth_middleware` | 知道 session_id 即可连接 |
| **无主机访问控制** | 所有用户可见所有主机 | 无法按租户限制可访问的主机范围 |
| **前端无登录** | 前端完全不传递认证信息 | 开放式访问 |

### 1.2 设计目标

将系统升级为**轻量级堡垒机**，实现：

| 目标 | 说明 |
|------|------|
| **租户登录** | 用户通过账号密码登录，获取 JWT Token |
| **会话隔离** | 每个租户只能看到和操作自己创建的终端会话 |
| **主机授权** | 不同租户可见不同的主机（基于 tags 授权） |
| **WebSocket 认证** | WebSocket 连接时验证 Token + 会话归属 |
| **MCP 兼容** | MCP 客户端通过 Bearer Token 认证，自动关联租户身份 |
| **审计日志** | 记录谁在什么时候连接了哪台主机 |

### 1.3 约束条件

- **轻量级**：不引入外部 IAM/LDAP，自建用户表 + JWT
- **YAML 驱动**：租户配置参照 `hosts.yaml` SSOT 模式
- **向后兼容**：开发模式（无 `WETTY_API_TOKEN`）仍可免登录使用
- **最小侵入**：尽量复用现有中间件和模块注入模式

---

## 2. 架构总览

```
                                  ┌───────────────────────────────┐
                                  │     config/tenants.yaml       │
                                  │                               │
                                  │  tenants:                     │
                                  │    - id: alice                │
                                  │      name: Alice              │
                                  │      password: (bcrypt hash)  │
                                  │      allowed_tags: [dev, tce] │
                                  │      role: admin              │
                                  │    - id: bob                  │
                                  │      ...                      │
                                  └──────────────┬────────────────┘
                                                 │ 启动加载 / 热加载
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         TenantRegistry (核心注册表)                           │
│                                                                              │
│  - load_from_yaml(path)          # 加载租户配置                               │
│  - authenticate(id, password)    # 验证凭据 → JWT                           │
│  - get_tenant(tenant_id)         # 获取租户信息                               │
│  - get_allowed_tags(tenant_id)   # 获取租户可见的 host tags                   │
│  - verify_token(token) → Tenant  # JWT Token 验证                           │
└──────────────────────────────────────────────────────────────────────────────┘
        │                    │                    │                    │
        ▼                    ▼                    ▼                    ▼
┌──────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌─────────────────┐
│  Login API   │ │   auth_middleware │ │  Session Filter  │ │  Host Filter    │
│              │ │   (升级版)        │ │                  │ │                 │
│ POST /login  │ │  JWT 解析 →      │ │ TerminalManager  │ │ HostManager     │
│ → JWT Token  │ │  request.state   │ │ .tenant_id 过滤  │ │ .tags 过滤      │
└──────────────┘ │  .tenant 注入    │ └──────────────────┘ └─────────────────┘
                 └──────────────────┘
```

### 2.1 分层职责

| 层 | 职责 | 变更频率 |
|----|------|----------|
| **Config 层** | `tenants.yaml` 租户元数据（账号、密码哈希、授权 tags） | 新增/修改租户时 |
| **Registry 层** | 解析配置、密码验证、JWT 签发/验证 | 极少变动 |
| **Middleware 层** | 从 JWT 提取租户身份，注入 `request.state.tenant` | 极少变动 |
| **API 层** | 各端点根据 `request.state.tenant` 做过滤和权限校验 | 少量改动 |
| **Frontend 层** | 登录页面 + Token 管理 + API 请求注入 Authorization | 新增 |

---

## 3. 详细设计

### 3.1 Config 层 — `config/tenants.yaml`

```yaml
# 租户认证配置
# 本文件是租户数据的 Single Source of Truth
# 支持 watchfiles 热加载（修改后无需重启）

# JWT 签名密钥（生产环境必须设置，可通过 WETTY_JWT_SECRET 环境变量覆盖）
jwt_secret: "change-me-in-production"

# Token 过期时间（小时）
token_expire_hours: 24

tenants:
  - id: admin
    name: 管理员
    password_hash: "$2b$12$..."      # bcrypt 哈希（通过 CLI 工具生成）
    role: admin                       # admin: 全部主机可见 | user: 按 tags 过滤
    allowed_tags: []                  # admin 角色忽略此字段

  - id: alice
    name: Alice
    password_hash: "$2b$12$..."
    role: user
    allowed_tags:                     # 只能看到带这些 tags 的主机
      - dev
      - tce

  - id: bob
    name: Bob
    password_hash: "$2b$12$..."
    role: user
    allowed_tags:
      - dev
```

#### 3.1.1 密码哈希生成工具

提供 CLI 工具生成 bcrypt 哈希，方便管理：

```bash
# 用法
python -m src.utils.password_hash "your-password"
# 输出
# $2b$12$...（粘贴到 tenants.yaml 的 password_hash 字段）
```

### 3.2 Registry 层 — `src/services/tenant_registry.py`

```python
class TenantRegistry:
    """租户核心注册表

    YAML SSOT 的内存表示，提供：
    - 租户认证（bcrypt 密码验证 + JWT 签发）
    - Token 验证（JWT 解析 + 过期检查）
    - 租户查询（按 ID 获取租户信息）
    """

    def load_from_yaml(self, yaml_path: Path) -> None: ...
    def reload(self) -> None: ...

    def authenticate(self, tenant_id: str, password: str) -> str | None:
        """验证凭据，成功返回 JWT Token，失败返回 None"""

    def verify_token(self, token: str) -> Tenant | None:
        """验证 JWT Token，成功返回 Tenant 对象，失败返回 None"""

    def get_tenant(self, tenant_id: str) -> Tenant | None: ...
    def get_allowed_tags(self, tenant_id: str) -> list[str] | None: ...
```

#### 3.2.1 数据模型

```python
# src/models/tenant.py

class TenantRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

class Tenant(BaseModel):
    id: str
    name: str
    password_hash: str
    role: TenantRole = TenantRole.USER
    allowed_tags: list[str] = []

class TenantsConfig(BaseModel):
    jwt_secret: str = "change-me"
    token_expire_hours: int = 24
    tenants: list[Tenant] = []
```

#### 3.2.2 JWT Token 结构

```json
{
  "sub": "alice",           // 租户 ID
  "name": "Alice",          // 租户名称
  "role": "user",           // 角色
  "exp": 1714000000,        // 过期时间
  "iat": 1713913600         // 签发时间
}
```

### 3.3 Middleware 层 — auth_middleware 升级

当前 `auth_middleware` 只做 Token 存在性验证，需升级为 **JWT 解析 + 租户身份注入**：

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # 白名单路径免认证
    if path in _AUTH_WHITELIST or path == "/api/auth/login":
        return await call_next(request)

    # 非保护路径免认证（静态文件等）
    if not path.startswith("/api/") and not path.startswith("/mcp/"):
        return await call_next(request)

    # ── 新增：JWT Token 解析 ──
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

        # 1. 优先尝试环境变量 Token（MCP / API 全局 Token，向后兼容）
        env_token = os.environ.get("WETTY_API_TOKEN")
        if env_token and secrets.compare_digest(token, env_token):
            # 环境变量 Token 视为 admin 身份（MCP 场景）
            request.state.tenant = _SYSTEM_TENANT  # 预定义的系统租户
            return await call_next(request)

        # 2. 尝试 JWT Token 解析（前端 / 多租户场景）
        tenant = tenant_registry.verify_token(token)
        if tenant:
            request.state.tenant = tenant
            return await call_next(request)

        # 3. 尝试自动生成的 Token（向后兼容）
        if verify_api_token(token):
            request.state.tenant = _SYSTEM_TENANT
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "无效的 Token"})

    # 开发模式放行
    if not os.environ.get("WETTY_API_TOKEN"):
        request.state.tenant = _SYSTEM_TENANT
        return await call_next(request)

    return JSONResponse(status_code=401, ...)
```

**关键设计**：`request.state.tenant` 统一注入租户身份，下游所有 API 都可以通过 `request.state.tenant` 获取当前操作者。

### 3.4 会话隔离 — TerminalManager 改造

#### 3.4.1 instance_name 增加租户前缀

```python
# 改前：instance_name = "tce-server--m12"
# 改后：instance_name = "alice::tce-server--m12"

@staticmethod
def build_instance_name(path: list[Host], tenant_id: str = "") -> str:
    host_path = "--".join(node.name for node in path)
    return f"{tenant_id}::{host_path}" if tenant_id else host_path
```

#### 3.4.2 会话操作增加租户校验

```python
class TerminalManager:
    def create_session(self, ..., tenant_id: str = "") -> tuple[TerminalSession, bool]:
        # instance_name 带上 tenant 前缀
        instance_name = f"{tenant_id}::{raw_instance_name}" if tenant_id else raw_instance_name
        ...

    def list_sessions(self, tenant_id: str = "") -> list[TerminalInfo]:
        """列出会话，按 tenant_id 过滤"""
        sessions = self._sessions.values()
        if tenant_id:
            sessions = [s for s in sessions if s.instance_name.startswith(f"{tenant_id}::")]
        return [...]

    def get_session_by_id(self, session_id: str, tenant_id: str = "") -> TerminalSession | None:
        """按 session_id 获取，增加 tenant 校验"""
        session = self._sessions_by_id.get(session_id)
        if session and tenant_id:
            if not session.instance_name.startswith(f"{tenant_id}::"):
                return None  # 非本租户的会话
        return session
```

#### 3.4.3 前端展示 instance_name 去掉租户前缀

```python
def display_instance_name(instance_name: str) -> str:
    """去掉 tenant 前缀，用于前端展示"""
    return instance_name.split("::", 1)[-1] if "::" in instance_name else instance_name
```

### 3.5 主机授权 — HostManager 过滤

```python
# src/api/hosts.py

@router.get("", response_model=list[HostResponse])
async def list_hosts(
    request: Request,
    manager: HostManagerDep,
    tag: str | None = None,
) -> list[HostResponse]:
    tenant: Tenant = request.state.tenant

    # admin 角色看到所有主机
    if tenant.role == TenantRole.ADMIN:
        return await manager.list_host_responses(tag=tag)

    # user 角色按 allowed_tags 过滤
    allowed_tags = tenant.allowed_tags
    return await manager.list_host_responses(tag=tag, filter_tags=allowed_tags)
```

### 3.6 WebSocket 认证

WebSocket 不经过 HTTP middleware，需要在 handshake 阶段单独验证：

```python
@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    mgr = _get_terminal_manager()

    # ── 新增：WebSocket Token 认证 ──
    # 方式1：URL query parameter（推荐，浏览器 WebSocket 不便设 Header）
    token = websocket.query_params.get("token")
    # 方式2：Sec-WebSocket-Protocol header（可选备选）
    if not token:
        protocol_header = websocket.headers.get("sec-websocket-protocol", "")
        if protocol_header.startswith("bearer."):
            token = protocol_header[7:]

    tenant = _verify_ws_token(token)
    if not tenant:
        await websocket.close(code=1008, reason="认证失败")
        return

    # ── 新增：会话归属校验 ──
    session = mgr.get_session_by_id(session_id, tenant_id=tenant.id)
    if not session or not session.running:
        await websocket.close(code=1008, reason="终端会话不存在或无权限")
        return

    await websocket.accept()
    ...
```

前端 WebSocket 连接时带上 Token：

```typescript
// useWebSocket.ts
const fullUrl = `${protocol}//${window.location.host}${wsUrl}?token=${getStoredToken()}`;
const ws = new WebSocket(fullUrl);
```

### 3.7 Login API

```python
# src/api/auth.py

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    tenant_id: str
    password: str

class LoginResponse(BaseModel):
    token: str
    tenant_id: str
    name: str
    role: str
    expires_at: str  # ISO 8601

@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    token = tenant_registry.authenticate(req.tenant_id, req.password)
    if not token:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    tenant = tenant_registry.get_tenant(req.tenant_id)
    return LoginResponse(
        token=token,
        tenant_id=tenant.id,
        name=tenant.name,
        role=tenant.role.value,
        expires_at=...,
    )
```

### 3.8 Frontend — 登录页面 + Token 管理

#### 3.8.1 组件架构

```
App.tsx
├── LoginPage.tsx (未登录时展示)
│   └── LoginForm (用户名 + 密码)
└── MainLayout.tsx (已登录后展示，即现有 App 主体)
    ├── Header (显示当前租户名 + 退出按钮)
    ├── HostList (按租户权限过滤)
    └── TerminalView (会话已隔离)
```

#### 3.8.2 Token 管理

```typescript
// frontend/src/services/auth.ts

const TOKEN_KEY = "wetty_jwt_token";

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setStoredToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function isTokenExpired(token: string): boolean {
  const payload = JSON.parse(atob(token.split(".")[1]));
  return Date.now() / 1000 > payload.exp;
}
```

#### 3.8.3 API 请求注入 Authorization

```typescript
// frontend/src/services/api.ts — fetchWithRetry 升级

async function fetchWithRetry(
  input: RequestInfo | URL,
  init?: RequestInit,
  retries = 3,
  baseDelay = 500,
): Promise<Response> {
  // ── 新增：自动注入 Authorization Header ──
  const token = getStoredToken();
  const headers = new Headers(init?.headers);
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const mergedInit = { ...init, headers };

  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(input, mergedInit);

      // ── 新增：401 自动跳转登录 ──
      if (res.status === 401) {
        clearToken();
        window.location.reload();
        throw new Error("认证过期，请重新登录");
      }

      return res;
    } catch (err) {
      lastError = err;
      if (attempt < retries) {
        const delay = baseDelay * 2 ** attempt;
        await new Promise((r) => setTimeout(r, delay));
      }
    }
  }
  throw lastError;
}
```

### 3.9 MCP 兼容

MCP 客户端通过 `mcp.json` 中的 `headers` 传递 Bearer Token：

```json
{
  "wetty-terminal": {
    "url": "http://<host>:8000/mcp/",
    "transportType": "streamable-http",
    "headers": {
      "Authorization": "Bearer <jwt-token-or-api-token>"
    }
  }
}
```

**两种 Token 兼容**：
1. **WETTY_API_TOKEN**（环境变量）：视为 admin 身份，全局权限
2. **JWT Token**：按租户身份过滤主机和会话

MCP 工具中获取当前租户身份：

```python
# MCP 工具中无法直接访问 request.state.tenant（因为 MCP SDK 封装了请求处理）
# 方案：在 auth_middleware 中将 tenant_id 存入 ContextVar
_current_tenant: ContextVar[Tenant | None] = ContextVar("current_tenant", default=None)

# auth_middleware 中
request.state.tenant = tenant
_current_tenant.set(tenant)

# MCP 工具中
@mcp.tool()
async def connect_host(host_name: str, ...) -> str:
    tenant = _current_tenant.get()
    tenant_id = tenant.id if tenant else ""
    ...
```

### 3.10 审计日志

在关键操作点记录租户行为：

```python
# 结构化日志
logger.info(
    "AUDIT: tenant=%s action=%s target=%s",
    tenant.id,
    "connect_host",
    host_name,
    extra={"audit": True, "tenant": tenant.id, "action": "connect_host", "target": host_name},
)
```

审计事件类型：
- `login` / `logout` — 登录/登出
- `connect_host` — 连接主机
- `disconnect_host` — 断开主机
- `load_snippet` — 加载排障脚本
- `run_command` — 执行命令（MCP 场景）

---

## 4. 安全设计

### 4.1 密码安全

| 措施 | 说明 |
|------|------|
| bcrypt 哈希 | 密码不明文存储，使用 bcrypt（cost=12）|
| 慢比较 | `bcrypt.checkpw()` 内置常数时间比较 |
| 密码不传输 | 只在登录 API 传输一次，后续全用 JWT |

### 4.2 JWT 安全

| 措施 | 说明 |
|------|------|
| HS256 签名 | 使用 `WETTY_JWT_SECRET` 环境变量作为密钥 |
| 短过期时间 | 默认 24h，可在 `tenants.yaml` 配置 |
| Token 不含敏感信息 | 只含 tenant_id、name、role |
| 前端自动清理 | 401 响应时清除 localStorage 中的 Token |

### 4.3 WebSocket 安全

| 措施 | 说明 |
|------|------|
| Token 必须 | WebSocket 握手时必须提供 Token |
| 会话归属校验 | 即使知道 session_id 也无法连接非本租户的会话 |
| query param 传递 | 浏览器 WebSocket API 不支持自定义 Header |

### 4.4 开发模式兼容

当 **`WETTY_API_TOKEN` 未设置** 且 **`tenants.yaml` 不存在** 时：
- 所有请求视为内置 `_SYSTEM_TENANT`（admin 角色）
- 前端不显示登录页面
- 行为与当前完全一致

---

## 5. 与现有系统的集成影响分析

| 模块 | 改动量 | 说明 |
|------|--------|------|
| `config/tenants.yaml` | **新增** | 租户配置文件（含 max_sessions） |
| `src/models/tenant.py` | **新增** | Pydantic 数据模型（含 RefreshTokenInfo） |
| `src/services/tenant_registry.py` | **新增** | 核心注册表（含 Refresh Token 管理 + 并发限制） |
| `src/api/auth.py` | **新增** | 登录 / 刷新 / 注销 / 密码修改 API |
| `src/api/admin.py` | **新增** | 管理 API（在线用户列表、Admin 重置密码） |
| `src/utils/password_hash.py` | **新增** | CLI 密码哈希工具 |
| `frontend/src/services/auth.ts` | **新增** | Token 管理（含自动刷新 + refresh_token 持久化） |
| `frontend/src/components/LoginPage.tsx` | **新增** | 登录页面 |
| `src/main.py` | 中等改动 | auth_middleware 升级 + TenantRegistry 注入 |
| `src/api/terminal.py` | 中等改动 | WebSocket 认证 + 会话隔离 |
| `src/api/hosts.py` | 小量改动 | 主机过滤 |
| `src/api/events.py` | 中等改动 | SSE 事件隔离（Event 携带 tenant_id + 分发过滤） |
| `src/services/terminal_manager.py` | 中等改动 | instance_name 租户前缀 + 会话查询过滤 |
| `src/mcp_server/server.py` | 小量改动 | ContextVar 获取租户身份 |
| `frontend/src/services/api.ts` | 中等改动 | fetchWithRetry 注入 Authorization + 401 自动 refresh |
| `frontend/src/App.tsx` | 中等改动 | 条件渲染登录/主界面 + 登出按钮 |
| `frontend/src/hooks/useWebSocket.ts` | 小量改动 | WebSocket URL 带 Token |

**对现有核心逻辑影响评估**：
- TerminalSession 本身不变，只是 TerminalManager 的查询和创建接口增加 tenant_id 参数
- hosts.yaml 不变，主机过滤在 API 层完成
- snippets.yaml 不变，排障脚本不做租户隔离（所有用户可用）
- MCP 工具签名不变，租户身份通过 ContextVar 透传
- EventBus 核心发布/订阅机制不变，只在分发层增加过滤逻辑

---

## 6. 实施计划

### Sprint 1 — 后端认证核心 + Token 刷新

| 任务 | 详情 | 依赖 |
|------|------|------|
| 创建 `src/models/tenant.py` | Pydantic 数据模型（含 `RefreshTokenInfo`、`max_sessions`） | 无 |
| 创建 `src/services/tenant_registry.py` | Registry 核心（YAML 加载 + bcrypt 验证 + JWT 签发/验证 + Refresh Token 管理） | tenant.py |
| 创建 `config/tenants.yaml` | 初始配置（含至少一个 admin 账号，含 `max_sessions` 字段） | 无 |
| 创建 `src/api/auth.py` | 登录 `POST /api/auth/login` + 刷新 `POST /api/auth/refresh` + 注销 `POST /api/auth/logout` | tenant_registry.py |
| 创建 `src/utils/password_hash.py` | CLI 密码哈希生成工具 | 无 |
| 升级 `src/main.py` | TenantRegistry 注入 + auth_middleware 升级为 JWT 解析 | 全部上述 |
| 新增依赖 | `bcrypt`、`PyJWT`（加入 requirements.txt） | 无 |

**验收标准**：
- `POST /api/auth/login` 返回 `access_token` (2h) + `refresh_token` (7d)
- 使用 JWT Token 调用 `/api/hosts` 正常返回
- 使用无效 Token 返回 401
- `POST /api/auth/refresh` 换取新 Token 对（旧 refresh_token 失效）
- `POST /api/auth/logout` 清除 refresh_token
- 并发登录超过 `max_sessions` 时踢掉最早登录
- 开发模式（无环境变量、无 tenants.yaml）仍可正常访问

### Sprint 2 — 会话隔离 + 主机授权 + SSE 隔离

| 任务 | 详情 | 依赖 |
|------|------|------|
| 改造 `TerminalManager` | instance_name 租户前缀 + 会话查询/操作增加 tenant_id 过滤 | Sprint 1 |
| 升级 `src/api/terminal.py` | WebSocket 认证 + start/stop/list 操作注入 tenant_id | Sprint 1 |
| 升级 `src/api/hosts.py` | 主机列表按 tenant.allowed_tags 过滤 | Sprint 1 |
| 升级 `src/mcp_server/server.py` | ContextVar 获取租户身份 + 工具中传递 tenant_id | Sprint 1 |
| 改造 SSE EventBus | 事件携带 tenant_id + SSE 连接认证 + 分发过滤 | Sprint 1 |

**验收标准**：
- Alice 创建的会话 Bob 看不到
- Alice 只能看到 `allowed_tags` 匹配的主机
- WebSocket 无 Token 或错误 Token 无法连接
- admin 角色可看到所有主机和会话
- SSE 事件：Alice 只收到自己的会话事件 + 自己可见主机的事件
- admin 收到所有事件

### Sprint 3 — 前端登录 UI + Token 自动刷新

| 任务 | 详情 | 依赖 |
|------|------|------|
| 创建 `frontend/src/services/auth.ts` | Token 管理（localStorage 存 access_token + refresh_token + 自动刷新） | 无 |
| 创建 `frontend/src/components/LoginPage.tsx` | 登录页面 UI | auth.ts |
| 升级 `frontend/src/services/api.ts` | fetchWithRetry 注入 Authorization + 401 自动 refresh + 请求重放 | auth.ts |
| 升级 `frontend/src/hooks/useWebSocket.ts` | WebSocket URL 带 Token query param | auth.ts |
| 升级 `frontend/src/App.tsx` | 条件渲染：未登录 → LoginPage / 已登录 → MainLayout + 登出按钮 | 全部上述 |

**验收标准**：
- 打开页面看到登录表单
- 登录后正常使用所有功能
- access_token 过期时自动 refresh，用户无感
- refresh_token 也过期时跳转登录页
- 登出后清除 Token + 调用后端 logout API
- 开发模式无 tenants.yaml 时不显示登录页

### Sprint 4 — 管理 API + 审计日志 + 热加载 + 测试

| 任务 | 详情 | 依赖 |
|------|------|------|
| `PUT /api/auth/password` | 密码修改 API（验证旧密码 + 新密码 bcrypt + YAML 写回 + 清除 refresh_token） | Sprint 1 |
| `PUT /api/admin/tenants/{id}/password` | Admin 重置密码 API | Sprint 1 |
| `GET /api/admin/online-users` | 在线用户列表 API（含活跃会话数、最后活动时间） | Sprint 2 |
| 审计日志 | 关键操作记录结构化日志 | Sprint 2 |
| tenants.yaml 热加载 | watchfiles 监听 + 防抖 | Sprint 1 |
| 单元测试 | 认证、隔离、过滤、JWT 过期、Token 刷新、并发限制 | Sprint 2 |
| 端到端测试 | 多租户并发操作验证 | Sprint 3 |

---

## 7. 设计决策记录

| 决策 | 选项 | 决定 | 理由 |
|------|------|------|------|
| 用户存储 | DB / YAML / LDAP | **YAML** | 与 hosts.yaml 一致，轻量、支持热加载 |
| 认证方式 | Session Cookie / JWT / OAuth | **JWT** | 无状态、前后端分离友好、MCP 兼容 |
| 密码哈希 | SHA256 / bcrypt / argon2 | **bcrypt** | 成熟稳定、内置慢哈希、Python 生态支持好 |
| 主机授权 | RBAC / ABAC / tags | **tags** | 复用现有 hosts.yaml 的 tags 字段，最小改动 |
| 会话隔离 | 独立 TerminalManager / 过滤 | **过滤** | 最小侵入，复用现有 Manager 架构 |
| WebSocket 认证 | Header / Query / Protocol | **Query param** | 浏览器 WebSocket API 不支持自定义 Header |
| Snippet 隔离 | 按租户 / 全部可见 | **全部可见** | 排障脚本是通用能力，无需按租户限制 |

---

## 8. 遗留问题（已决策）

| # | 问题 | 决策 | 设计方案 |
|---|------|------|----------|
| 1 | **Token 刷新** | ✅ 需要 | Refresh Token 机制，详见 8.1 |
| 2 | **密码修改** | ✅ 需要 | 提供 API + YAML 双写，详见 8.2 |
| 3 | **在线用户列表** | ✅ 需要 | Admin API 查看当前在线用户，详见 8.3 |
| 4 | **并发登录限制** | ✅ 限制 | 限制同一租户并发登录数，详见 8.4 |
| 5 | **SSE 事件隔离** | ✅ 需要 | SSE 事件流按租户过滤，详见 8.5 |
| 6 | **HTTPS 强制** | ❌ 暂不需要 | 暂不实施，后续按需添加 |

### 8.1 Token 刷新机制

**方案：Access Token + Refresh Token 双 Token 模式**

```
登录成功 → 返回 access_token (短期, 2h) + refresh_token (长期, 7d)
                        │
           access_token 过期
                        │
   POST /api/auth/refresh (携带 refresh_token)
                        │
           返回新 access_token + 新 refresh_token (Token Rotation)
```

**设计要点**：
- `access_token`：JWT HS256，有效期 **2小时**，用于 API/WebSocket 认证
- `refresh_token`：随机 UUID，有效期 **7天**，存储在 `TenantRegistry` 的内存字典中
- **Token Rotation**：每次刷新时旧 refresh_token 失效，签发新的，防止 Token 泄漏后长期可用
- **API**：`POST /api/auth/refresh`，请求体 `{ "refresh_token": "..." }`
- **前端**：`auth.ts` 拦截 401 响应 → 自动调用 refresh → 重放失败的请求
- **注销**：`POST /api/auth/logout` 清除 refresh_token

**数据结构**：
```python
# TenantRegistry 内存存储
_refresh_tokens: dict[str, RefreshTokenInfo]  # key: refresh_token_value

@dataclass
class RefreshTokenInfo:
    tenant_id: str
    expires_at: float  # timestamp
    created_at: float
```

### 8.2 密码修改

**方案：API 修改 + YAML 同步写入**

- **API**：`PUT /api/auth/password`
- **请求体**：`{ "old_password": "...", "new_password": "..." }`
- **权限**：任何已登录租户可修改自己的密码；admin 可通过 `PUT /api/admin/tenants/{tenant_id}/password` 重置其他用户密码
- **流程**：
  1. 验证 old_password 与当前 bcrypt hash 匹配
  2. 生成新 bcrypt hash
  3. 更新 `TenantRegistry` 内存中的 hash
  4. **同步写回 `config/tenants.yaml`**（原子写入：先写 `.tmp` 再 rename）
  5. 清除该租户所有 refresh_token（强制重新登录）
- **安全**：新密码强度校验（最少 8 字符，含大小写+数字）

### 8.3 在线用户列表

**方案：Admin API 查看在线状态**

- **API**：`GET /api/admin/online-users`（仅 `role: admin` 可调用）
- **在线判定**：拥有未过期的 refresh_token 即视为在线
- **响应数据**：

```json
{
  "online_users": [
    {
      "tenant_id": "alice",
      "name": "Alice",
      "role": "user",
      "active_sessions": 3,
      "last_activity": "2026-04-27T19:00:00Z",
      "login_time": "2026-04-27T08:00:00Z"
    }
  ],
  "total": 1
}
```

- **活跃会话数**：从 `TerminalManager` 按 `tenant_id` 统计当前运行中的会话
- **最后活动时间**：通过 access_token 签发时间或 refresh 时间更新

### 8.4 并发登录限制

**方案：基于 Refresh Token 数量的并发限制**

- **配置**：`tenants.yaml` 中新增 `max_sessions` 字段（可选，默认 3）

```yaml
tenants:
  - id: alice
    name: Alice
    password_hash: "$2b$12$..."
    role: user
    allowed_tags: [dev, staging]
    max_sessions: 2  # 最多同时 2 个登录会话
```

- **机制**：
  1. 登录时检查该 tenant 已存在的有效 refresh_token 数量
  2. 如果已达到 `max_sessions`，**踢掉最早的登录**（FIFO 策略）
  3. 被踢掉的 refresh_token 失效 → 对应客户端下次请求时 401 → 前端跳转登录页
- **admin 不受限**：`role: admin` 不受并发限制
- **全局默认值**：`WETTY_MAX_LOGIN_SESSIONS` 环境变量，默认 3

### 8.5 SSE 事件隔离

**方案：EventBus 增加租户维度过滤**

- **现状**：`/api/events` SSE 端点全局广播所有事件（会话状态变更、主机上下线等）
- **改造**：
  1. 事件发布时携带 `tenant_id` 字段（或 `"*"` 表示全局事件）
  2. SSE 连接建立时从 Token 解析 `tenant_id`
  3. 事件分发时过滤：
     - `tenant_id == "*"` → 所有人可见（如主机上下线，仅对 admin 全量，普通租户只看到自己 allowed_tags 内的主机事件）
     - `tenant_id == 连接者的 tenant_id` → 仅本人可见（如会话状态变更）
     - `role: admin` → 可见所有事件

- **EventBus 改造**：

```python
class Event:
    type: str
    data: dict
    tenant_id: str = "*"  # 新增字段

class SSESubscription:
    tenant_id: str  # 订阅者身份
    role: str
    queue: asyncio.Queue

def should_deliver(event: Event, subscriber: SSESubscription) -> bool:
    if subscriber.role == "admin":
        return True
    if event.tenant_id == "*":
        return True  # 全局事件（已在发布时过滤了主机范围）
    return event.tenant_id == subscriber.tenant_id
```

---

## 9. 更新后的实施计划

基于遗留问题决策，Sprint 安排调整如下：

### Sprint 1 — 后端认证核心（不变 + Token 刷新）

在原有基础上新增：
- `POST /api/auth/refresh` — Token 刷新 API
- `POST /api/auth/logout` — 注销 API（清除 refresh_token）
- `RefreshTokenInfo` 内存管理逻辑
- `TenantRegistry` 新增 `_refresh_tokens` 字典 + 自动过期清理

### Sprint 2 — 会话隔离 + 主机授权 + SSE 隔离

在原有基础上新增：
- SSE EventBus 改造：事件携带 `tenant_id`
- SSE 订阅连接认证 + 租户过滤
- 并发登录限制：登录时检查 + FIFO 踢出

### Sprint 3 — 前端登录 UI + Token 管理（不变 + 自动刷新）

在原有基础上新增：
- `auth.ts` 增加 refresh_token 持久化 + 401 自动刷新 + 请求重放
- 登出时调用 `POST /api/auth/logout`

### Sprint 4 — 审计日志 + 管理 API + 热加载 + 测试

在原有基础上新增：
- `PUT /api/auth/password` — 密码修改 API
- `PUT /api/admin/tenants/{tenant_id}/password` — Admin 重置密码
- `GET /api/admin/online-users` — 在线用户列表 API
- 密码修改后同步写回 YAML + 清除 refresh_token
- 管理 API 权限校验（仅 admin）

---

## 状态

- **当前阶段**：方案设计完成，遗留问题已全部决策
- **下一步**：确认后说"开始实施"，从 Sprint 1 开始

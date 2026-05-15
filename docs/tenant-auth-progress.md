# 租户认证系统 — 实施进展

> 设计文档：[tenant-auth-design.md](./tenant-auth-design.md)

---

## Sprint 1 — 后端认证核心 + Token 刷新 ✅

**完成时间**：2026-05-15

### 已完成任务

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1.1 | Tenant 数据模型 | `src/models/tenant.py` | ✅ |
| 1.2 | TenantRegistry 核心服务 | `src/services/tenant_registry.py` | ✅ |
| 1.3 | 租户配置 + 密码哈希工具 | `config/tenants.yaml` + `src/utils/password_hash.py` | ✅ |
| 1.4 | Auth REST API | `src/api/auth.py` | ✅ |
| 1.5 | auth_middleware 升级 | `src/main.py` | ✅ |
| 1.6 | 依赖更新 + 文档 | `requirements.txt` + 本文档 | ✅ |

### 新增文件清单

| 文件 | 说明 |
|------|------|
| `src/models/tenant.py` | Pydantic 数据模型：TenantRole、Tenant、TenantConfig、TenantsConfig、RefreshTokenInfo、SYSTEM_TENANT |
| `src/services/tenant_registry.py` | 核心注册表：YAML 加载、bcrypt 认证、JWT 签发/验证、Refresh Token 管理、并发登录限制、ContextVar 租户传播 |
| `src/api/auth.py` | 4 个认证端点：login、refresh、logout、password |
| `src/utils/password_hash.py` | CLI 工具：`python -m src.utils.password_hash "password"` |
| `config/tenants.yaml` | 默认配置：admin 账号（admin123, super_admin 角色） |

### 修改文件清单

| 文件 | 改动说明 |
|------|----------|
| `src/main.py` | 新增 TenantRegistry 初始化 + tenants.yaml 热加载 + auth_middleware 三优先级 Token 验证 + auth_api 路由注册 |
| `requirements.txt` | 新增 `bcrypt>=4.0.0`、`PyJWT>=2.8.0` |

### 关键设计实现

1. **三优先级 Token 验证**（auth_middleware）：
   - Priority 1：环境变量 `WETTY_API_TOKEN` → SYSTEM_TENANT
   - Priority 2：JWT Token → 解析出 Tenant
   - Priority 3：自动生成的 Token → SYSTEM_TENANT
   - Fallback：开发模式（无环境变量 + 无 tenants.yaml）→ SYSTEM_TENANT

2. **Token Rotation**：每次 refresh 旧 refresh_token 立即失效，签发新的一对 token

3. **并发登录限制**：基于 refresh_token 数量的 FIFO 踢出策略，admin 角色不受限

4. **ContextVar 租户传播**：`current_tenant_var` 在 auth_middleware 中设置，MCP 工具通过 ContextVar 获取租户身份

5. **Auth 白名单**：`/api/auth/login`、`/api/auth/refresh`、`/api/auth/logout` 免认证

### 默认账号

| 租户 ID | 密码 | 角色 | 说明 |
|---------|------|------|------|
| admin | admin123 | super_admin | 初始管理员账号，请在生产环境中修改密码 |

---

## Sprint 2 — 会话隔离 + 主机授权 + SSE 隔离 ✅

**完成时间**：2026-05-15

### 已完成任务

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 2.1 | TerminalManager 改造 | `src/services/terminal_manager.py` | ✅ |
| 2.2 | terminal API 升级 | `src/api/terminal.py` | ✅ |
| 2.3 | hosts API 升级 | `src/api/hosts.py` | ✅ |
| 2.4 | MCP Server 升级 | `src/mcp_server/server.py` | ✅ |
| 2.5 | SSE EventBus 改造 | `src/services/event_service.py` + `src/api/events.py` | ✅ |

### 新增文件清单

| 文件 | 说明 |
|------|------|
| `src/utils/tenant_helpers.py` | FastAPI 请求中获取租户身份的公共 helper：`get_current_tenant()`、`require_admin()`、`require_super_admin()` |

### 修改文件清单

| 文件 | 改动说明 |
|------|----------|
| `src/services/terminal_manager.py` | TerminalInfo/TerminalSession 添加 `tenant_id` 字段；`_build_storage_key()` 租户前缀隔离；create/get/stop/list 方法全部增加 `tenant_id` 参数；新增 `stop_session_by_id()` |
| `src/api/terminal.py` | start/stop/list 注入 `tenant_id`；WebSocket 通过 `?token=` 查询参数认证（`_authenticate_ws_token`）；admin 可操作所有会话 |
| `src/api/hosts.py` | 新增 `_filter_hosts_by_tenant_tags()` 递归树过滤；`list_hosts` 按 `tenant.allowed_tags` 过滤主机 |
| `src/mcp_server/server.py` | 新增 `_get_current_tenant()`（ContextVar）、`_get_session_for_tenant()`（租户归属校验）；所有 MCP 工具使用租户感知的会话访问；`_publish_event` 自动携带 `tenant_id`；`list_hosts` 工具复用 tag 过滤逻辑 |
| `src/services/event_service.py` | `AgentEvent` 添加 `tenant_id="*"` 字段；新增 `_Subscriber` 数据类；`publish` 按 `_should_deliver()` 过滤分发；`subscribe()` 支持 `tenant_id`/`is_admin` 参数 |
| `src/api/events.py` | SSE 端点提取 `request.state.tenant`，传递 `tenant_id`/`is_admin` 给 `subscribe()` |

### 关键设计实现

1. **Storage Key 租户前缀隔离**：`TerminalManager._build_storage_key(tenant_id, instance_name)` → `"{tenant_id}:{instance_name}"`，物理隔离不同租户的会话

2. **WebSocket Token 认证**：浏览器 WebSocket API 不支持自定义 Header，通过 `?token=` 查询参数传递 JWT，`_authenticate_ws_token()` 实现与 auth_middleware 相同的三优先级认证

3. **递归主机树过滤**：`_filter_hosts_by_tenant_tags(hosts, allowed_tags)` 深度优先过滤，保留所有匹配子节点的中间路径节点

4. **ContextVar 租户传播**（MCP）：`current_tenant_var` 在 auth_middleware 中设置，MCP 工具通过 `_get_current_tenant()` 获取；`_get_session_for_tenant()` 统一校验会话归属

5. **SSE 事件租户隔离**：
   - `AgentEvent.tenant_id`（`"*"` = 全局事件）
   - `_Subscriber` 携带 `tenant_id` + `is_admin`
   - `_should_deliver()` 规则：admin 收全部、全局事件所有人可见、其余按 tenant_id 匹配

6. **Admin 跨租户权限**：admin 在所有隔离层（会话/主机/事件）可访问全部资源，`tenant_id=None` 表示不做归属过滤

### 验收标准对照

| 验收项 | 实现方式 | 状态 |
|--------|----------|------|
| Alice 创建的会话 Bob 看不到 | storage_key 前缀隔离 + list_sessions 按 tenant_id 过滤 | ✅ |
| Alice 只能看到 allowed_tags 匹配的主机 | `_filter_hosts_by_tenant_tags()` 递归过滤 | ✅ |
| WebSocket 无 Token 或错误 Token 无法连接 | `_authenticate_ws_token()` 三优先级校验 | ✅ |
| admin 角色可看到所有主机和会话 | `tenant_id=None` 跳过过滤 + `is_admin` 标志 | ✅ |
| SSE 事件按租户过滤分发 | `_should_deliver()` 订阅过滤 | ✅ |

---

## Sprint 3 — 前端登录 UI + Token 管理 ✅

**完成时间**：2026-05-15

### 已完成任务

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 3.1 | Token 管理服务 | `frontend/src/services/auth.ts` | ✅ |
| 3.2 | API 层认证注入 | `frontend/src/services/api.ts` | ✅ |
| 3.3 | WebSocket Token 传递 | `frontend/src/hooks/useWebSocket.ts` | ✅ |
| 3.4 | 登录页面 UI | `frontend/src/components/LoginPage.tsx` | ✅ |
| 3.5 | App 认证守卫 + 登出 | `frontend/src/App.tsx` | ✅ |
| 3.6 | 后端 auth/status 端点 | `src/api/auth.py` + `src/main.py` | ✅ |

### 新增文件清单

| 文件 | 说明 |
|------|------|
| `frontend/src/services/auth.ts` | 前端 Token 管理中心：localStorage 持久化、JWT 过期检测、静默刷新（串行化）、登录/注销 API、认证状态事件总线 |
| `frontend/src/components/LoginPage.tsx` | 登录页面组件：暗色主题、毛玻璃效果、表单验证、错误提示、Enter 提交 |

### 修改文件清单

| 文件 | 改动说明 |
|------|----------|
| `frontend/src/services/api.ts` | 新增 `_injectAuth()` + `_authedFetch()` 辅助函数；`fetchWithRetry` 增加主动刷新 + 401 自动 refresh + 请求重放；所有 16+ 处 `fetch()` 调用替换为 `_authedFetch()` / `fetchWithRetry()`；SSE `_readSSEStream` 注入 Authorization Header |
| `frontend/src/hooks/useWebSocket.ts` | WebSocket URL 追加 `?token=<encoded_jwt>` 查询参数（浏览器 WebSocket 不支持自定义 Header） |
| `frontend/src/App.tsx` | App 组件增加 `authRequired` / `tenant` 状态管理；`useEffect` 调用 `checkAuthStatus()` + 订阅 `onAuthChange`；条件渲染：loading → LoginPage → MainApp；原 App 主体抽取为 `MainApp` 组件；Header 增加租户名称 + 角色徽章 + 登出按钮 |
| `src/api/auth.py` | 新增 `AuthStatusResponse` 模型 + `GET /api/auth/status` 端点（检测后端是否启用认证） |
| `src/main.py` | `/api/auth/status` 加入 auth 白名单 |

### 关键设计实现

1. **双 Token + 自动刷新**：
   - `access_token` (2h) 存 localStorage，API 请求自动注入 `Authorization: Bearer`
   - `refresh_token` (7d) 存 localStorage，过期前用 `POST /api/auth/refresh` 静默换新
   - `_refreshPromise` 全局锁保证多个 401 并发时只触发一次 refresh

2. **fetchWithRetry 认证增强**：
   - 请求前：检测 `isAccessTokenExpired(60)` → 主动刷新（避免已知过期请求失败）
   - 请求后：401 响应 → `refreshAccessToken()` → 刷新成功则重放原请求
   - refresh 失败 → `clearAuth()` → 上层 `onAuthChange` 监听器触发登录页跳转

3. **auth.ts 事件总线**：
   - `onAuthChange(listener)` 返回 unsubscribe 函数
   - `_saveTokens()` / `clearAuth()` 时自动通知所有监听器
   - App.tsx 通过 `useEffect` 订阅，实现登录/登出状态联动

4. **开发模式兼容**：
   - `GET /api/auth/status` 检查 `WETTY_API_TOKEN` 环境变量 + `TenantRegistry.loaded`
   - 两者都不存在时返回 `{ auth_required: false }`，前端跳过登录页直接进入主界面

5. **WebSocket Token 传递**：
   - 浏览器 WebSocket API 不支持自定义 Header
   - 通过 URL 查询参数 `?token=${encodeURIComponent(jwt)}` 传递
   - 后端 `_authenticate_ws_token()` 从 query_params 解析（Sprint 2 已实现）

6. **SSE 认证**：
   - `_readSSEStream` 的 fetch 请求注入 `Authorization: Bearer` Header
   - 后端 SSE 端点从 `request.state.tenant` 提取租户（Sprint 2 已实现）

### 验收标准对照

| 验收项 | 实现方式 | 状态 |
|--------|----------|------|
| 打开页面看到登录表单 | LoginPage 组件 + `authRequired` 条件渲染 | ✅ |
| 登录后正常使用所有功能 | `_saveTokens()` 保存 Token → `onAuthChange` 触发 App 切换到 MainApp | ✅ |
| access_token 过期时自动 refresh，用户无感 | `fetchWithRetry` 主动刷新 + 401 自动 refresh + 请求重放 | ✅ |
| refresh_token 也过期时跳转登录页 | `refreshAccessToken()` 失败 → `clearAuth()` → `onAuthChange` → 显示 LoginPage | ✅ |
| 登出后清除 Token + 调用后端 logout API | `logout()` 先 `clearAuth()` 再 `POST /api/auth/logout` | ✅ |
| 开发模式无 tenants.yaml 时不显示登录页 | `GET /api/auth/status` 返回 `auth_required: false` → 跳过登录 | ✅ |

---

## Sprint 4 — 审计日志 + 管理 API + 热加载 + 测试 🔲

**状态**：待开始

---

## Sprint 5 — 运营管理控制台 🔲

**状态**：待开始

---

## 遗留问题

暂无。

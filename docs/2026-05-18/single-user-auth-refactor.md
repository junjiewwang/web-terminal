# 多租户 → 单用户认证重构

> **日期**：2026-05-18
> **状态**：✅ 已完成

---

## 需求背景

wetty-mcp-terminal 是个人远程终端管理服务，不需要多租户隔离。原有的多租户设计（三级角色、租户隔离、运营端管理）增加了不必要的复杂度，需要简化为单用户密码保护模式。

## 设计目标

**保留**：
- 密码保护（bcrypt 哈希）
- JWT Token 认证（access + refresh 双 Token）
- 认证中间件
- 前端登录页面
- 开发模式免认证
- 环境变量 Token 兼容
- YAML 配置热加载

**移除**：
- 三级角色系统（TenantRole: super_admin / admin / user）
- 多租户 YAML 配置（tenants.yaml）
- 会话租户隔离（storage_key 租户前缀）
- 主机授权过滤（allowed_tags）
- SSE 事件租户过滤
- 并发登录限制
- ContextVar 租户传播
- require_admin / require_super_admin helpers
- SYSTEM_TENANT 概念

---

## 实施清单

### 1. 新建文件

| 文件 | 说明 |
|------|------|
| `config/auth.yaml` | 扁平化认证配置（enabled, password_hash, jwt_secret, token 过期时间） |
| `src/services/auth_service.py` | 新认证服务 AuthService（~240 行），替代 TenantRegistry（~437 行）+ Tenant 模型（~125 行） |

### 2. 重写后端模块

| 文件 | 改动要点 |
|------|----------|
| `src/api/auth.py` | LoginRequest 只保留 `password`，LoginResponse 去掉 tenant_id/name/role |
| `src/main.py` | 中间件从租户注入改为 `request.state.authenticated = True`；配置监听改为 `config/auth.yaml` |
| `src/api/terminal.py` | 移除所有租户归属校验，简化 start/stop/list/websocket |
| `src/api/hosts.py` | 删除 `_filter_hosts_by_tenant_tags()` 过滤函数，直接返回全量主机 |
| `src/services/event_service.py` | AgentEvent 去掉 `tenant_id`，subscribe() 广播所有事件 |
| `src/api/events.py` | SSE 端点不再提取租户信息 |
| `src/services/terminal_manager.py` | TerminalInfo/Session 去掉 `tenant_id`，storage_key 直接用 instance_name |
| `src/mcp_server/server.py` | 移除 ContextVar 租户获取、租户过滤逻辑 |

### 3. 简化前端

| 文件 | 改动要点 |
|------|----------|
| `frontend/src/services/auth.ts` | 移除 TenantInfo 接口和租户存储，login() 只传 password |
| `frontend/src/components/LoginPage.tsx` | 移除租户 ID 输入框，保留密码单输入 |
| `frontend/src/App.tsx` | tenant 状态替换为 isAuthenticated 布尔值，header 只保留退出按钮 |

### 4. 删除废弃文件

| 文件 | 原行数 |
|------|--------|
| `src/models/tenant.py` | ~125 行 |
| `src/services/tenant_registry.py` | ~437 行 |
| `src/utils/tenant_helpers.py` | ~53 行 |
| `config/tenants.yaml` | ~22 行 |

### 5. 其他清理

| 文件 | 改动 |
|------|------|
| `src/utils/password_hash.py` | 注释中 `tenants.yaml` 引用更新为 `config/auth.yaml` |

---

## 验证结果

| 验证项 | 状态 |
|--------|------|
| Python 编译（9 个核心文件 py_compile） | ✅ 全部通过 |
| TypeScript 编译（tsc --noEmit 0 errors） | ✅ 通过 |
| 前端 lint（0 diagnostics） | ✅ 通过 |
| 无残留多租户引用（grep tenant 仅 password_hash.py 注释已修正） | ✅ 清理完成 |
| 测试文件无引用（tests/ 无 tenant 相关 import） | ✅ 无影响 |

---

## 新配置格式

```yaml
# config/auth.yaml
enabled: true
password_hash: "$2b$12$..."   # bcrypt 哈希，用 python -m src.utils.password_hash 生成
jwt_secret: "change-me-in-production"
access_token_expire_hours: 2
refresh_token_expire_days: 7
```

## 代码量变化

- **删除**：~637 行（tenant.py + tenant_registry.py + tenant_helpers.py + tenants.yaml）
- **新增**：~240 行（auth_service.py + auth.yaml）
- **净减少**：~400 行，复杂度大幅降低

---

## 遗留问题

暂无。

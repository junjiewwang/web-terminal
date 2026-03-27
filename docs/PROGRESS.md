# WeTTY + MCP 智能终端服务 - 需求与实施文档

> **项目名称**: wetty-mcp-terminal
> **创建日期**: 2026-03-23
> **最后更新**: 2026-03-25
> **仓库**: [GitHub](https://github.com/junjiewwang/web-terminal)

---

## 一、需求概述

基于 WeTTY 构建「AI Agent 可控的 SSH 终端管理服务」：
- 预配置多个 SSH 主机资产
- Agent 通过 MCP 协议向远程终端发送命令、获取执行结果
- 用户可在 Web 界面同时看到 Agent 操作全过程

## 二、核心模块

| 模块 | 职责 | 技术 |
|------|------|------|
| 主机资产管理 | SSH 主机 CRUD + YAML 导入 | SQLAlchemy + SQLite |
| SSH 会话管理 | 持久连接池 + 命令执行 | asyncssh |
| MCP Server | 5 个 Agent 工具 | FastMCP Streamable HTTP |
| WeTTY 集成 | Web Terminal + 进程管理 + REST API | WeTTY + tmux |
| 前端 | Agent 面板 + 终端嵌入 | React + Tailwind CSS 4.0 |
| 事件推送 | Agent 操作实时通知 | SSE |

## 三、实施进展

### ✅ 已完成

- [x] **项目初始化**
  - 目录结构创建
  - pyproject.toml + requirements.txt 配置
  - .gitignore 配置
  - Git 仓库初始化

- [x] **数据模型层** (`src/models/`)
  - `host.py`: Host ORM 模型 + Pydantic Schema (HostCreate/HostUpdate/HostResponse)
  - `database.py`: 异步 SQLAlchemy 引擎 + 会话工厂 + 依赖注入

- [x] **核心服务层** (`src/services/`)
  - `host_manager.py`: 主机 CRUD + YAML 同步（sync_from_yaml：增/改/删 + Pydantic 校验 + 事务原子性）
  - `ssh_session.py`: SSH 会话管理（asyncssh 连接池 + 命令执行）
  - `wetty_manager.py`: WeTTY 实例管理（多主机端口分配）
  - `event_service.py`: SSE 事件总线（发布/订阅）

- [x] **REST API 层** (`src/api/`)
  - `hosts.py`: 主机 CRUD API（GET/POST/PUT/DELETE + YAML 同步）
  - `sessions.py`: 会话管理 API（创建/执行命令/关闭）
  - `events.py`: SSE 事件流端点
  - `wetty.py`: WeTTY 实例管理 API（启动/停止/列出）— **新增**

- [x] **MCP Server** (`src/mcp_server/server.py`)
  - `list_hosts`: 列出可用主机
  - `connect_host`: 连接到指定主机
  - `run_command`: 执行远程命令（含安全过滤）
  - `get_session_status`: 查询会话状态
  - `disconnect`: 断开连接
  - ✅ 通过 `init_mcp_server()` 依赖注入模式挂载到 FastAPI `/mcp` 路径

- [x] **FastAPI 入口** (`src/main.py`)
  - 生命周期管理（启动时初始化 DB + 同步 hosts.yaml）
  - hosts.yaml 文件监听热加载（watchfiles + 2s 防抖）
  - CORS 中间件
  - Bearer Token 认证中间件
  - 路由注册（含 WeTTY API）
  - MCP Server 挂载（`app.mount("/mcp", mcp.streamable_http_app())`）

- [x] **前端** (`frontend/`)
  - React + Vite + Tailwind CSS 4.0 + TypeScript
  - `App.tsx`: 三栏布局（主机列表 / 终端 / Agent 面板）
  - `HostList.tsx`: 主机列表组件
  - `TerminalView.tsx`: xterm.js + socket.io 直连终端（取代 iframe 方案）
  - `hooks/useTerminal.ts`: xterm.js 终端生命周期 Hook
  - `hooks/useWettySocket.ts`: WeTTY socket.io 连接管理 Hook
  - `AgentPanel.tsx`: Agent 操作日志面板
  - `api.ts`: API 调用 + SSE 订阅 + WeTTY API + 主机 updateHost

- [x] **Docker 部署**
  - `Dockerfile`: 多阶段构建（backend / frontend-build / nginx）
  - `docker-compose.yml`: 三服务编排（backend + frontend + wetty）
  - `nginx.conf`: 反向代理（API + SSE + MCP + WeTTY WebSocket）

- [x] **安全模块** (`src/utils/security.py`)
  - Fernet 对称加密（替代 base64）
  - API Token 生成与验证
  - 向后兼容旧 base64 格式密码

- [x] **测试** (`tests/`)
  - `test_host_manager.py`: Schema 验证测试
  - `test_ssh_session.py`: CommandResult + SessionManager 测试

### 🔲 待完成

- [x] ~~tmux 共享 Session 方案实现（Agent + WeTTY 共享终端）~~ ✅ Sprint 1 + Sprint 2 已实施
- [x] ~~前端 npm install + 构建验证~~
- [x] ~~端到端集成测试~~（浏览器端到端验证通过 ✅）
- [ ] CI/CD 配置
- [ ] PostgreSQL 支持（当前仅 SQLite）

## 四、修复记录（2026-03-23）

### P0 - 关键功能修复

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| 1 | **MCP Server 未挂载** — `server.py` 完全隔离，FastAPI 无 `/mcp` 端点 | 用 `init_mcp_server()` 依赖注入打破循环导入；`main.py` 中 `app.mount("/mcp", mcp.streamable_http_app())` | `src/mcp_server/server.py`, `src/main.py` |
| 2 | **WeTTY 集成未打通** — `WeTTYManager` 实例化但无 API 暴露；前端硬编码 `3000 + host.id` 端口 | 新建 `src/api/wetty.py` REST API；`TerminalView.tsx` 改为调用 `startWeTTY(hostId)` 动态获取 URL | `src/api/wetty.py`(新增), `src/main.py`, `frontend/src/components/TerminalView.tsx`, `frontend/src/services/api.ts` |

### P1 - 安全加固

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| 3 | **密码存储仅 base64 编码** — 不是加密 | 升级为 Fernet 对称加密，密文前缀 `fernet:`；向后兼容旧 base64 格式 | `src/utils/security.py`, `src/services/host_manager.py`, `src/services/ssh_session.py` |
| 4 | **API 无认证** — `security.py` 存在但从未集成 | 添加 Bearer Token 认证中间件；开发模式（无 `WETTY_API_TOKEN` 环境变量）自动放行 | `src/main.py`, `src/utils/security.py` |
| 5 | **MCP run_command 无安全过滤** | 添加 `_BLOCKED_COMMANDS` 正则黑名单 + `_validate_command()` 校验 | `src/mcp_server/server.py` |

### P2 - 代码清理

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| 6 | **密码解密逻辑重复** — `host_manager.py` 和 `ssh_session.py` 各自实现 | 统一到 `utils/security.py` 的 `encrypt_password()` / `decrypt_password()`；删除重复方法 | `src/utils/security.py`, `src/services/host_manager.py`, `src/services/ssh_session.py` |
| 7 | **events.py 函数内重复导入** | `import json` 和 `from dataclasses import asdict` 移至模块顶层 | `src/api/events.py` |
| 8 | **sessions.py 未用导入** | 删除 `from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession` | `src/api/sessions.py` |
| 9 | **前端未用依赖** | 移除 `@xterm/xterm` 和 `@xterm/addon-fit`（前端使用 iframe 嵌入 WeTTY） | `frontend/package.json` |
| 10 | **nginx.conf 缺少 WeTTY 反代** | 补充 `/wetty/` 路径的反向代理 + WebSocket 支持 | `nginx.conf` |

### P0 - Dockerfile 修复（2026-03-24）

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| 11 | **wetty 符号链接断裂** — Dockerfile 中 `ln -sf .../bin/wetty.js` 指向不存在的路径，新版 wetty 入口已改为 `build/main.js`，导致容器内 `wetty` 命令不可用，前端连接终端返回 503 | 将符号链接目标从 `wetty/bin/wetty.js` 改为 `wetty/build/main.js`，并确保文件有执行权限 | `Dockerfile` |

### P0 - WeTTY 终端黑屏修复（2026-03-24）

> **问题现象**: WeTTY 终端页面加载后一直黑屏，无法显示 SSH 登录界面
> **根因诊断**: 通过 socket.io WebSocket 协议分析和容器日志排查，定位出三个独立根因

| # | 问题 | 根因分析 | 修复方案 | 涉及文件 |
|---|------|----------|----------|----------|
| 12 | **SSH 算法协商失败** — socket.io 返回 `no matching host key type found. Their offer: ssh-rsa` | 容器内 Debian 13 的 OpenSSH 10.0p2 默认禁用了 `ssh-rsa` 算法，而目标堡垒机（JumpServer）只支持 `ssh-rsa` host key | Dockerfile 中创建 `/etc/ssh/ssh_config.d/legacy-compat.conf`，添加 `HostKeyAlgorithms +ssh-rsa` 和 `PubkeyAcceptedAlgorithms +ssh-rsa` | `Dockerfile` |
| 13 | **sshpass 未安装** — WeTTY 启动后报 `env: 'sshpass': No such file or directory` | WeTTY 的 `--ssh-pass` 参数内部通过调用系统 `sshpass` 命令来自动输入 SSH 密码，容器镜像中未安装此包 | Dockerfile 的 `apt-get install` 中添加 `sshpass` 包 | `Dockerfile` |
| 14 | **WeTTY 未传递密码认证参数** — WeTTY 启动命令中缺少 `--ssh-auth password --ssh-pass <密码>` | `wetty_manager.py` 的 `_WeTTYProcess.start()` 只处理了密钥认证（`private_key_path`），未处理密码认证 | 根据 `host.auth_type` 分支处理：`PASSWORD` → `--ssh-auth password --ssh-pass <解密后密码>`；密码通过 `decrypt_password()` 从加密存储解密 | `src/services/wetty_manager.py` |

### P0 - SSH 首次连接交互阻塞修复（2026-03-24）

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| (含在 #12) | **SSH 首次连接交互式确认** — 首次连接新主机时 SSH 提示 `Are you sure you want to continue connecting (yes/no)?` 导致连接阻塞 | 在 SSH 全局配置中添加 `StrictHostKeyChecking no` 和 `UserKnownHostsFile /dev/null`（已包含在 #12 的 `legacy-compat.conf` 中） | `Dockerfile` |

### P1 - 前端健壮性增强（2026-03-24）

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|----------|----------|
| 15 | **fetchHosts() 无重试机制** — React `useEffect` 只在挂载时调用一次，请求失败后无恢复手段 | ① 新增 `fetchWithRetry()` 通用工具函数（指数退避，最多 3 次，基础延迟 500ms）；② `App.tsx` 添加 `hostsLoading`/`hostsError` 状态管理 + `loadHosts()` 封装；③ `HostList.tsx` 扩展 loading/error/onRetry props，支持加载动画和错误重试 | `frontend/src/services/api.ts`, `frontend/src/App.tsx`, `frontend/src/components/HostList.tsx` |

### P0 - SSE 长连接阻塞 POST 请求修复（2026-03-24）

> **问题现象**: 点击主机后 `POST /api/wetty/start` 永远 pending，终端显示"连接中..."但永不完成
> **影响范围**: 所有需要 POST 请求的功能（startWeTTY、stopWeTTY 等）在 SSE 连接存在时全部失效

#### 根因分析（三层问题叠加）

| 层级 | 问题 | 详细说明 |
|------|------|----------|
| **L1 后端** | asyncio 事件循环阻塞 | `EventBus.subscribe()` 中的 `await queue.get()` 无超时，在无事件时无限期阻塞事件循环，其他协程（如 POST handler）无法被调度 |
| **L2 浏览器** | HTTP/1.1 TCP 连接槽位耗尽 | Chrome 对同一域名（`localhost:8000`）的 HTTP/1.1 并发 TCP 连接数有限（通常 2-6 个）。SSE streaming 响应永久占用 TCP 连接（`more_body: True` 期间连接无法复用），导致后续 POST 请求被浏览器排队到被占用的连接上 |
| **L3 架构** | uvicorn 直接暴露给浏览器 | 浏览器与 uvicorn 之间只有 HTTP/1.1 连接，SSE 长连接和 API 短请求共享连接池。即使前端 `EventSource.close()` 或 `AbortController.abort()`，底层 TCP 连接可能仍被 streaming 响应占用 |

#### 解决方案（三层修复）

| 修复 | 方案 | 涉及文件 |
|------|------|----------|
| **L1: 后端超时 yield** | `subscribe()` 改用 `await asyncio.wait_for(queue.get(), timeout=1.0)`，超时后 `continue` 释放事件循环控制权 | `src/services/event_service.py` |
| **L2: 前端 SSE 精确断开** | SSE 客户端从浏览器 `EventSource` 改为 `fetch + ReadableStream`，通过 `AbortController.abort()` 可以强制中断底层 TCP 连接（发送 RST） | `frontend/src/services/api.ts` |
| **L3: nginx 反向代理** | Docker 容器内嵌 nginx 作为前端反代层：`浏览器 → nginx(:8000) → uvicorn(:8001)`。nginx 独立管理与 uvicorn 的内部连接池，浏览器 SSE 连接只占用 nginx ↔ 浏览器 的连接，不影响 nginx ↔ uvicorn 的连接。POST 请求可以使用空闲的内部连接转发 | `Dockerfile`, `nginx.conf`, `entrypoint.sh`(新增) |
| **前端 SSE 全局单例** | SSE 管理从组件 prop 传递改为 `api.ts` 模块级 `_globalSSE` 全局单例，`startWeTTY` 内部自动暂停/恢复 SSE，组件层完全解耦 | `frontend/src/services/api.ts`, `frontend/src/App.tsx`, `frontend/src/components/TerminalView.tsx` |
| **SSE ping 调优** | `sse_starlette` 心跳从默认 15 秒改为 5 秒，更快检测断开 | `src/api/events.py` |

#### 排除的方案

| 方案 | 原因 |
|------|------|
| uvicorn `--workers 2` | 多 worker 导致 `wetty_manager`（WeTTY 子进程管理）、`event_bus`（SSE 订阅者）等全局状态跨进程不共享，引发 502 和事件丢失 |
| 仅前端暂停 SSE（无 nginx） | `EventSource.close()` 和 `AbortController.abort()` 都无法保证浏览器立即释放底层 TCP 连接。SSE streaming 响应在服务端仍然活跃（`more_body: True`），即使客户端已断开 |

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `src/services/event_service.py` | `subscribe()` 添加 1s 超时 yield |
| `src/api/events.py` | `ping=5`；添加 SSE 连接/断开日志；修复 `list[dict]` 类型注解 |
| `frontend/src/services/api.ts` | SSE 改为 `fetch + ReadableStream`；`_globalSSE` 全局单例；`startWeTTY` 暂停/恢复 SSE + `AbortController` 10s 超时 |
| `frontend/src/App.tsx` | 简化为 `subscribeEvents` 调用 |
| `frontend/src/components/TerminalView.tsx` | 移除 `sse` prop，回归简洁接口 |
| `Dockerfile` | 安装 nginx；使用 `entrypoint.sh` 启动 |
| `nginx.conf` | nginx 监听 8000 反代到 uvicorn 8001 |
| `entrypoint.sh` | 新增：同时启动 nginx 和 uvicorn |

#### 验证结论（2026-03-24）

| 验证项 | 方法 | 结果 |
|--------|------|------|
| SSE + POST 并发（httpx） | Python httpx 保持 SSE 连接同时发 POST | ✅ POST 29ms 返回 |
| 浏览器端到端 | Chrome DevTools MCP 点击主机 → startWeTTY | ✅ POST 200 成功，终端"已连接" |
| SSE 暂停/恢复 | 网络请求日志分析 | ✅ POST 前 SSE 断开，POST 后 SSE 重连 |
| 连续操作 | 切换主机多次点击 | ✅ 每次 POST 都成功返回 |
| SSE 断开日志 | 后端日志 | ✅ "SSE 客户端已连接"/"SSE 客户端已断开" 正常输出 |

### 验证结论（2026-03-24）

| 验证项 | 方法 | 结果 |
|--------|------|------|
| SSH 连接成功 | Python websockets 库直接测试 socket.io WebSocket 协议 | ✅ 收到 JumpServer 堡垒机完整欢迎界面："`<username>`, 欢迎使用JumpServer开源堡垒机系统" |
| socket.io 反代正常 | 分析 EIO4 握手 → `40` 命名空间连接 → `42["data","..."]` 数据流 | ✅ 全链路畅通 |
| 密码解密正确 | WeTTY 进程日志确认 `--ssh-auth password --ssh-pass` 参数传入 | ✅ 无报错 |
| 前端加载状态 | Playwright 截图 | ✅ 显示 "加载主机列表..." loading 状态，随后正常加载 |
| WeTTY 页面截图 | Playwright 直接访问 `/wetty/t/tce-server/` | ⚠️ 截图纯黑（xterm.js canvas 渲染不被 Playwright 截图正确捕获，不影响真实浏览器） |

### P0 - nginx WebSocket 条件升级修复（2026-03-24）

> **问题现象**: WeTTY 终端 iframe 黑屏 —— 状态栏显示"已连接"，但 iframe 内无终端交互内容（xterm.js 未初始化，`#terminal` div innerHTML 为空）
> **影响范围**: 所有通过 nginx 反代访问的 WeTTY 终端实例

#### 根因分析

| 层级 | 现象 | 原因 |
|------|------|------|
| **socket.io HTTP 轮询** | 200 OK，返回有效 `sid` + `upgrades:["websocket"]` | ✅ 正常 |
| **socket.io WebSocket 升级** | **400 Bad Request** | nginx `/wetty/` location 硬编码 `proxy_set_header Connection "upgrade"`，对所有请求（含非 WebSocket 的 HTTP 轮询）都强制发送 `Connection: upgrade` 头 |
| **xterm.js 初始化** | 未触发 | socket.io 无法完成 WebSocket 升级，终端 I/O 通道未建立 |

**核心 bug**：nginx.conf 第 94 行 `proxy_set_header Connection "upgrade"` 是硬编码的。

socket.io 协议流程为：① HTTP 轮询握手 → ② WebSocket Upgrade → ③ 双向数据流。硬编码 `Connection: upgrade` 会导致步骤 ① 的 HTTP 请求也带上升级头，与 upstream keepalive 连接复用冲突，且步骤 ② 的 WebSocket 升级也因此返回 400。

#### 修复方案

| 变更 | 说明 | 涉及文件 |
|------|------|----------|
| **添加 `map` 条件变量** | `map $http_upgrade $connection_upgrade { default upgrade; '' close; }` — 仅当客户端发送 `Upgrade` 头时才设置 `Connection: upgrade`，否则设置 `Connection: close` | `nginx.conf` |
| **替换硬编码** | `/wetty/` location 中 `proxy_set_header Connection "upgrade"` → `proxy_set_header Connection $connection_upgrade` | `nginx.conf` |

#### 验证方法

```bash
# 容器内测试 socket.io HTTP 轮询（应返回 200 + 有效 sid）
curl -s "http://127.0.0.1:8000/wetty/t/tce-server/socket.io/?EIO=4&transport=polling"

# 容器内测试 WebSocket 升级（应返回 101 Switching Protocols，而非 400）
curl -sv --no-buffer \
  -H "Upgrade: websocket" \
  -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  "http://127.0.0.1:8000/wetty/t/tce-server/socket.io/?EIO=4&transport=websocket"
```

### P0 - 前端 xterm.js 直连集成（取代 iframe 方案）（2026-03-24）

> **问题现象**: WeTTY iframe 嵌入方案存在多重问题：① iframe sandbox 属性阻塞文档加载；② SSE 长连接占用浏览器 TCP 连接槽位导致 iframe 导航请求 pending；③ WeTTY npm 2.7.0 缺少 `web_modules/` 目录导致 xterm.js 无法在浏览器初始化
> **影响范围**: 终端功能完全不可用（iframe 黑屏）
> **解决方案**: 彻底放弃 iframe，前端直接集成 xterm.js + socket.io-client，通过 socket.io 协议直连 WeTTY server

#### 架构变更

| 变更前（iframe） | 变更后（直连） |
|------------------|----------------|
| `TerminalView.tsx` 渲染 `<iframe src="/wetty/t/{host}/">` | `TerminalView.tsx` 渲染 xterm.js `<Terminal>` 实例 |
| 依赖 WeTTY 前端资源（HTML/CSS/JS/web_modules）正确构建 | 仅依赖 WeTTY server 端 socket.io 协议 |
| iframe 内 CSS 样式完全隔离，无法定制 | 终端主题与应用统一（暗色方案对齐） |
| 浏览器需要额外 TCP 连接加载 iframe 页面 | 只需一个 WebSocket 连接 |

#### socket.io 事件协议（前端 → WeTTY server）

| 方向 | 事件 | 数据 | 说明 |
|------|------|------|------|
| Client → Server | `input` | `string` | 用户键入的字符 |
| Client → Server | `resize` | `{ cols, rows }` | 终端窗口尺寸变化 |
| Server → Client | `data` | `string` | SSH 输出数据 |
| Server → Client | `login` | - | 登录成功 |
| Server → Client | `logout` | - | 登出 |

#### 新增文件

| 文件 | 职责 |
|------|------|
| `frontend/src/hooks/useTerminal.ts` | xterm.js 终端生命周期 Hook（创建/挂载/resize/销毁） |
| `frontend/src/hooks/useWettySocket.ts` | WeTTY socket.io 连接管理 Hook（connect/data/disconnect） |

#### 变更文件

| 文件 | 变更 |
|------|------|
| `frontend/package.json` | 新增 `@xterm/xterm` ^5.2.0、`@xterm/addon-fit` ^0.10.0、`@xterm/addon-web-links` ^0.11.0、`socket.io-client` ^4.5.1 |
| `frontend/src/components/TerminalView.tsx` | 完全重写：从 iframe 改为 useTerminal + useWettySocket Hook 组合 |
| `frontend/src/services/api.ts` | 更新 startWeTTY 注释（不再需要 SSE 暂停来服务 iframe） |
| `frontend/vite.config.ts` | WeTTY 代理目标从 `localhost:3000` 改为 `localhost:8000`（走 nginx） |

#### 设计决策

| 评估方案 | 决定 | 理由 |
|----------|------|------|
| 方案 A: 修复 iframe（构建 web_modules） | ❌ 放弃 | WeTTY npm 2.7.0 构建系统不稳定（Snowpack→esbuild 迁移不完整），且 iframe 天然存在 sandbox/连接管理问题 |
| **方案 B: 前端直接集成 xterm.js** | ✅ 采用 | 彻底消除 iframe 问题，终端完全可控，与 Agent 面板可深度集成 |
| 方案 C: 全自建 Python PTY Server | ❌ 放弃 | 工作量巨大，Python PTY 功能有限 |

### P0 - xterm.js 终端未挂载修复（2026-03-24）

> **问题现象**: 选择主机后，状态栏显示 "已连接 (socket.io ✓)"，但终端区域是纯黑空白，无任何终端内容
> **影响范围**: 终端功能完全不可用（xterm.js Terminal 实例未创建，DOM 容器为空）

#### 根因分析

| 层级 | 现象 | 原因 |
|------|------|------|
| **React ref 时序** | `useTerminal` hook 的 `useEffect` 在组件首次 mount 时执行，此时 `containerRef.current` 为 `null`（因为 `!host` 条件渲染返回了空状态 UI，不包含终端容器 div） | `useEffect` 依赖 `[containerRef]`，而 `containerRef` 是 `useRef` 创建的对象，其引用在整个生命周期中不变 |
| **条件渲染导致 effect 错过** | 当 `host` 从 `null` 变为有值时，组件重新渲染，终端容器 div 终于出现在 DOM 中 | 但 `useEffect` 的依赖 `containerRef`（对象引用）没有变化，effect 不会重新执行 |
| **Terminal.open() 永不调用** | `term.open(container)` 是在 effect 内部调用的，effect 不重新执行意味着 Terminal 永远不会挂载到 DOM | 容器 div 存在但 `children.length === 0` |

**核心问题**：`useRef` 创建的 ref 对象引用是稳定的，作为 `useEffect` 依赖时无法感知 `.current` 属性的变化。当容器 DOM 因条件渲染延迟出现时，effect 无法被重新触发。

#### 修复方案

| 变更 | 说明 | 涉及文件 |
|------|------|----------|
| **Callback ref 模式** | `useTerminal` 不再接收外部 `containerRef` 参数，改为内部创建 callback ref（`useState` + `useCallback`）并通过返回值暴露。React 在 DOM 挂载时自动调用 callback ref，触发 `setContainer(node)` → state 变化 → `useEffect` 重新执行 → `term.open(container)` | `frontend/src/hooks/useTerminal.ts` |
| **API 变更** | `useTerminal(containerRef, options)` → `useTerminal(options)`；返回值新增 `containerRef` 字段；调用方改为 `<div ref={terminal.containerRef}>` | `frontend/src/hooks/useTerminal.ts`, `frontend/src/components/TerminalView.tsx` |
| **移除 TerminalView 的 useRef** | 不再需要手动创建 `termContainerRef = useRef(null)`，由 hook 内部管理 | `frontend/src/components/TerminalView.tsx` |

#### 技术要点：为什么 callback ref 解决了问题

```
// ❌ 旧方案：useRef + useEffect（条件渲染下 effect 不会重新触发）
const containerRef = useRef<HTMLDivElement>(null);
useEffect(() => {
  if (!containerRef.current) return; // 首次 mount 时 host=null，容器不在 DOM 中
  term.open(containerRef.current);   // 永远不会执行
}, [containerRef]);                  // containerRef 引用不变，effect 不重新运行

// ✅ 新方案：callback ref + useState（DOM 挂载时自动触发）
const [container, setContainer] = useState<HTMLDivElement | null>(null);
const containerRef = useCallback((node: HTMLDivElement | null) => {
  setContainer(node);               // DOM 挂载时 React 调用 → state 变化
}, []);
useEffect(() => {
  if (!container) return;
  term.open(container);             // container state 变化触发 effect
}, [container]);                    // container 是新的 DOM 节点引用
```

### P1 - 终端 UI 美化：右侧白条 + 滚动条（2026-03-24）

> **问题现象**: ① 终端右侧有明显的白色竖条；② 内容超出时出现浏览器原生滚动条，在暗色终端上非常突兀

#### 根因

| 问题 | 原因 |
|------|------|
| 右侧白色竖条 | FitAddon 按字符网格计算 cols，终端实际渲染宽度 = cols × charWidth < 容器宽度，右侧缝隙露出 `.xterm-viewport` 默认背景色 |
| 滚动条丑 | xterm.js 的 `.xterm-viewport` 使用浏览器原生 `overflow-y: scroll`，原生滚动条在暗色背景上灰白色很突兀 |

#### 修复方案

| 修复 | 说明 | 涉及文件 |
|------|------|----------|
| 背景色统一 | `.xterm`、`.xterm-viewport`、`.xterm-screen` 全部覆盖为 `#0a0a0a !important`，与容器 div 完全一致 | `frontend/src/index.css` |
| 滚动条深色极细化 | Chromium 用 `::-webkit-scrollbar` 设置 6px 宽、半透明白色 thumb；Firefox 用 `scrollbar-width: thin` + `scrollbar-color` | `frontend/src/index.css` |
| 容器 overflow-hidden | 终端容器 div 添加 `overflow-hidden`，滚动行为完全由 xterm.js 内部管理 | `frontend/src/components/TerminalView.tsx` |
| body overflow-hidden | SPA 单屏布局不需要页面级滚动条 | `frontend/src/index.css` |

## 五、端到端验证（2026-03-24）

### 浏览器端到端验证结果

> **环境**: Chrome DevTools MCP → localhost:8000（Docker 容器内 nginx → uvicorn → WeTTY）
> **目标主机**: tce-server (`<user>@<bastion-ip>`:36000 → JumpServer 堡垒机)

| # | 验证项 | 方法 | 结果 |
|---|--------|------|------|
| 1 | **页面加载** | 强制刷新（ignoreCache） | ✅ 零错误，主机列表正确显示 |
| 2 | **WeTTY 启动** | 点击 tce-server → POST /api/wetty/start | ✅ 200 OK |
| 3 | **SSE 暂停/恢复** | 网络请求时序分析 | ✅ POST 前 SSE 断开，POST 后自动重连 |
| 4 | **socket.io 连接** | HTTP polling 握手 + 数据传输 | ✅ sid 分配成功，数据双向流通 |
| 5 | **xterm.js 挂载** | DOM 检查 `.xterm-screen` + `.xterm-rows` | ✅ 76 行 DOM 节点，viewport 正常 |
| 6 | **SSH 数据渲染** | 读取终端行内容 | ✅ JumpServer 完整欢迎界面 + 操作菜单 |
| 7 | **键盘交互** | 发送 `p` + `Enter` 命令 | ✅ 返回 2781 台主机列表（68条/页，41页） |
| 8 | **终端自适应** | 截图验证 | ✅ 终端填满中间区域，表格对齐正确 |
| 9 | **状态栏** | UI 快照 | ✅ "Terminal: tce-server (socket.io ✓) ● 已连接" |

### 已知瞬态问题

| 问题 | 影响 | 原因 | 处理 |
|------|------|------|------|
| 首次 socket.io 请求 502 | 无影响 | WeTTY 进程刚启动尚未就绪，socket.io 自动重连后成功 | socket.io 内置重连机制覆盖，无需修复 |
| socket.io 仅 HTTP polling 无 WebSocket | 性能未最优 | WebSocket 升级未发生（可能被代理层阻断或 socket.io 判断无需升级） | 优化项，当前 polling 传输正常 |

### 截图证据

- `terminal-e2e-verify.png`: 终端连接成功，JumpServer 欢迎界面
- `terminal-interactive-verify.png`: 键盘交互，主机列表表格渲染

## 六、hosts.yaml 热加载同步（2026-03-24）

### 需求背景

hosts.yaml 作为 Single Source of Truth（唯一真相），DB 是运行时缓存。需实现完整的增/改/删同步，而非仅导入。

### 设计方案

| 项目 | 方案 |
|------|------|
| **同步逻辑** | YAML → Pydantic 校验 → 与 DB 对比 → 增/改/删 |
| **校验策略** | 校验先行：任何一条格式错误则整批拒绝，不做任何变更 |
| **事务原子性** | 所有变更在一个 DB 事务中完成 |
| **触发方式** | ① 启动时自动同步 ② watchfiles 文件监听（2s 防抖）③ `POST /api/hosts/sync` 手动触发 |
| **安全保障** | YAML 内部 name 唯一性检查 + Pydantic 字段约束校验 |

### 变更文件

| 文件 | 变更内容 |
|------|----------|
| `src/services/host_manager.py` | 新增 `SyncResult` 数据类 + `sync_from_yaml()` 方法（替代 `import_from_yaml()`），实现增/改/删完整同步 |
| `src/main.py` | lifespan 中改用 `sync_from_yaml`；新增 `_watch_hosts_yaml()` 后台任务（watchfiles + 2s 防抖） |
| `src/api/hosts.py` | `POST /api/hosts/sync` 替代 `POST /api/hosts/import`，返回 `SyncResult` 格式 |
| `requirements.txt` | 新增 `watchfiles>=1.0.0` 依赖 |
| `config/hosts.yaml` | 更新头部注释，反映新的同步机制 |

### P0 - MCP Server 404/500/421 三连修复（2026-03-24）

> **问题现象**: CodeBuddy 连接 MCP Server 报 `SSE error: Non-200 status code (404)`
> **影响范围**: MCP 协议完全不可用，Agent 无法通过 MCP 控制终端

#### 根因分析（三层问题叠加）

| # | 问题 | 状态码 | 根因 |
|---|------|--------|------|
| 1 | **路径双重前缀** | 404 | `app.mount("/mcp", mcp.streamable_http_app())` 会剥离 `/mcp` 前缀后转发给子应用，而 FastMCP 默认 `streamable_http_path="/mcp"`，子应用内部路由也是 `/mcp`。外部请求 `/mcp/` → 子应用收到 `/` → 不匹配 `/mcp` → 404 |
| 2 | **Session Manager 未启动** | 500 | `app.mount()` 方式挂载子应用时，子应用的 lifespan 不会被 FastAPI 触发。FastMCP 的 `StreamableHTTPSessionManager.run()` 需要在 lifespan 中启动 task group，未启动导致 `RuntimeError: Task group is not initialized` |
| 3 | **nginx Host 头不含端口号** | 421 | nginx `proxy_set_header Host $host` 中 `$host` 不包含端口号（如 `localhost`），而 MCP transport security 的 `allowed_hosts = ["localhost:*"]` 用 `fnmatch` 匹配，`localhost:*` 不匹配纯 `localhost`（需要冒号后至少一个字符） |

#### 修复方案

| 修复 | 方案 | 涉及文件 |
|------|------|----------|
| **路径修复** | `FastMCP(streamable_http_path="/")` — 子应用路由改为 `/`，配合 `mount("/mcp")` 后外部 `/mcp/` → 子应用 `/` → ✅ 匹配 | `src/mcp_server/server.py` |
| **Session Manager 启动** | 在主 FastAPI lifespan 中 `async with mcp.session_manager.run():` 手动启动 task group | `src/main.py` |
| **Host 头修复** | nginx `/mcp/` location 中 `proxy_set_header Host $http_host`（含端口号），替代 `$host`（不含端口号） | `nginx.conf` |

#### 验证

| 验证项 | 方法 | 结果 |
|--------|------|------|
| uvicorn 直连 | `POST http://127.0.0.1:8001/mcp/` MCP initialize | ✅ 200 OK |
| nginx 代理 | `POST http://127.0.0.1:8000/mcp/` MCP initialize | ✅ 200 OK |
| 宿主机访问 | `curl -X POST http://localhost:8000/mcp/` | ✅ 200 OK，返回 `mcp-session-id` |

### P0 - MCP PTY 交互式模式（取代 exec 直连模式）（2026-03-24）

> **问题现象**: MCP 的 SSH exec 模式（`asyncssh conn.run(command)`）无法与堡垒机交互。JumpServer 要求 PTY 分配，exec 模式返回 "No PTY requested"
> **影响范围**: MCP 完全无法通过堡垒机连接目标主机，核心使用场景不可用
> **解决方案**: 完全重构 MCP 架构 — 从 SSH exec 模式改为 PTY 交互式模式，通过 WeTTY socket.io 共享终端

#### 架构变更

| 变更前（exec 模式） | 变更后（PTY 交互式模式） |
|---------------------|--------------------------|
| `asyncssh conn.run(command)` 每条命令独立 exec channel | WeTTY socket.io PTY 共享终端 |
| 无法分配 PTY，堡垒机拒绝 | 通过 WeTTY 的 SSH PTY 连接，支持堡垒机交互 |
| Agent 操作不可见 | Agent 操作在浏览器终端实时回显 |
| 5 个 MCP 工具 | 7 个 MCP 工具（新增 send_input / wait_for_output / read_terminal） |
| 依赖 `SSHSessionManager` | 依赖 `WeTTYManager` + `PTYSessionManager` |

#### 架构图

```
Agent (MCP) → PTYSession (socket.io client) → WeTTY (Node.js) → SSH PTY → 堡垒机 → 目标主机
浏览器      → socket.io client (xterm.js)  → 同一个 WeTTY 实例 ↗
```

MCP 和浏览器通过同一个 WeTTY 实例的 socket.io 连接共享 SSH PTY，Agent 的 `input` 事件写入 PTY，PTY 的 `data` 事件广播给所有客户端（包括浏览器）。

#### MCP 工具清单（7 个）

| 工具 | 功能 | 模式 |
|------|------|------|
| `list_hosts` | 列出可用主机 | 查询（无变更） |
| `connect_host` | 连接主机（启动 WeTTY + PTY session） | **重写** |
| `run_command` | 执行命令（PTY send + wait prompt） | **重写** |
| `send_input` | 向终端发送任意输入（堡垒机菜单等） | **新增** |
| `wait_for_output` | 等待终端输出匹配（expect 风格） | **新增** |
| `read_terminal` | 读取终端屏幕缓冲区 | **新增** |
| `get_session_status` | 查询会话状态 | **重写** |
| `disconnect` | 断开 PTY 连接 | **重写** |

#### 新增文件

| 文件 | 职责 |
|------|------|
| `src/services/pty_session.py` | PTY 交互式会话核心服务：`PTYSession`（socket.io 客户端、输出缓冲区、expect 模式匹配）、`PTYSessionManager`（会话生命周期管理）、`strip_ansi()`（ANSI 转义序列清洗） |

#### 变更文件

| 文件 | 变更 |
|------|------|
| `src/mcp_server/server.py` | 完全重写：从 `SSHSessionManager` 依赖改为 `WeTTYManager` + `PTYSessionManager`；7 个工具全部基于 PTY 实现 |
| `src/main.py` | `init_mcp_server(ssh_manager)` → `init_mcp_server(wetty_manager)`；lifespan 关闭时添加 PTY 会话清理 |
| `requirements.txt` | 新增 `python-socketio[asyncio_client]>=5.11.0` |

#### 关键技术要点

| 问题 | 解决方案 |
|------|----------|
| exec 模式无 PTY，堡垒机拒绝 | 通过 WeTTY 的 SSH PTY 连接，天然支持 PTY |
| Agent 操作如何回显到浏览器 | socket.io 多客户端连接同一 WeTTY 实例，PTY 输出广播给所有客户端 |
| 如何在 PTY 中等待命令完成 | `wait_for()` expect 风格模式匹配：正则扫描终端缓冲区 + asyncio.Event 唤醒 |
| ANSI 转义序列干扰输出解析 | `strip_ansi()` 综合正则清洗 CSI/OSC/颜色/光标序列 |
| `send_command` 快速命令输出被跳过 | 在 `send_input()` 前记录缓冲区位置 `pre_pos`，传给 `wait_for(_start_pos=pre_pos)` |
| WeTTY PTY 回车键格式 | 使用 `\r`（CR）而非 `\n`（LF），匹配终端行规程 |
| `wait_for` 提示符匹配 | `re.MULTILINE` 标志使 `$` 匹配每行末尾，适配各种 shell 提示符 |

#### 端到端验证（2026-03-24）

> **环境**: Python httpx MCP 客户端 → localhost:8000 → Docker 容器（nginx → uvicorn → WeTTY）
> **目标**: tce-server (JumpServer 堡垒机) → `<target-ip>` (目标主机)

| # | 步骤 | 工具 | 结果 |
|---|------|------|------|
| 1 | MCP 协议握手 | initialize | ✅ 200 OK，获取 mcp-session-id |
| 2 | 连接堡垒机 | `connect_host("tce-server")` | ✅ PTY 交互式模式连接成功 |
| 3 | 读取终端 | `read_terminal()` | ✅ JumpServer 完整欢迎界面 + 操作菜单 |
| 4 | 堡垒机跳转 | `send_input("<target-ip>\r")` | ✅ 输入 IP 跳转目标主机 |
| 5 | 等待登录 | `wait_for_output("Last login")` | ✅ 匹配到 `Last login: Tue Mar 24...` |
| 6 | 执行命令 | `run_command("uptime")` | ✅ `43 days up, load 5.30` |
| 7 | 执行命令 | `run_command("free -h")` | ✅ `62G total, 32G used` |
| 8 | 执行命令 | `run_command("df -h /")` | ✅ `197G, 15G used (8%)` |
| 9 | 断开连接 | `disconnect()` | ✅ PTY 连接正确关闭 |

## 七、UI/UX 问题修复（2026-03-24）

### P1 - tmux 多客户端终端点号显示修复

> **问题现象**: 浏览器终端下方显示多行点号（`·····`），影响用户体验
> **根因**: tmux 未配置 `window-size`，当 Agent PTY（80×30）和浏览器终端（~76×72）尺寸不同时，tmux 默认使用最小客户端的尺寸，多余区域用点号填充

#### 修复方案

| 修复 | 说明 | 涉及文件 |
|------|------|----------|
| **tmux window-size largest** | 在 `tmux-session.sh` 中添加 `tmux set-option -g window-size largest`，使 tmux 始终使用最大客户端（浏览器）的窗口尺寸 | `scripts/tmux-session.sh` |

### P1 - tmux-256color terminfo 兼容性修复

> **问题现象**: 通过 WeTTY → tmux → SSH → 堡垒机 → 目标主机后，执行 `top` 报错 `'tmux-256color': unknown terminal type.`
> **根因**: tmux 启动后会将 `$TERM` 从 `xterm-256color` 改为 `tmux-256color`（tmux 默认行为），该值通过 SSH 传播到目标主机。旧版 CentOS/RHEL 的 terminfo 数据库中没有 `tmux-256color` 条目，导致 `top`/`htop`/`vim` 等依赖 terminfo 的程序报错

#### 排查过程

| 步骤 | 发现 |
|------|------|
| 首次修复尝试 | `tmux-session.sh` 中添加 `tmux set-option -g default-terminal "xterm-256color" 2>/dev/null \|\| true` |
| 修复无效 | 重建容器后 `top` 仍然报错，目标主机 `$TERM` 仍为 `tmux-256color` |
| 根因定位 | `tmux set-option -g` 需要 tmux 服务器已运行；脚本执行时序为先 `set-option`（此时无 tmux 服务器 → 命令失败被 `\|\| true` 吞掉）后 `new-session`（此时才启动服务器），配置**从未真正生效过** |
| 验证 | 容器内手动执行 `tmux set-option -g default-terminal "xterm-256color"` → 返回 `no server running on /tmp/tmux-0/default` |

#### 修复方案（双层防御）

| 修复 | 说明 | 涉及文件 |
|------|------|----------|
| **Dockerfile 内置 .tmux.conf** | 在 Dockerfile 系统包安装层中 `printf '...' > /root/.tmux.conf`，tmux 启动服务器（`new-session`）时自动加载 | `Dockerfile` |
| **tmux-session.sh 防御性写入** | 脚本中 `cat > ~/.tmux.conf` 确保即使 Dockerfile 层被跳过也能生效 | `scripts/tmux-session.sh` |

#### 验证（2026-03-25）

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 容器内 .tmux.conf | `cat /root/.tmux.conf` | ✅ 含 `default-terminal "xterm-256color"` |
| tmux 运行时配置 | `tmux show-options -g default-terminal` | ✅ `xterm-256color` |
| 目标主机 $TERM | `echo $TERM` on target host | ✅ `xterm-256color` |
| top 命令 | `top -bn1 \| head -5` | ✅ 正常输出 load average 等信息 |

### P1 - Agent 操作日志面板无数据修复

> **问题现象**: 右侧 Agent 操作日志面板始终显示"等待 Agent 操作..."，即使 Agent 已执行过操作
> **根因**: 三层问题叠加

| # | 问题 | 根因 |
|---|------|------|
| 1 | **SSE 重连退避不精确** | `reconnectDelay` 在发起连接时就重置为 1s，而非收到第一条数据后重置，导致快速失败场景下无退避效果 |
| 2 | **SSE pause/resume 丢事件** | `startWeTTY()` 调用 `_globalSSE.pause()`（即 `disconnect`）和 `resume()`（即 `connect`），但 `paused` 状态未区分，重连条件不精确 |
| 3 | **页面加载无历史事件** | SSE 只推送连接后的新事件，页面刷新后面板为空，后端 `GET /api/events/history` 存在但前端从未调用 |
| 4 | **SSE ping 未处理** | `sse_starlette` 的心跳（`: ping`）是 SSE 注释行，`_readSSEStream` 未正确处理，可能导致解析状态异常 |

#### 修复方案

| 修复 | 说明 | 涉及文件 |
|------|------|----------|
| **精确退避重置** | `reconnectDelay` 改为在 `_readSSEStream` 内收到第一条数据时通过 `onFirstData` 回调重置 | `frontend/src/services/api.ts` |
| **pause/resume 状态分离** | 新增 `paused` 标志位，`pause()` 设为 true + 断开连接，`resume()` 设为 false + 重置退避 + 重连；自动重连只在非 `paused` 且非 `destroyed` 时触发 | `frontend/src/services/api.ts` |
| **历史事件加载** | `App.tsx` 初始化时调用 `fetchEventHistory()` 从后端拉取最近 100 条历史事件，与实时 SSE 事件去重合并 | `frontend/src/services/api.ts`, `frontend/src/App.tsx` |
| **SSE 注释行处理** | `_readSSEStream` 添加 `: ` 开头行（SSE 注释/ping）的识别和跳过逻辑 | `frontend/src/services/api.ts` |

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `scripts/tmux-session.sh` | 添加 `tmux set-option -g window-size largest` 全局配置 |
| `frontend/src/services/api.ts` | SSE 订阅重构：`fetchEventHistory()`、`paused` 状态分离、`onFirstData` 回调、SSE 注释行处理 |
| `frontend/src/App.tsx` | 新增 `fetchEventHistory` 导入 + `useEffect` 中加载历史事件 + 去重合并 |

### P1 - 终端 resize 自适应修复

> **问题现象**: 页面 resize（窗口缩放、侧边栏折叠/展开、面板拖拽等）后，终端区域和布局不会自动适应新尺寸
> **根因**: 三层问题叠加

| # | 问题 | 根因 |
|---|------|------|
| 1 | **仅监听 window.resize** | `useTerminal.ts` 只用 `window.addEventListener("resize")` 监听窗口变化，无法感知容器 DOM 自身的尺寸变化（侧边栏折叠、面板拖拽、CSS transition 等） |
| 2 | **flexbox 最小高度 auto** | `App.tsx` 中间终端区域 `<div className="flex-1">` 缺少 `min-h-0`，flexbox 子元素默认 `min-height: auto` 导致收缩时高度溢出 |
| 3 | **fit() 无防抖** | `fitAddon.fit()` 直接调用无节流，拖拽期间高频调用导致 DOM 重排和终端闪烁 |

#### 修复方案

| 修复 | 说明 | 涉及文件 |
|------|------|----------|
| **ResizeObserver 容器监听** | 替代 `window.addEventListener("resize")`，使用 `ResizeObserver` 监听容器 DOM 元素尺寸变化，能感知所有容器大小改变场景；保留 `window.resize` 作为兜底 | `frontend/src/hooks/useTerminal.ts` |
| **requestAnimationFrame debounce** | `fitAddon.fit()` 通过 `requestAnimationFrame` + 标志位实现防抖，每个渲染帧最多执行一次 fit，避免高频 DOM 重排 | `frontend/src/hooks/useTerminal.ts` |
| **flexbox min-h-0 修复** | `<div className="flex-1">` → `<div className="flex-1 min-h-0">`，允许 flexbox 子元素正确收缩 | `frontend/src/App.tsx` |

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `frontend/src/hooks/useTerminal.ts` | `window.addEventListener("resize")` → `ResizeObserver` + `requestAnimationFrame` debounce；清理函数中 `cancelAnimationFrame` + `resizeObserver.disconnect()` |
| `frontend/src/App.tsx` | 终端区域容器 div 添加 `min-h-0` class |

## 八、Jump Host 前端点击修复（2026-03-25）

### P0 - 点击 jump_host 创建独立 WeTTY 而非复用堡垒机

> **问题现象**: 前端点击 m12（jump_host 类型）时，看到的是堡垒机原始登录界面，而不是自动跳转到目标主机的终端
> **影响范围**: 所有 jump_host 类型主机通过浏览器点击连接的功能完全不可用

#### 根因分析

| 路径 | 行为 | 结果 |
|------|------|------|
| **MCP 路径**（正确） | `connect_host("m12")` → 检测 jump_host → 启动堡垒机 WeTTY → 创建 tmux 窗口 → JumpOrchestrator 自动编排 | ✅ 自动连接到目标主机 |
| **REST API 路径**（错误） | `POST /api/wetty/start {host_id: m12.id}` → `manager.start_instance(m12)` — 直接用 m12 的 Host 对象启动 WeTTY | ❌ 由于 m12 继承了堡垒机的连接信息(hostname/port/username)，创建了一个独立的 WeTTY 进程连接堡垒机，没有 tmux 窗口管理和跳板编排 |

**核心 bug**：`src/api/wetty.py` 的 `start_wetty()` 函数没有 host_type 感知——无论 direct/bastion/jump_host 都走同一条 `manager.start_instance(host)` 路径。

#### 修复方案（方案 A：后端感知 jump_host）

在 `POST /api/wetty/start` 中增加 jump_host 感知逻辑，对齐 MCP `_connect_jump_host()` 的流程：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 检测 `host.host_type == JUMP_HOST` | 分流 jump_host 和 direct/bastion |
| 2 | 查找父堡垒机 | `host_mgr.get_host_by_id(host.parent_id)` |
| 3 | 启动/复用堡垒机 WeTTY | `manager.start_instance(bastion)` |
| 4 | 创建 tmux 窗口 | `tmux_mgr.create_window()` + `select_window()` |
| 5 | 后台跳板编排 | `asyncio.create_task(_run_jump_orchestration(...))` — 不阻塞响应 |
| 6 | 返回堡垒机 URL + bastion_name | 前端据此连接正确的 socket.io 路径 |

前端 `TerminalView.tsx` 的 `connectToHost()` 根据 API 返回的 `bastion_name` 字段自动选择正确的 socket.io basePath：
- `bastion_name` 存在 → jump_host，basePath = `/wetty/t/{bastion_name}`
- `bastion_name` 为空 → direct/bastion，basePath = `instance.url`

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `src/api/wetty.py` | 重构 `start_wetty()`：增加 `_start_jump_host()` 和 `_start_direct_host()` 分流；新增 `_run_jump_orchestration()` 后台任务；`WeTTYInstanceResponse` 增加 `bastion_name` 字段；新增 `tmux_manager` 全局引用 |
| `src/main.py` | 注入 `wetty.tmux_manager = tmux_manager_instance` |
| `frontend/src/services/api.ts` | `WeTTYInstance` 类型增加 `bastion_name` 可选字段 |
| `frontend/src/components/TerminalView.tsx` | `connectToHost()` 根据 `instance.bastion_name` 选择 basePath；`disconnectFromHost()` jump_host 不调用 `stopWeTTY` |

#### 设计决策

| 决策 | 理由 |
|------|------|
| 后台任务而非阻塞编排 | 跳板编排涉及 PTY 连接、wait_for、send_input 等耗时操作（10-30s），阻塞 API 响应会导致前端超时。后台执行让前端先建立 socket.io 连接看到终端输出，编排过程在浏览器实时可见 |
| `bastion_name` 放在 API 响应中 | 避免前端需要额外知道 host_type 和父堡垒机映射关系，后端作为唯一真相源 |
| 不需要前端传 `bastionName` prop | 后端 API 返回已包含 bastion_name，`TerminalView` 自给自足，不依赖外部传参 |
| 复用 `PTYSessionManager` 但创建独立实例 | 后台编排的 PTY 会话是临时的（编排完即关闭），与 MCP 的 PTY 会话管理器互不干扰 |

#### 联调测试修复：tmux session 延迟创建（2026-03-25）

> **问题现象**：`POST /api/wetty/start {host_id: 4}` 返回 500 错误
> `tmux 窗口创建失败 (wetty-tce-server:m12)` → `error connecting to /tmp/tmux-0/default (No such file or directory)`

**根因**：tmux session 由 WeTTY 的 `--command tmux-session.sh` 延迟创建。WeTTY 进程启动后，tmux session **并不立即存在**——只有当第一个 socket.io 客户端（浏览器或 PTY）连接到 WeTTY 时，WeTTY 才会执行 `tmux-session.sh`，由脚本中的 `tmux new-session` 创建 tmux session。

原 `_start_jump_host()` 在 API 层面直接调用 `create_window()`，此时没有任何 socket.io 客户端连接过 WeTTY，tmux session 不存在，导致失败。

**修复**：将 tmux 窗口创建从 `_start_jump_host()` 移到后台任务 `_run_jump_orchestration()` 中，在 PTY 连接成功（已触发 tmux session 创建）之后再创建窗口。

| 修复前 | 修复后 |
|--------|--------|
| `_start_jump_host()`: 启动 WeTTY → `create_window()` ❌ → 后台 PTY 连接 | `_start_jump_host()`: 启动 WeTTY → 后台任务 |
| tmux session 不存在，API 返回 500 | `_run_jump_orchestration()`: PTY 连接 ✅（触发 tmux session 创建）→ `create_window()` ✅ → 跳板编排 |

**验证结果**：
- ✅ `POST /api/wetty/start {host_id: 4}` 返回 200 + `bastion_name: "tce-server"`
- ✅ 后台 PTY 连接成功 → tmux 窗口创建成功 → 跳板编排正常启动
- ✅ 直连堡垒机 `{host_id: 3}` 无回归，返回 `bastion_name: null`
- ⚠️ 跳板编排超时（`ready_pattern` 未匹配）— 已知遗留问题，新 tmux 窗口中需要先建立 SSH 到堡垒机

### P0 - 跳板编排新窗口自动 SSH 连接修复（2026-03-25）

> **问题现象**：`_run_jump_orchestration()` 后台任务中，`tmux new-window` 创建的窗口是空 shell（本地 bash），而非堡垒机 SSH 会话。`JumpOrchestrator.execute_jump()` 的 `_wait_for_ready()` 等待 `ready_pattern`（如 `[Host]>`）超时
> **根因**：`tmux new-window -t session -n window` 默认启动容器的 `/bin/bash`，不会自动 SSH 到堡垒机

#### 修复方案（方案 C：create_window 支持 command 参数）

扩展 `TmuxWindowManager.create_window()` 接受可选 `command` 参数，`tmux new-window` 时将 SSH 命令作为窗口的启动命令，窗口创建即自动 SSH 到堡垒机。

| 变更 | 说明 | 涉及文件 |
|------|------|----------|
| **SSH 命令构造抽取** | 新增 `src/utils/ssh_command.py`，提供 `build_ssh_command()` 和 `build_ssh_command_for_host()` 公共函数，遵循 DRY 原则，与 `tmux-session.sh` 中的 `build_ssh_command()` 格式一致 | `src/utils/ssh_command.py`（新增） |
| **create_window 扩展** | `TmuxWindowManager.create_window()` 新增 `command: Optional[str]` 参数，有值时追加到 `tmux new-window` 命令参数 | `src/services/tmux_manager.py` |
| **WeTTYManager 暴露 SSH 命令** | `_WeTTYProcess` 新增 `ssh_command` 属性（复用 `build_ssh_command()`）；`WeTTYManager` 新增 `get_ssh_command()` 方法 | `src/services/wetty_manager.py` |
| **后台编排传递 SSH 命令** | `_start_jump_host()` 获取堡垒机 SSH 命令传给后台任务；`_run_jump_orchestration()` 传递给 `create_window(command=ssh_cmd)` | `src/api/wetty.py` |
| **跳过重复窗口创建** | `JumpOrchestrator.execute_jump()` 新增 `skip_window_creation` 参数，窗口已由调用方创建时跳过内部的 `_create_tmux_window()` | `src/services/jump_orchestrator.py` |

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `src/utils/ssh_command.py` | **新增**：公共 SSH 命令构造函数（DRY 抽取） |
| `src/services/tmux_manager.py` | `create_window()` 新增 `command` 参数 |
| `src/services/wetty_manager.py` | 新增 `_WeTTYProcess.ssh_command` 属性 + `WeTTYManager.get_ssh_command()` |
| `src/api/wetty.py` | `_run_jump_orchestration()` 传递 `bastion_ssh_cmd` 给 `create_window()` + `execute_jump(skip_window_creation=True)` |
| `src/services/jump_orchestrator.py` | `execute_jump()` 新增 `skip_window_creation` 参数 |

#### 更新后的后台编排流程

```
_run_jump_orchestration():
  1. PTY 连接 WeTTY（触发 tmux session 创建）
  2. tmux_mgr.create_window(session, window, command="sshpass -p '...' ssh -o ... -p 36000 user@host")
     → 新窗口创建后自动 SSH 到堡垒机，显示堡垒机欢迎界面
  3. tmux_mgr.select_window()
  4. orchestrator.execute_jump(skip_window_creation=True)
     → _wait_for_ready("[Host]>|Opt>") ✅ 匹配成功
     → send_input(target_ip)
     → login_steps（如有）
  5. 清理 PTY
```

#### 验证结果

| 验证项 | 方法 | 结果 |
|--------|------|------|
| m12 jump_host（无 login_steps） | `POST /api/wetty/start {host_id: 4}` | ✅ API 200 + 跳板编排成功（`ready_pattern` 匹配） |
| 直连堡垒机（回归测试） | `POST /api/wetty/start {host_id: 3}` | ✅ `bastion_name: null`，无回归 |
| tmux 窗口 SSH 命令 | 容器日志 `command=sshpass -p '...' ssh -o ...` | ✅ SSH 命令正确传入了 tmux new-window |
| skip_window_creation | 容器日志 `skip_create=True` | ✅ 跳过了 JumpOrchestrator 内部的重复窗口创建 |

### P0 - 多 Tab 独立终端视图（2026-03-25）

> **问题现象**：点击 tce-server 后再点击 m12，浏览器终端被后台编排的 `tmux select-window` 全局切换到 m12 窗口。切换回 tce-server Tab 时显示的仍是 m12 的终端内容，而非堡垒机原始终端。所有 Tab 共享一个 TerminalView 实例，切换时断开旧连接、建立新连接。
> **根因**：1) `tmux select-window` 全局切换所有 client 视图；2) `App.tsx` 只渲染一个 `<TerminalView host={activeHost} />`，切换 Tab 时销毁/重建

#### 修复方案（多 TerminalView + tmux switch-client -c）

**第一层（前端）**：每个 Tab 维护独立的 TerminalView 实例
- 所有 Tab 的 TerminalView 同时渲染，通过 `display:none` 控制显隐
- 切换 Tab 时非活跃的 TerminalView 保持连接不销毁
- 每个实例有独立的 socket.io 连接 + xterm.js 终端

**第二层（后端）**：用 `tmux switch-client -c <tty>` 替代 `tmux select-window`
- `select-window` → 全局切换，所有 attached client 受影响（已废弃）
- `switch-client -c <client_tty>` → 只切换指定 client 的视图
- 每个 socket.io 连接 = 独立的 tmux client（WeTTY `tmux attach-session` 机制）
- 前端连接成功后通过 `GET /api/tmux/client-ttys/{bastion}` 获取 client TTY
- Tab 切换时调用 `POST /api/tmux/switch-window`（带 `client_tty` 参数）

#### 变更文件清单

| 文件 | 变更 |
|------|------|
| `src/services/tmux_manager.py` | 新增 `list_clients()` + `switch_client()` 方法；新增 `TmuxClient` 数据类 |
| `src/api/tmux.py` | `switch-window` 端点支持 `client_tty` 参数；新增 `GET /api/tmux/client-ttys/{bastion_name}` 端点 |
| `src/api/wetty.py` | `_run_jump_orchestration()` 中 `select_window` 改为 `switch_client`（per-client）；**保持后台 PTY 会话运行，不再关闭** |
| `frontend/src/services/api.ts` | 新增 `TmuxClient` 接口；`fetchClientTtys()` 返回类型更新 |
| `frontend/src/components/TerminalTabs.tsx` | `TerminalTab` 接口新增 `clientTty` 字段 |
| `frontend/src/components/TerminalView.tsx` | 重写：接收 `isActive` prop（非活跃时隐藏不断连）；`onClientTtyReady` 回调上报 client TTY |
| `frontend/src/App.tsx` | 重写：多 TerminalView 实例（绝对定位层叠，按 activeTabId 显隐）；Tab 切换使用 per-client switch |

#### 关键修复（2026-03-25）

**问题**：Tab 切换时报错 `can't find client: /dev/pts/3`，因为后台跳板编排的 PTY session 在编排完成后被关闭，其对应的 tmux client TTY 消失，但前端获取到了这个即将消失的 TTY。

**解决方案**：
1. **保持后台 PTY 会话运行**：不再在 `_run_jump_orchestration()` 结束时调用 `pty_mgr.close_session()`。PTY 会话将保持运行直到用户关闭 Tab 或 WeTTY 进程终止。
2. **增加 client TTY 获取延迟**：前端延迟 1 秒获取 client 列表，确保后台 PTY 稳定后再识别自己的 client。

#### 验证结果

| 验证项 | 结果 |
|--------|------|
| 点击 tce-server → 显示堡垒机终端 | ✅ `Terminal: tce-server (socket.io ✓) ● 已连接` |
| 点击 m12 → 打开新 Tab + 显示 m12 终端 | ✅ `🔗 tce-server/m12` Tab 出现 |
| 切换回 tce-server Tab → 显示堡垒机原始终端 | ✅ header 显示 `junjiewwang@...:36000`，不是 m12 |
| 切换回 m12 Tab → 显示 m12 终端 | ✅ header 显示 `tce-server → m12 (10.202.16.3)` |
| tce-server Tab 连接未断开 | ✅ 两个 Tab 都保持 `已连接` |
| 后台编排 per-client switch | ✅ 日志 `tmux switch-client`（不影响浏览器 client） |
| 跳板编排成功 | ✅ `跳板编排成功: tce-server → m12` |
| Tab 切换 API 调用 | ✅ 所有 `POST /api/tmux/switch-window` 返回 200 |
| 控制台无切换错误 | ✅ 无 `tmux 窗口切换失败` 警告 |

## 九、遗留问题

1. ~~**tmux 集成**：WeTTY + tmux 共享 Session 方案~~ → 已通过 tmux 会话共享实现（Sprint 1 + Sprint 2）
2. **WeTTY 安装**：Docker 环境使用 `wettyoss/wetty` 镜像，本地开发需要 `npm install -g wetty`
3. **PostgreSQL 支持**：当前仅 SQLite，生产环境切换 PostgreSQL 需调整 DATABASE_URL
4. **加密密钥管理**：当前 `WETTY_ENCRYPTION_KEY` 通过环境变量设置，生产环境建议接入 KMS
5. **MCP 认证**：当前 `/mcp/` 路径免认证（MCP 协议自身管理），需评估是否加入独立认证机制
6. **socket.io WebSocket 升级**：当前仅 HTTP polling 传输，WebSocket 升级可进一步降低延迟
7. **MCP 客户端会话缓存**：Docker 容器重建后 CodeBuddy MCP 客户端持有旧 session-id 导致 404，需手动触发重连
8. ~~**tmux 多客户端点号显示**~~ → 已通过 `window-size largest` 修复
9. ~~**Agent 操作日志面板无数据**~~ → 已通过 SSE 重连修复 + 历史事件加载修复
10. **jump_host 后台编排错误处理**：当前 `_run_jump_orchestration()` 是 fire-and-forget 后台任务，编排失败仅记录日志，前端无直接通知。可考虑通过 SSE 事件推送编排进度/错误
11. **jump_host Tab 关闭清理**：关闭 jump_host Tab 时应关闭对应的 tmux 窗口（当前仅跳过 stopWeTTY）
12. ~~**jump_host 新窗口缺少 SSH 连接**~~ → 已通过方案 C 修复：`create_window(command=ssh_cmd)` 新窗口创建时自动 SSH 到堡垒机，`JumpOrchestrator` 的 `ready_pattern` 能正确匹配
13. ~~**decrypt_password 类型错误**~~ → 已修复：`wetty_manager.py` 中添加 `password_encrypted` 的 None 检查后再调用 `decrypt_password()`
14. ~~**WeTTY 冷启动 502 瞬态问题**~~ → 已通过方案 E 修复：前端 socket.io 静默重试，冷启动期间前 3 次连接失败不显示错误，保持 "connecting" 状态让 socket.io 自动重连
15. ~~**MCP jump_host 连接**~~ → 已修复：更新 `_connect_jump_host` 为独立 WeTTY 实例架构，与 REST API 保持一致

## 十、开发里程碑

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | hosts.yaml + asyncssh + MCP Server 5 个工具 | ✅ 已完成 |
| Phase 2 | FastAPI REST API + SQLite + WeTTY 集成 | ✅ 已完成 |
| Phase 3 | 前端 Agent 面板 + SSE 事件推送 | ✅ 已完成 |
| Phase 4 | 安全加固（认证、命令过滤、密钥加密） | ✅ 已完成 |
| Phase 5 | Docker Compose 一键部署 | ✅ 已完成 |
| Phase 6 | MCP PTY 交互式模式（堡垒机 + 浏览器回显） | ✅ 已完成 |
| Phase 7 | Jump Host 浏览器直连（REST API jump_host 感知） | ✅ 已完成 |
| Phase 7.1 | 多 Tab 独立终端视图（per-client tmux switch-client） | ✅ 已完成 |
| Phase 7.2 | 独立 WeTTY 实例架构（每个 jump_host Tab 独立实例） | ✅ 已完成 |
| Phase 7.3 | Sprint 3 健壮性（并发测试 + 会话超时） | 🔲 进行中 |
| Phase 8 | CI/CD + 生产环境配置 | 🔲 待实施 |

## 十一、Sprint 3 健壮性进展（Phase 7.3）

### ✅ 已完成

- [x] **P0: 并发测试用例**（`tests/test_concurrency.py`）
  - 端口分配并发安全测试
  - 多主机并发启动测试
  - Agent 先连 → 浏览器后连验证
  - 多 PTY 会话并发测试

- [x] **P0: MCP jump_host 架构更新**（独立 WeTTY 实例架构）
  - 修复 MCP `_connect_jump_host` 使用旧架构（复用堡垒机 WeTTY + tmux 新窗口）问题
  - 改用 `start_instance_for_jump_host` 创建独立 WeTTY 实例
  - 设置 `skip_window_creation=True` 跳过 tmux 窗口创建
  - MCP 连接 m12 成功，资源使用情况正常获取

- [x] **P0: 浏览器 tce-server 与 m12 同步问题修复**
  - **问题现象**：切换到 m12 Tab 时，后端日志显示 `wetty-tce-server:m12 - can't find window: m12`
  - **根因**：前端 `createTabForHost` 手动设置 `bastionName = "tce-server"`（不包含 "--"），但 API 返回的 `bastion_name` 是 `tce-server--m12`（包含 "--"）
  - **修复（第一轮）**：在 `TerminalView` 检测到独立 WeTTY 实例时，通过 `onBastionNameUpdate` 回调更新 Tab 的 `bastionName`
  - **修复（第二轮）**：`createTabForHost` 和 `handleHostSelect` 不再为 jump_host 设置 `bastionName` 和 `tmuxWindow`，等待 API 返回后由 `onBastionNameUpdate` 更新，避免 Tab 切换时使用旧值
  - **涉及文件**：`frontend/src/components/TerminalView.tsx`、`frontend/src/App.tsx`、`frontend/src/components/TerminalTabs.tsx`
  - **验证状态**：✅ 浏览器端到端测试通过（Tab 创建 + 多次切换无错误）

- [x] **P1: 移除断开按钮，优化 Tab 关闭逻辑**
  - **问题现象**：状态栏的"断开终端"按钮点击后会自动重连，功能与 Tab 关闭按钮重复
  - **根因**：TerminalView 组件始终挂载（Tab 未关闭），断开后 `useEffect` 立即触发重连
  - **修复方案**：
    1. 移除状态栏的"✕ 断开终端"按钮
    2. 修复 `handleTabClose`：正确关闭 jump_host 的独立 WeTTY 实例（使用 `bastionName`）
    3. 修复后端 API：返回正确的 `bastion_name`（独立实例名如 `"tce-server--m12"`）
    4. 修复后端 API 复用实例时也返回 `bastion_name`
    5. 修复前端 Tab 显示：从 `bastionName` 提取堡垒机名作为前缀（`split("--")[0]`）
    6. 修复前端 header 显示：同样从 `bastionName` 提取堡垒机名
    7. **修复核心问题**：WeTTY 关闭时清理 tmux session，避免下次启动时复用旧 session
  - **涉及文件**：`frontend/src/components/TerminalView.tsx`、`frontend/src/App.tsx`、`frontend/src/components/TerminalTabs.tsx`、`src/api/wetty.py`、`src/services/wetty_manager.py`
  - **验证状态**：✅ 前端构建成功，待容器重启后验证

### ✅ 已修复（本轮）

- [x] **P0: tce-server 和 m12 终端同步 Bug — tmux session 残留 + 跳板编排重复执行**

  **问题现象**：
  - Agent 通过 MCP 连接 m12 后，用户在浏览器打开页面
  - m12 Tab 显示正常（已登录到 m12 主机）
  - 切换到 tce-server Tab 时，看到的不是堡垒机菜单，而是 m12 的内容（看起来同步了）
  - 之后重新连接 tce-server 和 m12，两者内容完全同步、不再独立

  **根因分析（通过日志和 tmux 状态确认）**：

  **Bug 1: tmux session 残留 — `stop_instance` 清理 tmux session 存在竞态/遗漏**

  日志时间线（12:10:36-12:10:40）：
  ```
  12:10:17 - stop tce-server → tmux session 清理: wetty-tce-server ✓
  12:10:18 - stop tce-server--m12 → WeTTY 实例已停止（但 tmux session 清理日志缺失！）
  12:10:36 - 启动 tce-server--m12 (port 3010) → tmux session: wetty-tce-server--m12
  12:10:37 - 启动 tce-server (port 3011) → tmux session: wetty-tce-server
  ```

  **关键问题**：`stop_instance` 停止 `tce-server--m12` 时，`_cleanup_tmux_session` 可能没有成功清理
  tmux session `wetty-tce-server--m12`。新 WeTTY 实例 (port 3010) 启动后，第一个 socket.io
  连接（浏览器或后台编排 PTY）触发 `tmux-session.sh`，发现 `wetty-tce-server--m12` session
  **已存在（残留的）**，执行 `tmux attach-session` 而非 `tmux new-session`。
  这导致新连接 attach 到**上一次的 SSH 会话（已在 m12 内）**，而不是创建新的 SSH 到堡垒机。

  **Bug 2: 后台编排 PTY 的 `buffer=59 lines` + 跳板编排重复执行**

  ```
  12:10:38 - PTY 终端就绪: c8d57c3e (0.0s, buffer=59 lines)  ← 已有历史输出！
  12:10:38 - 开始跳板编排: tce-server → m12 (steps=0, skip_create=True)
  12:10:39 - 已发送目标 IP: 10.202.16.3  ← 在已登录 m12 的终端里再次发送 IP！
  ```

  PTY 连接后立刻发现 buffer 有 59 行数据（来自残留 session 中已有的 m12 输出），
  然后 `execute_jump` 仍然执行跳板编排（发送 target_ip），在已经登录 m12 的 shell 里
  执行了 `10.202.16.3` 命令（被当作 shell 命令执行），导致终端混乱。

  **Bug 3: tce-server WeTTY 也 attach 到残留的 tmux session**

  `wetty-tce-server` (port 3011) 的 tmux session 如果在前一轮停止时没有被正确清理，
  新的 tce-server WeTTY 启动后同样会 `tmux attach` 到旧 session。
  如果旧 session 中的 SSH 曾经跳转到 m12，那 tce-server Tab 显示的就是 m12 的内容。

  **Bug 4: WeTTY `_cleanup_tmux_session` 使用同步 `subprocess.run` 可能不可靠**

  `_WeTTYProcess.stop()` 中：
  1. 先 `terminate()` WeTTY 进程
  2. 再调用 `_cleanup_tmux_session()` 用 `subprocess.run` 同步执行 `tmux kill-session`

  问题：
  - WeTTY 进程 `terminate()` 后，其管理的 tmux client 可能还没有完全断开
  - `subprocess.run` 是同步阻塞调用，在 asyncio 事件循环中可能引发竞态
  - 如果 tmux session 名中包含特殊字符 `--`，`tmux has-session -t` 可能匹配不精确

  **Bug 5: 端口单调递增不回收 — 容器生命周期内端口耗尽风险**

  端口从 3000 开始分配，每次 stop + start 都分配新端口（3000→3001→...→3011），
  `_port_counter` 只增不减。长时间运行或频繁重连后端口会超过合理范围。
  同时 nginx 反代的 upstream 端口映射需要动态更新，旧端口残留的 upstream 会导致 502。

  **Bug 6: `tmux list-clients` 返回空 — 所有 tmux session 无 client attached**

  当前状态确认：
  ```
  wetty-tce-server: attached=0
  wetty-tce-server--m12: attached=0
  ```
  两个 tmux session 都没有任何 client attached！但 WeTTY 进程 (port 3010/3011) 还在运行。
  说明浏览器的 socket.io 连接断开后（如页面刷新或 Tab 关闭），WeTTY 执行的 `tmux attach`
  shell 进程退出，tmux client 也随之断开。再次连接时，WeTTY 对新 socket.io 连接执行
  `tmux-session.sh`，又会 `tmux attach`。
  但在无 client 期间，tmux session 和其中的 SSH 连接仍然保持运行（zombie session 风险）。

  **修复实施（已完成）**：

  1. ✅ **tmux session 清理加固**（`src/services/wetty_manager.py`）：
     - `_cleanup_tmux_session` 改为 async（`asyncio.create_subprocess_exec`）
     - `tmux kill-session` 后二次 `has-session` 验证确认清理成功
     - `stop()` 顺序调换：先清理 tmux session，再终止 WeTTY 进程
     - 使用精确匹配 `-t '=SESSION_NAME'` 避免子串匹配
     - **关键新增**：`start()` 启动前先 `_cleanup_tmux_session()` 清理残留

  2. ✅ **跳板编排防重入**（`src/mcp_server/server.py` + `src/api/wetty.py`）：
     - `_connect_jump_host`：复用实例时通过 `is_session_logged_in()` 检测已登录状态，跳过编排
     - `_run_jump_orchestration`：后台编排也添加防重入，检测 pane_current_command + shell 提示符

  3. ✅ **端口回收机制**（`src/services/wetty_manager.py`）：
     - `stop_instance` 后将端口归还 `_recycled_ports` 列表
     - `_allocate_port` 优先复用回收端口

  4. ✅ **tmux 精确匹配**（`scripts/tmux-session.sh` + `src/services/tmux_manager.py`）：
     - `tmux-session.sh` 的 `has-session` 和 `attach-session` 使用 `=` 前缀精确匹配
     - `TmuxWindowManager.session_exists()` 也改用精确匹配
     - 新增 `is_session_logged_in()` 方法检测 pane 的 current_command

  5. ✅ **zombie session 定期清理**（`src/main.py` + `src/services/wetty_manager.py`）：
     - `WeTTYManager.cleanup_zombie_sessions()` 扫描所有 wetty- 前缀 session
     - 清理无 client attached 且无对应活跃 WeTTY 实例的 zombie session
     - `main.py` 注册 60s 间隔的后台清理任务

  **涉及文件**：
  - `src/services/wetty_manager.py` — tmux 清理加固 + 端口回收 + zombie 清理
  - `src/mcp_server/server.py` — 跳板编排防重入
  - `src/api/wetty.py` — 跳板编排防重入
  - `scripts/tmux-session.sh` — tmux 精确匹配
  - `src/services/tmux_manager.py` — session_exists 精确匹配 + is_session_logged_in
  - `src/main.py` — zombie 清理后台任务
  - **验证状态**：✅ Docker 构建成功，服务启动正常，zombie 清理任务已启动

- [x] **P1: tmux scrollback 体验优化** — 部分实施
  - **最终方案**：回滚 `mouse on`（副作用太大），保留 `history-limit 5000` + CSS 滚动条改善
  - **修改**：
    1. `scripts/tmux-session.sh` — `history-limit 5000`（Ctrl+B [ 进 copy mode 翻看更多历史）
    2. `frontend/src/index.css` — 滚动条宽度 6→8px，透明度 0.08→0.2 / hover 0.15→0.35
  - **mouse on 回滚原因**：
    - vim/top 等全屏程序内滚轮失效（tmux 拦截滚轮进入 copy mode 而非传递给 vim）
    - 浏览器文本选择需要 Shift+鼠标，对用户不友好
  - **当前体验**：
    - 鼠标滚轮在命令行模式下 → 上下键（查历史命令）——tmux Alternate Screen 的默认行为
    - vim/top/less 内滚轮 → 正常传递给应用
    - 文本选择 → 正常拖选
    - 翻看历史输出 → `Ctrl+B [` 进入 copy mode，按 `q` 退出

### ⏸ 已评估 — 暂不实施

- [ ] **P2: WeTTY 进程健康监控** — 暂缓
  - 当前稳定性足够（22h+ 运行无异常），等实际出现问题再做

- [ ] **P3: tmux 状态栏定制** — 不做
  - 前端 header + 状态栏已提供足够信息，属于过度设计

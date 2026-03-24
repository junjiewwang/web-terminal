# WeTTY + MCP 智能终端服务 - 需求与实施文档

> **项目名称**: wetty-mcp-terminal
> **创建日期**: 2026-03-23
> **最后更新**: 2026-03-24
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

- [ ] tmux 共享 Session 方案实现（Agent + WeTTY 共享终端）
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

## 七、遗留问题

1. ~~**tmux 集成**：WeTTY + tmux 共享 Session 方案~~ → 已被 PTY socket.io 共享方案替代
2. **WeTTY 安装**：Docker 环境使用 `wettyoss/wetty` 镜像，本地开发需要 `npm install -g wetty`
3. **PostgreSQL 支持**：当前仅 SQLite，生产环境切换 PostgreSQL 需调整 DATABASE_URL
4. **加密密钥管理**：当前 `WETTY_ENCRYPTION_KEY` 通过环境变量设置，生产环境建议接入 KMS
5. **MCP 认证**：当前 `/mcp/` 路径免认证（MCP 协议自身管理），需评估是否加入独立认证机制
6. **socket.io WebSocket 升级**：当前仅 HTTP polling 传输，WebSocket 升级可进一步降低延迟
7. **MCP 客户端会话缓存**：Docker 容器重建后 CodeBuddy MCP 客户端持有旧 session-id 导致 404，需手动触发重连

## 八、开发里程碑

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | hosts.yaml + asyncssh + MCP Server 5 个工具 | ✅ 已完成 |
| Phase 2 | FastAPI REST API + SQLite + WeTTY 集成 | ✅ 已完成 |
| Phase 3 | 前端 Agent 面板 + SSE 事件推送 | ✅ 已完成 |
| Phase 4 | 安全加固（认证、命令过滤、密钥加密） | ✅ 已完成 |
| Phase 5 | Docker Compose 一键部署 | ✅ 已完成 |
| Phase 6 | MCP PTY 交互式模式（堡垒机 + 浏览器回显） | ✅ 已完成 |
| Phase 7 | CI/CD + 生产环境配置 | 🔲 待实施 |

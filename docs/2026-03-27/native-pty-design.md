# 去掉 WeTTY — 自研 Python PTY + WebSocket 终端层

## 一、背景

当前架构使用 WeTTY（Node.js）作为终端中间层，引入了大量不必要的复杂度：
- 每个主机连接启动独立的 Node.js 进程（~50MB 内存/个）
- 端口分配/回收管理
- nginx 反代 HTTP + WebSocket 双向桥接
- socket.io 协议层（前端 + Agent PTY 都通过 socket.io 连 WeTTY）
- 冷启动延迟 0.5-1.5s（Node.js 进程启动）
- Docker 镜像包含 Node.js runtime（+200MB）

而 WeTTY 实际做的事情只有一件：**PTY fd ↔ WebSocket 桥接**。这可以用 ~200 行 Python 代码替代。

## 二、目标架构

```
浏览器 xterm.js → WebSocket → FastAPI → PTY fd → tmux → SSH → 远端主机
Agent           →              FastAPI → PTY fd（进程内共享）
```

去掉 WeTTY(Node.js)、socket.io、wetty_proxy、独立端口分配。

## 三、Sprint 计划

### Sprint 1: 后端核心 — Python PTY Manager + WebSocket 端点

**新建文件：**
- `src/services/terminal_manager.py` — 替代 `wetty_manager.py`
  - `TerminalSession`: 管理单个 PTY（`pty.fork()` + asyncio fd 读写 + tmux-session.sh）
  - `TerminalManager`: 管理所有终端会话的生命周期
  - 多客户端广播：一个 PTY fd 的输出广播给所有订阅的 WebSocket 连接
  - tmux session 清理逻辑（从 wetty_manager 迁移）

- `src/api/terminal.py` — 替代 `wetty.py` + `wetty_proxy.py`
  - `POST /api/terminal/start` — 创建终端会话（替代 `POST /api/wetty/start`）
  - `POST /api/terminal/stop/{session_id}` — 停止终端会话
  - `GET /api/terminal` — 列出所有会话
  - `WebSocket /ws/terminal/{session_id}` — xterm.js 直连（替代 socket.io）

**WebSocket 协议（JSON 消息）：**
```
Client → Server:
  {"type": "input", "data": "ls\r"}          // 用户键入
  {"type": "resize", "cols": 80, "rows": 24}  // 终端尺寸
Server → Client:
  {"type": "output", "data": "..."}           // PTY 输出
  {"type": "ready"}                            // 终端就绪
  {"type": "closed", "reason": "..."}          // 终端关闭
```

**改造文件：**
- `src/services/pty_session.py` — Agent PTY 改为直接读写 PTY fd（不再通过 socket.io）
  - `PTYSession` 保持 `send_input/wait_for/read_screen` 接口不变
  - 底层从 `socketio.AsyncClient` 改为直接操作 `TerminalSession` 的共享缓冲区
- `src/mcp_server/server.py` — 连接逻辑适配
- `src/main.py` — 注入新 Manager、注册新路由

**删除文件：**
- `src/services/wetty_manager.py`
- `src/api/wetty.py`
- `src/api/wetty_proxy.py`

### Sprint 2: 前端改造 — socket.io → 原生 WebSocket

**新建文件：**
- `frontend/src/hooks/useWebSocket.ts` — 替代 `useWettySocket.ts`
  - 原生 `new WebSocket(url)` 连接
  - JSON 消息解析（`output`/`ready`/`closed`）
  - 自动重连 + 冷启动静默重试
  - `sendInput(data)` / `sendResize(cols, rows)` / `disconnect()`

**改造文件：**
- `frontend/src/components/TerminalView.tsx`
  - `connectToHost` → 调用 `POST /api/terminal/start` 获取 `session_id`
  - `useWebSocket({ url: `/ws/terminal/${sessionId}` })`
  - 去掉 basePath/bastionName 逻辑（WebSocket URL 直接用 session_id）
- `frontend/src/services/api.ts`
  - `startTerminal` / `stopTerminal` 替代 `startWeTTY` / `stopWeTTY`
  - 类型定义更新
- `frontend/src/App.tsx` — 适配新 API
- `frontend/src/components/TerminalTabs.tsx` — 简化 bastionName 逻辑

**删除文件：**
- `frontend/src/hooks/useWettySocket.ts`

**依赖变化：**
- 移除 `socket.io-client`（package.json）

### Sprint 3: Docker 瘦身 + nginx 简化

**Dockerfile 改造：**
- 删除 Stage 1a（`FROM node:22-slim AS wetty-build`）
- 删除第 88-94 行（node/wetty 二进制拷贝和软链接）
- 镜像从 ~800MB 降到 ~600MB

**nginx.conf 改造：**
- `/wetty/` location → `/ws/` location（WebSocket 升级代理）
- 保留 `$connection_upgrade` map（新 WebSocket 也需要）

**requirements.txt 改造：**
- 移除 `python-socketio[asyncio_client]`
- 移除 `httpx`（仅 wetty_proxy 使用）
- 移除 `websockets`（仅 wetty_proxy 使用）

**vite.config.ts 改造：**
- `/wetty` 代理规则 → `/ws` 代理规则

**entrypoint.sh 改造：**
- 移除 WeTTY 相关注释

### Sprint 4: 清理 + 测试

- 移除所有 WeTTY 相关的遗留代码和注释
- 更新 README.md
- 更新 PROGRESS.md
- 端到端测试：Agent MCP 连接 + 浏览器连接 + Tab 切换 + 关闭清理

## 四、关键设计决策

### 4.1 保留 tmux

继续用 tmux 实现浏览器和 Agent 共享 SSH PTY。
- Python PTY `pty.fork()` 执行 `tmux-session.sh`（同现在）
- 浏览器 WebSocket → `tmux attach`
- Agent PTY → 直接读写 tmux 管理的 fd
- `tmux-session.sh` 无需修改

### 4.2 PTY fd 读写 + asyncio 集成

```python
import pty, os, asyncio

pid, fd = pty.fork()
if pid == 0:
    # 子进程：exec tmux-session.sh
    os.execvp("bash", ["bash", "/app/scripts/tmux-session.sh", ...])
else:
    # 父进程：asyncio 读写 fd
    loop = asyncio.get_event_loop()
    loop.add_reader(fd, on_pty_output, fd)
    # 写入：os.write(fd, data.encode())
```

### 4.3 多客户端广播

```python
class TerminalSession:
    _subscribers: list[WebSocket]  # 所有连接的浏览器 WebSocket
    _agent_buffer: deque[str]      # Agent 的输出缓冲区（共享）

    def _on_pty_output(self, data: bytes):
        text = data.decode(errors="replace")
        # 广播给所有浏览器
        for ws in self._subscribers:
            asyncio.create_task(ws.send_json({"type": "output", "data": text}))
        # 同时追加到 Agent 缓冲区
        self._agent_buffer.append(text)
        self._output_event.set()
```

### 4.4 WebSocket 路径设计

```
/ws/terminal/{session_id}   ← 浏览器 xterm.js 连接
```

`session_id` 在 `POST /api/terminal/start` 时返回，前端用它来构造 WebSocket URL。

## 五、文件变更清单

| 操作 | 文件 |
|------|------|
| 新建 | `src/services/terminal_manager.py` |
| 新建 | `src/api/terminal.py` |
| 新建 | `frontend/src/hooks/useWebSocket.ts` |
| 重写 | `src/services/pty_session.py` |
| 重写 | `src/mcp_server/server.py`（连接逻辑） |
| 改造 | `src/main.py` |
| 改造 | `frontend/src/components/TerminalView.tsx` |
| 改造 | `frontend/src/services/api.ts` |
| 改造 | `frontend/src/App.tsx` |
| 改造 | `frontend/src/components/TerminalTabs.tsx` |
| 改造 | `Dockerfile` |
| 改造 | `nginx.conf` |
| 改造 | `requirements.txt` |
| 改造 | `frontend/package.json` |
| 改造 | `frontend/vite.config.ts` |
| 改造 | `entrypoint.sh` |
| 删除 | `src/services/wetty_manager.py` |
| 删除 | `src/api/wetty.py` |
| 删除 | `src/api/wetty_proxy.py` |
| 删除 | `frontend/src/hooks/useWettySocket.ts` |
| 保留 | `src/services/tmux_manager.py` |
| 保留 | `src/services/jump_orchestrator.py` |
| 保留 | `scripts/tmux-session.sh` |
| 保留 | `frontend/src/hooks/useTerminal.ts` |

## 六、风险和回退

- **风险**：PTY + asyncio 集成可能有边界情况（UTF-8 半字符、大量输出时背压）
- **回退**：保留 `wetty_manager.py` 的 git 历史，如果自研出问题可以快速 revert
- **验证**：每个 Sprint 完成后做端到端测试再进入下一个 Sprint

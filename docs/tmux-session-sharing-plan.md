# 方案 A：tmux 会话共享 — 详细实施计划

> **状态**: 📋 待实施
> **创建日期**: 2026-03-24
> **关联需求**: MCP Agent 操作浏览器已连接终端 Session，用户可在浏览器实时观看 Agent 操作

---

## 一、目标与动机

### 当前问题

当前架构下，浏览器和 MCP Agent 各自建立独立的 socket.io 连接到 WeTTY，WeTTY 为每个 socket.io 连接 fork 一个独立的 SSH 子进程：

```
浏览器 (xterm.js) ──socket.io 连接A──→ WeTTY ──SSH PTY-A──→ 堡垒机 (会话X)
Agent (PTYSession) ──socket.io 连接B──→ WeTTY ──SSH PTY-B──→ 堡垒机 (会话Y)
```

**结果**：Agent 和浏览器操作的是两个完全独立的 SSH 会话，用户无法在浏览器终端中看到 Agent 的操作。

### 目标架构

通过 tmux 作为会话复用层，浏览器和 Agent 共享同一个 SSH PTY：

```
浏览器 ──socket.io──→ WeTTY ──→ tmux attach -t wetty-{host} ──→ 共享 PTY ──→ SSH ──→ 堡垒机
Agent  ──socket.io──→ WeTTY ──→ tmux attach -t wetty-{host} ──→ 共享 PTY ↗
```

### 期望效果

1. ✅ 浏览器终端实时显示 Agent 的所有命令输入和输出
2. ✅ 用户可以在浏览器终端和 Agent 操作之间无缝切换
3. ✅ 断开浏览器后 SSH 会话不丢失（tmux 守护）
4. ✅ Agent 操作日志面板同步展示操作记录（SSE 事件）

---

## 二、技术方案

### 2.1 WeTTY 启动参数变更

**核心变更**：WeTTY 不再直接 SSH 连接目标主机，而是通过 `--command` 参数启动 tmux 会话。

| 变更前 | 变更后 |
|--------|--------|
| `wetty --ssh-host <host> --ssh-user <user> --ssh-pass <pass>` | `wetty --command "tmux-session.sh {host_name} {ssh_host} {ssh_port} {ssh_user} {ssh_pass}"` |

**tmux 会话命名规则**：`wetty-{host_name}`（如 `wetty-tce-server`）

### 2.2 tmux 会话管理脚本

新增 `scripts/tmux-session.sh`，负责 tmux 会话的创建/attach：

```bash
#!/bin/bash
# tmux-session.sh — WeTTY 调用的 tmux 会话入口
#
# 逻辑：
#   1. 如果 tmux 会话已存在 → tmux attach
#   2. 如果 tmux 会话不存在 → tmux new-session + 在会话内执行 sshpass ssh

SESSION_NAME="$1"
SSH_HOST="$2"
SSH_PORT="$3"
SSH_USER="$4"
SSH_PASS="$5"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    # 会话已存在，直接 attach（多个 WeTTY 连接共享同一个 tmux 会话）
    exec tmux attach-session -t "$SESSION_NAME"
else
    # 会话不存在，创建新会话并在其中建立 SSH 连接
    if [ -n "$SSH_PASS" ]; then
        exec tmux new-session -s "$SESSION_NAME" \
            "sshpass -p '$SSH_PASS' ssh -o StrictHostKeyChecking=no -p $SSH_PORT $SSH_USER@$SSH_HOST"
    else
        exec tmux new-session -s "$SESSION_NAME" \
            "ssh -o StrictHostKeyChecking=no -p $SSH_PORT $SSH_USER@$SSH_HOST"
    fi
fi
```

### 2.3 连接时序（浏览器先连 → Agent 后连）

```
时间轴：
  t0: 用户点击主机 → POST /api/wetty/start → WeTTYManager.start_instance()
  t1: WeTTY 启动 → --command "tmux-session.sh wetty-tce-server ..."
  t2: tmux 创建会话 wetty-tce-server，内部执行 ssh → 连接到堡垒机
  t3: 浏览器 socket.io 连接 WeTTY → 看到 tmux 会话内容（堡垒机欢迎界面）
  ...
  t4: Agent 调用 connect_host("tce-server") → MCP Server 启动第二个 socket.io 连接
  t5: WeTTY 为新连接再次执行 tmux-session.sh → tmux has-session → attach
  t6: Agent 的 socket.io 连接也进入同一个 tmux 会话
  t7: Agent send_input("<target-ip>\r") → 浏览器终端同步显示输入
```

### 2.4 MCP PTYSession 适配

`PTYSession` 类的核心逻辑（`send_input` / `wait_for` / `read_screen`）基于 socket.io 事件，**无需大改**。因为：

- Agent 的 socket.io 连接 attach 到 tmux 会话后，WeTTY 仍然通过 `data` 事件广播终端输出
- Agent 的 `input` 事件仍然通过 WeTTY 写入到（tmux 内的）PTY

**需要调整的部分**：

| 组件 | 调整内容 |
|------|----------|
| `PTYSession.connect()` | 连接后等待 tmux 就绪（等待终端内容出现），替代当前固定的 `sleep(2.0)` |
| `PTYSessionManager.create_session()` | 新增参数标记是 "new" 还是 "attach" 模式 |
| `mcp_server/server.py` 的 `connect_host` | 检测 WeTTY 实例是否已运行，如已运行则直接 attach（无需等待 SSH 登录） |

---

## 三、文件变更清单

### 3.1 新增文件

| 文件 | 职责 |
|------|------|
| `scripts/tmux-session.sh` | tmux 会话创建/attach 入口脚本 |

### 3.2 变更文件

| 文件 | 变更内容 | 风险 |
|------|----------|------|
| `src/services/wetty_manager.py` | `_WeTTYProcess.start()` 启动参数从 `--ssh-host/--ssh-user/--ssh-pass` 改为 `--command "scripts/tmux-session.sh ..."` | 中 — 核心路径变更 |
| `src/services/pty_session.py` | `PTYSession.connect()` 增加 tmux attach 就绪检测 | 低 |
| `src/mcp_server/server.py` | `connect_host` 增加 attach 模式判断 | 低 |
| `Dockerfile` | `apt-get install` 添加 `tmux`；COPY `scripts/` 目录 | 低 |
| `entrypoint.sh` | 确保 tmux server 在容器启动时可用 | 低 |

### 3.3 无需变更的文件

| 文件 | 原因 |
|------|------|
| `frontend/src/hooks/useWettySocket.ts` | 浏览器 socket.io 连接方式不变 |
| `frontend/src/hooks/useTerminal.ts` | xterm.js 渲染逻辑不变 |
| `frontend/src/components/TerminalView.tsx` | 终端 UI 组件不变 |
| `src/api/wetty.py` | WeTTY REST API 接口不变 |
| `src/api/wetty_proxy.py` | 反向代理逻辑不变 |
| `nginx.conf` | nginx 配置不变 |

---

## 四、Sprint 拆解

### Sprint 1：tmux 基础集成（核心路径）

**目标**：WeTTY 通过 tmux 连接 SSH，浏览器正常使用

| # | 任务 | 验收标准 |
|---|------|----------|
| 1.1 | 编写 `scripts/tmux-session.sh` | 脚本可独立执行：创建 tmux 会话 + SSH 连接 |
| 1.2 | 修改 `wetty_manager.py` 启动参数 | WeTTY 使用 `--command` 参数启动 |
| 1.3 | 修改 `Dockerfile` 安装 tmux | 容器内 `tmux -V` 正常输出 |
| 1.4 | 构建部署 + 浏览器验证 | 浏览器点击主机 → 终端正常显示堡垒机界面 |

**回滚方案**：恢复 `wetty_manager.py` 的 `--ssh-host` 参数即可

### Sprint 2：MCP 会话共享

**目标**：MCP Agent 通过 tmux 与浏览器共享终端

| # | 任务 | 验收标准 |
|---|------|----------|
| 2.1 | 修改 `connect_host` 逻辑 | 检测已有 WeTTY 实例 → attach 模式 |
| 2.2 | PTYSession attach 就绪检测 | 连接后自动检测 tmux 会话是否就绪 |
| 2.3 | 端到端验证 | 浏览器 + MCP 同时操作同一终端 |

**验证场景**：
1. 浏览器先连接 → 看到堡垒机界面
2. MCP `connect_host` → attach 到同一会话
3. MCP `send_input("<target-ip>\r")` → 浏览器实时看到输入和跳转
4. MCP `run_command("uptime")` → 浏览器看到命令和输出
5. 断开 MCP → 浏览器终端不受影响

### Sprint 3：健壮性 + 边界场景

**目标**：处理各种边界情况

| # | 任务 | 验收标准 |
|---|------|----------|
| 3.1 | tmux 会话超时/异常断开恢复 | SSH 断开后 tmux 会话清理 + 重连 |
| 3.2 | 多主机并发 | 多个主机各自独立 tmux 会话 |
| 3.3 | Agent 先连 → 浏览器后连 | 反向顺序也正常工作 |
| 3.4 | tmux 状态栏定制（可选） | 显示主机名 + 连接状态 |

---

## 五、风险评估

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| WeTTY `--command` 模式与密码认证不兼容 | SSH 密码认证失效 | 中 | tmux-session.sh 内部用 `sshpass` 处理密码 |
| tmux 会话残留（SSH 断开后 tmux 仍存在） | 端口/内存泄漏 | 低 | 添加定期清理机制 + `tmux kill-session` |
| 多 WeTTY 连接同一个 tmux 导致 resize 冲突 | 终端尺寸异常 | 中 | 使用 `tmux set-option -g window-size smallest` 或由最新 attach 控制 |
| 堡垒机私钥认证路径问题 | 密钥认证失效 | 低 | tmux-session.sh 中支持 `-i <key_path>` 参数 |

---

## 六、验收标准（总体）

| # | 验收项 | 方法 |
|---|--------|------|
| 1 | 浏览器终端正常显示 SSH 内容 | 手动验证 |
| 2 | MCP Agent 操作在浏览器终端实时可见 | 同时打开浏览器 + MCP 客户端 |
| 3 | 断开 MCP 后浏览器终端不受影响 | MCP disconnect → 浏览器继续操作 |
| 4 | 断开浏览器后 MCP 仍可操作 | 关闭浏览器 → MCP 继续执行命令 |
| 5 | 重新打开浏览器可恢复会话 | 刷新页面 → tmux attach → 看到之前的终端内容 |
| 6 | 多主机独立会话 | 同时连接 2+ 主机，各自隔离 |

---

## 七、备选方案（已评估但未采用）

| 方案 | 评估结果 | 原因 |
|------|----------|------|
| **B: WeTTY 多客户端单 PTY** | ❌ 放弃 | 需要 fork WeTTY 源码，维护成本高 |
| **C: SSE 日志回显** | ❌ 放弃 | 不满足"浏览器终端看到 Agent 操作"的需求 |

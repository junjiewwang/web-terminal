<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/React-18-61dafb?logo=react&logoColor=white" />
  <img src="https://img.shields.io/badge/MCP-Streamable_HTTP-purple" />
  <img src="https://img.shields.io/badge/Docker-one--click-2496ED?logo=docker&logoColor=white" />
  <img src="https://img.shields.io/github/license/junjiewwang/web-terminal" />
</p>

# 🖥️ Web Terminal — 让 AI 帮你敲命令

> **一句话**：给 AI Agent 一个 SSH 终端，你在浏览器里围观它干活。

还在手动 SSH 到堡垒机，穿越三层跳板，敲一堆 `uptime`、`df -h`、`free -m`？
把这些苦差事交给 AI Agent 吧——它通过 [MCP 协议](https://modelcontextprotocol.io/) 操作终端，你只需要泡杯咖啡 ☕，打开浏览器看它表演。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/junjiewwang/web-terminal/main/.github/demo-dark.png">
    <img alt="Web Terminal Demo" src="https://raw.githubusercontent.com/junjiewwang/web-terminal/main/.github/demo.png" width="800">
  </picture>
  <br/>
  <em>Agent 在堡垒机里穿梭，你在浏览器里看得清清楚楚</em>
</p>

---

## ✨ 它能干什么

| 场景 | 描述 |
|------|------|
| 🤖 **Agent 远程运维** | AI 通过 MCP 连接 SSH 终端，自动执行巡检命令 |
| 🏰 **堡垒机穿越** | 支持 JumpServer 等堡垒机的交互式跳转（输入 IP → 等待菜单 → 选择主机） |
| 👀 **浏览器实时围观** | Agent 的每一次键入、每一行输出，你在 Web 终端里同步可见 |
| 📋 **操作日志面板** | Agent 干了什么？SSE 实时推送到侧边栏，明明白白 |
| 🔑 **主机资产管理** | YAML 一把梭配置，热加载不重启，密码 Fernet 加密存储 |

---

## 🏗️ 架构一览

```
                         ┌─────────────────────────────────┐
                         │        Browser (React)           │
                         │  ┌───────────┐  ┌─────────────┐ │
                         │  │  xterm.js  │  │ Agent Panel │ │
                         │  │ (终端画面) │  │ (操作日志)  │ │
                         │  └─────┬─────┘  └──────┬──────┘ │
                         └────────┼───────────────┼────────┘
                           socket.io            SSE
                                  │               │
                    ┌─────────────▼───────────────▼──────────┐
                    │            nginx (反向代理)              │
                    └─────┬──────────────┬──────────────┬─────┘
                          │              │              │
                    ┌─────▼────┐  ┌──────▼─────┐ ┌─────▼────┐
                    │  WeTTY   │  │  FastAPI    │ │   MCP    │
                    │(Node.js) │  │  REST API   │ │  Server  │
                    │SSH + PTY │  │  + SSE      │ │ 7 tools  │
                    └─────┬────┘  └────────────┘ └─────┬────┘
                          │                            │
                          │      socket.io (PTY)       │
                          │◄───────────────────────────┘
                          │
                    ┌─────▼──────────────────┐
                    │   SSH → 堡垒机 → 目标    │
                    └────────────────────────┘
```

**核心思路**：WeTTY 负责 SSH PTY，MCP Agent 和浏览器都通过 socket.io 连接到同一个 WeTTY 实例。Agent 敲命令 → WeTTY 广播输出 → 浏览器同步显示。

---

## 🚀 三步起飞

### 1. 配置主机

```bash
cp config/hosts-example.yaml config/hosts.yaml
```

编辑 `config/hosts.yaml`，填入你的 SSH 主机信息：

```yaml
hosts:
  - name: my-server          # 主机别名（唯一标识）
    hostname: 192.168.1.100  # IP 或域名
    port: 22
    username: deploy
    auth_type: password       # password | key
    password: "s3cret"        # 启动时自动 Fernet 加密
    description: 我的服务器
    tags: [prod, linux]
```

### 2. 一键启动

```bash
docker compose up -d
```

没了。打开 http://localhost:8000 看看效果。

### 3. 接入 AI Agent

在你的 MCP Client（如 Claude Desktop、CodeBuddy 等）中添加配置：

```json
{
  "mcpServers": {
    "web-terminal": {
      "type": "streamableHttp",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

然后对你的 AI 说：**"帮我连接 my-server，看看系统负载"**——剩下的交给它。

---

## 🛠️ MCP 工具箱

Agent 拿到了 **7 件装备**：

| 工具 | 干什么的 | 类比 |
|------|----------|------|
| `list_hosts` | 列出所有可用主机 | `cat /etc/hosts` |
| `connect_host` | 连接到指定主机 | `ssh user@host` |
| `run_command` | 执行命令并等输出 | 在终端里敲命令 + 按回车 + 等结果 |
| `send_input` | 发送任意输入 | 键盘打字（适配堡垒机菜单选择等） |
| `wait_for_output` | 等终端出现某段文字 | 盯着屏幕等关键字出现（expect 风格） |
| `read_terminal` | 读当前终端屏幕 | 抬头看看屏幕上显示了什么 |
| `disconnect` | 断开连接 | `exit` |

### 堡垒机场景示例

```
Agent: list_hosts()                           → 看到 "bastion" 主机
Agent: connect_host("bastion")                → SSH 连到堡垒机
Agent: read_terminal()                        → 看到 JumpServer 欢迎界面
Agent: send_input("10.0.1.100\r")             → 输入目标主机 IP
Agent: wait_for_output("Last login")          → 等到登录成功
Agent: run_command("uptime")                  → 43 days up, load 5.30 ✅
Agent: run_command("df -h /")                 → 197G, 15G used (8%) ✅
Agent: disconnect()                           → 收工！
```

---

## 📁 项目结构

```
web-terminal/
├── config/                    # 主机配置
│   └── hosts-example.yaml     # 配置模板（复制为 hosts.yaml 使用）
├── src/                       # Python 后端
│   ├── main.py                # FastAPI 入口 + 生命周期管理
│   ├── models/                # ORM 模型 + Pydantic Schema
│   ├── services/              # 核心业务
│   │   ├── host_manager.py    #   主机 CRUD + YAML 热加载同步
│   │   ├── wetty_manager.py   #   WeTTY 进程管理（多主机多端口）
│   │   ├── pty_session.py     #   PTY 交互式会话（socket.io + expect）
│   │   └── event_service.py   #   SSE 事件总线
│   ├── mcp_server/            # MCP Server（7 个 Agent 工具）
│   ├── api/                   # REST API + WeTTY 反向代理
│   └── utils/                 # 安全工具（Fernet 加密、Token 认证）
├── frontend/                  # React + xterm.js + Tailwind CSS
│   └── src/
│       ├── components/        #   HostList / TerminalView / AgentPanel
│       ├── hooks/             #   useTerminal / useWettySocket
│       └── services/          #   API 调用 + SSE 订阅
├── docs/                      # 项目文档
│   ├── PROGRESS.md            #   需求 & 实施记录 & 修复日志
│   └── tmux-session-sharing-plan.md  # tmux 会话共享方案（规划中）
├── tests/                     # 单元测试
├── Dockerfile                 # 多阶段构建（Python + Node.js + nginx）
├── docker-compose.yml         # 一键部署
├── nginx.conf                 # 反向代理配置
└── entrypoint.sh              # 容器启动脚本
```

---

## ⚙️ 技术栈

| 层 | 选型 | 为什么 |
|----|------|--------|
| **后端** | FastAPI + Uvicorn | 异步原生，MCP / SSE / WebSocket 全覆盖 |
| **MCP** | FastMCP (Streamable HTTP) | 官方 Python SDK，与 FastAPI 无缝集成 |
| **终端** | WeTTY (Node.js) | 成熟的 Web SSH 方案，socket.io 协议可编程 |
| **PTY 控制** | python-socketio | Agent 通过 socket.io 客户端操控 WeTTY PTY |
| **前端** | React 18 + xterm.js + Tailwind CSS | 终端渲染 + 现代 UI |
| **数据库** | SQLite (aiosqlite) | 轻量，单文件，开箱即用 |
| **安全** | Fernet 加密 + Bearer Token | 密码不裸奔，API 有认证 |
| **部署** | Docker + nginx | 一个容器搞定一切，nginx 搞定 SSE 长连接 |

---

## 🔧 本地开发

```bash
# 后端
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8001

# 前端
cd frontend && npm install && npm run dev

# WeTTY（需要 Node.js）
npm install -g wetty
```

> **提示**：本地开发需要 WeTTY 命令可用。Docker 部署则无需额外安装。

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WETTY_API_TOKEN` | API 认证 Token（不设则免认证） | 无 |
| `WETTY_ENCRYPTION_KEY` | 密码加密密钥（Fernet base64） | 自动生成 |
| `DATABASE_URL` | 数据库连接串 | `sqlite+aiosqlite:///./data/hosts.db` |

---

## 🗺️ Roadmap

- [x] 主机资产管理 + YAML 热加载
- [x] MCP Server — 7 个 PTY 交互式工具
- [x] Web Terminal — xterm.js 直连 WeTTY
- [x] Agent 操作面板 + SSE 实时推送
- [x] 安全加固 — Fernet 加密 + API 认证 + 命令黑名单
- [x] Docker 一键部署
- [ ] **tmux 会话共享** — Agent 和浏览器共享同一个 SSH PTY（[方案详情](docs/tmux-session-sharing-plan.md)）
- [ ] CI/CD 自动化
- [ ] PostgreSQL 生产级存储

---

## 📄 License

[MIT](LICENSE) — 随便用，记得 star ⭐

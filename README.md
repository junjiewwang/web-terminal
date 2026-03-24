# WeTTY + MCP 智能终端服务

基于 WeTTY 构建的「AI Agent 可控的 SSH 终端管理服务」。

## 功能特性

- **主机资产管理**：预配置多个 SSH 主机，支持 CRUD 管理
- **MCP Server**：AI Agent 通过 MCP 协议向远程终端发送命令、获取执行结果
- **Web Terminal**：基于 WeTTY 的浏览器终端，实时展示 Agent 操作过程
- **SSE 事件推送**：Agent 操作日志实时推送到前端

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端框架 | FastAPI (Python) | 异步、轻量，与 asyncssh 配合好 |
| SSH 库 | asyncssh | 纯 Python 异步 SSH |
| MCP Server | FastMCP (mcp[cli]) | 官方 Python SDK，SSE 模式 |
| 数据库 | SQLite（开发）/ PostgreSQL（生产） | 主机资产持久化 |
| 前端 | React + xterm.js | Agent 面板 + 嵌入 WeTTY |
| 事件推送 | SSE | Agent 操作过程实时推送 |
| Web Terminal | WeTTY | xterm.js + WebSocket SSH 穿透 |

## 快速开始

### 1. 安装依赖

```bash
# 后端
pip install -r requirements.txt

# 前端
cd frontend && npm install
```

### 2. 配置主机

编辑 `config/hosts.yaml`，添加 SSH 主机信息。

### 3. 启动服务

```bash
# 开发模式
uvicorn src.main:app --reload --port 8000

# Docker 一键部署
docker-compose up -d
```

### 4. MCP 连接

MCP Client 连接 `http://localhost:8000/mcp/sse` 即可使用 Agent 工具。

## 项目结构

```
wetty-mcp-terminal/
├── config/              # 配置文件
│   └── hosts.yaml       # 主机资产配置
├── src/                 # 后端源码
│   ├── main.py          # FastAPI 入口
│   ├── models/          # 数据模型
│   ├── services/        # 核心业务服务
│   ├── mcp_server/      # MCP Server
│   ├── api/             # REST API 路由
│   └── utils/           # 工具函数
├── frontend/            # React 前端
├── tests/               # 测试
├── docker-compose.yml   # Docker 编排
└── Dockerfile           # 后端镜像
```

## 开发里程碑

| 阶段 | 内容 | 工作量 |
|------|------|--------|
| Phase 1 | hosts.yaml + asyncssh + MCP Server 5 个工具 | 2天 |
| Phase 2 | FastAPI REST API + SQLite + WeTTY 集成 | 2天 |
| Phase 3 | 前端 Agent 面板 + SSE 事件推送 | 2天 |
| Phase 4 | 安全加固（认证、命令过滤、密钥加密） | 1天 |
| Phase 5 | Docker Compose 一键部署 | 0.5天 |

## License

MIT

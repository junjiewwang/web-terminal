# Docker 构建镜像源切换（清华 → 阿里云）

## 背景

2026-07-17，`make up`（即 `docker compose up -d --build`）构建失败。

## 问题

清华镜像源 `mirrors.tuna.tsinghua.edu.cn` 对 Debian trixie 的包返回 403 Forbidden，导致 `apt-get install` 阶段全部失败。

- 受影响阶段：`Dockerfile` Stage 2（`python:3.12-slim`）的系统包安装
- 错误节点 IP：`101.6.15.130`
- 镜像源 URL 中 `debian-security` 和 `debian` 路径均返回 403

## 解决方案

将 Docker 构建中所有使用的清华镜像源统一更换为阿里云镜像源：

| 语言/工具 | 旧镜像 (清华) | 新镜像 (阿里云) |
|-----------|--------------|----------------|
| apt (Debian) | `mirrors.tuna.tsinghua.edu.cn` | `mirrors.aliyun.com` |
| pip (Python) | `pypi.tuna.tsinghua.edu.cn/simple` | `mirrors.aliyun.com/pypi/simple/` |
| npm (Node.js) | `registry.npmmirror.com`（淘宝新域名） | 不变 |

改为阿里云镜像后构建成功。

## 变更文件

- `Dockerfile`：第 13 行注释、第 39 行 apt sed 命令、第 52 行 pip install 命令

## 额外修复：`make ip` 容器名硬编码问题

镜像源修复后构建成功，但 `make ip` 报告"容器未运行"。经排查，Makefile `ip` 目标中硬编码了容器名 `wetty-mcp-terminal-wetty-mcp-1`，而 docker compose 使用目录名 `web-terminal` 作为项目前缀，实际容器名为 `web-terminal-wetty-mcp-1`。

**修复**：改为 `docker compose ps -q wetty-mcp` 动态获取容器 ID，不再依赖硬编码名称。

**变更文件**：
- `Makefile`：`ip` 目标
- `docker-compose.yml`：第 15 行注释

## MCP `list_hosts` 输出截断修复（2026-07-28）

### 问题

MCP 客户端调用 `list_hosts` 时，主机列表结果被截断。实际堡垒机下有 10+ 子节点，但返回仅能看到 3 个。

### 根本原因

`list_hosts` 返回了每个节点的**全部字段**（id、port、username、tags、entry 等），其中 `entry` 字段包含嵌套的 `LoginStepSchema` 数组，用 `indent=2` 格式化后 JSON 体积极大。主机节点数多时，输出超出 MCP 客户端的 `maxOutputLength` 限制被截断。

截断层在 **MCP 客户端**，非服务端代码。

### 解决方案

`list_hosts` 默认返回精简输出，仅包含 Agent 做路由决策需要的 5 个核心字段：
- `name` — 连接时用
- `hostname` — 帮助识别 IP
- `description` — 理解主机用途
- `type` — root/nested
- `children` — 树结构

新增 `verbose=True` 参数，需要完整信息时显式开启。

### 变更文件

- `src/mcp_server/server.py`：`list_hosts` 函数（新增 `verbose` 参数，默认精简输出）


## MCP `download_file` 支持浏览器下载（2026-07-29）

### 问题

MCP `download_file` 工具只能将远端文件下载到容器内，Agent 返回的只是文本消息（"下载完成: /app/data/xxx.log"），用户无法真正拿到文件到本地电脑。

### 解决方案

复用已有的 REST API 两步下载机制：
1. `download_file` 完成 PTY 传输后，调用 `_register_download_token()` 注册一次性 token（120 秒 TTL）
2. 返回可直接在浏览器打开的下载 URL

**URL 自动检测**（优先级从高到低）：
1. **MCP 请求 Host 头**（推荐）：FastAPI 中间件 `_capture_host_middleware` 自动捕获客户端连接时使用的 Host（含端口），例如客户端配置 `http://10.0.0.5:8000/mcp` → 自动生成 `http://10.0.0.5:8000/...`。无需任何手动配置。
2. **`WETTY_EXTERNAL_URL` 环境变量**：手动指定外部地址（如经反向代理时）
3. **容器 IP**：兜底方案，用 `socket.gethostbyname()` 获取

下载流程：
```
远端节点 → PTY → 容器暂存 → 注册 token + 自动检测 URL → 返回下载链接
用户 → 浏览器打开 URL → GET .../download/{token} → 文件下载到本地
```

### 变更文件

- `src/mcp_server/server.py`：
  - 新增 `_mcp_request_host` ContextVar + `set_mcp_request_host()` + `_get_download_base_url()`
  - `download_file`：`local_path` 改为可选；下载成功注册 token 并返回下载链接
- `src/main.py`：
  - 新增 `_capture_host_middleware` 中间件，自动捕获 Host 头注入 MCP 上下文


## 遗留问题

- 清华镜像 403 是否为长期故障未知。如后续恢复，可考虑切换回去或引入多镜像 fallback 策略。
- npm 源（淘宝 npmmirror）暂时保留未变，目前未遇到问题。

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

## 遗留问题

- 清华镜像 403 是否为长期故障未知。如后续恢复，可考虑切换回去或引入多镜像 fallback 策略。
- npm 源（淘宝 npmmirror）暂时保留未变，目前未遇到问题。

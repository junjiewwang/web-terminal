# ══════════════════════════════════════════════
# MCP Terminal — 单镜像 · 单端口
#
# 架构：nginx(8000) → uvicorn(8001)
#   Python PTY + FastAPI WebSocket 直连终端
#   不再需要 Node.js / WeTTY 中间层
#
# 构建策略：
#   Stage 1: 编译前端（频繁变更）
#   Stage 2: 组装最终运行镜像（纯 Python）
#
# 层缓存优化原则：不变的在前，常变的在后
# 镜像源优化：apt 清华源 / npm 淘宝源 / pip 清华源
# ══════════════════════════════════════════════

# ── Stage 1: 前端编译 ────────────────────────
FROM node:22-slim AS frontend-build

RUN npm config set registry https://registry.npmmirror.com

WORKDIR /build
COPY frontend/package.json ./
RUN npm install

COPY frontend/ .
RUN npm run build

# ── Stage 2: 最终运行镜像（纯 Python，无 Node.js）──
FROM python:3.12-slim

LABEL maintainer="mcp-terminal"
WORKDIR /app

# 1) 系统包（几乎不变）
#    - openssh-client: SSH 客户端
#    - sshpass: tmux-session.sh 内通过 sshpass 自动输入 SSH 密码
#    - tmux: 会话复用，浏览器和 MCP Agent 共享同一个 SSH PTY
#    - nginx: 前端反向代理，解决浏览器 SSE 连接阻塞问题
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        sshpass \
        tmux \
        nginx \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/ssh/ssh_config.d \
    && printf 'Host *\n  HostKeyAlgorithms +ssh-rsa\n  PubkeyAcceptedAlgorithms +ssh-rsa\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n' \
       > /etc/ssh/ssh_config.d/legacy-compat.conf

# 2) Python 依赖（偶尔变）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

# 3) nginx 配置（偶尔变）
COPY nginx.conf /etc/nginx/sites-available/default

# 4) 启动脚本 + tmux 会话脚本 + 数据目录（几乎不变）
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/ /app/scripts/
RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh \
    && mkdir -p /app/data

# 5) 前端产物（经常变）
COPY --from=frontend-build /build/dist static/

# 6) 后端源码 & 配置（最常变）
COPY src/ src/
COPY config/ config/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["/app/entrypoint.sh"]

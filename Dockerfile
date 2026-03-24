# ══════════════════════════════════════════════
# WeTTY + MCP Terminal — 单镜像 · 单端口
#
# 构建策略：
#   Stage 1a: 安装 wetty（node-pty 编译，几乎不变）
#   Stage 1b: 编译前端（频繁变更）
#   Stage 2:  组装最终运行镜像
#
# 架构：nginx(8000) → uvicorn(8001)
#   nginx 作为前端反向代理，解决浏览器 HTTP/1.1 连接管理问题：
#   SSE streaming 响应会长期占用浏览器 TCP 连接槽位，导致后续
#   POST 请求被浏览器排队永远 pending。nginx 独立管理内部连接池，
#   与浏览器的连接互不干扰。
#
# 层缓存优化原则：不变的在前，常变的在后
#   Stage 2 层顺序：apt-get(几乎不变) → pip(偶尔变) → wetty(几乎不变)
#   → nginx.conf(偶尔变) → entrypoint(几乎不变) → 前端产物(经常变)
#   → 后端源码(最常变)
#
# 镜像源优化：apt 清华源 / npm 淘宝源 / pip 清华源
# ══════════════════════════════════════════════

# ── Stage 1a: 安装 wetty（独立 Stage，前端变更不影响缓存）──
FROM node:22-slim AS wetty-build

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && npm config set registry https://registry.npmmirror.com

# 安装 wetty（需要 node-gyp 编译 node-pty）
# 此层只有 wetty 版本变更时才会重建
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        python3 \
    && npm install -g wetty \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.npm

# ── Stage 1b: 前端编译（独立 Stage，频繁变更）──
FROM node:22-slim AS frontend-build

RUN npm config set registry https://registry.npmmirror.com

# 前端依赖（package.json 不变则缓存命中）
WORKDIR /build
COPY frontend/package.json ./
RUN npm install

# 前端编译
COPY frontend/ .
RUN npm run build

# ── Stage 2: 最终运行镜像 ─────────────────────
FROM python:3.12-slim

LABEL maintainer="wetty-mcp-terminal"
WORKDIR /app

# ┌─────────────────────────────────────────────┐
# │ 以下层按变更频率从低到高排列                    │
# │ 上层缓存命中 → 下层无需重建                     │
# └─────────────────────────────────────────────┘

# 1) 系统包（几乎不变）
#    - openssh-client: SSH 客户端
#    - sshpass: WeTTY --ssh-pass 参数依赖此工具自动输入密码
#    - nginx: 前端反向代理，解决浏览器 SSE 连接阻塞问题
#    - OpenSSH 10.x 默认禁用 ssh-rsa，旧服务器兼容配置
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        sshpass \
        nginx \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/ssh/ssh_config.d \
    && printf 'Host *\n  HostKeyAlgorithms +ssh-rsa\n  PubkeyAcceptedAlgorithms +ssh-rsa\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n' \
       > /etc/ssh/ssh_config.d/legacy-compat.conf

# 2) Python 依赖（偶尔变：requirements.txt 不变则缓存命中）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

# 3) node + wetty 二进制（几乎不变：从独立 Stage 拷贝，前端变更不影响）
#    注意：新版 wetty 入口从 bin/wetty.js 改为 build/main.js
COPY --from=wetty-build /usr/local/bin/node /usr/local/bin/node
COPY --from=wetty-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/wetty/build/main.js /usr/local/bin/wetty \
    && chmod +x /usr/local/lib/node_modules/wetty/build/main.js \
    && ln -sf /usr/local/bin/node /usr/local/bin/nodejs

# 4) nginx 配置（偶尔变）
COPY nginx.conf /etc/nginx/sites-available/default

# 5) 启动脚本 + 数据目录（几乎不变）
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data

# 6) 前端产物（经常变：从独立 Stage 拷贝）
COPY --from=frontend-build /build/dist static/

# 7) 后端源码 & 配置（最常变，放最后 → 增量构建秒级）
COPY src/ src/
COPY config/ config/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["/app/entrypoint.sh"]

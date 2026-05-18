# ══════════════════════════════════════════════
# wetty-mcp-terminal Makefile
#
# 常用命令速查：
#   make help           — 查看所有可用命令
#   make init           — 首次部署初始化
#   make up             — 启动服务
#   make up-with-mysql  — 启动服务（含本地 MySQL 容器）
#   make down           — 停止服务
#   make ip             — 获取容器 IP（访问地址）
#   make logs           — 查看日志
#
# 数据库模式由 .env 中的 DATABASE_URL 控制：
#   留空 → SQLite（零配置）
#   设值 → MySQL
#
# 访问方式：
#   不做宿主机端口映射，通过容器 IP 直接访问
#   make ip → 获取容器 IP，然后访问 http://<IP>:8000
# ══════════════════════════════════════════════

.PHONY: help init up up-with-mysql down restart logs logs-f \
        build rebuild status ps ip \
        password-hash password-reset \
        dev dev-frontend clean

# ── 帮助 ─────────────────────────────────────

help: ## 显示所有可用命令
	@echo ""
	@echo "  wetty-mcp-terminal 命令列表"
	@echo "  ════════════════════════════"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── 初始化 ───────────────────────────────────

init: ## 首次部署初始化（创建 .env）
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "✓ 已创建 .env（请修改其中的配置）"; \
	else \
		echo "⚠ .env 已存在，跳过创建"; \
	fi
	@echo ""
	@echo "下一步："
	@echo "  1. 编辑 .env 文件，按需设置 DATABASE_URL"
	@echo "  2. 运行 make up 启动服务"

# ── 服务管理 ─────────────────────────────────

up: _check-env ## 启动服务（通过 .env DATABASE_URL 控制数据库模式）
	docker compose up -d --build
	@echo ""
	@echo "✓ 服务已启动"
	@echo "  获取访问地址: make ip"

up-with-mysql: _check-env _check-override ## 启动服务（含本地 MySQL 容器）
	docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build
	@echo ""
	@echo "✓ 服务已启动（含本地 MySQL）"
	@echo "  获取访问地址: make ip"

down: ## 停止服务
	docker compose down
	@echo "✓ 服务已停止"

restart: ## 重启服务
	docker compose restart
	@echo "✓ 服务已重启"

# ── 构建 ─────────────────────────────────────

build: ## 构建 Docker 镜像
	docker compose build

rebuild: ## 强制重新构建（不使用缓存）
	docker compose build --no-cache

# ── 日志与状态 ────────────────────────────────

logs: ## 查看服务日志（最近 100 行）
	docker compose logs --tail=100

logs-f: ## 实时跟踪日志
	docker compose logs -f

ps: ## 查看服务状态
	@docker compose ps

status: ps ## ps 的别名

ip: ## 获取容器 IP 及访问地址
	@CONTAINER_IP=$$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' wetty-mcp-terminal-wetty-mcp-1 2>/dev/null); \
	if [ -z "$$CONTAINER_IP" ]; then \
		echo "⚠ 容器未运行，请先 make up"; \
	else \
		echo "  容器 IP: $$CONTAINER_IP"; \
		echo "  访问地址: http://$$CONTAINER_IP:8000"; \
	fi

# ── 密码工具 ──────────────────────────────────

password-hash: ## 生成 bcrypt 密码哈希
	@read -p "请输入密码: " pwd; \
	docker compose exec wetty-mcp python -m src.utils.password_hash "$$pwd" 2>/dev/null || \
	python3 -m src.utils.password_hash "$$pwd"

password-reset: ## 重置登录密码（直连数据库，无需重启）
	@echo "⚠ 此操作将重置密码并使所有登录会话失效"
	@read -p "请输入新密码: " pwd; \
	docker compose exec wetty-mcp python -m src.utils.reset_password "$$pwd" 2>/dev/null || \
	echo "提示: 容器未运行，请先 make up"

# ── 本地开发 ──────────────────────────────────

dev: ## 本地启动后端（开发模式）
	uvicorn src.main:app --host 0.0.0.0 --port 8001 --reload

dev-frontend: ## 本地启动前端（开发模式）
	cd frontend && npm run dev

# ── 清理 ──────────────────────────────────────

clean: ## 停止服务并清理所有数据卷（⚠️ 不可恢复）
	@echo "⚠ 警告：此操作将删除所有数据（数据库、上传文件等）"
	@read -p "确认清理? [y/N] " confirm; \
	if [ "$$confirm" = "y" ] || [ "$$confirm" = "Y" ]; then \
		docker compose down -v 2>/dev/null; \
		rm -rf data/*.db; \
		echo "✓ 已清理所有数据"; \
	else \
		echo "已取消"; \
	fi

# ── 内部辅助 ──────────────────────────────────

_check-env:
	@if [ ! -f .env ]; then \
		echo ""; \
		echo "⚠ .env 文件不存在！"; \
		echo "  运行 make init 进行首次初始化"; \
		echo "  或手动: cp .env.example .env"; \
		echo ""; \
		exit 1; \
	fi

_check-override:
	@if [ ! -f docker-compose.override.yml ]; then \
		echo ""; \
		echo "⚠ docker-compose.override.yml 不存在！"; \
		echo "  运行: cp docker-compose.override.yml.example docker-compose.override.yml"; \
		echo "  然后编辑 .env 设置 MYSQL_* 变量和 DATABASE_URL"; \
		echo ""; \
		exit 1; \
	fi

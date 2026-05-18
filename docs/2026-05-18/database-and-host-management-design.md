# 数据库双支持 + 页面主机管理 + 认证入库 设计文档

> **日期**：2026-05-18
> **状态**：🔧 实施中（Sprint 1 ✅ → Sprint 2 ✅ → 配置简化 ✅ → Sprint 3 ✅ → 首次初始化重构 ✅）

---

## 一、需求背景

当前 wetty-mcp-terminal 的数据管理存在以下痛点：

| 痛点 | 说明 |
|------|------|
| hosts.yaml 是唯一入口 | 必须手动编辑 YAML 文件，不方便、易出错 |
| 本地 SQLite | Docker 重建可能丢数据，不便于多实例 |
| auth.yaml 文件管理 | 忘记密码只能去改文件，缺少自助重置手段 |

**目标**：

1. 数据库 SQLite/MySQL 双支持（SQLite 默认，MySQL 可选）
2. 主机管理「YAML 导入」+「页面树形编辑器」双通道
3. 认证配置入库，提供密码重置 CLI 脚本
4. 数据库成为唯一真相，YAML 降级为导入/导出/备份工具

---

## 二、总体架构

```
┌─ 数据来源 ──────────────────────────────────────────────┐
│                                                          │
│  ① YAML 导入 ─────────────┐                             │
│     config/hosts.yaml      │     ┌──────────────────┐   │
│     (批量/初始化/迁移)      ├───→ │   数据库（SSOT）   │   │
│                            │     │                  │   │
│  ② 页面 CRUD ─────────────┤     │  SQLite (默认)   │   │
│     树形编辑器              │     │  MySQL  (可选)   │   │
│     (日常管理/新增/编辑)     │     │                  │   │
│                            │     │  ├─ hosts        │   │
│  ③ CLI 脚本 ──────────────┘     │  ├─ credentials  │   │
│     reset_password               │  └─ auth_config  │   │
│     (紧急密码重置)               │                  │   │
│                                  └───────┬──────────┘   │
│                                          │              │
│  ④ YAML 导出 ◄───────────────────────────┘              │
│     GET /api/hosts/export                                │
│     (备份/迁移)                                          │
└──────────────────────────────────────────────────────────┘
                    │
                    ▼
         REST API + 前端 UI + MCP Server
```

---

## 三、Sprint 划分

| Sprint | 内容 | 核心交付 |
|--------|------|----------|
| **Sprint 1** | SQLite/MySQL 双支持 | database.yaml 配置、引擎工厂、Enum 兼容、docker-compose MySQL 服务、迁移脚本 |
| **Sprint 2** | 认证入库 + 密码脚本 | auth_config 表、AuthService 改 DB 读取、reset_password CLI、前端设置页面 |
| **Sprint 3** | 页面主机管理 + YAML 导入导出 | 前端树形编辑器、YAML 上传导入 API、YAML 导出下载、移除 hosts.yaml 热加载 |

---

## 四、Sprint 1 — SQLite/MySQL 双支持

### 4.1 配置设计

> ⚠️ 下方为 Sprint 1 原始设计。后续"配置简化"中已移除 database.yaml，统一为 `DATABASE_URL` 单入口。

**优先级**（高→低）：
1. 环境变量 `DATABASE_URL`（Docker/CI 友好）
2. ~~`config/database.yaml` 配置文件~~ **（已删除）**
3. 默认 SQLite（`data/wetty_mcp.db`）

```yaml
# config/database.yaml（仅 MySQL 模式需要）
driver: mysql            # sqlite | mysql
host: 127.0.0.1
port: 3306
user: wetty
password: "${DB_PASSWORD}"   # 支持环境变量替换
database: wetty_terminal
charset: utf8mb4

# 连接池配置（MySQL 专用）
pool_size: 5
max_overflow: 10
pool_recycle: 3600
```

**SQLite 模式**（默认，无需任何配置）：
- 不存在 `DATABASE_URL` 且不存在 `config/database.yaml` → 自动使用 SQLite
- 数据文件：`data/wetty_mcp.db`

### 4.2 引擎工厂重构

**当前**：`src/models/database.py` 硬编码 SQLite

**改造后**：

```python
# src/models/database.py

def _build_database_url() -> str:
    """按优先级构建数据库 URL"""
    # 1. 环境变量优先
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    # 2. config/database.yaml
    config_path = Path("config/database.yaml")
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text())
        if config.get("driver") == "mysql":
            password = _resolve_env_var(config.get("password", ""))
            return (
                f"mysql+aiomysql://{config['user']}:{password}"
                f"@{config['host']}:{config.get('port', 3306)}"
                f"/{config['database']}?charset={config.get('charset', 'utf8mb4')}"
            )

    # 3. 默认 SQLite
    db_path = Path("data/wetty_mcp.db")
    return f"sqlite+aiosqlite:///{db_path}"

def _create_engine(url: str) -> AsyncEngine:
    """根据 URL 创建引擎，MySQL 额外配置连接池"""
    kwargs: dict[str, Any] = {"echo": False, "future": True}

    if url.startswith("mysql"):
        # MySQL 连接池参数
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=3600)
    else:
        # SQLite 启用外键约束
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return create_async_engine(url, **kwargs)
```

### 4.3 Enum 兼容性处理

**当前问题**：`Enum(AuthType)` 在 SQLite 中存为字符串，MySQL 中为原生 ENUM。

**方案**：改用 `String` + Python 侧验证，避免 MySQL 原生 ENUM 的 DDL 迁移问题：

```python
# 方案 A：保持当前 Enum，SQLAlchemy 自动处理方言差异（推荐）
# SQLAlchemy 2.0 的 Enum 类型在 SQLite 中自动退化为 VARCHAR，
# 在 MySQL 中使用原生 ENUM，无需特殊处理。

# 方案 B（如果 A 有问题）：手动指定非原生 ENUM
auth_type: Mapped[AuthType] = mapped_column(
    Enum(AuthType, native_enum=False),  # 所有数据库都用 VARCHAR
    nullable=False, default=AuthType.KEY
)
```

**推荐方案 A**，SQLAlchemy 2.0+ 已经很好地处理了这个差异。如果测试中发现兼容性问题再切到方案 B。

### 4.4 自动迁移兼容

当前 `_auto_migrate_columns()` 使用 `ALTER TABLE ADD COLUMN`，MySQL 完全支持相同语法。
唯一需要注意的是 `serialize_default_value()` 中枚举值的引号处理，当前用 `'{default_literal}'` 格式，MySQL 兼容。

### 4.5 init_db() 改造

```python
async def init_db() -> None:
    """初始化数据库"""
    if is_sqlite():
        _DB_DIR.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_auto_migrate_columns)
        await conn.run_sync(reset_hosts_table_if_legacy_enum_found)
```

### 4.6 新增依赖

```
# requirements.txt 新增
aiomysql>=0.2.0          # MySQL 异步驱动
```

### 4.7 docker-compose.yml 改造

```yaml
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:-wetty_root_123}
      MYSQL_DATABASE: wetty_terminal
      MYSQL_USER: wetty
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:-wetty_123}
    ports:
      - "3306"
    volumes:
      - mysql-data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 10s
      timeout: 5s
      retries: 5

  wetty-mcp:
    build: .
    depends_on:
      mysql:
        condition: service_healthy
    ports:
      - "8000"
    volumes:
      - ./config:/app/config:ro
      - ~/.ssh:/root/.ssh:ro
    environment:
      - PYTHONPATH=/app
      - WETTY_API_TOKEN=${WETTY_API_TOKEN:-}
      - DATABASE_URL=mysql+aiomysql://wetty:${MYSQL_PASSWORD:-wetty_123}@mysql:3306/wetty_terminal?charset=utf8mb4
    restart: unless-stopped

volumes:
  mysql-data:
```

**SQLite 模式 docker-compose**（`docker-compose.sqlite.yml`）：
```yaml
services:
  wetty-mcp:
    build: .
    ports:
      - "8000"
    volumes:
      - wetty-data:/app/data
      - ./config:/app/config:ro
      - ~/.ssh:/root/.ssh:ro
    environment:
      - PYTHONPATH=/app
    restart: unless-stopped

volumes:
  wetty-data:
```

### 4.8 SQLite → MySQL 迁移脚本

```bash
# 一键迁移（读取 SQLite → 写入 MySQL）
python -m src.utils.migrate_db --from sqlite --to mysql
```

### 4.9 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 重构 | `src/models/database.py` | 引擎工厂、配置读取、双数据库支持 |
| 新建 | `config/database.yaml.example` | MySQL 配置示例 |
| 修改 | `docker-compose.yml` | 增加 MySQL 服务 |
| 新建 | `docker-compose.sqlite.yml` | SQLite 模式的精简 compose |
| 新建 | `src/utils/migrate_db.py` | SQLite → MySQL 迁移脚本 |
| 修改 | `requirements.txt` | 新增 aiomysql |
| 修改 | `Dockerfile` | 确保包含 MySQL 客户端库 |

---

## 五、Sprint 2 — 认证入库 + 密码脚本

### 5.1 auth_config 表设计

```python
class AuthConfig(Base):
    """认证配置（单行表）"""
    __tablename__ = "auth_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用密码保护")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="bcrypt 哈希")
    jwt_secret: Mapped[str] = mapped_column(String(255), nullable=False, comment="JWT 签名密钥")
    access_token_expire_hours: Mapped[int] = mapped_column(Integer, default=2)
    refresh_token_expire_days: Mapped[int] = mapped_column(Integer, default=7)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
```

### 5.2 refresh_tokens 表（替代内存存储）

当前 AuthService 的 refresh token 存在内存 dict 中，重启即丢失。入库后持久化：

```python
class RefreshToken(Base):
    """Refresh Token 记录"""
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked: Mapped[bool] = mapped_column(default=False)
```

### 5.3 AuthService 改造

```python
class AuthService:
    """改造后：从数据库读取认证配置"""

    async def load(self, session: AsyncSession) -> None:
        """从 DB 加载配置，DB 为空时从 auth.yaml 种子初始化"""
        row = await session.get(AuthConfig, 1)
        if row is None:
            row = await self._seed_from_yaml(session)
        self._config = row

    async def _seed_from_yaml(self, session: AsyncSession) -> AuthConfig:
        """首次启动：从 auth.yaml 导入到 DB"""
        yaml_path = Path("config/auth.yaml")
        if yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text())
            config = AuthConfig(
                id=1,
                enabled=data.get("enabled", True),
                password_hash=data["password_hash"],
                jwt_secret=data.get("jwt_secret", secrets.token_urlsafe(32)),
                access_token_expire_hours=data.get("access_token_expire_hours", 2),
                refresh_token_expire_days=data.get("refresh_token_expire_days", 7),
            )
        else:
            # 无 YAML → 生成默认配置（禁用认证）
            config = AuthConfig(id=1, enabled=False, password_hash="", jwt_secret=secrets.token_urlsafe(32))
        session.add(config)
        await session.commit()
        return config
```

### 5.4 密码重置 CLI

```python
# src/utils/reset_password.py
"""密码重置 CLI 工具

用法：
    python -m src.utils.reset_password "new-password"
    python -m src.utils.reset_password              # 交互式

    # Docker 容器内
    docker exec -it wetty-mcp python -m src.utils.reset_password
"""

async def reset_password(new_password: str) -> None:
    """直连数据库重置密码 + 吊销所有 refresh token"""
    from src.models.database import engine, AuthConfig, RefreshToken

    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(12)).decode()

    async with AsyncSession(engine) as session:
        # 更新密码
        config = await session.get(AuthConfig, 1)
        if config:
            config.password_hash = hashed
        else:
            session.add(AuthConfig(id=1, password_hash=hashed, ...))

        # 吊销所有 refresh token
        await session.execute(
            update(RefreshToken).values(revoked=True)
        )
        await session.commit()

    print("✓ 密码已重置，所有登录会话已失效")
```

### 5.5 前端设置页面

```
┌──────────────────────────────────────────────────┐
│  ⚙️ 系统设置                                      │
├──────────────────────────────────────────────────┤
│                                                  │
│  🔐 认证设置                                      │
│  ├─ 启用密码保护: [✓]                             │
│  ├─ 修改密码: [当前密码] [新密码] [确认] [保存]     │
│  ├─ Access Token 有效期: [2] 小时                 │
│  └─ Refresh Token 有效期: [7] 天                  │
│                                                  │
│  🗄️ 数据库状态（只读）                             │
│  ├─ 类型: MySQL / SQLite                         │
│  ├─ 状态: ✅ 已连接                               │
│  └─ 地址: mysql:3306/wetty_terminal              │
│                                                  │
└──────────────────────────────────────────────────┘
```

新增 API：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/settings/auth` | 获取认证配置（不返回 password_hash） |
| PUT | `/api/settings/auth` | 更新认证配置 |
| GET | `/api/settings/database` | 获取数据库连接状态 |

### 5.6 兼容性策略

| 阶段 | auth.yaml 行为 |
|------|----------------|
| Sprint 1（数据库双支持） | 保持现有 auth.yaml 读取，不变 |
| Sprint 2（认证入库后） | auth.yaml 降级为初始化种子——仅在 DB `auth_config` 表为空时读取一次，之后忽略 |
| 长期 | auth.yaml 可保留作紧急恢复文档，不再被服务自动读取 |

### 5.7 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `src/models/auth.py` | AuthConfig、RefreshToken ORM 模型 |
| 重构 | `src/services/auth_service.py` | 改从 DB 读取，refresh token 持久化 |
| 新建 | `src/utils/reset_password.py` | 密码重置 CLI |
| 新建 | `src/api/settings.py` | 设置 API 路由 |
| 新建 | `frontend/src/components/SettingsPage.tsx` | 前端设置页面 |
| 修改 | `frontend/src/App.tsx` | 新增设置页面入口 |
| 修改 | `src/main.py` | 注册 settings API 路由，认证初始化改 DB |
| 修改 | `src/models/__init__.py` | 导出新模型 |

---

## 六、Sprint 3 — 页面主机管理 + YAML 导入导出

### 6.1 真相翻转

| 维度 | 改造前 | 改造后 |
|------|--------|--------|
| 唯一真相 | config/hosts.yaml | 数据库 hosts 表 |
| YAML 角色 | SSOT（热加载同步到 DB） | 导入/导出/备份工具 |
| 页面角色 | 只读展示 | 完整 CRUD + 树形编辑器 |
| 热加载 | `_watch_hosts_yaml` 监听文件变更 | 移除（DB 为准） |

### 6.2 新增/修改 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/hosts/import` | 上传 YAML 文件导入到 DB |
| GET | `/api/hosts/export` | 导出 DB 主机数据为 YAML 下载 |
| PUT | `/api/hosts/{id}/move` | 移动节点（改变 parent_id + 排序） |
| POST | `/api/hosts/{id}/duplicate` | 复制节点（含子树） |
| （已有） | POST/PUT/DELETE `/api/hosts` | 页面 CRUD |

#### 导入 API 设计

```python
class ImportMode(str, Enum):
    OVERWRITE = "overwrite"   # 清空现有 → 全量导入
    MERGE = "merge"           # 保留现有 + 按 name 匹配更新/新增

@router.post("/api/hosts/import")
async def import_hosts(
    file: UploadFile,
    mode: ImportMode = ImportMode.MERGE,
    session: AsyncSession = Depends(get_db),
):
    """上传 YAML 文件导入主机"""
```

#### 导出 API 设计

```python
@router.get("/api/hosts/export")
async def export_hosts(session: AsyncSession = Depends(get_db)):
    """导出所有主机为 YAML 格式下载"""
    hosts = await manager.list_host_responses()
    yaml_content = _hosts_to_yaml(hosts)
    return Response(
        content=yaml_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=hosts.yaml"},
    )
```

### 6.3 前端树形编辑器

```
┌──────────────────────────────────────────────────────────────┐
│  📋 主机管理                           [📥 导入] [📤 导出]    │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  🔍 搜索主机...                              [+ 新增根节点]   │
│                                                              │
│  ┌─ 🌳 主机树 ─────────────────────────────────────────┐    │
│  │                                                      │    │
│  │  📂 dev-cloud                                        │    │
│  │  │  hostname: 9.134.114.82:36000                     │    │
│  │  │  auth: key | status: active                       │    │
│  │  │  [编辑] [添加子节点] [删除]                        │    │
│  │  │                                                   │    │
│  │  📂 tce-server                           [展开/折叠]  │    │
│  │  │  hostname: 118.24.225.114:36000                   │    │
│  │  │  auth: password | status: active                  │    │
│  │  │  [编辑] [添加子节点] [删除]                        │    │
│  │  │                                                   │    │
│  │  ├─ 📂 tcs235测试环境                                │    │
│  │  │  ├─ 💻 m12(旧-待下线) ⚠️ deprecated               │    │
│  │  │  │   [编辑] [添加子节点] [删除]                    │    │
│  │  │  └─ 💻 m12-new                                   │    │
│  │  │      [编辑] [添加子节点] [删除]                    │    │
│  │  │                                                   │    │
│  │  └─ 📂 研发d12-x86ver                               │    │
│  │      [编辑] [添加子节点] [删除]                       │    │
│  │                                                      │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  ─────────────── 导入 YAML ──────────────────               │
│  📁 拖拽上传 hosts.yaml 或 [选择文件]                        │
│  导入模式: ○ 覆盖现有  ● 合并（保留+新增）                    │
│  [开始导入]                                                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

#### 主机编辑表单（Modal / Drawer）

```
┌──────────────────────────────────────────────────┐
│  编辑主机 — dev-cloud                      [✕]   │
├──────────────────────────────────────────────────┤
│                                                  │
│  基本信息                                        │
│  ├─ 名称:     [dev-cloud          ]             │
│  ├─ 地址:     [9.134.114.82       ]             │
│  ├─ 端口:     [36000              ]             │
│  ├─ 用户名:   [root               ]             │
│  ├─ 描述:     [开发环境服务器       ]             │
│  └─ 标签:     [dev] [linux] [+]                 │
│                                                  │
│  认证方式                                        │
│  ├─ 类型:     ● SSH Key  ○ Password             │
│  └─ 密钥路径: [~/.ssh/id_rsa      ]             │
│                                                  │
│  连接设置                                        │
│  ├─ 就绪模式: [\[\$#>%\]\s*$       ]             │
│  └─ 状态:     ● Active  ○ Deprecated ○ Disabled │
│                                                  │
│  入口动作（子节点专用）                            │
│  ├─ 类型:     ○ None ○ Menu Send ○ SSH Command  │
│  ├─ 值:       [                    ]             │
│  └─ 交互步骤:                                    │
│     ├─ Step 1: wait [Password:] send [***]       │
│     └─ [+ 添加步骤]                              │
│                                                  │
│  凭据                                            │
│  ├─ SSH 密码:        [***          ] (可选)      │
│  ├─ 入口密码:        [***          ] (可选)      │
│  └─ 共享凭据引用:    [tce-login    ] (可选)      │
│                                                  │
│              [取消]  [保存]                       │
└──────────────────────────────────────────────────┘
```

### 6.4 前端技术选型

| 需求 | 方案 | 理由 |
|------|------|------|
| 树形展示 | 递归组件 `HostTreeNode` | 数据结构本身是递归的，自定义组件最灵活 |
| 拖拽排序 | 暂不做（后续增强） | MVP 先保证 CRUD 和树形展示 |
| 编辑表单 | Drawer/Modal 内嵌表单 | 复杂字段较多，侧边抽屉体验更好 |
| YAML 上传 | `<input type="file">` + Drag&Drop | 简单可靠 |
| 样式 | Tailwind CSS（复用现有） | 与项目风格一致 |

### 6.5 HostManager 改造

| 改造点 | 说明 |
|--------|------|
| 移除 `sync_from_yaml()` 自动调用 | 不再启动时自动同步 |
| 保留 `sync_from_yaml()` 方法 | 供 `/api/hosts/import` 调用 |
| 新增 `export_to_yaml()` | 将 DB 树导出为 YAML 结构 |
| 新增 `move_host()` | 修改 parent_id 实现节点移动 |
| 新增 `duplicate_host()` | 深拷贝节点及其子树 |

### 6.6 main.py 改造

| 改造点 | 说明 |
|--------|------|
| 移除 `_watch_hosts_yaml()` | 不再监听 hosts.yaml 变更 |
| 移除 `_sync_hosts_from_yaml()` 启动调用 | 改为按需（import API 触发）|
| 保留 `_watch_auth_yaml()` | Sprint 2 后也移除 |

### 6.7 首次启动兼容

当 DB 为空且存在 `config/hosts.yaml` 时，自动执行一次导入（merge 模式），确保从旧版本升级时数据不丢失：

```python
async def _maybe_seed_hosts(session: AsyncSession) -> None:
    """DB 为空 + hosts.yaml 存在 → 自动导入一次"""
    count = await session.scalar(select(func.count(Host.id)))
    if count == 0:
        yaml_path = Path("config/hosts.yaml")
        if yaml_path.exists():
            logger.info("数据库为空，从 hosts.yaml 自动导入...")
            await manager.sync_from_yaml(session)
```

### 6.8 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `src/api/hosts.py` | 新增 import/export/move API |
| 修改 | `src/services/host_manager.py` | 新增 export_to_yaml、move_host、移除自动同步 |
| 修改 | `src/main.py` | 移除 hosts.yaml 热加载，新增首次种子逻辑 |
| 新建 | `frontend/src/pages/HostManagement.tsx` | 主机管理主页面 |
| 新建 | `frontend/src/components/HostTreeEditor.tsx` | 树形编辑器组件 |
| 新建 | `frontend/src/components/HostEditForm.tsx` | 主机编辑表单 |
| 新建 | `frontend/src/components/YamlImporter.tsx` | YAML 上传导入组件 |
| 修改 | `frontend/src/App.tsx` | 新增主机管理页面路由/入口 |
| 修改 | `frontend/src/services/api.ts` | 新增 import/export API 调用 |

---

## 七、credentials 管理

### 7.1 credentials 表

当前 hosts.yaml 顶层有 `credentials` 共享凭据区，需要入库：

```python
class Credential(Base):
    """共享凭据"""
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False, comment="加密后的密码")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
```

### 7.2 credentials API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/credentials` | 列出所有凭据（不返回密码） |
| POST | `/api/credentials` | 创建凭据 |
| PUT | `/api/credentials/{id}` | 更新凭据 |
| DELETE | `/api/credentials/{id}` | 删除凭据 |

### 7.3 归入 Sprint

credentials 表和 API 归入 **Sprint 3**，与主机管理一起实施（主机编辑表单中需要选择共享凭据）。

---

## 八、前端路由 / 导航设计

当前 App 是单页面，需要增加导航支持多页面：

```
┌─────────────────────────────────────────────────┐
│  侧边栏                                         │
│  ┌─────────────────────────────────────┐        │
│  │  ⌘ WebTerminal                      │        │
│  │                                     │        │
│  │  📡 终端    ← 当前主界面             │        │
│  │  📋 主机管理 ← Sprint 3 新增         │        │
│  │  ⚙️ 设置    ← Sprint 2 新增         │        │
│  └─────────────────────────────────────┘        │
│                                                 │
│  （终端页面：原有侧边栏主机列表不变）              │
└─────────────────────────────────────────────────┘
```

**路由方案**：无需引入 React Router，使用简单的 state-based 页面切换即可（项目本身是单实例管理工具，不需要 URL 路由）。

```typescript
type Page = "terminal" | "hosts" | "settings";
const [currentPage, setCurrentPage] = useState<Page>("terminal");
```

---

## 九、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| MySQL 不可用时服务无法启动 | 服务不可用 | SQLite fallback + 启动时明确错误提示 + 健康检查 |
| Enum 兼容性 | 数据异常 | 优先方案 A（SQLAlchemy 自动处理），必要时切方案 B |
| 密码存 DB 安全性 | 信息泄露 | bcrypt 哈希不变，DB 密码走环境变量 |
| 旧版本升级数据丢失 | hosts 数据丢失 | 首次启动自动从 hosts.yaml 种子导入 |
| 树形编辑器交互复杂 | 前端工作量大 | 先做基础 CRUD + 树形展示，拖拽排序后续增强 |

---

## 十、遗留问题

暂无。

---

## 附录：Sprint 1 实施记录

**完成时间**：2026-05-18

### 已完成任务

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1.1 | 引擎工厂重构（三级配置降级） | `src/models/database.py` | ✅ |
| 1.2 | MySQL 配置示例 | `config/database.yaml.example` | ✅ (后续配置简化时删除) |
| 1.3 | aiomysql 依赖 | `requirements.txt` | ✅ |
| 1.4 | MySQL 模式 docker-compose | `docker-compose.yml` | ✅ |
| 1.5 | SQLite 模式 docker-compose | `docker-compose.sqlite.yml` | ✅ (后续配置简化时删除) |
| 1.6 | SQLite → MySQL 迁移脚本 | `src/utils/migrate_db.py` | ✅ |
| 1.7 | main.py 适配（启动日志 + 健康检查） | `src/main.py` | ✅ |

### 新增文件清单

| 文件 | 说明 |
|------|------|
| `config/database.yaml.example` | MySQL 配置示例（含连接池参数、环境变量占位符） |
| `docker-compose.sqlite.yml` | SQLite 轻量单机部署的精简 compose |
| `src/utils/migrate_db.py` | CLI 迁移工具：`python -m src.utils.migrate_db` |

### 修改文件清单

| 文件 | 改动说明 |
|------|----------|
| `src/models/database.py` | 完全重写：`_build_database_url()` 三级降级、`_create_engine()` 连接池 + SQLite PRAGMA、`get_db_type()`/`is_sqlite()`/`is_mysql()`/`get_db_info()` 公共查询接口 |
| `requirements.txt` | 新增 `aiomysql>=0.2.0` |
| `docker-compose.yml` | 改为 MySQL 模式（MySQL 服务 + 健康检查 + DATABASE_URL 注入） |
| `src/main.py` | 导入 `get_db_info`、启动日志输出数据库类型、健康检查返回 database 字段 |

### 验证结果

| 验证项 | 状态 |
|--------|------|
| Python 编译（7 个核心文件 py_compile） | ✅ 全部通过 |
| 所有引用 database.py 的文件兼容性 | ✅ 无 breaking change |
| 默认 SQLite 模式（无 DATABASE_URL / database.yaml） | ✅ 行为不变 |

---

## Sprint 1 补充：Docker Compose 外部 MySQL 全流程测试

> **日期**：2026-05-18
> **目标**：通过 `make up-external` 验证完整部署流程

### 新增/修改文件

| 文件 | 说明 |
|------|------|
| `docker-compose.external-db.yml` | 外部数据库模式 compose 文件，不启动 MySQL 容器，通过 `${DATABASE_URL}` 连接远程数据库 |
| `Makefile` | 新增 `up-external` target；`down` 命令增加清理 external compose |
| `.env` | 实际测试配置（已 gitignore），包含 `DATABASE_URL` |
| `.env.example` | 模板文件，文档化所有可配置项 |

### 测试环境

| 项目 | 值 |
|------|---|
| 远程 MySQL | `9.135.251.193:3306` |
| 用户 | `test123` |
| 数据库 | `wetty_terminal`（测试前需手动创建） |
| 连接方式 | `DATABASE_URL=mysql+aiomysql://...` 环境变量注入 |

### 测试流程 & 结果

| 步骤 | 命令 | 结果 |
|------|------|------|
| 1. 检查 MySQL 可达性 | `nc -z 9.135.251.193 3306` | ✅ 连接成功 |
| 2. 创建数据库 | `CREATE DATABASE wetty_terminal ...` | ✅ |
| 3. 构建 + 启动 | `make up-external` | ✅ 容器正常启动 |
| 4. 数据库初始化 | 自动 `init_db()` → `CREATE TABLE` | ✅ hosts 表已创建 |
| 5. hosts.yaml 同步 | 启动时自动同步 | ✅ 8 条记录写入 MySQL |
| 6. Health 端点 | `/health` → `{"database":"mysql"}` | ✅ |
| 7. 登录认证 | POST `/api/auth/login` | ✅ 返回 JWT token |
| 8. API 鉴权 | GET `/api/hosts` (Bearer token) | ✅ 返回主机列表 |
| 9. 树形结构 | `parent_id` 外键关系 | ✅ ROOT + NESTED 正确 |

### 已知问题 & 备注

1. **数据库需预先存在**：`init_db()` 只创建表，不创建数据库本身。首次使用需手动 `CREATE DATABASE`。
   - 后续可在 `_build_database_url()` 中添加自动创建数据库逻辑
2. **容器内无 curl**：验证需使用 `python -c "import urllib.request; ..."` 或安装 curl
3. **`.env` 中的密码特殊字符**：`Test123456.` 中的 `.` 在 URL 中安全，无需额外编码

### Makefile 最终命令列表（已过期，见"配置简化实施记录"）

> ⚠️ 以下为 Sprint 1 时的命令列表，后续配置简化中已精简。最新列表见文档末尾。

```
make help            — 查看所有可用命令
make init            — 首次部署初始化（创建 .env）
make up              — 启动服务（本地 MySQL 模式）
make up-sqlite       — 启动服务（SQLite 模式）
make up-external     — 启动服务（外部数据库模式）
make down            — 停止服务
make restart         — 重启服务
make logs / logs-f   — 查看/跟踪日志
make build / rebuild — 构建 Docker 镜像
make ps / status     — 查看容器状态
make mysql-shell     — 进入 MySQL 终端
make db-migrate      — SQLite → MySQL 数据迁移
make password-hash   — 生成密码哈希
make password-reset  — 重置登录密码
make dev             — 本地后端开发
make dev-frontend    — 本地前端开发
make clean           — 清理所有数据
```

---

## Sprint 2 实施记录：认证入库 + 密码重置 CLI

> **日期**：2026-05-18
> **目标**：认证配置从 auth.yaml 迁移到数据库，Refresh Token 持久化，密码重置 CLI

### 核心架构变更

```
Before (Sprint 1):
  config/auth.yaml → AuthService (内存) → JWT 签发
  Refresh Token: 内存 dict（重启丢失）
  密码修改: 写回 auth.yaml + 清空内存

After (Sprint 2):
  config/auth.yaml → [种子初始化，仅首次] → DB auth_config 表
  AuthService: 从 DB 加载配置（启动时）+ 登录失败时自动刷新
  Refresh Token: DB refresh_tokens 表（SHA-256 哈希存储）
  密码重置: CLI 直连 DB + 服务自动感知（无需重启）
```

### 新建文件

| 文件 | 说明 |
|------|------|
| `src/models/auth.py` | ORM 模型：`AuthConfigModel`（单行配置表）+ `RefreshTokenModel`（Token 持久化表） |
| `src/utils/reset_password.py` | 密码重置 CLI：`python -m src.utils.reset_password "new-password"` |
| `src/api/settings.py` | 设置 API：`GET/PUT /api/settings/auth`、`GET /api/settings/database` |

### 重构文件

| 文件 | 改动说明 |
|------|----------|
| `src/services/auth_service.py` | 完全重写：DB 持久化、`init_from_db()` + YAML 种子、`authenticate_and_persist()` 自动刷新、Token Rotation DB 版、`cleanup_expired_tokens()` |
| `src/api/auth.py` | 所有 refresh token 操作改为异步 DB 持久化（注入 `session: AsyncSession`） |
| `src/main.py` | auth 初始化改为 `init_from_db()`、去掉 auth.yaml 监听、添加 token 清理任务、注册 settings 路由 |
| `src/models/database.py` | 导入 auth 模型确保 `create_all` 创建新表 |
| `src/models/__init__.py` | 导出 `AuthConfigModel`、`RefreshTokenModel` |
| `src/utils/password_hash.py` | 更新提示文本（推荐使用 reset_password） |
| `Makefile` | `password-reset` 命令支持 external 模式 |

### 数据库新增表

#### auth_config（单行配置表）

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INT PK | 固定 = 1 |
| enabled | BOOL | 是否启用密码保护 |
| password_hash | TEXT | bcrypt 哈希 |
| jwt_secret | VARCHAR(256) | JWT 签名密钥 |
| access_token_expire_hours | FLOAT | Access Token 过期时间 |
| refresh_token_expire_days | INT | Refresh Token 过期时间 |
| updated_at | DATETIME | 最后更新时间 |

#### refresh_tokens（Token 持久化表）

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INT PK AUTO | 自增主键 |
| token_hash | VARCHAR(128) UNIQUE | SHA-256 哈希（不存明文） |
| expires_at | DATETIME | 过期时间 |
| revoked | BOOL | 是否已撤销 |
| created_at | DATETIME | 创建时间 |

### 设计亮点

1. **YAML 降级为种子**：首次启动从 auth.yaml 初始化到 DB，后续以 DB 为 SSOT
2. **Token 安全存储**：数据库只存 SHA-256 哈希，不存明文 refresh token
3. **CLI 无需重启生效**：`authenticate_and_persist()` 首次失败时自动从 DB 刷新配置
4. **Token Rotation**：每次刷新旧 token 标记 revoked + 签发新 token
5. **定期清理**：每小时自动清理过期/已撤销的 refresh token
6. **设置 API**：提供脱敏的配置查询 + 部分字段更新

### 验证结果

| 验证项 | 结果 |
|--------|------|
| Python 编译（9 个文件） | ✅ 全部通过 |
| 首次启动 YAML 种子 → DB | ✅ `认证配置从 auth.yaml 种子初始化到数据库` |
| 重启从 DB 加载 | ✅ `认证配置从数据库加载完成: 启用` |
| 登录认证 | ✅ bcrypt 密码验证 → JWT + refresh token 签发 |
| Token Rotation | ✅ 旧 token 撤销，新 token 签发 |
| 旧 token 拒绝 | ✅ 401 Unauthorized |
| 注销 | ✅ 204 No Content |
| 设置查询 API | ✅ 脱敏返回配置信息 |
| 数据库状态 API | ✅ 返回 type + 脱敏 URL |
| CLI 密码重置 | ✅ 直连 DB 更新 + 吊销所有 token |
| CLI 重置后无需重启 | ✅ 下次登录自动感知新密码 |
| Refresh Token 持久化 | ✅ DB 中可见 token_hash + revoked 状态 |
| Token 清理后台任务 | ✅ 启动日志 `启动 Refresh Token 定期清理（间隔 3600s）` |

### 新增 API 端点

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| GET | `/api/settings/auth` | 查询认证配置（脱敏） | 需要 |
| PUT | `/api/settings/auth` | 更新认证配置 | 需要 |
| GET | `/api/settings/database` | 查询数据库状态 | 需要 |

### CLI 工具

```bash
# 重置密码（推荐）
python -m src.utils.reset_password "new-password"

# 生成密码哈希（仅查看）
python -m src.utils.password_hash "password"

# Docker 容器内执行
docker compose exec wetty-mcp python -m src.utils.reset_password "new-password"

# Makefile 快捷命令
make password-reset
```

---

## 配置简化实施记录

> **日期**：2026-05-18
> **状态**：✅ 已完成
> **背景**：Sprint 1/2 实施后，发现存在配置冗余和多 compose 文件维护成本问题

### 问题分析

| 问题 | 说明 |
|------|------|
| `config/database.yaml` 冗余 | Docker 方式下 `DATABASE_URL` 环境变量已是唯一入口，yaml 变成无用的第二优先级 |
| 三个 compose 文件 | `docker-compose.yml`(本地MySQL)、`docker-compose.external-db.yml`(远程MySQL)、`docker-compose.sqlite.yml` 本质只是 `DATABASE_URL` 有无和 MySQL 容器有无的区别 |
| Makefile 命令过多 | `up`/`up-sqlite`/`up-external` 三套启动逻辑 + 各种 fallback |

### 简化方案

#### 1. 配置入口统一为 `.env` 中的 `DATABASE_URL`

```
DATABASE_URL 有值 → 连接 MySQL（不管本地还是远程）
DATABASE_URL 为空/未设置 → 使用默认 SQLite
```

- 删除 `config/database.yaml`（含测试密码，不应在仓库中）
- 保留 `config/database.yaml.example` 仅作本地开发文档
- `database.py` 中 yaml 加载逻辑保留（本地开发不用 Docker 的后备），但 Docker 方式统一 `DATABASE_URL`

#### 2. Compose 文件合并

**Before**: 3 个文件
- `docker-compose.yml` — 本地 MySQL + wetty-mcp
- `docker-compose.external-db.yml` — 只有 wetty-mcp
- `docker-compose.sqlite.yml` — 只有 wetty-mcp + SQLite volume

**After**: 1 主 + 1 可选
- `docker-compose.yml` — 只有 wetty-mcp 服务，`DATABASE_URL` 从 `.env` 注入
- `docker-compose.override.yml`（可选，gitignore）— 本地开发时加 MySQL 容器

Docker Compose 自动合并 `docker-compose.yml` + `docker-compose.override.yml`。

#### 3. `.env.example` 简化

```env
# 数据库配置（二选一）
# 设置 DATABASE_URL → MySQL 模式
# 留空/注释掉 → SQLite 模式（默认）
# DATABASE_URL=mysql+aiomysql://user:pass@host:3306/wetty_terminal?charset=utf8mb4

# 应用配置
WETTY_API_TOKEN=
WETTY_PORT=8000
```

#### 4. Makefile 简化

| Before | After |
|--------|-------|
| `make up` (MySQL 模式) | `make up`（统一，行为由 .env 决定） |
| `make up-sqlite` | 删除（`.env` 不设 `DATABASE_URL` 即为 SQLite） |
| `make up-external` | 删除（`.env` 设 `DATABASE_URL` 指向远程即可） |
| `make up-with-mysql` (无) | 新增：`docker compose -f docker-compose.yml -f docker-compose.override.yml up -d` |
| `logs`/`down`/`restart` fallback 逻辑 | 统一操作单个 compose 文件 |

#### 5. 关于"连不上 MySQL 退化为 SQLite"

**决定：不做自动降级。** 理由：
- 数据一致性风险：用户以为连的 MySQL 结果数据写到了 SQLite
- 调试困难：静默降级会掩盖配置错误
- 正确做法：连不上就启动失败，日志明确告知连接错误，让用户修正配置

### 删除文件清单

| 文件 | 处理 |
|------|------|
| `config/database.yaml` | 删除（含测试明文密码） |
| `docker-compose.sqlite.yml` | 删除 |
| `docker-compose.external-db.yml` | 删除 |

### 影响范围

- `docker-compose.yml` — 重写（去掉 MySQL 容器，只保留 wetty-mcp）
- `docker-compose.override.yml.example` — 新建（本地 MySQL 容器模板）
- `.env.example` — 简化
- `Makefile` — 简化
- `src/models/database.py` — 移除 `database.yaml` 加载逻辑，统一 `DATABASE_URL` 单入口

### 实施结果

| 任务 | 状态 |
|------|------|
| 合并 3 个 compose 为 1 个 `docker-compose.yml` | ✅ |
| 创建 `docker-compose.override.yml.example` | ✅ |
| 简化 `.env.example` | ✅ |
| 简化 Makefile（移除 `up-sqlite`/`up-external`/fallback 逻辑） | ✅ |
| 删除 `docker-compose.sqlite.yml` | ✅ |
| 删除 `docker-compose.external-db.yml` | ✅ |
| 删除 `config/database.yaml` + `config/database.yaml.example` | ✅ |
| 重构 `database.py` 移除 yaml 加载逻辑 | ✅ |
| 重建验证全流程 | ✅ |

### 最终 Makefile 命令列表

```
make help              — 查看所有可用命令
make init              — 首次部署初始化（创建 .env）
make up                — 启动服务（.env DATABASE_URL 控制模式）
make up-with-mysql     — 启动服务（含本地 MySQL 容器，需 override 文件）
make down              — 停止服务
make restart           — 重启服务
make logs / logs-f     — 查看/跟踪日志
make build / rebuild   — 构建 Docker 镜像
make ps / status       — 查看容器状态
make password-hash     — 生成密码哈希
make password-reset    — 重置登录密码（直连 DB，无需重启）
make dev               — 本地后端开发
make dev-frontend      — 本地前端开发
make clean             — 清理所有数据
```

### 验证结果

| 验证项 | 结果 |
|--------|------|
| `docker compose up -d --build` | ✅ 构建 + 启动成功 |
| 数据库连接（MySQL via DATABASE_URL） | ✅ |
| 认证配置从 DB 加载 | ✅ |
| hosts.yaml 同步完成 | ✅ |
| `/api/auth/status` 公开端点 | ✅ `{"auth_required": true}` |
| Refresh Token 定期清理启动 | ✅ |
| 服务整体启动成功 | ✅ |

---

## 十、Sprint 3 实施记录 ✅

### 实施内容

| 模块 | 改动 | 文件 |
|------|------|------|
| 后端 — YAML 导入 API | 文件上传 + merge/overwrite 模式 | `src/api/hosts.py` |
| 后端 — YAML 导出 API | 导出 DB 数据为 YAML 下载 | `src/api/hosts.py` |
| 后端 — 路由顺序修复 | 固定路径 `/sync`、`/import`、`/export` 移至 `/{host_id}` 前 | `src/api/hosts.py` |
| 后端 — delete_all_hosts | overwrite 模式清空全部主机 | `src/services/host_manager.py` |
| 前端 — API 服务 | `importHostsYaml`、`exportHostsYaml`、`syncHostsFromYaml` | `frontend/src/services/api.ts` |
| 前端 — 主机管理页面 | 完整组件：树形展示、CRUD、编辑抽屉、导入弹窗 | `frontend/src/components/HostManagePage.tsx` |
| 前端 — 页面路由 | 侧边栏导航标签（终端 / 主机管理）条件渲染 | `frontend/src/App.tsx` |

### 关键设计决策

1. **路由冲突解决**：FastAPI 按注册顺序匹配路由，固定路径 `/export` 会被 `/{host_id}` 拦截。解决方案：将所有固定路径路由注册在路径参数路由之前。

2. **页面路由方案**：使用简单的状态切换（`type Page = "terminal" | "hosts"`）而非 React Router，保持单页面架构简洁。侧边栏导航标签控制 main 区域的条件渲染。

3. **组件架构**：`HostManagePage` 接收 `hosts` 和 `onHostsChange` 作为 props，共享 App 层的数据加载逻辑，避免重复 fetch。

4. **导入双模式**：
   - `merge`：保留现有数据，按 name 匹配更新/新增（默认）
   - `overwrite`：先调用 `delete_all_hosts()` 清空，再全量导入

### 验证结果

| 验证项 | 结果 |
|--------|------|
| TypeScript 编译无错误 | ✅ |
| Vite 前端构建成功 | ✅ |
| Docker 镜像构建成功 | ✅ |
| 服务启动成功 | ✅ |
| `GET /api/hosts/export` 返回 YAML | ✅ (200, application/x-yaml) |
| `POST /api/hosts/sync` 同步完成 | ✅ (200, 新增 0, 更新 7, 删除 0) |
| `POST /api/hosts/import` 路由注册 | ✅ |
| 路由冲突已解决 | ✅ (/export 不再被 /{host_id} 拦截) |

### 后续补充实施

| 模块 | 改动 | 文件 |
|------|------|------|
| Docker — 端口映射移除 | 不再声明 ports，通过容器 IP 直接访问 | `docker-compose.yml` |
| Docker — Makefile ip 命令 | `make ip` 获取容器 IP + 访问 URL | `Makefile` |
| 后端 — YAML 编辑 API | `GET /yaml` 返回纯文本、`PUT /yaml` 接收 JSON 校验后导入 | `src/api/hosts.py` |
| 前端 — YAML 编辑器弹窗 | `YamlEditorModal` 组件（全屏编辑器 + 校验 + merge/overwrite） | `frontend/src/components/HostManagePage.tsx` |
| 前端 — API 函数 | `fetchHostsYaml()`、`updateHostsYaml()` | `frontend/src/services/api.ts` |
| 安全 — 加密密钥持久化 | `WETTY_ENCRYPTION_KEY` 从可选变为必填，注入 docker-compose | `.env`、`.env.example`、`docker-compose.yml` |

**YAML 编辑器组件特性**：
- 全屏 modal，monospace textarea 代码编辑体验
- 打开时自动加载当前配置（`GET /api/hosts/yaml`）
- merge / overwrite 模式切换
- 后端校验 YAML 语法 + `hosts` 键存在性检查
- 错误信息实时展示（支持多行错误列表）
- 行数统计

**加密密钥问题根因**：
- `WETTY_ENCRYPTION_KEY` 未设置时自动生成临时密钥
- 容器重启后密钥变化，导致 MySQL 中已加密的密码无法解密
- 修复：生成固定密钥写入 `.env`，通过 docker-compose 注入环境变量

### 遗留项

| 项目 | 优先级 | 说明 |
|------|--------|------|
| 移除 `_watch_hosts_yaml` 热加载 | 低 | DB 已为 SSOT，热加载可保留作兼容 |
| credentials 表 + API | 中 | 设计文档 7.3 节，共享凭据管理 |
| 节点拖拽排序（move API） | 低 | 设计文档 6.2 节规划的 `/api/hosts/{id}/move` |
| 节点复制（duplicate API） | 低 | 设计文档 6.2 节规划的 `/api/hosts/{id}/duplicate` |

---

## 十一、前端 UX 优化实施记录

### 优化内容

| 优化项 | 改动 | 文件 |
|--------|------|------|
| **P0 — entry_steps 编辑器** | `StepsEditor` 组件：可视化增/删/排序交互步骤（wait正则 + send内容 + timeout） | `HostManagePage.tsx` |
| **P1 — 顶栏重构** | 搜索集成到顶栏（节省垂直空间）；YAML编辑/导入/导出收入 dropdown；新增按钮独立突出 | `HostManagePage.tsx` |
| **P1 — 状态指示条** | 节点左侧彩色竖线：green=活跃, amber=待下线, red=禁用；disabled 节点整行降低透明度 | `HostManagePage.tsx` |
| **P1 — 标签可视化** | 节点行始终展示前2个标签（非搜索时也可见） | `HostManagePage.tsx` |
| **P1 — 根节点视觉区分** | 根节点图标使用蓝色背景高亮，与嵌套节点区分 | `HostManagePage.tsx` |
| **P2 — 侧边栏利用** | 主机管理模式下显示：节点状态统计（active/deprecated/disabled 计数）+ 快捷操作说明 | `App.tsx` |

### 设计决策

1. **entry_steps 编辑器**：
   - 每个 step 是独立的 card，包含 wait（正则）、send（文本）、timeout（秒）三字段
   - 支持上移/下移重排序，支持删除
   - `{{password}}` 变量有 inline 提示说明
   - 空状态有引导说明

2. **Dropdown 收纳**：
   - 将 3 个低频操作（YAML编辑/导入/导出）收进"⚙ 批量操作"dropdown
   - 保留"+ 新增根节点"作为唯一 primary action（符合 Fitts's Law）
   - 点击外部区域自动关闭

3. **状态指示条设计**：
   - 使用 `absolute` 定位在节点行最左侧（left-1）
   - 活跃节点低透明度（不抢注意力），异常状态高透明度
   - 配合 disabled 节点的 `opacity-50` 整行弱化

### 验证结果

| 验证项 | 结果 |
|--------|------|
| TypeScript 编译无错误 | ✅ |
| Lint 检查通过 | ✅ |
| Vite 前端构建成功 | ✅ |

---

## 十二、共享凭据管理功能

### 需求

- 在 Web 页面上管理 `credentials` 信息（如 `tce-server-login`）
- 主机节点通过 `credential_ref` 引用共享凭据
- 凭据密码加密存储，API 不返回明文

### 技术方案

**方案 A：凭据持久化到数据库**

1. 新增 `credentials` 数据库表
2. 后端 CRUD API（`/api/credentials`）
3. YAML 同步时 upsert 到 credentials 表
4. 前端凭据管理页面 + credential_ref 下拉选择器

### 数据库设计

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `name` | VARCHAR(128) UNIQUE | 凭据名称 |
| `password_encrypted` | TEXT | Fernet 加密后的密码 |
| `description` | TEXT NULL | 凭据用途描述 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

### API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/credentials` | 凭据列表（脱敏，含引用数） |
| GET | `/api/credentials/names` | 名称列表（下拉选择用） |
| POST | `/api/credentials` | 创建凭据 |
| PUT | `/api/credentials/{id}` | 更新凭据 |
| DELETE | `/api/credentials/{id}` | 删除凭据（被引用时拒绝） |

### 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/models/credential.py` | 新增 | ORM 模型 + Pydantic Schema |
| `src/services/credential_service.py` | 新增 | 业务逻辑（CRUD + upsert） |
| `src/api/credentials.py` | 新增 | REST API 路由 |
| `src/models/database.py` | 修改 | 导入 Credential 模型注册到 metadata |
| `src/services/host_manager.py` | 修改 | sync_from_yaml 时 upsert credentials |
| `src/main.py` | 修改 | 导入并注册 credentials 路由 |
| `frontend/src/services/api.ts` | 修改 | 新增 credential API 调用函数 + Host 接口添加 credential_ref |
| `frontend/src/components/CredentialManagePanel.tsx` | 新增 | 凭据管理页面组件 |
| `frontend/src/components/HostManagePage.tsx` | 修改 | CredentialRefField 下拉选择器 |
| `frontend/src/App.tsx` | 修改 | 导航增加"凭据"标签 + 渲染分支 |

### 前端交互

1. **导航栏**：新增「🔑 凭据」标签页（与终端、主机并列）
2. **凭据管理页面**：列表 + 新增/编辑/删除弹窗
3. **主机编辑表单**：`credential_ref` 字段改为 ComboBox（下拉选择 + 可手动输入）
4. **编辑模式**：加载已有 host 的 `credential_ref` 值

### 构建验证

| 验证项 | 结果 |
|--------|------|
| TypeScript 编译无错误 | ✅ |
| Python 语法检查通过 | ✅ |
| Vite 前端构建成功 | ✅ (613KB JS, 61KB CSS) |

---

## 十三、启动同步重构——首次初始化模式

### 背景

既然页面已支持完整的 YAML 编辑和凭据管理，容器启动时自动从 `hosts.yaml` 同步的行为可能会覆盖用户在页面上的编辑。需要将自动同步改为"首次初始化"模式。

### 设计方案

| 场景 | 行为 |
|------|------|
| 数据库为空（首次部署） | 自动从 `hosts.yaml` 导入种子数据 |
| 数据库已有数据 | 跳过自动同步，打印 info 日志提示 |
| 手动 API 调用 `POST /api/hosts/sync` | 始终强制执行同步（不受首次初始化限制） |
| 页面 YAML 编辑保存 | 直接写入 DB，不涉及文件 |

### 文件监听控制

| 环境变量 | 默认值 | 行为 |
|----------|--------|------|
| `WETTY_WATCH_YAML` | 空（关闭） | 不监听 `hosts.yaml` 文件变更 |
| `WETTY_WATCH_YAML=true` | 启用 | 检测到文件变更时自动强制同步到 DB |

适用场景：
- **默认（关闭）**：以页面管理为主的用户，避免文件变更意外覆盖数据
- **启用**：以 YAML 文件为主要管理方式的用户（如 GitOps 工作流）

### 代码变更

| 文件 | 变更 |
|------|------|
| `src/services/host_manager.py` | 新增 `has_hosts()` 方法检查 DB 是否已有数据 |
| `src/main.py` | `_sync_hosts_from_yaml(force=False)` 增加首次初始化判断逻辑 |
| `src/main.py` | 文件监听启动改为 `WETTY_WATCH_YAML` 环境变量控制 |
| `.env.example` | 新增 `WETTY_WATCH_YAML` 配置说明 |
| `docker-compose.yml` | 添加 `WETTY_WATCH_YAML` 环境变量传递 |

### 验证

| 验证项 | 结果 |
|--------|------|
| Python 语法检查通过 | ✅ |
| 首次启动（DB 空）→ 自动同步 | 按设计 |
| 重启（DB 有数据）→ 跳过同步 | 按设计 |
| 手动 API 同步始终可用 | 按设计 |
| `WETTY_WATCH_YAML=true` 时文件监听启动 | 按设计 |
| 默认不启动文件监听 | 按设计 |

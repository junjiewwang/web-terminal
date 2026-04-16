# Troubleshoot Snippets 系统 — 抽象设计文档

## 1. 需求概述

### 1.1 背景

[troubleshoot-snippets](https://git.woa.com/junjiewwang/troubleshoot-snippets) 仓库包含按技术领域组织的排障脚本集合（ES、K8s、MySQL、Redis），每个领域有一个 `.sh` 脚本文件和一个 `README.md`。

需要将这些排障脚本集成到 `wetty-mcp-terminal`，实现：

1. **浏览器端**：Web 终端页面上提供快速命令面板，用户点击即可执行排障命令
2. **Agent 端**：AI Agent 通过 MCP 工具也能使用相同的排障脚本和命令

### 1.2 核心设计目标

| 目标 | 说明 |
|------|------|
| **可扩展性** | 新增领域（如 Nginx、Kafka）只需添加配置 + 脚本文件，零代码改动 |
| **DRY 复用** | 浏览器 UI 和 MCP Agent 共享同一份 Snippet 数据源和执行逻辑 |
| **配置驱动** | 参照 `hosts.yaml` SSOT 模式，所有 Snippet 元数据由 YAML 定义 |
| **松耦合** | Snippet 系统作为独立模块，不侵入现有终端管理核心逻辑 |

---

## 2. 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    config/snippets.yaml  (SSOT)                  │
│            + config/snippets/*.sh  (脚本文件)                     │
└──────────────────────┬───────────────────────────────────────────┘
                       │ 启动加载 / 热加载
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                   SnippetRegistry (核心注册表)                     │
│                                                                  │
│  - load_from_yaml(path)     # 加载 YAML 配置                     │
│  - list_domains()           # 列出所有领域                        │
│  - get_domain(domain_id)    # 获取领域详情                        │
│  - get_commands(domain_id)  # 获取领域下所有命令                   │
│  - get_script_content(id)   # 获取脚本内容（用于 heredoc 注入）    │
│  - resolve_command(id, params)  # 解析命令模板                    │
└────────┬─────────────────────────────┬───────────────────────────┘
         │                             │
         ▼                             ▼
┌─────────────────────┐   ┌────────────────────────────────┐
│   REST API Layer     │   │     MCP Tool Layer              │
│                      │   │                                │
│ GET /api/snippets    │   │ @mcp.tool()                    │
│ GET /api/snippets/   │   │   list_snippet_domains()       │
│     {domain}/cmds    │   │   load_snippet_domain(domain)  │
│ GET /api/snippets/   │   │   run_snippet_command(domain,  │
│     {domain}/script  │   │     command, params)            │
└────────┬────────────┘   └────────────┬───────────────────┘
         │                             │
         ▼                             ▼
┌─────────────────────┐   ┌────────────────────────────────┐
│   Frontend UI        │   │     AI Agent (via MCP)          │
│                      │   │                                │
│ SnippetBar (toolbar) │   │ 1. list_snippet_domains()      │
│ SnippetPanel (popup) │   │ 2. load_snippet_domain("es")   │
│   → sendInput(cmd)   │   │ 3. run_snippet_command(...)    │
└──────────────────────┘   └────────────────────────────────┘
```

### 2.1 分层职责

| 层 | 职责 | 变更频率 |
|----|------|----------|
| **Config 层** | YAML 元数据 + `.sh` 脚本文件 | 新增领域时改动 |
| **Registry 层** | 解析配置、管理 Snippet 数据、模板渲染 | 极少变动（核心稳定） |
| **API 层** | REST 端点供前端消费 | 极少变动 |
| **MCP 层** | Agent 工具定义，委托给 Registry | 极少变动 |
| **Frontend 层** | UI 组件，从 API 获取数据 | 极少变动 |

> **新增领域时只需改 Config 层**（添加 YAML 配置项 + 放入 `.sh` 文件），其他所有层零改动。

---

## 3. 详细设计

### 3.1 Config 层 — `config/snippets.yaml` + `config/snippets/`

#### 3.1.1 目录结构

```
config/
├── hosts.yaml            # 已有的主机配置 SSOT
├── snippets.yaml         # Snippet 元数据 SSOT
└── snippets/             # 各领域脚本文件
    ├── es-snippet.sh
    ├── k8s-image-snippet.sh
    ├── mysql-snippet.sh
    └── redis-snippet.sh
```

#### 3.1.2 `snippets.yaml` 配置格式

```yaml
# Troubleshoot Snippets 配置
# 本文件是 Snippet 元数据的 Single Source of Truth
# 新增领域只需在 domains 下添加一项 + 放入脚本文件

domains:
  - id: es
    name: Elasticsearch
    icon: 🔍              # 前端展示用图标（emoji 或 icon class）
    description: Elasticsearch 集群排障工具集
    script_file: snippets/es-snippet.sh   # 相对于 config/ 的路径
    tags:
      - search
      - database
    commands:
      - id: es
        name: 集群概览
        description: 查看集群健康状态、节点数、分片数
        syntax: "es [host:port]"
        template: "es {{endpoint}}"
        params:
          - name: endpoint
            description: "ES 地址，格式 host:port"
            default: "localhost:9200"
            required: false

      - id: esl
        name: 索引列表
        description: 按大小排序列出所有索引
        syntax: "esl [host:port]"
        template: "esl {{endpoint}}"
        params:
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: esr
        name: 恢复中的索引
        description: 查看正在恢复的索引状态
        syntax: "esr [host:port]"
        template: "esr {{endpoint}}"
        params:
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: esq
        name: 查询索引
        description: 查询指定索引的前 N 条文档
        syntax: "esq index [size] [host:port]"
        template: "esq {{index}} {{size}} {{endpoint}}"
        params:
          - name: index
            description: 索引名称
            required: true
          - name: size
            default: "3"
            required: false
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: esm
        name: 集群 Mapping
        description: 查看指定索引的 Mapping
        syntax: "esm index [host:port]"
        template: "esm {{index}} {{endpoint}}"
        params:
          - name: index
            description: 索引名称
            required: true
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: esn
        name: 节点信息
        description: 查看集群节点详细信息
        syntax: "esn [host:port]"
        template: "esn {{endpoint}}"
        params:
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: ess
        name: 分片分布
        description: 查看指定索引的分片分布
        syntax: "ess index [host:port]"
        template: "ess {{index}} {{endpoint}}"
        params:
          - name: index
            description: 索引名称
            required: true
          - name: endpoint
            default: "localhost:9200"
            required: false

      - id: esa
        name: 别名列表
        description: 查看所有索引别名
        syntax: "esa [host:port]"
        template: "esa {{endpoint}}"
        params:
          - name: endpoint
            default: "localhost:9200"
            required: false

  - id: k8s
    name: Kubernetes
    icon: ☸️
    description: Kubernetes 镜像版本排查工具
    script_file: snippets/k8s-image-snippet.sh
    tags:
      - container
      - orchestration
    commands:
      - id: ki
        name: 镜像列表
        description: 按 owner/pod 列出命名空间内镜像
        syntax: "ki namespace [owner|pod]"
        template: "ki {{namespace}} {{group_by}}"
        params:
          - name: namespace
            description: K8s 命名空间
            required: true
          - name: group_by
            description: 分组方式 (owner 或 pod)
            default: "owner"
            required: false

      - id: kic
        name: 镜像版本对比
        description: 对比两个命名空间的镜像版本差异
        syntax: "kic ns1 ns2"
        template: "kic {{ns1}} {{ns2}}"
        params:
          - name: ns1
            description: 第一个命名空间
            required: true
          - name: ns2
            description: 第二个命名空间
            required: true

  - id: mysql
    name: MySQL
    icon: 🐬
    description: MySQL 数据库排障工具集
    script_file: snippets/mysql-snippet.sh
    tags:
      - database
      - rdbms
    commands:
      - id: my
        name: 快速查询
        description: MySQL 快速排障查询
        syntax: "my [options]"
        template: "my {{options}}"
        params:
          - name: options
            description: 查询选项
            required: false
            default: ""

  - id: redis
    name: Redis
    icon: 🔴
    description: Redis 排障工具集
    script_file: snippets/redis-snippet.sh
    tags:
      - database
      - cache
    commands:
      - id: rd
        name: Redis 诊断
        description: Redis 快速诊断
        syntax: "rd [host:port]"
        template: "rd {{endpoint}}"
        params:
          - name: endpoint
            default: "localhost:6379"
            required: false
```

#### 3.1.3 设计要点

- **声明式配置**：每个领域的 `commands` 列出所有可用命令及其参数元数据
- **模板化**：`template` 字段使用 `{{param_name}}` 占位符，由 Registry 层渲染
- **自描述**：每个命令有 `description`、`syntax` 等元数据，前端 UI 和 Agent docstring 均可直接消费
- **脚本分离**：`.sh` 文件独立存放，`script_file` 字段指向实际路径，支持 heredoc 注入到远端

---

### 3.2 Registry 层 — `src/services/snippet_registry.py`

核心类，负责解析 YAML、管理 Snippet 数据、提供查询接口。

#### 3.2.1 数据模型（Pydantic）

```python
# src/models/snippet.py

from pydantic import BaseModel

class SnippetParam(BaseModel):
    """命令参数定义"""
    name: str
    description: str = ""
    default: str = ""
    required: bool = False

class SnippetCommand(BaseModel):
    """单个命令定义"""
    id: str
    name: str
    description: str = ""
    syntax: str = ""
    template: str = ""
    timeout: int | None = None          # 命令级超时（秒），None 时回退到领域级
    params: list[SnippetParam] = []

class SnippetDomain(BaseModel):
    """领域定义"""
    id: str
    name: str
    icon: str = "📦"
    description: str = ""
    script_file: str = ""
    script_sha256: str | None = None    # 可选完整性校验
    default_timeout: int | None = None  # 领域级默认超时（秒），None 时回退到全局 30s
    tags: list[str] = []
    commands: list[SnippetCommand] = []

class SnippetsConfig(BaseModel):
    """顶层配置"""
    domains: list[SnippetDomain] = []
```

#### 3.2.2 SnippetRegistry 类

```python
# src/services/snippet_registry.py

import hashlib
import re

class SnippetRegistry:
    """Snippet 核心注册表
    
    Single Source of Truth 的内存表示，
    被 REST API 和 MCP Tools 共同依赖。
    支持热加载（watchfiles 监听 snippets.yaml 变更）。
    """
    
    _GLOBAL_DEFAULT_TIMEOUT = 30
    
    # 危险命令模式（加载脚本时扫描）
    _DANGEROUS_PATTERNS = [
        re.compile(r'\brm\s+-rf\s+/'),         # rm -rf /
        re.compile(r'\bmkfs\b'),                # 格式化磁盘
        re.compile(r'\bdd\s+if='),              # dd 覆写
        re.compile(r'>\s*/dev/sd'),             # 直写磁盘设备
        re.compile(r'\bcurl\b.*\|\s*bash'),     # curl | bash
        re.compile(r'\bwget\b.*\|\s*bash'),     # wget | bash
        re.compile(r'\bchmod\s+777\s+/'),       # 开放根目录权限
    ]
    
    def __init__(self) -> None:
        self._domains: dict[str, SnippetDomain] = {}
        self._config_path: Path | None = None
        self._script_base: Path | None = None
    
    # ── 加载 ──
    
    def load_from_yaml(self, yaml_path: str | Path) -> None:
        """从 YAML 文件加载配置（启动时 + 热加载时调用）"""
        path = Path(yaml_path)
        self._config_path = path
        self._script_base = path.parent
        
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        
        config = SnippetsConfig.model_validate(raw or {})
        
        # 安全审计：校验每个脚本文件
        for domain in config.domains:
            if domain.script_file:
                self._audit_script(domain)
        
        self._domains = {d.id: d for d in config.domains}
        logger.info("Snippet Registry 加载完成: %d 个领域", len(self._domains))
    
    def reload(self) -> None:
        """热加载：重新读取 YAML（由 watchfiles 回调触发）"""
        if self._config_path:
            self.load_from_yaml(self._config_path)
    
    # ── 查询 ──
    
    def list_domains(self) -> list[SnippetDomain]:
        """列出所有领域"""
        return list(self._domains.values())
    
    def get_domain(self, domain_id: str) -> SnippetDomain | None:
        """获取领域完整信息"""
        return self._domains.get(domain_id)
    
    def get_command(self, domain_id: str, command_id: str) -> SnippetCommand | None:
        """获取单个命令定义"""
        domain = self._domains.get(domain_id)
        if not domain:
            return None
        return next((c for c in domain.commands if c.id == command_id), None)
    
    def get_script_content(self, domain_id: str) -> str | None:
        """读取领域脚本文件内容"""
        domain = self._domains.get(domain_id)
        if not domain or not domain.script_file or not self._script_base:
            return None
        script_path = self._script_base / domain.script_file
        if not script_path.exists():
            return None
        return script_path.read_text(encoding="utf-8")
    
    # ── 命令渲染 ──
    
    def resolve_command(
        self, domain_id: str, command_id: str, params: dict[str, str] | None = None
    ) -> str | None:
        """解析命令模板，用实际参数替换占位符。
        
        required 参数缺失时返回 None（由调用方处理错误提示）。
        """
        cmd = self.get_command(domain_id, command_id)
        if not cmd:
            return None
        
        resolved = cmd.template
        actual_params = params or {}
        
        for p in cmd.params:
            value = actual_params.get(p.name, p.default)
            if p.required and not value:
                return None  # required 参数缺失
            resolved = resolved.replace(f"{{{{{p.name}}}}}", value)
        
        return resolved.strip()
    
    # ── 超时 ──
    
    def get_timeout(self, domain_id: str, command_id: str) -> int:
        """获取命令超时（秒）。优先级：命令级 > 领域级 > 全局默认"""
        domain = self._domains.get(domain_id)
        if not domain:
            return self._GLOBAL_DEFAULT_TIMEOUT
        cmd = next((c for c in domain.commands if c.id == command_id), None)
        if cmd and cmd.timeout:
            return cmd.timeout
        return domain.default_timeout or self._GLOBAL_DEFAULT_TIMEOUT
    
    # ── 探测 ──
    
    def get_probe_command(self, domain_id: str) -> str | None:
        """获取检测脚本是否已加载的探测命令"""
        domain = self._domains.get(domain_id)
        if not domain or not domain.commands:
            return None
        first_cmd = domain.commands[0].id
        return f"type {first_cmd} 2>/dev/null && echo '__SNIPPET_LOADED__' || echo '__SNIPPET_NOT_LOADED__'"
    
    # ── Heredoc 注入 ──
    
    def build_heredoc_loader(self, domain_id: str) -> str | None:
        """生成 heredoc 注入命令，将脚本加载到远端 /tmp"""
        content = self.get_script_content(domain_id)
        if not content:
            return None
        script_name = f"ts-{domain_id}.sh"
        return (
            f"cat << 'SNIPPET_EOF' > /tmp/{script_name}\n"
            f"{content}\n"
            f"SNIPPET_EOF\n"
            f"source /tmp/{script_name}"
        )
    
    # ── 安全审计 ──
    
    def _audit_script(self, domain: SnippetDomain) -> None:
        """加载时审计脚本文件安全性"""
        if not self._script_base:
            return
        script_path = self._script_base / domain.script_file
        if not script_path.exists():
            logger.warning("脚本文件不存在: %s", script_path)
            return
        
        content = script_path.read_text(encoding="utf-8")
        
        # 层1：危险命令检测
        for pattern in self._DANGEROUS_PATTERNS:
            match = pattern.search(content)
            if match:
                raise ValueError(
                    f"脚本安全审计失败: {domain.id} ({domain.script_file}) "
                    f"包含危险命令: {match.group()}"
                )
        
        # 层2：SHA256 完整性校验（可选）
        if domain.script_sha256:
            actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual_sha256 != domain.script_sha256:
                raise ValueError(
                    f"脚本完整性校验失败: {domain.id} ({domain.script_file}) "
                    f"期望 SHA256={domain.script_sha256}, 实际={actual_sha256}"
                )
```

#### 3.2.3 设计要点

- **纯数据服务**：SnippetRegistry 只管理 Snippet 元数据和脚本内容，不依赖 TerminalManager
- **无状态查询**：所有方法都是纯查询，不产生副作用
- **命令执行**由上层（MCP Tool / REST API / Frontend）自行通过 `sendInput` 或 `send_command` 完成
- **模板渲染**：`resolve_command()` 负责参数替换，支持默认值回退

---

### 3.3 REST API 层 — `src/api/snippets.py`

为前端提供 Snippet 数据的 REST 端点。

```python
# src/api/snippets.py

from fastapi import APIRouter, HTTPException, status
from src.services.snippet_registry import SnippetRegistry

router = APIRouter(prefix="/api/snippets", tags=["snippets"])

# 由 main.py 注入
snippet_registry: SnippetRegistry | None = None

def _get_registry() -> SnippetRegistry:
    if snippet_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Snippet Registry 未初始化",
        )
    return snippet_registry


@router.get("")
async def list_domains():
    """列出所有 Snippet 领域"""
    reg = _get_registry()
    domains = reg.list_domains()
    return {
        "domains": [
            {
                "id": d.id,
                "name": d.name,
                "icon": d.icon,
                "description": d.description,
                "tags": d.tags,
                "command_count": len(d.commands),
            }
            for d in domains
        ]
    }


@router.get("/{domain_id}")
async def get_domain_detail(domain_id: str):
    """获取指定领域的详细信息（含所有命令）"""
    reg = _get_registry()
    domain = reg.get_domain(domain_id)
    if not domain:
        raise HTTPException(status_code=404, detail=f"领域不存在: {domain_id}")
    return domain.model_dump()


@router.get("/{domain_id}/script")
async def get_domain_script(domain_id: str):
    """获取领域脚本的 heredoc 加载命令"""
    reg = _get_registry()
    loader = reg.build_heredoc_loader(domain_id)
    if not loader:
        raise HTTPException(status_code=404, detail=f"脚本不存在: {domain_id}")
    return {"domain_id": domain_id, "loader_command": loader}
```

---

### 3.4 MCP Tool 层 — 在 `src/mcp_server/server.py` 中注册

MCP 工具直接委托给 `SnippetRegistry`，Agent 通过工具获取 Snippet 信息并执行命令。

```python
# 新增全局引用（在 server.py 顶部）
_snippet_registry: SnippetRegistry | None = None

# init_mcp_server 中注入
def init_mcp_server(
    terminal_manager, tmux_manager=None, snippet_registry=None
):
    global _terminal_manager, _tmux_manager, _snippet_registry
    _terminal_manager = terminal_manager
    _tmux_manager = tmux_manager or TmuxWindowManager()
    _snippet_registry = snippet_registry
    ...


# ── Snippet 工具 ──

@mcp.tool()
async def list_snippet_domains() -> str:
    """列出所有可用的排障脚本领域。
    
    返回所有已配置的排障工具域（如 Elasticsearch、Kubernetes、MySQL、Redis 等），
    每个域包含名称、描述、可用命令数等信息。
    
    使用流程：
    1. 先调用本工具查看有哪些领域
    2. 调用 load_snippet_domain 加载所需领域的脚本到远端
    3. 调用 run_snippet_command 执行具体命令
    """
    if not _snippet_registry:
        return "❌ Snippet 功能未启用"
    
    domains = _snippet_registry.list_domains()
    result = []
    for d in domains:
        cmds = ", ".join(c.id for c in d.commands)
        result.append(f"• {d.icon} {d.name} ({d.id}): {d.description} [{len(d.commands)} 命令: {cmds}]")
    
    return "可用排障领域：\n" + "\n".join(result)


@mcp.tool()
async def load_snippet_domain(
    session_id: str,
    domain_id: str,
) -> str:
    """将指定领域的排障脚本加载到远端主机。
    
    通过 heredoc 方式将脚本注入到远端 /tmp/ 目录并 source，
    加载后该领域的所有快捷命令即可在终端中直接使用。
    
    Args:
        session_id: 已连接的终端会话 ID
        domain_id: 领域 ID（如 es、k8s、mysql、redis）
    """
    if not _snippet_registry or not _terminal_manager:
        return "❌ 服务未就绪"
    
    loader = _snippet_registry.build_heredoc_loader(domain_id)
    if not loader:
        return f"❌ 领域不存在或脚本缺失: {domain_id}"
    
    session = _terminal_manager.get_session(session_id)
    if not session:
        return f"❌ 会话不存在: {session_id}"
    
    # 通过 send_command 发送 heredoc 加载脚本
    output = await session.send_command(loader, timeout=10)
    
    domain = _snippet_registry.get_domain(domain_id)
    cmds = ", ".join(c.id for c in domain.commands) if domain else ""
    return f"✅ {domain_id} 排障脚本已加载。可用命令: {cmds}\n{output}"


@mcp.tool()
async def run_snippet_command(
    session_id: str,
    domain_id: str,
    command_id: str,
    params: str = "",
) -> str:
    """执行排障脚本中的具体命令。
    
    在已加载脚本的会话中执行指定命令，返回输出结果。
    需要先调用 load_snippet_domain 加载脚本。
    
    Args:
        session_id: 终端会话 ID
        domain_id: 领域 ID
        command_id: 命令 ID（如 es, esl, ki 等）
        params: 命令参数，JSON 格式如 {"endpoint": "10.0.0.1:9200"}，也可为空
    """
    if not _snippet_registry or not _terminal_manager:
        return "❌ 服务未就绪"
    
    # 解析参数
    param_dict = {}
    if params:
        import json
        try:
            param_dict = json.loads(params)
        except json.JSONDecodeError:
            return f"❌ 参数格式错误，需要 JSON: {params}"
    
    # 渲染命令
    resolved = _snippet_registry.resolve_command(domain_id, command_id, param_dict)
    if not resolved:
        return f"❌ 命令不存在: {domain_id}/{command_id}"
    
    session = _terminal_manager.get_session(session_id)
    if not session:
        return f"❌ 会话不存在: {session_id}"
    
    output = await session.send_command(resolved, timeout=30)
    return f"📋 执行: {resolved}\n{output}"
```

### 3.5 Agent 使用流程示例

```
Agent                                   Remote Host
  │                                        │
  │  1. list_snippet_domains()             │
  │  ← "ES, K8s, MySQL, Redis"            │
  │                                        │
  │  2. load_snippet_domain(sid, "es")     │
  │  → cat << 'SNIPPET_EOF' > /tmp/...    │──→ /tmp/ts-es.sh
  │  → source /tmp/ts-es.sh               │──→ 函数加载到 shell
  │  ← "✅ es 已加载"                      │
  │                                        │
  │  3. run_snippet_command(sid, "es",     │
  │       "es", {"endpoint":"10.0:9200"})  │
  │  → es 10.0:9200                        │──→ 执行排障
  │  ← 集群状态输出                         │
```

---

### 3.6 Frontend 层 — 快速命令面板

#### 3.6.1 组件架构

```
TerminalView
├── Terminal (xterm.js)
├── StatusBar (已有)
└── SnippetBar (新增)
    ├── DomainTabs            # 领域切换标签（ES | K8s | MySQL | Redis）
    └── SnippetPanel (弹出)
        ├── LoadScriptButton  # "加载脚本到远端" 按钮
        └── CommandList       # 命令列表，每项可点击执行
            └── CommandItem
                ├── 命令名 + 描述
                ├── ParamInputs (可选参数输入)
                └── ExecuteButton → sendInput(resolved_cmd)
```

#### 3.6.2 数据流

```
1. 页面加载 → fetch("/api/snippets") → 获取领域列表
2. 用户选择领域 → fetch("/api/snippets/{id}") → 获取命令列表
3. 用户点击"加载脚本" → fetch("/api/snippets/{id}/script")
   → 获取 heredoc 命令 → sendInput(loader_command) → 脚本注入远端
4. 用户点击命令 → 本地渲染 template + params → sendInput(resolved_cmd)
```

#### 3.6.3 前端 API Service 扩展

```typescript
// frontend/src/services/api.ts — 新增

export interface SnippetDomainSummary {
  id: string;
  name: string;
  icon: string;
  description: string;
  tags: string[];
  command_count: number;
}

export interface SnippetParam {
  name: string;
  description: string;
  default: string;
  required: boolean;
}

export interface SnippetCommand {
  id: string;
  name: string;
  description: string;
  syntax: string;
  template: string;
  params: SnippetParam[];
}

export interface SnippetDomain extends SnippetDomainSummary {
  script_file: string;
  commands: SnippetCommand[];
}

/** 获取所有 Snippet 领域列表 */
export async function fetchSnippetDomains(): Promise<SnippetDomainSummary[]> {
  const res = await fetchWithRetry(`${API_BASE}/snippets`);
  const data = await res.json();
  return data.domains;
}

/** 获取指定领域详情（含命令列表） */
export async function fetchSnippetDomain(domainId: string): Promise<SnippetDomain> {
  const res = await fetchWithRetry(`${API_BASE}/snippets/${domainId}`);
  return res.json();
}

/** 获取领域的 heredoc 脚本加载命令 */
export async function fetchSnippetLoader(domainId: string): Promise<string> {
  const res = await fetchWithRetry(`${API_BASE}/snippets/${domainId}/script`);
  const data = await res.json();
  return data.loader_command;
}
```

#### 3.6.4 前端命令解析工具（复用 Registry 逻辑）

```typescript
// frontend/src/utils/snippet.ts

/** 在前端解析命令模板（与后端 resolve_command 逻辑对称） */
export function resolveTemplate(
  template: string,
  params: SnippetParam[],
  values: Record<string, string>,
): string {
  let resolved = template;
  for (const p of params) {
    const value = values[p.name] || p.default || "";
    resolved = resolved.replace(`{{${p.name}}}`, value);
  }
  return resolved.trim();
}
```

---

## 4. 扩展性验证 — 新增 Nginx 领域

以新增一个 Nginx 排障领域为例，验证零代码扩展性：

### Step 1：编写脚本文件

```bash
# config/snippets/nginx-snippet.sh

nginx_status() {
  local host=${1:-localhost}
  curl -s "http://${host}/nginx_status"
}

nginx_error_log() {
  local lines=${1:-50}
  tail -n "$lines" /var/log/nginx/error.log
}

nginx_test() {
  nginx -t 2>&1
}
```

### Step 2：在 `snippets.yaml` 中添加配置

```yaml
  - id: nginx
    name: Nginx
    icon: 🌐
    description: Nginx Web 服务器排障工具
    script_file: snippets/nginx-snippet.sh
    tags:
      - web
      - proxy
    commands:
      - id: nginx_status
        name: 连接状态
        description: 查看 Nginx stub_status
        syntax: "nginx_status [host]"
        template: "nginx_status {{host}}"
        params:
          - name: host
            default: "localhost"
            required: false
      - id: nginx_error_log
        name: 错误日志
        description: 查看最近 N 行错误日志
        syntax: "nginx_error_log [lines]"
        template: "nginx_error_log {{lines}}"
        params:
          - name: lines
            default: "50"
            required: false
      - id: nginx_test
        name: 配置检查
        description: 测试 Nginx 配置是否正确
        syntax: "nginx_test"
        template: "nginx_test"
        params: []
```

### Step 3：完成 ✅

无需修改任何 Python 代码、TypeScript 代码或 MCP 工具定义。热加载机制（如启用）会自动识别新领域。

---

## 5. 实施计划

> 详见 [第10节 — 更新后的实施计划](#10-更新后的实施计划)，已包含遗留问题决策后的完整 Sprint 规划。

---

## 6. 与现有系统的集成点

| 集成点 | 方式 | 影响范围 |
|--------|------|----------|
| `main.py` | 添加 `SnippetRegistry` 实例化和注入 | 3-5 行代码 |
| `mcp_server/server.py` | 添加 3 个 `@mcp.tool()` + 注入变量 | ~80 行新增 |
| `api/__init__.py` 或 `main.py` | 注册 snippets router | 1-2 行代码 |
| `TerminalView.tsx` | 添加 `<SnippetBar />` 子组件 | 3-5 行代码 |
| `api.ts` | 新增 3 个 fetch 函数 + 类型 | ~40 行新增 |

> **对现有核心逻辑零侵入**：不修改 `TerminalManager`、`TerminalSession`、`useWebSocket`、`useTerminal` 等核心模块。

---

## 7. 设计决策记录

| 决策 | 选项 | 决定 | 理由 |
|------|------|------|------|
| 配置格式 | JSON / YAML / TOML | **YAML** | 与 `hosts.yaml` 保持一致，复用现有模式 |
| 脚本存储 | 内联 YAML / 独立文件 / Git submodule | **独立文件** | 方便编辑、版本管理、语法高亮 |
| 数据持久化 | YAML only / YAML + DB | **YAML only** | Snippet 是静态配置，无需 DB 运行时状态 |
| 前端数据源 | 硬编码 / 从 API 获取 | **API 获取** | 保证前后端数据一致，新增领域无需前端部署 |
| MCP 工具粒度 | 单一工具 / 三工具 | **三工具** | 符合 Agent 渐进式使用习惯（查看→加载→执行） |
| 脚本注入方式 | SCP / heredoc / Base64 | **heredoc** | 零依赖，通过 PTY 直接注入，兼容堡垒机场景 |
| 模板引擎 | Jinja2 / 简单替换 | **简单替换** | 场景简单，`{{param}}` 替换足够，无需重依赖 |

---

## 8. 遗留问题决策记录

| # | 问题 | 决策 | 设计影响 |
|---|------|------|----------|
| 1 | **热加载** | ✅ 支持 | `snippets.yaml` 参照 `hosts.yaml` 的 watchfiles 热加载机制，修改配置文件后自动重新加载 Registry |
| 2 | **脚本版本管理** | ❌ 不做 | Snippet 是通用能力，只依赖抽象不依赖具体仓库。脚本文件手动维护在 `config/snippets/` 中 |
| 3 | **脚本已加载检测** | ✅ 需要 | 通过 `type <first_command>` 检测，详见 8.1 |
| 4 | **多会话隔离** | ✅ 已确认 | 每个 SSH 连接是独立 shell，脚本加载状态天然隔离，无需额外处理 |
| 5 | **参数校验** | ✅ 需要 | 前端在发送前校验 required 参数，缺失时提示用户，详见 8.2 |
| 6 | **权限控制** | ❌ 不做 | 不基于用户角色限制 Snippet 领域，所有用户可见所有领域 |
| 7 | **脚本安全审计** | ✅ 需要 | 脚本文件加载时进行安全校验（危险命令检测 + SHA256 完整性校验），详见 8.3 |
| 8 | **命令执行超时** | ✅ 需要 | 支持命令级 `timeout` 配置，详见 8.4 |

### 8.1 脚本已加载检测

每个领域在 `snippets.yaml` 中声明第一个命令的 `id` 作为检测函数名，通过在远端执行 `type <function_name>` 来判断脚本是否已加载：

```python
# SnippetRegistry 新增方法
def get_probe_command(self, domain_id: str) -> str | None:
    """获取检测脚本是否已加载的探测命令"""
    domain = self._domains.get(domain_id)
    if not domain or not domain.commands:
        return None
    first_cmd = domain.commands[0].id
    return f"type {first_cmd} 2>/dev/null && echo '__SNIPPET_LOADED__' || echo '__SNIPPET_NOT_LOADED__'"
```

**前端流程**：选择领域时，通过 API 获取探测命令 → `sendInput` 执行 → 解析输出判断是否需要加载脚本。

**MCP 流程**：`load_snippet_domain` 工具内部先执行探测，已加载则跳过 heredoc 注入。

### 8.2 参数校验

前端在点击执行命令前，检查所有 `required: true` 的参数是否已填写：

```typescript
// frontend/src/utils/snippet.ts
export function validateParams(
  params: SnippetParam[],
  values: Record<string, string>,
): string[] {
  const errors: string[] = [];
  for (const p of params) {
    if (p.required && !values[p.name]?.trim()) {
      errors.push(`参数 "${p.name}" 为必填项`);
    }
  }
  return errors;
}
```

后端 `resolve_command` 也增加校验逻辑，required 参数缺失时返回错误而非空字符串。

### 8.3 脚本安全审计

脚本文件加载时进行两层安全校验：

#### 层1：危险命令检测

```python
# 加载脚本时扫描危险模式
_DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\s+/',       # rm -rf /
    r'\bmkfs\b',              # 格式化磁盘
    r'\bdd\s+if=',            # dd 覆写
    r'>\s*/dev/sd',           # 直写磁盘设备
    r'\bcurl\b.*\|\s*bash',   # curl | bash（远程代码执行）
    r'\bwget\b.*\|\s*bash',   # wget | bash
    r'\bchmod\s+777\s+/',     # 开放根目录权限
]
```

#### 层2：SHA256 完整性校验（可选）

`snippets.yaml` 中可选声明脚本文件的 SHA256：

```yaml
domains:
  - id: es
    script_file: snippets/es-snippet.sh
    script_sha256: "a1b2c3d4..."   # 可选，声明后强制校验
```

加载时若 SHA256 不匹配则拒绝加载，防止脚本被篡改。

### 8.4 命令执行超时配置

在 `snippets.yaml` 中支持两级超时配置：

```yaml
domains:
  - id: es
    default_timeout: 15   # 领域级默认超时（秒），不设则用全局默认 30s
    commands:
      - id: esq
        timeout: 60       # 命令级超时（秒），查询可能较慢
      - id: es
        # 未设 timeout，使用领域级 default_timeout: 15s
```

**优先级**：命令级 `timeout` > 领域级 `default_timeout` > 全局默认 `30s`

```python
# SnippetRegistry 新增方法
_GLOBAL_DEFAULT_TIMEOUT = 30

def get_timeout(self, domain_id: str, command_id: str) -> int:
    """获取命令的超时配置"""
    domain = self._domains.get(domain_id)
    if not domain:
        return self._GLOBAL_DEFAULT_TIMEOUT
    cmd = next((c for c in domain.commands if c.id == command_id), None)
    if cmd and cmd.timeout:
        return cmd.timeout
    return domain.default_timeout or self._GLOBAL_DEFAULT_TIMEOUT
```

MCP 工具 `run_snippet_command` 使用此超时值：

```python
timeout = _snippet_registry.get_timeout(domain_id, command_id)
output = await session.send_command(resolved, timeout=timeout)
```

---

## 9. 默认终端 Backend 切换

### 需求

将默认终端 Backend 从 `tmux` 改为 `broker`。

### 改动点

`src/services/terminal_backend.py` 中的常量：

```python
# 改前
DEFAULT_TERMINAL_BACKEND: Final[TerminalBackend] = TerminalBackend.TMUX

# 改后
DEFAULT_TERMINAL_BACKEND: Final[TerminalBackend] = TerminalBackend.BROKER
```

**影响分析**：
- 仅影响未设置 `WETTY_SESSION_BACKEND` 环境变量时的默认行为
- 已通过环境变量明确指定 backend 的部署不受影响
- 前端 `startTerminal` 的 `backend` 参数为 `null` 时会使用此默认值
- MCP `connect_host` 的 `backend` 参数为 `None` 时会使用此默认值

> 此改动纳入 Sprint 1 一起实施，仅改一行常量。

---

## 10. 更新后的实施计划

### Sprint 1 — 核心 Registry + REST API + 默认 Backend ✅

| 任务 | 详情 | 状态 |
|------|------|------|
| 创建 `src/models/snippet.py` | Pydantic 数据模型（含 `timeout`、`script_sha256` 字段） | ✅ |
| 创建 `src/services/snippet_registry.py` | Registry 核心逻辑（含热加载、安全审计、超时解析、探测命令） | ✅ |
| 创建 `config/snippets.yaml` | 配置文件（4 领域 23 命令） | ✅ |
| 拷贝脚本到 `config/snippets/` | 从 troubleshoot-snippets 仓库获取 4 个脚本 | ✅ |
| 创建 `src/api/snippets.py` | REST API 端点（3 个接口） | ✅ |
| 在 `main.py` 中注入 | 启动时加载 + watchfiles 热加载 + 路由注册 | ✅ |
| 修改 `terminal_backend.py` | 默认 Backend 改为 `broker` | ✅ |
| 单元测试 | Registry 加载、模板渲染、安全审计、API 响应 | ⬜ 待补充 |

**验收标准**：
- `GET /api/snippets` 返回领域列表
- `GET /api/snippets/es` 返回命令详情（含超时配置）
- `GET /api/snippets/es/script` 返回探测命令和 heredoc 加载器
- 修改 `snippets.yaml` 或 `snippets/*.sh` 后自动热加载
- 默认终端 Backend 为 `broker`

### Sprint 2 — MCP 工具集成 ✅

| 任务 | 详情 | 状态 |
|------|------|------|
| 在 `server.py` 注册 3 个 MCP 工具 | `list_snippet_domains`, `load_snippet_domain`, `run_snippet_command` | ✅ |
| `load_snippet_domain` 内置探测 | 已加载则跳过 heredoc 注入 | ✅ |
| `run_snippet_command` 使用配置超时 | 从 Registry 获取超时值（三级优先级） | ✅ |
| 在 `init_mcp_server` 中注入 Registry | 依赖注入 + guard 函数 | ✅ |
| 更新 MCP Server instructions | 包含排障脚本工具使用说明 | ✅ |
| 修改 `main.py` 传入 `snippet_registry` | `init_mcp_server(..., snippet_registry=snippet_registry)` | ✅ |
| Agent 端到端测试 | 通过 Agent 加载脚本 + 执行命令 | ⬜ 待验证 |

**验收标准**：Agent 能通过 MCP 工具完整执行排障流程（含自动探测已加载状态）

### Sprint 3 — 前端 UI ✅

| 任务 | 详情 | 状态 |
|------|------|------|
| 前端 API Service 扩展 | `fetchSnippetDomains`/`fetchSnippetDomain`/`fetchSnippetScript` + TypeScript 类型 | ✅ |
| 命令模板解析 + 参数校验 | `resolveSnippetTemplate` + `validateSnippetParams` 工具函数 | ✅ |
| 创建 `SnippetPanel` 组件 | 领域列表 + 命令面板 + 参数输入 + 脚本加载 + 命令执行 | ✅ |
| 集成到 `TerminalView` | 底部面板 + StatusBar toggle 按钮 + `sendInput` 注入 | ✅ |
| SnippetPanel 滚动条样式 | 与现有侧栏风格统一的暗色滚动条 | ✅ |

**验收标准**：用户可在浏览器通过 UI 面板加载脚本并执行排障命令，required 参数缺失时有提示

---

## 状态

- **当前阶段**：Sprint 3 已完成（前端 UI），MCP 鉴权已实施
- **下一步**：端到端验证（启动服务 → 浏览器 UI 操作 → 验证 Snippet 加载和命令执行）

---

## 11. MCP Endpoint 鉴权

### 11.1 背景

MCP endpoint (`/mcp/`) 需要对外公开但需要鉴权保护。客户端在 `mcp.json` 中配置 `headers` 字段传递 Bearer Token，后端统一校验。

### 11.2 改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/main.py` | `auth_middleware` 条件修改 | 移除 `/mcp/` 免认证，使其与 `/api/` 一样需要 Bearer Token |
| `src/mcp_server/server.py` | DNS Rebinding Protection 已关闭 | 之前已完成，安全性由 Header 鉴权保障 |
| `docker-compose.yml` | `WETTY_API_TOKEN` 改为 `${WETTY_API_TOKEN:-}` | 通过环境变量注入 Token，未设置时为开发模式（免认证） |

### 11.3 认证流程

```
MCP Client (mcp.json)                    FastAPI (main.py)
  │                                          │
  │  POST /mcp/ (streamable-http)            │
  │  Header: Authorization: Bearer <token>   │
  │ ──────────────────────────────────────→  │
  │                                          │  auth_middleware:
  │                                          │    path.startswith("/mcp/") → 需要认证
  │                                          │    检查 Bearer Token
  │                                          │    ↓ 匹配 WETTY_API_TOKEN → 放行
  │  ←──────────────────────────────────── │
  │  HTTP 200 (MCP Response)                 │
```

### 11.4 客户端配置示例

```json
{
  "wetty-terminal": {
    "url": "http://<host>:8000/mcp/",
    "transportType": "streamable-http",
    "headers": {
      "Authorization": "Bearer <与 WETTY_API_TOKEN 相同的值>"
    }
  }
}
```

### 11.5 开发模式兼容

当 `WETTY_API_TOKEN` 环境变量**未设置**时，所有路径（包括 `/mcp/`）免认证，开发环境无需配置 Token。

# 堡垒机跳板 + 二级主机（Jump Host）架构设计

> **状态**: ✅ Sprint 1–4 已完成
> **创建日期**: 2026-03-25
> **最后更新**: 2026-03-25
> **关联需求**: 堡垒机场景下，通过预配置的二级主机名称（如 m12）自动编排跳板连接

---

## 一、需求背景

用户通过堡垒机（如 tce-server）中转连接多台目标服务器。当前流程是手动操作：
1. 浏览器/Agent 连接堡垒机
2. 在堡垒机中手动输入目标 IP
3. 可能需要选择账号、输入密码等交互步骤
4. 登录成功后操作目标服务器

**目标**：预配置二级主机（如 `m12` = `10.x.x.3`），前端和 Agent 通过名称直接连接，系统自动编排所有跳板步骤。

---

## 二、核心设计

### 2.1 hosts.yaml 扩展

```yaml
hosts:
  - name: my-bastion
    hostname: <bastion-ip>
    port: 36000
    username: <user>
    auth_type: password
    password: "xxx"
    description: 堡垒机
    type: bastion                              # 新增：主机类型
    tags: [prod]
    ready_pattern: "\\[Host\\]>|Opt>"          # 新增：堡垒机就绪模式
    login_success_pattern: "Last login|\\]#|\\]\\$"  # 新增：登录成功标志
    jump_hosts:                                # 新增：二级主机列表
      - name: m12
        target_ip: "10.x.x.3"
        description: "应用服务 A"
        # login_steps 为空 = 输入 IP 后直接等待登录成功
      - name: m15
        target_ip: "10.x.x.5"
        description: "数据库服务器"
        login_steps:
          - wait: "请选择账号|Select account"
            send: "1"
      - name: m20
        target_ip: "10.x.x.20"
        description: "生产应用服务器"
        login_steps:
          - wait: "password:"
            send: "{{password}}"
        password: "target-password"
```

### 2.2 DB 数据模型

Host 表扩展字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | Enum(direct, bastion) | 主机类型，默认 direct |
| `parent_id` | Integer, FK(hosts.id), nullable | 二级主机指向父堡垒机 |
| `target_ip` | String, nullable | 二级主机的目标 IP |
| `jump_config` | JSON/Text, nullable | 堡垒机配置 (ready_pattern, login_success_pattern) |
| `login_steps` | JSON/Text, nullable | 二级主机的登录步骤链 |

### 2.3 多步登录编排（login_steps）

每个步骤是一个 `wait → send` 原子操作：

```python
@dataclass
class LoginStep:
    wait: str       # 等待出现的正则模式
    send: str       # 匹配后发送的内容
    timeout: float = 15.0

# 变量替换
# {{password}} → jump_host 配置的 password 字段
# {{manual}}   → 需要人工在浏览器终端中输入
```

**容错机制**：每步同时检测 `login_success_pattern`，如果提前匹配到登录成功则跳过剩余步骤。

### 2.4 tmux 多窗口

每个二级主机在堡垒机的 tmux session 中创建独立 window：
- `tmux new-window -t wetty-my-bastion -n "m12"` 创建命名窗口
- `tmux select-window -t wetty-my-bastion:m12` 切换窗口
- 浏览器和 Agent 通过 Tab 切换活跃窗口

### 2.5 连接编排流程

```
connect_host("m12")
  ├─ 1. 查 DB: m12.parent_id → my-bastion (bastion)
  ├─ 2. 确保堡垒机 WeTTY 实例运行中
  │     ├─ 已运行 → 复用
  │     └─ 未运行 → 启动 + 等待 ready_pattern
  ├─ 3. tmux new-window -n "m12" (在堡垒机 session 中)
  ├─ 4. send_input("10.x.x.3\r")
  ├─ 5. 执行 login_steps (如有)
  │     ├─ 每步: wait(pattern) → send(input)
  │     └─ 任意步骤匹配到 login_success_pattern → 跳过剩余
  ├─ 6. 等待 login_success_pattern
  └─ 7. 返回 session_id
```

---

## 三、Sprint 拆解

### Sprint 1：数据模型

| # | 任务 | 涉及文件 | 状态 |
|---|------|----------|------|
| 1.1 | Host ORM 扩展: type, parent_id, target_ip, jump_config, login_steps | `src/models/host.py` | ✅ |
| 1.2 | Pydantic Schema 扩展: HostResponse 支持 children + jump_host 字段 | `src/models/host.py` | ✅ |
| 1.3 | host_manager sync_from_yaml 支持 jump_hosts 嵌套解析 | `src/services/host_manager.py` | ✅ |
| 1.4 | API 返回树形结构（bastion hosts with children） | `src/api/hosts.py` | ✅ |
| 1.5 | 前端 Host 类型扩展 | `frontend/src/services/api.ts` | ✅ |
| 1.6 | hosts.yaml 添加示例二级主机配置 | `config/hosts.yaml` | ✅ |

### Sprint 2：后端连接编排

| # | 任务 | 涉及文件 | 状态 |
|---|------|----------|------|
| 2.1 | LoginStep 模型 + 编排引擎 (_execute_login_steps) | `src/services/jump_orchestrator.py` (新增) | ✅ |
| 2.2 | tmux 多窗口管理函数 | `src/services/tmux_manager.py` (新增) | ✅ |
| 2.3 | connect_host 自动识别 jump_host + 编排跳板 | `src/mcp_server/server.py` | ✅ |
| 2.4 | 新 MCP 工具: switch_window, list_windows | `src/mcp_server/server.py` | ✅ |
| 2.5 | PTYSession 绑定 tmux window | `src/services/pty_session.py` | ✅ |

### Sprint 3：前端 UI

| # | 任务 | 涉及文件 | 状态 |
|---|------|----------|------|
| 3.1 | 树形主机列表（bastion → children 展开/折叠 + 类型图标） | `frontend/src/components/HostList.tsx` | ✅ |
| 3.2 | 终端 Tab 栏（多窗口 Tab + 切换 + 关闭） | `frontend/src/components/TerminalTabs.tsx` (新增) | ✅ |
| 3.3 | App.tsx 集成（Tab 状态管理 + 终端区域联动） | `frontend/src/App.tsx` | ✅ |
| 3.4 | AgentPanel + SSE 事件类型扩展 | `frontend/src/components/AgentPanel.tsx`, `frontend/src/services/api.ts` | ✅ |

---

## 四、实施记录

### Sprint 1：数据模型

**实施日期**: 2026-03-25
**状态**: ✅ 全部完成

#### 1.1 + 1.2 Host ORM & Pydantic Schema (`src/models/host.py`)
- 新增 `HostType` 枚举: `DIRECT`, `BASTION`, `JUMP_HOST`
- ORM 扩展字段: `host_type`, `parent_id`(FK self-ref), `target_ip`, `jump_config`(JSON), `login_steps`(JSON)
- 自引用 ORM 关系: `children`(one-to-many, cascade delete) / `parent`(many-to-one)
- 新 Schema: `LoginStepSchema`, `JumpHostConfigSchema`, `JumpHostYAMLSchema`
- `HostCreate`/`HostUpdate`/`HostResponse` 扩展跳板字段
- `HostResponse.from_orm_model()` 递归构建 children 树

#### 1.3 host_manager sync_from_yaml (`src/services/host_manager.py`)
- 两阶段解析: 顶层主机 → 嵌套 jump_hosts
- 全局 name 唯一性检查（跨顶层 + 所有二级主机）
- per-bastion 子主机增/改/删同步
- 辅助方法: `_list_jump_hosts()`, `_jump_host_needs_update()`, `_build_jump_host_create()`, `_build_jump_host_update()`

#### 1.4 API 树形结构 (`src/api/hosts.py`)
- `list_hosts` 过滤掉 `jump_host` 类型，仅返回顶层主机
- jump_host 通过 bastion 的 `children` 递归返回

#### 1.5 前端类型扩展 (`frontend/src/services/api.ts`)
- 新类型: `HostType`, `LoginStep`, `JumpHostConfig`
- `Host` 接口扩展: `host_type`, `parent_id`, `target_ip`, `jump_config`, `login_steps`, `children`
- `CreateHostRequest` 同步扩展

#### 1.6 hosts.yaml 配置 (`config/hosts.yaml`)
- `tce-server` 升级为 `type: bastion`
- 添加 `ready_pattern`, `login_success_pattern`
- 添加 `jump_hosts` 示例: m12(直连), m15(需选择账号)

### Sprint 2：后端连接编排

**实施日期**: 2026-03-25
**状态**: ✅ 全部完成

#### 2.1 JumpOrchestrator 编排引擎 (`src/services/jump_orchestrator.py`)
- 新增 `JumpResult` 数据类: `success`, `message`, `window_name`, `steps_executed`, `skipped_reason`
- `_resolve_variables()` 函数: 支持 `{{password}}` 密码替换和 `{{manual}}` 人工输入暂停
- `JumpOrchestrator` 核心编排: tmux 窗口创建 → 等待就绪 → 发送目标 IP → 执行 login_steps → 等待登录成功
- 容错: 每步用组合正则 `(?P<step_wait>...)|(?P<login_ok>...)` 同时检测步骤匹配和登录成功
- 辅助方法: `_parse_jump_config()`, `_parse_login_steps()`, `_decrypt_jump_password()`

#### 2.2 TmuxWindowManager (`src/services/tmux_manager.py`)
- `TmuxWindow` 数据类: session_name, window_name, window_index, active
- `TmuxWindowManager` 基于 `asyncio.create_subprocess_exec` 执行 tmux CLI
- 方法: `session_name_for()`, `session_exists()`, `create_window()`(幂等), `select_window()`, `list_windows()`(解析格式化输出), `close_window()`, `get_active_window()`, `send_keys()`

#### 2.3 connect_host 自动识别与分发 (`src/mcp_server/server.py`)
- `connect_host` 重写为类型分发入口: 查 DB → 判断 `is_jump_host` → 分发
- `_connect_direct_host()`: 提取自旧版 connect_host，处理 direct/bastion 直连
- `_connect_jump_host()`: 新增，堡垒机 WeTTY 复用 → tmux 窗口创建 → PTY 连接 → JumpOrchestrator 编排
- 全局状态扩展: `_tmux_manager` + `_get_tmux_manager()` 辅助函数
- `list_hosts` MCP 工具: 过滤 jump_host，bastion 展示 jump_hosts 子列表
- 导入扩展: `Host`, `HostType`, `JumpOrchestrator`, `TmuxWindowManager`

#### 2.4 switch_window / list_windows MCP 工具 (`src/mcp_server/server.py`)
- `list_windows(bastion_name)`: 查询堡垒机 tmux 会话中所有窗口，返回 JSON（含 active 标记）
- `switch_window(bastion_name, window_name)`: 切换活跃窗口，失败时返回可用窗口列表辅助诊断
- 两个工具均检查 tmux 会话存在性

#### 2.5 PTYSession tmux window 绑定 (`src/services/pty_session.py`)
- `PTYSessionInfo` 数据类: 新增 `tmux_window: str | None`
- `PTYSession.__init__()`: 新增 `tmux_window` 参数
- `PTYSession.info`: 包含 `tmux_window`
- `PTYSessionManager.create_session()`: 新增 `tmux_window` 参数，传递到 PTYSession

### Sprint 3：前端 UI

**实施日期**: 2026-03-25
**状态**: ✅ 全部完成

#### 3.1 HostList 树形结构 (`frontend/src/components/HostList.tsx`)
- 完全重写为树形结构组件，支持递归渲染
- `_HostItem` 内部组件: 支持任意深度嵌套，bastion 类型可展开/折叠 children
- 三种主机类型图标: 🖥 direct / 🏰 bastion / 🔗 jump_host
- bastion 项右侧显示子主机数量 badge
- jump_host 显示 `→ target_ip` 替代 `user@host:port`
- 缩进级别通过 `depth * 16 + 16` 动态计算 paddingLeft

#### 3.2 TerminalTabs 组件 (`frontend/src/components/TerminalTabs.tsx`)
- `TerminalTab` 数据模型: id, label, host, bastionName, tmuxWindow
- 辅助函数: `tabIdForHost()`, `createTabForHost()` — 稳定 Tab ID 生成
- Tab 栏 UI: emerald 底边框高亮活跃 Tab, hover 显示关闭按钮
- jump_host Tab 显示堡垒机前缀（如 `bastion/m12`），便于区分
- 无 Tab 时不渲染，终端区域使用完整高度

#### 3.3 App.tsx 集成 (`frontend/src/App.tsx`)
- 新增状态: `tabs: TerminalTab[]`, `activeTabId: string | null`
- 主机选择 → Tab 管理: 已有 Tab 则切换，否则创建新 Tab
- jump_host 自动查找父 bastion 名称作为 Tab 前缀
- 关闭 Tab: 停止 WeTTY（仅非 jump_host），切换到相邻 Tab
- header 信息: jump_host 显示 `bastion → name (target_ip)` 格式
- 终端区域: Tab 栏 → TerminalView 垂直排列，Tab 栏仅在有 Tab 时渲染

#### 3.4 AgentPanel + SSE 事件扩展
- `AgentPanel.tsx`: 新增 `session_error`(⚠️) 和 `window_switched`(🔀) 事件类型
- `api.ts`: SSE 事件白名单添加 `session_error` 和 `window_switched`

### Sprint 4：优化 & 测试

| # | 任务 | 涉及文件 | 状态 |
|---|------|----------|------|
| 4.1 | hosts-example.yaml 堡垒机+二级主机完整示例 | `config/hosts-example.yaml` | ✅ |
| 4.2 | tmux switch-window REST API（前端 Tab 切换调用） | `src/api/tmux.py` (新增), `src/main.py` | ✅ |
| 4.3 | 前端 Tab 切换联动 tmux select-window | `frontend/src/App.tsx`, `frontend/src/services/api.ts` | ✅ |
| 4.4 | HostList 连接状态指标（绿点/无点） | `frontend/src/App.tsx`, `frontend/src/components/HostList.tsx` | ✅ |
| 4.5 | 后端单元测试（TmuxWindowManager + JumpOrchestrator） | `tests/test_tmux_manager.py`, `tests/test_jump_orchestrator.py` (新增) | ✅ |
| 4.6 | 设计文档更新（Sprint 4 记录） | `docs/jump-host-design.md` | ✅ |

---

### Sprint 4：优化 & 测试

**实施日期**: 2026-03-25
**状态**: ✅ 全部完成

#### 4.1 hosts-example.yaml 完整示例 (`config/hosts-example.yaml`)
- 更新字段文档区：新增 `type`, `ready_pattern`, `login_success_pattern`, `jump_hosts`, `login_steps`, `password` 变量替换说明
- 替换旧版注释掉的堡垒机示例为完整新格式
- 三种二级主机示例：直连（无 login_steps）、账号选择、密码认证（`{{password}}` 变量）

#### 4.2 tmux REST API (`src/api/tmux.py`, `src/main.py`)
- 新增 `POST /api/tmux/switch-window` — 接收 `{ bastion_name, window_name }`，调用 `TmuxWindowManager.select_window()`
- 新增 `GET /api/tmux/windows/{bastion_name}` — 列出堡垒机 tmux 会话中所有窗口
- Pydantic 请求/响应: `SwitchWindowRequest`, `TmuxWindowResponse`
- **关键设计**: `TmuxWindowManager` 单例提升到 `main.py` 全局，通过 lifespan 注入到 `tmux.py` 和 `mcp_server/server.py`，两者共享同一实例
- `init_mcp_server` 签名扩展: `tmux_manager` 可选参数，外部注入优先于内部创建

#### 4.3 前端 Tab 联动 tmux (`frontend/src/App.tsx`, `frontend/src/services/api.ts`)
- `api.ts`: 新增 `switchTmuxWindow(bastionName, windowName)` → `POST /api/tmux/switch-window`
- `App.tsx handleTabSelect`: 立即更新 `activeTabId`（UI 响应），异步调用 `switchTmuxWindow`（fire-and-forget + catch 日志）
- 仅对有 `tmuxWindow` + `bastionName` 的 Tab（即 jump_host 类型）才发起后端调用

#### 4.4 HostList 连接状态指标 (`frontend/src/App.tsx`, `frontend/src/components/HostList.tsx`)
- **方案 C（本地推导）**: 从 `tabs` 数组用 `useMemo` 计算 `connectedHostIds: Set<number>`，传给 HostList
- `HostListProps` 新增 `connectedHostIds?: Set<number>` 可选属性
- `_HostItem` 中计算 `isConnected = connectedHostIds?.has(host.id)`，已连接的主机名后显示翠绿色圆点 (`w-1.5 h-1.5 rounded-full bg-emerald-500`)
- `connectedHostIds` 逐层传递到递归子项

#### 4.5 后端单元测试
- **`tests/test_tmux_manager.py`** (已有): 7 个测试类、11 个测试方法，覆盖 `session_name_for`、`session_exists`、`list_windows`（解析 + 错误）、`create_window`（新建 + 幂等）、`select_window`（成功 + 失败）、`close_window`、`get_active_window`
- **`tests/test_jump_orchestrator.py`** (新增): 10 个测试类、17 个测试方法，覆盖：
  - `_resolve_variables`: password/manual/unknown 变量替换、混合变量、纯文本
  - `_parse_jump_config`: 正常解析 / 缺失 / 无效 JSON 容错
  - `_parse_login_steps`: 正常解析 / 空 / 无效 JSON 容错
  - `execute_jump` 直连成功 / target_ip 缺失失败
  - `execute_jump` 多步骤成功 / 步骤超时失败
  - `execute_jump` 提前登录成功跳过剩余步骤
  - `execute_jump` `{{manual}}` 变量暂停编排
  - `execute_jump` tmux 窗口创建失败 / 堡垒机就绪超时
  - `_decrypt_jump_password` 空密码 / 解密失败容错

#### 4.6 设计文档更新
- 文档状态更新为 `✅ Sprint 1–4 已完成`
- 新增 Sprint 4 任务表（6 项，全部 ✅）
- 新增 Sprint 4 实施记录（详细变更说明）

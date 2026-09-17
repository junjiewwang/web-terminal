# 智能识别登录交互步骤（交互规则）

## 需求

消除多跳连接中每个节点手写 `entry.steps` 的重复劳动。提供一套**抽象通用**的「匹配正则 → 发送内容」规则机制，由用户在页面上配置；连接时节点 steps 为空则自动按优先级应用；特殊节点可手动覆盖。

规则**不内置**任何具体业务（如"密码"、"堡垒机用户选择"），完全由用户自由定义。

## 设计

| 维度 | 方案 |
|------|------|
| 规则抽象 | `{name, description, priority, wait_pattern, send_text, timeout, max_retries, enabled}` |
| 存储 | 数据库表 `interaction_rules`（页面 CRUD） |
| 内置规则 | 无（空表起步） |
| 示例 | 后端 `GET /api/interaction-rules/examples` 返回，页面仅文字参考 |
| 触发 | `interaction_mode`（默认 `auto`）：steps 非空→手动；steps 空→自动识别；`manual`→强制手动 |
| 匹配优先级 | `success_pattern` 强标志 > 规则 `priority` > 声明顺序（单字符提示符不视为成功） |
| 变量 | `{{password}}` / `{{user_id}}` / `{{username}}` / `{{manual}}` |
| 安全边界 | 总步数上限（10）+ 单规则重试上限 + 人工兜底 + 密码缺失拒绝 |

## 实施进展

- [x] 模型：`src/models/interaction_rule.py`（ORM `interaction_rules` 表 + Pydantic schema）
- [x] 服务：`src/services/interaction_rule_service.py`（CRUD + `list_enabled` + `get_examples`）
- [x] API：`src/api/interaction_rules.py`（CRUD + `/examples`）
- [x] 引擎：`_auto_interact` 从数据库读规则；`_load_enabled_rules` 容错降级
- [x] 注册：`database.py` 导入新表；`main.py` 挂载路由
- [x] 前端：`InteractionRulePanel.tsx` + `api.ts` + `App.tsx` 导航
- [x] 清理：删除 `src/services/interaction_rules.py`、`config/interaction_rules.yaml`
- [x] 初始化示例规则（4 条）+ 清空所有节点手动 steps（arm64 加 `rule_vars.user_id`）
- [x] MCP 真实验证：TCE-V113（密码）+ arm64（ID 选择 + 密码）自动识别均成功

## 变更文件

- 新增：`src/models/interaction_rule.py`、`src/services/interaction_rule_service.py`、`src/api/interaction_rules.py`、`frontend/src/components/InteractionRulePanel.tsx`
- 修改：`src/services/jump_orchestrator.py`、`src/services/terminal_manager.py`、`src/models/database.py`、`src/main.py`、`src/api/hosts.py`、`frontend/src/services/api.ts`、`frontend/src/App.tsx`、`frontend/src/components/HostManagePage.tsx`
- 删除：`src/services/interaction_rules.py`、`config/interaction_rules.yaml`
- 测试：`tests/test_interaction_rules.py`

## 修复的问题

1. `_wrap_inline_flags`：规则 `wait` 全局 `(?i)` flags 嵌套命名分组报 `re.error`，转换为局部 flags 语法。
2. `model_validate` 触发 `MissingGreenlet`：`server_default=func.now()` 的 `created_at`/`updated_at` 在 flush 后未加载，`service.create`/`update` 增加 `refresh`。
3. **逐字符回显导致规则无法匹配**（治本）：PTY 对 JumpServer 菜单的 `ID>` 提示逐字符输出（`I\nD\n>`），多字符规则 `^\s*ID\s*>\s*$` 无法匹配。在 `wait_for` 增加 `_merge_fragment_lines` 合并连续单字符行（先剥离行尾 `\r`）。
4. **success_pattern 误匹配单字符提示符**：`[\$#>]\s*$` 会匹配 `Opt> Opt>`、`ID>` 的孤立 `>`，导致引擎误判登录成功提前结束。`_auto_interact` 改为：单字符提示符不视为登录成功，仅"强标志"（多字符，如 `Last login`、`]#`、`]$`）才判定成功。
5. **终端屏幕可视化（自动识别过程上屏）**：`_auto_interact` 应用规则/发送入口值/登录成功时，通过 `TerminalSession.broadcast_notice()` 把黄色高亮提示写入终端 scrollback 并广播给浏览器，用户直接在终端画面上看到「[自动识别] 应用规则 X」。提示不经过 PTY、不写入远端终端、不影响 `wait_for` 匹配。事件仅含规则名称/说明/正则，**不含 `send_text`**，避免泄露密码。
6. **操作轨迹可视化（辅助）**：同时通过 `event_bus.publish` 发布 `command_start` 事件到操作轨迹面板。

## 终端屏幕可视化（自动识别过程上屏）

连接节点时，终端画面直接显示黄色高亮的自动识别过程（不污染远端终端）：

```
[自动识别] 发送入口值 · 10.22.2.146
[自动识别] 应用规则 select_user · 堡垒机用户选择菜单（ID>）
[自动识别] 应用规则 password · 密码输入提示
[自动识别] 登录成功
```

- 后端：`terminal_manager.py` 新增 `broadcast_notice()`（ANSI 黄色 + 独立换行，写入 scrollback + 广播）
  - 同时写入 scrollback：MCP 触发连接时浏览器尚未建立 WebSocket，后连接的浏览器通过 history 回放仍能看到提示
- 后端：`jump_orchestrator.py` 的 `_publish_action` 同时调用 `broadcast_notice()`（终端上屏）+ `event_bus.publish`（操作轨迹）
- 前端：`App.tsx` 的 `eventSummary` 对 `[自动识别]` 前缀事件直接展示

## MCP 真实验证结果

| 节点 | 场景 | 结果 |
|------|------|------|
| `TCE-V113` | 密码提示 | ✅ 应用 `password` 规则，登录成功 |
| `TCE3.10.12.1集测环境-arm64` | ID 选择 + 密码 | ✅ 应用 `select_user` + `password` 规则，`hostname`/`whoami` 返回正确 |

## 遗留问题

1. **`_execute_step` 的单字符提示符误匹配**：手动 `steps` 路径的 `_execute_step` 仍有 `success_pattern` 误匹配单字符提示符的隐患（当前节点已全部清空 steps，不影响），后续可统一处理。
2. **裸提示符环境的权衡**：单字符提示符不视为登录成功的策略，对使用裸 `$`/`#`/`>` 提示符（如 `sh`/`dash`）的环境可能无法判定登录成功，需依赖超时兜底。bash 环境（`]#`/`]$`）不受影响。
3. **YAML 未同步**：本次直接操作数据库清空 steps，`config/hosts.yaml` 里的 steps 仍是旧值。若后续强制 `POST /api/hosts/sync` 会回退，需将 YAML 同步更新。

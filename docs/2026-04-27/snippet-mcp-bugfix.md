# Snippet MCP 工具 Bug 修复

## 发现经过

在实际使用 MCP `load_snippet_domain` + `run_snippet_command` 工具操作 TCE 环境时，
发现以下问题链导致 Snippet 系统在 MCP 场景下完全不可用：

1. `load_snippet_domain` 返回"已加载"，但远端 `type ki` 实际报 `command not found`
2. `run_snippet_command` 传参后直接执行失败
3. 手动 `run_command "ki tce apm-nacos"` 也报 `command not found`（脚本从未注入）

## Bug 列表

### Bug 1：探测命令回显污染导致假阳性（核心 Bug）

**文件**：`src/services/snippet_registry.py` + `src/mcp_server/server.py`

**根因**：探测命令生成的是：
```bash
type ki 2>/dev/null && echo '__SNIPPET_LOADED__' || echo '__SNIPPET_NOT_LOADED__'
```

PTY 终端会回显输入命令，`probe_output` 包含完整回显。由于回显行中包含了
字面量 `__SNIPPET_LOADED__`（在 `echo '__SNIPPET_LOADED__'` 部分），
而判定逻辑使用 `"__SNIPPET_LOADED__" in probe_output`（全文子串匹配），
导致即使实际结果是 `__SNIPPET_NOT_LOADED__`，也会匹配到回显行中的 `__SNIPPET_LOADED__`，
误判为"已加载"，跳过脚本注入。

**修复**：
1. 探测标记改为 `__PROBE_YES__` / `__PROBE_NO__`
2. 判定逻辑改为只检查输出的**最后非空行**（排除回显行干扰）
3. `type` 重定向改为 `>/dev/null 2>&1`（避免 type 输出函数定义信息干扰匹配）

### Bug 2：`ki` / `kic` 命令模板缺少 `filter` 参数

**文件**：`config/snippets.yaml`

**根因**：Shell 函数 `ki` 的签名是 `ki [-p] [namespace] [name-filter]`，
但 YAML 模板只定义了 `{{options}}` 和 `{{namespace}}`，缺少 `{{filter}}`。
导致通过 `run_snippet_command` 传入 filter 参数时被丢弃。

同理 `kic` 函数签名 `kic <ns1> <ns2> [name-filter]` 也缺少 `{{filter}}`。

**修复**：在 `ki` 和 `kic` 命令模板中补充 `{{filter}}` 占位符和参数定义。

### Bug 3：Heredoc 注入等待逻辑不精确

**文件**：`src/mcp_server/server.py` + `src/services/snippet_registry.py`

**根因**：Heredoc 多行脚本通过 `send_input` 一次性发送后，
`wait_for` 使用通用 shell 提示符正则 `(?:[\$#>%])\s*$` 等待完成。
但脚本内容在 PTY 回显时，其中的 `$`、`#` 等字符可能导致正则提前匹配，
使 `wait_for` 在注入完成前就返回。

**修复**：
1. `build_heredoc_loader` 末尾追加 `&& echo '__SNIPPET_INJECTED__'` 确认标记
2. `wait_for` 改为等待 `__SNIPPET_INJECTED__` 精确标记，而非通用提示符

### Bug 4：`run_snippet_command` params 类型与 MCP 协议不兼容

**文件**：`src/mcp_server/server.py`

**根因**：`run_snippet_command` 函数签名中 `params: str | None = None`，
期望 MCP client 将参数序列化为 JSON 字符串再传入（如 `'{"namespace": "tce"}'`），
服务端再 `json.loads()` 解析。

但 MCP 协议的工具调用传参是 **JSON object**，client 直接传 `{"namespace": "tce"}`（dict），
Pydantic 校验 `dict` 不匹配 `str` 类型 → 返回 `isError: true` 的验证错误。

**表现**：IDE 中显示 `MCP tool execution failed`，无具体错误信息。
通过 `curl` 直接调用 MCP 接口才看到真实错误：
```
Input should be a valid string [type=string_type, input_value={'namespace': 'tce', ...}, input_type=dict]
```

**修复**：
1. `params` 类型改为 `dict[str, str] | None`（符合 MCP 协议惯例）
2. 移除 `json.loads()` 解析逻辑
3. 增加 `str(v)` 标准化确保值类型安全

### 附加优化：模板渲染空格压缩

**文件**：`src/services/snippet_registry.py`

**问题**：可选参数为空字符串时，模板渲染后出现连续空格（如 `"ki  tce "`）。

**修复**：`resolve_command` 返回前用 `re.sub(r"\s+", " ", resolved).strip()` 压缩连续空格。

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `src/services/snippet_registry.py` | 探测标记改为公共常量 + 注入确认标记 + 空格压缩 |
| `src/mcp_server/server.py` | 探测判定改为最后行匹配 + 注入等待改为确认标记 + params 类型修正 |
| `config/snippets.yaml` | `ki` + `kic` 补充 `filter` 参数 |

## 状态

- **修复完成**：2026-04-27（Bug 1-3 + 附加优化），2026-04-27（Bug 4）
- **发现方式**：MCP 实际使用中复现（连接 TCE 环境查看 tcs-apm-nacos 资源）
- **排查方法**：Bug 4 通过 `curl` 直接调用 MCP 接口绕过 IDE client 获取到真实 Pydantic 校验错误

# Bugfix #21: ft snippet 注入超时与版本探测失败

## 问题现象

上传文件时出现以下日志序列：

```
[08:58:06] INFO     注入 ft snippet（首次注入）    file_transfer.py:524
[08:58:21] WARNING  ft snippet 注入可能超时，继续尝试执行  file_transfer.py:541
```

随后页面报错：`上传超时: 等待模式 '__FT_RECV_READY__|__FT_RECV_ERR__' 超时（15.0s）`

即使在同一个终端重复上传（ft_recv 已可用），每次仍触发重新注入。

修复 Fix a/b/c 部署后，问题仍然存在 — 注入超时且 post-inject probe 也超时，
但手动在终端里执行 `type ft_recv` 是正常的，说明根因不在注入逻辑本身。

## 根因分析

### ★ 真正根因 (Fix d): `deque(maxlen=500)` 导致 `wait_for` 位置追踪完全失效

这是导致所有 `wait_for` 超时的核心问题。

**机制分析：**

`TerminalSession._raw_buffer` 是 `deque(maxlen=500)` —— 有界双端队列，最多保留 500 行。
`wait_for()` 使用 `len(self._raw_buffer)` 作为增量扫描的位置追踪：

```python
start_pos = len(self._raw_buffer)  # 记录扫描起点
# ... 等待新数据 ...
current_len = len(self._raw_buffer)
if current_len > start_pos:  # ← 这是问题所在
    new_lines = list(self._raw_buffer)[start_pos:current_len]
```

**故障链：**
1. heredoc 注入命令本身很大（gzip+base64 压缩的 shell 脚本），Shell 回显产生大量输出行
2. 这些输出行快速填满 500 行 deque
3. deque 满后，`len()` 始终返回 500，新数据 append 时旧数据被淘汰，但 **`len()` 不再增长**
4. `wait_for` 的 `current_len > start_pos` 检查 → `500 > 500` → **永远为 False**
5. 所有后续 `wait_for` 调用都无法检测到新数据 → **全部超时**

不仅注入确认标记 `__SNIPPET_INJECTED__` 匹配不到，后续的 probe 探测、`ft_recv` 命令、
ACK 确认等所有依赖 `wait_for` 的操作也全部超时。

**修复方案：**

引入 `_buffer_write_seq: int = 0` 单调递增写入序号：
- 每次 `_on_pty_readable` 向 deque append 一行，`_buffer_write_seq += 1`
- `wait_for` / `send_command` / `_collect_batch_acks` / `_collect_chunks` 全部改用
  `_buffer_write_seq` 做位置追踪
- 通过 seq 偏移公式将序号映射到 deque 索引：
  ```
  oldest_seq = _buffer_write_seq - len(_raw_buffer)  # deque 中最旧行的 seq
  deque_index = target_seq - oldest_seq               # seq → deque 索引映射
  ```
- 即使 deque 满（`len()` 恒为 500），`_buffer_write_seq` 持续递增，`current_seq > scan_seq` 始终能正确检测新数据

### 问题 a: 版本探测被命令回显行干扰

版本探测命令：
```bash
echo "__PROBE_VER__:${__FT_SNIPPET_VERSION__:-none}"
```

Shell 的 PTY 输出包含两行：
1. **回显行**（命令本身）：`echo "__PROBE_VER__:${__FT_SNIPPET_VERSION__:-none}"`
2. **输出行**（执行结果）：`__PROBE_VER__:2026.05.14.3`

`wait_pattern=r"__PROBE_VER__:"` 同时匹配回显行和输出行。`wait_for` 可能在只收到回显行时就提前返回，此时提取到的 `remote_version` 是 `${__FT_SNIPPET_VERSION__:-none}`（未展开的变量引用），与 `local_version="2026.05.14.3"` 不匹配，被误判为"版本过期"触发重注入。

### 问题 b: 注入超时后盲目继续执行

```python
except TimeoutError:
    logger.warning("ft snippet 注入可能超时，继续尝试执行")
```

注入超时后代码直接继续调用 `ft_recv` 命令，但此时 `ft_recv` 函数可能根本不存在（`source` 还没执行完），导致 Shell 输出 `command not found`（不含协议标记），`wait_for` 再次等待 15 秒超时。

**故障链：** 注入超时(15s) → 盲目执行 ft_recv → 再超时(15s) → 前端报错（总计约 30 秒白等）

### 问题 c: 日志条件反转

```python
logger.info("注入 ft snippet（%s）",
            "版本更新" if not need_inject else "首次注入")
```

此代码在 `if not need_inject: return` 之后，此处 `need_inject` 一定为 `True`，所以 `"版本更新"` 分支永远不会走到，日志永远显示"首次注入"。

## 修复方案

### Fix d (核心): `_buffer_write_seq` 单调递增位置追踪

**terminal_manager.py:**
- 新增 `_buffer_write_seq: int = 0` 字段
- `_on_pty_readable`: 每 append 一行递增 `_buffer_write_seq += 1`
- `wait_for()`: 完全重写扫描逻辑，使用 seq-based 位置追踪
- `send_command()`: `pre_pos` 改用 `_buffer_write_seq`

**pty_file_transfer.py:**
- `upload()` / `download()` 中所有 `len(self._session._raw_buffer)` 改为 `self._session._buffer_write_seq`
- `_collect_batch_acks()`: 重写为 seq-based 扫描
- `_collect_chunks()`: 重写为 seq-based 扫描

**file_transfer.py / server.py:**
- `_ensure_ft_snippet_loaded()` 中 `pre_inject_pos` 改用 `session._buffer_write_seq`

### Fix a: 使用 `__PROBE_VER_RESULT__` 标记区分输出行与回显行

**snippet_registry.py:**
- 输出标记改为 `__PROBE_VER_RESULT__`
- `wait_pattern` 改为 `r"__PROBE_VER_RESULT__:\w"`（`\w` 匹配版本号首字符，排除 `${` 开头的回显）

**file_transfer.py:**
- 版本提取时增加回显行过滤：值中含 `$` 或 `{` 的行跳过

### Fix b: 注入超时后做 post-inject probe 验证

注入等待 `__SNIPPET_INJECTED__` 超时后，不再盲目继续，而是：
1. 执行一次快速 `type ft_recv` 探测（5 秒超时）
2. 如果探测确认 `ft_recv` 可用 → 继续执行（可能是 `__SNIPPET_INJECTED__` 标记被 PTY 缓冲区吞掉了，但脚本实际已加载）
3. 如果探测确认不可用 → 直接抛出 HTTP 503 错误，避免后续 30 秒无意义等待

**同时修复了 MCP server 中的同名函数（`server.py` 的 `_ensure_ft_snippet_loaded`）。**

### Fix c: 使用 `inject_reason` 变量记录注入原因

改为在探测逻辑中维护 `inject_reason` 字符串，注入时直接使用。

## 修改文件

| 文件 | 变更 |
|------|------|
| `src/services/terminal_manager.py` | ★ Fix d: `_buffer_write_seq` 字段 + `_on_pty_readable` 递增 + `wait_for` / `send_command` 重写为 seq-based 扫描 |
| `src/services/pty_file_transfer.py` | ★ Fix d: 所有 `len(_raw_buffer)` 位置追踪改为 `_buffer_write_seq`；`_collect_batch_acks` / `_collect_chunks` 重写 |
| `src/api/file_transfer.py` | Fix d: `pre_inject_pos` 改用 `_buffer_write_seq`；Fix a: 版本探测 pattern 修复；Fix b: post-inject probe；Fix c: 日志修正 |
| `src/mcp_server/server.py` | Fix d: `pre_inject_pos` 改用 `_buffer_write_seq`；Fix b: 增加 post-inject probe 验证 |
| `src/services/snippet_registry.py` | Fix a: `get_version_probe_command` 输出标记改为 `__PROBE_VER_RESULT__` |

## 验证方法

1. 首次上传：应看到 `注入 ft snippet（首次注入）` → `ft snippet 注入完成`
2. 重复上传（同会话）：应看到 `ft snippet 已是最新版本: 2026.05.14.3`，**不再重注入**
3. 大量输出场景（deque 溢出）：注入后 wait_for 应正常检测到标记，**不再超时**
4. 网络慢导致注入超时：应看到 `post-inject 探测确认: ft_recv 函数可用/不可用`，而非盲目继续

## 关键设计决策

**为什么选择单调递增 seq 而不是增大 deque？**

增大 deque 只是延迟问题，不是修复问题。在足够长的会话中（或足够大的 heredoc 注入），
任何有限大小的 deque 都可能被填满。单调递增 seq 从根本上解耦了"新数据检测"和
"deque 容量限制"两个关注点。

---

# Bugfix #22c: 文件传输 base64 数据回显刷屏终端（WebSocket 广播层静默）

## 问题现象

文件传输（上传/下载）时，浏览器终端中出现大量 base64 乱码文本，占满终端屏幕。
包括：
- snippet 注入时的 loader base64 数据
- upload 时的 `__FT_CHUNK__:seq:base64...` 数据
- download 时的 `ft_send` 命令回显和 `__FT_CHUNK__` 数据

实际上文件传输功能本身正常运行（进度条正常推进），但用户体验极差。

## 失败方案历史

### 方案 A: `stty -echo`（Bugfix #22，失败）

通过 `send_input("stty -echo\n")` 发送到远程 Shell 来抑制回显。
**失败原因：** `stty -echo` 只影响远程 Shell 的 PTY 设置，
不影响本地 PTY。且交互式 Shell（Zsh/Oh-My-Zsh）在每次命令执行后
显示 prompt 时会重置 termios 设置，覆盖 `stty -echo` 的效果。

### 方案 B: `termios.tcsetattr()`（Bugfix #22b，失败）

通过 Python `termios` 模块直接操作本地 PTY fd 的 ECHO 标志位。
**失败原因：** SSH 客户端在本地 PTY slave 端以 raw mode 运行，
本地 PTY 的 ECHO **本来就是关闭的**（SSH 的标准行为）。
真正的回显来自**远端 Shell 的 PTY**——SSH 将输入转发到远端，
远端 Shell 的 PTY 回显后通过 SSH 传回本地。

## 根因分析

### 完整的数据流

```
send_input("ft_send '/tmp/file'\n")
  → os.write(local PTY master fd)
  → local PTY slave (bash/SSH, raw mode, ECHO=OFF)
  → SSH 传到远端
  → 远端 Shell 的 PTY (ECHO=ON, 交互式 Shell 默认行为)
  → 远端 PTY 回显 "ft_send '/tmp/file'\n"     ← 回显发生在这里！
  → SSH 传回本地
  → local PTY slave → master
  → _on_pty_readable()
  → _broadcast_output() → WebSocket → 浏览器 xterm.js
```

### 为什么 `stty -echo` 和 `termios` 都无效

1. **本地 PTY 的 ECHO 本来就是 OFF**：SSH 客户端启动时已经将本地 PTY 设为
   raw mode（`-echo -icanon`），所以 `termios.tcsetattr(local_fd, ECHO=OFF)` 是多余的。

2. **回显来自远端 PTY**：远端交互式 Shell（Zsh+Oh-My-Zsh）的 PTY 有 ECHO=ON，
   且 Shell 在每次显示 prompt 后会重置 termios，覆盖之前的 `stty -echo`。

3. **不可靠的时序**：即使 `stty -echo` 在远端生效，从 Python 发送命令到远端
   Shell 执行之间有网络延迟，后续数据可能在 echo 关闭前就已到达远端 PTY。

### 唯一可靠的方案

在应用层控制：**直接在 `_on_pty_readable()` 到 WebSocket 广播的路径上截断**。

## 修复方案：WebSocket 输出静默模式

### 核心机制

在 `TerminalSession` 上新增 `_ws_muted: bool` 标志。
当设为 `True` 时，`_on_pty_readable()` 跳过 WebSocket 广播。

```python
# terminal_manager.py - _on_pty_readable()

# 数据仍写入 Agent 缓冲区（wait_for 不受影响）
for line in text.split("\n"):
    if line:
        self._raw_buffer.append(line)
        self._buffer_write_seq += 1
self._output_event.set()

# ★ 静默时跳过 WebSocket 广播 ★
if self._ws_muted:
    return

# 正常广播给所有 WebSocket 客户端
self._broadcast_output(text)
```

### 优势

1. **100% 可靠**：不依赖任何 PTY/termios/stty 设置，纯 Python 层面控制
2. **无竞态条件**：Python 标志位设置即时生效，不存在网络延迟
3. **Agent 缓冲区不受影响**：`wait_for()` / `_collect_batch_acks()` / `_collect_chunks()` 
   仍能正常检测标记和 ACK
4. **scrollback 不受影响**：数据仍写入 scrollback 缓冲区
5. **同时抑制上传和下载**：upload 的 chunk 数据回显和 download 的远端输出都被屏蔽

### 调用方

```python
# snippet 注入
session.set_ws_muted(True)
try:
    await session.send_input(loader + "\n")
    await session.wait_for(pattern=..., timeout=15.0)
finally:
    session.set_ws_muted(False)

# 文件上传/下载
self._session.set_ws_muted(True)
try:
    # ... 传输逻辑 ...
finally:
    self._session.set_ws_muted(False)
```

## 修改文件

| 文件 | 变更 |
|------|------|
| `src/services/terminal_manager.py` | 新增 `_ws_muted: bool` 标志 + `set_ws_muted()` 方法；`_on_pty_readable()` 中静默时跳过 WebSocket 广播 |
| `src/services/snippet_registry.py` | `ensure_snippet_loaded()` 改用 `session.set_ws_muted(True/False)` |
| `src/services/pty_file_transfer.py` | `upload()` 和 `download()` 方法：传输前 `set_ws_muted(True)`，`finally` 中 `set_ws_muted(False)` |

## 清理

移除了之前失败方案的残留代码：
- `ECHO_OFF` / `ECHO_ON` 常量（stty 方案残留）
- `set_local_echo()` 方法（termios 方案残留）
- loader 末尾的 `; stty echo 2>/dev/null`（belt-and-suspenders 残留）

## 验证方法

1. 文件上传时，终端**不再显示任何传输协议数据**（chunk、ACK、标记）
2. 文件下载时，终端**不再显示 `__FT_CHUNK__` base64 数据**
3. snippet 注入时，终端**不再显示 loader base64 数据**
4. 传输完成后，终端恢复正常（可以正常输入命令、看到输出）
5. 传输异常中断（超时/断连）后，终端也恢复正常（`finally` 块保证）
6. Agent 功能不受影响：`wait_for` 仍能检测标记，进度条仍正常推进

# Broker 多客户端虚拟终端渲染中间层 — 技术设计方案

> **当前状态**：✅ 已实施（Sprint 3 完成）
>
> 本方案基于 pyte VirtualTerminal 的共享 PTY + min-size 策略已在 Sprint 3 中完整实施。
> 核心实现位于 `src/services/virtual_terminal.py`（渲染中间层）和 `src/services/terminal_manager.py`（集成逻辑）。
>
> 实施进展见 `docs/pty-broker-refactor-progress.md` 的 §6.8–6.11。

## 1. 问题定义

### 1.1 当前架构

Broker 模式下，所有 WebSocket 客户端共享**同一个 PTY fd**：

```
Browser A  ──┐                                    ┌──→ ws.send_json() → Browser A
Browser B  ──┼─ WebSocket → session.write() → PTY ─→ _on_pty_readable()
Agent      ──┘       (os.write(fd))           fd    └──→ _broadcast_output() → Browser B
                                                        (所有客户端收到完全相同的字节流)
```

**核心问题：PTY fd 是全局唯一的终端设备，终端尺寸 (`TIOCSWINSZ`) 只有一份。**

- 当 Browser A（120×30）和 Browser B（80×24）同时连接时，最后一个发送 `resize` 的客户端 "赢"。
- PTY 后端程序（shell/SSH）按赢家的尺寸排版输出，另一个客户端看到错乱的内容。
- 这不是 bug，而是架构层面的根本限制。

### 1.2 tmux 为什么没有这个问题

tmux 的架构是：

```
Browser A → tmux client A → PTY A  ──┐
                                     ├─ tmux server (维护虚拟屏幕缓冲区)
Browser B → tmux client B → PTY B  ──┘    │
                                           └─→ 程序 PTY（真正的 shell/SSH）
```

每个 tmux client 有**独立的 PTY**。tmux server 在内存中维护一个**虚拟屏幕**（字符矩阵），将程序 PTY 的输出解析到虚拟屏幕上，然后**按每个 client 的 PTY 尺寸分别渲染**输出。

### 1.3 目标

在 Broker 模式中引入类似的"渲染中间层"，使多个客户端即使窗口尺寸不同，也能各自看到正确排版的终端输出。

---

## 2. 架构设计

### 2.1 目标架构

```
                                ┌─────────────────────────────────┐
Browser A (120×30) ──┐          │  VirtualTerminal (渲染中间层)    │
                     │          │  ┌───────────────────────────┐  │
Browser B (80×24)  ──┼─ WS ──→ │  │ pyte.Screen (80×24)       │  │  ← min(cols)×min(rows) 策略
                     │          │  │ 虚拟屏幕缓冲区 (字符矩阵)  │  │
Agent              ──┘          │  │ + 完整 ANSI 状态机          │  │
                                │  └───────────────────────────┘  │
                                │            ↑ feed()              │
                                │            │                     │
                                │    PTY fd  (os.read → bytes)     │
                                │            │                     │
                                │    ┌───────┴──────────┐          │
                                │    │ resize PTY to     │          │
                                │    │ min(cols)×min(rows)│         │
                                │    └──────────────────┘          │
                                │            │                     │
                                │    ┌───────┴──────────┐          │
                                │    │ per-client render │          │
                                │    │ Screen → ANSI     │          │
                                │    └──────────────────┘          │
                                │       ↓           ↓              │
                                │   Browser A    Browser B         │
                                │  (120×30 viewport) (80×24 viewport)
                                └─────────────────────────────────┘
```

### 2.2 核心设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| **PTY 尺寸策略** | min(cols) × min(rows) 策略 | 保证所有客户端都能看到完整输出，与 tmux 默认行为一致 |
| **VT 解析器** | `pyte` 库 | 纯 Python、LGPL、VT100/220/520 兼容、有 dirty tracking、有 resize、零外部依赖 |
| **渲染输出格式** | 差分渲染 ANSI 转义序列 | 仅发送变更行，降低带宽；前端 xterm.js 天然支持 ANSI |
| **Client 尺寸不同时的处理** | 所有客户端统一渲染相同内容 | PTY 只有一份 → 程序输出只有一种排版，不同尺寸客户端看到的是同一画面的不同视口 |
| **独立渲染 vs 统一渲染** | 统一渲染 + 前端视口裁剪 | 避免为每个客户端维护独立 Screen 的复杂度 |

### 2.3 方案对比：为什么不选"每客户端独立 PTY/Screen"

| 维度 | 方案 A: 统一 Screen + 视口 | 方案 B: 每客户端独立 Screen |
|------|--------------------------|--------------------------|
| PTY 数量 | 1 个（共享） | 1 个（共享） |
| Screen 数量 | 1 个 | N 个（每客户端一个） |
| 输出一致性 | 天然一致 | 需要同步 N 个 Screen 的 feed |
| PTY 尺寸 | min(cols) × min(rows) | 仍然只有一份，无法为不同 Screen 产出不同排版 |
| 复杂度 | 低 | 高，且无法真正解决"不同尺寸产生不同排版" |
| 结论 | **推荐** | 不推荐（除非引入 N 个独立 PTY） |

**关键洞察**：只要共享同一个 PTY fd，程序的输出就是单一的字节流，无论在服务端维护多少个 Screen 对象，解析的都是同一份数据，画面排版不会因客户端不同而不同。真正的"独立渲染"需要独立 PTY（即方案 D），但那就打破了共享会话的语义。

---

## 3. 详细设计

### 3.1 数据结构

#### 3.1.1 ClientInfo — 每客户端元数据

```python
@dataclass
class ClientInfo:
    """WebSocket 客户端元数据"""
    ws: WebSocket
    client_id: str              # UUID
    cols: int = 80              # 客户端终端列数
    rows: int = 24              # 客户端终端行数
    connected_at: datetime = field(default_factory=datetime.now)
```

#### 3.1.2 VirtualTerminal — 渲染中间层

```python
class VirtualTerminal:
    """虚拟终端渲染中间层

    维护一个 pyte.Screen 作为虚拟屏幕缓冲区。
    PTY 原始输出通过 feed() 写入 Screen，Screen 解析 ANSI 序列
    并更新字符矩阵。渲染时将 dirty 行转换为 ANSI 转义序列发送给客户端。
    """

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._cols = cols
        self._rows = rows
        # 上一帧的显示内容（用于差分计算）
        self._prev_display: list[str] = ["" * cols for _ in range(rows)]
```

### 3.2 改造 TerminalSession

#### 3.2.1 客户端管理：从 `list[WebSocket]` 升级为 `dict[str, ClientInfo]`

**现有代码**（`terminal_manager.py:121`）：
```python
self._ws_clients: list[WebSocket] = []
```

**改造为**：
```python
self._ws_clients: dict[str, ClientInfo] = {}  # client_id → ClientInfo
self._vterm: VirtualTerminal | None = None     # 虚拟终端（仅 broker 需要）
```

#### 3.2.2 resize 逻辑：从"直写 PTY"变为"收集尺寸 → 计算最小 → 写 PTY + 更新 Screen"

**现有代码**（`terminal_manager.py:289-296`）：
```python
def resize(self, cols: int, rows: int) -> None:
    if self._fd is not None and self._running:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
```

**改造为**：
```python
def resize(self, cols: int, rows: int, client_id: str | None = None) -> None:
    """调整终端尺寸。

    Broker 模式 + 虚拟终端启用时：
    1. 更新该 client 的尺寸记录
    2. 计算所有 client 的 min(cols) × min(rows)
    3. 用最小尺寸设置 PTY + 更新虚拟 Screen
    4. 通知所有客户端当前有效尺寸
    """
    if self._fd is None or not self._running:
        return

    if self._vterm and client_id:
        # 更新 client 尺寸
        client = self._ws_clients.get(client_id)
        if client:
            client.cols = cols
            client.rows = rows

        # 计算最小尺寸
        effective_cols, effective_rows = self._compute_min_size()

        # 设置 PTY 尺寸
        self._set_pty_size(effective_cols, effective_rows)

        # 更新虚拟 Screen 尺寸
        self._vterm.resize(effective_cols, effective_rows)

        # 通知客户端有效尺寸
        self._broadcast_resize_hint(effective_cols, effective_rows)
    else:
        # 非虚拟终端模式：直写 PTY（兼容 tmux 和 Agent）
        self._set_pty_size(cols, rows)

def _set_pty_size(self, cols: int, rows: int) -> None:
    """底层 PTY ioctl 调用。"""
    if self._fd is not None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.debug("PTY resize 失败: %s - %s", self.session_id[:8], e)

def _compute_min_size(self) -> tuple[int, int]:
    """计算所有活跃客户端的 min(cols) × min(rows)。"""
    if not self._ws_clients:
        return (80, 24)
    cols = min(c.cols for c in self._ws_clients.values())
    rows = min(c.rows for c in self._ws_clients.values())
    return (max(cols, 10), max(rows, 3))  # 设下限避免异常值
```

#### 3.2.3 输出路径：从"直接广播"变为"feed Screen → 差分渲染 → 广播"

**现有代码**（`terminal_manager.py:298-337`）：
```python
def _on_pty_readable(self) -> None:
    data = os.read(self._fd, 65536)
    text = data.decode(errors="replace")
    self._append_scrollback(data)
    # ... Agent 缓冲区 ...
    self._broadcast_output(text)
```

**改造后**：
```python
def _on_pty_readable(self) -> None:
    data = os.read(self._fd, 65536)
    text = data.decode(errors="replace")
    self._append_scrollback(data)

    # Agent 缓冲区（不变）
    for line in text.split("\n"):
        if line:
            self._raw_buffer.append(line)
    self._output_event.set()

    if self._vterm:
        # 虚拟终端模式：feed → 差分渲染 → 广播
        rendered = self._vterm.feed_and_render(text)
        if rendered:
            self._broadcast_output(rendered)
    else:
        # 直通模式：原始广播（兼容 tmux）
        self._broadcast_output(text)
```

### 3.3 VirtualTerminal 详细实现

#### 3.3.1 feed_and_render — 核心方法

```python
def feed_and_render(self, text: str) -> str:
    """将 PTY 输出 feed 到虚拟 Screen，返回差分渲染结果。

    流程：
    1. pyte.Stream.feed(text) — 解析 ANSI 序列，更新 Screen 字符矩阵
    2. 检查 Screen.dirty — 获取变更的行号集合
    3. 将变更行转换为 ANSI 转义序列
    4. 清空 dirty 集合
    5. 返回 ANSI 文本供客户端 xterm.js 渲染

    Returns:
        ANSI 转义序列字符串。如果没有变化返回空字符串。
    """
    self._stream.feed(text)

    if not self._screen.dirty:
        return ""

    output_parts: list[str] = []
    dirty_lines = sorted(self._screen.dirty)

    for line_no in dirty_lines:
        # 移动光标到行首
        output_parts.append(f"\x1b[{line_no + 1};1H")
        # 清除整行
        output_parts.append("\x1b[2K")
        # 渲染该行的字符和属性
        output_parts.append(self._render_line(line_no))

    # 恢复光标位置
    cursor = self._screen.cursor
    output_parts.append(f"\x1b[{cursor.y + 1};{cursor.x + 1}H")

    self._screen.dirty.clear()
    return "".join(output_parts)

def _render_line(self, line_no: int) -> str:
    """将 Screen 的一行转换为带 ANSI 属性的文本。"""
    line = self._screen.buffer[line_no]
    parts: list[str] = []
    prev_attrs = None

    for col in range(self._cols):
        char = line[col]
        # 构建 SGR 属性序列
        attrs = self._build_sgr(char)
        if attrs != prev_attrs:
            parts.append(attrs)
            prev_attrs = attrs
        parts.append(char.data)

    # 重置属性
    parts.append("\x1b[0m")
    return "".join(parts)

@staticmethod
def _build_sgr(char: pyte.screens.Char) -> str:
    """根据 Char 属性构建 SGR 转义序列。"""
    codes: list[str] = []
    if char.bold:
        codes.append("1")
    if char.italics:
        codes.append("3")
    if char.underscore:
        codes.append("4")
    if char.blink:
        codes.append("5")
    if char.reverse:
        codes.append("7")
    if char.strikethrough:
        codes.append("9")
    if char.fg != "default":
        codes.append(_color_to_sgr(char.fg, foreground=True))
    if char.bg != "default":
        codes.append(_color_to_sgr(char.bg, foreground=False))
    if not codes:
        return "\x1b[0m"
    return f"\x1b[{';'.join(codes)}m"

def resize(self, cols: int, rows: int) -> None:
    """调整虚拟 Screen 尺寸。"""
    if cols != self._cols or rows != self._rows:
        self._screen.resize(rows, cols)
        self._cols = cols
        self._rows = rows
```

#### 3.3.2 颜色映射辅助函数

```python
_BASIC_COLORS = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}

def _color_to_sgr(color: str, foreground: bool) -> str:
    """将 pyte 颜色值转换为 SGR 代码。"""
    base = 30 if foreground else 40

    # 基本 8 色
    if color in _BASIC_COLORS:
        return str(base + _BASIC_COLORS[color])

    # 256 色（pyte 返回 "000"~"255" 格式的字符串）
    try:
        idx = int(color)
        if 0 <= idx <= 255:
            prefix = 38 if foreground else 48
            return f"{prefix};5;{idx}"
    except ValueError:
        pass

    return ""
```

### 3.4 WebSocket 协议扩展

#### 3.4.1 Client → Server: resize 消息增加 client_id

```json
{"type": "resize", "cols": 80, "rows": 24}
```

后端在 `terminal_websocket()` 中已知每个 WebSocket 连接的 `client_id`（在 `add_ws_client` 时分配），所以无需前端改动，只需在后端处理时传入 `client_id`。

#### 3.4.2 Server → Client: 新增 resize_hint 消息

```json
{"type": "resize_hint", "effective_cols": 80, "effective_rows": 24}
```

当 min-size 策略生效时，通知客户端"当前有效终端尺寸"。前端可以据此：
- 在 StatusBar 显示 `有效尺寸: 80×24（受限于另一客户端）`
- 可选：调整 xterm.js 的 viewport

### 3.5 terminal.py WebSocket 端点改造

**现有代码**（`terminal.py:185-219`）：

```python
@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    await session.add_ws_client(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg_type == "resize":
                session.resize(cols, rows)  # ← 直写
    finally:
        session.remove_ws_client(websocket)
```

**改造为**：

```python
@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    client_id = await session.add_ws_client(websocket)  # 返回 client_id
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg_type == "resize":
                session.resize(cols, rows, client_id=client_id)  # 传入 client_id
    finally:
        session.remove_ws_client(client_id)  # 按 client_id 移除
```

---

## 4. 改动影响范围

### 4.1 后端改动清单

| 文件 | 改动 | 级别 |
|------|------|------|
| `src/services/terminal_manager.py` | `_ws_clients` 结构升级、新增 `VirtualTerminal`、resize 逻辑改造、输出路径改造 | **核心** |
| `src/api/terminal.py` | WebSocket 端点传入 `client_id`、`remove_ws_client` 签名变更 | 中等 |
| `src/services/virtual_terminal.py` | **新增文件** — VirtualTerminal 类 | 核心 |
| `requirements.txt` | 新增 `pyte>=0.8.0` 依赖 | 低 |
| `tests/test_terminal_backend.py` | 新增虚拟终端相关测试 | 中等 |

### 4.2 前端改动清单（最小化）

| 文件 | 改动 | 级别 |
|------|------|------|
| `frontend/src/hooks/useWebSocket.ts` | 处理 `resize_hint` 消息类型 | 低 |
| `frontend/src/components/TerminalView.tsx` | StatusBar 显示有效尺寸提示（可选） | 低 |

### 4.3 不受影响的模块

- tmux 模式的所有行为（VirtualTerminal 仅在 broker 模式启用）
- Agent 接口（`send_input`/`wait_for`/`read_screen` 不受影响）
- scrollback 缓冲区（`_append_scrollback` 仍然使用原始字节）
- 前端 `useTerminal.ts`（xterm.js 终端实例管理不变）

---

## 5. 数据流对比

### 5.1 改造前（当前直通模式）

```
PTY output (bytes)
    │
    ▼
_on_pty_readable()
    │
    ├── _append_scrollback(data)        # 字节级 scrollback
    ├── _raw_buffer.append(line)        # Agent 行级缓冲
    │
    ▼
_broadcast_output(text)                 # 原始文本 → 所有客户端
    │
    ├── ws_A.send_json({"output": text})
    └── ws_B.send_json({"output": text})  ← 相同内容
```

### 5.2 改造后（虚拟终端模式）

```
PTY output (bytes)
    │
    ▼
_on_pty_readable()
    │
    ├── _append_scrollback(data)        # 字节级 scrollback（不变）
    ├── _raw_buffer.append(line)        # Agent 行级缓冲（不变）
    │
    ▼
VirtualTerminal.feed_and_render(text)
    │
    ├── pyte.Stream.feed(text)          # ANSI 解析 → 更新字符矩阵
    ├── Screen.dirty → 变更行集合
    ├── _render_line() × dirty lines    # 字符矩阵 → ANSI 文本
    │
    ▼
_broadcast_output(rendered_ansi)        # 差分 ANSI → 所有客户端
    │
    ├── ws_A.send_json({"output": rendered_ansi})
    └── ws_B.send_json({"output": rendered_ansi})  ← 相同内容
```

---

## 6. 关键问题分析

### 6.1 为什么统一 Screen 而不是每客户端独立 Screen？

**因为 PTY fd 只有一个。**

程序（shell/vim/top）的输出是针对 PTY 尺寸格式化的。如果 PTY 是 80×24，`ls` 就会按 80 列排版。即使为 Browser A（120×30）维护一个 120×30 的 Screen，feed 进去的 `ls` 输出仍然是按 80 列排版的——多出的 40 列只是空白。

真正的独立渲染（像 tmux 那样让每个客户端看到不同排版的 `ls`）需要：
1. 为每个客户端创建独立的 PTY（方案 D）
2. 在每个 PTY 中运行独立的 shell/SSH 进程
3. 这就不再是"共享会话"了

**结论**：在保持共享会话语义的前提下，min-size + 统一 Screen 是最优解。

### 6.2 pyte 的性能影响

#### 解析开销估算

- pyte 是纯 Python 实现的 ANSI 状态机
- 典型终端输出速率：1~100 KB/s（交互式），高峰 1~10 MB/s（`cat` 大文件）
- pyte 在 Python 3.10+ 上的 feed 吞吐量约 5~20 MB/s（取决于 ANSI 序列复杂度）

#### 瓶颈场景

| 场景 | 输出速率 | pyte 能否跟上 | 说明 |
|------|----------|--------------|------|
| 普通交互（命令行、编辑器） | < 100 KB/s | ✅ 轻松 | 大部分时间屏幕变化很小 |
| `cat` 大文件 / `find /` | 1~10 MB/s | ⚠️ 可能有延迟 | 每次 feed 后 dirty 包含几乎所有行 |
| 高速日志流（`tail -f`） | 100 KB~1 MB/s | ✅ 可接受 | 通常只更新最后几行 |

#### 优化策略

1. **批量 feed**：`_on_pty_readable()` 每次读 64KB，一次性 feed，避免逐字符调用
2. **dirty 裁剪**：只渲染变更行，不全屏重绘
3. **高速输出降级**：当 dirty 行数 > 阈值（如 80%）时，跳过差分渲染，改为全屏 dump
4. **可选旁路**：为 `cat` 等突发高速场景设置输出速率检测，超过阈值时临时回退直通模式

### 6.3 已有 scrollback 缓冲区的兼容

当前 `_append_scrollback(data)` 保存的是 PTY **原始字节**（含 ANSI）。引入虚拟终端后：

- `_append_scrollback` **不变**：继续保存原始字节
- 新客户端连接时的历史回放路径需要改造：
  - **现有**：直接 `send_json({"output": scrollback.decode()})`
  - **改造后**：历史字节通过 `VirtualTerminal` 重新 feed 后渲染输出
  - 或者：仍然直传原始字节（xterm.js 本身能解析 ANSI），但可能与虚拟终端的当前状态不一致

**推荐方案**：新客户端连接时，先发送虚拟 Screen 的**全屏快照**（非增量），而不是原始 scrollback。这样客户端看到的是当前终端的精确状态。

```python
async def add_ws_client(self, ws: WebSocket) -> str:
    client_id = str(uuid.uuid4())
    client = ClientInfo(ws=ws, client_id=client_id)

    if self._vterm:
        # 发送虚拟 Screen 全屏快照
        snapshot = self._vterm.full_screen_dump()
        await ws.send_json({"type": "output", "data": snapshot})
    else:
        # 直通模式：发送原始 scrollback
        history = self.get_scrollback()
        if history:
            await ws.send_json({"type": "output", "data": history.decode(errors="replace")})

    self._ws_clients[client_id] = client
    return client_id
```

### 6.4 xterm.js 前端适配

虚拟终端的输出是**光标定位 + SGR 属性 + 文本**的 ANSI 序列，xterm.js 天然支持。前端无需解析或特殊处理，`terminal.write(data)` 即可正确渲染。

唯一需要注意的是：虚拟终端模式下，第一条消息不再是"历史流式回放"（原始 scrollback），而是"全屏快照"（一次性写入整个屏幕）。xterm.js 处理这两种情况的方式是相同的。

### 6.5 pyte 的已知限制

| 限制 | 影响 | 应对 |
|------|------|------|
| 不支持 True Color（24-bit） | vim 等启用 True Color 的程序颜色可能不准 | pyte 0.8+ 有部分支持；可自定义 `_build_sgr` 扩展 |
| 不支持某些 xterm 扩展序列 | 如 OSC 52（剪贴板）、bracketed paste mode | 这些序列不影响屏幕渲染，可在 feed 前过滤 |
| CJK 宽字符处理 | 中文字符占 2 列，pyte 需要正确处理 | pyte 0.7+ 已支持 `wcwidth`，但需验证 |
| 性能纯 Python | 高速输出场景有延迟 | 见 6.2 优化策略 |

---

## 7. 实施计划

### Sprint 3.5：虚拟终端渲染中间层（如果决定实施）

#### Task VT1: pyte 集成与 VirtualTerminal 基础实现（P0）

**范围**：
- 新增 `src/services/virtual_terminal.py`
- `VirtualTerminal` 类：`__init__` / `feed_and_render` / `resize` / `full_screen_dump`
- `_render_line` / `_build_sgr` / `_color_to_sgr` 辅助函数
- 单元测试：基础 feed → 渲染、dirty tracking、resize

**验收标准**：
- `VirtualTerminal` 可独立运行，不依赖 `TerminalSession`
- `feed_and_render("hello\r\nworld\r\n")` 返回正确的 ANSI 输出
- `resize(40, 12)` 后 Screen 尺寸正确

#### Task VT2: TerminalSession 客户端管理升级（P0）

**范围**：
- `_ws_clients` 从 `list[WebSocket]` 升级为 `dict[str, ClientInfo]`
- `add_ws_client` 返回 `client_id`
- `remove_ws_client` 按 `client_id` 移除
- `terminal.py` WebSocket 端点适配
- 兼容性：tmux 模式下行为不变

**验收标准**：
- 现有 44 个测试全部通过（不回归）
- 新增 ClientInfo 管理测试

#### Task VT3: resize min-size 策略实现（P0）

**范围**：
- `resize()` 方法改造：接受 `client_id`、计算 min-size、更新 PTY + Screen
- `_compute_min_size()` / `_set_pty_size()`
- `_broadcast_resize_hint()` 通知客户端
- 前端处理 `resize_hint` 消息

**验收标准**：
- 两个不同尺寸的客户端连接后，PTY 尺寸为 min(cols) × min(rows)
- 客户端断开后，尺寸自动调整为剩余客户端的 min-size
- 单客户端场景下行为与现有相同

#### Task VT4: 输出路径集成（P0）

**范围**：
- `_on_pty_readable()` 集成 `VirtualTerminal.feed_and_render()`
- Broker 模式自动启用虚拟终端
- `add_ws_client` 发送全屏快照
- Feature flag 控制（`BROKER_VTERM_ENABLED=true/false`）

**验收标准**：
- Broker 模式下终端输出正确渲染
- 新客户端连接时看到当前终端状态
- `cat` 大文件等高速场景不崩溃（可能有延迟，但不丢数据）

#### Task VT5: 回归测试与性能验证（P1）

**范围**：
- 扩展自动化测试覆盖虚拟终端路径
- 性能基准测试：feed 吞吐量、渲染延迟
- 手工验证：vim、top、less、中文输入输出

**验收标准**：
- 普通交互延迟 < 50ms
- 高速输出（`seq 1 100000`）不崩溃，延迟可接受
- vim、top 等全屏程序正确显示

---

## 8. 复杂度评估与建议

### 8.1 工作量估算

| 任务 | 预估工作量 | 风险 |
|------|-----------|------|
| VT1: VirtualTerminal 基础 | 1~2 天 | 低（pyte API 明确） |
| VT2: 客户端管理升级 | 0.5~1 天 | 中（需确保不回归） |
| VT3: min-size 策略 | 0.5~1 天 | 低 |
| VT4: 输出路径集成 | 1~2 天 | 高（ANSI 兼容性细节多） |
| VT5: 测试与验证 | 1~2 天 | 中（全屏程序兼容性） |
| **总计** | **4~8 天** | |

### 8.2 与简单方案的对比

| 维度 | 方案 B: min-size 直通 | 虚拟终端渲染中间层 |
|------|---------------------|-------------------|
| 工作量 | 0.5~1 天 | 4~8 天 |
| 多客户端 resize | ✅ 解决 | ✅ 解决 |
| 输出排版正确性 | ✅（PTY 按 min-size 排版） | ✅（同样是 min-size） |
| 差分输出优化 | ❌（原始流全量广播） | ✅（只发送变更行） |
| 全屏程序兼容性 | ✅（直通，原生 ANSI） | ⚠️（依赖 pyte 解析准确性） |
| 断线重连体验 | 原始 scrollback 回放 | 精确全屏快照恢复 |
| 未来扩展（会话录制/回放） | ❌ | ✅（Screen 状态可序列化） |
| 维护复杂度 | 极低 | 中高（ANSI 边界 case） |

### 8.3 推荐分阶段策略

**第一步（Sprint 3 收尾）：先做方案 B — min-size 直通**
- 工作量小（0.5~1 天），立即解决多客户端 resize 的核心痛点
- 不引入 pyte 依赖，不改变输出路径，风险极低
- 实现内容：`_ws_clients` 升级为 `dict[str, ClientInfo]`，resize 计算 min-size

**第二步（Sprint 4 或更后）：评估是否需要虚拟终端**
- 如果方案 B 已经满足实际使用场景 → 不需要虚拟终端
- 如果需要以下能力 → 引入虚拟终端：
  - 差分输出优化（降低带宽）
  - 精确的断线重连恢复（全屏快照）
  - 会话录制 / 回放 / 审计
  - 更精细的 Agent 屏幕感知

---

## 9. 决策点

在实施前，需要确认以下决策：

| # | 问题 | 选项 | 推荐 |
|---|------|------|------|
| 1 | 是否先做 min-size 直通（方案 B）作为第一步？ | 是 / 否 | **是** |
| 2 | 虚拟终端是否在本轮实施？ | 是 / 推迟到 Sprint 4 | **推迟**（先验证方案 B 是否足够） |
| 3 | pyte 版本 | 0.8.x（最新，dirty tracking 内置） | 0.8.x |
| 4 | Feature flag 命名 | `BROKER_VTERM_ENABLED` | ✅ |
| 5 | min-size 下限 | 10×3（避免异常小窗口） | ✅ |

---

## 10. 结论

**虚拟终端渲染中间层在技术上是可行的**，核心依赖 `pyte` 库提供的 ANSI 状态机和字符矩阵。但需要认识到：

1. **它不能让不同尺寸的客户端看到不同排版的输出**——因为 PTY fd 是共享的，程序输出只有一份。
2. **它解决的核心问题与方案 B（min-size 直通）相同**——都是通过统一 PTY 尺寸为 min-size 来避免排版错乱。
3. **它的额外价值在于**：差分输出、精确快照恢复、未来的录制/回放能力。
4. **代价是**：引入 pyte 依赖、增加 ANSI 解析/渲染的中间环节、潜在的兼容性风险。

**推荐策略**：先实施方案 B（min-size 直通），验证多客户端 resize 问题已解决后，再根据产品需求决定是否引入虚拟终端渲染中间层。
